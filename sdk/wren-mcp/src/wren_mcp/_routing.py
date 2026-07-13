"""Multi-project routing: contextvars + registry + dispatcher + middleware.

wren-mcp serves one or many Wren projects from a single process. Each HTTP
request is routed to a per-project ``ServerState`` by the ``X-Wren-Project``
header (or a process default). ``ServerContext`` is the dispatcher tools
capture - it transparently delegates per-project attributes (``query``,
``engine_lock``, ``project_path``, ...) to the current request's state via the
``current_state`` contextvar, set by ``ProjectRoutingMiddleware``.

Two registry backends:

  - ``SingleProjectRegistry``: one local state built from ``ServerConfig`` at
    startup (backward-compatible single-project mode, no REST service).
  - ``RestProjectRegistry``: lazily fetches connection info from a
    wren-datasource REST service, LRU-caching per-project states.

``ServerContext`` quacks like ``ServerState`` so tool closures (which capture
``state``) need no changes: ``state.query`` / ``state.engine_lock`` /
``state.project_path`` / ``state.config`` all resolve correctly. Per-project
attributes read ``current_state``; process-level attributes (``config``,
``memory_enabled``, ``tool_timeout``) are served directly.

In-process ``call_tool`` (no HTTP middleware) falls back to
``registry.default_state`` so unit tests and single-project mode work without a
header.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from pathlib import Path

    from wren_mcp._state import ServerConfig, ServerState

# Request-scoped routing. Set by ProjectRoutingMiddleware; read by ServerContext.
# A request that flows through the HTTP middleware has both set; an in-process
# call_tool leaves them unset and ServerContext falls back to default_state.
current_state: contextvars.ContextVar[ServerState | None] = contextvars.ContextVar(
    "wren_mcp_current_state", default=None
)
current_project_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "wren_mcp_current_project_id", default=None
)


class ProjectNotFoundError(Exception):
    """Raised when a project id is not registered / not found via REST."""


# ── REST client ───────────────────────────────────────────────────────────


class DatasourceClient:
    """Async REST client for the wren-datasource service (connection resolution)."""

    def __init__(self, base_url: str, token: str | None, timeout: float = 10.0) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._timeout = timeout
        self._client: Any = None

    def _ensure(self):  # httpx.AsyncClient, lazily created
        if self._client is None:
            import httpx  # noqa: PLC0415

            self._client = httpx.AsyncClient(
                timeout=self._timeout, headers=self._headers
            )
        return self._client

    async def get_connection(self, project_id: str) -> dict[str, Any]:
        """GET /projects/{id}/connection -> {datasource, connection_info, ...}."""
        client = self._ensure()
        r = await client.get(f"{self._base}/projects/{project_id}/connection")
        if r.status_code == 404:
            raise ProjectNotFoundError(project_id)
        r.raise_for_status()
        return r.json()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# ── Registries ────────────────────────────────────────────────────────────


class SingleProjectRegistry:
    """One local state (single-project backward-compat mode).

    ``get`` ignores the project_id and always returns the single state.
    ``default_state`` is set so in-process call_tool without a header still
    resolves.
    """

    def __init__(self, state: ServerState) -> None:
        self._state = state
        self.default_state: ServerState | None = state

    async def get(self, project_id: str) -> ServerState:
        return self._state

    def close_all(self) -> None:
        self._state.close()
        self.default_state = None


class RestProjectRegistry:
    """LRU cache of per-project states, fetched from wren-datasource.

    Connection info is fetched on first use of each project_id and cached.
    States past ``WREN_MCP_MAX_PROJECTS`` (default 8) are evicted LRU, closing
    their connector + memory store. ``default_state`` is populated lazily when
    the configured default project is first fetched.
    """

    def __init__(
        self,
        rest: DatasourceClient,
        tool_timeout: float,
        *,
        max_projects: int | None = None,
        default_project: str | None = None,
        config: ServerConfig | None = None,
    ) -> None:
        self._rest = rest
        self._tool_timeout = tool_timeout
        self._max = max_projects or int(os.getenv("WREN_MCP_MAX_PROJECTS", "8"))
        self._default_project = default_project
        self._config = config
        self._states: OrderedDict[str, ServerState] = OrderedDict()
        self._lock = asyncio.Lock()
        self.default_state: ServerState | None = None

    async def get(self, project_id: str) -> ServerState:
        # Fast path: already cached.
        async with self._lock:
            st = self._states.get(project_id)
            if st is not None:
                self._states.move_to_end(project_id)
                return st

        # Fetch outside the lock so concurrent requests for *different* projects
        # don't serialize on the REST round-trip.
        info = await self._rest.get_connection(project_id)
        from wren_mcp._state import ServerState  # noqa: PLC0415

        st = ServerState.from_rest(
            project_id=project_id,
            project_path=info["project_path"],
            datasource=info["datasource"],
            connection_info=info["connection_info"],
            memory_collection_prefix=info.get("memory_collection_prefix")
            or f"wren_{project_id}",
            tool_timeout=self._tool_timeout,
            config=self._config,
        )

        async with self._lock:
            # Another request may have inserted the same id concurrently.
            if project_id in self._states:
                st.close()
                st = self._states[project_id]
                self._states.move_to_end(project_id)
            else:
                self._states[project_id] = st
                while len(self._states) > self._max:
                    _, old = self._states.popitem(last=False)
                    try:
                        old.close()
                    except Exception:
                        pass
            if self._default_project == project_id:
                self.default_state = st
        return st

    def close_all(self) -> None:
        for st in self._states.values():
            try:
                st.close()
            except Exception:
                pass
        self._states.clear()
        self.default_state = None

    async def close(self) -> None:
        self.close_all()
        await self._rest.close()


# ── Dispatcher ────────────────────────────────────────────────────────────


class ServerContext:
    """Process-level context + transparent dispatcher to the current ProjectState.

    Tools capture a ``ServerContext`` (named ``state`` in closures for
    compatibility) and access per-project attributes which delegate to the
    current ``ServerState`` via ``current_state``. Process-level attributes
    (``config``, ``memory_enabled``, ``tool_timeout``) are served directly.
    """

    def __init__(
        self,
        *,
        registry: SingleProjectRegistry | RestProjectRegistry,
        config: ServerConfig,
        memory_enabled: bool,
        default_project: str | None = None,
        rest_client: DatasourceClient | None = None,
    ) -> None:
        self.registry = registry
        self.config = config
        self.memory_enabled = memory_enabled
        self.default_project = default_project
        self.rest_client = rest_client
        self.tool_timeout = float(os.getenv("WREN_MCP_TOOL_TIMEOUT", "120"))

    def _current(self) -> ServerState:
        """Resolve the current request's state, or the default if unrouted."""
        st = current_state.get()
        if st is not None:
            return st
        # In-process call_tool (no middleware) or a headerless request in
        # single-project mode -> the registry's default state.
        if self.registry.default_state is not None:
            return self.registry.default_state
        raise ProjectNotFoundError(
            "no project routed for this request - set the X-Wren-Project header "
            "or configure a default project"
        )

    # ── per-project delegation (read current_state) ──────────────────────

    @property
    def project_id(self) -> str:
        return self._current().project_id

    @property
    def project_path(self) -> Path:  # type: ignore[name-defined]
        return self._current().project_path

    @property
    def engine_lock(self) -> asyncio.Lock:
        return self._current().engine_lock

    @property
    def memory_lock(self) -> asyncio.Lock:
        return self._current().memory_lock

    @property
    def memory_collection_prefix(self) -> str | None:
        return self._current().memory_collection_prefix

    def build_engine(self):  # type: ignore[no-untyped-def]
        return self._current().build_engine()

    def query(self, sql: str, limit: int | None = None):  # type: ignore[no-untyped-def]
        return self._current().query(sql, limit)

    def dry_plan(self, sql: str) -> str:
        return self._current().dry_plan(sql)

    def dry_run(self, sql: str) -> None:
        return self._current().dry_run(sql)

    def load_manifest(self) -> dict[str, Any]:
        return self._current().load_manifest()

    def datasource(self) -> str | None:
        return self._current().datasource()

    def connection_info(self) -> dict[str, Any]:
        return self._current().connection_info()

    def memory_store(self):  # type: ignore[no-untyped-def]
        return self._current().memory_store()

    def close(self) -> None:
        """Close all cached project states (shutdown)."""
        self.registry.close_all()


# ── Middleware ────────────────────────────────────────────────────────────


class ProjectRoutingMiddleware(BaseHTTPMiddleware):
    """Route each request to a project via ``X-Wren-Project`` header.

    Sets ``current_state`` + ``current_project_id`` for the request. With no
    header, falls back to ``ctx.default_project`` (single-project mode pins
    this to "default"). If neither is set, tools fall back to
    ``registry.default_state`` (or raise if there is none).
    """

    def __init__(self, app, ctx: ServerContext) -> None:
        super().__init__(app)
        self._ctx = ctx

    async def dispatch(self, request: Request, call_next):
        pid = request.headers.get("x-wren-project") or self._ctx.default_project
        if pid:
            try:
                st = await self._ctx.registry.get(pid)
            except ProjectNotFoundError:
                return JSONResponse(
                    {"error": f"project {pid!r} not found"}, status_code=404
                )
            current_state.set(st)
            current_project_id.set(pid)
        return await call_next(request)
