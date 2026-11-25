"""Memory learning-loop benchmark: stale belief -> correction -> dream -> recall.

CL-Bench-style protocol (Patwardhan et al., 2025) on a compact synthetic set:
each "lesson" plants a stale fact, the user corrects it in a transcript, the
dream engine consolidates (G -> G+1), and the same queries are re-run.

Metrics (top-k recall):
- stale_rate: fraction of queries whose top-k still returns the stale belief (lower is better)
- correct_rate: fraction whose top-k returns the corrected belief (higher is better)

Usage:
  python benchmarks/memory_loop.py --k 3
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


async def main_async(args: argparse.Namespace) -> int:
    cfg = load_config()
    tmp = tempfile.TemporaryDirectory(prefix="argo_mem_bench_")
    async with LLMClient(cfg) as llm:
        store = MemoryStore(Path(tmp.name) / "memory.sqlite", embed=llm.embed,
                            rerank=lambda q, d, n: llm.rerank(q, d, top_n=n), model=cfg.models.embedding_model or "default")
        try:
            for i, lesson in enumerate(LESSONS):
                await store.add(lesson["stale"], layer="note", kind="fact", importance=0.75,
                                confidence=0.9, source="bench:stale")
                store.log("bench", "user", f"Update for lesson {i}: {lesson['correction']}")
                store.log("bench", "assistant", f"Noted. {lesson['correction']}")

            before = [await probe(store, lesson, args.k) for lesson in LESSONS]
            eng = DreamEngine(store, llm, cfg=cfg)
            t0 = time.monotonic()
            report = await eng.run(session_ids=["bench"], apply=True)
            dream_s = time.monotonic() - t0
            after = [await probe(store, lesson, args.k) for lesson in LESSONS]
            stats = store.stats()
            active = [i.text for i in store.list_items()]
        finally:
            store.close()

    def rate(rows, key):
        return round(sum(1 for r in rows if r[key]) / len(rows), 4) if rows else 0.0

    summary = {
        "benchmark": "memory-learning-loop", "lessons": len(LESSONS), "k": args.k,
        "dream": {"status": report.status, "generation": f"{report.input_generation}->{report.output_generation}",
                  "added": report.added, "superseded": report.superseded,
                  "merged": report.merged, "archived": report.archived,
                  "wiki": report.wiki, "insights": report.insights,
                  "procedures": report.procedures, "unverified": report.unverified,
                  "warnings": len(report.details.get("warnings") or []), "seconds": round(dream_s, 1)},
        "before": {"stale_rate": rate(before, "stale"), "stale_mention_rate": rate(before, "stale_mention"),
                   "correct_rate": rate(before, "correct")},
        "after": {"stale_rate": rate(after, "stale"), "stale_mention_rate": rate(after, "stale_mention"),
                  "correct_rate": rate(after, "correct")},
        "memory_stats": stats, "items": active[:20], "per_lesson": {"before": before, "after": after},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / (f"memory_loop_{time.strftime('%Y%m%d-%H%M%S')}.json")
    out.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    print(f"dream {report.status} ({summary['dream']['generation']}) in {dream_s:.1f}s; "
          f"added={report.added} superseded={report.superseded} merged={report.merged} "
          f"warnings={len(report.details.get('warnings') or [])}")
    print(f"before: contradictory_stale={summary['before']['stale_rate']:.2f} "
          f"stale_mention={summary['before']['stale_mention_rate']:.2f} correct={summary['before']['correct_rate']:.2f}")
    print(f"after:  contradictory_stale={summary['after']['stale_rate']:.2f} "
          f"stale_mention={summary['after']['stale_mention_rate']:.2f} correct={summary['after']['correct_rate']:.2f}")
    for lesson, b, a in zip(LESSONS, before, after):
        print(f"- {lesson['query'][:52]:<54} stale {b['stale']}->{a['stale']}  "
              f"mention {b['stale_mention']}->{a['stale_mention']}  correct {b['correct']}->{a['correct']}")
    warnings = report.details.get("warnings") or []
    if warnings:
        print("warnings:", warnings[:5])
    print(f"\nSaved: {out.relative_to(ROOT)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--k", type=int, default=3)
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
