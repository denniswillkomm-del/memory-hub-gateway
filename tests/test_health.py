from __future__ import annotations

import uuid

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


def test_health_companion_returns_online_after_heartbeat(client: TestClient) -> None:
    device_id = str(uuid.uuid4())
    start = client.post("/api/v1/companion/pair/start", json={"device_id": device_id})
    request_id = start.json()["request_id"]
    client.post(
        f"/approve/device/{request_id}/action",
        data={"action": "approve"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    refresh_token = client.get(f"/api/v1/companion/pair/poll/{request_id}").json()["refresh_token"]
    access_token = client.post(
        "/api/v1/companion/token/refresh",
        json={"device_id": device_id, "refresh_token": refresh_token},
    ).json()["access_token"]

    heartbeat = client.post(
        "/api/v1/companion/heartbeat",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert heartbeat.status_code == 200

    response = client.get("/health/companion")
    assert response.status_code == 200
    data = response.json()
    assert data["companion_online"] is True
    assert data["last_seen"] is not None
