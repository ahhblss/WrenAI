"""wren-mcp server entrypoint.

Run as ``wren-mcp --project /path/to/wren/project [options]`` (after
``pip install wren-mcp``). Starts a streamable-http MCP server that AI agents
point at via their MCP client config.

Example Claude Code config (``mcp_settings.json``)::

    {
      "mcpServers": {
        "wren": {
          "url": "http://127.0.0.1:8765/mcp",
          "headers": { "Authorization": "Bearer <YOUR_TOKEN>" }
        }
      }
    }
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import uvicorn

from wren_mcp._app import build_asgi_app
from wren_mcp._state import ServerConfig


def _parse_args(argv: list[str] | None = None) -> ServerConfig:
    parser = argparse.ArgumentParser(
        prog="wren-mcp",
        description=(
            "Wren MCP server - expose a Wren semantic SQL project as a "
            "streamable-http MCP service for AI agents."
        ),
    )
    parser.add_argument(
        "--project",
        required=True,
        type=Path,
        help="Path to a CLI-prepared Wren project (has wren_project.yml + target/mdl.json).",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Profile to use (overrides wren_project.yml `profile:` and the active profile).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host (default 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        default=8765,
        type=int,
        help="Bind port (default 8765).",
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
            "processes, each with its own engine/connector/lock, to scale SQL "
            "concurrency past the GIL. Costs N x memory + N x DB/Qdrant "
            "connections. memory calls already run concurrently with engine "
            "calls within each process (separate locks)."
        ),
    )
    args = parser.parse_args(argv)

    return ServerConfig(
        project_path=args.project,
        profile=args.profile,
        host=args.host,
        port=args.port,
        token=args.token,
        read_only=args.read_only,
        tools=args.tools,
        workers=args.workers,
    )


def _export_config_env(config: ServerConfig) -> None:
    """Stash config in env vars so uvicorn-spawned workers can rebuild it.

    uvicorn workers are fresh processes (spawn, not fork) - they don't inherit
    the master's Python objects, only the environment. Each worker's
    ``app_factory`` reads these back to build its own ServerState.
    """
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


def main(argv: list[str] | None = None) -> None:
    config = _parse_args(argv)
    try:
        app = build_asgi_app(config)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:  # WrenToolkitInitError + any startup failure
        print(f"error: failed to build Wren project state: {exc}", file=sys.stderr)
        sys.exit(1)

    state = app.state.wren_state
    print(
        f"wren-mcp: serving project {state.project_path} "
        f"(datasource={state.datasource()!r}, "
        f"memory={'on' if state.memory_enabled else 'off'}, "
        f"tools={config.tools}, read_only={config.read_only}, "
        f"workers={config.workers})",
        file=sys.stderr,
    )

    if config.workers > 1:
        # Multi-worker: hand off to uvicorn, which spawns N independent
        # processes each calling app_factory() (config passed via env vars).
        # The master's app/state above was for validation + the banner only;
        # close it so it doesn't hold a DB/Qdrant connection it never serves.
        _export_config_env(config)
        state.close()
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
        # Graceful shutdown window so state.close() (connector + memory store)
        # runs before the process exits.
        timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(uv_config)
    try:
        server.run()
    finally:
        state.close()


if __name__ == "__main__":
    main()
