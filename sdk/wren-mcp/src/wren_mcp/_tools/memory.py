"""Tier 1 memory tools: wren_fetch_context, wren_recall_queries, wren_store_query.

Registered only when memory is enabled (QDRANT_URL set at startup).
wren_store_query is additionally dropped when config.read_only is set, matching
the SDK include_memory_write=False pattern. Memory calls go through the cached
MemoryStore (Qdrant + Volcengine Ark embeddings) and are serialized by the
engine_lock alongside engine calls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from wren_mcp._bridge import run_memory_blocked
from wren_mcp._envelope import make_error, make_success
from wren_mcp._format import (
    format_fetch_context_content,
    format_recall_content,
    format_store_content,
)

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from wren_mcp._state import ServerState


def register(mcp: FastMCP, state: ServerState) -> None:
    """Register the memory tools on *mcp*. Caller checks state.memory_enabled."""

    @mcp.tool(
        description=(
            "Fetch relevant schema and business context for an analytical "
            "question via embedding search. Call this BEFORE writing SQL so "
            "you query the correct Wren models and columns. Use item_type to "
            "narrow scope (model/column/relationship/view) and model to narrow "
            "to a single model when known."
        )
    )
    async def wren_fetch_context(
        question: str,
        limit: int = 5,
        item_type: Literal["model", "column", "relationship", "view"] | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        def _fetch() -> dict[str, Any]:
            store = state.memory_store()
            manifest = state.load_manifest()
            kwargs: dict[str, Any] = {
                "query": question,
                "manifest": manifest,
                "limit": limit,
            }
            if item_type is not None:
                kwargs["item_type"] = item_type
            if model is not None:
                kwargs["model_name"] = model
            return store.get_context(**kwargs)

        try:
            result = await run_memory_blocked(state, _fetch)
        except Exception as exc:
            return make_error(exc)
        return make_success(
            content=format_fetch_context_content(result),
            data=result,
        )

    @mcp.tool(
        description=(
            "Recall up to limit past NL->SQL pairs similar to question, for "
            "use as few-shot examples before writing new SQL. Pairs were "
            "previously confirmed by users or seeded for the project."
        )
    )
    async def wren_recall_queries(question: str, limit: int = 3) -> dict[str, Any]:
        def _recall() -> list[dict[str, Any]]:
            return state.memory_store().recall_queries(query=question, limit=limit)

        try:
            rows = await run_memory_blocked(state, _recall)
        except Exception as exc:
            return make_error(exc)
        return make_success(
            content=format_recall_content(rows),
            data={"results": rows},
        )

    if not state.config.read_only:

        @mcp.tool(
            description=(
                "Save a confirmed natural-language -> SQL pair for future "
                "recall. Call AFTER wren_query succeeds and the result was "
                "useful, so future runs can recall the example. Tags must not "
                "contain commas (reserved as the storage separator)."
            )
        )
        async def wren_store_query(
            nl: str,
            sql: str,
            tags: list[str] | None = None,
        ) -> dict[str, Any]:
            tags_list = tags or []
            # Tags are comma-joined before storage; a tag containing a comma
            # would silently split on every future consumer. Reject early.
            for tag in tags_list:
                if "," in tag:
                    return make_error(
                        ValueError(
                            f"tag {tag!r} contains a comma; commas are reserved "
                            "as the separator for the underlying storage format. "
                            "Replace commas with dashes or spaces."
                        )
                    )

            def _store() -> None:
                tag_str = ",".join(tags_list) if tags_list else None
                state.memory_store().store_query(
                    nl_query=nl, sql_query=sql, tags=tag_str
                )

            try:
                await run_memory_blocked(state, _store)
            except Exception as exc:
                return make_error(exc)
            return make_success(
                content=format_store_content(nl, sql, tags_list),
                data={"nl": nl, "sql": sql, "tags": tags_list},
            )
