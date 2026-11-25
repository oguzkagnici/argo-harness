"""Session persistence: JSONL event log + lightweight state, plus transcript mirroring."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .types import Message, ToolCall, Usage


@dataclass
class Session:
    id: str
    path: Path
    cfg: Any
    memory: Any = None
    created_at: float = field(default_factory=time.time)
    plan: list[dict[str, str]] = field(default_factory=list)
    plan_approved: bool = False
    approved_actions: list[str] = field(default_factory=list)
    files_touched: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, cfg: Any, memory: Any = None, session_id: str | None = None) -> "Session":
        sid = session_id or time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        path = Path(cfg.session_dir) / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        sess = cls(id=sid, path=path, cfg=cfg, memory=memory)
        sess.record("session_start", created_at=sess.created_at, workspace=str(cfg.workspace))
        return sess

    @classmethod
    def load(cls, cfg: Any, session_id: str, memory: Any = None) -> "Session":
        path = Path(cfg.session_dir) / f"{session_id}.jsonl"
        sess = cls(id=session_id, path=path, cfg=cfg, memory=memory)
        if path.exists():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "plan":
                    sess.plan = ev.get("steps", [])
                elif ev.get("type") == "plan_approved":
                    sess.plan_approved = True
                    sess.approved_actions = list(ev.get("actions", []) or [])
                elif ev.get("type") == "plan_revoked":
                    sess.plan_approved = False
                    sess.approved_actions = []
                elif ev.get("type") == "usage":
                    sess.usage.prompt_tokens += ev.get("prompt", 0)
                    sess.usage.completion_tokens += ev.get("completion", 0)
                    sess.usage.total_tokens += ev.get("total", 0)
        return sess

    def record(self, type: str, **data: Any) -> None:
        entry = {"ts": time.time(), "type": type, **data}
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass

    def log_message(self, msg: Message) -> None:
        self.record("message", role=msg.role, content=(msg.content or "")[:20000],
                    reasoning=(msg.reasoning or "")[:8000] if msg.reasoning else "",
                    tool_calls=[{"name": t.name, "arguments": t.arguments[:2000]} for t in msg.tool_calls],
                    tool_call_id=msg.tool_call_id, name=msg.name)
        if self.memory is not None and msg.role in ("user", "assistant") and msg.content:
            role = "assistant" if msg.role == "assistant" or msg.tool_calls else "user"
            self.memory.log(self.id, role, msg.content,
                            {"tool_calls": [t.name for t in msg.tool_calls]} if msg.tool_calls else None)

    def log_tool(self, name: str, args: dict[str, Any], result: str, is_error: bool,
                 duration_s: float | None = None) -> None:
        self.record("tool", name=name, args=args, result=result[:12000], is_error=is_error,
                    duration_s=duration_s)
        if self.memory is not None:
            self.memory.log(self.id, "tool", f"{name}({json.dumps(args, default=str)[:800]}) -> "
                                            f"{'ERROR: ' if is_error else ''}{result[:2000]}",
                            {"tool": name, "is_error": is_error})

    def set_plan(self, steps: list[dict[str, str]]) -> None:
        self.plan = steps
        self.record("plan", steps=steps)

    def approve_plan(self, steps: list[str], actions: list[str] | None = None) -> None:
        """Persist a batch plan approval so a resumed session keeps write access."""
        self.plan_approved = True
        self.approved_actions = [str(a) for a in (actions or [])]
        self.set_plan([{"step": str(s), "status": "pending"} for s in steps])
        self.record("plan_approved", steps=[str(s) for s in steps], actions=self.approved_actions)

    def revoke_plan(self) -> None:
        self.plan_approved = False
        self.approved_actions = []
        self.record("plan_revoked")

    def add_usage(self, usage: Usage) -> None:
        self.usage.add(usage)
        self.record("usage", **usage.as_dict())

    def touch_file(self, path: str) -> None:
        if path not in self.files_touched:
            self.files_touched.append(path)

    def events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return out

    def transcript_messages(self) -> list[Message]:
        out = []
        for ev in self.events():
            if ev.get("type") == "message":
                out.append(Message(role=ev.get("role", "assistant"), content=ev.get("content"),
                                   reasoning=ev.get("reasoning") or None,
                                   name=ev.get("name"), tool_call_id=ev.get("tool_call_id"),
                                   tool_calls=[ToolCall(id="", name=t["name"], arguments=t.get("arguments", "{}"))
                                               for t in ev.get("tool_calls", [])]))
        return out


def list_sessions(cfg: Any, limit: int = 30) -> list[dict[str, Any]]:
    d = Path(cfg.session_dir)
    if not d.exists():
        return []
    rows = []
    for p in sorted(d.glob("*.jsonl"), key=lambda x: -x.stat().st_mtime)[:limit]:
        try:
            first = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
        except (OSError, IndexError, json.JSONDecodeError):
            first = {}
        rows.append({"id": p.stem, "mtime": p.stat().st_mtime, "workspace": first.get("workspace", "")})
    return rows
