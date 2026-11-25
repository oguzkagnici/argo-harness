"""Workflow tools exposed to the agent."""
from __future__ import annotations

from pathlib import Path

from ..core.types import ToolResult
from .core import Workflow, list_workflows, load_workflow, run_workflow
from .synth import workflow_to_skill


async def _run(ctx, name: str, params: dict | None = None) -> ToolResult:
    wf = load_workflow(Path(ctx.cfg.workflow_dir), name)
    if not wf:
        names = [w.name for w in list_workflows(Path(ctx.cfg.workflow_dir))]
        return ToolResult(f"Workflow '{name}' not found. Available: {', '.join(names) or 'none'}", is_error=True)
    if ctx.cfg is None:
        return ToolResult("No config", is_error=True)
    registry = ctx.extra.get("registry")
    if registry is None:
        return ToolResult("Workflow runner unavailable (no registry).", is_error=True)
    run = await run_workflow(wf, params or {}, registry)
    return ToolResult(run.summary(), is_error=not run.ok,
                      metadata={"workflow": wf.name, "steps": len(run.steps), "ok": run.ok})


def _list(ctx) -> ToolResult:
    wfs = list_workflows(Path(ctx.cfg.workflow_dir))
    if not wfs:
        return ToolResult("No saved workflows. Use `argo workflow extract` on a successful session.")
    lines = ["Saved workflows:"]
    for w in wfs:
        lines.append(f"- {w.name}: {w.description or '(no description)'}  [{len(w.steps)} steps]")
    return ToolResult("\n".join(lines))


def _show(ctx, name: str) -> ToolResult:
    wf = load_workflow(Path(ctx.cfg.workflow_dir), name)
    if not wf:
        return ToolResult(f"Workflow '{name}' not found.", is_error=True)
    import json
    return ToolResult(json.dumps(wf.to_dict(), indent=2, ensure_ascii=False))


def _save(ctx, name: str, description: str, steps: list[dict], params: list[dict] | None = None,
          notes: str = "") -> ToolResult:
    valid = set(ctx.extra["registry"].tools) if ctx.extra.get("registry") else set()
    for s in steps:
        if s.get("tool") not in valid:
            return ToolResult(f"Unknown tool in step: {s.get('tool')}", is_error=True)
    wf = Workflow.from_dict({"name": name, "description": description, "params": params or [],
                             "steps": steps, "notes": notes, "created_from": "agent"})
    path = wf.save(Path(ctx.cfg.workflow_dir))
    skill_dir = Path(ctx.cfg.skill_dir) / wf.name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(workflow_to_skill(wf), encoding="utf-8")
    return ToolResult(f"Saved workflow {path} and skill {skill_dir / 'SKILL.md'}", metadata={"path": str(path)})


def register(reg) -> None:
    def params(props: dict, required: list[str] | None = None) -> dict:
        return {"type": "object", "properties": props, "required": required or []}

    reg.add("run_workflow",
            "Replay a saved parameterized workflow (a vetted multi-step procedure). Returns a step-by-step log.",
            params({"name": {"type": "string"}, "params": {"type": "object", "additionalProperties": True}}),
            _run, read_only=False, timeout=900)
    reg.add("list_workflows", "List saved workflows (reusable automations).",
            params({}), _list, read_only=True)
    reg.add("show_workflow", "Show a workflow's JSON definition.",
            params({"name": {"type": "string"}}, ["name"]), _show, read_only=True)
    reg.add("save_workflow",
            "Save a new parameterized workflow and matching skill from explicit steps. Use after discovering a "
            "repeatable procedure (or instead of letting the user record it).",
            params({"name": {"type": "string"}, "description": {"type": "string"},
                    "steps": {"type": "array", "items": {"type": "object"}},
                    "params": {"type": "array", "items": {"type": "object"}},
                    "notes": {"type": "string"}}, ["name", "description", "steps"]),
            _save, read_only=False)
