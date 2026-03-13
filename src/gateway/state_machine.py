import asyncio
import hashlib
import json
import uuid
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Header, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

router = APIRouter()
logger = logging.getLogger(__name__)

def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)

def _isoformat(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")

def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)

class ConfirmRequest(BaseModel):
    state: str
    result: Optional[Any] = None
    error: Optional[Any] = None

@router.post("/api/v1/tool-call")
async def handle_tool_call(
    request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid_json"})
        
    tool_name = body.get("tool_name")
    if not tool_name:
        return JSONResponse(status_code=400, content={"error": "tool_name_required"})
    
    args_str = json.dumps(body.get("arguments", {}), sort_keys=True)
    arguments_hash = hashlib.sha256(args_str.encode("utf-8")).hexdigest()
    
    if not idempotency_key:
        idempotency_key = str(uuid.uuid4())
        
    db = request.app.state.db
    settings = request.app.state.settings
    
    def get_request_by_idem(key: str):
        return db.execute("SELECT * FROM approval_requests WHERE idempotency_key = ?", (key,)).fetchone()
        
    row = await run_in_threadpool(get_request_by_idem, idempotency_key)
    
    now = _utc_now()
    if row:
        row_dict = dict(row)
        if row_dict["arguments_hash"] != arguments_hash:
            return JSONResponse(status_code=409, content={"error": "idempotency_key_reused_with_different_payload"})
            
        result_expires_at = _parse_timestamp(row_dict["result_expires_at"])
        if result_expires_at <= now:
            def delete_and_create():
                db.execute("DELETE FROM approval_requests WHERE idempotency_key = ?", (idempotency_key,))
                db.commit()
            await run_in_threadpool(delete_and_create)
            row = None
        else:
            state = row_dict["state"]
            if state == "executed":
                result_data = json.loads(row_dict["result"]) if row_dict["result"] else None
                return JSONResponse(content={"result": result_data, "idempotency_key": idempotency_key})
            elif state == "denied":
                return JSONResponse(status_code=403, content={"error": "approval_denied", "request_id": row_dict["request_id"]})
            elif state == "expired":
                return JSONResponse(status_code=408, content={"error": "approval_timeout", "request_id": row_dict["request_id"]})
            elif state == "failed":
                error_data = json.loads(row_dict["error"]) if row_dict["error"] else "unknown_error"
                return JSONResponse(status_code=500, content={"error": error_data, "request_id": row_dict["request_id"]})

    request_id = str(uuid.uuid4())
    if not row:
        def create_req():
            expires_at = now + timedelta(seconds=settings.approval_timeout_seconds)
            result_expires_at = now + timedelta(seconds=settings.result_ttl_seconds)
            db.execute(
                """
                INSERT INTO approval_requests (request_id, idempotency_key, tool_name, arguments_hash, state, created_at, expires_at, result_expires_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (request_id, idempotency_key, tool_name, arguments_hash, _isoformat(now), _isoformat(expires_at), _isoformat(result_expires_at))
            )
            db.commit()
            return expires_at
        expires_at = await run_in_threadpool(create_req)
    else:
        request_id = row_dict["request_id"]
        expires_at = _parse_timestamp(row_dict["expires_at"])
        
    timeout_dt = min(expires_at, now + timedelta(seconds=settings.approval_timeout_seconds))
    
    while True:
        await asyncio.sleep(0.5)
        now = _utc_now()
        
        def check_status():
            return db.execute("SELECT state, result, error FROM approval_requests WHERE request_id = ?", (request_id,)).fetchone()
            
        current = await run_in_threadpool(check_status)
        if not current:
            return JSONResponse(status_code=500, content={"error": "request_deleted"})
            
        state = current["state"]
        if state == "executed":
            result_data = json.loads(current["result"]) if current["result"] else None
            return JSONResponse(content={"result": result_data, "idempotency_key": idempotency_key})
        elif state == "denied":
            return JSONResponse(status_code=403, content={"error": "approval_denied", "request_id": request_id})
        elif state == "failed":
            error_data = json.loads(current["error"]) if current["error"] else "unknown_error"
            return JSONResponse(status_code=500, content={"error": error_data, "request_id": request_id})
        elif state == "expired":
            return JSONResponse(status_code=408, content={"error": "approval_timeout", "request_id": request_id})
            
        if now >= timeout_dt:
            def mark_expired():
                db.execute("UPDATE approval_requests SET state = 'expired' WHERE request_id = ? AND state IN ('pending', 'approved')", (request_id,))
                db.commit()
                return db.execute("SELECT state FROM approval_requests WHERE request_id = ?", (request_id,)).fetchone()["state"]
            final_state = await run_in_threadpool(mark_expired)
            if final_state == "expired":
                return JSONResponse(status_code=408, content={"error": "approval_timeout", "request_id": request_id})

@router.post("/api/v1/approval-requests/{request_id}/approve")
async def approve_request(request_id: str, request: Request):
    db = request.app.state.db
    def do_approve():
        row = db.execute("SELECT state FROM approval_requests WHERE request_id = ?", (request_id,)).fetchone()
        if not row: return "not_found"
        if row["state"] != "pending": return "conflict"
        db.execute("UPDATE approval_requests SET state = 'approved' WHERE request_id = ?", (request_id,))
        db.commit()
        return "ok"
    res = await run_in_threadpool(do_approve)
    if res == "not_found": raise HTTPException(status_code=404)
    if res == "conflict": return JSONResponse(status_code=409, content={"error": "invalid_state_transition"})
    return {"status": "approved"}

@router.post("/api/v1/approval-requests/{request_id}/deny")
async def deny_request(request_id: str, request: Request):
    db = request.app.state.db
    def do_deny():
        row = db.execute("SELECT state FROM approval_requests WHERE request_id = ?", (request_id,)).fetchone()
        if not row: return "not_found"
        if row["state"] != "pending": return "conflict"
        db.execute("UPDATE approval_requests SET state = 'denied' WHERE request_id = ?", (request_id,))
        db.commit()
        return "ok"
    res = await run_in_threadpool(do_deny)
    if res == "not_found": raise HTTPException(status_code=404)
    if res == "conflict": return JSONResponse(status_code=409, content={"error": "invalid_state_transition"})
    return {"status": "denied"}

@router.post("/api/v1/approval-requests/{request_id}/confirm")
async def confirm_approval_request(request_id: str, body: ConfirmRequest, request: Request):
    db = request.app.state.db
    
    if body.state not in ("executed", "failed"):
        return JSONResponse(status_code=400, content={"error": "invalid_state_transition"})
        
    def update_state():
        row = db.execute("SELECT state FROM approval_requests WHERE request_id = ?", (request_id,)).fetchone()
        if not row:
            return "not_found"
            
        current_state = row["state"]
        if current_state != "approved":
            return "conflict"
            
        res_str = json.dumps(body.result) if body.result is not None else None
        err_str = json.dumps(body.error) if body.error is not None else None
        
        db.execute(
            "UPDATE approval_requests SET state = ?, result = ?, error = ? WHERE request_id = ?",
            (body.state, res_str, err_str, request_id)
        )
        db.commit()
        return "ok"
        
    res = await run_in_threadpool(update_state)
    if res == "not_found":
        raise HTTPException(status_code=404, detail="request_not_found")
    if res == "conflict":
        return JSONResponse(status_code=409, content={"error": "invalid_state_transition"})
        
    return {"status": "ok"}
