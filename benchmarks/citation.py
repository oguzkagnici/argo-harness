"""Citation-verification benchmark (ALCE-inspired, local corpus).

Twelve claims are recorded in the evidence ledger against a six-document corpus: six are fully supported,
two overstate what the source says, three are false, and one cites the wrong document. The verifier must
label each claim. A perfect verifier is not required to be useful; what matters is whether unsupported
claims are prevented from entering the report.

Usage:
  python benchmarks/citation.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from argo.config import load_config  # noqa: E402
from argo.llm.client import LLMClient  # noqa: E402
from argo.research.ledger import EvidenceLedger  # noqa: E402
from argo.research.verify import verify_ledger  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"

SOURCES = [
    ("https://ops.example/q3-review", "Q3 operations review",
     "Throughput rose 12% to 4,800 req/s in Q3. The error rate fell from 0.8% to 0.3% over the quarter."),
    ("https://ops.example/latency", "Latency report",
     "p95 latency was 180 ms in Q3, down from 240 ms in Q2. The target of 200 ms was met for the first time."),
    ("https://ops.example/cache", "Cache migration note",
     "The cache migration completed on 2026-08-14. Cache hit ratio reached 91% after the cutover."),
    ("https://ops.example/finance", "Finance summary",
     "Headcount grew from 42 to 45 engineers. Cloud spend was $380k for the quarter."),
    ("https://ops.example/incidents", "Incident review",
     "Two incidents occurred: INC-77 (12 min) and INC-112 (47 min). No customer data was exposed."),
    ("https://ops.example/rollout", "Rollout plan",
     "The rollout covered 3 regions: eu-west, us-east, ap-south. Two regions remain on the legacy stack."),
]

# (claim, cited source index, expected verdict)
CLAIMS = [
    ("Throughput rose 12% in Q3.", 0, "supported"),
    ("p95 latency fell to 180 ms.", 1, "supported"),
    ("The cache hit ratio reached 91%.", 2, "supported"),
    ("No customer data was exposed.", 4, "supported"),
    ("The rollout covered three regions.", 5, "supported"),
    ("Cloud spend was $380k.", 3, "supported"),
    ("Throughput doubled in Q3.", 0, "partial"),
    ("p95 latency fell by half.", 1, "partial"),
    ("Headcount grew from 42 to 60 engineers.", 3, "unsupported"),
    ("The rollout covered five regions.", 5, "unsupported"),
    ("The error rate rose to 0.8%.", 0, "unsupported"),
    ("The cache hit ratio reached 91%.", 3, "unsupported"),  # true statement, wrong source
]


async def main_async() -> int:
    cfg = load_config()
    ledger = EvidenceLedger()
    ids = [ledger.add_source(url, text, title).id for url, title, text in SOURCES]
    expected = []
    for text, idx, verdict in CLAIMS:
        ledger.add_claim(text, [ids[idx]])
        expected.append(verdict)
    started = time.monotonic()
    async with LLMClient(cfg) as llm:
        stats = await verify_ledger(ledger, llm, concurrency=4)
    elapsed = round(time.monotonic() - started, 1)
    got = [c.verdict for c in ledger.claims.values()]
    n = len(got)
    exact = sum(g == e for g, e in zip(got, expected))
    unsupported_idx = [i for i, e in enumerate(expected) if e != "supported"]
    false_support = sum(got[i] == "supported" for i in unsupported_idx)
    caught = sum(got[i] in ("partial", "unsupported") for i in unsupported_idx)
    supported_idx = [i for i, e in enumerate(expected) if e == "supported"]
    retained = sum(got[i] == "supported" for i in supported_idx)
    rows = [{"claim": c.text[:80], "expected": e, "verdict": v, "reason": c.reason[:160]}
            for c, e, v in zip(ledger.claims.values(), expected, got)]
    summary = {
        "benchmark": "citation-verification-local",
        "claims": n, "sources": len(SOURCES), "accuracy": round(exact / n, 3),
        "non_supported_claims": len(unsupported_idx),
        "false_support_rate": round(false_support / len(unsupported_idx), 3),
        "bad_claims_caught_rate": round(caught / len(unsupported_idx), 3),
        "supported_claims_retained_rate": round(retained / len(supported_idx), 3),
        "ledger_stats": stats, "seconds": elapsed,
        "baseline_no_verifier": {"false_support_rate": 1.0, "bad_claims_caught_rate": 0.0},
        "rows": rows, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"citation_{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    for r in rows:
        print(f"{r['verdict']:<11} expected={r['expected']:<11} {r['claim'][:70]}")
    print(f"\naccuracy={summary['accuracy']:.3f} "
          f"bad_caught={summary['bad_claims_caught_rate']:.3f} "
          f"false_support={summary['false_support_rate']:.3f} "
          f"supported_retained={summary['supported_claims_retained_rate']:.3f} "
          f"({elapsed}s)")
    print(f"baseline (no verifier): false_support=1.000 bad_caught=0.000")
    print(f"Saved: {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
