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


@pytest.mark.asyncio
async def test_approved_plan_persists_across_sessions(tmp_path):
    from argo.core.session import Session

    cfg = make_cfg(tmp_path, plan_mode=True)

    def approve(tool, args, reason):
        return True

    script = [call("propose_plan", steps=["write out.txt"], actions=["write_file"]),
              call("write_file", path="out.txt", content="first")]
    async with Agent(cfg, llm=ScriptedLLM(script), approvals=approve, max_iterations=4) as agent:
        first = await agent.run("write out.txt")
    assert (tmp_path / "out.txt").read_text() == "first"

    resumed = Session.load(cfg, first.session_id)
    assert resumed.plan_approved is True and resumed.approved_actions == ["write_file"]

    # New run on the resumed session: no propose_plan needed, writes stay enabled.
    prompts: list[str] = []

    def approve2(tool, args, reason):
        prompts.append(tool.name)
        return True

    script2 = [call("write_file", path="out.txt", content="second")]
    async with Agent(cfg, llm=ScriptedLLM(script2), approvals=approve2, max_iterations=3) as agent:
        await agent.run("continue", session=resumed)
    assert (tmp_path / "out.txt").read_text() == "second"
    assert "propose_plan" not in prompts  # prior approval reused


@pytest.mark.asyncio
async def test_scope_change_requires_fresh_approval(tmp_path):
    from argo.core.session import Session

    cfg = make_cfg(tmp_path, plan_mode=True)
    approvals: list[dict] = []

    def approve(tool, args, reason):
        approvals.append({"tool": tool.name, "args": args})
        return True

    script = [call("propose_plan", steps=["write out.txt"], actions=["write_file"]),
              call("write_file", path="out.txt", content="ok")]
    async with Agent(cfg, llm=ScriptedLLM(script), approvals=approve, max_iterations=4) as agent:
        first = await agent.run("write out.txt")

    resumed = Session.load(cfg, first.session_id)
    approvals.clear()
    # The resumed session asks for a shell action that was not in the approved scope.
    script2 = [call("propose_plan", steps=["inspect repo", "edit"], actions=["bash"]),
               call("bash", command="echo hi")]
    async with Agent(cfg, llm=ScriptedLLM(script2), approvals=approve, max_iterations=4) as agent:
        await agent.run("continue", session=resumed)
    assert any(a["tool"] == "propose_plan" for a in approvals)  # scope change re-prompted
