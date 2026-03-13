from __future__ import annotations

import hashlib
import hmac
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, AsyncIterator

import jwt
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from gateway.allowlist import AllowlistConfig, allowlist_middleware_dispatch
from gateway.config import Settings, get_settings
from gateway.db import get_connection, run_migrations
from gateway.direct_call import DirectCallError, MemoryHubDirectClient
from gateway.state_machine import router as state_machine_router

import os as _os
MIGRATIONS_DIR = Path(_os.getenv("GATEWAY_MIGRATIONS_DIR", str(Path(__file__).resolve().parents[2] / "migrations")))


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _isoformat(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _verify_token(token: str, hashed_token: str) -> bool:
    return hmac.compare_digest(_hash_token(token), hashed_token)


def _issue_access_token(device_id: str, secret: str, ttl_seconds: int) -> tuple[str, datetime]:
    expires_at = _utc_now() + timedelta(seconds=ttl_seconds)
    token = jwt.encode(
        {"device_id": device_id, "exp": expires_at},
        secret,
        algorithm="HS256",
    )
    return token, expires_at


def _verify_access_token(request: Request) -> str:
    """Verify Bearer JWT and return device_id. Raises 401 on failure."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="bearer_token_required")
    token = auth.removeprefix("Bearer ")
    secret: str = request.app.state.settings.jwt_secret
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="access_token_expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="invalid_access_token")
    return payload["device_id"]


def _expire_stale_approval_requests(db: Any) -> None:
    """Transition pending approval requests past their deadline to expired."""
    db.execute(
        "UPDATE approval_requests SET state = 'expired' WHERE state = 'pending' AND expires_at <= ?",
        (_isoformat(_utc_now()),),
    )
    db.commit()


def _build_absolute_url(request: Request, path: str) -> str:
    return f"{str(request.base_url).rstrip('/')}{path}"


def _get_pairing_request(request: Request, request_id: str) -> dict[str, Any]:
    row = request.app.state.db.execute(
        "SELECT request_id, device_id, status, created_at, expires_at FROM pairing_requests WHERE request_id = ?",
        (request_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="pairing_request_not_found")

    payload = dict(row)
    if payload["status"] == "pending" and _parse_timestamp(payload["expires_at"]) <= _utc_now():
        request.app.state.db.execute(
            "UPDATE pairing_requests SET status = 'expired' WHERE request_id = ?",
            (request_id,),
        )
        request.app.state.db.commit()
        payload["status"] = "expired"
    return payload


def _render_pairing_page(payload: dict[str, Any]) -> str:
    request_id = payload["request_id"]
    device_id = payload["device_id"]
    status = payload["status"]
    return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Device Pairing</title>
    <style>
      body {{ font-family: system-ui, sans-serif; background: #f4f4f5; color: #18181b; margin: 0; }}
      main {{ max-width: 36rem; margin: 3rem auto; padding: 0 1rem; }}
      .card {{ background: white; border-radius: 12px; padding: 1.5rem; box-shadow: 0 8px 30px rgba(0,0,0,0.08); }}
      code {{ background: #f4f4f5; padding: 0.1rem 0.35rem; border-radius: 6px; }}
      .actions {{ display: flex; gap: 0.75rem; margin-top: 1.5rem; }}
      button {{ flex: 1; border: 0; border-radius: 8px; padding: 0.85rem 1rem; font-weight: 600; cursor: pointer; }}
      .approve {{ background: #16a34a; color: white; }}
      .deny {{ background: #dc2626; color: white; }}
      .muted {{ color: #52525b; }}
    </style>
  </head>
  <body>
    <main>
      <div class="card">
        <h1>Approve device pairing</h1>
        <p class="muted">Request <code>{request_id}</code></p>
        <p>Device <code>{device_id}</code> is waiting to pair with this gateway.</p>
        <p>Current status: <strong>{status}</strong></p>
        <form method="post" action="/approve/device/{request_id}/action">
          <div class="actions">
            <button class="approve" type="submit" name="action" value="approve">Approve</button>
            <button class="deny" type="submit" name="action" value="deny">Deny</button>
          </div>
        </form>
      </div>
    </main>
  </body>
</html>"""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        conn = get_connection(settings.db_path)
        run_migrations(conn, MIGRATIONS_DIR)
        app.state.db = conn
        app.state.settings = settings
        app.state.allowlist = AllowlistConfig(settings.allowlist_path)
        app.state.pending_pairing_tokens = {}
        yield
        conn.close()

    app = FastAPI(title="Memory Hub Gateway", version="0.1.0", lifespan=lifespan)
    app.middleware("http")(allowlist_middleware_dispatch)
    app.include_router(state_machine_router)

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/health/companion")
    def health_companion(request: Request) -> dict[str, object]:
        db = request.app.state.db
        s: Settings = request.app.state.settings
        row = db.execute(
            "SELECT last_heartbeat_at FROM companion_heartbeats ORDER BY last_heartbeat_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return {"companion_online": False, "last_seen": None}
        last_seen = row["last_heartbeat_at"]
        last_dt = datetime.fromisoformat(last_seen).astimezone(UTC)
        age = (datetime.now(UTC) - last_dt).total_seconds()
        online = age < s.companion_heartbeat_timeout_seconds
        return {"companion_online": online, "last_seen": last_seen}

    @app.post("/api/v1/direct-call")
    def direct_call(request: Request, body: dict[str, Any]) -> dict[str, object]:
        tool_name = body.get("tool_name")
        if not tool_name:
            raise HTTPException(status_code=400, detail="tool_name_required")

        tier = request.app.state.allowlist.get_tier(tool_name)
        if tier != 1:
            raise HTTPException(status_code=403, detail={"error": "tool_not_exposed", "tool": tool_name})

        try:
            return MemoryHubDirectClient(request.app.state.settings).call_tool(
                tool_name,
                body.get("arguments", {}),
            )
        except DirectCallError as exc:
            raise HTTPException(
                status_code=502,
                detail={"error": "memory_hub_call_failed", "detail": str(exc)},
            ) from exc

    @app.post("/api/v1/companion/pair/start")
    def start_pairing(request: Request, body: dict[str, str]) -> dict[str, object]:
        device_id = body.get("device_id")
        if not device_id:
            raise HTTPException(status_code=400, detail="device_id_required")

        request_id = str(uuid.uuid4())
        now = _utc_now()
        expires_at = now + timedelta(seconds=request.app.state.settings.pairing_timeout_seconds)
        request.app.state.db.execute(
            """
            INSERT INTO pairing_requests (request_id, device_id, status, created_at, expires_at)
            VALUES (?, ?, 'pending', ?, ?)
            """,
            (request_id, device_id, _isoformat(now), _isoformat(expires_at)),
        )
        request.app.state.db.commit()
        return {
            "request_id": request_id,
            "verification_url": _build_absolute_url(request, f"/approve/device/{request_id}"),
            "poll_url": _build_absolute_url(request, f"/api/v1/companion/pair/poll/{request_id}"),
            "interval_seconds": 2,
        }

    @app.get("/approve/device/{request_id}", response_class=HTMLResponse)
    def pairing_verification_page(request: Request, request_id: str) -> HTMLResponse:
        payload = _get_pairing_request(request, request_id)
        if payload["status"] == "expired":
            return HTMLResponse("<h1>Pairing request expired.</h1>", status_code=410)
        return HTMLResponse(_render_pairing_page(payload))

    @app.post("/approve/device/{request_id}/action", response_class=HTMLResponse)
    def pairing_verification_action(
        request: Request,
        request_id: str,
        action: Annotated[str, Form()],
    ) -> HTMLResponse:
        payload = _get_pairing_request(request, request_id)
        if payload["status"] != "pending":
            status_code = 410 if payload["status"] == "expired" else 409
            return HTMLResponse(f"<h1>Pairing request {payload['status']}.</h1>", status_code=status_code)
        if action not in {"approve", "deny"}:
            raise HTTPException(status_code=400, detail="invalid_pairing_action")

        if action == "deny":
            request.app.state.db.execute(
                "UPDATE pairing_requests SET status = 'denied' WHERE request_id = ?",
                (request_id,),
            )
            request.app.state.db.commit()
            return HTMLResponse("<h1>Device pairing denied.</h1>")

        refresh_token = str(uuid.uuid4())
        hashed_refresh_token = _hash_token(refresh_token)
        now = _utc_now()
        refresh_token_expires_at = now + timedelta(days=request.app.state.settings.refresh_token_ttl_days)
        request.app.state.db.execute(
            """
            INSERT INTO devices (
                device_id,
                hashed_refresh_token,
                registered_at,
                last_seen,
                refresh_token_expires_at,
                revoked
            ) VALUES (?, ?, ?, ?, ?, 0)
            ON CONFLICT(device_id) DO UPDATE SET
                hashed_refresh_token = excluded.hashed_refresh_token,
                registered_at = excluded.registered_at,
                last_seen = excluded.last_seen,
                refresh_token_expires_at = excluded.refresh_token_expires_at,
                revoked = 0
            """,
            (
                payload["device_id"],
                hashed_refresh_token,
                _isoformat(now),
                _isoformat(now),
                _isoformat(refresh_token_expires_at),
            ),
        )
        request.app.state.db.execute(
            "UPDATE pairing_requests SET status = 'approved' WHERE request_id = ?",
            (request_id,),
        )
        request.app.state.db.commit()
        request.app.state.pending_pairing_tokens[request_id] = refresh_token
        return HTMLResponse("<h1>Device pairing approved.</h1>")

    @app.get("/api/v1/companion/pair/poll/{request_id}")
    def poll_pairing(request: Request, request_id: str) -> dict[str, object]:
        payload = _get_pairing_request(request, request_id)
        status = payload["status"]
        if status != "approved":
            return {"status": status}

        device = request.app.state.db.execute(
            """
            SELECT registered_at, last_seen, refresh_token_expires_at
            FROM devices
            WHERE device_id = ?
            """,
            (payload["device_id"],),
        ).fetchone()
        if device is None:
            raise HTTPException(status_code=409, detail="approved_device_missing")

        refresh_token = request.app.state.pending_pairing_tokens.pop(request_id, None)
        if refresh_token is None:
            raise HTTPException(status_code=409, detail="refresh_token_already_delivered")

        return {
            "status": "approved",
            "refresh_token": refresh_token,
            "registered_at": device["registered_at"],
            "last_seen": device["last_seen"],
            "refresh_token_expires_at": device["refresh_token_expires_at"],
        }

    @app.post("/api/v1/companion/devices/{device_id}/revoke")
    def revoke_device(request: Request, device_id: str, body: dict[str, str]) -> dict[str, bool]:
        refresh_token = body.get("refresh_token")
        if not refresh_token:
            raise HTTPException(status_code=400, detail="refresh_token_required")

        row = request.app.state.db.execute(
            "SELECT hashed_refresh_token, revoked FROM devices WHERE device_id = ?",
            (device_id,),
        ).fetchone()
        if row is None or not _verify_token(refresh_token, row["hashed_refresh_token"]):
            raise HTTPException(status_code=401, detail="invalid_refresh_token")
        if row["revoked"]:
            raise HTTPException(status_code=401, detail="device_revoked")

        request.app.state.db.execute(
            "UPDATE devices SET revoked = 1 WHERE device_id = ?",
            (device_id,),
        )
        request.app.state.db.commit()
        return {"revoked": True}

    # ── Token refresh ──────────────────────────────────────────────────────────

    @app.post("/api/v1/companion/token/refresh")
    def token_refresh(request: Request, body: dict[str, str]) -> dict[str, object]:
        device_id = body.get("device_id")
        refresh_token = body.get("refresh_token")
        if not device_id or not refresh_token:
            raise HTTPException(status_code=400, detail="device_id_and_refresh_token_required")

        s: Settings = request.app.state.settings
        db = request.app.state.db
        row = db.execute(
            "SELECT hashed_refresh_token, refresh_token_expires_at, revoked FROM devices WHERE device_id = ?",
            (device_id,),
        ).fetchone()

        if row is None:
            raise HTTPException(status_code=401, detail="invalid_refresh_token")
        if row["revoked"]:
            raise HTTPException(status_code=401, detail={"error": "device_revoked"})
        if not _verify_token(refresh_token, row["hashed_refresh_token"]):
            raise HTTPException(status_code=401, detail={"error": "invalid_refresh_token"})
        if row["refresh_token_expires_at"] and _parse_timestamp(row["refresh_token_expires_at"]) <= _utc_now():
            raise HTTPException(status_code=401, detail={"error": "expired_refresh_token"})

        # Rotate refresh token
        new_refresh_token = str(uuid.uuid4())
        new_hashed = _hash_token(new_refresh_token)
        now = _utc_now()
        new_refresh_expires_at = now + timedelta(days=s.refresh_token_ttl_days)
        db.execute(
            "UPDATE devices SET hashed_refresh_token = ?, last_seen = ?, refresh_token_expires_at = ? WHERE device_id = ?",
            (new_hashed, _isoformat(now), _isoformat(new_refresh_expires_at), device_id),
        )
        db.commit()

        access_token, access_expires_at = _issue_access_token(device_id, s.jwt_secret, s.access_token_ttl_seconds)
        return {
            "access_token": access_token,
            "expires_at": _isoformat(access_expires_at),
            "refresh_token": new_refresh_token,
            "refresh_token_expires_at": _isoformat(new_refresh_expires_at),
            "last_seen": _isoformat(now),
        }

    # ── Companion helpers (auth-gated) ────────────────────────────────────────

    @app.post("/api/v1/companion/heartbeat")
    def companion_heartbeat(request: Request) -> dict[str, object]:
        device_id = _verify_access_token(request)
        db = request.app.state.db
        now = _utc_now()

        device = db.execute(
            "SELECT revoked FROM devices WHERE device_id = ?",
            (device_id,),
        ).fetchone()
        if device is None:
            raise HTTPException(status_code=401, detail="unknown_device")
        if device["revoked"]:
            raise HTTPException(status_code=401, detail="device_revoked")

        db.execute(
            """
            INSERT INTO companion_heartbeats (device_id, last_heartbeat_at)
            VALUES (?, ?)
            ON CONFLICT(device_id) DO UPDATE SET last_heartbeat_at = excluded.last_heartbeat_at
            """,
            (device_id, _isoformat(now)),
        )
        db.execute(
            "UPDATE devices SET last_seen = ? WHERE device_id = ?",
            (_isoformat(now), device_id),
        )
        _expire_stale_approval_requests(db)
        rows = db.execute(
            """
            SELECT request_id, tool_name, arguments_hash, state, created_at, expires_at
            FROM approval_requests
            WHERE state = 'pending' AND expires_at > ?
            ORDER BY created_at ASC
            """,
            (_isoformat(now),),
        ).fetchall()
        db.commit()
        return {
            "ok": True,
            "device_id": device_id,
            "last_seen": _isoformat(now),
            "pending": [dict(r) for r in rows],
        }


    return app


# Module-level app instance for uvicorn / Railway
app = create_app()