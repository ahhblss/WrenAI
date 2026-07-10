"""Tier 2 ask + skills tools.

``wren_ask`` wraps a natural-language prompt into a processed agent prompt
(guided = strict flow for weaker LLMs, direct = minimal for stronger).
``wren_skills_get`` / ``wren_skills_list`` serve bundled agent skill guides so
content always matches the installed wren version. All read-only / pure.
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
            "Wrap a natural-language prompt into a processed prompt for an "
            "agent. mode='guided' uses a strict task flow (for weaker LLMs); "
            "mode='direct' is minimal wrapping (for stronger LLMs). Produces a "
            "prompt string, executes nothing."
        )
    )
    async def wren_ask(
        prompt: str,
        mode: Literal["guided", "direct"],
    ) -> dict[str, Any]:
        def _render() -> str:
            from wren.ask import render  # noqa: PLC0415

            return render(mode, prompt)

        try:
            rendered = await run_blocked(state, _render)
        except Exception as exc:
            return make_error(exc)
        return make_success(
            content=rendered, data={"mode": mode, "length": len(rendered)}
        )

    @mcp.tool(
        description=(
            "List bundled Wren agent skills (name, summary, reference docs, "
            "scripts). Read-only."
        )
    )
    async def wren_skills_list() -> dict[str, Any]:
        def _list() -> list[dict[str, Any]]:
            from wren.skills_delivery import list_skills  # noqa: PLC0415

            return [
                {
                    "name": s.name,
                    "summary": s.summary,
                    "references": s.references,
                    "scripts": s.scripts,
                }
                for s in list_skills()
            ]

        try:
            skills = await run_blocked(state, _list)
        except Exception as exc:
            return make_error(exc)
        return make_success(
            content=", ".join(s["name"] for s in skills) or "(no skills)",
            data={"skills": skills},
        )

    @mcp.tool(
        description=(
            "Fetch a bundled Wren agent skill guide (or a reference doc set "
            "with full=true). Content always matches the installed wren version."
        )
    )
    async def wren_skills_get(name: str, full: bool = False) -> dict[str, Any]:
        def _get() -> str:
            from wren.skills_delivery import get_skill  # noqa: PLC0415

            return get_skill(name, full=full)

        try:
            content = await run_blocked(state, _get)
        except Exception as exc:
            return make_error(exc)
        return make_success(content=content, data={"name": name, "full": full})
