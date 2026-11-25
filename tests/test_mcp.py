import sys
from pathlib import Path

import pytest

from argo.config import MCPServerConfig, load_config
from argo.core.agent import Agent
from argo.core.types import LLMResponse, Message, Usage
from argo.tools.mcp import MCPClient

FIXTURE = Path(__file__).parent / "fixtures" / "mcp_echo_server.py"


def server_cfg(name="echo", read_only=False):
    return MCPServerConfig(name=name, command=sys.executable, args=[str(FIXTURE)], read_only=read_only)


@pytest.mark.asyncio
async def test_mcp_client_lists_and_calls_tools():
    client = await MCPClient([server_cfg()]).connect()
    try:
        assert client.errors == {}
        names = {t.exposed_name for t in client.tools}
        assert names == {"mcp__echo__add", "mcp__echo__reverse"}
        add = next(t for t in client.tools if t.name == "add")
        assert add.input_schema.get("required") == ["a", "b"]
        assert set(add.input_schema.get("properties", {})) == {"a", "b"}
        result = await client.call("echo", "add", {"a": 2, "b": 40})
        assert not result.is_error and result.content.strip() == "42"
        rev = await client.call("echo", "reverse", {"text": "argo"})
        assert rev.content.strip() == "ogra"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mcp_bad_server_is_isolated():
    bad = MCPServerConfig(name="broken", command="/nonexistent/binary")
    client = await MCPClient([bad, server_cfg()]).connect()
    try:
        assert "broken" in client.errors
        assert {"mcp__echo__add", "mcp__echo__reverse"} <= {t.exposed_name for t in client.tools}
        failed = await client.call("broken", "x", {})
        assert failed.is_error and "unavailable" in failed.content
    finally:
        await client.close()


class FakeLLM:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def chat(self, messages, tools=None, **kwargs):
        return LLMResponse(message=Message(role="assistant", content="done"), usage=Usage())

    async def embed(self, texts):
        return [[0.0] * 8 for _ in texts]

    async def rerank(self, query, docs, top_n=None):
        return [(float(len(docs) - i), i) for i in range(len(docs))][: top_n or len(docs)]


@pytest.mark.asyncio
async def test_agent_registry_exposes_mcp_tools(tmp_path):
    cfg = load_config({"workspace": str(tmp_path), "data_dir": str(tmp_path / ".argo")})
    cfg.mcp_servers = [server_cfg(read_only=True)]
    async with Agent(cfg, llm=FakeLLM(), max_iterations=1) as agent:
        await agent.run("say done")
        assert agent.mcp is not None and len(agent.mcp.tools) == 2
        names = set(agent.registry.tools)
        assert {"mcp__echo__add", "mcp__echo__reverse"} <= names
        tool = agent.registry.tools["mcp__echo__add"]
        assert tool.read_only and tool.parallel_safe and tool.category == "mcp"
        result = await agent.registry.execute("mcp__echo__add", {"a": 5, "b": 6})
        assert not result.is_error and result.content.strip() == "11"
