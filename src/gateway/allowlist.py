import json
import logging
from pathlib import Path
from typing import Any

import yaml
from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

class AllowlistConfig:
    def __init__(self, path: Path):
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        self.tiers = data.get("tiers", {})
        self.auto_approved = set(self.tiers.get("auto_approved", []))
        self.approval_gated = set(self.tiers.get("approval_gated", []))

    def get_tier(self, tool_name: str) -> int | None:
        if tool_name in self.auto_approved:
            return 1
        if tool_name in self.approval_gated:
            return 2
        return None

async def allowlist_middleware_dispatch(request: Request, call_next: Any) -> Any:
    if request.url.path == "/api/v1/tool-call" and request.method == "POST":
        try:
            body = await request.body()
            async def receive() -> dict[str, Any]:
                return {"type": "http.request", "body": body}
            request._receive = receive
            
            if not body:
                return await call_next(request)
                
            data = json.loads(body)
            tool_name = data.get("tool_name")
            if not tool_name:
                return await call_next(request)
                
            allowlist: AllowlistConfig = request.app.state.allowlist
            tier = allowlist.get_tier(tool_name)
            
            if tier is None:
                logger.warning(f"Tool rejected by allowlist: {tool_name}")
                return JSONResponse(
                    status_code=403,
                    content={"error": "tool_not_exposed", "tool": tool_name}
                )

            if tier == 1:
                # TIER 1 (read-only) tools are auto-approved; they go through
                # /api/v1/direct-call, not the approval flow at /api/v1/tool-call.
                return JSONResponse(
                    status_code=404,
                    content={"error": "use_direct_call_for_read_only_tools", "tool": tool_name}
                )

            request.state.tier = tier
        except Exception as e:
            logger.error(f"Allowlist middleware error: {e}")
            
    return await call_next(request)
