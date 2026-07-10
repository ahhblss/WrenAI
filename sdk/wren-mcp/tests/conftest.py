"""Shared fixtures for wren-mcp tests.

`tmp_project` patches the shared provider trio's profile resolution
(wren.providers.connection) so unit tests can build a ServerState without a
real ~/.wren/profiles.yml. Integration tests that spawn a real server
subprocess use WREN_HOME instead (see test_server.py) - a subprocess can't
inherit monkeypatched module globals.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def tmp_project(tmp_path, monkeypatch):
    """A minimal Wren project + a mocked duckdb in-memory profile."""
    import wren.providers.connection as conn

    monkeypatch.setattr(
        conn,
        "list_profiles",
        lambda: {"test": {"datasource": "duckdb", "path": ":memory:"}},
    )
    monkeypatch.setattr(
        conn,
        "get_active_profile",
        lambda: ("test", {"datasource": "duckdb", "path": ":memory:"}),
    )

    (tmp_path / "wren_project.yml").write_text("schema_version: 1\n")
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "mdl.json").write_text(
        json.dumps({"models": [], "cubes": []})
    )
    return tmp_path
