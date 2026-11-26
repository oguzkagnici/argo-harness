"""Per-turn model routing: policy, intent detection, and loop wiring."""
from __future__ import annotations

from pathlib import Path

from argo.config import ArgoConfig
from argo.core.agent import Agent
from argo.core.router import ModelRouter, TurnSignal, intent_is_mutating
from argo.core.types import LLMResponse, Message, ToolCall, ToolResult, Usage
from argo.tools.base import ToolContext, ToolRegistry


def test_intent_detection():
    assert intent_is_mutating("Write a summary to report.md")
    assert intent_is_mutating("Fix the failing test")
    assert intent_is_mutating("Implement a rate limiter")
    assert not intent_is_mutating("Research who leads the vendor")
    assert not intent_is_mutating("What is Project Alpha's launch code?")
    assert not intent_is_mutating("Summarize the quarterly metrics")
    assert not intent_is_mutating("")


def test_policy_escalation_rules():
    r = ModelRouter("strong", "cheap", "explore")
    assert r.enabled
    assert r.choose(TurnSignal()) == "cheap"
    assert r.choose(TurnSignal(task_writes=True)) is None
    assert r.choose(TurnSignal(wrote=True)) is None
    assert r.choose(TurnSignal(last_tool_error=True)) is None
    assert r.stats == {"cheap": 1, "strong": 3}
    assert "mutates state" in r.reason(TurnSignal(task_writes=True))

    off = ModelRouter("strong", "cheap", "off")
    assert not off.enabled and off.choose(TurnSignal()) is None

    same = ModelRouter("m", "m", "explore")
    assert not same.enabled, "routing to the same model is a no-op"
    empty = ModelRouter("strong", "", "explore")
    assert not empty.enabled


class ModelRecorder:
    def __init__(self, script):
        self.script = list(script)
        self.models: list[str | None] = []

    async def chat(self, messages, tools=None, **kwargs):
        self.models.append(kwargs.get("model"))
        msg = self.script.pop(0) if self.script else Message(role="assistant", content="done")
        return LLMResponse(message=msg, usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15))

    async def embed(self, texts, model=None):
        return [[0.0] for _ in texts]

    async def rerank(self, query, docs, top_n=None):
        return list(range(len(docs)))


def tool_turn(*calls):
    return Message(role="assistant", content=None,
                   tool_calls=[ToolCall(id=f"c{i}", name=n, arguments=a) for i, (n, a) in enumerate(calls)])


def build(tmp_path: Path, llm, policy: str = "explore") -> Agent:
    cfg = ArgoConfig(workspace=tmp_path, data_dir=tmp_path / "d", session_dir=tmp_path / "s",
                     index_dir=tmp_path / "i", workflow_dir=tmp_path / "w", skill_dir=tmp_path / "k",
                     spool_dir=tmp_path / "p", max_iterations=10, compaction_threshold=0.95,
                     reset_threshold=0.99, daily_token_budget=0, cheap_model="cheap-model")
    cfg.router_policy = policy
    cfg.ensure_dirs()
    agent = Agent(cfg, llm=llm)

    def registry() -> ToolRegistry:
        reg = ToolRegistry(agent.tool_ctx)

        async def inspect(ctx: ToolContext, key: str = "") -> ToolResult:
            return ToolResult(f"observed {key}")

        async def write(ctx: ToolContext, key: str = "") -> ToolResult:
            return ToolResult(f"wrote {key}")

        reg.add("inspect", "read", {"type": "object", "properties": {"key": {"type": "string"}}},
                inspect, read_only=True)
        reg.add("write_file", "write", {"type": "object", "properties": {"key": {"type": "string"}}},
                write, read_only=False)
        return reg

    agent._build_registry = registry  # type: ignore[method-assign]
    return agent


async def test_read_only_task_stays_on_cheap_model(tmp_path):
    llm = ModelRecorder([tool_turn(("inspect", '{"key": "a"}')), Message(role="assistant", content="answer")])
    agent = build(tmp_path, llm)
    async with agent:
        await agent.run("Research what the vendor budget is")
    assert llm.models == ["cheap-model", "cheap-model"]
    assert agent.router.stats["cheap"] == 2


async def test_mutating_task_never_uses_cheap_model(tmp_path):
    llm = ModelRecorder([Message(role="assistant", content="done")])
    agent = build(tmp_path, llm)
    async with agent:
        await agent.run("Write the release notes to NOTES.md")
    assert llm.models == [None], "strong default (no model kwarg) for a write task"


async def test_escalates_after_mutation_even_if_task_looked_read_only(tmp_path):
    llm = ModelRecorder([
        tool_turn(("write_file", '{"key": "out"}')),   # cheap turn that mutates
        Message(role="assistant", content="done"),
    ])
    agent = build(tmp_path, llm)
    async with agent:
        await agent.run("Look at the data and store the result")  # no write verb -> exploit allowed
    assert llm.models == ["cheap-model", None]
    assert agent.router.stats["strong"] == 1


async def test_policy_off_sends_no_model_override(tmp_path):
    llm = ModelRecorder([Message(role="assistant", content="done")])
    agent = build(tmp_path, llm, policy="off")
    async with agent:
        await agent.run("Research the vendor budget")
    assert llm.models == [None]
