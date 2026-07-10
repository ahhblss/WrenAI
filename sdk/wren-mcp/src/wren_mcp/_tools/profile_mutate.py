"""Tier 2 profile mutation tools: add, remove, switch.

Edit the global ``~/.wren/profiles.yml`` (shared with the ``wren`` CLI). The
running wren-mcp server is pinned to the profile resolved at startup, so
``wren_profile_switch`` does NOT re-route the served connection mid-flight -
restart the server (with ``--profile`` or a new active profile) to serve a
different one. All three are gated by ``config.read_only`` and return a
read-only error envelope when disabled, so agents get actionable feedback
instead of a missing tool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from wren_mcp._bridge import run_blocked
from wren_mcp._envelope import make_error, make_success

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from wren_mcp._state import ServerState


def _validate_connection(name: str) -> tuple[bool, str | None]:
    """Test a saved profile by running ``SELECT 1`` through its connector.

    Mirrors ``profile_cli._validate_connection`` but returns ``(ok, error)``
    instead of printing. The profile is never deleted on failure - the caller
    surfaces the warning so the user can fix fields and retry. Resolves
    ``${VAR}`` placeholders just before handing the connection info to the
    connector; the stored profile keeps the placeholders.
    """
    from pydantic import ValidationError  # noqa: PLC0415
    from wren.connector.factory import get_connector  # noqa: PLC0415
    from wren.model.data_source import DataSource  # noqa: PLC0415
    from wren.profile import (  # noqa: PLC0415
        MissingSecretError,
        _load_raw,
        expand_profile_secrets,
    )

    profile = _load_raw().get("profiles", {}).get(name)
    if not profile:
        return False, "profile not found after save"
    ds_str = profile.get("datasource")
    if not isinstance(ds_str, str) or not ds_str:
        return False, "profile has no datasource"
    try:
        ds = DataSource(ds_str.lower())
    except ValueError:
        return False, f"unknown datasource {ds_str!r}"

    conn_info_dict = {k: v for k, v in profile.items() if k != "datasource"}
    try:
        conn_info_dict = expand_profile_secrets(conn_info_dict)
    except MissingSecretError as exc:
        return False, str(exc)
    try:
        conn_info = ds.get_connection_info(conn_info_dict)
    except (ValidationError, ValueError) as exc:
        return False, f"invalid connection info: {exc}"

    connector = None
    try:
        connector = get_connector(ds, conn_info)
        connector.dry_run("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - surface whatever driver raises
        return False, f"connection failed: {exc}"
    finally:
        if connector is not None:
            close = getattr(connector, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass
    return True, None


def register(mcp: FastMCP, state: ServerState) -> None:
    @mcp.tool(
        description=(
            "Add a connection profile to ~/.wren/profiles.yml (shared with the "
            "CLI). Pass datasource plus a flat fields dict (host, port, user, "
            "password, ...). ${VAR} placeholders in field values are resolved "
            "from the env / project .env at connection time. By default the "
            "profile is validated with SELECT 1; a failed validation is "
            "returned as a warning (the profile is still saved). Does NOT "
            "switch the running server's connection - restart to use it."
        )
    )
    async def wren_profile_add(
        name: str,
        datasource: str,
        fields: dict[str, Any] | None = None,
        activate: bool = False,
        validate: bool = True,
    ) -> dict[str, Any]:
        if state.config.read_only:
            return make_error(
                RuntimeError("read-only mode: wren_profile_add is disabled")
            )

        profile_data: dict[str, Any] = {"datasource": datasource}
        if fields:
            profile_data.update(fields)

        def _add() -> None:
            from wren.profile import add_profile  # noqa: PLC0415

            add_profile(name, profile_data, activate=activate)

        try:
            await run_blocked(state, _add)
        except Exception as exc:
            return make_error(exc)

        data: dict[str, Any] = {
            "name": name,
            "datasource": datasource,
            "active": activate,
        }
        warnings: list[str] = []
        if validate:
            validated, validation_error = await run_blocked(
                state, _validate_connection, name
            )
            data["validated"] = validated
            if validation_error:
                data["validation_error"] = validation_error
                warnings.append(
                    f"profile saved but connection validation failed: {validation_error}"
                )
        return make_success(
            content=f"Profile '{name}' added (datasource={datasource}, active={activate}).",
            data=data,
            warnings=warnings,
        )

    @mcp.tool(
        description=(
            "Remove a profile from ~/.wren/profiles.yml. If it was the active "
            "profile, another is auto-selected. Does NOT affect the running "
            "server's connection."
        )
    )
    async def wren_profile_remove(name: str) -> dict[str, Any]:
        if state.config.read_only:
            return make_error(
                RuntimeError("read-only mode: wren_profile_remove is disabled")
            )

        def _remove() -> bool:
            from wren.profile import remove_profile  # noqa: PLC0415

            return remove_profile(name)

        try:
            found = await run_blocked(state, _remove)
        except Exception as exc:
            return make_error(exc)
        if not found:
            return make_error(ValueError(f"profile {name!r} not found"))
        return make_success(
            content=f"Profile '{name}' removed.",
            data={"name": name, "removed": True},
        )

    @mcp.tool(
        description=(
            "Set the active profile in ~/.wren/profiles.yml. Does NOT re-route "
            "the running server's connection (pinned at startup) - restart the "
            "server to serve the newly active profile."
        )
    )
    async def wren_profile_switch(name: str) -> dict[str, Any]:
        if state.config.read_only:
            return make_error(
                RuntimeError("read-only mode: wren_profile_switch is disabled")
            )

        def _switch() -> bool:
            from wren.profile import switch_profile  # noqa: PLC0415

            return switch_profile(name)

        try:
            found = await run_blocked(state, _switch)
        except Exception as exc:
            return make_error(exc)
        if not found:
            return make_error(ValueError(f"profile {name!r} not found"))
        return make_success(
            content=f"Active profile: {name}",
            data={"name": name, "active": True},
        )
