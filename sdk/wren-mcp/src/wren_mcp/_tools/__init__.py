"""Tool registration for the wren-mcp server.

Tools are registered as closures capturing the ServerState - no global state.
Registration is tiered:

  - Tier 1 (always on): query/sql (wren_query, wren_dry_plan, wren_dry_run,
    wren_list_models) + memory (auto-dropped when QDRANT_URL is unset;
    wren_store_query additionally dropped in read-only mode).
  - Tier 2 (config.tools == "all"): context, cube, profile (read + mutate),
    memory introspection + mutation, genbi deploy, types, ask/skills, docs.
    Side-effect / destructive tools (profile add/rm/switch, memory
    index/load/dump/forget/reset, genbi deploy, context build) are gated by
    config.read_only at call time - they return a read-only error envelope
    rather than disappearing, so agents get actionable feedback. The remaining
    long-running CLI commands (``wren memory watch``, ``wren genbi open``)
    block forever and have no MCP equivalent - run those from the CLI.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from wren_mcp._tools import (
    ask_skills,
    context,
    cube,
    docs,
    genbi,
    memory,
    memory_extra,
    memory_mutate,
    profile,
    profile_mutate,
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

    # Tier 2: full CLI surface (minus long-running: memory watch, genbi open).
    if state.config.tools == "all":
        context.register(mcp, state)
        cube.register(mcp, state)
        profile.register(mcp, state)
        profile_mutate.register(mcp, state)
        memory_extra.register(mcp, state)
        if state.memory_enabled:
            memory_mutate.register(mcp, state)
        genbi.register(mcp, state)
        types.register(mcp, state)
        ask_skills.register(mcp, state)
        docs.register(mcp, state)
