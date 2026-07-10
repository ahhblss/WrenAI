"""End-to-end integration: real streamable-http server + mcp client.

Spawns `wren-mcp` as a subprocess (with WREN_HOME pointing at a temp
profiles.yml, since a subprocess can't inherit monkeypatched globals), waits
for it to bind, then drives it with a real MCP streamable-http client:
auth rejection, list_tools, and a call_tool round-trip.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

PORT = "8781"


def _write_project(tmp_path: Path) -> None:
    (tmp_path / "wren_project.yml").write_text("schema_version: 1\n")
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "mdl.json").write_text(json.dumps({"models": []}))


def _write_wren_home(tmp_path: Path) -> Path:
    home = tmp_path / "wren_home"
    home.mkdir()
    (home / "profiles.yml").write_text(
        'active: test\nprofiles:\n  test:\n    datasource: duckdb\n    path: ":memory:"\n'
    )
    return home


@pytest.fixture
def server_url(tmp_path):
    _write_project(tmp_path)
    wren_home = _write_wren_home(tmp_path)
    env = dict(os.environ, WREN_HOME=str(wren_home))
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "wren_mcp.server",
            "--project",
            str(tmp_path),
            "--token",
            "secret",
            "--port",
            PORT,
            "--host",
            "127.0.0.1",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{PORT}/mcp"
    try:
        # Wait for the server to start accepting connections.
        for _ in range(40):
            try:
                httpx.get(url, timeout=1, headers={"Authorization": "Bearer secret"})
                break
            except Exception:
                time.sleep(0.5)
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


async def test_rejects_missing_token(server_url):
    r = await asyncio.to_thread(httpx.get, server_url)
    assert r.status_code == 401


async def test_rejects_bad_token(server_url):
    r = await asyncio.to_thread(
        httpx.get, server_url, headers={"Authorization": "Bearer wrong"}
    )
    assert r.status_code == 401


async def test_list_tools_and_call_over_wire(server_url):
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(
        server_url, headers={"Authorization": "Bearer secret"}
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert "wren_query" in names
            assert "wren_list_models" in names

            res = await session.call_tool("wren_list_models", {})
            assert res.isError is False
            assert res.structuredContent["ok"] is True
            assert res.structuredContent["data"]["models"] == []
