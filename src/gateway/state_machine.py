import asyncio
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Optional

import jwt
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


def _verify_access_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="bearer_token_required")
    token = auth.removeprefix("Bearer ")
    secret: str = request.app.state.settings.jwt_secret
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=401, detail="access_token_expired") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="invalid_access_token") from exc
    return payload["device_id"]


def _companion_online(request: Request) -> bool:
    row = request.app.state.db.execute(
        "SELECT last_heartbeat_at FROM companion_heartbeats ORDER BY last_heartbeat_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return False
    last_seen = _parse_timestamp(row["last_heartbeat_at"])
    timeout_seconds = request.app.state.settings.companion_heartbeat_timeout_seconds
    return (_utc_now() - last_seen).total_seconds() < timeout_seconds

async def _queue_tool_and_poll(
    request: Request,
    tool_name: str,
    arguments: dict[str, Any],
    tier: int,
) -> dict[str, Any]:
    """Queue a tool call and long-poll until the companion executes it."""
    db = request.app.state.db
    settings = request.app.state.settings

    if not _companion_online(request):
        raise HTTPException(
            status_code=503,
            detail={"error": "companion_unavailable", "retry_after": settings.companion_heartbeat_timeout_seconds},
        )

    args_str = json.dumps(arguments, sort_keys=True)
    arguments_hash = hashlib.sha256(args_str.encode()).hexdigest()
    request_id = str(uuid.uuid4())
    idempotency_key = str(uuid.uuid4())
    now = _utc_now()
    expires_at = now + timedelta(seconds=settings.approval_timeout_seconds)
    result_expires_at_dt = now + timedelta(seconds=settings.result_ttl_seconds)

    def _create() -> None:
        db.execute(
            """
            INSERT INTO approval_requests
                (request_id, idempotency_key, tool_name, arguments_hash, arguments, tier, state, created_at, expires_at, result_expires_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (request_id, idempotency_key, tool_name, arguments_hash, args_str, tier,
             _isoformat(now), _isoformat(expires_at), _isoformat(result_expires_at_dt)),
        )
        db.commit()

    await run_in_threadpool(_create)

    timeout_dt = expires_at
    while True:
        await asyncio.sleep(0.5)
        current_now = _utc_now()

        def _check() -> Any:
            return db.execute(
                "SELECT state, result, error FROM approval_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()

        row = await run_in_threadpool(_check)
        if row is None:
            raise HTTPException(status_code=500, detail="request_deleted")

        state = row["state"]
        if state == "executed":
            return {"result": json.loads(row["result"]) if row["result"] else None}
        if state == "denied":
            raise HTTPException(status_code=403, detail={"error": "approval_denied"})
        if state == "failed":
            raise HTTPException(status_code=500, detail={"error": json.loads(row["error"]) if row["error"] else "unknown_error"})
        if state == "expired":
            raise HTTPException(status_code=408, detail={"error": "approval_timeout"})

        if current_now >= timeout_dt:
            def _expire() -> None:
                db.execute(
                    "UPDATE approval_requests SET state = 'expired' WHERE request_id = ? AND state IN ('pending', 'approved')",
                    (request_id,),
                )
                db.commit()
            await run_in_threadpool(_expire)
            raise HTTPException(status_code=408, detail={"error": "approval_timeout"})


# ── Dedicated tool endpoints (explicit schemas for ChatGPT) ───────────────────

class SearchMemoriesBody(BaseModel):
    query: str
    limit: int = 10

class GetMemoryBody(BaseModel):
    memory_id: str

class ListRecentMemoriesBody(BaseModel):
    limit: int = 20
    project: Optional[str] = None

class GetProjectContextBody(BaseModel):
    project: str

class ListWorkItemsBody(BaseModel):
    status: Optional[str] = None

class CreateMemoryBody(BaseModel):
    title: str
    content: str
    type: str
    project: Optional[str] = None
    summary: Optional[str] = None
    agent_name: str = "chatgpt"

class UpdateMemoryBody(BaseModel):
    memory_id: str
    content: str

class ArchiveMemoryBody(BaseModel):
    memory_id: str

class CreateWorkItemBody(BaseModel):
    title: str
    description: Optional[str] = None
    project: Optional[str] = None


@router.post("/api/v1/tools/search-memories")
async def tool_search_memories(body: SearchMemoriesBody, request: Request) -> dict[str, Any]:
    return await _queue_tool_and_poll(request, "search_memories", {"query": body.query, "limit": body.limit}, tier=1)

@router.post("/api/v1/tools/get-memory")
async def tool_get_memory(body: GetMemoryBody, request: Request) -> dict[str, Any]:
    return await _queue_tool_and_poll(request, "get_memory", {"memory_id": body.memory_id}, tier=1)

@router.post("/api/v1/tools/list-recent-memories")
async def tool_list_recent_memories(body: ListRecentMemoriesBody, request: Request) -> dict[str, Any]:
    args: dict[str, Any] = {"limit": body.limit}
    if body.project:
        args["project"] = body.project
    return await _queue_tool_and_poll(request, "list_recent_memories", args, tier=1)

@router.post("/api/v1/tools/get-project-context")
async def tool_get_project_context(body: GetProjectContextBody, request: Request) -> dict[str, Any]:
    return await _queue_tool_and_poll(request, "get_project_context", {"project": body.project}, tier=1)

@router.post("/api/v1/tools/list-work-items")
async def tool_list_work_items(body: ListWorkItemsBody, request: Request) -> dict[str, Any]:
    args: dict[str, Any] = {}
    if body.status:
        args["status"] = body.status
    return await _queue_tool_and_poll(request, "list_work_items", args, tier=1)

@router.post("/api/v1/tools/create-memory")
async def tool_create_memory(body: CreateMemoryBody, request: Request) -> dict[str, Any]:
    args: dict[str, Any] = {"title": body.title, "content": body.content, "type": body.type, "agent_name": body.agent_name}
    if body.project:
        args["project"] = body.project
    if body.summary:
        args["summary"] = body.summary
    return await _queue_tool_and_poll(request, "create_memory", args, tier=2)

@router.post("/api/v1/tools/update-memory")
async def tool_update_memory(body: UpdateMemoryBody, request: Request) -> dict[str, Any]:
    return await _queue_tool_and_poll(request, "update_memory", {"memory_id": body.memory_id, "content": body.content}, tier=2)

@router.post("/api/v1/tools/archive-memory")
async def tool_archive_memory(body: ArchiveMemoryBody, request: Request) -> dict[str, Any]:
    return await _queue_tool_and_poll(request, "archive_memory", {"memory_id": body.memory_id}, tier=2)

@router.post("/api/v1/tools/create-work-item")
async def tool_create_work_item(body: CreateWorkItemBody, request: Request) -> dict[str, Any]:
    args: dict[str, Any] = {"title": body.title}
    if body.description:
        args["description"] = body.description
    if body.project:
        args["project"] = body.project
    return await _queue_tool_and_poll(request, "create_work_item", args, tier=2)


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
    if not _companion_online(request):
        retry_after = request.app.state.settings.companion_heartbeat_timeout_seconds
        return JSONResponse(
            status_code=503,
            content={"error": "local_companion_unavailable", "retry_after": retry_after},
        )
    
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
                INSERT INTO approval_requests (request_id, idempotency_key, tool_name, arguments_hash, arguments, tier, state, created_at, expires_at, result_expires_at)
                VALUES (?, ?, ?, ?, ?, 2, 'pending', ?, ?, ?)
                """,
                (request_id, idempotency_key, tool_name, arguments_hash, args_str, _isoformat(now), _isoformat(expires_at), _isoformat(result_expires_at))
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

@router.get("/api/v1/companion/pending-requests")
async def get_pending_requests(request: Request):
    _verify_access_token(request)
    db = request.app.state.db

    def fetch():
        now = _isoformat(_utc_now())
        rows = db.execute(
            """
            SELECT request_id, tool_name, arguments_hash, arguments, tier, state, created_at, expires_at
            FROM approval_requests
            WHERE state = 'pending' AND expires_at > ?
            ORDER BY created_at ASC
            """,
            (now,),
        ).fetchall()
        return [dict(r) for r in rows]

    pending = await run_in_threadpool(fetch)
    return {"pending": pending}


@router.get("/api/v1/tool-call/{request_id}")
async def get_tool_call_status(request_id: str, request: Request):
    db = request.app.state.db

    def fetch():
        return db.execute(
            "SELECT request_id, tool_name, arguments, tier, state, result, error, created_at, expires_at FROM approval_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()

    row = await run_in_threadpool(fetch)
    if row is None:
        raise HTTPException(status_code=404, detail="request_not_found")
    return dict(row)


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
    _verify_access_token(request)
    db = request.app.state.db
    
    if body.state not in ("executed", "failed"):
        return JSONResponse(status_code=400, content={"error": "invalid_state_transition"})
        
    def update_state():
        row = db.execute("SELECT state FROM approval_requests WHERE request_id = ?", (request_id,)).fetchone()
        if not row:
            return "not_found"
            
        current_state = row["state"]
        if current_state not in ("approved", "pending"):
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
