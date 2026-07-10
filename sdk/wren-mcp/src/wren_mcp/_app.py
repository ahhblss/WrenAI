"""FastMCP app builder: tool registration + ASGI assembly with auth.

``build_mcp`` constructs the FastMCP app and resolves the Wren project state
(raises WrenToolkitInitError at startup if the project is missing
wren_project.yml / target/mdl.json). ``build_asgi_app`` wraps the MCP
streamable-http app in bearer-token middleware so the remote endpoint
authenticates every request.
"""

from __future__ import annotations

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
    mcp = FastMCP("wren-mcp")
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
