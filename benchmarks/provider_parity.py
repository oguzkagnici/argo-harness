"""Live parity check: /chat/completions vs /responses against the configured endpoint.

Verifies text, tool-call assembly, tool-result replay, and streaming for the same
model/config. Not part of the offline test suite (requires network + key).

Usage:
  python benchmarks/provider_parity.py
"""
from __future__ import annotations

import asyncio
import copy
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from argo.config import load_config  # noqa: E402
from argo.core.types import Message  # noqa: E402
from argo.llm.client import LLMClient  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"
CALC = [{"type": "function", "function": {
    "name": "calc", "description": "Evaluate arithmetic",
    "parameters": {"type": "object", "properties": {"expr": {"type": "string"}}, "required": ["expr"]}}}]


def evaluate(expr: str) -> str:
    allowed = set("0123456789+-*/(). %")
    if not expr or any(ch not in allowed for ch in expr):
        raise ValueError("unsafe expression")
    return str(eval(expr, {"__builtins__": {}}, {}))  # noqa: S307 - input restricted above


async def run_mode(wire_api: str) -> dict:
    cfg = load_config()
    cfg = copy.deepcopy(cfg)
    cfg.provider.wire_api = wire_api
    row = {"wire_api": wire_api}
    async with LLMClient(cfg) as llm:
        t0 = time.monotonic()
        first = await llm.chat([Message(role="user", content="Use the calc tool to compute 17*23. Call it now.")], CALC)
        row["tool_call_latency_s"] = round(time.monotonic() - t0, 2)
        row["tool_calls"] = [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in first.message.tool_calls]
        if not first.message.tool_calls:
            row["ok"] = False
            row["error"] = "no tool call"
            return row
        call = first.message.tool_calls[0]
        result = evaluate(call.args().get("expr", ""))
        messages = [
            Message(role="user", content="Use the calc tool to compute 17*23. Call it now."),
            Message(role="assistant", content=first.message.content, tool_calls=first.message.tool_calls),
            Message(role="tool", content=result, tool_call_id=call.id, name=call.name),
        ]
        t1 = time.monotonic()
        final = await llm.chat(messages, CALC)
        row["final_latency_s"] = round(time.monotonic() - t1, 2)
        row["final_text"] = (final.message.content or "")[:200]
        row["final_has_391"] = "391" in (final.message.content or "")
        row["tokens"] = first.usage.total_tokens + final.usage.total_tokens
        t2 = time.monotonic()
        streamed = await llm.collect_stream([Message(role="user", content="Reply with exactly PARITY-STREAM-OK")])
        row["stream_latency_s"] = round(time.monotonic() - t2, 2)
        row["stream_text"] = (streamed.message.content or "")[:120]
        row["stream_ok"] = "PARITY" in (streamed.message.content or "").upper()
        row["ok"] = bool(row["final_has_391"] and row["stream_ok"])
    return row


async def main() -> int:
    rows = [await run_mode("chat"), await run_mode("responses")]
    for row in rows:
        print(f"{row['wire_api']:<10} ok={row['ok']} tool_calls={len(row.get('tool_calls', []))} "
              f"final391={row.get('final_has_391')} stream={row.get('stream_ok')} "
              f"tokens={row.get('tokens')} lat={row.get('tool_call_latency_s')}/{row.get('final_latency_s')}s")
        if not row["ok"]:
            print("  detail:", row)
    summary = {"benchmark": "provider-parity", "model": load_config().provider.model, "rows": rows,
               "ok": all(r["ok"] for r in rows),
               "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"provider_parity_{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nSaved: {out.relative_to(ROOT)}")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
