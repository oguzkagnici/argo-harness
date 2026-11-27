"""Served-run approvals: a destructive tool blocks until a decision arrives (no model, no network)."""
from __future__ import annotations

import asyncio

import pytest

httpx = pytest.importorskip("httpx")
from argo.config import ArgoConfig  # noqa: E402
from argo.server import create_app  # noqa: E402


class FakeTool:
    def __init__(self, name: str, read_only: bool = False):
        self.name = name
        self.read_only = read_only


def make_app(tmp_path, **kwargs):
    cfg = ArgoConfig(workspace=tmp_path, data_dir=tmp_path / "data", session_dir=tmp_path / "sessions",
                     index_dir=tmp_path / "i", workflow_dir=tmp_path / "w", skill_dir=tmp_path / "k",
                     spool_dir=tmp_path / "p")
    cfg.ensure_dirs()
    return create_app(cfg, **kwargs)


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_destructive_tool_blocks_until_approved(tmp_path):
    app = make_app(tmp_path, approval_timeout=5.0)
    gate = app.state.approval_gate
    async with client_for(app) as client:
        task = asyncio.create_task(gate(FakeTool("bash"), {"command": "rm -rf /"}, "mutating tool"))
        await asyncio.sleep(0.05)
        pending = (await client.get("/v1/approvals")).json()["approvals"]
        assert len(pending) == 1 and pending[0]["source"] == "serve"
        assert pending[0]["tool"] == "bash" and "rm -rf" in pending[0]["args"]["command"]
        assert not task.done()
        decided = await client.post(f"/v1/approvals/{pending[0]['id']}", json={"approve": True})
        assert decided.json()["approved"] is True
        assert await asyncio.wait_for(task, timeout=1) is True
        assert (await client.get("/v1/approvals")).json()["approvals"] == []


@pytest.mark.asyncio
async def test_denial_returns_false_and_timeout_denies(tmp_path):
    app = make_app(tmp_path, approval_timeout=0.15)
    gate = app.state.approval_gate
    async with client_for(app) as client:
        first = asyncio.create_task(gate(FakeTool("bash"), {"command": "sudo rm -rf /"}, "mutating tool"))
        second = asyncio.create_task(gate(FakeTool("bash"), {"command": "mkfs.ext4 /dev/sda"}, "mutating tool"))
        await asyncio.sleep(0.05)
        pending = (await client.get("/v1/approvals")).json()["approvals"]
        assert len(pending) == 2
        await client.post(f"/v1/approvals/{pending[0]['id']}", json={"approve": False})
        assert await asyncio.wait_for(first, timeout=1) is False
        # the second one is never decided -> denied when the timeout expires
        assert await asyncio.wait_for(second, timeout=2) is False


@pytest.mark.asyncio
async def test_policy_levels_and_benign_writes(tmp_path):
    app = make_app(tmp_path)
    gate = app.state.approval_gate
    assert await gate(FakeTool("write_file"), {"path": "a.txt", "content": "hi"}, "mutating tool") is True

    strict = make_app(tmp_path, approvals="all", approval_timeout=0.1)
    assert await asyncio.wait_for(strict.state.approval_gate(
        FakeTool("write_file"), {"path": "a.txt", "content": "hi"}, "mutating tool"), timeout=2) is False

    off = make_app(tmp_path, approvals="off")
    assert off.state.approval_gate is None


@pytest.mark.asyncio
async def test_serve_requires_key_for_approvals(tmp_path):
    app = make_app(tmp_path, require_key="secret")
    async with client_for(app) as client:
        assert (await client.get("/v1/approvals")).status_code == 401
        ok = await client.get("/v1/approvals", headers={"Authorization": "Bearer secret"})
        assert ok.status_code == 200 and "approvals" in ok.json()


@pytest.mark.asyncio
async def test_served_agent_receives_the_gate(tmp_path, monkeypatch):
    import argo.server as server_mod

    captured: dict = {}

    class FakeUsage:
        def as_dict(self):
            return {"total_tokens": 1}

    class FakeResult:
        output = "ok"
        session_id = "s1"
        stop_reason = "final"
        iterations = 1
        usage = FakeUsage()
        files: list = []
        plan: list = []

    class StubAgent:
        def __init__(self, cfg, **kwargs):
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def run(self, task, session=None, stream=False):
            return FakeResult()

        async def close(self):
            pass

    monkeypatch.setattr(server_mod, "Agent", StubAgent)
    app = make_app(tmp_path)
    async with client_for(app) as client:
        r = await client.post("/v1/agent", json={"task": "do it"})
        assert r.status_code == 200 and r.json()["output"] == "ok"
    assert captured.get("approvals") is app.state.approval_gate

    off = make_app(tmp_path, approvals="off")
    captured.clear()
    async with client_for(off) as client:
        await client.post("/v1/agent", json={"task": "do it"})
    assert captured.get("approvals") is None
