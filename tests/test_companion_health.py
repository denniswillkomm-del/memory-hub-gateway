from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient


def _pair_and_auth(client: TestClient) -> tuple[str, str]:
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
    return device_id, access_token


def _auth(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def test_tool_call_returns_503_when_companion_offline(client: TestClient) -> None:
    response = client.post("/api/v1/tool-call", json={"tool_name": "create_memory", "arguments": {}})
    assert response.status_code == 503
    assert response.json() == {"error": "local_companion_unavailable", "retry_after": 30}


def test_heartbeat_returns_pending_requests_in_submission_order(client: TestClient) -> None:
    device_id, access_token = _pair_and_auth(client)
    stale_time = (datetime.now(UTC) - timedelta(seconds=31)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    client.app.state.db.execute(
        "INSERT INTO companion_heartbeats (device_id, last_heartbeat_at) VALUES (?, ?)",
        (device_id, stale_time),
    )

    now = datetime.now(UTC).replace(microsecond=0)
    client.app.state.db.executemany(
        """
        INSERT INTO approval_requests
        (request_id, idempotency_key, tool_name, arguments_hash, state, created_at, expires_at, result_expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "req-old",
                "idem-old",
                "create_memory",
                "hash-old",
                "pending",
                (now - timedelta(seconds=20)).isoformat().replace("+00:00", "Z"),
                (now + timedelta(seconds=40)).isoformat().replace("+00:00", "Z"),
                (now + timedelta(seconds=600)).isoformat().replace("+00:00", "Z"),
            ),
            (
                "req-new",
                "idem-new",
                "create_work_item",
                "hash-new",
                "pending",
                (now - timedelta(seconds=10)).isoformat().replace("+00:00", "Z"),
                (now + timedelta(seconds=40)).isoformat().replace("+00:00", "Z"),
                (now + timedelta(seconds=600)).isoformat().replace("+00:00", "Z"),
            ),
            (
                "req-expired",
                "idem-expired",
                "create_memory",
                "hash-expired",
                "pending",
                (now - timedelta(seconds=80)).isoformat().replace("+00:00", "Z"),
                (now - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
                (now + timedelta(seconds=600)).isoformat().replace("+00:00", "Z"),
            ),
        ],
    )
    client.app.state.db.commit()

    heartbeat = client.post("/api/v1/companion/heartbeat", headers=_auth(access_token))
    assert heartbeat.status_code == 200
    payload = heartbeat.json()
    assert payload["ok"] is True
    assert [item["request_id"] for item in payload["pending"]] == ["req-old", "req-new"]

    expired_row = client.app.state.db.execute(
        "SELECT state FROM approval_requests WHERE request_id = 'req-expired'"
    ).fetchone()
    assert expired_row["state"] == "expired"


def test_confirm_requires_authentication(client: TestClient) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    client.app.state.db.execute(
        """
        INSERT INTO approval_requests
        (request_id, idempotency_key, tool_name, arguments_hash, state, created_at, expires_at, result_expires_at)
        VALUES (?, ?, ?, ?, 'approved', ?, ?, ?)
        """,
        (
            "req-auth",
            "idem-auth",
            "create_memory",
            "hash-auth",
            now.isoformat().replace("+00:00", "Z"),
            (now + timedelta(seconds=60)).isoformat().replace("+00:00", "Z"),
            (now + timedelta(seconds=600)).isoformat().replace("+00:00", "Z"),
        ),
    )
    client.app.state.db.commit()

    response = client.post(
        "/api/v1/approval-requests/req-auth/confirm",
        json={"state": "executed", "result": {"ok": True}},
    )
    assert response.status_code == 401
