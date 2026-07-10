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
    args = parser.parse_args(argv)

    return ServerConfig(
        project_path=args.project,
        profile=args.profile,
        host=args.host,
        port=args.port,
        token=args.token,
        read_only=args.read_only,
        tools=args.tools,
    )


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
        f"tools={config.tools}, read_only={config.read_only})",
        file=sys.stderr,
    )

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
