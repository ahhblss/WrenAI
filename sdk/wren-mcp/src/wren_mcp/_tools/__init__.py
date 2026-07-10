"""Tool registration for the wren-mcp server.

Tools are registered as closures capturing the ServerState - no global state.
Registration is tiered:

  - Tier 1 (always on): query/sql (wren_query, wren_dry_plan, wren_dry_run,
    wren_list_models) + memory (auto-dropped when QDRANT_URL is unset;
    wren_store_query additionally dropped in read-only mode).
  - Tier 2 (config.tools == "all"): context, cube, profile, memory
    introspection, types, ask/skills, docs - the extended CLI surface, minus
    long-running / destructive commands (memory watch/reset/index, genbi
    open/deploy, profile add/rm/switch, memory load/dump/forget).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from wren_mcp._tools import (
    ask_skills,
    context,
    cube,
    docs,
    memory,
    memory_extra,
    profile,
    query,
    types,
)

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from wren_mcp._state import ServerState


def register_all(mcp: FastMCP, state: ServerState) -> None:
    """Register tools on the FastMCP app, scoped to config.tools tier."""
    # Tier 1: always on.
    query.register(mcp, state)
    if state.memory_enabled:
        memory.register(mcp, state)

    # Tier 2: full CLI surface (minus long-running/destructive).
    if state.config.tools == "all":
        context.register(mcp, state)
        cube.register(mcp, state)
        profile.register(mcp, state)
        memory_extra.register(mcp, state)
        types.register(mcp, state)
        ask_skills.register(mcp, state)
        docs.register(mcp, state)
