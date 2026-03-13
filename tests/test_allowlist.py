import pytest
from fastapi.testclient import TestClient

def test_allowlist_auto_approved(client: TestClient):
    response = client.post("/api/v1/tool-call", json={"tool_name": "search_memories"})
    assert response.status_code == 404

def test_allowlist_approval_gated(fast_client: TestClient):
    # TIER 2 tool is rejected immediately while the companion is offline.
    response = fast_client.post("/api/v1/tool-call", json={"tool_name": "create_memory"})
    assert response.status_code == 503
    assert response.json()["error"] == "local_companion_unavailable"

def test_allowlist_excluded(client: TestClient):
    response = client.post("/api/v1/tool-call", json={"tool_name": "attach_artifact"})
    assert response.status_code == 403
    assert response.json() == {"error": "tool_not_exposed", "tool": "attach_artifact"}

def test_allowlist_unknown(client: TestClient):
    response = client.post("/api/v1/tool-call", json={"tool_name": "unknown_tool"})
    assert response.status_code == 403
    assert response.json() == {"error": "tool_not_exposed", "tool": "unknown_tool"}
