"""Long-horizon context-policy benchmark (synthetic, deterministic transcript).

Builds a ~30k-token agent transcript with facts planted at known steps, replays it through
compaction policies, then asks the model for 12 facts (early/mid/late). Reports how much of the
fact set survives, the tokens each policy would send on the final turn, and the extra tokens the
policy spent on summaries. Aimed at the question: how aggressively should we compact?

Usage:
  python benchmarks/context_bench.py --policies full,microcompact,autocompact_75,autocompact_95,reset
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from argo.config import load_config  # noqa: E402
from argo.core.context import ContextManager  # noqa: E402
from argo.core.tokens import estimate_messages  # noqa: E402
from argo.core.types import Message, ToolCall  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"
FILLER = ("The pipeline processed a batch of records and reported no anomalies. "
          "Metrics stayed within the expected envelope for this phase of the run. ")


def build_transcript(steps: int, seed: int = 7) -> tuple[list[Message], dict[int, str]]:
    rng = random.Random(seed)
    facts: dict[int, str] = {}
    msgs = [Message(role="system", content="You are an operations agent. Work through the checklist.")]
    for i in range(steps):
        value = f"{rng.randrange(10**6):06d}-{rng.choice('ABCDEFGH')}"
        facts[i] = value
        msgs.append(Message(role="user", content=f"Step {i}: inspect shard {i} and record its checksum."))
        msgs.append(Message(role="assistant", content="",
                            tool_calls=[ToolCall(id=f"call_{i}", name="bash",
                                                 arguments=json.dumps({"command": f"inspect {i}"}))]))
        body = (f"shard {i} inspection complete.\nFACT-{i:02d}: {value}\n" + FILLER * 12)
        msgs.append(Message(role="tool", tool_call_id=f"call_{i}", name="bash", content=body))
        msgs.append(Message(role="assistant", content=f"Shard {i} ok."))
    return msgs, facts


def make_cfg(window: int, output: int, threshold: float, keep_recent_tokens: int, ledger: bool = True):
    cfg = load_config()
    cfg.models.context_window = window
    cfg.models.max_output_tokens = output
    cfg.compaction_threshold = threshold
    cfg.keep_recent_tokens = keep_recent_tokens
    cfg.compact_reserve_tokens = 13_000
    cfg.compact_max_failures = 3
    cfg.compact_fact_ledger = ledger
    cfg.time_based_microcompact = False
    cfg.spool_dir = Path("/tmp")
    return cfg


async def apply_policy(name: str, msgs: list[Message], cfg, cm: ContextManager) -> list[Message]:
    if name == "full":
        return list(msgs)
    if name == "microcompact":
        out = [Message(role=m.role, content=m.content, tool_calls=m.tool_calls,
                       tool_call_id=m.tool_call_id, name=m.name) for m in msgs]
        cm.microcompact(out, keep_recent=8, limit=1400)
        return out
    if name == "reset":
        _handoff, kept = await cm.make_handoff(msgs, state_summary="operations checklist")
        return kept
    return await cm.manage(list(msgs))


async def ask_facts(cfg, msgs: list[Message], facts: dict[int, str], probes: list[int]) -> tuple[int, str]:
    from argo.llm.client import LLMClient as LLM
    llm = LLM(cfg)
    # keep every message verbatim: compaction inserts system blocks that must stay visible
    convo = list(msgs)
    q = "Answer from the conversation above. Reply with one line per fact: FACT-<nn>: <value>\n" + \
        "Facts to report:\n" + "\n".join(f"FACT-{i:02d}" for i in probes)
    convo = convo + [Message(role="user", content=q)]
    try:
        resp = await llm.chat(convo, tools=None, temperature=0.0, max_tokens=800)
    except Exception as exc:  # provider rejected the prompt -> hard failure for this policy
        return 0, f"error: {exc}"
    text = resp.message.content or ""
    hits = 0
    for i in probes:
        m = re.search(rf"FACT-{i:02d}\s*[:=]\s*(\S+)", text)
        if m and m.group(1).strip(".,;") == facts[i]:
            hits += 1
    return hits, text[:400]


async def main_async(args) -> int:
    msgs, facts = build_transcript(args.steps)
    tokens = estimate_messages(msgs)
    probes = [1, 3, 5, 8, 13, 21, 26, 31, 38, 45, 52, min(args.steps - 1, 57)]
    probes = sorted({p for p in probes if p in facts})
    print(f"transcript: {len(msgs)} messages, ~{tokens:,} tokens, probes={probes}")
    rows = []
    for name in args.policies.split(","):
        if name == "full":
            cfg = make_cfg(args.window, args.output, 0.75, args.keep_recent)
            cm = ContextManager(cfg)
        elif name == "microcompact":
            cfg = make_cfg(args.window, args.output, 0.75, args.keep_recent)
            cm = ContextManager(cfg)
        elif name == "reset":
            cfg = make_cfg(args.window, args.output, 0.75, args.keep_recent)
            from argo.llm.client import LLMClient as LLM
            cm = ContextManager(cfg, llm=LLM(cfg))
        else:
            threshold = {"autocompact_50": 0.50, "autocompact_75": 0.75, "autocompact_90": 0.90,
                         "autocompact_95": 0.95}.get(name, 0.75)
            cfg = make_cfg(args.window, args.output, threshold, args.keep_recent, ledger=not args.no_ledger)
            from argo.llm.client import LLMClient as LLM
            cm = ContextManager(cfg, llm=LLM(cfg))
        started = time.monotonic()
        final = await apply_policy(name, msgs, cfg, cm)
        final = [m for m in final if not m.internal]
        hits, preview = await ask_facts(cfg, final, facts, probes)
        row = {
            "policy": name,
            "final_tokens": estimate_messages(final),
            "compactions": cm.stats.compactions,
            "microcompactions": cm.stats.microcompactions,
            "resets": cm.stats.resets,
            "fact_hits": hits,
            "fact_total": len(probes),
            "accuracy": round(hits / len(probes), 3),
            "seconds": round(time.monotonic() - started, 1),
            "preview": preview,
        }
        rows.append(row)
        print(f"{name:<16} tokens={row['final_tokens']:>7,} acc={row['accuracy']:.2f} "
              f"({hits}/{len(probes)}) comp={row['compactions']} micro={row['microcompactions']} "
              f"reset={row['resets']} {row['seconds']:>5.1f}s", flush=True)
    out = {"benchmark": "context-policy-synthetic", "steps": args.steps, "window": args.window,
           "fact_ledger": not args.no_ledger,
           "transcript_tokens": tokens, "probes": probes, "rows": rows,
           "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"context_{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nSaved: {path.relative_to(ROOT)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", type=int, default=60)
    ap.add_argument("--window", type=int, default=32_000)
    ap.add_argument("--output", type=int, default=4_000)
    ap.add_argument("--keep-recent", type=int, default=6_000)
    ap.add_argument("--policies", default="full,microcompact,autocompact_75,autocompact_95,reset")
    ap.add_argument("--no-ledger", action="store_true", help="ablation: disable verbatim fact ledger")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
