"""FastMCP app builder: tool registration + ASGI assembly with auth.

``build_mcp`` constructs the FastMCP app and resolves the Wren project state
(raises WrenToolkitInitError at startup if the project is missing
wren_project.yml / target/mdl.json). ``build_asgi_app`` wraps the MCP
streamable-http app in bearer-token middleware so the remote endpoint
authenticates every request.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP

from wren_mcp._auth import BearerTokenMiddleware
from wren_mcp._state import ServerConfig, ServerState
from wren_mcp._tools import register_all

if TYPE_CHECKING:
    from starlette.applications import Starlette


def build_mcp(config: ServerConfig) -> FastMCP:
    """Build a FastMCP app pinned to one Wren project + profile.

    Raises ``WrenToolkitInitError`` if the project is missing
    ``wren_project.yml`` or ``target/mdl.json`` - call at server startup so the
    error surfaces before accepting requests.
    """
    state = ServerState.from_config(config)
    # stateless_http when scaling across workers: streamable-http session
    # state lives in-process, so a multi-worker deployment would break a
    # stateful session (initialize on worker A, call_tool on worker B hangs).
    # Stateless mode treats every request independently. wren-mcp tools are
    # stateless so this only costs per-request session setup.
    mcp = FastMCP("wren-mcp", stateless_http=config.workers > 1)
    register_all(mcp, state)
    # Stash state for shutdown wiring (server.py closes it on exit).
    mcp._wren_state = state  # type: ignore[attr-defined]
    return mcp


def build_asgi_app(config: ServerConfig) -> Starlette:
    """Starlette ASGI app: bearer middleware around the MCP streamable-http app.

    Raises ``ValueError`` if no token is configured - a remote MCP service
    must authenticate clients.
    """
    if not config.token:
        raise ValueError(
            "wren-mcp streamable-http requires an auth token. "
            "Set WREN_MCP_TOKEN or pass --token."
        )
    mcp = build_mcp(config)
    state: ServerState = mcp._wren_state  # type: ignore[attr-defined]

    app = mcp.streamable_http_app()
    app.add_middleware(BearerTokenMiddleware, token=config.token)
    # Expose state on the Starlette app so the uvicorn entrypoint can close
    # the cached connector + MemoryStore on shutdown.
    app.state.wren_state = state
    app.state.wren_mcp = mcp
    return app


def app_factory() -> Starlette:
    """Build the ASGI app from ``WREN_MCP_CFG_*`` env vars (multi-worker mode).

    Each uvicorn worker process calls this to build its own ServerState + app
    pinned to the same project/profile. Config is passed via env vars because
    uvicorn spawns workers fresh - the master's in-process config isn't
    inherited as Python objects, only via the environment. The master
    validates config + builds once for the startup banner, then hands off to
    uvicorn which spawns N workers each calling this factory.
    """
    config = ServerConfig(
        project_path=Path(os.environ["WREN_MCP_CFG_PROJECT"]),
        profile=os.environ.get("WREN_MCP_CFG_PROFILE") or None,
        token=os.environ.get("WREN_MCP_CFG_TOKEN")
        or os.environ.get("WREN_MCP_TOKEN"),
        read_only=os.environ.get("WREN_MCP_CFG_READ_ONLY") == "1",
        tools=os.environ.get("WREN_MCP_CFG_TOOLS", "all"),
        host=os.environ.get("WREN_MCP_CFG_HOST", "127.0.0.1"),
        port=int(os.environ.get("WREN_MCP_CFG_PORT", "8765")),
        workers=int(os.environ.get("WREN_MCP_CFG_WORKERS", "1")),
    )
    return build_asgi_app(config)
