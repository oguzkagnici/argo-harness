"""SSE contract tests for the OpenAI-compatible and native streaming endpoints."""
from __future__ import annotations

import inspect
import json

import httpx
import pytest

import argo.server as server
from argo.config import ArgoConfig
from argo.core.events import EventBus
from argo.core.types import Usage
from argo.core.agent import RunResult
from argo.llm.client import StreamDelta


def make_cfg(tmp_path) -> ArgoConfig:
    cfg = ArgoConfig(workspace=tmp_path, data_dir=tmp_path / "data", session_dir=tmp_path / "sessions",
                     index_dir=tmp_path / "index", workflow_dir=tmp_path / "wf", skill_dir=tmp_path / "skills",
                     spool_dir=tmp_path / "spool")
    cfg.ensure_dirs()
    return cfg


class FakeAgent:
    instances: list["FakeAgent"] = []

    def __init__(self, cfg, on_delta=None, **kwargs):
        self.cfg = cfg
        self.on_delta = on_delta
        self.events = EventBus()
        self.memory = None
        FakeAgent.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def close(self):
        return None

    async def run(self, task, session=None, continue_conversation=False, stream=None):
        self.events.emit("assistant", content="hello ", tool_calls=[])
        if self.on_delta is not None:
            result = self.on_delta(StreamDelta(text="hello "))
            if inspect.isawaitable(result):
                await result
            result = self.on_delta(StreamDelta(text="world"))
            if inspect.isawaitable(result):
                await result
        usage = Usage(prompt_tokens=4, completion_tokens=2, total_tokens=6)
        return RunResult(output="hello world", session_id="sess-test", iterations=1, usage=usage)


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "Agent", FakeAgent)
    FakeAgent.instances.clear()
    return server.create_app(make_cfg(tmp_path))


async def post_stream(app, path, body):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with client.stream("POST", path, json=body) as resp:
            assert resp.status_code == 200
            chunks = [line async for line in resp.aiter_lines()]
    return chunks


async def test_openai_chat_stream_emits_deltas_and_done(app):
    chunks = await post_stream(app, "/v1/chat/completions",
                               {"stream": True, "messages": [{"role": "user", "content": "hi"}]})
    data = [c[6:] for c in chunks if c.startswith("data: ") and c != "data: [DONE]"]
    payloads = [json.loads(x) for x in data]
    text = "".join(p["choices"][0]["delta"].get("content", "") for p in payloads)
    assert "hello world" == text

    finish = [p for p in payloads if p["choices"][0].get("finish_reason") == "stop"]
    assert finish and finish[-1]["usage"]["total_tokens"] == 6
    assert "data: [DONE]" in chunks


async def test_native_agent_stream_emits_events_and_result(app):
    chunks = await post_stream(app, "/v1/agent", {"task": "hi", "stream": True, "events": True})
    kinds = [c.split(":", 1)[1].strip() if c.startswith("event:") else c for c in chunks if c]
    assert "delta" in kinds
    joined = "\n".join(chunks)
    assert '"hello "' in joined
    result_line = next(c for c in chunks if c.startswith("data: ") and '"output"' in c)
    result = json.loads(result_line[6:])
    assert result["output"] == "hello world" and result["session_id"] == "sess-test"
    assert "event: done" in chunks


async def test_stream_error_is_reported(app, monkeypatch):
    class BoomAgent(FakeAgent):
        async def run(self, *a, **k):
            raise RuntimeError("kaboom")

    monkeypatch.setattr(server, "Agent", BoomAgent)
    chunks = await post_stream(app, "/v1/chat/completions",
                               {"stream": True, "messages": [{"role": "user", "content": "hi"}]})
    assert any("argo error: RuntimeError: kaboom" in c for c in chunks)
    assert "data: [DONE]" in chunks
