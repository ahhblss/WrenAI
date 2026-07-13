"""Project CRUD: POST/GET/DELETE /projects.

A project binds a name + a built Wren project directory (``wren_project.yml`` +
``target/mdl.json``) + an optional profile. The project_path is validated at
registration so a bad path fails fast rather than on the first query.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from wren.providers import WrenToolkitInitError

from wren_datasource._resolve import validate_project_path

router = APIRouter(prefix="/projects", tags=["projects"])


class ProjectCreate(BaseModel):
    name: str
    project_path: str
    profile_name: str | None = None


@router.post("")
async def create_project(request: Request, body: ProjectCreate) -> dict[str, Any]:
    store = request.app.state.store
    try:
        validate_project_path(body.project_path)
        return store.add_project(
            name=body.name,
            project_path=body.project_path,
            profile_name=body.profile_name,
        )
    except (ValueError, WrenToolkitInitError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("")
async def list_projects(request: Request) -> dict[str, Any]:
    return {"projects": request.app.state.store.list_projects()}


@router.get("/{project_id}")
async def get_project(request: Request, project_id: str) -> dict[str, Any]:
    proj = request.app.state.store.get_project(project_id)
    if proj is None:
        raise HTTPException(404, f"project {project_id!r} not found")
    return proj


@router.delete("/{project_id}")
async def delete_project(request: Request, project_id: str) -> dict[str, Any]:
    if not request.app.state.store.delete_project(project_id):
        raise HTTPException(404, f"project {project_id!r} not found")
    return {"deleted": project_id}
