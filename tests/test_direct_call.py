from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import Settings


def test_direct_call_rejects_non_tier1_tools(client: TestClient) -> None:
    response = client.post("/api/v1/direct-call", json={"tool_name": "create_memory", "arguments": {}})
    assert response.status_code == 403
    assert response.json()["detail"] == {"error": "tool_not_exposed", "tool": "create_memory"}


def test_direct_call_executes_tier1_tool_via_memory_hub_subprocess(tmp_path: Path) -> None:
    settings = Settings(
        db_path=tmp_path / "gateway.db",
        jwt_secret="test-secret",
        memory_hub_path="python3 -m memory_hub.cli",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/direct-call",
            json={"tool_name": "list_recent_memories", "arguments": {"project": "gateway", "limit": 5}},
        )

    assert response.status_code == 200
    payload = response.json()
    assert "results" in payload
    assert isinstance(payload["results"], list)
