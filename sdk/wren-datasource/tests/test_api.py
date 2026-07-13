"""API tests via FastAPI TestClient: auth, profile/project lifecycle, connection."""

from __future__ import annotations

from pathlib import Path


def _make_project(tmp_path: Path) -> str:
    """Create a minimal valid Wren project dir (wren_project.yml + target/mdl.json)."""
    p = tmp_path / "proj"
    p.mkdir()
    (p / "wren_project.yml").write_text("name: proj\n")
    (p / "target").mkdir()
    (p / "target" / "mdl.json").write_text('{"models": []}')
    return str(p)


def test_health(client, auth_headers):
    r = client.get("/health", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_auth_required(client):
    assert client.get("/health").status_code == 401


def test_bad_token(client):
    r = client.get("/health", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_profile_lifecycle(client, auth_headers):
    r = client.post(
        "/profiles",
        json={
            "name": "pg",
            "datasource": "postgres",
            "host": "h",
            "password": "secret",
        },
        headers=auth_headers,
    )
    assert r.status_code == 200
    # list redacts
    r = client.get("/profiles", headers=auth_headers)
    prof = r.json()["profiles"][0]
    assert prof["password"] == "***"
    assert prof["datasource"] == "postgres"
    # get redacts
    r = client.get("/profiles/pg", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["profile"]["password"] == "***"
    # delete
    assert client.delete("/profiles/pg", headers=auth_headers).status_code == 200
    assert client.get("/profiles/pg", headers=auth_headers).status_code == 404


def test_project_invalid_path(client, auth_headers, tmp_path):
    r = client.post(
        "/projects",
        json={"name": "bad", "project_path": str(tmp_path / "nope")},
        headers=auth_headers,
    )
    assert r.status_code == 400


def test_project_connection_and_manifest(client, auth_headers, tmp_path):
    proj_path = _make_project(tmp_path)
    client.post(
        "/profiles",
        json={"name": "pg", "datasource": "postgres"},
        headers=auth_headers,
    )
    r = client.post(
        "/projects",
        json={"name": "sales", "project_path": proj_path, "profile_name": "pg"},
        headers=auth_headers,
    )
    assert r.status_code == 200
    pid = r.json()["id"]

    # connection resolution
    r = client.get(f"/projects/{pid}/connection", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["datasource"] == "postgres"
    assert body["memory_collection_prefix"] == f"wren_{pid}"
    assert body["project_path"] == proj_path

    # manifest read-through
    r = client.get(f"/projects/{pid}/manifest", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == {"models": []}

    # delete
    assert client.delete(f"/projects/{pid}", headers=auth_headers).status_code == 200
    assert client.get(f"/projects/{pid}", headers=auth_headers).status_code == 404


def test_project_connection_without_profile(client, auth_headers, tmp_path):
    proj_path = _make_project(tmp_path)
    r = client.post(
        "/projects",
        json={"name": "sales", "project_path": proj_path},
        headers=auth_headers,
    )
    pid = r.json()["id"]
    r = client.get(f"/projects/{pid}/connection", headers=auth_headers)
    assert r.status_code == 400
    assert "no profile" in r.json()["detail"]
