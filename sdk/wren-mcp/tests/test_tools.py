"""Unit tests: tool registration tiers + envelope output (in-process)."""

from __future__ import annotations

from wren_mcp._app import build_mcp
from wren_mcp._state import ServerConfig


async def test_tier1_registers_core_four_and_no_memory(tmp_project):
    """Tier 1 exposes the 4 query tools; memory is auto-dropped (no QDRANT_URL)."""
    mcp = build_mcp(ServerConfig(project_path=tmp_project, token="t", tools="tier1"))
    names = {t.name for t in await mcp.list_tools()}
    assert {
        "wren_query",
        "wren_dry_plan",
        "wren_dry_run",
        "wren_list_models",
    } <= names
    assert "wren_fetch_context" not in names
    assert "wren_recall_queries" not in names
    assert "wren_store_query" not in names


async def test_all_tier_registers_extended_surface(tmp_project):
    """Tier 2 adds context / cube / profile / memory / types / ask / docs tools."""
    mcp = build_mcp(ServerConfig(project_path=tmp_project, token="t", tools="all"))
    names = {t.name for t in await mcp.list_tools()}
    assert {
        "wren_context_show",
        "wren_context_build",
        "wren_context_validate",
        "wren_cube_list",
        "wren_cube_query",
        "wren_profile_list",
        "wren_profile_debug",
        "wren_memory_describe",
        "wren_parse_type",
        "wren_translate_type",
        "wren_ask",
        "wren_skills_list",
        "wren_docs_connection_info",
    } <= names
    assert len(names) >= 20


async def test_list_models_returns_envelope(tmp_project):
    """wren_list_models returns a success envelope with the manifest models."""
    mcp = build_mcp(ServerConfig(project_path=tmp_project, token="t", tools="tier1"))
    _content, structured = await mcp.call_tool("wren_list_models", {})
    assert structured["ok"] is True
    assert structured["data"]["models"] == []
    assert structured["warnings"] == []


async def test_parse_type_normalizes(tmp_project):
    """wren_parse_type normalizes a postgres type via sqlglot."""
    mcp = build_mcp(ServerConfig(project_path=tmp_project, token="t", tools="all"))
    _content, structured = await mcp.call_tool(
        "wren_parse_type", {"type": "character varying(255)", "dialect": "postgres"}
    )
    assert structured["ok"] is True
    assert structured["data"]["normalized"] == "VARCHAR(255)"


async def test_query_limit_validation_error_envelope(tmp_project):
    """An out-of-range limit returns an error envelope, not a raised exception."""
    mcp = build_mcp(ServerConfig(project_path=tmp_project, token="t", tools="tier1"))
    _content, structured = await mcp.call_tool(
        "wren_query", {"sql": "SELECT 1", "limit": 99999}
    )
    assert structured["ok"] is False
    assert "limit" in structured["error"]["message"]


async def test_read_only_drops_store_query_and_build(tmp_project):
    """read_only drops wren_store_query (memory) and blocks wren_context_build."""
    import os

    os.environ["QDRANT_URL"] = "http://localhost:6333"  # enable memory tier
    try:
        mcp = build_mcp(
            ServerConfig(project_path=tmp_project, token="t", tools="all", read_only=True)
        )
        names = {t.name for t in await mcp.list_tools()}
        assert "wren_recall_queries" in names  # read memory tool kept
        assert "wren_store_query" not in names  # write tool dropped
        # build is a side-effect tool: registered, but returns an error envelope
        _c, structured = await mcp.call_tool("wren_context_build", {})
        assert structured["ok"] is False
        assert "read-only" in structured["error"]["message"]
    finally:
        del os.environ["QDRANT_URL"]
