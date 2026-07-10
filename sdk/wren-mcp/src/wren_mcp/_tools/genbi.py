"""Tier 2 GenBI deploy tool.

``wren_genbi_deploy`` verifies a registered app and ships it to the user's
Vercel or Cloudflare account - an irreversible, outward-facing action. It is
gated by ``config.read_only`` and returns a clear error otherwise. The deploy
token is read from the environment / project ``.env`` (``VERCEL_TOKEN`` /
``CLOUDFLARE_API_TOKEN``) and is never accepted as a tool argument, so it
cannot leak into agent transcripts.

``wren genbi open`` (local preview server, ``serve_forever``) has no MCP
equivalent - it blocks forever and would hold the engine lock. Run it from
the CLI.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Literal

from wren_mcp._bridge import run_blocked
from wren_mcp._envelope import make_error, make_success

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from wren_mcp._state import ServerState

# App names become a path segment under <project>/apps/. Constrain to a slug
# so a crafted name (e.g. "../../etc") can't escape apps/ - mirrors
# genbi/cli._APP_NAME_RE.
_APP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def register(mcp: FastMCP, state: ServerState) -> None:
    project = state.project_path

    @mcp.tool(
        description=(
            "Verify then deploy a registered GenBI app to Vercel or "
            "Cloudflare. IRREVERSIBLE and outward-facing - the app is "
            "published to a public URL under the user's account. The deploy "
            "token is read from the environment (VERCEL_TOKEN / "
            "CLOUDFLARE_API_TOKEN) or the project .env, never as an argument. "
            "Requires the app to be registered (`wren genbi register`) and "
            "built under apps/<name>/."
        )
    )
    async def wren_genbi_deploy(
        name: str,
        provider: Literal["vercel", "cloudflare"] = "vercel",
        prod: bool = False,
    ) -> dict[str, Any]:
        if state.config.read_only:
            return make_error(
                RuntimeError("read-only mode: wren_genbi_deploy is disabled")
            )
        if not _APP_NAME_RE.fullmatch(name):
            return make_error(
                ValueError(
                    "invalid app name: use letters, numbers, '_' or '-' "
                    "(no path separators, not starting with a dot)."
                )
            )

        def _deploy() -> dict[str, Any]:
            from datetime import date  # noqa: PLC0415

            from wren.genbi.index import get_app, update_app  # noqa: PLC0415
            from wren.genbi.providers import get_provider  # noqa: PLC0415
            from wren.genbi.providers.base import DeployError  # noqa: PLC0415
            from wren.genbi.tokens import resolve_token  # noqa: PLC0415
            from wren.genbi.verify import verify_app  # noqa: PLC0415

            entry = get_app(project, name)
            if entry is None:
                raise ValueError(
                    f"app {name!r} is not registered. "
                    f"Run `wren genbi register {name}` first."
                )
            app_dir = project / "apps" / name
            if not app_dir.is_dir():
                raise ValueError(
                    f"no app found at {app_dir}. Build it first (`wren genbi build`)."
                )

            adapter = get_provider(provider)

            # Preflight - a broken app never reaches a public URL.
            result = verify_app(app_dir, data_mode=entry.get("data_mode", "snapshot"))
            if not result.passed:
                raise ValueError("verify failed: " + "; ".join(result.failures))

            token = resolve_token(adapter.env_token_var, project)
            if not token:
                raise ValueError(
                    f"no {adapter.env_token_var} found. Export it or add it "
                    "to the project's .env. Never pass tokens as tool "
                    "arguments - they leak into transcripts."
                )

            try:
                deployment = adapter.deploy(
                    app_dir,
                    app_name=name,
                    token=token,
                    prod=prod,
                    link=entry.get("deploy"),
                )
            except DeployError as exc:
                raise RuntimeError(f"deploy failed: {exc}") from exc

            deploy_state = {
                "provider": adapter.name,
                "project_id": deployment.project_id,
                "org_id": deployment.org_id,
                "account_id": deployment.account_id,
                "last_url": deployment.url,
                "last_deployed_at": date.today().isoformat(),
                "environment": deployment.environment,
            }
            update_app(project, name, status="deployed", deploy=deploy_state)
            return {
                "url": deployment.url,
                "environment": deployment.environment,
                "provider": adapter.name,
            }

        try:
            result = await run_blocked(state, _deploy)
        except Exception as exc:
            return make_error(exc)
        return make_success(
            content=f"Deployed {name} ({result['environment']}): {result['url']}",
            data={"name": name, **result},
        )
