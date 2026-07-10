"""Server state: the long-lived engine + providers + concurrency lock.

One ``ServerState`` per running wren-mcp process, pinned to a single Wren
project + profile at startup (single-project model, per the v1 design). The
``WrenEngine`` is rebuilt per tool call so manifest changes are picked up
read-through, but the DB connector and MemoryStore are cached for the process
lifetime - the same pattern as the wren-langchain / wren-pydantic WrenToolkit.

wren-mcp does NOT depend on either SDK package: it consumes the shared
provider trio (``wren.providers``) and ``WrenEngine`` directly, replicating
only the thin ``_build_engine`` / ``from_project`` glue.
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
    NoopMemoryProvider,
    ProfileConnectionProvider,
    ProjectMDLSource,
    QdrantMemoryProvider,
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
        project_path: Path,
        profile: str | None = None,
        host: str = "127.0.0.1",
        port: int = 8765,
        token: str | None = None,
        read_only: bool = False,
        tools: str = "all",
        workers: int = 1,
    ):
        self.project_path = project_path
        self.profile = profile
        self.host = host
        self.port = port
        self.token = token
        self.read_only = read_only
        # "tier1" = the 6 core SDK tools; "all" = full CLI surface (minus
        # long-running / destructive commands).
        self.tools = tools
        # uvicorn worker process count. >1 spawns independent processes, each
        # with its own engine/connector/lock, to scale SQL concurrency past the
        # GIL and the single-process engine_lock. Costs N x memory and N x
        # DB/Qdrant connections. workers==1 keeps the in-process server so
        # state.close() runs on shutdown.
        self.workers = workers


class ServerState:
    """Long-lived engine state for one Wren project + profile.

    Holds the provider trio (resolved once at startup) and a process-wide
    ``asyncio.Lock`` that serializes every engine/connector/memory call. The
    engine itself is rebuilt per tool call (read-through manifest); the
    connector and MemoryStore are cached.
    """

    def __init__(
        self,
        *,
        project_path: Path,
        mdl_source: ProjectMDLSource,
        connection: ProfileConnectionProvider,
        memory_provider: QdrantMemoryProvider | NoopMemoryProvider,
        config: ServerConfig,
    ):
        self.project_path = project_path
        self._mdl_source = mdl_source
        self._connection = connection
        self._memory = memory_provider
        self.config = config
        # Connector cached at the state level so DB auth happens once.
        self._connector_cache: Any = None
        # MemoryStore (Qdrant client + Ark embedding client) cached on first use.
        self._memory_store_cache: MemoryStore | None = None
        # Serializes all engine/connector/memory calls - they are blocking and
        # not concurrency-safe (one cached psycopg connection, etc.).
        self.engine_lock = asyncio.Lock()
        # Memory (Qdrant/Ark) calls share no state with engine/connector calls
        # (separate cached MemoryStore vs cached DB connection), so they run
        # under a separate lock - an embedding call can proceed while a SQL
        # query runs, and vice versa. See _bridge.run_memory_blocked.
        self.memory_lock = asyncio.Lock()
        # Hard ceiling on any single tool call. A hung connector/embedding
        # call can't hold its lock longer than this: run_blocked /
        # run_memory_blocked release the lock on timeout so the server stays
        # responsive (the worker thread itself can't be force-stopped, but it
        # no longer blocks other calls). Defaults high enough that normal
        # queries never trip.
        self.tool_timeout = float(os.getenv("WREN_MCP_TOOL_TIMEOUT", "120"))

    # ── Engine construction (mirrors WrenToolkit._build_engine) ──────────

    def build_engine(self) -> WrenEngine:
        """Construct a fresh WrenEngine with a read-through manifest.

        The connector is reused across calls when available so DB
        authentication only happens once per server lifetime.
        """
        manifest = self._mdl_source.load_manifest()
        manifest_str = base64.b64encode(json.dumps(manifest).encode("utf-8")).decode()
        engine = WrenEngine(
            manifest_str=manifest_str,
            data_source=self._connection.datasource(),
            connection_info=self._connection.connection_info(),
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
        return self._memory.enabled

    def memory_store(self) -> MemoryStore:
        """Lazily open + cache the Qdrant-backed MemoryStore."""
        if self._memory_store_cache is None:
            self._memory_store_cache = self._memory.open()
        return self._memory_store_cache

    def connection_info(self) -> dict[str, Any]:
        return self._connection.connection_info()

    def datasource(self) -> str | None:
        return self._connection.datasource()

    def close(self) -> None:
        """Close cached connector + memory store. Called on server shutdown."""
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

    # ── Factory ──────────────────────────────────────────────────────────

    @classmethod
    def from_config(cls, config: ServerConfig) -> ServerState:
        """Build a ServerState from startup config (mirrors WrenToolkit.from_project)."""
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
        memory_provider = cls._resolve_memory_provider()

        return cls(
            project_path=project_path,
            mdl_source=mdl_source,
            connection=connection,
            memory_provider=memory_provider,
            config=config,
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

    @staticmethod
    def _resolve_memory_provider() -> QdrantMemoryProvider | NoopMemoryProvider:
        # Memory is enabled when a Qdrant server is configured (QDRANT_URL).
        # Without it, memory tools are auto-dropped at registration time.
        if os.environ.get("QDRANT_URL"):
            return QdrantMemoryProvider()
        return NoopMemoryProvider()
