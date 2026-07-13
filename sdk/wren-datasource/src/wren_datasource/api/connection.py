"""Connection resolution + manifest + validate.

  - ``GET /projects/{id}/connection`` -> {datasource, connection_info,
    memory_collection_prefix, project_path}. This is the call wren-mcp makes
    per-project to build a ``ProjectState``.
  - ``GET /projects/{id}/manifest`` -> read-through ``target/mdl.json``.
  - ``POST /projects/{id}/validate`` -> SELECT 1 through the connector.

The connection response carries expanded credentials (real DB secrets) - it
must only travel to an authenticated wren-mcp over a trusted network.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from wren.providers import WrenToolkitInitError

from wren_datasource._resolve import load_manifest, resolve_connection

router = APIRouter(tags=["connection"])


def _get_project_or_404(request: Request, project_id: str) -> dict[str, Any]:
    proj = request.app.state.store.get_project(project_id)
    if proj is None:
        raise HTTPException(404, f"project {project_id!r} not found")
    return proj


def _resolve_profile(
    request: Request, proj: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    if not proj["profile_name"]:
        raise HTTPException(400, f"project {proj['name']!r} has no profile bound")
    profile = request.app.state.store.get_profile(proj["profile_name"])
    if profile is None:
        raise HTTPException(400, f"profile {proj['profile_name']!r} not found")
    try:
        return resolve_connection(profile)
    except WrenToolkitInitError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/projects/{project_id}/connection")
async def get_connection(request: Request, project_id: str) -> dict[str, Any]:
    proj = _get_project_or_404(request, project_id)
    datasource, connection_info = _resolve_profile(request, proj)
    return {
        "datasource": datasource,
        "connection_info": connection_info,
        # Per-project Qdrant collection isolation (MemoryStore supports
        # collection_prefix natively - store.py:77).
        "memory_collection_prefix": f"wren_{proj['id']}",
        "project_path": proj["project_path"],
    }


@router.get("/projects/{project_id}/manifest")
async def get_manifest(request: Request, project_id: str) -> dict[str, Any]:
    proj = _get_project_or_404(request, project_id)
    try:
        return load_manifest(proj["project_path"])
    except WrenToolkitInitError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/projects/{project_id}/validate")
async def validate(request: Request, project_id: str) -> dict[str, Any]:
    """Run ``SELECT 1`` through the connector. Mirrors profile_cli._validate_connection."""
    proj = _get_project_or_404(request, project_id)
    try:
        datasource, connection_info = _resolve_profile(request, proj)
    except HTTPException as exc:  # profile misconfigured
        return {
            "ok": False,
            "error": exc.detail if isinstance(exc.detail, str) else str(exc.detail),
        }
    from pydantic import ValidationError  # noqa: PLC0415
    from wren.connector.factory import get_connector  # noqa: PLC0415
    from wren.model.data_source import DataSource  # noqa: PLC0415

    try:
        ds = DataSource(datasource.lower())
        conn_info = ds.get_connection_info(connection_info)
        connector = get_connector(ds, conn_info)
        connector.dry_run("SELECT 1")
    except (ValidationError, ValueError) as exc:
        return {"ok": False, "error": f"invalid connection info: {exc}"}
    except Exception as exc:  # noqa: BLE001 - surface the raw driver error
        return {"ok": False, "error": str(exc)}
    return {"ok": True}
