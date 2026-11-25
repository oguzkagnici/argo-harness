import json

import pytest

from argo.config import load_config
from argo.core.agent import Agent
from argo.core.types import LLMResponse, Message, ToolCall, Usage


def call(name, **args):
    return LLMResponse(message=Message(role="assistant", tool_calls=[
        ToolCall(id=f"c-{name}-{len(args)}", name=name, arguments=json.dumps(args))]), usage=Usage())


def final(text="done"):
    return LLMResponse(message=Message(role="assistant", content=text), usage=Usage())


class ReviewLLM:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.reviews = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def chat(self, messages, tools=None, **kwargs):
        if any(m.role == "user" and "You are an independent evaluator" in (m.content or "") for m in messages):
            self.reviews += 1
            return final("VERDICT: PASS\n- artifact opens and matches the request")
        idx = self.calls
        self.calls += 1
        return self.script[idx] if idx < len(self.script) else final()

    async def embed(self, texts):
        return [[0.0] * 8 for _ in texts]

    async def rerank(self, query, docs, top_n=None):
        return [(float(len(docs) - i), i) for i in range(len(docs))][: top_n or len(docs)]


def cfg_for(tmp_path, policy, threshold=4):
    return load_config({"workspace": str(tmp_path), "data_dir": str(tmp_path / ".argo"),
                        "review_policy": policy, "review_risk_threshold": threshold})


@pytest.mark.asyncio
async def test_adaptive_reviews_two_code_files(tmp_path):
    script = [call("write_file", path="a.py", content="print(1)"),
              call("write_file", path="b.py", content="print(2)"),
              final()]
    llm = ReviewLLM(script)
    async with Agent(cfg_for(tmp_path, "adaptive"), llm=llm, max_iterations=6) as agent:
        result = await agent.run("write two scripts")
    assert llm.reviews == 1
    assert "AUTO-REVIEW" in result.output and "VERDICT: PASS" in result.output


@pytest.mark.asyncio
async def test_adaptive_skips_low_risk_run(tmp_path):
    script = [call("write_file", path="a.py", content="print(1)"), final()]
    llm = ReviewLLM(script)
    async with Agent(cfg_for(tmp_path, "adaptive"), llm=llm, max_iterations=5) as agent:
        result = await agent.run("write one script")
    assert llm.reviews == 0
    assert "AUTO-REVIEW" not in result.output


@pytest.mark.asyncio
async def test_off_policy_never_reviews(tmp_path):
    script = [call("write_file", path="a.py", content="print(1)"),
              call("write_file", path="b.py", content="print(2)"), final()]
    llm = ReviewLLM(script)
    async with Agent(cfg_for(tmp_path, "off"), llm=llm, max_iterations=6) as agent:
        result = await agent.run("write two scripts")
    assert llm.reviews == 0 and "AUTO-REVIEW" not in result.output


@pytest.mark.asyncio
async def test_always_skips_when_review_already_used(tmp_path):
    script = [call("write_file", path="a.py", content="print(1)"),
              call("review_work", criteria="file exists", paths=["a.py"]),
              final()]
    llm = ReviewLLM(script)
    async with Agent(cfg_for(tmp_path, "always"), llm=llm, max_iterations=6) as agent:
        result = await agent.run("write and review")
    assert llm.reviews == 1          # only the explicit call
    assert "AUTO-REVIEW" not in result.output


def test_risk_score_weights_documents_and_code(tmp_path):
    cfg = cfg_for(tmp_path, "adaptive")
    async def build():
        return Agent(cfg, llm=ReviewLLM([]))
    agent = __import__("asyncio").run(build())
    from argo.core.session import Session
    agent.session = Session.create(cfg, None)
    agent.session.files_touched = ["report.docx"]
    assert agent._review_risk()[0] == 4
    agent.session.files_touched = ["a.py", "b.py", "c.py"]
    assert agent._review_risk()[0] == 4          # code capped at 4
    agent.session.files_touched = ["report.docx", "deck.pptx"]
    assert agent._review_risk()[0] == 8


@pytest.mark.asyncio
async def test_auto_review_criteria_include_goal(tmp_path):
    script = [call("write_file", path="a.py", content="print(1)"),
              call("write_file", path="b.py", content="print(2)"), final()]
    llm = ReviewLLM(script)
    seen: list[str] = []
    original = llm.chat

    async def spy(messages, tools=None, **kwargs):
        if any(m.role == "user" and "You are an independent evaluator" in (m.content or "") for m in messages):
            seen.append(next(m.content for m in messages if m.role == "user"))
        return await original(messages, tools, **kwargs)

    llm.chat = spy  # type: ignore[method-assign]
    async with Agent(cfg_for(tmp_path, "adaptive"), llm=llm, max_iterations=6) as agent:
        await agent.run("write two scripts named a and b")
    assert seen and "The user asked: write two scripts named a and b" in seen[0]
    assert "Do not require sections" in seen[0]
