from __future__ import annotations

import threading
import time
import uuid

from fastapi.testclient import TestClient


def _get_access_token(client: TestClient) -> tuple[str, str]:
    """Helper: pair a device and return (device_id, access_token)."""
    device_id = str(uuid.uuid4())
    r = client.post("/api/v1/companion/pair/start", json={"device_id": device_id})
    request_id = r.json()["request_id"]
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


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _heartbeat(client: TestClient, access_token: str) -> None:
    response = client.post("/api/v1/companion/heartbeat", headers=_auth(access_token))
    assert response.status_code == 200


def _call_tool_and_approve(client: TestClient, access_token: str, tool_name: str, arguments: dict) -> dict:
    """
    Full companion flow: starts tool call in background thread, approves + executes via
    companion endpoints, then joins the thread and returns the gateway's final response.
    """
    headers = _auth(access_token)
    _heartbeat(client, access_token)
    result_container: dict = {}

    def make_tool_call() -> None:
        r = client.post("/api/v1/tool-call", json={"tool_name": tool_name, "arguments": arguments})
        result_container["response"] = r

    t = threading.Thread(target=make_tool_call, daemon=True)
    t.start()

    # Poll until the pending request appears
    request_id = None
    for _ in range(40):
        time.sleep(0.15)
        pending = client.get("/api/v1/companion/pending-requests", headers=headers).json().get("pending", [])
        if pending:
            request_id = pending[0]["request_id"]
            break

    assert request_id is not None, "Pending request never appeared"

    # Approve
    r_approve = client.post(f"/api/v1/approval-requests/{request_id}/approve")
    assert r_approve.status_code == 200

    # Execute and confirm
    r_confirm = client.post(
        f"/api/v1/approval-requests/{request_id}/confirm",
        json={"state": "executed", "result": {"memory_id": "mem_abc"}},
        headers=headers,
    )
    assert r_confirm.status_code == 200

    t.join(timeout=5)
    assert not t.is_alive(), "Tool call thread did not finish in time"
    return result_container["response"]


def test_tool_call_excluded_tool_rejected_by_allowlist(client: TestClient) -> None:
    r = client.post("/api/v1/tool-call", json={"tool_name": "attach_artifact", "arguments": {}})
    assert r.status_code == 403


def test_tool_call_auto_approved_tier1_rejected_at_tool_call_endpoint(client: TestClient) -> None:
    # TIER 1 (read-only) tools are not routed through the approval flow
    r = client.post("/api/v1/tool-call", json={"tool_name": "search_memories", "arguments": {}})
    assert r.status_code == 404


def test_same_idempotency_key_different_payload_returns_409(client: TestClient) -> None:
    _, access_token = _get_access_token(client)
    _heartbeat(client, access_token)
    idem_key = str(uuid.uuid4())

    # First call starts the long-poll in a thread
    first_done = threading.Event()

    def first_call() -> None:
        client.post(
            "/api/v1/tool-call",
            json={"tool_name": "create_memory", "arguments": {"title": "A"}},
            headers={"Idempotency-Key": idem_key},
        )
        first_done.set()

    t = threading.Thread(target=first_call, daemon=True)
    t.start()

    # Wait until the request is in DB, then send conflicting payload
    for _ in range(30):
        time.sleep(0.1)
        rows = client.get(
            "/api/v1/companion/pending-requests",
            headers=_auth(access_token),
        ).json().get("pending", [])
        if rows:
            break

    r = client.post(
        "/api/v1/tool-call",
        json={"tool_name": "create_memory", "arguments": {"title": "B"}},
        headers={"Idempotency-Key": idem_key},
    )
    assert r.status_code == 409

    # Clean up — deny the first request so the thread finishes
    if rows:
        client.post(f"/api/v1/approval-requests/{rows[0]['request_id']}/deny")
    t.join(timeout=5)


def test_companion_confirm_full_lifecycle(client: TestClient) -> None:
    _, access_token = _get_access_token(client)
    r = _call_tool_and_approve(client, access_token, "create_memory", {"title": "T"})
    assert r.status_code == 200
    assert r.json()["result"]["memory_id"] == "mem_abc"


def test_companion_deny_returns_403_to_client(client: TestClient) -> None:
    _, access_token = _get_access_token(client)
    _heartbeat(client, access_token)
    result_container: dict = {}

    def make_call() -> None:
        r = client.post("/api/v1/tool-call", json={"tool_name": "create_memory", "arguments": {}})
        result_container["r"] = r

    t = threading.Thread(target=make_call, daemon=True)
    t.start()

    for _ in range(30):
        time.sleep(0.15)
        pending = client.get(
            "/api/v1/companion/pending-requests", headers=_auth(access_token)
        ).json().get("pending", [])
        if pending:
            break

    assert pending
    client.post(f"/api/v1/approval-requests/{pending[0]['request_id']}/deny")

    t.join(timeout=5)
    assert result_container["r"].status_code == 403


def test_invalid_state_transition_returns_409(client: TestClient) -> None:
    _, access_token = _get_access_token(client)
    _heartbeat(client, access_token)

    # Start and immediately try to confirm (skipping approve) — should fail
    result_container: dict = {}

    def make_call() -> None:
        r = client.post("/api/v1/tool-call", json={"tool_name": "create_memory", "arguments": {}})
        result_container["r"] = r

    t = threading.Thread(target=make_call, daemon=True)
    t.start()

    for _ in range(30):
        time.sleep(0.15)
        pending = client.get(
            "/api/v1/companion/pending-requests", headers=_auth(access_token)
        ).json().get("pending", [])
        if pending:
            break

    request_id = pending[0]["request_id"]
    # Can't jump from pending → executed (must go through approved first)
    r = client.post(
        f"/api/v1/approval-requests/{request_id}/confirm",
        json={"state": "executed"},
        headers=_auth(access_token),
    )
    assert r.status_code == 409

    # Clean up
    client.post(f"/api/v1/approval-requests/{request_id}/deny")
    t.join(timeout=5)


def test_unauthenticated_companion_endpoint_returns_401(client: TestClient) -> None:
    r = client.get("/api/v1/companion/pending-requests")
    assert r.status_code == 401


def test_poll_endpoint_returns_state(client: TestClient) -> None:
    _, access_token = _get_access_token(client)
    _heartbeat(client, access_token)

    result_container: dict = {}

    def make_call() -> None:
        r = client.post("/api/v1/tool-call", json={"tool_name": "create_memory", "arguments": {}})
        result_container["r"] = r

    t = threading.Thread(target=make_call, daemon=True)
    t.start()

    for _ in range(30):
        time.sleep(0.15)
        pending = client.get(
            "/api/v1/companion/pending-requests", headers=_auth(access_token)
        ).json().get("pending", [])
        if pending:
            break

    request_id = pending[0]["request_id"]
    r = client.get(f"/api/v1/tool-call/{request_id}")
    assert r.status_code == 200
    assert r.json()["state"] == "pending"

    # Clean up
    client.post(f"/api/v1/approval-requests/{request_id}/deny")
    t.join(timeout=5)
