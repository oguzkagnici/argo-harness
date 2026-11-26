"""Deep-research exact-answer subset (GAIA/BrowseComp-style) over cached local pages.

The agent must gather facts from several pages (some multi-hop, one with a stale duplicate)
and reply with an exact value. Pages are deterministic and cached on disk, so the benchmark
measures research + synthesis, not web availability.

Usage:
  python benchmarks/deep_research.py --questions 6
  python benchmarks/deep_research.py --self-check     # validate page server + scorer offline
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from argo.config import load_config  # noqa: E402
from argo.core.agent import Agent  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"
CACHE = Path(__file__).resolve().parent / "data" / "pages"

PAGES: dict[str, str] = {
    "docs/alpha": "Program overview. Project Alpha is our flagship rollout. Its launch code is ZX-441. "
                  "The rollout spans three regions.",
    "docs/beta": "Finance note. Project Beta has an approved budget of $2.4M for this year.",
    "docs/teams": "Ownership map. The Helios team owns Project Beta. The Vega team owns Project Alpha.",
    "docs/people": "Staff note. The Helios team lead is Mira Chen. The Vega team lead is Tomas Ruiz.",
    "docs/metrics": "Quarterly metrics. Q3 revenue was $1.2M. Q4 revenue was $1.5M.",
    "docs/vendors": "Vendor registry. Vendor Orion's contract ends 2027-03-15. Vendor Lyra is renewed annually.",
    "docs/alpha-old": "Draft (2024). Project Alpha launch code is QQ-000. Superseded by the 2026 overview.",
    "docs/omega-note": "Planning note. Project Omega has a proposed budget of $9.9M. "
                       "Project Omega is unrelated to Alpha or Beta.",
    "docs/contracts": "Contract registry. Vendor Orion is managed by the Helios team. "
                      "Vendor Lyra is managed by the Vega team.",
}

QUESTIONS = [
    {"id": "alpha_code", "question": "What is Project Alpha's launch code?",
     "answer": "ZX-441", "aliases": ["zx441", "zx 441"]},
    {"id": "beta_budget", "question": "What is Project Beta's approved budget?",
     "answer": "$2.4M", "aliases": ["2.4m", "2.4 million", "$2.4 million"]},
    {"id": "beta_lead", "question": "Who leads the team that owns Project Beta?",
     "answer": "Mira Chen", "aliases": ["mira"]},
    {"id": "revenue_total", "question": "What was total revenue across Q3 and Q4?",
     "answer": "$2.7M", "aliases": ["2.7m", "2.7 million", "$2.7 million"]},
    {"id": "orion_end", "question": "When does vendor Orion's contract end?",
     "answer": "2027-03-15", "aliases": ["march 15, 2027", "2027-03-15", "15 march 2027"]},
    {"id": "omega_budget", "question": "What is Project Omega's budget?",
     "answer": "$9.9M", "aliases": ["9.9m", "9.9 million", "$9.9 million"]},
]


# Three-hop questions: each answer requires chaining at least three pages.
HARD_QUESTIONS = [
    {"id": "vendor_lead", "question": "Who leads the vendor whose contract ends 2027-03-15?",
     "answer": "Mira Chen", "aliases": ["mira"],
     "chain": "vendors → contracts → people"},
    {"id": "alpha_code_chain",
     "question": "What is the launch code of the project owned by the team that manages vendor Lyra?",
     "answer": "ZX-441", "aliases": ["zx441", "zx 441"],
     "chain": "contracts → teams → alpha"},
    {"id": "beta_vendor", "question": "Which vendor is managed by the team that owns Project Beta?",
     "answer": "Orion", "aliases": ["vendor orion"],
     "chain": "beta/teams → contracts"},
    {"id": "revenue_growth", "question": "By how much did revenue grow from Q3 to Q4?",
     "answer": "$0.3M", "aliases": ["0.3m", "300k", "$300,000", "0.3 million"],
     "chain": "metrics (difference)"},
]


def write_cache() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    wanted = {p.replace("/", "__") + ".txt" for p in PAGES}
    for stale in CACHE.glob("*.txt"):
        if stale.name not in wanted:
            stale.unlink()
    for path, text in PAGES.items():
        target = CACHE / (path.replace("/", "__") + ".txt")
        if not target.exists():
            target.write_text(text, encoding="utf-8")


def load_pages() -> dict[str, str]:
    if CACHE.exists():
        pages = {}
        for f in CACHE.glob("*.txt"):
            pages[f.name.removesuffix(".txt").replace("__", "/")] = f.read_text(encoding="utf-8")
        if pages:
            return pages
    return dict(PAGES)


def normalize(text: str) -> str:
    text = text.strip().lower()
    text = text.replace(",", "").replace("$", "").replace("usd", "").strip()
    text = re.sub(r"\s+", " ", text)
    return text


REFUSAL_MARKERS = ("not stated", "not mentioned", "no authoritative", "cannot determine",
                   "not found", "unknown", "no document", "does not state")


def answer_matches(got: str, question: dict) -> bool:
    """Short-answer matching that rejects hedged/negated replies.

    A refusal that happens to quote the decoy value must not count as correct, so we
    require the reply to be essentially just the value (exact, or the value plus a few
    filler words such as 'it is').
    """
    norm = normalize(got)
    if any(marker in norm for marker in REFUSAL_MARKERS):
        return False
    # drop a trailing/embedded parenthetical gloss, e.g. "$9.9M (proposed budget)"
    bare = re.sub(r"\([^)]*\)", " ", norm)
    bare = re.sub(r"\s+", " ", bare).strip()
    variants = {norm, bare}
    candidates = [question["answer"], *question.get("aliases", [])]
    for candidate in candidates:
        cand = normalize(candidate)
        if not cand:
            continue
        for variant in variants:
            if variant == cand:
                return True
            if cand in variant and len(variant) <= len(cand) + 16:
                return True
    return False


def extract_answer(text: str) -> str:
    m = re.search(r"ANSWER\s*[:=]\s*(.+)", text or "", re.I)
    if m:
        return m.group(1).strip().splitlines()[0].strip(" .`*")
    lines = [l.strip() for l in (text or "").strip().splitlines() if l.strip()]
    return lines[-1].strip(" .`*") if lines else ""


def serve(pages: dict[str, str]) -> tuple[ThreadingHTTPServer, str]:
    index = "DOCUMENT INDEX\n" + "\n".join(f"- /{p}" for p in sorted(pages))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.strip("/").split("?")[0]
            text = index if path == "docs" else pages.get(path)
            if text is None:
                body = b"not found"
                self.send_response(404)
            else:
                body = text.encode()
                self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{port}"


COVERAGE_INSTRUCTIONS = (
    "\nBefore searching, call plan_research with the question and one sub-question per hop. "
    "For each page you fetch, record the relevant text with add_source, then call mark_covered for every "
    "sub-question that page answers. You cannot finish until every sub-question is covered."
)


def prompt(base: str, question: str, coverage: bool = False) -> str:
    text = (f"Research question: {question}\n\n"
            f"Start by fetching {base}/docs — it is an index listing every page. Fetch the pages you need. "
            f"Some pages are stale drafts; prefer the most recent authoritative page. Combine sources when "
            f"the answer needs two steps. Do not guess URLs that are not in the index.\n"
            f"Reply with a short explanation and then a final line exactly 'ANSWER: <value>'.")
    return text + (COVERAGE_INSTRUCTIONS if coverage else "")


async def run_question(cfg, base: str, q: dict, timeout: float, coverage: bool = False) -> dict:
    started = time.monotonic()
    tools = {"fetch_url"}
    if coverage:
        tools |= {"plan_research", "mark_covered", "add_source", "list_sources"}
    async with Agent(cfg, max_iterations=18 if coverage else 14, allow_tools=tools) as agent:
        try:
            result = await asyncio.wait_for(agent.run(prompt(base, q["question"], coverage)),
                                            timeout=timeout)
            out, stop = result.output, result.stop_reason
            tokens, iters = result.usage.total_tokens, result.iterations
        except asyncio.TimeoutError:
            out, stop, tokens, iters = "", "timeout", 0, 0
    got = extract_answer(out)
    plan = (agent.tool_ctx.extra.get("research_plan") if agent.tool_ctx else None)
    return {"id": q["id"], "question": q["question"], "expected": q["answer"], "got": got,
            "correct": answer_matches(got, q), "stop": stop, "iterations": iters, "tokens": tokens,
            "coverage": plan.stats() if plan is not None else None,
            "answer_text": out[-500:], "seconds": round(time.monotonic() - started, 1)}


async def main_async(args) -> int:
    write_cache()
    pages = load_pages()
    srv, base = serve(pages)
    cfg = load_config({"workspace": str(Path("/tmp") / "argo_deep_research")})
    cfg.session_dir = Path("/tmp/argo_deep_research/sessions")
    cfg.ensure_dirs()
    bank = HARD_QUESTIONS if args.set == "hard" else QUESTIONS
    questions = bank if args.set == "hard" else bank[: max(1, args.questions)]
    if args.ids:
        wanted = {x.strip() for x in args.ids.split(",") if x.strip()}
        questions = [q for q in bank if q["id"] in wanted]
    rows = []
    try:
        for q in questions:
            row = await run_question(cfg, base, q, args.timeout, coverage=args.coverage)
            rows.append(row)
            cov = f" cov={row['coverage']['coverage']:.2f}" if row.get("coverage") else ""
            print(f"{row['id']:<17} {'OK ' if row['correct'] else 'MISS'} got={row['got'][:26]!r:<30} "
                  f"stop={row['stop']:<10} tok={row['tokens']:<6} {row['seconds']:.0f}s{cov}", flush=True)
    finally:
        srv.shutdown()
    correct = sum(r["correct"] for r in rows)
    summary = {"benchmark": "deep-research-exact-answer", "set": args.set, "coverage": args.coverage,
               "questions": len(rows), "correct": correct,
               "accuracy": round(correct / len(rows), 4),
               "tokens": sum(r["tokens"] for r in rows), "rows": rows,
               "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"deep_research_{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\naccuracy={summary['accuracy']:.3f} ({correct}/{len(rows)}) tokens={summary['tokens']:,}")
    print(f"Saved: {out.relative_to(ROOT)}")
    return 0


def self_check() -> int:
    write_cache()
    pages = load_pages()
    assert set(pages) >= set(PAGES), (sorted(pages), sorted(PAGES))
    assert all(q["answer"] and q["question"] for q in HARD_QUESTIONS)
    assert answer_matches("$0.3M", [q for q in HARD_QUESTIONS if q["id"] == "revenue_growth"][0])
    srv, base = serve(pages)
    try:
        import urllib.request
        body = urllib.request.urlopen(f"{base}/docs/alpha", timeout=5).read().decode()
        assert "ZX-441" in body
        try:
            urllib.request.urlopen(f"{base}/docs/missing", timeout=5)
            raise AssertionError("missing page should 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        srv.shutdown()
    assert answer_matches(extract_answer("reasoning...\nANSWER: ZX-441."), QUESTIONS[0])
    assert answer_matches("2.4 million", QUESTIONS[1])
    assert not answer_matches("QQ-000", QUESTIONS[0])
    # a refusal that quotes the decoy value is still wrong
    assert not answer_matches(
        "Not stated in any authoritative document (decoy claims $9.9M)", QUESTIONS[5])
    assert not answer_matches("The answer is not mentioned; maybe 2.4M", QUESTIONS[1])
    assert answer_matches("The answer is $2.4M", QUESTIONS[1])
    assert answer_matches("$9.9M (proposed budget)", QUESTIONS[5])
    assert answer_matches("It is 2027-03-15 (contract end).", QUESTIONS[4])
    assert not answer_matches("$2.4M in Q3 and $3.3M in Q4 for a total of $5.7M", QUESTIONS[1])
    assert extract_answer("ANSWER: **Mira Chen**") .strip("*") == "Mira Chen"
    print(f"self-check ok: {len(pages)} pages, {len(QUESTIONS)} questions, scorer works")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--questions", type=int, default=6)
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--ids", default="", help="comma-separated question ids to run")
    ap.add_argument("--set", choices=["base", "hard"], default="base",
                    help="base = single/2-hop set; hard = 3-hop set")
    ap.add_argument("--coverage", action="store_true",
                    help="require plan_research decomposition + mark_covered before answering")
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args()
    if args.self_check:
        return self_check()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
