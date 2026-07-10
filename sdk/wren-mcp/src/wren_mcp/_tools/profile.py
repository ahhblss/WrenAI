"""Tier 2 profile tools: list, debug.

Read-only profile introspection. ``wren_profile_debug`` masks sensitive fields
(password/token/credential) via redact_secrets so it's safe to return to
clients. Mutation tools (add/rm/switch) are intentionally NOT exposed over MCP
in v1 - they change global ~/.wren state and belong to the CLI.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from wren_mcp._bridge import run_blocked
from wren_mcp._envelope import make_error, make_success, redact_secrets

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from wren_mcp._state import ServerState


def register(mcp: FastMCP, state: ServerState) -> None:
    @mcp.tool(
        description=(
            "List all connection profiles from ~/.wren/profiles.yml, marking "
            "the active one. Read-only."
        )
    )
    async def wren_profile_list() -> dict[str, Any]:
        def _list() -> list[dict[str, Any]]:
            from wren.profile import get_active_name, list_profiles

            active = get_active_name()
            return [
                {"name": n, "datasource": p.get("datasource"), "active": n == active}
                for n, p in list_profiles().items()
            ]

        try:
            profiles = await run_blocked(state, _list)
        except Exception as exc:
            return make_error(exc)
        summary = ", ".join(p["name"] for p in profiles) or "(no profiles)"
        return make_success(content=summary, data={"profiles": profiles})

    @mcp.tool(
        description=(
            "Show a profile's resolved config with sensitive fields "
            "(password/token/credential) masked. Safe to return to clients. "
            "Defaults to the active profile. Read-only."
        )
    )
    async def wren_profile_debug(name: str | None = None) -> dict[str, Any]:
        def _debug() -> dict[str, Any]:
            from wren.profile import get_active_name, list_profiles

            resolved = name if name is not None else get_active_name()
            profiles = list_profiles()
            if resolved is None or resolved not in profiles:
                raise ValueError(
                    f"profile {resolved!r} not found in ~/.wren/profiles.yml"
                )
            active = get_active_name()
            return {
                "name": resolved,
                "active": resolved == active,
                "datasource": profiles[resolved].get("datasource"),
                "profile": redact_secrets(dict(profiles[resolved])),
            }

        try:
            result = await run_blocked(state, _debug)
        except Exception as exc:
            return make_error(exc)
        return make_success(
            content=f"profile {result['name']} (active={result['active']}, datasource={result['datasource']!r})",
            data=result,
        )
