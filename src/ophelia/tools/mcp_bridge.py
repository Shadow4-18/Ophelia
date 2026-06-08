"""MCP tool bridge — optional stdio MCP servers from ~/.ophelia/mcp.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Awaitable, Callable

import structlog

from ophelia.config import OPHELIA_HOME

log = structlog.get_logger()

McpHandler = Callable[..., Awaitable[str]]


def load_mcp_config(path: Path | None = None) -> dict:
    p = path or OPHELIA_HOME / "mcp.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


class MCPBridge:
    """Lazy MCP client; tools prefixed with mcp_."""

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or load_mcp_config()
        self._handlers: dict[str, McpHandler] = {}
        self._definitions: list[dict[str, Any]] = []
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        servers = self.config.get("servers") or {}
        if not servers:
            return
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError:
            log.warning("mcp.missing", hint="pip install mcp for MCP server support")
            return

        for server_id, spec in servers.items():
            if not spec.get("enabled", True):
                continue
            command = spec.get("command")
            if not command:
                continue
            args = spec.get("args") or []
            env = spec.get("env")
            try:
                params = StdioServerParameters(
                    command=command,
                    args=args,
                    env=env,
                )
                # Note: full persistent MCP sessions per server would need lifecycle mgmt.
                # For v1 we register definitions from config static list if provided.
                for tool in spec.get("tools") or []:
                    tname = tool.get("name")
                    if not tname:
                        continue
                    full = f"mcp_{server_id}_{tname}"

                    def make_handler(sid: str, tn: str) -> McpHandler:
                        async def _call(**kwargs: Any) -> str:
                            return await self._call_tool_stdio(
                                sid, tn, kwargs, params
                            )

                        return _call

                    self._handlers[full] = make_handler(server_id, tname)
                    self._definitions.append(
                        {
                            "type": "function",
                            "function": {
                                "name": full,
                                "description": tool.get(
                                    "description", f"MCP {server_id}/{tname}"
                                ),
                                "parameters": tool.get(
                                    "parameters",
                                    {"type": "object", "properties": {}},
                                ),
                            },
                        }
                    )
            except Exception as e:
                log.warning("mcp.server_config", server=server_id, error=str(e))

    async def _call_tool_stdio(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict,
        params: Any,
    ) -> str:
        try:
            from mcp import ClientSession
            from mcp.client.stdio import stdio_client
        except ImportError:
            return "MCP not installed. pip install mcp"

        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments=arguments)
                    parts = []
                    for block in result.content:
                        text = getattr(block, "text", None)
                        if text:
                            parts.append(text)
                    return "\n".join(parts) or str(result)
        except Exception as e:
            return f"MCP {server_id}/{tool_name} failed: {e}"

    def definitions(self) -> list[dict[str, Any]]:
        return list(self._definitions)

    async def dispatch(self, name: str, args: dict) -> str | None:
        handler = self._handlers.get(name)
        if not handler:
            return None
        return await handler(**args)
