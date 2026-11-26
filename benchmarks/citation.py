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


REPORT_QUESTION = (
    "Summarize our Q3 operations for an executive: throughput, latency, cache migration and cloud spend. "
    "Use ONLY the sources below and cite every factual sentence with [S#]."
)
REQUIRED_SOURCES = {1, 2, 3, 4}  # throughput, latency, cache, finance


def report_prompt() -> str:
    lines = [REPORT_QUESTION, "", "SOURCES:"]
    for i, (url, title, text) in enumerate(SOURCES, 1):
        lines.append(f"[S{i}] {title} ({url}): {text}")
    lines += ["", "Rules: 4-6 short bullets. Each bullet states one fact and ends with its citation(s), e.g. "
                  "'Throughput rose 12% in Q3. [S1]'. Never cite a source that does not contain the fact."]
    return "\n".join(lines)


def parse_report_lines(report: str) -> list[dict]:
    """Split a cited report into {text, refs} entries (one per non-empty bullet/sentence line)."""
    import re
    out = []
    for raw in report.splitlines():
        line = raw.strip().lstrip("-*• ").strip()
        if not line:
            continue
        refs = sorted({int(m) for m in re.findall(r"\[S(\d+)\]", line, re.I)})
        text = re.sub(r"\[S\d+\]", "", line, flags=re.I).strip()
        text = re.sub(r"^\d+[.)]\s*", "", text)
        if text:
            out.append({"text": text, "refs": refs})
    return out


def citation_scores(rows: list[dict], required: set[int]) -> dict:
    """Precision over cited lines (verdict supported) and recall of required sources."""
    cited = [r for r in rows if r["refs"]]
    supported = sum(1 for r in cited if r.get("verdict") == "supported")
    covered = set().union(*(set(r["refs"]) for r in cited)) if cited else set()
    return {
        "lines": len(rows), "cited_lines": len(cited), "uncited_lines": len(rows) - len(cited),
        "citation_precision": round(supported / len(cited), 3) if cited else 0.0,
        "citation_recall": round(len(covered & required) / len(required), 3) if required else 0.0,
        "sources_cited": sorted(covered), "missing_required": sorted(required - covered),
    }


async def run_report(cfg, llm) -> dict:
    from argo.research.ledger import EvidenceLedger
    from argo.research.verify import verify_ledger

    started = time.monotonic()
    from argo.core.types import Message
    resp = await llm.chat([Message(role="user", content=report_prompt())])
    report = (resp.message.content or "").strip()
    rows = parse_report_lines(report)
    ledger = EvidenceLedger()
    ids: dict[int, str] = {}
    for i, (url, title, text) in enumerate(SOURCES, 1):
        ids[i] = ledger.add_source(url, text, title).id
    for row in rows:
        valid = [ids[r] for r in row["refs"] if r in ids]
        claim = ledger.add_claim(row["text"], valid) if valid else None
        row["claim_id"] = claim.id if claim else ""
    if ledger.claims:
        await verify_ledger(ledger, llm, concurrency=4)
    for row in rows:
        cid = row.get("claim_id")
        row["verdict"] = ledger.claims[cid].verdict if cid and cid in ledger.claims else "uncited"
        row["reason"] = ledger.claims[cid].reason[:200] if cid and cid in ledger.claims else ""
    scores = citation_scores(rows, REQUIRED_SOURCES)
    return {"benchmark": "citation-report-level", "question": REPORT_QUESTION,
            "report": report, "rows": rows, "scores": scores,
            "seconds": round(time.monotonic() - started, 1),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


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


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", action="store_true",
                    help="ALCE-style report-level run: generate a cited report and score precision/recall")
    args = ap.parse_args()
    if args.report:
        return asyncio.run(_report_main())
    return asyncio.run(main_async())


async def _report_main() -> int:
    cfg = load_config()
    async with LLMClient(cfg) as llm:
        row = await run_report(cfg, llm)
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"citation_report_{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(row, indent=2), encoding="utf-8")
    print(row["report"])
    print()
    for r in row["rows"]:
        print(f"{r['verdict']:<11} refs={r['refs']} {r['text'][:70]}")
    s = row["scores"]
    print(f"\ncitation_precision={s['citation_precision']:.3f} "
          f"citation_recall={s['citation_recall']:.3f} "
          f"cited={s['cited_lines']}/{s['lines']} missing={s['missing_required']} ({row['seconds']}s)")
    print(f"Saved: {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
