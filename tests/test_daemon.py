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


def _age_goal(queue, gid: str, seconds: float = 9000, heartbeat: bool = True) -> None:
    import time
    old = time.time() - seconds
    if heartbeat:
        queue.db.execute("UPDATE goals SET updated_at=?, heartbeat_at=? WHERE id=?", (old, old, gid))
    else:
        queue.db.execute("UPDATE goals SET updated_at=? WHERE id=?", (old, gid))
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


def test_two_workers_claim_each_goal_exactly_once(tmp_path):
    from argo.daemon import GoalQueue
    db = tmp_path / "q.sqlite"
    q1, q2 = GoalQueue(db), GoalQueue(db)
    ids = {q1.enqueue(f"goal {i}") for i in range(20)}

    seen = []
    while True:
        got = [q1.claim_next("w1") or q2.claim_next("w2")]
        got = [g for g in got if g]
        if not got:
            break
        seen.extend(g["id"] for g in got)

    assert len(seen) == 20 and set(seen) == ids
    assert all(g["status"] == "running" for g in q1.list_goals())


def test_live_lease_is_not_recovered(tmp_path):
    d = Daemon(make_cfg(tmp_path), stale_claim_seconds=60)
    gid = d.queue.enqueue("slow but alive")
    d.queue.claim_next("worker-a")
    _age_goal(d.queue, gid, seconds=3600, heartbeat=False)  # updated_at old...

    # ...but a heartbeat keeps the lease fresh.
    assert d.queue.heartbeat(gid, "worker-a") is True
    assert d.queue.recover_stale(d.stale_claim_seconds) == []
    assert d.queue.list_goals()[0]["status"] == "running"


def test_crashed_worker_lease_is_recovered(tmp_path):
    d = Daemon(make_cfg(tmp_path), stale_claim_seconds=60)
    gid = d.queue.enqueue("worker died")
    d.queue.claim_next("worker-a")
    _age_goal(d.queue, gid)  # heartbeat also stale -> worker presumed dead

    assert d.queue.recover_stale(d.stale_claim_seconds) == [gid]
    assert d.queue.list_goals()[0]["status"] == "pending"


def test_stolen_goal_heartbeat_is_rejected(tmp_path):
    d = Daemon(make_cfg(tmp_path), stale_claim_seconds=60)
    gid = d.queue.enqueue("contested")
    d.queue.claim_next("worker-a")
    assert d.queue.heartbeat(gid, "worker-a") is True
    # A worker that does not own the lease cannot renew it.
    assert d.queue.heartbeat(gid, "worker-b") is False


async def test_execute_renews_lease_while_running(tmp_path, monkeypatch):
    import asyncio

    class SlowAgent(CaptureAgent):
        async def run(self, task, session=None, **kwargs):
            await asyncio.sleep(0.3)
            return await super().run(task, session=session, **kwargs)

    monkeypatch.setattr(daemon_mod, "Agent", SlowAgent)
    d = Daemon(make_cfg(tmp_path), stale_claim_seconds=3)  # renews ~1s
    gid = d.queue.enqueue("long task")
    goal = d.queue.claim_next(d.worker_id)
    exec_task = asyncio.create_task(d._execute(goal))
    await asyncio.sleep(0.15)
    first = d.queue.db.execute("SELECT heartbeat_at FROM goals WHERE id=?", (gid,)).fetchone()[0]
    await exec_task
    stored = d.queue.list_goals()[0]
    assert stored["status"] == "done"
    assert first is not None
