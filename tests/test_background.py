import asyncio

import pytest

from argo.config import load_config
from argo.core.agent import Agent
from argo.core.types import LLMResponse, Message, Usage


class BgLLM:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def chat(self, messages, tools=None, **kwargs):
        text = "\n".join(m.content or "" for m in messages if m.role == "user")
        if "FAIL_JOB" in text:
            raise RuntimeError("worker exploded")
        if "BG_JOB" in text:
            await asyncio.sleep(0.05)
            return LLMResponse(message=Message(role="assistant", content="bg result 42"), usage=Usage())
        return LLMResponse(message=Message(role="assistant", content="parent done"), usage=Usage())

    async def embed(self, texts):
        return [[0.0] * 8 for _ in texts]

    async def rerank(self, query, docs, top_n=None):
        return [(float(len(docs) - i), i) for i in range(len(docs))][: top_n or len(docs)]


async def wait_done(agent, job_id, timeout=3.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        res = await agent.registry.execute("check_background", {"id": job_id})
        if res.metadata.get("status") != "running":
            return res
        await asyncio.sleep(0.03)
    raise AssertionError("background job did not finish")


@pytest.mark.asyncio
async def test_background_job_runs_and_reports(tmp_path):
    cfg = load_config({"workspace": str(tmp_path), "data_dir": str(tmp_path / ".argo")})
    async with Agent(cfg, llm=BgLLM(), max_iterations=2) as agent:
        await agent.run("hi")
        started = await agent.registry.execute("start_background", {"task": "BG_JOB: compute"})
        assert not started.is_error
        job_id = started.metadata["job_id"]
        first = await agent.registry.execute("check_background", {"id": job_id})
        assert first.metadata["status"] in ("running", "done")
        waited = await agent.registry.execute("check_background", {"id": job_id, "wait_seconds": 2})
        assert waited.metadata["status"] == "done" and "bg result 42" in waited.content
        done = await wait_done(agent, job_id)
        assert "bg result 42" in done.content
        assert done.metadata["status"] == "done"
        listing = await agent.registry.execute("list_background", {})
        assert job_id in listing.content


@pytest.mark.asyncio
async def test_background_failure_is_contained(tmp_path):
    cfg = load_config({"workspace": str(tmp_path), "data_dir": str(tmp_path / ".argo")})
    async with Agent(cfg, llm=BgLLM(), max_iterations=2) as agent:
        await agent.run("hi")
        started = await agent.registry.execute("start_background", {"task": "FAIL_JOB"})
        done = await wait_done(agent, started.metadata["job_id"])
        assert done.metadata["status"] == "error"
        assert "worker exploded" in done.content
        unknown = await agent.registry.execute("check_background", {"id": "nope"})
        assert unknown.is_error
