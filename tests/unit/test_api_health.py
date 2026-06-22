"""Tests for the HTTP health endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tollgate.api.app import create_api


def test_healthz_returns_ok() -> None:
    client = TestClient(create_api())
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
