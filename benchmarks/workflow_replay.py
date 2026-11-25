"""Workflow replay benchmark: re-run an extracted, parameterized workflow across inputs.

Tests the trace->workflow -> deterministic replay path (workflows/core.py) with
the real Playwright computer tool against a local form page.

Usage:
  python benchmarks/workflow_replay.py --workflow demo_login --trials 5
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import http.server
import json
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from argo.config import load_config  # noqa: E402
from argo.tools import computer  # noqa: E402
from argo.tools.base import ToolContext, ToolRegistry  # noqa: E402
from argo.workflows.core import load_workflow, run_workflow  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"
NAMES = ["Grace", "Alan", "Ada", "Linus", "Mira", "Katherine", "Dennis", "Barbara"]


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D401
        pass


def serve(directory: Path) -> tuple[http.server.ThreadingHTTPServer, int]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    handler = functools.partial(QuietHandler, directory=str(directory))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


async def main_async(args: argparse.Namespace) -> int:
    cfg = load_config()
    wf = load_workflow(Path(cfg.workflow_dir), args.workflow)
    if wf is None:
        print(f"workflow '{args.workflow}' not found in {cfg.workflow_dir}", file=sys.stderr)
        return 2
    srv, port = serve(ROOT / "examples" / "web")
    ctx = ToolContext(cfg=cfg)
    registry = ToolRegistry(ctx)
    computer.register(registry)
    rows = []
    try:
        for name in NAMES[: args.trials]:
            params = {"url": f"http://127.0.0.1:{port}/form.html", "name": name}
            started = time.monotonic()
            run = await run_workflow(wf, params, registry)
            final = run.steps[-1].output if run.steps else ""
            success = bool(run.ok and f"Welcome, {name}" in final)
            rows.append({"name": name, "success": success, "ok": run.ok,
                         "steps": len(run.steps), "seconds": round(time.monotonic() - started, 2),
                         "error": run.error, "final": final[:300]})
            print(f"{name:<10} {'PASS' if success else 'FAIL'}  {len(run.steps)} steps "
                  f"{rows[-1]['seconds']:.1f}s {run.error}", flush=True)
    finally:
        await computer.shutdown(ctx)
        srv.shutdown()

    rate = sum(r["success"] for r in rows) / len(rows) if rows else 0.0
    summary = {"benchmark": "workflow-replay", "workflow": wf.name, "workflow_id": wf.id,
               "trials": len(rows), "success_rate": round(rate, 4),
               "mean_seconds": round(sum(r["seconds"] for r in rows) / len(rows), 2) if rows else 0.0,
               "rows": rows, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"workflow_{wf.name}_{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nsuccess_rate={rate:.2%} over {len(rows)} replays")
    print(f"Saved: {out.relative_to(ROOT)}")
    return 0 if rate == 1.0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", default="demo_login")
    parser.add_argument("--trials", type=int, default=5)
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
