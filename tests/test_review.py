"""review_work must spawn a read-only, tool-scoped evaluator with explicit guidance."""
from __future__ import annotations

import pytest

import argo.core.agent as agent_mod
from argo.config import ArgoConfig
from argo.core.agent import Agent
from argo.core.session import Session
from argo.core.types import LLMResponse, Message, Usage
from argo.tools.base import ToolContext


class FakeLLM:
    async def chat(self, messages, tools=None, **kwargs):
        return LLMResponse(message=Message(role="assistant", content="{}"), usage=Usage())

    async def embed(self, texts, model=None):
        return [[0.0, 0.0] for _ in texts]


def make_cfg(tmp_path) -> ArgoConfig:
    cfg = ArgoConfig(workspace=tmp_path, data_dir=tmp_path / "data", session_dir=tmp_path / "sessions",
                     index_dir=tmp_path / "index", workflow_dir=tmp_path / "wf", skill_dir=tmp_path / "skills",
                     spool_dir=tmp_path / "spool")
    cfg.ensure_dirs()
    return cfg


class FakeReviewer:
    captured: dict = {}

    def __init__(self, cfg, **kwargs):
        self.cfg = cfg
        self.kwargs = kwargs
        self.session = None
        FakeReviewer.captured = {"kwargs": kwargs}

    async def run(self, task, **kwargs):
        from argo.core.agent import RunResult
        FakeReviewer.captured["brief"] = task
        self.session = Session.create(self.cfg, session_id="reviewer-test")
        return RunResult(output="PASS: all criteria met", session_id=self.session.id, iterations=2,
                         usage=Usage(total_tokens=42))

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_review_work_scopes_reviewer_to_readonly_tools(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    agent = Agent(cfg, llm=FakeLLM())
    agent.tool_ctx = ToolContext(cfg=cfg)
    agent.registry = agent._build_registry()  # registers review_work with the current registry
    monkeypatch.setattr(agent_mod, "Agent", FakeReviewer)

    result = await agent.registry.execute("review_work", {
        "criteria": "File exists and has three bullets", "paths": ["report.md"]})

    kwargs = FakeReviewer.captured["kwargs"]
    allow = kwargs["allow_tools"]
    assert "read_document" in allow and "read_file" in allow
    assert "bash" not in allow and "run_code" not in allow and "write_file" not in allow
    assert kwargs["max_iterations"] == 6

    brief = FakeReviewer.captured["brief"]
    assert "AVAILABLE TOOLS" in brief and "bash, run_code and write tools are NOT available" in brief
    assert "report.md" in brief and "three bullets" in brief
    assert "INDEPENDENT REVIEW" in result.content and "PASS" in result.content
    assert result.metadata["reviewer_session"] == "reviewer-test"
