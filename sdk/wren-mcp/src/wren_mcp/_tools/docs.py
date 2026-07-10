"""Tier 2 docs tool: connection_info.

Show connection-info fields for each data source (or one), as markdown or JSON
schema. Drives dynamic connection forms. Read-only / pure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from wren_mcp._bridge import run_blocked
from wren_mcp._envelope import make_error, make_success

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from wren_mcp._state import ServerState


def register(mcp: FastMCP, state: ServerState) -> None:
    @mcp.tool(
        description=(
            "Show connection-info fields for each data source (or a single one "
            "via datasource, e.g. postgres/mysql). format='md' returns markdown "
            "docs, 'json' returns the JSON schema. Read-only."
        )
    )
    async def wren_docs_connection_info(
        datasource: str | None = None,
        format: Literal["md", "json"] = "md",
    ) -> dict[str, Any]:
        def _gen() -> str:
            from wren.docs import (  # noqa: PLC0415
                generate_json_schema,
                generate_markdown,
            )

            if format == "md":
                return generate_markdown(datasource)
            return generate_json_schema(datasource, envelope=False)

        try:
            text = await run_blocked(state, _gen)
        except Exception as exc:
            return make_error(exc)
        return make_success(
            content=text[:200] + ("..." if len(text) > 200 else ""),
            data={"datasource": datasource, "format": format, "length": len(text)},
        )
