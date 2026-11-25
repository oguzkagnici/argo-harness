"""Memory learning-loop benchmark: stale belief -> correction -> dream -> recall.

CL-Bench-style protocol (Patwardhan et al., 2025) on a compact synthetic set:
each "lesson" plants a stale fact, the user corrects it in a transcript, the
dream engine consolidates (G -> G+1), and the same queries are re-run.

Metrics (top-k recall):
- stale_rate: fraction of queries whose top-k still returns the stale belief (lower is better)
- correct_rate: fraction whose top-k returns the corrected belief (higher is better)

Usage:
  python benchmarks/memory_loop.py --k 3
  python benchmarks/memory_loop.py --arms no_dream,no_wiki,no_grounding,two_generations,full

Ablation arms answer *why* the loop works:
  no_dream          baseline: correction in a transcript, no consolidation
  no_wiki           dream runs but may not create wiki pages or procedures
  no_grounding      dream runs with the evidence check disabled (trust everything)
  two_generations   dream applied twice (does a second pass help or add duplicates?)
  full              default production behavior
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from argo.config import load_config  # noqa: E402
from argo.llm.client import LLMClient  # noqa: E402
from argo.memory.dream import DreamEngine  # noqa: E402
from argo.memory.store import MemoryStore  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"

LESSONS = [
    {"query": "What is the staging database host?",
     "stale": "Staging database host is db1.internal",
     "correction": "The staging database moved to db2.internal after the migration.",
     "must_keep": "db2.internal", "must_drop": "db1.internal"},
    {"query": "Which model do we use for batch summarization?",
     "stale": "Batch summarization uses model-summarizer-v1",
     "correction": "We upgraded batch summarization to model-summarizer-v3 last week.",
     "must_keep": "model-summarizer-v3", "must_drop": "model-summarizer-v1"},
    {"query": "What is the deploy freeze window?",
     "stale": "Deploy freeze runs Friday 12:00 to 14:00 UTC",
     "correction": "The deploy freeze window is now Friday 16:00 to 18:00 UTC.",
     "must_keep": "16:00", "must_drop": "12:00 to 14:00"},
    {"query": "Who owns the billing service on-call?",
     "stale": "Billing service on-call owner is team Atlas",
     "correction": "Billing service on-call ownership moved from team Atlas to team Beacon.",
     "must_keep": "Beacon", "must_drop": "Atlas"},
    {"query": "What is the API rate limit for enterprise tenants?",
     "stale": "Enterprise API rate limit is 500 requests per minute",
     "correction": "Enterprise API rate limit was raised to 2000 requests per minute.",
     "must_keep": "2000", "must_drop": "500 requests"},
    {"query": "Which region holds the primary analytics warehouse?",
     "stale": "Primary analytics warehouse is in us-east-1",
     "correction": "Primary analytics warehouse was migrated to eu-central-1.",
     "must_keep": "eu-central-1", "must_drop": "us-east-1"},
]


async def probe(store: MemoryStore, lesson: dict, k: int) -> dict:
    hits = await store.retrieve(lesson["query"], k=k, include_files=False, rerank=False)
    text = "\n".join(h.text for h in hits).lower()
    has_stale = lesson["must_drop"].lower() in text
    has_correct = lesson["must_keep"].lower() in text
    return {"stale": has_stale and not has_correct,   # contradicts the correction
            "stale_mention": has_stale,               # historical mention is acceptable
            "correct": has_correct,
            "top": [h.text[:90] for h in hits]}


def rate(rows, key):
    return round(sum(1 for r in rows if r[key]) / len(rows), 4) if rows else 0.0


async def run_arm(arm: str, cfg, llm, k: int) -> dict:
    import argo.memory.dream as dream_mod

    tmp = tempfile.TemporaryDirectory(prefix=f"argo_mem_{arm}_")
    store = MemoryStore(Path(tmp.name) / "memory.sqlite", embed=llm.embed,
                        rerank=lambda q, d, n: llm.rerank(q, d, top_n=n),
                        model=cfg.models.embedding_model or "default")
    original_grounded = dream_mod._grounded
    if arm == "no_grounding":
        dream_mod._grounded = lambda evidence, corpus: True
    try:
        for i, lesson in enumerate(LESSONS):
            await store.add(lesson["stale"], layer="note", kind="fact", importance=0.75,
                            confidence=0.9, source="bench:stale")
            store.log("bench", "user", f"Update for lesson {i}: {lesson['correction']}")
            store.log("bench", "assistant", f"Noted. {lesson['correction']}")
        before = [await probe(store, lesson, k) for lesson in LESSONS]

        reports = []
        seconds = 0.0
        if arm != "no_dream":
            eng = DreamEngine(store, llm, cfg=cfg)
            passes = 2 if arm == "two_generations" else 1
            instructions = ("Do not create wiki pages or procedures; only reconcile notes and insights."
                            if arm == "no_wiki" else "")
            for _ in range(passes):
                t0 = time.monotonic()
                reports.append(await eng.run(session_ids=["bench"], instructions=instructions, apply=True))
                seconds += time.monotonic() - t0
        after = [await probe(store, lesson, k) for lesson in LESSONS]
        stats = store.stats()
        active = [i.text for i in store.list_items()]
    finally:
        dream_mod._grounded = original_grounded
        store.close()
        tmp.cleanup()

    return {
        "arm": arm,
        "dream": [{"status": r.status, "generation": f"{r.input_generation}->{r.output_generation}",
                   "added": r.added, "superseded": r.superseded, "merged": r.merged,
                   "archived": r.archived, "wiki": r.wiki, "insights": r.insights,
                   "procedures": r.procedures, "unverified": r.unverified,
                   "warnings": len(r.details.get("warnings") or [])} for r in reports],
        "seconds": round(seconds, 1),
        "before": {"stale_rate": rate(before, "stale"), "stale_mention_rate": rate(before, "stale_mention"),
                   "correct_rate": rate(before, "correct")},
        "after": {"stale_rate": rate(after, "stale"), "stale_mention_rate": rate(after, "stale_mention"),
                  "correct_rate": rate(after, "correct")},
        "memory_stats": stats, "items": active[:20],
        "active_items": sum(v.get("active", 0) for v in stats.get("layers", {}).values()),
        "per_lesson_after": after,
    }


async def main_async(args: argparse.Namespace) -> int:
    cfg = load_config()
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    rows = []
    async with LLMClient(cfg) as llm:
        for arm in arms:
            row = await run_arm(arm, cfg, llm, args.k)
            rows.append(row)
            print(f"{arm:<16} stale {row['before']['stale_rate']:.2f}->{row['after']['stale_rate']:.2f}  "
                  f"mention {row['before']['stale_mention_rate']:.2f}->{row['after']['stale_mention_rate']:.2f}  "
                  f"correct {row['before']['correct_rate']:.2f}->{row['after']['correct_rate']:.2f}  "
                  f"items={row['active_items']} {row['seconds']:.1f}s", flush=True)

    summary = {"benchmark": "memory-learning-loop-ablation", "lessons": len(LESSONS), "k": args.k,
               "arms": rows, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / (f"memory_ablation_{time.strftime('%Y%m%d-%H%M%S')}.json")
    out.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    print(f"\n{'arm':<16} {'stale':>7} {'mention':>8} {'correct':>8} {'items':>6}")
    for row in rows:
        print(f"{row['arm']:<16} {row['after']['stale_rate']:>7.2f} {row['after']['stale_mention_rate']:>8.2f} "
              f"{row['after']['correct_rate']:>8.2f} {row['active_items']:>6}")
    print(f"\nSaved: {out.relative_to(ROOT)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--k", type=int, default=3)
    parser.add_argument("--arms", default="no_dream,full",
                        help="comma-separated: no_dream,no_wiki,no_grounding,two_generations,full")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
