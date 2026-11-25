"""Live long-horizon context run: does compaction keep early details?

A local server hands out a 36-step token chain: each reply contains the next step's token, so the agent
must make one sequential fetch per step (no batching is possible without the token). Step payloads carry
~300 tokens of realistic noise around a short token. The context window is configured small (18k) so that
compaction engages mid-run, and the final answer asks for the tokens of steps 3, 20 and 36.

Modes:
  full       no context management (window only used for reporting)
  autocompact production policy (0.75 threshold + verbatim fact ledger)
  no_ledger  same but without the fact ledger (ablation)

Usage:
  python benchmarks/context_live.py --steps 8                 # smoke
  python benchmarks/context_live.py --steps 36 --modes full,autocompact,no_ledger
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from argo.config import load_config  # noqa: E402
from argo.core.agent import Agent  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"
FILLER = ("The upstream batch completed without anomalies; latency stayed inside the expected envelope, "
          "the retry budget was untouched, and the reconciliation job reported no drift. ")

STATE: dict[str, object] = {"index": -1, "steps": [], "mode": "full", "hits": 0}


def build_steps(n: int, seed: int = 11) -> list[dict[str, str]]:
    rng = random.Random(seed)
    steps = []
    for i in range(n):
        token = f"T{i:02d}-{rng.randrange(16**6):06x}"
        body = FILLER * 4
        steps.append({"idx": i, "token": token,
                      "payload": f"STEP {i:02d}. {body} Checkpoint id {token}. End of step {i:02d}."})
    return steps


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/reset":
            STATE["index"] = -1
            return self._send({"ok": True})
        if parsed.path == "/start":
            STATE["index"] = 0
            return self._send({"step": 0, "payload": STATE["steps"][0]["payload"]})
        if parsed.path == "/step":
            token = (parse_qs(parsed.query).get("token") or [""])[0]
            idx = int(STATE["index"]) + 1
            steps = STATE["steps"]
            if idx >= len(steps):
                return self._send({"done": True})
            if token != steps[idx - 1]["token"]:
                return self._send({"error": "invalid token; use the checkpoint id from the previous reply"},
                                  status=403)
            STATE["index"] = idx
            STATE["hits"] = int(STATE["hits"]) + 1
            return self._send({"step": idx, "payload": steps[idx]["payload"]})
        self._send({"error": "not found"}, status=404)

    def _send(self, body: dict, status: int = 200) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def serve() -> tuple[ThreadingHTTPServer, str]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{port}"


def prompt(base: str, n: int, probes: list[int]) -> str:
    probe_list = ", ".join(str(p) for p in probes)
    return (
        f"Follow this protocol exactly, one fetch_url call per response (never batch, never use code):\n"
        f"1) Fetch {base}/start. It returns step 0's payload containing a checkpoint id.\n"
        f"2) For k = 1..{n - 1}: fetch {base}/step?token=<checkpoint id from the previous reply>. Each reply "
        f"has the next payload and a new checkpoint id.\n"
        f"3) After step {n - 1}, reply with only the checkpoint ids for steps {probe_list}, one per line as "
        f"'STEP <n>: <id>'. Do not write files. Do not explain."
    )


def build_cfg(ws: Path, mode: str):
    cfg = load_config({"workspace": str(ws), "data_dir": str(ws / ".argo")})
    cfg.models.context_window = 12_000
    cfg.models.max_output_tokens = 2_000
    cfg.compact_reserve_tokens = 4_000
    cfg.keep_recent_tokens = 3_000
    cfg.time_based_microcompact = False
    cfg.compaction_enabled = mode != "full"  # unmanaged baseline: never summarize
    cfg.compaction_threshold = 0.75
    cfg.compact_fact_ledger = mode != "no_ledger"
    return cfg


async def run_mode(mode: str, base: str, steps: list[dict], probes: list[int], timeout: float) -> dict:
    STATE["index"] = -1
    STATE["hits"] = 0
    ws = Path(f"/tmp/argo_ctx_live_{mode}_{int(time.time())}")
    ws.mkdir(parents=True, exist_ok=True)
    cfg = build_cfg(ws, mode)
    started = time.monotonic()
    async with Agent(cfg, max_iterations=len(steps) + 4, allow_tools={"fetch_url"}) as agent:
        try:
            result = await asyncio.wait_for(agent.run(prompt(base, len(steps), probes)), timeout=timeout)
            out, stop = result.output, result.stop_reason
            iterations, tokens = result.iterations, result.usage.total_tokens
            compactions, resets = result.compactions, result.resets
        except asyncio.TimeoutError:
            out, stop, iterations, tokens, compactions, resets = "", "timeout", 0, 0, 0, 0
    found = {}
    for p in probes:
        match = re.search(rf"STEP\s*0*{p}\s*[:=]\s*(T{p:02d}-[0-9a-f]{{6}})", out, re.I)
        found[p] = match.group(1) if match else ""
    expected = {p: steps[p]["token"] for p in probes}
    correct = sum(found[p].lower() == expected[p].lower() for p in probes)
    return {"mode": mode, "steps_fetched": STATE["hits"], "stop": stop, "iterations": iterations,
            "tokens": tokens, "compactions": compactions, "resets": resets,
            "correct": correct, "probes": len(probes),
            "expected": expected, "found": found,
            "answer": out[-600:], "seconds": round(time.monotonic() - started, 1)}


async def main_async(args) -> int:
    steps = build_steps(args.steps)
    probes = sorted({p for p in (3, 20, min(36, args.steps - 1)) if p < args.steps})
    srv, base = serve()
    STATE["steps"] = steps
    rows = []
    try:
        for mode in [m.strip() for m in args.modes.split(",") if m.strip()]:
            print(f"running {mode} ({args.steps} steps, probes {probes})...", flush=True)
            row = await run_mode(mode, base, steps, probes, args.timeout)
            rows.append(row)
            print(f"{mode:<12} correct={row['correct']}/{row['probes']} fetched={row['steps_fetched']} "
                  f"stop={row['stop']} iter={row['iterations']} tok={row['tokens']:,} "
                  f"comp={row['compactions']} reset={row['resets']} {row['seconds']:.0f}s", flush=True)
    finally:
        srv.shutdown()
    summary = {"benchmark": "context-live-token-chain", "steps": args.steps, "probes": probes,
               "window": 12_000, "rows": rows,
               "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"context_live_{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nSaved: {out.relative_to(ROOT)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--steps", type=int, default=36)
    ap.add_argument("--modes", default="full,autocompact,no_ledger")
    ap.add_argument("--timeout", type=float, default=1800.0)
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
