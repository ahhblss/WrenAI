"""SQLite-backed store for projects + profiles.

Replaces ``~/.wren/profiles.yml`` as the datasource management backend for
wren-mcp multi-project mode. Two tables:

  - ``profiles(name, datasource, config_json, created_at)``
  - ``projects(id, name, project_path, profile_name, created_at)``

Profiles store the raw dict (with ``${ENV}`` placeholders *unexpanded*) as JSON;
secret expansion happens at resolution time in :mod:`wren_datasource._resolve`,
so the DB never persists expanded credentials. The DB file is ``0600`` on disk,
matching ``profiles.yml``'s permission model.

SQLite calls are synchronous and local (sub-millisecond); REST routes call
these methods directly. If a route ever does heavy DB work, offload it via
``anyio.to_thread.run_sync`` - but for the current CRUD surface that is not
needed.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path.home() / ".wren" / "datasource.db"


class Store:
    """Project + profile registry backed by a single SQLite file."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        # 0600 on the DB file, matching profiles.yml's permission model.
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS profiles (
                    name        TEXT PRIMARY KEY,
                    datasource  TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    created_at  TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS projects (
                    id           TEXT PRIMARY KEY,
                    name         TEXT UNIQUE NOT NULL,
                    project_path TEXT NOT NULL,
                    profile_name TEXT,
                    created_at   TEXT NOT NULL,
                    FOREIGN KEY (profile_name) REFERENCES profiles(name)
                        ON DELETE SET NULL
                );
                """
            )

    # ── Profiles ─────────────────────────────────────────────────────────

    def add_profile(self, name: str, profile: dict[str, Any]) -> dict[str, Any]:
        """Insert or replace a named profile.

        ``datasource`` is pulled out into its own column; the remainder is
        stored as JSON config. Raises ``ValueError`` if no datasource.
        """
        datasource = profile.get("datasource")
        if not datasource:
            raise ValueError("profile must have a 'datasource' field")
        config = {k: v for k, v in profile.items() if k != "datasource"}
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO profiles(name, datasource, config_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (name, str(datasource), json.dumps(config), _now()),
            )
        return self.get_profile(name)  # type: ignore[return-value]

    def list_profiles(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT name, datasource, config_json FROM profiles ORDER BY name"
            ).fetchall()
        return [_profile_row(r) for r in rows]

    def get_profile(self, name: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT name, datasource, config_json FROM profiles WHERE name = ?",
                (name,),
            ).fetchone()
        return _profile_row(row) if row else None

    def delete_profile(self, name: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM profiles WHERE name = ?", (name,))
        return cur.rowcount > 0

    # ── Projects ─────────────────────────────────────────────────────────

    def add_project(
        self,
        *,
        name: str,
        project_path: str,
        profile_name: str | None = None,
    ) -> dict[str, Any]:
        """Register a project. ``profile_name`` (if given) must already exist."""
        if profile_name and self.get_profile(profile_name) is None:
            raise ValueError(f"profile {profile_name!r} not found")
        pid = uuid.uuid4().hex
        with self._conn() as conn:
            try:
                conn.execute(
                    "INSERT INTO projects(id, name, project_path, profile_name, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (pid, name, str(project_path), profile_name, _now()),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"project name {name!r} already exists") from exc
        return self.get_project(pid)  # type: ignore[return-value]

    def list_projects(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, name, project_path, profile_name, created_at "
                "FROM projects ORDER BY name"
            ).fetchall()
        return [_project_row(r) for r in rows]

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, name, project_path, profile_name, created_at "
                "FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
        return _project_row(row) if row else None

    def get_project_by_name(self, name: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, name, project_path, profile_name, created_at "
                "FROM projects WHERE name = ?",
                (name,),
            ).fetchone()
        return _project_row(row) if row else None

    def delete_project(self, project_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        return cur.rowcount > 0

    # ── Migration ────────────────────────────────────────────────────────

    def import_profiles_yml(self, yml_path: Path | str | None = None) -> int:
        """Import profiles from ``~/.wren/profiles.yml``. Returns count imported.

        Existing profiles with the same name are overwritten. Profiles without
        a ``datasource`` field are skipped (they are malformed for our purpose).
        """
        import yaml  # noqa: PLC0415

        path = Path(yml_path) if yml_path else Path.home() / ".wren" / "profiles.yml"
        if not path.exists():
            return 0
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        profiles = data.get("profiles", {}) if isinstance(data, dict) else {}
        count = 0
        for name, prof in profiles.items():
            if not isinstance(prof, dict) or "datasource" not in prof:
                continue
            self.add_profile(name, prof)
            count += 1
        return count


def _profile_row(row: sqlite3.Row) -> dict[str, Any]:
    config = json.loads(row["config_json"])
    return {"name": row["name"], "datasource": row["datasource"], **config}


def _project_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "project_path": row["project_path"],
        "profile_name": row["profile_name"],
        "created_at": row["created_at"],
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
