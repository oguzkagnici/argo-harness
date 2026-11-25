"""Document QA benchmark: structure-aware read_document vs manual parsing.

A workspace is generated with known facts in DOCX (headings, bullets, a table), PPTX (slides), XLSX
(sheet) and PDF. Ten questions have short exact answers. Each question is answered by a fresh agent in
two modes:

  tool : read_document, list_dir, grep_files, read_file
  bash : bash, run_code, list_dir, read_file   (agent must parse the formats itself)

This measures whether the document layer earns its keep, and gives per-format accuracy.

Usage:
  python benchmarks/document_qa.py --self-check
  python benchmarks/document_qa.py --modes tool,bash
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from argo.config import load_config  # noqa: E402
from argo.core.agent import Agent  # noqa: E402
from argo.tools.base import ToolContext  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"

QUESTIONS = [
    ("docx_throughput", "dsx", "What throughput did Q3 reach (req/s)?", "4800"),
    ("docx_error", "dsx", "What was the Q3 error rate?", "0.3%"),
    ("docx_eu_revenue", "dsx", "What revenue did the eu-west region report?", "$120k"),
    ("docx_ap_uptime", "dsx", "What uptime did the ap-south region report?", "99.80%"),
    ("pptx_launch", "dsx", "What is the launch date in the roadmap deck?", "2027-03-15"),
    ("pptx_owner", "dsx", "Who owns the launch according to the deck?", "Team Beacon"),
    ("xlsx_latency", "dsx", "What is p95_latency_ms in the metrics workbook?", "180"),
    ("xlsx_cache", "dsx", "What is the cache_hit_ratio in the metrics workbook?", "0.91"),
    ("pdf_retention", "dsx", "How many days is the data retention period in the policy?", "45"),
    ("pdf_owner", "dsx", "Who owns the security policy document?", "Security Team"),
]

PROMPT = ("Answer the question using only the files in this workspace. Reply with the exact value and "
          "nothing else; if the value is not present, reply UNKNOWN.\n\nQuestion: {q}")


def build_documents(ws: Path) -> None:
    from docx import Document
    from pptx import Presentation
    from pptx.util import Inches
    from openpyxl import Workbook
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    doc = Document()
    doc.add_heading("Q3 Operations Review", level=0)
    doc.add_heading("Reliability", level=1)
    doc.add_paragraph("Throughput reached 4800 req/s in Q3.")
    doc.add_paragraph("Error rate fell to 0.3% after the cache change.")
    doc.add_heading("Regional finance", level=1)
    table = doc.add_table(rows=4, cols=3)
    for row, values in zip(table.rows, [("Region", "Revenue", "Uptime"),
                                        ("eu-west", "$120k", "99.95%"),
                                        ("us-east", "$180k", "99.90%"),
                                        ("ap-south", "$80k", "99.80%")]):
        for cell, value in zip(row.cells, values):
            cell.text = value
    doc.save(str(ws / "q3_report.docx"))

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "Launch roadmap"
    body = slide.placeholders[1].text_frame
    body.text = "Launch: 2027-03-15"
    body.add_paragraph().text = "Owner: Team Beacon"
    body.add_paragraph().text = "Scope: three regions"
    slide2 = prs.slides.add_slide(prs.slide_layouts[1])
    slide2.shapes.title.text = "Risks"
    slide2.placeholders[1].text_frame.text = "Vendor lock-in is the top risk"
    prs.save(str(ws / "roadmap.pptx"))

    wb = Workbook()
    sheet = wb.active
    sheet.title = "Metrics"
    sheet.append(["Metric", "Value"])
    sheet.append(["p95_latency_ms", 180])
    sheet.append(["cache_hit_ratio", 0.91])
    sheet.append(["incidents", 2])
    wb.save(str(ws / "metrics.xlsx"))

    pdf = canvas.Canvas(str(ws / "security_policy.pdf"), pagesize=letter)
    pdf.drawString(72, 720, "Security Policy")
    pdf.drawString(72, 700, "Data retention period is 45 days.")
    pdf.drawString(72, 680, "Policy owner: Security Team")
    pdf.save()


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip().strip(".")


def grade(answer: str, expected: str) -> bool:
    a, e = norm(answer), norm(expected)
    if not a or "unknown" in a:
        return False
    if e in a:
        return True
    # tolerate formatting differences such as 0.3 vs 0.3% or 1,800 vs 1800
    return norm(e.replace(",", "")) in norm(a.replace(",", ""))


def self_check(ws: Path) -> int:
    from argo.tools.docs import _read_document
    cfg = load_config({"workspace": str(ws), "data_dir": str(ws / ".argo")})
    ctx = ToolContext(cfg=cfg)
    text = ""
    for name in ("q3_report.docx", "roadmap.pptx", "metrics.xlsx", "security_policy.pdf"):
        res = _read_document(ctx, name)
        if res.is_error:
            print("READ ERROR", name, res.content)
            return 1
        text += "\n" + res.content
    missing = [exp for _, _, _, exp in QUESTIONS if norm(exp) not in norm(text)]
    print(f"self-check: extracted {len(text)} chars; missing answers: {missing or 'none'}")
    if missing:
        print(text[:1500])
        return 1
    return 0


async def answer(cfg, mode: str, question: str, timeout: float = 180) -> dict:
    tools = ({"read_document", "list_dir", "grep_files", "read_file"} if mode == "tool"
             else {"bash", "run_code", "list_dir", "read_file"})
    started = time.monotonic()
    async with Agent(cfg, max_iterations=5, allow_tools=tools) as agent:
        try:
            result = await asyncio.wait_for(agent.run(PROMPT.format(q=question)), timeout=timeout)
            out = result.output
            return {"answer": out[:400], "tokens": result.usage.total_tokens,
                    "iterations": result.iterations, "stop": result.stop_reason,
                    "seconds": round(time.monotonic() - started, 1)}
        except asyncio.TimeoutError:
            return {"answer": "", "tokens": 0, "iterations": 0, "stop": "timeout",
                    "seconds": round(time.monotonic() - started, 1)}


async def main_async(args) -> int:
    ws = Path(tempfile.mkdtemp(prefix="argo_docqa_"))
    build_documents(ws)
    if args.self_check:
        code = self_check(ws)
        shutil.rmtree(ws, ignore_errors=True)
        return code

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    questions = QUESTIONS[: args.limit] if args.limit else QUESTIONS
    rows = []
    for mode in modes:
        for qid, fmt, question, expected in questions:
            cfg = load_config({"workspace": str(ws), "data_dir": str(ws / ".argo")})
            row = await answer(cfg, mode, question, args.timeout)
            row.update({"mode": mode, "id": qid, "question": question, "expected": expected,
                        "ok": grade(row["answer"], expected)})
            rows.append(row)
            print(f"{mode:<4} {qid:<16} ok={row['ok']!s:<5} {row['seconds']:>5.1f}s "
                  f"iter={row['iterations']} tok={row['tokens']:<6} ans={row['answer'][:60]!r}", flush=True)

    summary = {"benchmark": "document-qa-local", "rows": rows,
               "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    for mode in modes:
        group = [r for r in rows if r["mode"] == mode]
        summary[mode] = {
            "questions": len(group),
            "accuracy": round(sum(r["ok"] for r in group) / len(group), 3) if group else 0.0,
            "mean_tokens": round(sum(r["tokens"] for r in group) / len(group)) if group else 0,
            "mean_iterations": round(sum(r["iterations"] for r in group) / len(group), 2) if group else 0,
        }
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"document_qa_{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print()
    for mode in modes:
        m = summary[mode]
        print(f"{mode:<4} accuracy={m['accuracy']:.2f} ({m['questions']} q) "
              f"mean_tokens={m['mean_tokens']} mean_iter={m['mean_iterations']}")
    print(f"Saved: {out.relative_to(ROOT)}")
    shutil.rmtree(ws, ignore_errors=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--modes", default="tool,bash")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--self-check", action="store_true")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
