"""Multi-project routing tests: header routing, contextvar propagation, LRU,
per-project memory collection_prefix, and the ProjectRoutingMiddleware.

These cover the Phase 2/3 multi-project behavior. Single-project backward
compat is covered by test_tools.py / test_server.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest
from fastapi.testclient import TestClient

from wren_mcp._app import build_asgi_app, build_mcp
from wren_mcp._routing import (
    ProjectNotFoundError,
    current_state,
)
from wren_mcp._state import ServerConfig


def _make_project(base: Path, name: str) -> Path:
    """Create a minimal valid Wren project dir with a distinct manifest."""
    p = base / name
    p.mkdir()
    (p / "wren_project.yml").write_text(f"name: {name}\n")
    (p / "target").mkdir()
    (p / "target" / "mdl.json").write_text(
        json.dumps({"models": [{"name": name, "columns": []}]})
    )
    return p


def _conn_info(project_path: Path, pid: str) -> dict:
    return {
        "datasource": "duckdb",
        "connection_info": {"path": ":memory:"},
        "project_path": str(project_path),
        "memory_collection_prefix": f"wren_{pid}",
    }


def _patch_rest(ctx, projects: dict[str, Path]) -> None:
    """Make ctx.rest_client.get_connection return per-pid connection info."""

    async def fake_get(pid: str) -> dict:
        if pid not in projects:
            raise ProjectNotFoundError(pid)
        return _conn_info(projects[pid], pid)

    ctx.rest_client.get_connection = fake_get  # type: ignore[method-assign]


# ── registry + dispatcher ─────────────────────────────────────────────────


async def test_rest_registry_fetches_and_caches_per_project(tmp_path):
    proj_a = _make_project(tmp_path, "a")
    proj_b = _make_project(tmp_path, "b")
    config = ServerConfig(
        datasource_url="http://fake", datasource_token="t", token="t", tools="tier1"
    )
    mcp = build_mcp(config)
    ctx = mcp._wren_ctx
    _patch_rest(ctx, {"a": proj_a, "b": proj_b})

    state_a = await ctx.registry.get("a")
    state_b = await ctx.registry.get("b")
    assert state_a.project_id == "a"
    assert state_b.project_id == "b"
    # Distinct projects, distinct paths + memory prefixes.
    assert state_a.project_path == proj_a
    assert state_b.project_path == proj_b
    assert state_a.memory_collection_prefix == "wren_a"
    assert state_b.memory_collection_prefix == "wren_b"
    # Distinct locks - concurrent projects don't serialize on each other.
    assert state_a.engine_lock is not state_b.engine_lock


async def test_rest_registry_unknown_project_raises(tmp_path):
    config = ServerConfig(
        datasource_url="http://fake", datasource_token="t", token="t", tools="tier1"
    )
    mcp = build_mcp(config)
    ctx = mcp._wren_ctx
    _patch_rest(ctx, {})
    with pytest.raises(ProjectNotFoundError):
        await ctx.registry.get("nope")


async def test_dispatcher_routes_to_current_state(tmp_path):
    """ServerContext (dispatcher) delegates to the current request's state."""
    proj_a = _make_project(tmp_path, "a")
    proj_b = _make_project(tmp_path, "b")
    config = ServerConfig(
        datasource_url="http://fake", datasource_token="t", token="t", tools="tier1"
    )
    mcp = build_mcp(config)
    ctx = mcp._wren_ctx
    _patch_rest(ctx, {"a": proj_a, "b": proj_b})

    state_a = await ctx.registry.get("a")
    state_b = await ctx.registry.get("b")

    token = current_state.set(state_a)
    try:
        assert ctx.project_path == proj_a
        assert ctx.memory_collection_prefix == "wren_a"
        # load_manifest reads proj_a's mdl.json (dispatcher delegates).
        manifest = ctx.load_manifest()
        assert manifest["models"][0]["name"] == "a"
    finally:
        current_state.reset(token)

    token = current_state.set(state_b)
    try:
        assert ctx.project_path == proj_b
        assert ctx.load_manifest()["models"][0]["name"] == "b"
    finally:
        current_state.reset(token)


async def test_dispatcher_falls_back_to_default_when_unrouted(tmp_path):
    """In-process call without middleware falls back to default_state (None here)."""
    config = ServerConfig(
        datasource_url="http://fake", datasource_token="t", token="t", tools="tier1"
    )
    mcp = build_mcp(config)
    ctx = mcp._wren_ctx
    # No default project configured, no current_state set -> raise.
    current_state.set(None)
    with pytest.raises(ProjectNotFoundError):
        ctx._current()


# ── contextvar propagation across worker threads ─────────────────────────


async def test_contextvar_propagates_to_worker_thread():
    """current_state must survive anyio.to_thread.run_sync (copy_context).

    The dispatcher's worker-thread calls (run_blocked) read current_state; if
    it didn't propagate, every tool call would miss the routed project.
    """

    class _Marker:
        pass

    marker = _Marker()
    token = current_state.set(marker)
    try:

        def read_in_thread():
            return current_state.get()

        # anyio.to_thread.run_sync copies the context by default.
        result = await anyio.to_thread.run_sync(read_in_thread)
        assert result is marker
    finally:
        current_state.reset(token)


# ── middleware (over-the-wire header routing) ────────────────────────────


def test_middleware_routes_by_header(tmp_path):
    proj_x = _make_project(tmp_path, "x")
    config = ServerConfig(
        datasource_url="http://fake", datasource_token="t", token="t", tools="tier1"
    )
    app = build_asgi_app(config)
    ctx = app.state.wren_ctx
    _patch_rest(ctx, {"x": proj_x})

    client = TestClient(app)
    # The routing middleware runs before the router. /health isn't a registered
    # route on the MCP app (only /mcp is), so the response itself is a 404 -
    # but we only need to verify the middleware resolved the project into the
    # registry cache before call_next.
    client.get(
        "/health",
        headers={"Authorization": "Bearer t", "X-Wren-Project": "x"},
    )
    assert "x" in ctx.registry._states
    assert ctx.registry._states["x"].project_path == proj_x


def test_middleware_unknown_project_returns_404(tmp_path):
    config = ServerConfig(
        datasource_url="http://fake", datasource_token="t", token="t", tools="tier1"
    )
    app = build_asgi_app(config)
    ctx = app.state.wren_ctx
    _patch_rest(ctx, {})  # no projects

    client = TestClient(app)
    r = client.get(
        "/health",
        headers={"Authorization": "Bearer t", "X-Wren-Project": "nope"},
    )
    assert r.status_code == 404


# ── LRU eviction ──────────────────────────────────────────────────────────


async def test_rest_registry_evicts_lru(tmp_path):
    """States past max_projects are evicted LRU (closing their connector)."""
    projects = {f"p{i}": _make_project(tmp_path, f"p{i}") for i in range(4)}
    config = ServerConfig(
        datasource_url="http://fake", datasource_token="t", token="t", tools="tier1"
    )
    mcp = build_mcp(config)
    ctx = mcp._wren_ctx
    _patch_rest(ctx, projects)
    ctx.registry._max = 2  # tight cap for the test

    s0 = await ctx.registry.get("p0")
    await ctx.registry.get("p1")
    # p2 evicts p0 (oldest), p3 evicts p1.
    await ctx.registry.get("p2")
    await ctx.registry.get("p3")
    assert "p0" not in ctx.registry._states
    assert "p1" not in ctx.registry._states
    assert "p2" in ctx.registry._states
    assert "p3" in ctx.registry._states
    # Re-fetching p0 builds a new state (the old one was evicted/closed).
    s0_again = await ctx.registry.get("p0")
    assert s0_again is not s0
