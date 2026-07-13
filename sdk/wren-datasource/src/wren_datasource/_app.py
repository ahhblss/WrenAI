"""FastAPI app builder: store + bearer middleware + routers.

``build_app`` constructs the FastAPI app with a SQLite-backed ``Store`` and
bearer-token middleware. Routes live in :mod:`wren_datasource.api` and reach
the store via ``request.app.state.store``.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from wren_datasource._auth import BearerTokenMiddleware
from wren_datasource._store import Store


def build_app(*, token: str, db_path: Path | str | None = None) -> FastAPI:
    """Build the datasource REST app.

    Raises ``ValueError`` if no token is configured - a service that returns
    expanded DB credentials must authenticate clients.
    """
    if not token:
        raise ValueError(
            "wren-datasource requires an auth token. "
            "Set WREN_DATASOURCE_TOKEN or pass --token."
        )
    app = FastAPI(title="wren-datasource", version="0.1.0")
    app.state.store = Store(db_path)
    app.add_middleware(BearerTokenMiddleware, token=token)

    from wren_datasource.api import connection, profiles, projects  # noqa: PLC0415

    app.include_router(projects.router)
    app.include_router(profiles.router)
    app.include_router(connection.router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
