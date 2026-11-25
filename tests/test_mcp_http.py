"""MCP over streamable HTTP: a real uvicorn server in-process, connected via transport='http'."""
from __future__ import annotations

import socket
import threading
import time

import pytest

from argo.config import MCPServerConfig
from argo.tools.mcp import MCPClient


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.asyncio
async def test_mcp_http_transport_lists_tools_and_reads_resource():
    uvicorn = pytest.importorskip("uvicorn")
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("http_echo")

    @server.tool()
    def add(a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    @server.resource("echo://readme", name="readme", mime_type="text/plain")
    def readme() -> str:
        return "http echo readme"

    port = _free_port()
    config = uvicorn.Config(server.streamable_http_app(), host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    assert srv.started, "uvicorn did not start"

    client = await MCPClient([MCPServerConfig(name="http_echo", transport="http",
                                              url=f"http://127.0.0.1:{port}/mcp")]).connect()
    try:
        assert client.errors == {}, client.errors
        assert {t.exposed_name for t in client.tools} == {"mcp__http_echo__add"}
        assert [r.uri for r in client.resources] == ["echo://readme"]
        result = await client.call("http_echo", "add", {"a": 20, "b": 22})
        assert not result.is_error and result.content.strip() == "42"
        doc = await client.read_resource("http_echo", "echo://readme")
        assert not doc.is_error and "http echo" in doc.content.lower()
    finally:
        await client.close()
        srv.should_exit = True
        thread.join(timeout=5)
