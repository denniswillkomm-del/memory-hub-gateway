from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_health_companion_returns_offline_when_no_heartbeat(client: TestClient) -> None:
    response = client.get("/health/companion")
    assert response.status_code == 200
    data = response.json()
    assert data["companion_online"] is False
    assert data["last_seen"] is None
