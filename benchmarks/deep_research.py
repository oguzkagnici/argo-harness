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
    "docs/distractor": "Project Omega budget is $9.9M. Project Omega is unrelated to Alpha or Beta.",
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


def write_cache() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
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


def answer_matches(got: str, question: dict) -> bool:
    norm = normalize(got)
    candidates = [question["answer"], *question.get("aliases", [])]
    return any(normalize(c) == norm or normalize(c) in norm for c in candidates)


def extract_answer(text: str) -> str:
    m = re.search(r"ANSWER\s*[:=]\s*(.+)", text or "", re.I)
    if m:
        return m.group(1).strip().splitlines()[0].strip(" .`*")
    lines = [l.strip() for l in (text or "").strip().splitlines() if l.strip()]
    return lines[-1].strip(" .`*") if lines else ""


def serve(pages: dict[str, str]) -> tuple[ThreadingHTTPServer, str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.strip("/").split("?")[0]
            text = pages.get(path)
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


def prompt(base: str, question: str) -> str:
    return (f"Research question: {question}\n\n"
            f"Pages live under {base}/docs/<name>. Discover them with fetch_url on {base}/docs/alpha, "
            f"{base}/docs/beta, {base}/docs/teams, {base}/docs/people, {base}/docs/metrics, "
            f"{base}/docs/vendors and {base}/docs/distractor. Some pages are stale drafts; prefer the "
            f"most recent authoritative page. Combine sources when the answer needs two steps.\n"
            f"Reply with a short explanation and then a final line exactly 'ANSWER: <value>'.")


async def run_question(cfg, base: str, q: dict, timeout: float) -> dict:
    started = time.monotonic()
    cfg = cfg
    async with Agent(cfg, max_iterations=14, allow_tools={"fetch_url"}) as agent:
        try:
            result = await asyncio.wait_for(agent.run(prompt(base, q["question"])), timeout=timeout)
            out, stop = result.output, result.stop_reason
            tokens, iters = result.usage.total_tokens, result.iterations
        except asyncio.TimeoutError:
            out, stop, tokens, iters = "", "timeout", 0, 0
    got = extract_answer(out)
    return {"id": q["id"], "question": q["question"], "expected": q["answer"], "got": got,
            "correct": answer_matches(got, q), "stop": stop, "iterations": iters, "tokens": tokens,
            "answer_text": out[-500:], "seconds": round(time.monotonic() - started, 1)}


async def main_async(args) -> int:
    write_cache()
    pages = load_pages()
    srv, base = serve(pages)
    cfg = load_config({"workspace": str(Path("/tmp") / "argo_deep_research")})
    cfg.session_dir = Path("/tmp/argo_deep_research/sessions")
    cfg.ensure_dirs()
    questions = QUESTIONS[: max(1, args.questions)]
    rows = []
    try:
        for q in questions:
            row = await run_question(cfg, base, q, args.timeout)
            rows.append(row)
            print(f"{row['id']:<13} {'OK ' if row['correct'] else 'MISS'} got={row['got'][:30]!r:<34} "
                  f"stop={row['stop']:<10} tok={row['tokens']:<6} {row['seconds']:.0f}s", flush=True)
    finally:
        srv.shutdown()
    correct = sum(r["correct"] for r in rows)
    summary = {"benchmark": "deep-research-exact-answer", "questions": len(rows), "correct": correct,
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
    assert extract_answer("ANSWER: **Mira Chen**") .strip("*") == "Mira Chen"
    print(f"self-check ok: {len(pages)} pages, {len(QUESTIONS)} questions, scorer works")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--questions", type=int, default=6)
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args()
    if args.self_check:
        return self_check()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
