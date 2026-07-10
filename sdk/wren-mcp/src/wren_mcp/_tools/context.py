"""Tier 2 context/MDL tools: show, build, validate, instructions.

Wrap wren.context (build_json / build_manifest / validate_project / load_rules)
so an agent can inspect, rebuild, and validate the project's MDL without
shelling out to the CLI. ``wren_context_build`` writes target/mdl.json (a side
effect) and is gated by config.read_only.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from wren_mcp._bridge import run_blocked
from wren_mcp._envelope import make_error, make_success

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from wren_mcp._state import ServerState


def register(mcp: FastMCP, state: ServerState) -> None:
    project = state.project_path

    @mcp.tool(
        description=(
            "Show the current project context: models (source/columns/pk), "
            "views, relationships, cubes. Returns the snake_case manifest. "
            "Read-only; call first to discover what's queryable."
        )
    )
    async def wren_context_show() -> dict[str, Any]:
        def _show() -> dict[str, Any]:
            from wren.context import build_manifest

            return build_manifest(project)

        try:
            manifest = await run_blocked(state, _show)
        except Exception as exc:
            return make_error(exc)
        summary = (
            f"models={len(manifest.get('models', []))}, "
            f"views={len(manifest.get('views', []))}, "
            f"relationships={len(manifest.get('relationships', []))}, "
            f"cubes={len(manifest.get('cubes', []))}"
        )
        return make_success(content=summary, data={"manifest": manifest})

    @mcp.tool(
        description=(
            "Build the project into target/mdl.json for the engine. Call after "
            "editing models/views/cubes/relationships so the engine picks up "
            "changes. Writes a file (side effect)."
        )
    )
    async def wren_context_build() -> dict[str, Any]:
        if state.config.read_only:
            return make_error(
                RuntimeError("read-only mode: wren_context_build is disabled")
            )

        def _build() -> dict[str, Any]:
            from wren.context import build_json

            manifest = build_json(project)
            target = project / "target" / "mdl.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(manifest), encoding="utf-8")
            return manifest

        try:
            manifest = await run_blocked(state, _build)
        except Exception as exc:
            return make_error(exc)
        models = len(manifest.get("models", []))
        views = len(manifest.get("views", []))
        return make_success(
            content=f"Built: {models} models, {views} views -> target/mdl.json",
            data={
                "models": models,
                "views": views,
                "path": str(project / "target" / "mdl.json"),
            },
        )

    @mcp.tool(
        description=(
            "Validate the MDL project: YAML structure + view SQL dry-plan + "
            "description checks. Returns structured diagnostics; never throws "
            "for validation issues. Read-only."
        )
    )
    async def wren_context_validate() -> dict[str, Any]:
        def _validate() -> list[Any]:
            from wren.context import validate_project

            return validate_project(project)

        try:
            errors = await run_blocked(state, _validate)
        except Exception as exc:
            return make_error(exc)
        err_list = [
            {"message": getattr(e, "message", str(e)), "path": getattr(e, "path", None)}
            for e in errors
        ]
        ok = not err_list
        return make_success(
            content="Valid" if ok else f"{len(err_list)} validation error(s)",
            data={"ok": ok, "errors": err_list},
        )

    @mcp.tool(
        description=(
            "Print business rules (knowledge/rules/ + legacy instructions.md) "
            "for LLM consumption. Read-only."
        )
    )
    async def wren_context_instructions() -> dict[str, Any]:
        def _load() -> tuple[str, bool]:
            from wren.context import load_rules

            return load_rules(project)

        try:
            content, _used_legacy = await run_blocked(state, _load)
        except Exception as exc:
            return make_error(exc)
        return make_success(
            content=content or "(no business rules defined)",
            data={"length": len(content)},
        )
