"""Unit tests: tool registration tiers + envelope output (in-process)."""

from __future__ import annotations

import pytest

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


async def test_context_instructions_handles_no_rules(tmp_project):
    """wren_context_instructions returns ok on a project with no rules.

    Regression: load_rules returns None content for a rule-less project, and
    the tool used to crash on len(None). Now it returns an ok envelope.
    """
    mcp = build_mcp(ServerConfig(project_path=tmp_project, token="t", tools="all"))
    _c, res = await mcp.call_tool("wren_context_instructions", {})
    assert res["ok"] is True
    assert "length" in res["data"]


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
            ServerConfig(
                project_path=tmp_project, token="t", tools="all", read_only=True
            )
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


# ── Side-effect tool registration (Tier 2) ────────────────────────────────


async def test_mutation_tools_registered_tier2(tmp_project):
    """profile_mutate + genbi deploy register on Tier 2 without memory."""
    mcp = build_mcp(ServerConfig(project_path=tmp_project, token="t", tools="all"))
    names = {t.name for t in await mcp.list_tools()}
    assert {
        "wren_profile_add",
        "wren_profile_remove",
        "wren_profile_switch",
        "wren_genbi_deploy",
    } <= names
    # memory_mutate needs memory enabled -> not registered here.
    assert "wren_memory_index" not in names
    assert "wren_memory_reset" not in names


async def test_memory_mutate_registered_when_enabled(tmp_project, monkeypatch):
    """memory_mutate tools register when QDRANT_URL is set."""
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    mcp = build_mcp(ServerConfig(project_path=tmp_project, token="t", tools="all"))
    names = {t.name for t in await mcp.list_tools()}
    assert {
        "wren_memory_index",
        "wren_memory_load",
        "wren_memory_dump",
        "wren_memory_forget",
        "wren_memory_reset",
    } <= names


# ── read-only guards ──────────────────────────────────────────────────────


async def test_read_only_blocks_mutation_tools(tmp_project, monkeypatch):
    """Every side-effect tool returns a read-only error envelope."""
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    mcp = build_mcp(
        ServerConfig(project_path=tmp_project, token="t", tools="all", read_only=True)
    )
    calls = [
        ("wren_profile_add", {"name": "p", "datasource": "duckdb", "validate": False}),
        ("wren_profile_remove", {"name": "p"}),
        ("wren_profile_switch", {"name": "p"}),
        ("wren_genbi_deploy", {"name": "a"}),
        ("wren_memory_index", {}),
        ("wren_memory_load", {"pairs": [{"nl": "q", "sql": "SELECT 1"}]}),
        ("wren_memory_forget", {"source": "user"}),
        ("wren_memory_reset", {"force": True}),
    ]
    for name, args in calls:
        _c, structured = await mcp.call_tool(name, args)
        assert structured["ok"] is False, name
        assert "read-only" in structured["error"]["message"], name


# ── profile round-trip (mocked ~/.wren) ───────────────────────────────────


def _patch_profiles(monkeypatch):
    """Redirect wren.profile storage to an in-memory dict (no ~/.wren writes)."""
    import wren.profile

    state = {"active": None, "profiles": {}}
    monkeypatch.setattr(wren.profile, "_load_raw", lambda: state)
    monkeypatch.setattr(wren.profile, "_save_raw", lambda data: state.update(data))
    return state


async def test_profile_add_remove_roundtrip(tmp_project, monkeypatch):
    state = _patch_profiles(monkeypatch)
    mcp = build_mcp(ServerConfig(project_path=tmp_project, token="t", tools="all"))

    _c, add = await mcp.call_tool(
        "wren_profile_add",
        {
            "name": "p1",
            "datasource": "duckdb",
            "fields": {"path": ":memory:"},
            "activate": True,
            "validate": False,
        },
    )
    assert add["ok"] is True
    assert state["profiles"]["p1"]["datasource"] == "duckdb"
    assert state["active"] == "p1"

    _c, rm = await mcp.call_tool("wren_profile_remove", {"name": "p1"})
    assert rm["ok"] is True
    assert "p1" not in state["profiles"]


async def test_profile_add_validation_warning(tmp_project, monkeypatch):
    """A failed connection validation is a warning, not an error; profile is kept."""
    state = _patch_profiles(monkeypatch)
    import wren_mcp._tools.profile_mutate as pm

    monkeypatch.setattr(
        pm, "_validate_connection", lambda name: (False, "connection failed: boom")
    )
    mcp = build_mcp(ServerConfig(project_path=tmp_project, token="t", tools="all"))
    _c, res = await mcp.call_tool(
        "wren_profile_add", {"name": "p1", "datasource": "duckdb", "validate": True}
    )
    assert res["ok"] is True  # profile saved despite validation failure
    assert res["data"]["validated"] is False
    assert res["data"]["validation_error"] == "connection failed: boom"
    assert any("validation failed" in w for w in res["warnings"])
    assert "p1" in state["profiles"]


async def test_profile_switch_unknown_returns_error(tmp_project, monkeypatch):
    _patch_profiles(monkeypatch)
    mcp = build_mcp(ServerConfig(project_path=tmp_project, token="t", tools="all"))
    _c, res = await mcp.call_tool("wren_profile_switch", {"name": "nope"})
    assert res["ok"] is False
    assert "not found" in res["error"]["message"]


# ── memory mutation (fake store) ──────────────────────────────────────────


class _FakeStore:
    def __init__(self):
        self.pairs = []
        self.reset_called = False

    def index_schema(self, manifest, *, seed_queries=True, **kw):
        return {"schema_items": 3, "seed_queries": 2 if seed_queries else 0}

    def load_queries(self, pairs, *, overwrite=False, upsert=False):
        self.pairs.extend(pairs)
        return {"loaded": len(pairs), "updated": 0, "skipped": 0}

    def dump_queries(self, *, source=None):
        return [
            {"nl_query": "q1", "sql_query": "SELECT 1", "datasource": "", "tags": ""}
        ]

    def forget_queries_by_ids(self, ids):
        return len(ids)

    def forget_queries_by_source(self, source):
        return 1

    def reset(self):
        self.reset_called = True


class _FakeMemory:
    enabled = True

    def __init__(self):
        self._store = _FakeStore()

    def open(self):
        return self._store


@pytest.fixture
def memory_mcp(tmp_project, monkeypatch):
    """Tier-2 mcp with memory enabled but backed by an in-memory fake store."""
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    mcp = build_mcp(ServerConfig(project_path=tmp_project, token="t", tools="all"))
    # Inject a fake MemoryStore directly into the single-project state's cache
    # so memory tools exercise the fake without a real Qdrant.
    state = mcp._wren_ctx.registry.default_state
    monkeypatch.setattr(state, "_memory_store_cache", _FakeStore())
    return mcp


async def test_memory_reset_requires_force(memory_mcp):
    _c, res = await memory_mcp.call_tool("wren_memory_reset", {"force": False})
    assert res["ok"] is False
    assert "force=true" in res["error"]["message"]


async def test_memory_reset_with_force(memory_mcp):
    _c, res = await memory_mcp.call_tool("wren_memory_reset", {"force": True})
    assert res["ok"] is True
    assert res["data"]["reset"] is True


async def test_memory_forget_mutual_exclusion(memory_mcp):
    _c, res = await memory_mcp.call_tool(
        "wren_memory_forget", {"ids": ["1"], "source": "user"}
    )
    assert res["ok"] is False
    assert "mutually exclusive" in res["error"]["message"]


async def test_memory_forget_neither(memory_mcp):
    _c, res = await memory_mcp.call_tool("wren_memory_forget", {})
    assert res["ok"] is False
    assert "ids" in res["error"]["message"]


async def test_memory_load_validates_pairs(memory_mcp):
    _c, res = await memory_mcp.call_tool(
        "wren_memory_load", {"pairs": [{"sql": "SELECT 1"}]}
    )
    assert res["ok"] is False
    assert "missing 'nl' or 'sql'" in res["error"]["message"]


async def test_memory_load_dry_run(memory_mcp):
    _c, res = await memory_mcp.call_tool(
        "wren_memory_load",
        {"pairs": [{"nl": "q", "sql": "SELECT 1"}], "dry_run": True},
    )
    assert res["ok"] is True
    assert res["data"]["dry_run"] is True
    assert res["data"]["count"] == 1


async def test_memory_index_success(memory_mcp):
    _c, res = await memory_mcp.call_tool("wren_memory_index", {})
    assert res["ok"] is True
    assert res["data"]["schema_items"] == 3
    assert res["data"]["seed_queries"] == 2


async def test_memory_dump_envelope(memory_mcp):
    _c, res = await memory_mcp.call_tool("wren_memory_dump", {})
    assert res["ok"] is True
    assert res["data"]["count"] == 1
    assert res["data"]["pairs"][0]["sql_query"] == "SELECT 1"
