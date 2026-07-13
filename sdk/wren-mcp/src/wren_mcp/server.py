"""wren-mcp server entrypoint.

Run as ``wren-mcp --project /path/to/wren/project [options]`` (single-project
mode, after ``pip install wren-mcp``) or ``wren-mcp --datasource-url URL
--datasource-token T [options]`` (multi-project mode, serving many projects
resolved via the wren-datasource REST service). Starts a streamable-http MCP
server that AI agents point at via their MCP client config.

Example Claude Code config (``mcp_settings.json``) - multi-project::

    {
      "mcpServers": {
        "wren-sales": {
          "url": "http://127.0.0.1:8765/mcp",
          "headers": {"Authorization": "Bearer <TOKEN>", "X-Wren-Project": "sales"}
        },
        "wren-ops": {
          "url": "http://127.0.0.1:8765/mcp",
          "headers": {"Authorization": "Bearer <TOKEN>", "X-Wren-Project": "ops"}
        }
      }
    }
"""

from __future__ import annotations

import argparse
import os
import sys

import uvicorn

from wren_mcp._app import build_asgi_app
from wren_mcp._state import ServerConfig


def _parse_args(argv: list[str] | None = None) -> ServerConfig:
    parser = argparse.ArgumentParser(
        prog="wren-mcp",
        description=(
            "Wren MCP server - expose one or many Wren semantic SQL projects as "
            "a streamable-http MCP service for AI agents."
        ),
    )
    parser.add_argument(
        "--project",
        default=None,
        type=str,
        help=(
            "Path to a CLI-prepared Wren project (single-project mode). "
            "Optional in multi-project mode (--datasource-url)."
        ),
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Profile to use (single-project mode; overrides wren_project.yml + active).",
    )
    parser.add_argument(
        "--datasource-url",
        default=os.environ.get("WREN_MCP_DATASOURCE_URL"),
        help=(
            "wren-datasource REST service URL (multi-project mode). When set, "
            "connections are resolved per-request by X-Wren-Project header. "
            "Default: $WREN_MCP_DATASOURCE_URL."
        ),
    )
    parser.add_argument(
        "--datasource-token",
        default=os.environ.get("WREN_MCP_DATASOURCE_TOKEN"),
        help="Bearer token for the wren-datasource REST service. Default: $WREN_MCP_DATASOURCE_TOKEN.",
    )
    parser.add_argument(
        "--default-project",
        default=os.environ.get("WREN_MCP_DEFAULT_PROJECT"),
        help=(
            "Project id to serve when a request has no X-Wren-Project header "
            "(multi-project mode). Default: $WREN_MCP_DEFAULT_PROJECT."
        ),
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="Bind host (default 127.0.0.1)."
    )
    parser.add_argument(
        "--port", default=8765, type=int, help="Bind port (default 8765)."
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("WREN_MCP_TOKEN"),
        help="Bearer token clients must send (default: $WREN_MCP_TOKEN). Required.",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Drop write/mutation tools (wren_store_query, context build, profile rm, ...).",
    )
    parser.add_argument(
        "--tools",
        default="all",
        choices=["tier1", "all"],
        help="Tool tier: tier1 (6 core) or all (full CLI surface). Default all.",
    )
    parser.add_argument(
        "--workers",
        default=1,
        type=int,
        help=(
            "uvicorn worker process count (default 1). >1 spawns independent "
            "processes, each with its own registry/locks, to scale SQL "
            "concurrency past the GIL. Costs N x memory + N x DB/Qdrant "
            "connections."
        ),
    )
    args = parser.parse_args(argv)

    project_path = None
    if args.project:
        from pathlib import Path  # noqa: PLC0415

        project_path = Path(args.project)

    return ServerConfig(
        project_path=project_path,
        profile=args.profile,
        host=args.host,
        port=args.port,
        token=args.token,
        read_only=args.read_only,
        tools=args.tools,
        workers=args.workers,
        datasource_url=args.datasource_url,
        datasource_token=args.datasource_token,
        default_project=args.default_project,
    )


def _export_config_env(config: ServerConfig) -> None:
    """Stash config in env vars so uvicorn-spawned workers can rebuild it.

    uvicorn workers are fresh processes (spawn, not fork) - they don't inherit
    the master's Python objects, only the environment. Each worker's
    ``app_factory`` reads these back to build its own ServerContext.
    """
    if config.project_path is not None:
        os.environ["WREN_MCP_CFG_PROJECT"] = str(config.project_path)
    if config.profile:
        os.environ["WREN_MCP_CFG_PROFILE"] = config.profile
    if config.token:
        os.environ["WREN_MCP_CFG_TOKEN"] = config.token
    os.environ["WREN_MCP_CFG_TOOLS"] = config.tools
    os.environ["WREN_MCP_CFG_READ_ONLY"] = "1" if config.read_only else "0"
    os.environ["WREN_MCP_CFG_HOST"] = config.host
    os.environ["WREN_MCP_CFG_PORT"] = str(config.port)
    os.environ["WREN_MCP_CFG_WORKERS"] = str(config.workers)
    if config.datasource_url:
        os.environ["WREN_MCP_CFG_DATASOURCE_URL"] = config.datasource_url
    if config.datasource_token:
        os.environ["WREN_MCP_CFG_DATASOURCE_TOKEN"] = config.datasource_token
    if config.default_project:
        os.environ["WREN_MCP_CFG_DEFAULT_PROJECT"] = config.default_project


def _banner(config: ServerConfig, ctx) -> str:  # type: ignore[no-untyped-def]
    if config.datasource_url:
        return (
            f"wren-mcp: serving via {config.datasource_url} "
            f"(default_project={config.default_project!r}, "
            f"memory={'on' if ctx.memory_enabled else 'off'}, "
            f"tools={config.tools}, read_only={config.read_only}, "
            f"workers={config.workers})"
        )
    # single-project mode
    state = ctx.registry.default_state
    return (
        f"wren-mcp: serving project {state.project_path} "
        f"(datasource={state.datasource()!r}, "
        f"memory={'on' if ctx.memory_enabled else 'off'}, "
        f"tools={config.tools}, read_only={config.read_only}, "
        f"workers={config.workers})"
    )


def main(argv: list[str] | None = None) -> None:
    config = _parse_args(argv)
    if not config.token:
        print("error: --token (or WREN_MCP_TOKEN) is required", file=sys.stderr)
        sys.exit(2)
    if not config.datasource_url and config.project_path is None:
        print(
            "error: either --project (single-project mode) "
            "or --datasource-url (multi-project mode) is required",
            file=sys.stderr,
        )
        sys.exit(2)
    try:
        app = build_asgi_app(config)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:  # WrenToolkitInitError + any startup failure
        print(f"error: failed to build Wren project state: {exc}", file=sys.stderr)
        sys.exit(1)

    ctx = app.state.wren_ctx
    print(_banner(config, ctx), file=sys.stderr)

    if config.workers > 1:
        # Multi-worker: hand off to uvicorn, which spawns N independent
        # processes each calling app_factory() (config passed via env vars).
        # The master's app/ctx above was for validation + the banner only;
        # close it so it doesn't hold a DB/Qdrant connection it never serves.
        _export_config_env(config)
        ctx.close()
        uvicorn.run(
            "wren_mcp._app:app_factory",
            factory=True,
            host=config.host,
            port=config.port,
            workers=config.workers,
            log_level="info",
            timeout_graceful_shutdown=5,
        )
        return

    uv_config = uvicorn.Config(
        app,
        host=config.host,
        port=config.port,
        log_level="info",
        # Graceful shutdown window so ctx.close() (connectors + memory stores)
        # runs before the process exits.
        timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(uv_config)
    try:
        server.run()
    finally:
        ctx.close()


if __name__ == "__main__":
    main()
