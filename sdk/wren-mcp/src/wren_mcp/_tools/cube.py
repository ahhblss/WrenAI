"""Tier 2 cube tools: list, describe, query.

Cubes are structured measure/dimension queries. ``wren_cube_query`` translates
a CubeQuery to SQL via wren-core (cube_query_to_sql), then either returns the
SQL (sql_only=True, no DB) or executes it through WrenEngine.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from wren_mcp._bridge import run_blocked
from wren_mcp._envelope import make_error, make_success
from wren_mcp._format import format_dry_plan_content, format_query_content

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from wren_mcp._state import ServerState


def register(mcp: FastMCP, state: ServerState) -> None:
    @mcp.tool(
        description=(
            "List all cubes defined in the project MDL with their measures, "
            "dimensions, and time dimensions. Read-only introspection."
        )
    )
    async def wren_cube_list() -> dict[str, Any]:
        def _list() -> list[dict[str, Any]]:
            manifest = state.load_manifest()
            return manifest.get("cubes", []) or []

        try:
            cubes = await run_blocked(state, _list)
        except Exception as exc:
            return make_error(exc)
        summary = ", ".join(c.get("name", "?") for c in cubes) or "(no cubes)"
        return make_success(content=summary, data={"cubes": cubes})

    @mcp.tool(
        description=(
            "Print the full schema for one cube (measures/dimensions/time "
            "dimensions). Read-only."
        )
    )
    async def wren_cube_describe(name: str) -> dict[str, Any]:
        def _describe() -> dict[str, Any] | None:
            manifest = state.load_manifest()
            return next(
                (c for c in manifest.get("cubes", []) if c.get("name") == name),
                None,
            )

        try:
            cube = await run_blocked(state, _describe)
        except Exception as exc:
            return make_error(exc)
        if cube is None:
            return make_error(ValueError(f"cube {name!r} not found"))
        return make_success(content=cube.get("name", name), data={"cube": cube})

    @mcp.tool(
        description=(
            "Execute a structured cube query (measures/dimensions/filters) or, "
            "with sql_only=true, emit just the generated SQL without a DB "
            "round-trip. measures is a list of measure names; dimensions a "
            "list of dimension names; time_dimensions/filters are lists of "
            "{dimension, granularity, dateRange?} / {dimension, operator, value?}."
        )
    )
    async def wren_cube_query(
        cube: str,
        measures: list[str],
        dimensions: list[str] | None = None,
        time_dimensions: list[dict[str, Any]] | None = None,
        filters: list[dict[str, Any]] | None = None,
        limit: int | None = None,
        offset: int | None = None,
        sql_only: bool = False,
    ) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            from wren_core import cube_query_to_sql  # noqa: PLC0415

            cube_query: dict[str, Any] = {"cube": cube, "measures": measures}
            if dimensions:
                cube_query["dimensions"] = dimensions
            if time_dimensions:
                cube_query["timeDimensions"] = time_dimensions
            if filters:
                cube_query["filters"] = filters
            if limit is not None:
                cube_query["limit"] = limit
            if offset is not None:
                cube_query["offset"] = offset

            mdl_json = json.dumps(state.load_manifest())
            sql = cube_query_to_sql(json.dumps(cube_query), mdl_json)
            if sql_only:
                return {"sql": sql, "table": None}
            table = state.query(sql)
            return {"sql": sql, "table": table}

        try:
            result = await run_blocked(state, _run)
        except Exception as exc:
            return make_error(exc)

        if result["table"] is None:
            return make_success(
                content=format_dry_plan_content(result["sql"]),
                data={"sql": result["sql"]},
            )
        table = result["table"]
        content, warnings = format_query_content(table, total_rows=table.num_rows)
        return make_success(
            content=content,
            data={
                "sql": result["sql"],
                "columns": table.column_names,
                "rows": table.to_pylist(),
                "row_count": table.num_rows,
            },
            warnings=warnings,
        )
