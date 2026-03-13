from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from gateway.config import PROJECT_ROOT, Settings


class DirectCallError(RuntimeError):
    pass


class MemoryHubDirectClient:
    def __init__(self, settings: Settings, timeout_seconds: int = 30):
        self.settings = settings
        self.timeout_seconds = timeout_seconds

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return anyio.run(self._call_tool_async, tool_name, arguments)

    async def _call_tool_async(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        async with stdio_client(self._server_parameters()) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                response = await session.call_tool(
                    tool_name,
                    arguments=arguments,
                    read_timeout_seconds=timedelta(seconds=self.timeout_seconds),
                )
        return self._normalize_response(response)

    def _server_parameters(self) -> StdioServerParameters:
        command_parts = self._command_parts()
        env = os.environ.copy()
        memory_hub_src = PROJECT_ROOT.parent / "memory-hub" / "src"
        if memory_hub_src.exists():
            existing_pythonpath = env.get("PYTHONPATH")
            env["PYTHONPATH"] = (
                str(memory_hub_src)
                if not existing_pythonpath
                else f"{memory_hub_src}{os.pathsep}{existing_pythonpath}"
            )
        workdir = PROJECT_ROOT.parent / "memory-hub"
        return StdioServerParameters(
            command=command_parts[0],
            args=command_parts[1:],
            env=env,
            cwd=str(workdir if workdir.exists() else PROJECT_ROOT),
        )

    def _command_parts(self) -> list[str]:
        if self.settings.memory_hub_path != "memory-hub":
            return [*shlex.split(self.settings.memory_hub_path), "run-mcp"]

        memory_hub_binary = shutil.which("memory-hub")
        if memory_hub_binary:
            return [memory_hub_binary, "run-mcp"]

        python_binary = shutil.which("python3") or sys.executable
        return [python_binary, "-m", "memory_hub.cli", "run-mcp"]

    @staticmethod
    def _normalize_response(response: Any) -> dict[str, Any]:
        structured = getattr(response, "structuredContent", None)
        if isinstance(structured, dict):
            return structured

        payload = response.model_dump(mode="python", by_alias=True)
        if payload.get("isError"):
            raise DirectCallError(json.dumps(payload.get("content", [])))

        for item in payload.get("content", []):
            if item.get("type") != "text":
                continue
            text = item.get("text", "")
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed

        raise DirectCallError("memory-hub tool response did not include structured JSON content")
