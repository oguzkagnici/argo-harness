"""Declarative, parameterized workflows extracted from successful agent traces.

A workflow is a small program over the tool API:

  {"name": ..., "description": ..., "params": [{"name","description","required","default"}],
   "steps": [{"tool": "...", "args": {... "{{param}}"} , "save_as": "var",
              "expect_contains": "...", "expect_exit_zero": true,
              "retries": 1, "on_error": "stop"|"continue"}]}

This is deliberately *not* raw code: it is inspectable, diffable, permission-checked,
and replays through the same ToolRegistry (so policy and truncation still apply).
That mirrors Codex/Claude Code's preference for structured tool actions over
free-form script replay.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

VAR_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}")


@dataclass
class WorkflowStep:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    save_as: str = ""
    expect_contains: str = ""
    expect_exit_zero: bool = False
    retries: int = 0
    on_error: str = "stop"          # stop | continue


@dataclass
class Workflow:
    name: str
    description: str = ""
    params: list[dict[str, Any]] = field(default_factory=list)
    steps: list[WorkflowStep] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    created_from: str = ""
    notes: str = ""
    id: str = field(default_factory=lambda: f"wf_{uuid.uuid4().hex[:8]}")

    def param_defaults(self) -> dict[str, Any]:
        out = {}
        for p in self.params:
            if p.get("default") is not None:
                out[p["name"]] = p["default"]
        return out

    def required_params(self) -> list[str]:
        return [p["name"] for p in self.params if p.get("required", True)]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "params": self.params,
                "steps": [asdict(s) for s in self.steps], "created_at": self.created_at,
                "created_from": self.created_from, "notes": self.notes, "id": self.id}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Workflow":
        steps = [WorkflowStep(**{k: v for k, v in s.items() if k in WorkflowStep.__dataclass_fields__})
                 for s in d.get("steps", [])]
        return cls(name=d.get("name", "workflow"), description=d.get("description", ""),
                   params=d.get("params", []), steps=steps,
                   created_at=d.get("created_at", time.time()), created_from=d.get("created_from", ""),
                   notes=d.get("notes", ""), id=d.get("id") or f"wf_{uuid.uuid4().hex[:8]}")

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.name}.json"
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        return path


def load_workflow(directory: Path, name: str) -> Workflow | None:
    path = directory / f"{name}.json"
    if not path.exists():
        for p in directory.glob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if d.get("name") == name:
                return Workflow.from_dict(d)
        return None
    return Workflow.from_dict(json.loads(path.read_text(encoding="utf-8")))


def list_workflows(directory: Path) -> list[Workflow]:
    out = []
    if not directory.exists():
        return out
    for p in sorted(directory.glob("*.json")):
        try:
            out.append(Workflow.from_dict(json.loads(p.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _substitute(value: Any, variables: dict[str, Any]) -> Any:
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            key = m.group(1)
            if key in variables:
                v = variables[key]
                return v if isinstance(v, str) else json.dumps(v)
            return m.group(0)
        whole = VAR_RE.fullmatch(value.strip())
        if whole and whole.group(1) in variables:
            return variables[whole.group(1)]
        return VAR_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _substitute(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, variables) for v in value]
    return value


@dataclass
class StepResult:
    index: int
    tool: str
    ok: bool
    output: str
    attempts: int = 1
    skipped: bool = False


@dataclass
class WorkflowRun:
    name: str
    ok: bool
    steps: list[StepResult]
    variables: dict[str, Any]
    duration_s: float
    error: str = ""

    def summary(self, max_chars: int = 6000) -> str:
        lines = [f"Workflow '{self.name}': {'SUCCESS' if self.ok else 'FAILED'} "
                 f"({sum(s.ok for s in self.steps)}/{len(self.steps)} steps ok, {self.duration_s:.1f}s)"]
        if self.error:
            lines.append(f"Error: {self.error}")
        for s in self.steps:
            mark = "skipped" if s.skipped else ("ok" if s.ok else "FAIL")
            lines.append(f"\n[{s.index + 1}] {s.tool} — {mark}" + (f" (attempts={s.attempts})" if s.attempts > 1 else ""))
            body = s.output.strip()
            if body:
                if len(body) > 700:
                    body = body[:700] + "…"
                lines.append("    " + body.replace("\n", "\n    "))
        out = "\n".join(lines)
        return out[:max_chars]


async def run_workflow(wf: Workflow, params: dict[str, Any], registry: Any,
                       variables: dict[str, Any] | None = None) -> WorkflowRun:
    started = time.monotonic()
    variables = {**wf.param_defaults(), **(variables or {}), **params}
    missing = [p for p in wf.required_params() if p not in variables or variables[p] in (None, "")]
    if missing:
        return WorkflowRun(wf.name, False, [], variables, 0.0, f"missing required params: {', '.join(missing)}")
    results: list[StepResult] = []
    ok = True
    error = ""
    for i, step in enumerate(wf.steps):
        args = _substitute(step.args, variables)
        attempt = 0
        last = None
        step_ok = False
        while attempt <= max(0, step.retries):
            attempt += 1
            res = await registry.execute(step.tool, args)
            last = res
            text = res.content or ""
            good = not res.is_error
            if good and step.expect_contains:
                good = step.expect_contains.lower() in text.lower()
            if good and step.expect_exit_zero:
                good = "exit_code: 0" in text
            if good:
                step_ok = True
                break
            if attempt <= step.retries:
                import asyncio
                await asyncio.sleep(min(2 ** (attempt - 1), 8))
        output = last.content if last else "(no result)"
        results.append(StepResult(i, step.tool, step_ok, output, attempt))
        if step.save_as and last is not None:
            variables[step.save_as] = last.content
            for meta_key, meta_val in last.metadata.items():
                if isinstance(meta_val, (str, int, float)):
                    variables[f"{step.save_as}.{meta_key}"] = meta_val
        if not step_ok:
            ok = False
            if step.on_error != "continue":
                error = f"step {i + 1} ({step.tool}) failed"
                break
            results[-1].skipped = False
    return WorkflowRun(wf.name, ok, results, variables, time.monotonic() - started, error)
