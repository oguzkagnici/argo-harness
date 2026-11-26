"""Deterministic selector-drift ablation for workflow replay (no LLM, no tokens).

Replays a stale login workflow against a live local site twice: with self-healing locators and with
plain CSS matching. Measures how many steps survive when ids/classes were renamed.

Usage:
  python benchmarks/workflow_drift.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmarks.computer_use import CREDS, Site  # noqa: E402
from argo.config import load_config  # noqa: E402
from argo.tools import computer as comp  # noqa: E402
from argo.tools.base import ToolContext  # noqa: E402
from argo.workflows.core import Workflow, WorkflowStep, run_workflow  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"

# selector written when the UI still had ids; the live page uses name/data-testid instead
STALE_STEPS = [
    ("goto", {"action": "goto", "url": "{BASE}/"}),
    ("type", {"action": "type", "selector": "#email-field", "text": CREDS["email"]}),
    ("type", {"action": "type", "selector": "#password-input", "text": CREDS["password"]}),
    ("click", {"action": "click", "selector": "#signin"}),
]


def make_workflow(base: str) -> Workflow:
    steps = []
    for tool, args in STALE_STEPS:
        resolved = {k: (v.replace("{BASE}", base) if isinstance(v, str) else v) for k, v in args.items()}
        steps.append(WorkflowStep(tool="computer", args=resolved,
                                  expect_contains="" if tool == "goto" else "ok"))
    return Workflow(name="stale_login", steps=steps)


class Registry:
    """Minimal registry that forwards steps to the computer tool."""

    def __init__(self, ctx: ToolContext):
        self.ctx = ctx

    async def execute(self, tool: str, args: dict):
        assert tool == "computer", tool
        return await comp._computer(self.ctx, **args)


async def arm(base: str, heal: bool) -> dict:
    site_ctx_cfg = load_config({"workspace": str(Path("/tmp/argo_wf_drift"))})
    site_ctx_cfg.ensure_dirs()
    ctx = ToolContext(cfg=site_ctx_cfg)
    restored = None
    if not heal:
        restored = (comp.selector_variants, comp._resolve)
        comp.selector_variants = lambda _s: []  # type: ignore[assignment]

        async def plain(page, selector, text="", timeout=1500):
            loc = page.locator(selector)
            if await loc.count():
                return loc.first, selector, ""
            raise ValueError(f"no element matched selector {selector!r}")
        comp._resolve = plain  # type: ignore[assignment]
    started = time.monotonic()
    try:
        run = await run_workflow(make_workflow(base), {}, Registry(ctx))
    finally:
        if restored:
            comp.selector_variants, comp._resolve = restored
        await comp.shutdown(ctx)
    return {"heal": heal, "ok": run.ok, "steps_ok": sum(s.ok for s in run.steps),
            "steps": len(run.steps), "heals": len([e for e in ctx.events.history if e.type == "selector_heal"]),
            "seconds": round(time.monotonic() - started, 2),
            "error": run.error, "outputs": [s.output[:120] for s in run.steps]}


async def main() -> int:
    site = Site()
    base = site.start()
    try:
        rows = [await arm(base, heal=False), await arm(base, heal=True)]
    finally:
        site.stop()
    RESULTS.mkdir(parents=True, exist_ok=True)
    rec = {"benchmark": "workflow-selector-drift", "rows": rows,
           "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    path = RESULTS / "workflow_drift.json"
    path.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    for r in rows:
        label = "heal on " if r["heal"] else "heal off"
        print(f"{label}: ok={r['ok']} steps={r['steps_ok']}/{r['steps']} heals={r['heals']} "
              f"{r['seconds']}s {('- ' + r['error']) if r['error'] else ''}")
    print(f"Saved: {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
