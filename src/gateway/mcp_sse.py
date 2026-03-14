"""MCP-over-SSE transport for Claude web (Remote MCP).

Protocol:  MCP 2024-11-05, SSE transport
Auth:      OAuth 2.0 Authorization Code + PKCE  (required by Claude.ai)
           OR static Bearer token via GATEWAY_MCP_TOKEN env var

Flow:
  1. Claude.ai fetches /.well-known/oauth-authorization-server
  2. Claude.ai redirects user to /oauth/authorize
  3. User clicks Approve in browser
  4. Claude.ai exchanges code at POST /oauth/token  → gets access_token (JWT)
  5. Claude.ai connects  GET /mcp/sse   with  Authorization: Bearer <token>
  6. Claude.ai POSTs JSON-RPC to  POST /mcp/messages?sessionId=<id>
  7. Gateway routes tool calls through _queue_tool_and_poll → companion → memory-hub
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, AsyncIterator

import jwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from gateway.state_machine import _queue_tool_and_poll

router = APIRouter()

# ── Static-token fallback (set GATEWAY_MCP_TOKEN to skip OAuth) ──────────────
_STATIC_TOKEN = os.getenv("GATEWAY_MCP_TOKEN", "")

# ── In-memory SSE sessions: session_id → asyncio.Queue ───────────────────────
_sessions: dict[str, asyncio.Queue] = {}

# ── MCP tool definitions ──────────────────────────────────────────────────────
_TOOLS = [
    {
        "name": "search_memories",
        "description": "Full-text search across all memories in Memory Hub.",
        "inputSchema": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "Full-text search query"},
                "limit": {"type": "integer", "default": 10},
            },
        },
    },
    {
        "name": "get_memory",
        "description": "Get the full content of a specific memory by its ID.",
        "inputSchema": {
            "type": "object",
            "required": ["memory_id"],
            "properties": {
                "memory_id": {"type": "string", "description": "Memory ID, e.g. mem_abc123"},
            },
        },
    },
    {
        "name": "list_recent_memories",
        "description": "List recently updated memories, optionally filtered by project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 20},
                "project": {"type": "string", "description": "Optional project name filter"},
            },
        },
    },
    {
        "name": "get_project_context",
        "description": "Get aggregated context and memories for a named project.",
        "inputSchema": {
            "type": "object",
            "required": ["project"],
            "properties": {
                "project": {"type": "string", "description": "Project name, e.g. gateway"},
            },
        },
    },
    {
        "name": "list_work_items",
        "description": "List work items, optionally filtered by status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["open", "in_progress", "done"],
                    "description": "Filter by status; omit for all",
                },
            },
        },
    },
    {
        "name": "create_memory",
        "description": "Create a new memory (requires approval on your Mac).",
        "inputSchema": {
            "type": "object",
            "required": ["title", "content", "type"],
            "properties": {
                "title": {"type": "string"},
                "content": {"type": "string", "description": "Full memory content in markdown"},
                "type": {
                    "type": "string",
                    "enum": ["decision", "project_context", "reference", "snippet"],
                },
                "summary": {"type": "string"},
                "project": {"type": "string"},
            },
        },
    },
    {
        "name": "update_memory",
        "description": "Update the content of an existing memory (requires approval).",
        "inputSchema": {
            "type": "object",
            "required": ["memory_id", "content"],
            "properties": {
                "memory_id": {"type": "string"},
                "content": {"type": "string"},
            },
        },
    },
    {
        "name": "archive_memory",
        "description": "Archive a memory (requires approval).",
        "inputSchema": {
            "type": "object",
            "required": ["memory_id"],
            "properties": {
                "memory_id": {"type": "string"},
            },
        },
    },
    {
        "name": "create_work_item",
        "description": "Create a new work item (requires approval on your Mac).",
        "inputSchema": {
            "type": "object",
            "required": ["title"],
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "project": {"type": "string"},
            },
        },
    },
]

_TOOL_TIERS: dict[str, int] = {
    "search_memories": 1,
    "get_memory": 1,
    "list_recent_memories": 1,
    "get_project_context": 1,
    "list_work_items": 1,
    "create_memory": 2,
    "update_memory": 2,
    "archive_memory": 2,
    "create_work_item": 2,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)

def _isoformat(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")

def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _verify_bearer(request: Request) -> None:
    """Verify Bearer token — static token OR JWT issued by our OAuth flow."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="bearer_token_required")
    token = auth.removeprefix("Bearer ")

    # Static token shortcut
    if _STATIC_TOKEN and token == _STATIC_TOKEN:
        return

    # JWT issued by our OAuth flow
    secret: str = request.app.state.settings.jwt_secret
    try:
        jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="access_token_expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="invalid_access_token")


def _issue_access_token(settings: Any) -> tuple[str, datetime]:
    expires_at = _utc_now() + timedelta(seconds=settings.access_token_ttl_seconds)
    token = jwt.encode(
        {"sub": "mcp_client", "exp": expires_at},
        settings.jwt_secret,
        algorithm="HS256",
    )
    return token, expires_at


# ── OAuth 2.0 discovery + endpoints ──────────────────────────────────────────

@router.get("/.well-known/oauth-authorization-server")
def oauth_metadata(request: Request) -> dict[str, Any]:
    base = str(request.base_url).rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }


@router.get("/oauth/authorize", response_class=HTMLResponse)
def oauth_authorize(
    request: Request,
    client_id: str = "",
    redirect_uri: str = "",
    state: str = "",
    code_challenge: str = "",
    code_challenge_method: str = "S256",
    response_type: str = "code",
    scope: str = "",
) -> HTMLResponse:
    if response_type != "code":
        return HTMLResponse("<h1>unsupported_response_type</h1>", status_code=400)

    # Render approval page; form POSTs back to /oauth/authorize/action
    params = (
        f"client_id={client_id}&redirect_uri={redirect_uri}"
        f"&state={state}&code_challenge={code_challenge}&scope={scope}"
    )
    return HTMLResponse(_render_oauth_page(client_id, redirect_uri, state, params))


def _render_oauth_page(client_id: str, redirect_uri: str, state: str, params: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Authorize Memory Hub</title>
    <style>
      body {{ font-family: system-ui, sans-serif; background: #f4f4f5; color: #18181b; margin: 0; }}
      main {{ max-width: 36rem; margin: 3rem auto; padding: 0 1rem; }}
      .card {{ background: white; border-radius: 12px; padding: 1.5rem; box-shadow: 0 8px 30px rgba(0,0,0,0.08); }}
      code {{ background: #f4f4f5; padding: 0.1rem 0.35rem; border-radius: 6px; font-size: 0.85em; }}
      .actions {{ display: flex; gap: 0.75rem; margin-top: 1.5rem; }}
      button {{ flex: 1; border: 0; border-radius: 8px; padding: 0.85rem 1rem; font-weight: 600; cursor: pointer; font-size: 1rem; }}
      .approve {{ background: #16a34a; color: white; }}
      .deny {{ background: #dc2626; color: white; }}
      .muted {{ color: #52525b; font-size: 0.9em; }}
    </style>
  </head>
  <body>
    <main>
      <div class="card">
        <h1>Authorize Memory Hub access</h1>
        <p>An external app wants to connect to your Memory Hub.</p>
        <p class="muted">Client: <code>{client_id or "unknown"}</code></p>
        <p class="muted">Redirect: <code>{redirect_uri}</code></p>
        <form method="post" action="/oauth/authorize/action?{params}">
          <div class="actions">
            <button class="approve" type="submit" name="action" value="approve">Approve</button>
            <button class="deny"   type="submit" name="action" value="deny">Deny</button>
          </div>
        </form>
      </div>
    </main>
  </body>
</html>"""


@router.post("/oauth/authorize/action", response_class=HTMLResponse)
async def oauth_authorize_action(
    request: Request,
    client_id: str = "",
    redirect_uri: str = "",
    state: str = "",
    code_challenge: str = "",
    scope: str = "",
) -> HTMLResponse:
    from fastapi import Form
    form = await request.form()
    action = form.get("action", "deny")

    if action != "approve":
        # Redirect with error
        sep = "&" if "?" in redirect_uri else "?"
        return HTMLResponse(
            f'<meta http-equiv="refresh" content="0;url={redirect_uri}{sep}error=access_denied&state={state}">',
        )

    # Issue authorization code
    code = str(uuid.uuid4()).replace("-", "")
    now = _utc_now()
    expires_at = now + timedelta(seconds=300)  # codes expire in 5 min

    db = request.app.state.db

    def _store() -> None:
        db.execute(
            """
            INSERT INTO oauth_codes (code, client_id, redirect_uri, code_challenge, scope, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (code, client_id, redirect_uri, code_challenge or None, scope, _isoformat(now), _isoformat(expires_at)),
        )
        db.commit()

    await run_in_threadpool(_store)

    sep = "&" if "?" in redirect_uri else "?"
    return HTMLResponse(
        f'<meta http-equiv="refresh" content="0;url={redirect_uri}{sep}code={code}&state={state}">',
    )


@router.post("/oauth/token")
async def oauth_token(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        form = await request.form()
        body = dict(form)

    grant_type = body.get("grant_type", "")
    if grant_type != "authorization_code":
        return JSONResponse(status_code=400, content={"error": "unsupported_grant_type"})

    code = body.get("code", "")
    redirect_uri = body.get("redirect_uri", "")
    code_verifier = body.get("code_verifier", "")

    db = request.app.state.db
    settings = request.app.state.settings

    def _fetch_code() -> Any:
        return db.execute(
            "SELECT * FROM oauth_codes WHERE code = ? AND used = 0",
            (code,),
        ).fetchone()

    row = await run_in_threadpool(_fetch_code)

    if row is None:
        return JSONResponse(status_code=400, content={"error": "invalid_grant"})

    row = dict(row)

    if _parse_timestamp(row["expires_at"]) <= _utc_now():
        return JSONResponse(status_code=400, content={"error": "invalid_grant", "detail": "code_expired"})

    if row["redirect_uri"] != redirect_uri:
        return JSONResponse(status_code=400, content={"error": "invalid_grant", "detail": "redirect_uri_mismatch"})

    # Verify PKCE if challenge was stored
    if row["code_challenge"]:
        if not code_verifier:
            return JSONResponse(status_code=400, content={"error": "invalid_grant", "detail": "code_verifier_required"})
        digest = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        ).rstrip(b"=").decode()
        if digest != row["code_challenge"]:
            return JSONResponse(status_code=400, content={"error": "invalid_grant", "detail": "pkce_mismatch"})

    # Mark code as used
    def _use_code() -> None:
        db.execute("UPDATE oauth_codes SET used = 1 WHERE code = ?", (code,))
        db.commit()

    await run_in_threadpool(_use_code)

    access_token, expires_at = _issue_access_token(settings)
    return JSONResponse(content={
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": settings.access_token_ttl_seconds,
    })


# ── MCP SSE transport ─────────────────────────────────────────────────────────

@router.get("/mcp/sse")
async def mcp_sse(request: Request) -> StreamingResponse:
    _verify_bearer(request)

    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    _sessions[session_id] = queue

    base_url = str(request.base_url).rstrip("/")
    messages_url = f"{base_url}/mcp/messages?sessionId={session_id}"

    async def event_stream() -> AsyncIterator[str]:
        try:
            # MCP SSE handshake: send the messages endpoint URL
            yield f"event: endpoint\ndata: {messages_url}\n\n"

            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=30.0)
                    if msg is None:  # sentinel → close stream
                        break
                    yield f"data: {json.dumps(msg)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            _sessions.pop(session_id, None)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/mcp/messages")
async def mcp_messages(request: Request, sessionId: str = "") -> JSONResponse:
    queue = _sessions.get(sessionId)
    if queue is None:
        raise HTTPException(status_code=404, detail="session_not_found")

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid_json"})

    # Dispatch async; respond immediately with 202
    asyncio.create_task(_handle_jsonrpc(body, queue, request))
    return JSONResponse(status_code=202, content={})


async def _handle_jsonrpc(body: dict, queue: asyncio.Queue, request: Request) -> None:
    req_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params") or {}

    try:
        if method == "initialize":
            await queue.put({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "memory-hub-gateway", "version": "2.0.0"},
                },
            })

        elif method in ("notifications/initialized", "notifications/cancelled"):
            pass  # fire-and-forget, no response

        elif method == "ping":
            await queue.put({"jsonrpc": "2.0", "id": req_id, "result": {}})

        elif method == "tools/list":
            await queue.put({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": _TOOLS},
            })

        elif method == "tools/call":
            await _handle_tool_call(req_id, params, queue, request)

        else:
            await queue.put({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            })
    except Exception as exc:
        await queue.put({
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32000, "message": str(exc)},
        })


async def _handle_tool_call(
    req_id: Any,
    params: dict,
    queue: asyncio.Queue,
    request: Request,
) -> None:
    tool_name = params.get("name", "")
    arguments = params.get("arguments") or {}

    tier = _TOOL_TIERS.get(tool_name)
    if tier is None:
        await queue.put({
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
        })
        return

    # Inject agent_name for create_memory so memory-hub doesn't reject it
    if tool_name == "create_memory" and "agent_name" not in arguments:
        arguments = {**arguments, "agent_name": "claude_web"}

    try:
        result = await _queue_tool_and_poll(request, tool_name, arguments, tier=tier)
        result_text = json.dumps(result.get("result", result), ensure_ascii=False, indent=2)
        await queue.put({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": result_text}],
                "isError": False,
            },
        })
    except HTTPException as exc:
        detail = exc.detail
        msg = detail.get("error", str(detail)) if isinstance(detail, dict) else str(detail)
        await queue.put({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": f"Error: {msg}"}],
                "isError": True,
            },
        })
