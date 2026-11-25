import json

import pytest

from argo.config import load_config
from argo.core.agent import Agent
from argo.core.types import LLMResponse, Message, ToolCall, Usage


def call(name, **args):
    return LLMResponse(message=Message(role="assistant", tool_calls=[
        ToolCall(id=f"c-{name}", name=name, arguments=json.dumps(args))]), usage=Usage())


class ScriptedLLM:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def chat(self, messages, tools=None, **kwargs):
        idx = self.calls
        self.calls += 1
        return self.script[idx] if idx < len(self.script) else LLMResponse(
            message=Message(role="assistant", content="done"), usage=Usage())

    async def embed(self, texts):
        return [[0.0] * 8 for _ in texts]

    async def rerank(self, query, docs, top_n=None):
        return [(float(len(docs) - i), i) for i in range(len(docs))][: top_n or len(docs)]


def make_cfg(tmp_path, plan_mode):
    return load_config({"workspace": str(tmp_path), "data_dir": str(tmp_path / ".argo"),
                        "plan_mode": plan_mode})


@pytest.mark.asyncio
async def test_plan_mode_blocks_until_plan_approved(tmp_path):
    approvals: list[tuple[str, dict, str]] = []

    def approve(tool, args, reason):
        approvals.append((tool.name, args, reason))
        return True

    script = [call("write_file", path="out.txt", content="hello"),
              call("propose_plan", steps=["write out.txt"], actions=["write_file"]),
              call("write_file", path="out.txt", content="hello")]
    cfg = make_cfg(tmp_path, plan_mode=True)
    async with Agent(cfg, llm=ScriptedLLM(script), approvals=approve, max_iterations=6) as agent:
        result = await agent.run("write out.txt")
    assert result.stop_reason == "final"
    tool_msgs = [m.content for m in agent.messages if m.role == "tool"]
    assert "Plan mode is on" in tool_msgs[0]
    assert "Plan approved" in tool_msgs[1]
    assert "Wrote" in tool_msgs[2]
    assert (tmp_path / "out.txt").read_text() == "hello"
    # one approval prompt for the whole plan, none for the individual write
    assert [a[0] for a in approvals] == ["propose_plan", "write_file"]
    assert approvals[0][1]["steps"] == ["write out.txt"]
    assert approvals[1][2] == "covered by approved plan"


@pytest.mark.asyncio
async def test_rejected_plan_keeps_mutations_blocked(tmp_path):
    def reject(tool, args, reason):
        return False

    script = [call("write_file", path="out.txt", content="nope"),
              call("propose_plan", steps=["write out.txt"]),
              call("final_answer", text="stopped")]
    cfg = make_cfg(tmp_path, plan_mode=True)
    async with Agent(cfg, llm=ScriptedLLM(script), approvals=reject, max_iterations=4) as agent:
        await agent.run("write out.txt")
    tool_msgs = [m.content for m in agent.messages if m.role == "tool"]
    assert "Plan rejected" in tool_msgs[1]
    assert not (tmp_path / "out.txt").exists()


@pytest.mark.asyncio
async def test_plan_mode_off_allows_direct_writes(tmp_path):
    script = [call("write_file", path="out.txt", content="direct")]
    cfg = make_cfg(tmp_path, plan_mode=False)
    async with Agent(cfg, llm=ScriptedLLM(script), max_iterations=3) as agent:
        await agent.run("write out.txt")
    assert (tmp_path / "out.txt").read_text() == "direct"
    assert not any("Plan mode" in (m.content or "") for m in agent.messages)
