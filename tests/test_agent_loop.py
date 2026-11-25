"""Loop-guard unit tests: duplicate read-only blocking, no-progress nudge/stop, order preservation."""
from __future__ import annotations

from pathlib import Path

import pytest

from argo.config import ArgoConfig
from argo.core.agent import Agent
from argo.core.types import LLMResponse, Message, ToolCall, ToolResult, Usage
from argo.tools.base import ToolContext, ToolRegistry


def make_cfg(tmp_path: Path) -> ArgoConfig:
    return ArgoConfig(
        workspace=tmp_path,
        data_dir=tmp_path / "data",
        session_dir=tmp_path / "sessions",
        index_dir=tmp_path / "index",
        workflow_dir=tmp_path / "workflows",
        skill_dir=tmp_path / "skills",
        spool_dir=tmp_path / "spool",
        max_iterations=30,
        compaction_threshold=0.95,
        reset_threshold=0.99,
        daily_token_budget=0,
    )


class FakeLLM:
    """Returns scripted assistant turns, then a final answer."""

    def __init__(self, script: list[Message] | None = None):
        self.script = list(script or [])
        self.calls: list[list[Message]] = []

    async def chat(self, messages, tools=None, **kwargs):
        self.calls.append(list(messages))
        if tools is None:  # forced summary turn after a hard stop
            return LLMResponse(message=Message(role="assistant", content="summary of remaining work"),
                               usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15))
        if self.script:
            msg = self.script.pop(0)
        else:
            msg = Message(role="assistant", content="done")
        return LLMResponse(message=msg, usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15))

    async def embed(self, texts, model=None):
        return [[0.0, 0.0, 0.0, 0.0] for _ in texts]

    async def rerank(self, query, docs, top_n=None):
        return list(range(len(docs)))


def tool_turn(*calls: tuple[str, str]) -> Message:
    tcs = [ToolCall(id=f"c{i}", name=name, arguments=args) for i, (name, args) in enumerate(calls)]
    return Message(role="assistant", content=None, tool_calls=tcs)


def build_agent(tmp_path, llm: FakeLLM, executed: list[str]) -> Agent:
    cfg = make_cfg(tmp_path)
    cfg.ensure_dirs()
    agent = Agent(cfg, llm=llm)

    def fake_registry() -> ToolRegistry:
        assert agent.tool_ctx is not None
        reg = ToolRegistry(agent.tool_ctx)

        async def inspect(ctx: ToolContext, key: str = "") -> ToolResult:
            executed.append(f"inspect:{key}")
            return ToolResult(f"observed {key}")

        async def mutate(ctx: ToolContext, key: str = "") -> ToolResult:
            executed.append(f"write_file:{key}")
            return ToolResult(f"wrote {key}")

        async def fetch(ctx: ToolContext, url: str = "") -> ToolResult:
            executed.append(f"fetch_url:{url}")
            return ToolResult(f"page body for {url}")

        reg.add("inspect", "read-only probe", {"type": "object", "properties": {"key": {"type": "string"}}},
                inspect, read_only=True)
        reg.add("write_file", "mutating write", {"type": "object", "properties": {"key": {"type": "string"}}},
                mutate, read_only=False)
        reg.add("fetch_url", "read-only external fetch", {"type": "object", "properties": {"url": {"type": "string"}}},
                fetch, read_only=True, parallel_safe=True)
        agent.tool_ctx.extra["registry"] = reg
        return reg

    # Bind the fake registry instance-wide; ToolRegistry is fully exercised elsewhere.
    agent._build_registry = fake_registry  # type: ignore[method-assign]
    return agent


async def run(tmp_path, script, executed):
    llm = FakeLLM(script)
    agent = build_agent(tmp_path, llm, executed)
    async with agent:
        return await agent.run("test task"), agent


async def test_duplicate_read_only_calls_blocked(tmp_path):
    executed: list[str] = []
    dup = ('inspect', '{"key": "same"}')
    result, agent = await run(tmp_path, [tool_turn(dup, dup, dup)], executed)

    # First two identical calls run, the third is rejected inside the same turn.
    assert executed == ["inspect:same", "inspect:same"]
    blocked = [m for m in result.messages if m.role == "tool" and "Duplicate read-only call blocked" in (m.content or "")]
    assert len(blocked) == 1
    assert any(e.type == "tool_duplicate" for e in agent.events.history)
    assert result.stop_reason == "final"


async def test_duplicate_blocked_across_turns(tmp_path):
    executed: list[str] = []
    call = ('inspect', '{"key": "x"}')
    script = [tool_turn(call), tool_turn(call), tool_turn(call)]
    result, agent = await run(tmp_path, script, executed)

    assert executed == ["inspect:x", "inspect:x"]
    assert sum(1 for e in agent.events.history if e.type == "tool_duplicate") == 1
    assert result.stop_reason == "final"


async def test_no_progress_nudges_then_stops(tmp_path):
    executed: list[str] = []
    script = [tool_turn(("inspect", f'{{"key": "k{i}"}}')) for i in range(12)]
    result, agent = await run(tmp_path, script, executed)

    assert result.stop_reason == "no_progress"
    assert result.iterations == 10
    nudges = [e for e in agent.events.history if e.type == "loop_nudge"]
    assert [e.data["count"] for e in nudges] == [4, 7]
    assert any(e.type == "loop_stop" for e in agent.events.history)
    # A concise status report is produced after the hard stop.
    assert result.output and result.output != "(no final answer)"


async def test_progress_resets_no_progress_counter(tmp_path):
    executed: list[str] = []
    script = [
        tool_turn(("inspect", '{"key": "a"}')),
        tool_turn(("inspect", '{"key": "b"}')),
        tool_turn(("inspect", '{"key": "c"}')),
        tool_turn(("write_file", '{"key": "out"}')),
        tool_turn(("inspect", '{"key": "d"}')),
        tool_turn(("inspect", '{"key": "e"}')),
        tool_turn(("inspect", '{"key": "f"}')),
    ]
    result, agent = await run(tmp_path, script, executed)

    assert result.stop_reason == "final"
    assert not any(e.type == "loop_nudge" for e in agent.events.history)
    assert result.iterations == 8  # 7 tool turns + 1 final


async def test_tool_results_preserve_call_order(tmp_path):
    executed: list[str] = []
    turn = tool_turn(("inspect", '{"key": "one"}'),
                     ("write_file", '{"key": "two"}'),
                     ("inspect", '{"key": "three"}'))
    result, agent = await run(tmp_path, [turn], executed)

    assert executed == ["inspect:one", "write_file:two", "inspect:three"]
    tool_msgs = [m for m in result.messages if m.role == "tool"]
    assert [m.name for m in tool_msgs] == ["inspect", "write_file", "inspect"]
    assert [m.content for m in tool_msgs] == ["observed one", "wrote two", "observed three"]
    assert [m.tool_call_id for m in tool_msgs] == ["c0", "c1", "c2"]


async def test_no_progress_limit_is_configurable(tmp_path):
    executed: list[str] = []
    script = [tool_turn(("inspect", f'{{"key": "z{i}"}}')) for i in range(8)]
    llm = FakeLLM(script)
    agent = build_agent(tmp_path, llm, executed)
    agent.cfg.no_progress_limit = 5
    async with agent:
        result = await agent.run("test task")

    assert result.stop_reason == "no_progress" and result.iterations == 5
    nudges = [e.data["count"] for e in agent.events.history if e.type == "loop_nudge"]
    assert nudges == [2, 3]


async def test_external_fetches_count_as_progress(tmp_path):
    """A long sequential fetch chain is research, not a verify-only loop (36-step context benchmark)."""
    executed: list[str] = []
    script = [tool_turn(("fetch_url", f'{{"url": "https://example.test/step/{i}"}}')) for i in range(14)]
    result, agent = await run(tmp_path, script, executed)

    assert result.stop_reason == "final"
    assert len(executed) == 14
    assert not any(e.type == "loop_nudge" for e in agent.events.history)


async def test_repeated_external_fetch_is_not_progress(tmp_path):
    """Identical reads still trip the guard: only novel external calls count."""
    executed: list[str] = []
    script = [tool_turn(("fetch_url", '{"url": "https://example.test/same"}')) for _ in range(16)]
    result, agent = await run(tmp_path, script, executed)

    assert result.stop_reason == "no_progress"
    assert len(executed) == 2


def read_agent(tmp_path, script, executed):
    """Like build_agent but with real read-tool names (novel-path progress rule)."""
    cfg = make_cfg(tmp_path)
    cfg.ensure_dirs()
    llm = FakeLLM(script)
    agent = Agent(cfg, llm=llm)

    def fake_registry() -> ToolRegistry:
        reg = ToolRegistry(agent.tool_ctx)

        async def read(ctx: ToolContext, path: str = "") -> ToolResult:
            executed.append(f"read_file:{path}")
            return ToolResult(f"contents of {path}")

        reg.add("read_file", "read a file", {"type": "object", "properties": {"path": {"type": "string"}}},
                read, read_only=True, parallel_safe=True)
        agent.tool_ctx.extra["registry"] = reg
        return reg

    agent._build_registry = fake_registry  # type: ignore[method-assign]
    return agent, llm


async def test_novel_file_reads_count_as_progress(tmp_path):
    executed: list[str] = []
    script = [tool_turn(("read_file", f'{{"path": "src/f{i}.py"}}')) for i in range(14)]
    agent, _ = read_agent(tmp_path, script, executed)
    async with agent:
        result = await agent.run("explore the repo")

    assert result.stop_reason == "final" and len(executed) == 14
    assert not any(e.type == "loop_nudge" for e in agent.events.history)


async def test_rereading_the_same_file_is_not_progress(tmp_path):
    executed: list[str] = []
    script = [tool_turn(("read_file", '{"path": "src/app.py"}')) for _ in range(20)]
    agent, _ = read_agent(tmp_path, script, executed)
    async with agent:
        result = await agent.run("re-inspect")

    assert result.stop_reason == "no_progress"
    assert len(executed) == 2  # exact-repeat guard blocks the rest
