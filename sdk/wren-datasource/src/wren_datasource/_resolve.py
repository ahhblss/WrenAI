"""Connection resolution + project-bound helpers.

Replaces ``ProfileConnectionProvider`` for REST-served projects. Secret
expansion (``${ENV}``) happens here so the DB stores placeholders and only the
resolved (expanded) connection_info leaves the REST service to wren-mcp.

Also provides manifest read-through (``ProjectMDLSource``) and project-path
validation - the project-bound operations wren-mcp would otherwise do locally.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from wren.profile import expand_profile_secrets
from wren.providers import ProjectMDLSource, WrenToolkitInitError


class ProjectNotFoundError(Exception):
    """Raised when a project id/name is not registered in the store."""


def resolve_connection(profile: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Expand secrets and split a profile into ``(datasource, connection_info)``.

    ``datasource`` is popped from the expanded profile; the remainder is the
    connection_info dict handed to ``WrenEngine``. The input profile keeps its
    ``${ENV}`` placeholders intact (callers pass a copy if needed).
    """
    expanded = expand_profile_secrets(dict(profile))
    datasource = expanded.pop("datasource", None)
    if not datasource:
        raise WrenToolkitInitError("profile has no 'datasource' field")
    return str(datasource), dict(expanded)


def load_manifest(project_path: str | Path) -> dict[str, Any]:
    """Read-through ``<project>/target/mdl.json`` via ``ProjectMDLSource``.

    Each call re-reads from disk so ``wren context build`` updates by an
    external CLI run are picked up on the next request.
    """
    return ProjectMDLSource(project_path=Path(project_path)).load_manifest()


def validate_project_path(project_path: str | Path) -> None:
    """Verify a path is a built Wren project (has ``wren_project.yml`` + ``mdl.json``)."""
    p = Path(project_path)
    if not (p / "wren_project.yml").exists():
        raise WrenToolkitInitError(
            f"wren_project.yml not found at {p}. "
            "Is this a Wren project? Run `wren context init` to create one."
        )
    if not (p / "target" / "mdl.json").exists():
        raise WrenToolkitInitError(
            f"target/mdl.json not found at {p}/target/mdl.json. "
            "Run `wren context build` first."
        )
