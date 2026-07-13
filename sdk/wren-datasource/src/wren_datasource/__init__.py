"""wren-datasource: datasource management REST service for Wren.

Exposes project + profile + connection management over a REST API so that
wren-mcp (and other Wren services) can serve multiple projects from a single
process by resolving connections per-request instead of pinning one project +
profile at startup. The REST service replaces ``~/.wren/profiles.yml`` +
``ProfileConnectionProvider`` as the datasource management layer.
"""

from wren_datasource._resolve import (
    ProjectNotFoundError,
    load_manifest,
    resolve_connection,
    validate_project_path,
)
from wren_datasource._store import DEFAULT_DB_PATH, Store

__all__ = [
    "DEFAULT_DB_PATH",
    "ProjectNotFoundError",
    "Store",
    "load_manifest",
    "resolve_connection",
    "validate_project_path",
]
