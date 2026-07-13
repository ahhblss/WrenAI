"""Profile CRUD: POST/GET/DELETE /profiles.

Profiles store ``${ENV}`` placeholders unexpanded; list/get responses redact
sensitive fields so a leaked token or log never exposes credentials. The raw
(expanded) profile only leaves the service via ``GET /projects/{id}/connection``
toward an authenticated wren-mcp over a trusted network.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

router = APIRouter(prefix="/profiles", tags=["profiles"])

# Substring match on field names - same intent as wren.profile's redaction.
_SENSITIVE = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "credential",
)


def _redact(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        k: ("***" if any(s in k.lower() for s in _SENSITIVE) else v)
        for k, v in profile.items()
    }


class ProfileCreate(BaseModel):
    """A profile is ``datasource`` + arbitrary connection fields."""

    name: str
    datasource: str
    model_config = ConfigDict(extra="allow")


@router.post("")
async def create_profile(request: Request, body: ProfileCreate) -> dict[str, Any]:
    store = request.app.state.store
    profile = body.model_dump(exclude={"name"})
    try:
        store.add_profile(body.name, profile)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"name": body.name, "datasource": body.datasource}


@router.get("")
async def list_profiles(request: Request) -> dict[str, Any]:
    return {"profiles": [_redact(p) for p in request.app.state.store.list_profiles()]}


@router.get("/{name}")
async def get_profile(request: Request, name: str) -> dict[str, Any]:
    prof = request.app.state.store.get_profile(name)
    if prof is None:
        raise HTTPException(404, f"profile {name!r} not found")
    return {"profile": _redact(prof)}


@router.delete("/{name}")
async def delete_profile(request: Request, name: str) -> dict[str, Any]:
    if not request.app.state.store.delete_profile(name):
        raise HTTPException(404, f"profile {name!r} not found")
    return {"deleted": name}
