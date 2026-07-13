"""Shared test fixtures: temp DB, app, authenticated TestClient."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from wren_datasource._app import build_app


@pytest.fixture
def app(tmp_path):
    return build_app(token="test-token", db_path=tmp_path / "test.db")


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer test-token"}
