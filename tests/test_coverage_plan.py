"""Coverage-plan tests: decomposition state, research tools, and the loop's finish gate."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from argo.config import ArgoConfig
from argo.core.agent import Agent
from argo.core.types import LLMResponse, Message, ToolCall, Usage
from argo.research.plan import CoveragePlan
from argo.tools import research as research_tools
from argo.tools.base import ToolContext, ToolRegistry


def test_plan_roundtrip_and_marking():
    plan = CoveragePlan.from_texts("who leads the vendor?", ["find the vendor", "find its team", "find the lead"])
    assert [s.id for s in plan.subs] == ["Q1", "Q2", "Q3"]
    assert plan.stats()["coverage"] == 0.0 and not plan.complete
    plan.mark("Q1", ["S1"])
    plan.mark("Q1", ["S1", "S2"])
    plan.mark("Q2", ["S2"])
    assert plan.get("Q1").covered_by == ["S1", "S2"], "coverage ids dedupe"
    assert [s.id for s in plan.uncovered()] == ["Q3"]
    assert "Q3" in plan.render()
    plan.mark("Q3", ["S3"])
    assert plan.complete and plan.stats()["coverage"] == 1.0
    with pytest.raises(KeyError):
        plan.mark("Q9", ["S1"])


def test_plan_requires_subquestions():
    with pytest.raises(ValueError):
        CoveragePlan.from_texts("q", ["   "])


def make_ctx(tmp_path: Path) -> ToolContext:
    cfg = ArgoConfig(workspace=tmp_path, data_dir=tmp_path / "d", session_dir=tmp_path / "s",
                     index_dir=tmp_path / "i", workflow_dir=tmp_path / "w", skill_dir=tmp_path / "k",
                     spool_dir=tmp_path / "p")
    return ToolContext(cfg=cfg)


def test_tools_require_recorded_sources_and_valid_ids(tmp_path):
    ctx = make_ctx(tmp_path)
    assert research_tools._cover(ctx, "Q1", ["S1"]).is_error, "no plan yet"
    out = research_tools._plan(ctx, "q", ["a", "b"])
    assert not out.is_error and "Q1" in out.content
    assert research_tools._plan(ctx, "q", ["a"]).is_error, "an incomplete plan cannot be silently replaced"
    assert research_tools._cover(ctx, "Q7", ["S1"]).is_error
    assert research_tools._cover(ctx, "Q1", ["S9"]).is_error, "unknown source ids cannot fake coverage"
    research_tools._add_source(ctx, "http://x/1", "alpha code is ZX-441")
    ok = research_tools._cover(ctx, "Q1", ["S1"])
    assert not ok.is_error and ok.metadata["covered"] == 1
    # duplicates of a covered sub-question stay idempotent
    research_tools._cover(ctx, "Q1", ["S1"])
    assert ctx.extra["research_plan"].stats()["covered"] == 1


class ScriptedLLM:
    def __init__(self, script: list[Message]):
        self.script = list(script)
        self.calls: list[list[Message]] = []

    async def chat(self, messages, tools=None, **kwargs):
        self.calls.append(list(messages))
        msg = self.script.pop(0) if self.script else Message(role="assistant", content="done")
        return LLMResponse(message=msg, usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15))

    async def embed(self, texts, model=None):
        return [[0.0] for _ in texts]

    async def rerank(self, query, docs, top_n=None):
        return list(range(len(docs)))


def tool_turn(*calls: tuple[str, str]) -> Message:
    return Message(role="assistant", content=None,
                   tool_calls=[ToolCall(id=f"c{i}", name=n, arguments=a) for i, (n, a) in enumerate(calls)])


def make_agent(tmp_path: Path, script: list[Message]) -> Agent:
    cfg = ArgoConfig(workspace=tmp_path, data_dir=tmp_path / "data", session_dir=tmp_path / "sessions",
                     index_dir=tmp_path / "index", workflow_dir=tmp_path / "workflows",
                     skill_dir=tmp_path / "skills", spool_dir=tmp_path / "spool",
                     max_iterations=12, compaction_threshold=0.95, reset_threshold=0.99,
                     daily_token_budget=0)
    cfg.ensure_dirs()
    agent = Agent(cfg, llm=ScriptedLLM(script))

    def registry() -> ToolRegistry:
        reg = ToolRegistry(agent.tool_ctx)
        research_tools.register(reg)
        return reg

    agent._build_registry = registry  # type: ignore[method-assign]
    return agent


async def test_loop_refuses_to_finish_with_uncovered_plan(tmp_path):
    plan_args = json.dumps({"question": "who leads the vendor?",
                            "subquestions": ["find the vendor", "find its team"]})
    src_args = json.dumps({"url": "http://docs/vendors", "content": "Orion is managed by Helios."})
    script = [
        tool_turn(("plan_research", plan_args)),
        tool_turn(("add_source", src_args)),
        Message(role="assistant", content="Orion is managed by Helios, so the answer is Mira Chen."),
        Message(role="assistant", content="Final answer: Mira Chen"),
        Message(role="assistant", content="Answer: Mira Chen"),
    ]
    agent = make_agent(tmp_path, script)
    async with agent:
        result = await agent.run("who leads the vendor?")
    gates = [e for e in agent.events.history if e.type == "coverage_gate"]
    assert len(gates) == 2, "incomplete plan is nudged twice, then accepted"
    assert result.stop_reason == "final"
    assert "Mira Chen" in result.output
    # the reminder names the open sub-questions, so the model could act on it
    nudges = [m.content for m in result.messages if m.role == "user" and "not complete" in (m.content or "")]
    assert len(nudges) == 2 and "Q2" in nudges[0]


async def test_loop_finishes_once_plan_is_covered(tmp_path):
    plan_args = json.dumps({"question": "q", "subquestions": ["a"]})
    src_args = json.dumps({"url": "http://x", "content": "fact"})
    cover_args = json.dumps({"sub_id": "Q1", "source_ids": ["S1"]})
    script = [
        tool_turn(("plan_research", plan_args)),
        tool_turn(("add_source", src_args)),
        tool_turn(("mark_covered", cover_args)),
        Message(role="assistant", content="ANSWER: fact"),
    ]
    agent = make_agent(tmp_path, script)
    async with agent:
        result = await agent.run("q")
    assert not any(e.type == "coverage_gate" for e in agent.events.history)
    assert result.output == "ANSWER: fact"
    assert result.iterations == 4
