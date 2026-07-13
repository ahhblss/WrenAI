"""Store-level tests: SQLite CRUD + profiles.yml import."""

from __future__ import annotations

import pytest

from wren_datasource._store import Store


def test_profile_crud(tmp_path):
    store = Store(tmp_path / "t.db")
    store.add_profile("pg", {"datasource": "postgres", "host": "h", "password": "s"})
    got = store.get_profile("pg")
    assert got["datasource"] == "postgres"
    assert got["host"] == "h"
    assert got["password"] == "s"  # raw (unexpanded) - redaction is at the API layer
    assert "pg" in [p["name"] for p in store.list_profiles()]
    assert store.delete_profile("pg")
    assert store.get_profile("pg") is None


def test_profile_requires_datasource(tmp_path):
    store = Store(tmp_path / "t.db")
    with pytest.raises(ValueError):
        store.add_profile("bad", {"host": "h"})


def test_profile_upsert(tmp_path):
    store = Store(tmp_path / "t.db")
    store.add_profile("pg", {"datasource": "postgres", "host": "h1"})
    store.add_profile("pg", {"datasource": "postgres", "host": "h2"})
    assert store.get_profile("pg")["host"] == "h2"
    assert len(store.list_profiles()) == 1


def test_project_crud(tmp_path):
    store = Store(tmp_path / "t.db")
    store.add_profile("pg", {"datasource": "postgres"})
    proj = store.add_project(name="sales", project_path="/tmp/sales", profile_name="pg")
    assert proj["name"] == "sales"
    assert proj["profile_name"] == "pg"
    assert store.get_project(proj["id"])["name"] == "sales"
    assert store.get_project_by_name("sales")["id"] == proj["id"]
    assert len(store.list_projects()) == 1
    assert store.delete_project(proj["id"])
    assert store.get_project(proj["id"]) is None


def test_project_name_unique(tmp_path):
    store = Store(tmp_path / "t.db")
    store.add_project(name="a", project_path="/tmp/a")
    with pytest.raises(ValueError, match="already exists"):
        store.add_project(name="a", project_path="/tmp/a2")


def test_project_profile_must_exist(tmp_path):
    store = Store(tmp_path / "t.db")
    with pytest.raises(ValueError, match="not found"):
        store.add_project(name="a", project_path="/tmp/a", profile_name="missing")


def test_delete_profile_clears_project_binding(tmp_path):
    """FK ON DELETE SET NULL: deleting a profile leaves the project bound to None."""
    store = Store(tmp_path / "t.db")
    store.add_profile("pg", {"datasource": "postgres"})
    proj = store.add_project(name="sales", project_path="/tmp/sales", profile_name="pg")
    store.delete_profile("pg")
    assert store.get_project(proj["id"])["profile_name"] is None


def test_import_profiles_yml(tmp_path):
    yml = tmp_path / "profiles.yml"
    yml.write_text(
        "profiles:\n"
        "  pg:\n    datasource: postgres\n    host: h\n"
        "  bad:\n    host: h\n"  # no datasource -> skipped
    )
    store = Store(tmp_path / "t.db")
    n = store.import_profiles_yml(yml)
    assert n == 1
    assert store.get_profile("pg")["datasource"] == "postgres"
    assert store.get_profile("bad") is None


def test_import_profiles_yml_missing_file(tmp_path):
    store = Store(tmp_path / "t.db")
    assert store.import_profiles_yml(tmp_path / "nope.yml") == 0
