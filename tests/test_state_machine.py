import threading
import time
import pytest
from fastapi.testclient import TestClient

def test_state_machine_happy_path(client: TestClient):
    result_container = {}
    
    def make_call():
        res = client.post("/api/v1/tool-call", json={"tool_name": "create_memory", "arguments": {"a": 1}}, headers={"Idempotency-Key": "test-key-1"})
        result_container["res"] = res

    t = threading.Thread(target=make_call)
    t.start()
    
    # Give it time to insert
    time.sleep(0.5)
    
    db = client.app.state.db
    row = db.execute("SELECT request_id FROM approval_requests WHERE idempotency_key = 'test-key-1'").fetchone()
    assert row is not None
    req_id = row["request_id"]
    
    r_approve = client.post(f"/api/v1/approval-requests/{req_id}/approve")
    assert r_approve.status_code == 200
    
    r_confirm = client.post(f"/api/v1/approval-requests/{req_id}/confirm", json={"state": "executed", "result": {"success": True}})
    assert r_confirm.status_code == 200
    
    t.join(timeout=2.0)
    res = result_container.get("res")
    assert res is not None
    assert res.status_code == 200
    assert res.json()["result"] == {"success": True}

    # Test idempotency (cached)
    res2 = client.post("/api/v1/tool-call", json={"tool_name": "create_memory", "arguments": {"a": 1}}, headers={"Idempotency-Key": "test-key-1"})
    assert res2.status_code == 200
    assert res2.json()["result"] == {"success": True}
    
    # Test conflict (same key, different args)
    res3 = client.post("/api/v1/tool-call", json={"tool_name": "create_memory", "arguments": {"b": 2}}, headers={"Idempotency-Key": "test-key-1"})
    assert res3.status_code == 409

def test_state_machine_timeout(client: TestClient):
    # override timeout
    client.app.state.settings.approval_timeout_seconds = 1
    
    res = client.post("/api/v1/tool-call", json={"tool_name": "create_memory", "arguments": {"a": 2}}, headers={"Idempotency-Key": "test-key-timeout"})
    
    assert res.status_code == 408
    assert res.json()["error"] == "approval_timeout"
