from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient


def test_pairing_flow_approves_and_returns_refresh_token_once(client: TestClient) -> None:
    start = client.post(
        "/api/v1/companion/pair/start",
        json={"device_id": "device-123", "device_name": "Dennis MacBook"},
    )
    assert start.status_code == 200
    payload = start.json()
    request_id = payload["request_id"]

    verification = client.get(f"/approve/device/{request_id}")
    assert verification.status_code == 200
    assert "device-123" in verification.text

    approve = client.post(f"/approve/device/{request_id}/action", data={"action": "approve"})
    assert approve.status_code == 200

    poll = client.get(f"/api/v1/companion/pair/poll/{request_id}")
    assert poll.status_code == 200
    approved = poll.json()
    assert approved["status"] == "approved"
    assert approved["refresh_token"]
    assert approved["registered_at"]
    assert approved["last_seen"]
    assert approved["refresh_token_expires_at"]

    row = client.app.state.db.execute(
        "SELECT hashed_refresh_token, revoked FROM devices WHERE device_id = ?",
        ("device-123",),
    ).fetchone()
    assert row is not None
    assert row["hashed_refresh_token"] != approved["refresh_token"]
    assert row["revoked"] == 0

    delivered_again = client.get(f"/api/v1/companion/pair/poll/{request_id}")
    assert delivered_again.status_code == 409
    assert delivered_again.json()["detail"] == "refresh_token_already_delivered"


def test_pairing_request_expires_when_polled_after_timeout(client: TestClient) -> None:
    start = client.post("/api/v1/companion/pair/start", json={"device_id": "device-expired"})
    request_id = start.json()["request_id"]
    expired_at = (datetime.now(UTC) - timedelta(seconds=1)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    client.app.state.db.execute(
        "UPDATE pairing_requests SET expires_at = ? WHERE request_id = ?",
        (expired_at, request_id),
    )
    client.app.state.db.commit()

    poll = client.get(f"/api/v1/companion/pair/poll/{request_id}")
    assert poll.status_code == 200
    assert poll.json() == {"status": "expired"}

    verification = client.get(f"/approve/device/{request_id}")
    assert verification.status_code == 410


def test_revoking_device_marks_it_revoked(client: TestClient) -> None:
    start = client.post("/api/v1/companion/pair/start", json={"device_id": "device-revoke"})
    request_id = start.json()["request_id"]
    client.post(f"/approve/device/{request_id}/action", data={"action": "approve"})
    refresh_token = client.get(f"/api/v1/companion/pair/poll/{request_id}").json()["refresh_token"]

    revoke = client.post(
        "/api/v1/companion/devices/device-revoke/revoke",
        json={"refresh_token": refresh_token},
    )
    assert revoke.status_code == 200
    assert revoke.json() == {"revoked": True}

    row = client.app.state.db.execute(
        "SELECT revoked FROM devices WHERE device_id = ?",
        ("device-revoke",),
    ).fetchone()
    assert row is not None
    assert row["revoked"] == 1

    invalid = client.post(
        "/api/v1/companion/devices/device-revoke/revoke",
        json={"refresh_token": refresh_token},
    )
    assert invalid.status_code == 401
    assert invalid.json()["detail"] == "device_revoked"
