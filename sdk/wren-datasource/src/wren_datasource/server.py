"""wren-datasource server entrypoint.

Run as ``wren-datasource --token <TOKEN> [options]``. Starts a FastAPI REST
service managing projects + profiles + connection resolution for wren-mcp
multi-project mode.

Example::

    wren-datasource --token $WREN_DATASOURCE_TOKEN --port 8766
    wren-datasource --token $TOKEN --import-profiles   # first run
"""

from __future__ import annotations

import argparse
import os
import sys

import uvicorn

from wren_datasource._app import build_app
from wren_datasource._store import DEFAULT_DB_PATH, Store


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="wren-datasource",
        description="Wren datasource management REST service (project + profile + connection).",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("WREN_DATASOURCE_TOKEN"),
        help="Bearer token clients must send (default: $WREN_DATASOURCE_TOKEN). Required.",
    )
    parser.add_argument(
        "--db-path",
        default=os.environ.get("WREN_DATASOURCE_DB", str(DEFAULT_DB_PATH)),
        help=f"SQLite DB path (default: {DEFAULT_DB_PATH}).",
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="Bind host (default 127.0.0.1)."
    )
    parser.add_argument(
        "--port", default=8766, type=int, help="Bind port (default 8766)."
    )
    parser.add_argument(
        "--import-profiles",
        action="store_true",
        help="Import ~/.wren/profiles.yml into the DB on startup (one-time migration).",
    )
    parser.add_argument(
        "--profiles-yml",
        default=None,
        help="Source profiles.yml for --import-profiles (default: ~/.wren/profiles.yml).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if not args.token:
        print("error: --token (or WREN_DATASOURCE_TOKEN) is required", file=sys.stderr)
        sys.exit(2)

    if args.import_profiles:
        store = Store(args.db_path)
        n = store.import_profiles_yml(args.profiles_yml)
        print(
            f"wren-datasource: imported {n} profile(s) into {args.db_path}",
            file=sys.stderr,
        )

    app = build_app(token=args.token, db_path=args.db_path)
    print(
        f"wren-datasource: serving on {args.host}:{args.port} (db={args.db_path})",
        file=sys.stderr,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
