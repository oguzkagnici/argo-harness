"""Per-subagent MCP scoping: children start only the servers a parent names."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from argo.config import MCPServerConfig, load_config
from argo.core.agent import Agent
from argo.core.types import LLMResponse, Message, Usage

FIXTURE = Path(__file__).parent / "fixtures" / "mcp_echo_server.py"


def server_cfg(name: str, read_only: bool = True) -> MCPServerConfig:
    return MCPServerConfig(name=name, command=sys.executable, args=[str(FIXTURE)], read_only=read_only)


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


def cfg_for(tmp_path, names=("alpha", "beta")):
    cfg = load_config({"workspace": str(tmp_path), "data_dir": str(tmp_path / ".argo")})
    cfg.mcp_servers = [server_cfg(n) for n in names]
    return cfg


@pytest.mark.asyncio
async def test_child_starts_only_named_servers(tmp_path):
    cfg = cfg_for(tmp_path)
    async with Agent(cfg, llm=FakeLLM(), max_iterations=1, depth=1, mcp_servers=["alpha"]) as child:
        await child.run("say done")
        names = set(child.registry.tools)
        assert any(n.startswith("mcp__alpha__") for n in names)
        assert not any(n.startswith("mcp__beta__") for n in names)


@pytest.mark.asyncio
async def test_child_gets_no_mcp_by_default(tmp_path):
    cfg = cfg_for(tmp_path)
    async with Agent(cfg, llm=FakeLLM(), max_iterations=1, depth=1) as child:
        await child.run("say done")
        assert child.mcp is None
        assert not any(n.startswith("mcp__") for n in child.registry.tools)


@pytest.mark.asyncio
async def test_top_level_still_gets_every_server(tmp_path):
    cfg = cfg_for(tmp_path)
    async with Agent(cfg, llm=FakeLLM(), max_iterations=1) as agent:
        await agent.run("say done")
        names = set(agent.registry.tools)
        assert any(n.startswith("mcp__alpha__") for n in names)
        assert any(n.startswith("mcp__beta__") for n in names)


@pytest.mark.asyncio
async def test_spawn_rejects_unknown_server(tmp_path):
    cfg = cfg_for(tmp_path)
    async with Agent(cfg, llm=FakeLLM(), max_iterations=1) as agent:
        await agent.run("say done")
        result = await agent.registry.execute("spawn_subagent", {"task": "x", "mcp_servers": ["nope"]})
        assert result.is_error and "Unknown MCP server" in result.content


@pytest.mark.asyncio
async def test_spawn_emits_the_scope_and_child_runs(tmp_path):
    cfg = cfg_for(tmp_path)
    async with Agent(cfg, llm=FakeLLM(), max_iterations=1) as agent:
        await agent.run("say done")
        result = await agent.registry.execute("spawn_subagent",
                                              {"task": "say done", "mcp_servers": ["alpha"], "max_iterations": 1})
        assert not result.is_error, result.content
    scopes = [e.data.get("servers") for e in agent.events.history if e.type == "subagent_mcp_scope"]
    assert scopes == [["alpha"]]
