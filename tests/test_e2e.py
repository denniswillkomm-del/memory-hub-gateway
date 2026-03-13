"""
End-to-end test: full ChatGPT → Gateway → Companion → (mocked) MemoryHub flow.

Starts a real uvicorn server on a free port so the companion's urllib calls
go through real TCP. Only MemoryHubExecutor.execute_tool is mocked.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import uvicorn

from gateway.app import create_app
from gateway.config import Settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _http(method: str, url: str, body: dict | None = None, token: str | None = None) -> dict[str, Any]:
    import urllib.request as ureq

    data = json.dumps(body).encode() if body is not None else b""
    headers: dict[str, str] = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = ureq.Request(url, data=data or None, headers=headers, method=method)
    with ureq.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _pair_and_get_access_token(gateway_url: str) -> tuple[str, str, str]:
    """Full pairing flow. Returns (device_id, refresh_token, access_token)."""
    import urllib.request as ureq

    device_id = str(uuid.uuid4())
    r = _http("POST", f"{gateway_url}/api/v1/companion/pair/start", {"device_id": device_id})
    request_id = r["request_id"]

    # Approve via HTML form POST
    req = ureq.Request(
        f"{gateway_url}/approve/device/{request_id}/action",
        data=b"action=approve",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    ureq.urlopen(req)

    r2 = _http("GET", f"{gateway_url}/api/v1/companion/pair/poll/{request_id}")
    refresh_token = r2["refresh_token"]

    r3 = _http("POST", f"{gateway_url}/api/v1/companion/token/refresh",
               {"device_id": device_id, "refresh_token": refresh_token})
    return device_id, refresh_token, r3["access_token"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def live_gateway(tmp_path: Path):
    """Real uvicorn gateway on a free port."""
    port = _free_port()
    settings = Settings(
        db_path=tmp_path / "e2e.db",
        jwt_secret="e2e-test-secret-32-bytes-long!!",
        approval_timeout_seconds=10,
        result_ttl_seconds=60,
        companion_heartbeat_timeout_seconds=5,
    )
    app = create_app(settings)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait until ready
    import urllib.request as ureq
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            ureq.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5)
            break
        except Exception:
            time.sleep(0.05)
    else:
        pytest.fail("Gateway did not start within 5 s")

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Fake UI server (no browser, auto-resolves for tests)
# ---------------------------------------------------------------------------


class _FakeUIServer:
    """Drop-in for ApprovalUIServer: immediately calls on_resolve when a request arrives."""

    def __init__(self) -> None:
        self.on_resolve: Any = None

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def add_request(self, req: Any, open_browser: bool = False) -> None:  # noqa: FBT001
        if self.on_resolve:
            self.on_resolve(req.request_id, "approve")


# ---------------------------------------------------------------------------
# E2E test
# ---------------------------------------------------------------------------


def test_companion_loop_full_approval_flow(live_gateway: str, tmp_path: Path) -> None:
    """
    Full flow:
      1. Pair companion with gateway
      2. Start CompanionEventLoop in a background asyncio thread
      3. POST /api/v1/tool-call (simulates ChatGPT)
      4. Companion polls, picks up request, auto-approves via FakeUIServer
      5. execute_tool (mocked) → confirm → gateway returns 200 with result
    """
    from memory_hub.companion import (
        AccessTokenLease,
        CompanionBootstrapManager,
        CompanionClient,
        CompanionEventLoop,
        PairingState,
    )
    from memory_hub.config import Settings as HubSettings, ensure_data_dirs

    gateway_url = live_gateway
    FAKE_RESULT = {"memory_id": "mem_e2e_001"}

    # ── 1. Pair & get tokens ──────────────────────────────────────────────────
    device_id, refresh_token, access_token = _pair_and_get_access_token(gateway_url)

    # ── 2. Build a manager with pre-loaded state (avoid real macOS Keychain) ──
    class _FakeKeychain:
        def __init__(self) -> None:
            self._store: dict[str, str] = {}

        def store_refresh_token(self, dev: str, tok: str) -> None:
            self._store[dev] = tok

        def get_refresh_token(self, dev: str) -> str | None:
            return self._store.get(dev)

        def delete_refresh_token(self, dev: str) -> bool:
            return bool(self._store.pop(dev, None))

    hub_settings = ensure_data_dirs(
        HubSettings(
            db_path=tmp_path / "hub.db",
            artifacts_dir=tmp_path / "artifacts",
            state_dir=tmp_path / "state",
        )
    )
    keychain = _FakeKeychain()
    keychain.store_refresh_token(device_id, refresh_token)

    manager = CompanionBootstrapManager(settings=hub_settings, store=keychain)
    manager._write_state(
        PairingState(device_id=device_id, gateway_url=gateway_url)
    )
    # Pre-load a valid access token lease so the companion doesn't need to refresh immediately
    from datetime import UTC, datetime, timedelta
    manager._access_token_lease = AccessTokenLease(
        access_token=access_token,
        expires_at=(datetime.now(UTC) + timedelta(minutes=14)).isoformat().replace("+00:00", "Z"),
    )

    # ── 3. Build CompanionClient + EventLoop ──────────────────────────────────
    companion = CompanionClient(manager=manager)
    fake_ui = _FakeUIServer()
    event_loop = CompanionEventLoop(client=companion, settings=hub_settings, ui_server=fake_ui)
    fake_ui.on_resolve = event_loop._on_ui_resolve

    # ── 4. Start companion loop in a background thread ────────────────────────
    loop_errors: list[Exception] = []
    loop_started = threading.Event()

    def _run_companion() -> None:
        async def _main() -> None:
            loop_started.set()
            await event_loop.run()

        try:
            asyncio.run(_main())
        except Exception as exc:
            loop_errors.append(exc)

    companion_thread = threading.Thread(target=_run_companion, daemon=True)

    with patch.object(companion.executor, "execute_tool", return_value=FAKE_RESULT):
        companion_thread.start()
        assert loop_started.wait(timeout=3), "Companion loop did not start"

        # Send heartbeat so gateway considers companion online
        _http("POST", f"{gateway_url}/api/v1/companion/heartbeat", token=access_token)

        # ── 5. Submit tool-call (simulate ChatGPT) ────────────────────────────
        tool_call_result: dict[str, Any] = {}

        def _do_tool_call() -> None:
            try:
                data = _http(
                    "POST",
                    f"{gateway_url}/api/v1/tool-call",
                    {"tool_name": "create_memory", "arguments": {"title": "E2E Test Memory"}},
                )
                tool_call_result["data"] = data
            except Exception as exc:
                tool_call_result["error"] = exc

        call_thread = threading.Thread(target=_do_tool_call, daemon=True)
        call_thread.start()
        call_thread.join(timeout=12)

        # ── 6. Stop companion loop ────────────────────────────────────────────
        event_loop.stop()
        companion_thread.join(timeout=5)

    # ── 7. Assert ─────────────────────────────────────────────────────────────
    assert not loop_errors, f"Companion loop raised: {loop_errors[0]}"
    assert "error" not in tool_call_result, f"Tool call failed: {tool_call_result.get('error')}"
    assert "data" in tool_call_result, "Tool call thread did not return a response"
    assert tool_call_result["data"]["result"] == FAKE_RESULT
