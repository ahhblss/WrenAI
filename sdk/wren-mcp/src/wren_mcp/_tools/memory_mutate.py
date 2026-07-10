"""Tier 2 memory mutation tools: index, load, dump, forget, reset.

Backed by the cached Qdrant ``MemoryStore`` (requires memory enabled at
startup, i.e. ``QDRANT_URL`` set). Registered only when memory is enabled -
like the Tier 1 memory tools. All write/delete tools are gated by
``config.read_only`` and return a read-only error envelope when disabled.

``wren_memory_reset`` is destructive (drops the derived Qdrant index) and
requires ``force=True`` - the ``knowledge/sql/*.md`` source of truth is never
touched; run ``wren_memory_index`` to rebuild.

``wren memory watch`` (long-running poller) has no MCP equivalent - it blocks
forever and would hold the engine lock. Run it from the CLI.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from wren_mcp._bridge import run_memory_blocked
from wren_mcp._envelope import make_error, make_success
from wren_mcp._format import format_memory_dump_content

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from wren_mcp._state import ServerState


def register(mcp: FastMCP, state: ServerState) -> None:
    project = state.project_path

    @mcp.tool(
        description=(
            "Rebuild the Qdrant semantic index from the project MDL: schema "
            "items + optional seed NL-SQL examples + knowledge/sql/*.md pairs. "
            "Call after wren_context_build so recall serves the current "
            "schema. Requires memory enabled. Writes the index (side effect)."
        )
    )
    async def wren_memory_index(
        seed_queries: bool = True,
        include_instructions: bool = True,
        load_markdown_pairs: bool = True,
    ) -> dict[str, Any]:
        if state.config.read_only:
            return make_error(
                RuntimeError("read-only mode: wren_memory_index is disabled")
            )
        if not state.memory_enabled:
            return make_error(
                RuntimeError(
                    "memory is not enabled (QDRANT_URL unset). "
                    "Install wrenai[memory] and set QDRANT_URL."
                )
            )

        def _index() -> dict[str, Any]:
            from wren.memory.markdown import load_query_pairs  # noqa: PLC0415

            manifest = state.load_manifest()
            if include_instructions:
                try:
                    from wren.context import load_rules  # noqa: PLC0415

                    instr, _used_legacy = load_rules(project)
                    if instr:
                        manifest["_instructions"] = instr
                except Exception:  # noqa: BLE001 - instructions are optional
                    pass  # never fail index because of them
            store = state.memory_store()
            result = store.index_schema(manifest, seed_queries=seed_queries)

            pairs_loaded, pairs_updated = 0, 0
            if load_markdown_pairs:
                md_pairs = load_query_pairs(project)
                if md_pairs:
                    res = store.load_queries(md_pairs, upsert=True)
                    pairs_loaded = res.get("loaded", 0)
                    pairs_updated = res.get("updated", 0)
            return {
                "schema_items": result.get("schema_items", 0),
                "seed_queries": result.get("seed_queries", 0),
                "pairs_loaded": pairs_loaded,
                "pairs_updated": pairs_updated,
            }

        try:
            result = await run_memory_blocked(state, _index)
        except Exception as exc:
            return make_error(exc)
        content = (
            f"Indexed {result['schema_items']} schema item(s)"
            + (
                f", {result['seed_queries']} seed querie(s)"
                if result["seed_queries"]
                else ""
            )
            + f", {result['pairs_loaded'] + result['pairs_updated']} markdown pair(s)."
        )
        return make_success(content=content, data=result)

    @mcp.tool(
        description=(
            "Import NL-SQL pairs into the Qdrant query history. Pass pairs as a "
            "list of {nl, sql, datasource?, source?, created_at?}. mode: skip "
            "(default, idempotent) | upsert (update sql for existing nl) | "
            "overwrite (clear same-source pairs first). dry_run validates and "
            "counts without writing. Requires memory enabled."
        )
    )
    async def wren_memory_load(
        pairs: list[dict[str, str]],
        mode: Literal["skip", "upsert", "overwrite"] = "skip",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        if state.config.read_only:
            return make_error(
                RuntimeError("read-only mode: wren_memory_load is disabled")
            )
        if not state.memory_enabled:
            return make_error(RuntimeError("memory is not enabled (QDRANT_URL unset)."))
        if not isinstance(pairs, list) or not pairs:
            return make_error(ValueError("pairs must be a non-empty list"))
        for i, p in enumerate(pairs):
            if not isinstance(p, dict) or "nl" not in p or "sql" not in p:
                return make_error(ValueError(f"pair #{i + 1} missing 'nl' or 'sql'"))

        if dry_run:
            from collections import Counter  # noqa: PLC0415

            sources = Counter(p.get("source", "user") for p in pairs)
            summary = ", ".join(f"{s}: {c}" for s, c in sources.items())
            return make_success(
                content=f"Would load {len(pairs)} pair(s) ({summary}) [{mode}]",
                data={"dry_run": True, "count": len(pairs), "mode": mode},
            )

        def _load() -> dict[str, Any]:
            store = state.memory_store()
            return store.load_queries(
                pairs,
                overwrite=(mode == "overwrite"),
                upsert=(mode == "upsert"),
            )

        try:
            result = await run_memory_blocked(state, _load)
        except Exception as exc:
            return make_error(exc)
        parts = []
        if result.get("loaded"):
            parts.append(f"{result['loaded']} new")
        if result.get("updated"):
            parts.append(f"{result['updated']} updated")
        if result.get("skipped"):
            parts.append(f"{result['skipped']} skipped")
        total = result.get("loaded", 0) + result.get("updated", 0)
        return make_success(
            content=f"Loaded {total} pair(s) ({', '.join(parts) or '0'}).",
            data={"result": result, "mode": mode},
        )

    @mcp.tool(
        description=(
            "Export stored NL-SQL pairs from the Qdrant query history as a list "
            "of {nl, sql, datasource, source, created_at}. Filter by source "
            "(seed/user/view). Read-only. Requires memory enabled."
        )
    )
    async def wren_memory_dump(
        source: Literal["seed", "user", "view"] | None = None,
    ) -> dict[str, Any]:
        if not state.memory_enabled:
            return make_error(RuntimeError("memory is not enabled (QDRANT_URL unset)."))

        def _dump() -> list[dict[str, Any]]:
            return state.memory_store().dump_queries(source=source)

        try:
            rows = await run_memory_blocked(state, _dump)
        except Exception as exc:
            return make_error(exc)
        content, warnings = format_memory_dump_content(rows)
        return make_success(
            content=content,
            data={"pairs": rows, "count": len(rows)},
            warnings=warnings,
        )

    @mcp.tool(
        description=(
            "Remove NL-SQL pairs from the Qdrant query history. Pass ids "
            "(Qdrant point IDs) OR a source to batch-delete all pairs of that "
            "source (seed/user/view). ids and source are mutually exclusive. "
            "Requires memory enabled."
        )
    )
    async def wren_memory_forget(
        ids: list[str] | None = None,
        source: Literal["seed", "user", "view"] | None = None,
    ) -> dict[str, Any]:
        if state.config.read_only:
            return make_error(
                RuntimeError("read-only mode: wren_memory_forget is disabled")
            )
        if not state.memory_enabled:
            return make_error(RuntimeError("memory is not enabled (QDRANT_URL unset)."))
        if ids and source:
            return make_error(
                ValueError("ids and source are mutually exclusive; pass one.")
            )
        if not ids and not source:
            return make_error(
                ValueError("pass ids (list of point IDs) or source to forget.")
            )

        def _forget() -> int:
            store = state.memory_store()
            if ids:
                return store.forget_queries_by_ids(ids)
            return store.forget_queries_by_source(source)  # type: ignore[arg-type]

        try:
            deleted = await run_memory_blocked(state, _forget)
        except Exception as exc:
            return make_error(exc)
        scope = "ids" if ids else f"source:{source}"
        return make_success(
            content=f"Forgot {deleted} pair(s) by {scope}.",
            data={"deleted": deleted, "by": scope},
        )

    @mcp.tool(
        description=(
            "Drop the derived Qdrant memory index (schema + query history "
            "collections). Destructive and irreversible for the index - but "
            "knowledge/sql/*.md source files are preserved; run "
            "wren_memory_index to rebuild. Requires force=true as "
            "confirmation. Requires memory enabled."
        )
    )
    async def wren_memory_reset(force: bool = False) -> dict[str, Any]:
        if state.config.read_only:
            return make_error(
                RuntimeError("read-only mode: wren_memory_reset is disabled")
            )
        if not state.memory_enabled:
            return make_error(RuntimeError("memory is not enabled (QDRANT_URL unset)."))
        if not force:
            return make_error(
                ValueError(
                    "wren_memory_reset is destructive: pass force=true to "
                    "confirm. knowledge/sql/*.md is preserved; the index can be "
                    "rebuilt with wren_memory_index."
                )
            )

        def _reset() -> None:
            state.memory_store().reset()

        try:
            await run_memory_blocked(state, _reset)
        except Exception as exc:
            return make_error(exc)
        return make_success(
            content="Memory index reset. Run wren_memory_index to rebuild.",
            data={"reset": True},
        )
