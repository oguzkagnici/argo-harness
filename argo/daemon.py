"""Always-on agent runtime.

A regular harness is request/response: a human starts a turn, the harness ends when
the turn ends. An always-on agent inverts that:

  triggers (schedule / webhook / watch) -> durable goal queue -> prioritised worker
  -> budget + approval guardrails -> persistent memory -> proactive dream cycle

Everything here is crash-safe in SQLite: goals survive restarts, dedupe prevents
trigger storms from fanning out into duplicate work, and risky actions go to an
approval queue instead of silently running.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape

from .config import ArgoConfig
from .core.agent import Agent
from .core.session import Session
from .memory.dream import DreamEngine
from .memory.store import MemoryStore

console = Console()

SCHEMA = """
CREATE TABLE IF NOT EXISTS triggers (
  id TEXT PRIMARY KEY, name TEXT UNIQUE, kind TEXT, spec TEXT, goal TEXT,
  enabled INTEGER DEFAULT 1, last_run REAL DEFAULT 0, next_run REAL DEFAULT 0, created_at REAL
);
CREATE TABLE IF NOT EXISTS goals (
  id TEXT PRIMARY KEY, goal TEXT, source TEXT DEFAULT '', trigger_id TEXT,
  priority INTEGER DEFAULT 5, status TEXT DEFAULT 'pending', dedupe_key TEXT,
  created_at REAL, updated_at REAL, started_at REAL, finished_at REAL,
  session_id TEXT, result TEXT, error TEXT, attempts INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status, priority DESC, created_at);
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY, goal_id TEXT, tool TEXT, args TEXT, reason TEXT,
  status TEXT DEFAULT 'pending', created_at REAL, decided_at REAL, decision TEXT
);
CREATE TABLE IF NOT EXISTS daemon_state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS outbox (
  id TEXT PRIMARY KEY, goal_id TEXT, channel TEXT DEFAULT 'user', message TEXT,
  status TEXT DEFAULT 'pending', created_at REAL, sent_at REAL
);
"""

DANGER_PATTERNS = [
    (r"\brm\s+-rf\s+/(\s|$)", "recursive delete of filesystem root"),
    (r"curl[^|]*\|\s*(ba)?sh", "piping remote content into a shell"),
    (r"wget[^|]*\|\s*(ba)?sh", "piping remote content into a shell"),
    (r"\bsudo\b", "privilege escalation"),
    (r"git\s+push\s+.*--force", "force push"),
    (r"\bDROP\s+(TABLE|DATABASE)\b", "destructive SQL"),
    (r"\btruncate\s+table\b", "destructive SQL"),
    (r"chmod\s+777\s+/", "unsafe permissions on root"),
    (r":\(\)\s*\{.*\};:", "fork bomb"),
    (r"mkfs\.", "filesystem format"),
]


def danger_check(tool: str, args: dict[str, Any]) -> str | None:
    text = ""
    if tool == "bash":
        text = str(args.get("command", ""))
    elif tool == "run_code":
        text = str(args.get("code", ""))
    if not text:
        return None
    for pattern, reason in DANGER_PATTERNS:
        if re.search(pattern, text, re.I | re.S):
            return reason
    return None


class GoalQueue:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        try:  # migrate pre-attempts databases
            self.db.execute("ALTER TABLE goals ADD COLUMN attempts INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.commit()

    # ---------------------------------------------------------------- goals
    def enqueue(self, goal: str, source: str = "manual", trigger_id: str | None = None,
                priority: int = 5, dedupe_key: str | None = None) -> str | None:
        key = dedupe_key or hashlib.sha1((goal.strip().lower() + (trigger_id or "")).encode()).hexdigest()
        existing = self.db.execute("SELECT id,status FROM goals WHERE dedupe_key=? AND status IN "
                                   "('pending','running')", (key,)).fetchone()
        if existing:
            return None
        gid = f"g_{uuid.uuid4().hex[:10]}"
        now = time.time()
        self.db.execute("""INSERT INTO goals(id,goal,source,trigger_id,priority,status,dedupe_key,
                           created_at,updated_at) VALUES(?,?,?,?,?,'pending',?,?,?)""",
                        (gid, goal, source, trigger_id, priority, key, now, now))
        self.db.commit()
        return gid

    def claim_next(self) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM goals WHERE status='pending' ORDER BY priority DESC, created_at ASC "
                              "LIMIT 1").fetchone()
        if not row:
            return None
        now = time.time()
        cur = self.db.execute("UPDATE goals SET status='running', started_at=?, updated_at=? "
                              "WHERE id=? AND status='pending'", (now, now, row["id"]))
        self.db.commit()
        if cur.rowcount == 0:
            return None
        return dict(row)

    def recover_stale(self, stale_after: float = 1800.0, max_attempts: int = 3) -> list[str]:
        """Requeue goals whose worker died mid-run; give up after ``max_attempts``."""
        cutoff = time.time() - stale_after
        rows = self.db.execute("SELECT id,attempts FROM goals WHERE status='running' AND updated_at < ?",
                               (cutoff,)).fetchall()
        recovered = []
        for row in rows:
            attempts = int(row["attempts"] or 0) + 1
            if attempts >= max_attempts:
                self.db.execute("UPDATE goals SET status='failed', attempts=?, error=?, updated_at=? WHERE id=?",
                                (attempts, f"stale claim after {attempts} attempts", time.time(), row["id"]))
            else:
                self.db.execute("UPDATE goals SET status='pending', attempts=?, updated_at=?, "
                                "error='requeued after stale claim' WHERE id=?",
                                (attempts, time.time(), row["id"]))
                recovered.append(row["id"])
        self.db.commit()
        return recovered

    def finish(self, gid: str, result: str = "", error: str = "", session_id: str = "",
               status: str = "done") -> None:
        self.db.execute("UPDATE goals SET status=?,result=?,error=?,session_id=?,finished_at=?,updated_at=? "
                        "WHERE id=?", (status, result[:20000], error[:4000], session_id, time.time(),
                                       time.time(), gid))
        self.db.commit()

    def list_goals(self, limit: int = 50) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM goals ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()]

    # ------------------------------------------------------------- approvals
    def request_approval(self, goal_id: str | None, tool: str, args: dict[str, Any], reason: str) -> str:
        aid = f"ap_{uuid.uuid4().hex[:10]}"
        self.db.execute("INSERT INTO approvals(id,goal_id,tool,args,reason,created_at) VALUES(?,?,?,?,?,?)",
                        (aid, goal_id, tool, json.dumps(args, default=str)[:4000], reason, time.time()))
        self.db.commit()
        return aid

    def pending_approvals(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM approvals WHERE status='pending' ORDER BY created_at").fetchall()]

    def decide(self, approval_id: str, approve: bool) -> None:
        self.db.execute("UPDATE approvals SET status='decided',decision=?,decided_at=? WHERE id=?",
                        ("approve" if approve else "deny", time.time(), approval_id))
        self.db.commit()

    # -------------------------------------------------------------- triggers
    def add_trigger(self, name: str, kind: str, spec: dict[str, Any], goal: str,
                    enabled: bool = True) -> str:
        now = time.time()
        tid = f"tr_{uuid.uuid4().hex[:10]}"
        next_run = self._next_run(kind, spec, now)
        self.db.execute("INSERT OR REPLACE INTO triggers(id,name,kind,spec,goal,enabled,next_run,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (tid, name, kind, json.dumps(spec), goal, int(enabled), next_run, now))
        self.db.commit()
        return tid

    def _next_run(self, kind: str, spec: dict[str, Any], now: float) -> float:
        if kind == "interval":
            return now + max(30, int(spec.get("seconds", 300)))
        if kind == "daily":
            hh, mm = (str(spec.get("at", "09:00")).split(":") + ["0"])[:2]
            t = time.localtime(now)
            target = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, int(hh), int(mm), 0, 0, 0, -1))
            return target if target > now else target + 86400
        if kind == "weekly":
            return now + 7 * 86400
        if kind == "once":
            return now + max(1, int(spec.get("seconds", 60)))
        return 0.0  # webhook / manual

    def due_triggers(self, now: float | None = None) -> list[dict[str, Any]]:
        now = now or time.time()
        rows = self.db.execute("SELECT * FROM triggers WHERE enabled=1 AND kind IN "
                               "('interval','daily','weekly','once') AND next_run>0 AND next_run<=?",
                               (now,)).fetchall()
        return [dict(r) for r in rows]

    def mark_trigger_ran(self, row: dict[str, Any]) -> None:
        spec = json.loads(row["spec"] or "{}")
        if row["kind"] == "once":
            self.db.execute("UPDATE triggers SET last_run=?, next_run=0, enabled=0 WHERE id=?",
                            (time.time(), row["id"]))
        else:
            nxt = self._next_run(row["kind"], spec, time.time())
            self.db.execute("UPDATE triggers SET last_run=?, next_run=? WHERE id=?", (time.time(), nxt, row["id"]))
        self.db.commit()

    def get_trigger(self, trigger_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM triggers WHERE id=?", (trigger_id,)).fetchone()
        return dict(row) if row else None

    def remove_trigger(self, name: str) -> bool:
        cur = self.db.execute("DELETE FROM triggers WHERE name=?", (name,))
        self.db.commit()
        return cur.rowcount > 0

    # ---------------------------------------------------------------- outbox
    def send(self, goal_id: str | None, message: str, channel: str = "user") -> str:
        oid = f"out_{uuid.uuid4().hex[:10]}"
        self.db.execute("INSERT INTO outbox(id,goal_id,channel,message,created_at) VALUES(?,?,?,?,?)",
                        (oid, goal_id, channel, message[:20000], time.time()))
        self.db.commit()
        return oid

    def outbox(self, status: str = "pending", limit: int = 50) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM outbox WHERE status=? ORDER BY created_at LIMIT ?", (status, limit)).fetchall()]

    def mark_sent(self, outbox_id: str) -> None:
        self.db.execute("UPDATE outbox SET status='sent', sent_at=? WHERE id=?", (time.time(), outbox_id))
        self.db.commit()

    def webhook_trigger(self, name: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM triggers WHERE name=? AND enabled=1", (name,)).fetchone()
        return dict(row) if row else None

    def triggers(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT * FROM triggers ORDER BY created_at").fetchall()]

    # ------------------------------------------------------------------ state
    def state(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM daemon_state WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_state(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO daemon_state(key,value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.db.commit()


class Daemon:
    def __init__(self, cfg: ArgoConfig, once: bool = False, poll_seconds: int = 30,
                 dream_every_hours: float = 24.0, stale_claim_seconds: float = 1800.0):
        self.cfg = cfg
        self.once = once
        self.poll_seconds = max(5, poll_seconds)
        self.stale_claim_seconds = stale_claim_seconds
        self.dream_every = dream_every_hours
        self.queue = GoalQueue(Path(cfg.data_dir) / "daemon.sqlite")
        self.running = True

    async def run(self) -> None:
        console.print(f"[bold]Argo daemon[/] pid={__import__('os').getpid()} workspace={self.cfg.workspace}")
        console.print(f"[dim]queue: {len(self.queue.list_goals())} goals · "
                      f"triggers: {len(self.queue.triggers())} · poll {self.poll_seconds}s[/]")
        while self.running:
            try:
                self._enqueue_due()
                recovered = self.queue.recover_stale(self.stale_claim_seconds)
                if recovered:
                    console.print(f"[yellow]requeued {len(recovered)} stale goal(s)[/]")
                goal = self.queue.claim_next()
                if goal:
                    await self._execute(goal)
                elif self._dream_due():
                    await self._dream()
                self._heartbeat()
                if self.once:
                    break
                await asyncio.sleep(self.poll_seconds)
            except KeyboardInterrupt:
                break
            except Exception as exc:  # noqa: BLE001
                console.print(f"[red]daemon error:[/] {type(exc).__name__}: {exc}")
                if self.once:
                    break
                await asyncio.sleep(self.poll_seconds)

    def stop(self) -> None:
        self.running = False

    def _enqueue_due(self) -> None:
        for tr in self.queue.due_triggers():
            gid = self.queue.enqueue(tr["goal"], source=f"trigger:{tr['name']}", trigger_id=tr["id"],
                                     dedupe_key=f"{tr['id']}:{int(time.time() // 60)}")
            if gid:
                console.print(f"[cyan]trigger[/] {tr['name']} → {tr['goal'][:80]}")
            self.queue.mark_trigger_ran(tr)

    def _heartbeat(self) -> None:
        self.queue.set_state("heartbeat", json.dumps({"ts": time.time(), "pid": __import__("os").getpid()}))

    def _dream_due(self) -> bool:
        last = float(self.queue.state("last_dream", "0") or 0)
        return (time.time() - last) > self.dream_every * 3600

    async def _dream(self) -> None:
        console.print("[magenta]◇ dream cycle[/]")
        async with Agent(self.cfg) as agent:
            if agent.memory is None:
                return
            mem_count = agent.memory.stats()["transcripts"]
            if mem_count == 0:
                self.queue.set_state("last_dream", str(time.time()))
                return
            report = await DreamEngine(agent.memory, agent.llm, self.cfg).run(apply=True, max_sessions=50)
            console.print(f"[magenta]◇ dream[/] {report.status}: +{report.added} notes, "
                          f"{report.merged} merged, {report.wiki} wiki, {report.insights} insights")
        self.queue.set_state("last_dream", str(time.time()))

    def _approvals(self, goal_id: str):
        def approve(tool, args, reason):
            danger = danger_check(tool.name, args)
            if danger:
                aid = self.queue.request_approval(goal_id, tool.name, args, danger)
                self.queue.send(goal_id, f"Approval needed ({aid}): `{tool.name}` was blocked: {danger}",
                                channel="approval")
                console.print(f"[yellow]approval required[/] {tool.name}: {danger} ({aid})")
                return False
            return True
        return approve

    async def _execute(self, goal: dict[str, Any]) -> None:
        console.print(f"\n[bold]goal[/] {goal['goal'][:160]}\n[dim]id={goal['id']} source={goal['source']}[/]")
        sess = Session.create(self.cfg, session_id=None)
        allow: set[str] | None = None
        if goal.get("trigger_id"):
            tr = self.queue.get_trigger(goal["trigger_id"])
            spec = json.loads((tr or {}).get("spec") or "{}")
            if spec.get("read_only"):
                allow = {"read_file", "list_dir", "glob_files", "grep_files", "web_search", "fetch_url",
                         "wikipedia_search", "arxiv_search", "read_document", "recall", "memory_stats",
                         "update_plan", "which", "list_skills", "read_skill", "list_workflows", "show_workflow"}
        agent = Agent(self.cfg, approvals=self._approvals(goal["id"]), max_iterations=self.cfg.max_iterations,
                      allow_tools=allow,
                      extra={"daemon_queue": self.queue, "goal_id": goal["id"]})
        agent.events.subscribe(lambda ev: None)
        started = time.time()
        try:
            async with agent:
                result = await agent.run(goal["goal"], session=sess)
            self.queue.finish(goal["id"], result=result.output, session_id=result.session_id,
                              status="done" if result.stop_reason == "final" else result.stop_reason)
            if result.output and result.output != "(no final answer)":
                self.queue.send(goal["id"], result.output)
            console.print(f"[green]done[/] [dim]{escape(result.brief())}[/] [{time.time() - started:.0f}s]")
        except Exception as exc:  # noqa: BLE001
            self.queue.finish(goal["id"], error=f"{type(exc).__name__}: {exc}", status="failed")
            console.print(f"[red]failed[/] {exc}")
        finally:
            await agent.close()
