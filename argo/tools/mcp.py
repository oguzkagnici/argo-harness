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
class MCPResourceInfo:
    server: str
    uri: str
    name: str = ""
    mime_type: str = ""
    description: str = ""


@dataclass
class MCPPromptInfo:
    server: str
    name: str
    description: str = ""
    arguments: list[Any] = field(default_factory=list)


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
    resources: list[MCPResourceInfo] = field(default_factory=list)
    prompts: list[MCPPromptInfo] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    _stack: AsyncExitStack | None = None

    async def _transport(self, srv: Any):
        transport = str(getattr(srv, "transport", "stdio") or "stdio").lower()
        if transport in ("http", "streamable_http", "streamable-http"):
            from mcp.client.streamable_http import streamable_http_client
            read, write = await self._stack.enter_async_context(streamable_http_client(srv.url))
            return read, write
        if transport == "sse":
            from mcp.client.sse import sse_client
            read, write = await self._stack.enter_async_context(sse_client(srv.url))
            return read, write
        from mcp.client.stdio import StdioServerParameters, stdio_client
        env = {**os.environ, **(getattr(srv, "env", None) or {})}
        params = StdioServerParameters(command=srv.command, args=list(getattr(srv, "args", []) or []),
                                       env=env, cwd=getattr(srv, "cwd", None))
        return await self._stack.enter_async_context(stdio_client(params))

    async def connect(self) -> "MCPClient":
        from mcp import ClientSession

        self._stack = AsyncExitStack()
        for srv in self.servers:
            try:
                read, write = await self._transport(srv)
                session = await self._stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self.sessions[srv.name] = session
                listed = await session.list_tools()
                for tool in getattr(listed, "tools", []) or []:
                    schema = (getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None)
                              or {"type": "object", "properties": {}})
                    self.tools.append(MCPToolInfo(server=srv.name, name=tool.name,
                                                  description=(tool.description or "")[:1024],
                                                  input_schema=dict(schema)))
                try:
                    res = await session.list_resources()
                except Exception:
                    res = None
                for item in getattr(res, "resources", []) or []:
                    self.resources.append(MCPResourceInfo(
                        server=srv.name, uri=str(getattr(item, "uri", "")),
                        name=str(getattr(item, "name", "") or ""),
                        mime_type=str(getattr(item, "mimeType", "") or getattr(item, "mime_type", "") or ""),
                        description=str(getattr(item, "description", "") or "")[:1024]))
                try:
                    listed = await session.list_prompts()
                except Exception:
                    listed = None
                for item in getattr(listed, "prompts", []) or []:
                    self.prompts.append(MCPPromptInfo(
                        server=srv.name, name=str(getattr(item, "name", "")),
                        description=str(getattr(item, "description", "") or "")[:1024],
                        arguments=list(getattr(item, "arguments", []) or [])))
            except Exception as exc:  # one bad server must not take down the harness
                self.errors[srv.name] = f"{type(exc).__name__}: {exc}"
        return self

    async def get_prompt(self, server: str, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        session = self.sessions.get(server)
        if session is None:
            return ToolResult(f"MCP server '{server}' unavailable: {self.errors.get(server, 'not connected')}",
                              is_error=True)
        try:
            result = await session.get_prompt(name, arguments=arguments or {})
        except Exception as exc:
            return ToolResult(f"MCP prompt '{server}/{name}' failed: {type(exc).__name__}: {exc}", is_error=True)
        parts: list[str] = []
        for item in getattr(result, "messages", []) or []:
            content = getattr(item, "content", item)
            text = getattr(content, "text", None)
            if text is not None:
                parts.append(text)
            elif hasattr(content, "model_dump"):
                parts.append(json.dumps(content.model_dump(), default=str))
            else:
                parts.append(str(content))
        return ToolResult("\n".join(parts) or "(empty prompt)", metadata={"server": server, "prompt": name})

    async def read_resource(self, server: str, uri: str) -> ToolResult:
        session = self.sessions.get(server)
        if session is None:
            return ToolResult(f"MCP server '{server}' unavailable: {self.errors.get(server, 'not connected')}",
                              is_error=True)
        try:
            result = await session.read_resource(uri)
        except Exception as exc:
            return ToolResult(f"MCP resource '{server}/{uri}' failed: {type(exc).__name__}: {exc}", is_error=True)
        parts: list[str] = []
        for item in getattr(result, "contents", []) or []:
            text = getattr(item, "text", None)
            if text is not None:
                parts.append(text)
            elif getattr(item, "blob", None) is not None:
                parts.append(f"[blob {len(item.blob)} bytes {getattr(item, 'mimeType', '')}]")
            elif hasattr(item, "model_dump"):
                parts.append(json.dumps(item.model_dump(), default=str))
            else:
                parts.append(str(item))
        return ToolResult("\n".join(parts) or "(empty resource)", metadata={"server": server, "uri": uri})

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
