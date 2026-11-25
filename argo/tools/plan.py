"""Task plan tool: a durable checklist that survives compaction."""
from __future__ import annotations

import inspect
from dataclasses import dataclass

from ..core.types import ToolResult
from .base import ToolContext, ToolRegistry

ICON = {"pending": "☐", "in_progress": "◐", "completed": "☑", "blocked": "⊘"}


def _render(steps: list[dict]) -> str:
    lines = []
    for i, s in enumerate(steps, 1):
        status = s.get("status", "pending")
        lines.append(f"{i}. {ICON.get(status, '☐')} [{status}] {s.get('step', '')}"
                     + (f"  — {s['notes']}" if s.get("notes") else ""))
    return "\n".join(lines)


def _update_plan(ctx: ToolContext, steps: list[dict] | list[str]) -> ToolResult:
    norm: list[dict[str, str]] = []
    for s in steps:
        if isinstance(s, str):
            norm.append({"step": s, "status": "pending"})
        elif isinstance(s, dict):
            status = str(s.get("status", "pending")).lower().replace(" ", "_")
            if status in ("done", "complete", "finished"):
                status = "completed"
            if status in ("active", "doing", "started"):
                status = "in_progress"
            norm.append({"step": str(s.get("step") or s.get("title") or s.get("task") or ""),
                         "status": status if status in ICON else "pending",
                         **({"notes": str(s["notes"])} if s.get("notes") else {})})
    if ctx.session is not None:
        ctx.session.set_plan(norm)
    ctx.emit("plan_updated", steps=norm)
    active = [s for s in norm if s["status"] == "in_progress"]
    footer = ""
    if len(active) > 1:
        footer = "\n\nWarning: keep exactly one step in_progress."
    return ToolResult("Plan updated:\n" + _render(norm) + footer, metadata={"steps": norm})


@dataclass
class PlanProposal:
    """Stand-in tool object so the CLI approval hook can recognize a plan approval."""
    name: str = "propose_plan"
    read_only: bool = True


async def _propose_plan(ctx: ToolContext, steps: list[str], actions: list[str] | None = None) -> ToolResult:
    steps = [s for s in (steps or []) if str(s).strip()]
    if not steps:
        return ToolResult("propose_plan requires at least one step.", is_error=True)
    if ctx.approvals is None:
        return ToolResult("Plan mode needs an approval channel; run interactively or disable plan_mode.",
                          is_error=True)
    payload = {"steps": steps, "actions": actions or []}
    decision = ctx.approvals(PlanProposal(), payload, "approve the whole plan (mutating actions are batched)")
    if inspect.isawaitable(decision):
        decision = await decision
    if not decision:
        return ToolResult("Plan rejected. Continue read-only exploration or propose a revised plan.", is_error=True)
    ctx.extra["plan_approved"] = True
    if ctx.session is not None:
        ctx.session.set_plan([{"step": s, "status": "pending"} for s in steps])
    ctx.emit("plan_approved", steps=steps, actions=actions or [])
    return ToolResult("Plan approved. Mutating tools are enabled for this run; follow the approved steps "
                      "and keep the plan updated with update_plan.")


def register(reg: ToolRegistry) -> None:
    reg.add("update_plan",
            "Create or update the task plan. Pass the full list each time. Statuses: pending, in_progress, "
            "completed, blocked. Keep exactly one step in_progress. The plan survives context compaction.",
            {"type": "object", "properties": {"steps": {"type": "array", "items": {
                "type": "object", "properties": {"step": {"type": "string"},
                                                 "status": {"type": "string", "enum": list(ICON)},
                                                 "notes": {"type": "string"}},
                "required": ["step"]}}}, "required": ["steps"]},
            _update_plan, read_only=True)

    reg.add("propose_plan",
            "Plan mode only: submit the complete plan (ordered steps plus the mutating actions you intend) for "
            "ONE batch approval. Call this after read-only exploration and before any write/shell action.",
            {"type": "object", "properties": {
                "steps": {"type": "array", "items": {"type": "string"}},
                "actions": {"type": "array", "items": {"type": "string"},
                            "description": "Mutating tools/commands this plan will use."}},
             "required": ["steps"]},
            _propose_plan, read_only=True)
