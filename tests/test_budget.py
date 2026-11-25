"""Per-run budget caps and USD cost accounting."""
from __future__ import annotations

import asyncio

from argo.core.agent import Agent, Budget
from argo.core.observability import summarize
from argo.core.types import Usage

from tests.test_agent_loop import FakeLLM, build_agent, make_cfg, tool_turn


async def _run_with_limits(tmp_path, script, executed, **limits):
    llm = FakeLLM(script)
    agent = build_agent(tmp_path, llm, executed)
    for key, val in limits.items():
        setattr(agent.cfg, key, val)
        agent.run_limits[key.replace("max_run_", "max_").replace("max_wall_seconds", "max_wall")] = val
    async with agent:
        return await agent.run("test task"), agent


async def test_tool_call_budget_stops_run(tmp_path):
    executed: list[str] = []
    script = [tool_turn(("inspect", f'{{"key": "k{i}"}}')) for i in range(10)]
    result, agent = await _run_with_limits(tmp_path, script, executed, max_run_tool_calls=3)

    assert result.stop_reason == "budget"
    assert len(executed) <= 6  # check happens at the top of the next iteration
    assert any(e.type == "run_budget" and e.data["reason"] == "run tool-call budget"
               for e in agent.events.history)


async def test_token_budget_stops_run(tmp_path):
    executed: list[str] = []
    # FakeLLM reports 15 total tokens per call; cap below one call's worth.
    script = [tool_turn(("inspect", f'{{"key": "k{i}"}}')) for i in range(8)]
    result, _agent = await _run_with_limits(tmp_path, script, executed, max_run_tokens=20)

    assert result.stop_reason == "budget"
    assert len(executed) <= 2


async def test_wall_clock_budget_stops_run(tmp_path):
    class SlowLLM(FakeLLM):
        async def chat(self, messages, tools=None, **kwargs):
            await asyncio.sleep(0.05)
            return await super().chat(messages, tools, **kwargs)

    executed: list[str] = []
    llm = SlowLLM([tool_turn(("inspect", '{"key": "a"}')) for _ in range(5)])
    agent = build_agent(tmp_path, llm, executed)
    agent.run_limits["max_wall"] = 0.01
    async with agent:
        result = await agent.run("test task")
    assert result.stop_reason == "budget"
    assert "wall clock" in result.output


def test_cost_uses_longest_model_prefix():
    from argo.config import ArgoConfig
    c = ArgoConfig(model_prices={"deepseek": {"input": 0.27, "output": 1.1},
                                 "deepseek-v4.1-flash": {"input": 0.5, "output": 2.0}})
    b = Budget(c)
    usage = Usage(prompt_tokens=1_000_000, completion_tokens=1_000_000, total_tokens=2_000_000)
    assert b.cost(usage, "deepseek-v4.1-flash") == 2.5
    assert b.cost(usage, "deepseek-other") == 1.37
    assert b.cost(usage, "unknown-model") == 0.0


def test_summarize_reports_cost(tmp_path):
    import json
    p = tmp_path / "s.jsonl"
    events = [
        {"type": "usage", "prompt": 1_000_000, "completion": 0, "total": 1_000_000, "model": "m-x"},
        {"type": "run_end", "stop_reason": "final"},
    ]
    p.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    stats = summarize([p], model_prices={"m-x": {"input": 2.0, "output": 8.0}})
    assert stats["cost"]["usd"] == 2.0 and stats["cost"]["priced"] is True
    assert summarize([p])["cost"]["usd"] == 0.0
