"""Tier 1 query/sql tools: wren_query, wren_dry_plan, wren_dry_run, wren_list_models.

These mirror the wren-langchain / wren-pydantic SDK runtime tools 1:1. Each
tool is an async closure that captures the ServerState and offloads the
blocking engine call via _bridge.run_blocked (serialized by engine_lock).
Recoverable SQL errors return a success result containing the error envelope;
infra errors raise to the MCP layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from wren_mcp._bridge import run_blocked
from wren_mcp._envelope import make_error, make_success
from wren_mcp._format import (
    format_dry_plan_content,
    format_list_models_content,
    format_query_content,
)

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from wren_mcp._state import ServerState

# Hard cap for wren_query. The 16 KB content cap already truncates the rendered
# preview, but data.rows materializes every row via to_pylist() - a runaway
# limit would balloon memory before that cap fires. 1000 leaves headroom over
# the 100-default while keeping payloads bounded.
MAX_QUERY_ROWS = 1000


def register(mcp: FastMCP, state: ServerState) -> None:
    """Register the four query/sql tools on *mcp*."""

    @mcp.tool(
        description=(
            "Execute SQL through the Wren semantic layer and return rows. "
            "Use after wren_dry_plan looks correct. Default limit is 100 rows; "
            "increase only when needed. Hard cap is 1000 rows - beyond that, "
            "aggregate in SQL instead."
        )
    )
    async def wren_query(sql: str, limit: int = 100) -> dict[str, Any]:
        if limit < 1 or limit > MAX_QUERY_ROWS:
            return make_error(
                ValueError(
                    f"limit must be between 1 and {MAX_QUERY_ROWS} (got {limit}). "
                    "Aggregate in SQL if you need more rows."
                )
            )
        try:
            table = await run_blocked(state, state.query, sql, limit)
        except Exception as exc:
            return make_error(exc)

        content, warnings = format_query_content(table, total_rows=table.num_rows)
        data = {
            "columns": table.column_names,
            "rows": table.to_pylist(),
            "row_count": table.num_rows,
            "content_truncated": bool(warnings),
        }
        return make_success(content=content, data=data, warnings=warnings)

    @mcp.tool(
        description=(
            "Plan SQL through MDL and return the expanded target-dialect SQL, "
            "no DB round-trip. Use to verify your SQL targets Wren models "
            "correctly before running wren_query. Cheap."
        )
    )
    async def wren_dry_plan(sql: str) -> dict[str, Any]:
        try:
            dialect_sql = await run_blocked(state, state.dry_plan, sql)
        except Exception as exc:
            return make_error(exc)
        return make_success(
            content=format_dry_plan_content(dialect_sql),
            data={"dialect_sql": dialect_sql},
        )

    @mcp.tool(
        description=(
            "Validate SQL by planning then asking the DB to plan it without "
            "executing (LIMIT 0). Returns ok or a structured error. Use to "
            "check a query is executable before committing to wren_query."
        )
    )
    async def wren_dry_run(sql: str) -> dict[str, Any]:
        try:
            await run_blocked(state, state.dry_run, sql)
        except Exception as exc:
            return make_error(exc)
        return make_success(content="OK", data={"valid": True})

    @mcp.tool(
        description=(
            "List all models defined in this Wren project with column counts "
            "and descriptions. Zero-arg introspection - call first to discover "
            "what's queryable."
        )
    )
    async def wren_list_models() -> dict[str, Any]:
        try:
            manifest = await run_blocked(state, state.load_manifest)
        except Exception as exc:
            return make_error(exc)
        return make_success(
            content=format_list_models_content(manifest),
            data={"models": manifest.get("models", []) or []},
        )
