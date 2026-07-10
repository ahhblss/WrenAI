"""Tier 2 type tools: parse_type, translate_type.

Pure deterministic transforms over SQL type strings via sqlglot. Useful for
normalizing vendor-specific types across the 22+ data sources Wren supports.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from wren_mcp._envelope import make_error, make_success

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from wren_mcp._state import ServerState


def register(mcp: FastMCP, state: ServerState) -> None:
    @mcp.tool(
        description=(
            "Normalize a single SQL type string to sqlglot canonical form for a "
            "dialect (e.g. postgres 'character varying(255)' -> 'VARCHAR(255)'). "
            "Falls back to the original string if parsing fails."
        )
    )
    async def wren_parse_type(type: str, dialect: str) -> dict[str, Any]:
        try:
            from wren.type_mapping import parse_type  # noqa: PLC0415

            result = parse_type(type, dialect)
        except Exception as exc:
            return make_error(exc)
        return make_success(
            content=result,
            data={"type": type, "dialect": dialect, "normalized": result},
        )

    @mcp.tool(
        description=(
            "Translate a SQL type string from one dialect to another (e.g. "
            "postgres 'int8' -> bigquery 'INT64'). Maps vendor-specific "
            "spellings across engines. Falls back to the original if parsing fails."
        )
    )
    async def wren_translate_type(
        type: str, source: str, target: str
    ) -> dict[str, Any]:
        try:
            from wren.type_mapping import translate_type  # noqa: PLC0415

            result = translate_type(type, source, target)
        except Exception as exc:
            return make_error(exc)
        return make_success(
            content=result,
            data={
                "type": type,
                "source": source,
                "target": target,
                "translated": result,
            },
        )
