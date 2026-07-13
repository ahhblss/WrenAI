"""FastMCP app builder: tool registration + ASGI assembly with auth + routing.

``build_mcp`` constructs the FastMCP app and a ``ServerContext`` (the
dispatcher tools capture). In single-project mode (no ``--datasource-url``)
the context wraps one local ``ServerState`` built from ``ServerConfig``. In
multi-project mode it wraps a ``RestProjectRegistry`` that fetches per-project
connection info from the wren-datasource REST service. ``build_asgi_app`` wraps
the MCP streamable-http app in bearer-token middleware (outer) and
project-routing middleware (inner).

Raises ``WrenToolkitInitError`` at startup in single-project mode if the
project is missing ``wren_project.yml`` / ``target/mdl.json`` - call at server
startup so the error surfaces before accepting requests.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP

from wren_mcp._auth import BearerTokenMiddleware
from wren_mcp._routing import (
    DatasourceClient,
    ProjectRoutingMiddleware,
    RestProjectRegistry,
    ServerContext,
    SingleProjectRegistry,
)
from wren_mcp._state import ServerConfig, ServerState
from wren_mcp._tools import register_all

if TYPE_CHECKING:
    from starlette.applications import Starlette


def _build_context(config: ServerConfig) -> ServerContext:
    """Build the ServerContext: multi-project (REST) or single-project (local)."""
    tool_timeout = float(os.getenv("WREN_MCP_TOOL_TIMEOUT", "120"))
    memory_enabled = bool(os.environ.get("QDRANT_URL"))

    if config.datasource_url:
        rest = DatasourceClient(config.datasource_url, config.datasource_token)
        registry = RestProjectRegistry(
            rest,
            tool_timeout,
            default_project=config.default_project,
            config=config,
        )
        return ServerContext(
            registry=registry,
            config=config,
            memory_enabled=memory_enabled,
            default_project=config.default_project,
            rest_client=rest,
        )

    # Single-project mode (backward compatible).
    state = ServerState.from_config(config)
    registry = SingleProjectRegistry(state)
    return ServerContext(
        registry=registry,
        config=config,
        memory_enabled=memory_enabled,
        default_project="default",
    )


def build_mcp(config: ServerConfig) -> FastMCP:
    """Build a FastMCP app + ServerContext for one or many Wren projects.

    In single-project mode, raises ``WrenToolkitInitError`` if the project is
    missing ``wren_project.yml`` or ``target/mdl.json`` - call at server
    startup so the error surfaces before accepting requests. In multi-project
    mode the project is validated lazily on first request via the REST service.
    """
    ctx = _build_context(config)
    # stateless_http when scaling across workers: streamable-http session
    # state lives in-process, so a multi-worker deployment would break a
    # stateful session (initialize on worker A, call_tool on worker B hangs).
    mcp = FastMCP("wren-mcp", stateless_http=config.workers > 1)
    register_all(mcp, ctx)
    # Stash ctx for shutdown wiring (server.py closes it on exit).
    mcp._wren_ctx = ctx  # type: ignore[attr-defined]
    return mcp


def build_asgi_app(config: ServerConfig) -> Starlette:
    """Starlette ASGI app: bearer middleware (outer) + project routing (inner).

    Raises ``ValueError`` if no token is configured - a remote MCP service
    must authenticate clients.
    """
    if not config.token:
        raise ValueError(
            "wren-mcp streamable-http requires an auth token. "
            "Set WREN_MCP_TOKEN or pass --token."
        )
    mcp = build_mcp(config)
    ctx: ServerContext = mcp._wren_ctx  # type: ignore[attr-defined]

    app = mcp.streamable_http_app()
    # add_middleware is LIFO: ProjectRouting is added first (inner), Bearer last
    # (outer) so auth runs before routing.
    app.add_middleware(ProjectRoutingMiddleware, ctx=ctx)
    app.add_middleware(BearerTokenMiddleware, token=config.token)
    # Expose ctx on the Starlette app so the uvicorn entrypoint can close the
    # cached connectors + MemoryStores on shutdown.
    app.state.wren_ctx = ctx
    app.state.wren_mcp = mcp
    return app


def app_factory() -> Starlette:
    """Build the ASGI app from ``WREN_MCP_CFG_*`` env vars (multi-worker mode).

    Each uvicorn worker process calls this to build its own ServerContext +
    app. Config is passed via env vars because uvicorn spawns workers fresh -
    the master's in-process config isn't inherited as Python objects, only via
    the environment. The master validates config + builds once for the startup
    banner, then hands off to uvicorn which spawns N workers each calling this
    factory.
    """
    project_env = os.environ.get("WREN_MCP_CFG_PROJECT")
    config = ServerConfig(
        project_path=Path(project_env) if project_env else None,
        profile=os.environ.get("WREN_MCP_CFG_PROFILE") or None,
        token=os.environ.get("WREN_MCP_CFG_TOKEN") or os.environ.get("WREN_MCP_TOKEN"),
        read_only=os.environ.get("WREN_MCP_CFG_READ_ONLY") == "1",
        tools=os.environ.get("WREN_MCP_CFG_TOOLS", "all"),
        host=os.environ.get("WREN_MCP_CFG_HOST", "127.0.0.1"),
        port=int(os.environ.get("WREN_MCP_CFG_PORT", "8765")),
        workers=int(os.environ.get("WREN_MCP_CFG_WORKERS", "1")),
        datasource_url=os.environ.get("WREN_MCP_CFG_DATASOURCE_URL") or None,
        datasource_token=os.environ.get("WREN_MCP_CFG_DATASOURCE_TOKEN") or None,
        default_project=os.environ.get("WREN_MCP_CFG_DEFAULT_PROJECT") or None,
    )
    return build_asgi_app(config)
