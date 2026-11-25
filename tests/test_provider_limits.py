"""Provider self-contention guard: concurrency cap, jittered backoff, 429 retry through the real client."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import argo.llm.client as client_mod
from argo.config import load_config
from argo.core.types import Message
from argo.llm.client import LLMClient, backoff_delay, provider_concurrency


def cfg(tmp_path):
    c = load_config({"workspace": str(tmp_path), "data_dir": str(tmp_path / "d")})
    c.provider.wire_api = "chat"  # parity with the /chat/completions path under test
    return c


def test_backoff_honours_retry_after_and_caps():
    assert backoff_delay(0, "2") == 2.0
    assert backoff_delay(0, "999") == 60.0
    assert 0.75 <= backoff_delay(0) <= 1.25
    assert backoff_delay(20) <= 60.0
    # jitter: repeated samples differ
    assert len({round(backoff_delay(1), 3) for _ in range(8)}) > 1


def test_provider_concurrency_env(monkeypatch):
    monkeypatch.setenv("ARGO_MAX_CONCURRENCY", "3")
    assert provider_concurrency() == 3
    monkeypatch.setenv("ARGO_MAX_CONCURRENCY", "nope")
    assert provider_concurrency() == 2
    monkeypatch.setenv("ARGO_MAX_CONCURRENCY", "0")
    assert provider_concurrency() == 1


async def test_limiter_caps_in_flight_requests(monkeypatch):
    monkeypatch.setenv("ARGO_MAX_CONCURRENCY", "2")
    peak, current = 0, 0

    async def worker():
        nonlocal peak, current
        async with client_mod._limiter():
            current += 1
            peak = max(peak, current)
            await asyncio.sleep(0.02)
            current -= 1

    await asyncio.gather(*(worker() for _ in range(10)))
    assert peak <= 2


async def test_chat_retries_429_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGO_MAX_CONCURRENCY", "2")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": "slow down"})
        return httpx.Response(200, json={
            "model": "m", "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}})

    async with LLMClient(cfg(tmp_path), client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as llm:
        resp = await llm.chat([Message(role="user", content="hi")], tools=None)

    assert calls["n"] == 3 and resp.message.content == "ok"


async def test_chat_raises_after_exhausting_retries(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGO_MAX_CONCURRENCY", "2")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, headers={"retry-after": "0"}, json={"error": "always"})

    async with LLMClient(cfg(tmp_path), client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as llm:
        with pytest.raises(Exception) as exc:
            await llm.chat([Message(role="user", content="hi")], tools=None)

    assert calls["n"] == 6 and "429" in str(exc.value)
