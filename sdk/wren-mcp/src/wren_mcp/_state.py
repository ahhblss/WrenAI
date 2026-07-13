"""Server state: per-project engine state + startup config.

A ``ServerState`` is the per-project engine state: manifest source, resolved
datasource + connection_info, cached connector + memory store, and the two
concurrency locks. It is built two ways:

  - ``from_config`` (single-project mode): resolves a profile via
    ``ProfileConnectionProvider`` at startup - the backward-compatible path.
  - ``from_rest`` (multi-project mode): constructed from connection info
    fetched from the wren-datasource REST service by ``ProjectRegistry``.

The ``WrenEngine`` is rebuilt per tool call so manifest changes are picked up
read-through, but the DB connector is cached for the state's lifetime.

wren-mcp does NOT depend on wren-langchain / wren-pydantic: it consumes the
shared provider trio (``wren.providers``) and ``WrenEngine`` directly.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from wren.engine import WrenEngine
from wren.providers import (
    ProfileConnectionProvider,
    ProjectMDLSource,
    WrenToolkitInitError,
)

if TYPE_CHECKING:
    import pyarrow as pa
    from wren.memory.store import MemoryStore


class ServerConfig:
    """Startup configuration for the wren-mcp server."""

    def __init__(
        self,
        *,
        project_path: Path | None = None,
        profile: str | None = None,
        host: str = "127.0.0.1",
        port: int = 8765,
        token: str | None = None,
        read_only: bool = False,
        tools: str = "all",
        workers: int = 1,
        datasource_url: str | None = None,
        datasource_token: str | None = None,
        default_project: str | None = None,
    ):
        self.project_path = project_path
        self.profile = profile
        self.host = host
        self.port = port
        self.token = token
        self.read_only = read_only
        # "tier1" = the 6 core SDK tools; "all" = full CLI surface.
        self.tools = tools
        self.workers = workers
        # Multi-project mode: when set, connection info is fetched per-request
        # from the wren-datasource REST service instead of pinning one project.
        self.datasource_url = datasource_url
        self.datasource_token = datasource_token
        # Default project id used when a request has no X-Wren-Project header
        # (single-project mode pins this to "default"; multi-project mode uses
        # the project id registered as the default, if any).
        self.default_project = default_project


class ServerState:
    """Per-project engine state for one Wren project + connection.

    Holds the manifest source, resolved datasource + connection_info, and a
    per-state pair of ``asyncio.Lock``s serializing engine/connector and
    memory calls. The engine is rebuilt per tool call (read-through manifest);
    the connector and MemoryStore are cached.
    """

    def __init__(
        self,
        *,
        project_id: str,
        project_path: Path,
        datasource: str | None,
        connection_info: dict[str, Any],
        memory_collection_prefix: str | None,
        mdl_source: ProjectMDLSource,
        config: ServerConfig | None = None,
        tool_timeout: float,
    ):
        self.project_id = project_id
        self.project_path = project_path
        self._datasource = datasource
        self._connection_info = connection_info
        # Per-project Qdrant collection isolation. None = MemoryStore default
        # (single-project backward compat). Multi-project uses "wren_{id}".
        self.memory_collection_prefix = memory_collection_prefix
        self._mdl_source = mdl_source
        self.config = config
        # Connector cached at the state level so DB auth happens once.
        self._connector_cache: Any = None
        # MemoryStore (Qdrant client + Ark embedding client) cached on first use.
        self._memory_store_cache: MemoryStore | None = None
        # Serializes all engine/connector calls - blocking, not concurrency-safe
        # (one cached psycopg connection, etc.).
        self.engine_lock = asyncio.Lock()
        # Memory (Qdrant/Ark) calls share no state with engine/connector calls,
        # so they run under a separate lock - an embedding call can proceed
        # while a SQL query runs, and vice versa.
        self.memory_lock = asyncio.Lock()
        self.tool_timeout = tool_timeout

    # ── Engine construction (mirrors WrenToolkit._build_engine) ──────────

    def build_engine(self) -> WrenEngine:
        """Construct a fresh WrenEngine with a read-through manifest.

        The connector is reused across calls when available so DB
        authentication only happens once per state lifetime.
        """
        manifest = self._mdl_source.load_manifest()
        manifest_str = base64.b64encode(json.dumps(manifest).encode("utf-8")).decode()
        engine = WrenEngine(
            manifest_str=manifest_str,
            data_source=self._datasource,
            connection_info=self._connection_info,
        )
        if self._connector_cache is not None:
            engine._connector = self._connector_cache
        return engine

    # ── Direct engine API (sync; call via _bridge.run_blocked) ───────────

    def query(self, sql: str, limit: int | None = None) -> pa.Table:
        engine = self.build_engine()
        try:
            result = engine.query(sql, limit=limit)
        finally:
            self._connector_cache = engine._connector
        return result

    def dry_plan(self, sql: str) -> str:
        return self.build_engine().dry_plan(sql)

    def dry_run(self, sql: str) -> None:
        engine = self.build_engine()
        try:
            engine.dry_run(sql)
        finally:
            self._connector_cache = engine._connector

    def load_manifest(self) -> dict[str, Any]:
        return self._mdl_source.load_manifest()

    @property
    def memory_enabled(self) -> bool:
        # Process-level: memory is on iff a Qdrant server is configured.
        return bool(os.environ.get("QDRANT_URL"))

    def memory_store(self) -> MemoryStore:
        """Lazily open + cache the Qdrant-backed MemoryStore.

        ``collection_prefix`` isolates projects sharing one Qdrant instance
        (multi-project mode). None falls back to the MemoryStore default.
        """
        if self._memory_store_cache is None:
            from wren.memory.store import MemoryStore  # noqa: PLC0415

            self._memory_store_cache = MemoryStore(
                collection_prefix=self.memory_collection_prefix
            )
        return self._memory_store_cache

    def connection_info(self) -> dict[str, Any]:
        return dict(self._connection_info)

    def datasource(self) -> str | None:
        return self._datasource

    def close(self) -> None:
        """Close cached connector + memory store. Called on eviction/shutdown."""
        if self._connector_cache is not None:
            close = getattr(self._connector_cache, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
            self._connector_cache = None
        if self._memory_store_cache is not None:
            close = getattr(self._memory_store_cache, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
            self._memory_store_cache = None

    # ── Factories ────────────────────────────────────────────────────────

    @classmethod
    def from_config(cls, config: ServerConfig) -> ServerState:
        """Single-project mode: resolve a profile locally at startup.

        Mirrors WrenToolkit.from_project. ``project_id`` is pinned to
        ``"default"`` and ``memory_collection_prefix`` to None (MemoryStore
        default) for backward compatibility.
        """
        if config.project_path is None:
            raise WrenToolkitInitError(
                "--project is required in single-project mode "
                "(or use --datasource-url for multi-project mode)."
            )
        project_path = config.project_path.expanduser().resolve()

        if not (project_path / "wren_project.yml").exists():
            raise WrenToolkitInitError(
                f"wren_project.yml not found at {project_path}. "
                "Is this a Wren project? Run `wren context init` to create one."
            )
        if not (project_path / "target" / "mdl.json").exists():
            raise WrenToolkitInitError(
                f"target/mdl.json not found at {project_path}/target/mdl.json. "
                "Run `wren context build` first."
            )

        cls._load_project_dotenv(project_path)

        mdl_source = ProjectMDLSource(project_path=project_path)
        connection = ProfileConnectionProvider(
            project_path=project_path,
            explicit_profile=config.profile,
        )
        tool_timeout = float(os.getenv("WREN_MCP_TOOL_TIMEOUT", "120"))

        return cls(
            project_id="default",
            project_path=project_path,
            datasource=connection.datasource(),
            connection_info=connection.connection_info(),
            memory_collection_prefix=None,
            mdl_source=mdl_source,
            config=config,
            tool_timeout=tool_timeout,
        )

    @classmethod
    def from_rest(
        cls,
        *,
        project_id: str,
        project_path: str,
        datasource: str,
        connection_info: dict[str, Any],
        memory_collection_prefix: str,
        tool_timeout: float,
        config: ServerConfig | None = None,
    ) -> ServerState:
        """Multi-project mode: build from REST-resolved connection info."""
        p = Path(project_path)
        if not (p / "target" / "mdl.json").exists():
            raise WrenToolkitInitError(
                f"target/mdl.json not found at {p}/target/mdl.json. "
                "Run `wren context build` first."
            )
        cls._load_project_dotenv(p)
        return cls(
            project_id=project_id,
            project_path=p,
            datasource=datasource,
            connection_info=connection_info,
            memory_collection_prefix=memory_collection_prefix,
            mdl_source=ProjectMDLSource(project_path=p),
            config=config,
            tool_timeout=tool_timeout,
        )

    @staticmethod
    def _load_project_dotenv(project_path: Path) -> None:
        """Load ``<project>/.env`` into ``os.environ`` if present.

        Required so profile secrets (``${POSTGRES_PASSWORD}`` etc.) resolve
        when the server is started from outside the project directory.
        ``override=False`` so shell-exported values still win.
        """
        env_path = project_path / ".env"
        if not env_path.exists():
            return
        try:
            from dotenv import load_dotenv  # noqa: PLC0415
        except ImportError:
            return
        load_dotenv(env_path, override=False)
