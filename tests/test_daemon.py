"""Daemon tests: dangerous-action approvals, outbox delivery, read-only trigger allowlist."""
from __future__ import annotations

import json

import pytest

import argo.daemon as daemon_mod
from argo.config import ArgoConfig
from argo.core.agent import RunResult
from argo.core.events import EventBus
from argo.core.types import Usage
from argo.daemon import Daemon


def make_cfg(tmp_path) -> ArgoConfig:
    cfg = ArgoConfig(workspace=tmp_path, data_dir=tmp_path / "data", session_dir=tmp_path / "sessions",
                     index_dir=tmp_path / "index", workflow_dir=tmp_path / "wf", skill_dir=tmp_path / "skills",
                     spool_dir=tmp_path / "spool")
    cfg.ensure_dirs()
    return cfg


class Tool:
    def __init__(self, name):
        self.name = name


def test_approvals_queue_dangerous_actions_and_notify(tmp_path):
    d = Daemon(make_cfg(tmp_path))
    approve = d._approvals("g1")

    assert approve(Tool("bash"), {"command": "ls -la"}, "") is True
    assert approve(Tool("bash"), {"command": "rm -rf /"}, "") is False

    pending = d.queue.pending_approvals()
    assert len(pending) == 1 and pending[0]["tool"] == "bash"
    notes = d.queue.outbox()
    assert any("Approval needed" in n["message"] and n["channel"] == "approval" for n in notes)

    d.queue.decide(pending[0]["id"], True)
    assert not d.queue.pending_approvals()


class CaptureAgent:
    last: "CaptureAgent | None" = None

    def __init__(self, cfg, **kwargs):
        self.cfg = cfg
        self.kwargs = kwargs
        self.events = EventBus()
        self.memory = None
        CaptureAgent.last = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def close(self):
        return None

    async def run(self, task, session=None, **kwargs):
        return RunResult(output="quarterly report ready", session_id="sess-1", iterations=2,
                         usage=Usage(prompt_tokens=3, completion_tokens=4, total_tokens=7))


async def test_execute_finishes_goal_and_delivers_outbox(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_mod, "Agent", CaptureAgent)
    d = Daemon(make_cfg(tmp_path))
    d.queue.enqueue("write the quarterly report")
    goal = d.queue.claim_next()
    await d._execute(goal)

    stored = d.queue.list_goals()[0]
    assert stored["status"] == "done" and stored["result"] == "quarterly report ready"
    out = d.queue.outbox()
    assert len(out) == 1 and out[0]["message"] == "quarterly report ready"
    d.queue.mark_sent(out[0]["id"])
    assert not d.queue.outbox()


async def test_read_only_trigger_restricts_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_mod, "Agent", CaptureAgent)
    d = Daemon(make_cfg(tmp_path))
    tid = d.queue.add_trigger("daily", "interval", {"seconds": 60, "read_only": True}, "summarize inbox")
    gid = d.queue.enqueue("summarize inbox", source="trigger:daily", trigger_id=tid)
    goal = next(g for g in d.queue.list_goals() if g["id"] == gid)
    await d._execute(goal)

    allow = CaptureAgent.last.kwargs["allow_tools"]
    assert "read_file" in allow and "web_search" in allow
    assert "bash" not in allow and "write_file" not in allow and "run_code" not in allow
    assert CaptureAgent.last.kwargs["extra"]["daemon_queue"] is d.queue

    # A manual goal (no trigger) keeps the full toolset.
    d.queue.enqueue("manual task")
    await d._execute(d.queue.claim_next())
    assert CaptureAgent.last.kwargs["allow_tools"] is None


def _age_goal(queue, gid: str, seconds: float = 9000) -> None:
    queue.db.execute("UPDATE goals SET updated_at=? WHERE id=?", (__import__("time").time() - seconds, gid))
    queue.db.commit()


def test_recover_stale_requeues_running_goal(tmp_path):
    d = Daemon(make_cfg(tmp_path), stale_claim_seconds=60)
    gid = d.queue.enqueue("long research job")
    d.queue.claim_next()  # worker claims, then dies
    _age_goal(d.queue, gid)

    assert d.queue.recover_stale(d.stale_claim_seconds) == [gid]
    goal = d.queue.list_goals()[0]
    assert goal["status"] == "pending" and goal["attempts"] == 1
    assert "requeued after stale claim" in goal["error"]


def test_recover_stale_fails_after_max_attempts(tmp_path):
    d = Daemon(make_cfg(tmp_path), stale_claim_seconds=60)
    gid = d.queue.enqueue("poison job")
    for _ in range(3):
        d.queue.claim_next()
        _age_goal(d.queue, gid)
        d.queue.recover_stale(d.stale_claim_seconds, max_attempts=3)

    goal = d.queue.list_goals()[0]
    assert goal["status"] == "failed" and goal["attempts"] == 3
    assert "stale claim after 3 attempts" in goal["error"]
    assert d.queue.claim_next() is None


def test_recover_stale_leaves_fresh_and_pending_alone(tmp_path):
    d = Daemon(make_cfg(tmp_path), stale_claim_seconds=60)
    fresh = d.queue.enqueue("fresh job")
    d.queue.claim_next()
    pending = d.queue.enqueue("queued job")

    assert d.queue.recover_stale(d.stale_claim_seconds) == []
    status = {g["id"]: g["status"] for g in d.queue.list_goals()}
    assert status[fresh] == "running" and status[pending] == "pending"


def test_dedupe_survives_stale_requeue(tmp_path):
    d = Daemon(make_cfg(tmp_path))
    gid = d.queue.enqueue("same goal")
    d.queue.claim_next()
    _age_goal(d.queue, gid)
    d.queue.recover_stale(60)

    assert d.queue.enqueue("same goal") is None  # pending duplicate still rejected
