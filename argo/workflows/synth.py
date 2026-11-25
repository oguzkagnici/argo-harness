"""Synthesize a reusable workflow from a successful session trace.

This is the "extract automation" step: a messy exploratory trace becomes a small,
parameterized, replayable program plus a skill document. The model is constrained
to the tools that actually exist, and literal values that vary between runs are
lifted into parameters.
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..core.types import Message
from .core import Workflow

SYSTEM = """You convert a successful agent tool trace into a REUSABLE, parameterized workflow.

You will receive a JSON trace of tool calls plus the current tool catalog. Produce the smallest workflow that
reproduces the successful outcome.

Rules:
- Use ONLY tools in the catalog, with exactly the argument names from it.
- Lift run-specific literals into parameters: URLs, search queries, file paths, names, dates, ids, emails.
  Do NOT parameterize structural constants (selectors, CSS classes, API endpoints) unless they vary.
- Drop exploratory dead ends, failed attempts that were later fixed, and redundant reads.
- Collapse repeated near-identical steps (e.g. typing field by field may stay if selectors differ).
- Add `expect_contains` only for genuine success checks visible in the trace output
  (e.g. "exit_code: 0", "Created ...", a confirmation string on the page).
- Add `save_as` when a later step needs an earlier step's output; reference it as {{var}}.
- Determine required parameters and sensible defaults.
- Prefer `run_code`/`bash` for deterministic local computation, `fetch_url` for public data,
  `computer` for UI-only flows. Do not invent API endpoints.
- Write a description that tells a future agent WHEN to use this workflow.

Return ONLY JSON:
{
  "name": "snake_case_name",
  "description": "when/why to use this, and the outcome it produces",
  "params": [{"name": "...", "description": "...", "required": true, "default": null}],
  "steps": [{"tool": "...", "args": {}, "save_as": "", "expect_contains": "",
             "expect_exit_zero": false, "retries": 0, "on_error": "stop"}],
  "notes": "caveats, assumptions, and what to verify"
}
"""


def _trace_digest(events: list[dict[str, Any]], max_calls: int = 60, max_chars: int = 40_000) -> list[dict[str, Any]]:
    calls = []
    for ev in events:
        if ev.get("type") == "tool":
            calls.append({"tool": ev.get("name"), "args": ev.get("args"),
                          "output": (ev.get("result") or "")[:1500], "is_error": ev.get("is_error")})
        elif ev.get("type") == "plan":
            calls.append({"plan": ev.get("steps")})
    return calls[-max_calls:]


def _extract_json(text: str) -> dict[str, Any]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object")
    depth = 0
    in_str = esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unbalanced JSON")


async def synthesize_workflow(llm: Any, events: list[dict[str, Any]], catalog: list[dict[str, Any]],
                              name_hint: str = "", instructions: str = "") -> Workflow:
    trace = _trace_digest(events)
    if not trace:
        raise ValueError("session has no tool calls to extract")
    user = (f"TRACE:\n{json.dumps(trace, ensure_ascii=False, default=str)[:40000]}\n\n"
            f"TOOL CATALOG:\n{json.dumps(catalog, ensure_ascii=False)[:24000]}\n\n"
            + (f"NAME HINT: {name_hint}\n" if name_hint else "")
            + (f"INSTRUCTIONS: {instructions}\n" if instructions else "")
            + "JSON only.")
    resp = await llm.chat([Message(role="system", content=SYSTEM), Message(role="user", content=user)],
                          temperature=0.2, max_tokens=6000, response_format={"type": "json_object"})
    data = _extract_json(resp.message.content or resp.message.reasoning or "")
    wf = Workflow.from_dict(data)
    if name_hint:
        wf.name = re.sub(r"[^a-zA-Z0-9_-]+", "_", name_hint).strip("_").lower() or wf.name
    wf.name = re.sub(r"[^a-zA-Z0-9_-]+", "_", wf.name).strip("_").lower() or "workflow"
    valid = {t["function"]["name"] for t in catalog}
    wf.steps = [s for s in wf.steps if s.tool in valid]
    return wf


SKILL_TEMPLATE = """---
name: {name}
description: {description}
---

# {title}

{description}

## When to use
{when}

## Parameters
{params}

## Procedure
{steps}

## Verify
{verify}

## Notes
{notes}

Replay with the `run_workflow` tool:
`run_workflow(name="{name}", params={{{example}}})`
"""


def workflow_to_skill(wf: Workflow) -> str:
    params = "\n".join(f"- `{p['name']}`" + (f" (default: `{p['default']}`)" if p.get("default") is not None else "")
                       + (f": {p.get('description','')}" if p.get("description") else "")
                       for p in wf.params) or "- (none)"
    steps = "\n".join(
        f"{i}. `{s.tool}` " + json.dumps(s.args, ensure_ascii=False)
        + (f" → save as `{s.save_as}`" if s.save_as else "")
        + (f" (expect: {s.expect_contains!r})" if s.expect_contains else "")
        for i, s in enumerate(wf.steps, 1))
    verify = "\n".join(f"- Step {i}: {s.expect_contains}" for i, s in enumerate(wf.steps, 1)
                       if s.expect_contains) or "- Review the final step output and the produced artifact."
    example = ", ".join(f'"{p["name"]}": "..."' for p in wf.params[:4])
    return SKILL_TEMPLATE.format(
        name=wf.name, description=(wf.description or wf.name).replace("\n", " "),
        title=wf.name.replace("_", " ").title(), when=wf.description or "(see procedure)",
        params=params, steps=steps, verify=verify, notes=wf.notes or "-", example=example,
    )
