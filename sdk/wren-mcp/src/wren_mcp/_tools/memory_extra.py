"""Tier 2 memory introspection tools: describe, status.

``wren_memory_describe`` is a pure function (no Qdrant) - it renders the full
schema as structured plain text. ``wren_memory_status`` queries the live
MemoryStore for backend + index stats (requires memory enabled).

Both are read-only. The mutation tools (index/load/dump/reset) are not exposed
in v1 - ``wren memory index`` is a long-running operation best run from the
CLI; reset is destructive.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from wren_mcp._bridge import run_memory_blocked
from wren_mcp._envelope import make_error, make_success

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from wren_mcp._state import ServerState


def register(mcp: FastMCP, state: ServerState) -> None:
    @mcp.tool(
        description=(
            "Print the full MDL schema as structured plain text. No embedding "
            "or vector store needed - pure function. Useful as a fallback when "
            "semantic memory (wren_fetch_context) is not available."
        )
    )
    async def wren_memory_describe() -> dict[str, Any]:
        def _describe() -> str:
            from wren.memory.schema_indexer import describe_schema  # noqa: PLC0415

            return describe_schema(state.load_manifest())

        try:
            text = await run_memory_blocked(state, _describe)
        except Exception as exc:
            return make_error(exc)
        return make_success(
            content=text[:200] + ("..." if len(text) > 200 else ""),
            data={"length": len(text)},
        )

    @mcp.tool(
        description=(
            "Show the memory backend (qdrant) and index statistics (collection "
            "row counts). Requires memory enabled (QDRANT_URL set). Read-only."
        )
    )
    async def wren_memory_status() -> dict[str, Any]:
        if not state.memory_enabled:
            return make_error(
                RuntimeError(
                    "memory is not enabled (QDRANT_URL unset). "
                    "Install wrenai[memory] and run `wren memory index`."
                )
            )

        def _status() -> dict[str, Any]:
            return state.memory_store().status()

        try:
            result = await run_memory_blocked(state, _status)
        except Exception as exc:
            return make_error(exc)
        return make_success(content=str(result), data=result)
