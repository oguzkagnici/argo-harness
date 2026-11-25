"""Exploratory shell/code probes count as progress when their output is new; verify loops still stop."""
from __future__ import annotations

from argo.core.agent import Agent
from argo.core.types import ToolResult
from argo.tools.base import ToolContext, ToolRegistry

from tests.test_agent_loop import FakeLLM, make_cfg, tool_turn


class ShellLLM(FakeLLM):
    pass


def build(tmp_path, outputs, script):
    cfg = make_cfg(tmp_path)
    cfg.ensure_dirs()
    agent = Agent(cfg, llm=FakeLLM(script))
    state = {"i": 0}

    def registry() -> ToolRegistry:
        reg = ToolRegistry(agent.tool_ctx)

        async def bash(ctx: ToolContext, command: str = "") -> ToolResult:
            i = state["i"]
            state["i"] += 1
            body = outputs[min(i, len(outputs) - 1)]
            return ToolResult(body, is_error=False, metadata={"duration_s": 0.01})

        reg.add("bash", "run shell", {"type": "object", "properties": {"command": {"type": "string"}}},
                bash, read_only=False)
        agent.tool_ctx.extra["registry"] = reg
        return reg

    agent._build_registry = registry  # type: ignore[method-assign]
    return agent


async def test_novel_shell_output_prevents_no_progress_stop(tmp_path):
    outputs = [f"result line {i}" for i in range(15)]
    script = [tool_turn(("bash", f'{{"command": "grep x{i}"}}')) for i in range(14)]
    agent = build(tmp_path, outputs, script)
    async with agent:
        result = await agent.run("explore", stream=False)
    assert result.stop_reason == "final"  # ran all 14 turns without hitting the 10-step guard
    assert result.iterations >= 14


async def test_identical_shell_output_is_not_progress(tmp_path):
    script = [tool_turn(("bash", '{"command": "pytest same"}')) for _ in range(14)]
    agent = build(tmp_path, ["the same failure traceback\n"], script)
    async with agent:
        result = await agent.run("verify", stream=False)
    assert result.stop_reason == "no_progress"
    # the first probe is novel (progress); the next 10 identical probes stop the run
    assert result.iterations == 11
