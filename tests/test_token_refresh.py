from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient


def _do_pair(client: TestClient) -> str:
    """Helper: full pair flow, returns plaintext refresh_token."""
    device_id = str(uuid.uuid4())
    r = client.post("/api/v1/companion/pair/start", json={"device_id": device_id})
    assert r.status_code == 200
    request_id = r.json()["request_id"]

    r = client.post(
        f"/approve/device/{request_id}/action",
        data={"action": "approve"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 200

    r = client.get(f"/api/v1/companion/pair/poll/{request_id}")
    assert r.json()["status"] == "approved"
    return device_id, r.json()["refresh_token"]


def test_token_refresh_returns_access_token_and_rotates_refresh_token(client: TestClient) -> None:
    device_id, refresh_token = _do_pair(client)

    r = client.post(
        "/api/v1/companion/token/refresh",
        json={"device_id": device_id, "refresh_token": refresh_token},
    )
    assert r.status_code == 200
    data = r.json()
    assert "access_token" in data
    assert "expires_at" in data
    assert "refresh_token" in data
    assert data["refresh_token"] != refresh_token  # rotated


def test_old_refresh_token_rejected_after_rotation(client: TestClient) -> None:
    device_id, refresh_token = _do_pair(client)
    client.post("/api/v1/companion/token/refresh", json={"device_id": device_id, "refresh_token": refresh_token})

    r = client.post(
        "/api/v1/companion/token/refresh",
        json={"device_id": device_id, "refresh_token": refresh_token},
    )
    assert r.status_code == 401


def test_access_token_is_valid_jwt_with_device_id(client: TestClient, settings) -> None:
    device_id, refresh_token = _do_pair(client)

    r = client.post(
        "/api/v1/companion/token/refresh",
        json={"device_id": device_id, "refresh_token": refresh_token},
    )
    token = r.json()["access_token"]
    payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    assert payload["device_id"] == device_id


def test_revoked_device_cannot_refresh(client: TestClient) -> None:
    device_id, refresh_token = _do_pair(client)
    client.post(f"/api/v1/companion/devices/{device_id}/revoke", json={"refresh_token": refresh_token})

    r2 = client.post(
        "/api/v1/companion/token/refresh",
        json={"device_id": device_id, "refresh_token": refresh_token},
    )
    assert r2.status_code == 401
