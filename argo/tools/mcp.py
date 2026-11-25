"""MCP client: expose tools from Model Context Protocol servers as Argo tools.

Uses the official Python SDK (v2). Each server is launched as a stdio subprocess, initialized in the
same task that will use it (the SDK's transports are task-affine), and its tools are namespaced as
``mcp__<server>__<tool>`` — the same convention Claude Code uses, so model-facing names stay unique and
traceable. A failing server is isolated: it is recorded in ``errors`` and the remaining tools still load.
"""
from __future__ import annotations

import json
import os
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from ..core.types import ToolResult

_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")


@dataclass
class MCPToolInfo:
    server: str
    name: str
    description: str
    input_schema: dict[str, Any]

    @property
    def exposed_name(self) -> str:
        return f"mcp__{_NAME_RE.sub('_', self.server)}__{_NAME_RE.sub('_', self.name)}"


@dataclass
class MCPClient:
    servers: list[Any]
    sessions: dict[str, Any] = field(default_factory=dict)
    tools: list[MCPToolInfo] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    _stack: AsyncExitStack | None = None

    async def connect(self) -> "MCPClient":
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        self._stack = AsyncExitStack()
        for srv in self.servers:
            try:
                env = {**os.environ, **(getattr(srv, "env", None) or {})}
                params = StdioServerParameters(command=srv.command, args=list(getattr(srv, "args", []) or []),
                                               env=env, cwd=getattr(srv, "cwd", None))
                read, write = await self._stack.enter_async_context(stdio_client(params))
                session = await self._stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                listed = await session.list_tools()
                self.sessions[srv.name] = session
                for tool in getattr(listed, "tools", []) or []:
                    schema = (getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None)
                              or {"type": "object", "properties": {}})
                    self.tools.append(MCPToolInfo(server=srv.name, name=tool.name,
                                                  description=(tool.description or "")[:1024],
                                                  input_schema=dict(schema)))
            except Exception as exc:  # one bad server must not take down the harness
                self.errors[srv.name] = f"{type(exc).__name__}: {exc}"
        return self

    async def call(self, server: str, tool: str, arguments: dict[str, Any]) -> ToolResult:
        session = self.sessions.get(server)
        if session is None:
            detail = self.errors.get(server, "server not connected")
            return ToolResult(f"MCP server '{server}' unavailable: {detail}", is_error=True)
        try:
            result = await session.call_tool(tool, arguments=arguments or {})
        except Exception as exc:
            return ToolResult(f"MCP tool '{server}/{tool}' failed: {type(exc).__name__}: {exc}", is_error=True)
        parts: list[str] = []
        for item in getattr(result, "content", []) or []:
            text = getattr(item, "text", None)
            if text is not None:
                parts.append(text)
            elif hasattr(item, "model_dump"):
                parts.append(json.dumps(item.model_dump(), default=str))
            else:
                parts.append(str(item))
        is_error = bool(getattr(result, "isError", False))
        body = "\n".join(parts) or "(empty result)"
        return ToolResult(body, is_error=is_error, metadata={"server": server, "tool": tool})

    async def close(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:
                pass
            self._stack = None
        self.sessions.clear()
