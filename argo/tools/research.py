"""Research tools backed by the evidence ledger (R1) and citation verifier (R2)."""
from __future__ import annotations

from ..core.types import ToolResult
from ..research.ledger import EvidenceLedger
from ..research.plan import CoveragePlan
from ..research.verify import verify_ledger
from .base import ToolContext, ToolRegistry


def _ledger(ctx: ToolContext) -> EvidenceLedger:
    return ctx.extra.setdefault("ledger", EvidenceLedger())


def _add_source(ctx: ToolContext, url: str, content: str, title: str = "") -> ToolResult:
    ledger = _ledger(ctx)
    src = ledger.add_source(url, content, title)
    ctx.emit("research_source", source=src.id, url=src.url)
    return ToolResult(f"Recorded source [{src.id}] {src.url} ({len(src.text)} chars). Cite it as {src.id}.",
                      metadata={"source_id": src.id})


def _add_claim(ctx: ToolContext, text: str, source_ids: list[str], quote: str = "") -> ToolResult:
    ledger = _ledger(ctx)
    missing = [sid for sid in source_ids if sid not in ledger.sources]
    if missing:
        return ToolResult(f"Unknown source ids: {', '.join(missing)}. Record sources first.", is_error=True)
    claim = ledger.add_claim(text, source_ids, quote)
    ctx.emit("research_claim", claim=claim.id, citations=claim.citations)
    return ToolResult(f"Recorded claim [{claim.id}] citing {', '.join(claim.citations)}.",
                      metadata={"claim_id": claim.id})


async def _verify(ctx: ToolContext) -> ToolResult:
    ledger = _ledger(ctx)
    if not ledger.claims:
        return ToolResult("No claims recorded; add claims before verifying.", is_error=True)
    llm = ctx.extra.get("llm")
    if llm is None:
        return ToolResult("Verifier needs an LLM client (missing in this context).", is_error=True)
    stats = await verify_ledger(ledger, llm)
    rows = [f"{c.id} [{c.verdict}] {c.text[:120]}" for c in ledger.claims.values()]
    body = ("Verification results:\n" + "\n".join(rows) +
            f"\n\nstats: {stats}\n"
            "Rewrite or demote anything not marked supported; never cite an id that is not in the ledger.")
    return ToolResult(body, metadata=stats)


def _report(ctx: ToolContext, title: str = "Research report") -> ToolResult:
    ledger = _ledger(ctx)
    if not ledger.claims:
        return ToolResult("Nothing to report yet.", is_error=True)
    return ToolResult(ledger.to_markdown(title), metadata=ledger.stats())


def _plan(ctx: ToolContext, question: str, subquestions: list[str]) -> ToolResult:
    existing = ctx.extra.get("research_plan")
    if isinstance(existing, CoveragePlan) and not existing.complete:
        return ToolResult(
            f"A plan is already active with {len(existing.uncovered())} uncovered sub-question(s); "
            f"finish or replace it.\n{existing.render()}", is_error=True)
    try:
        plan = CoveragePlan.from_texts(question, subquestions)
    except ValueError as exc:
        return ToolResult(f"Cannot build plan: {exc}", is_error=True)
    ctx.extra["research_plan"] = plan
    ctx.emit("research_plan", question=question, sub_questions=len(plan.subs))
    return ToolResult(f"Saved coverage plan. Mark each sub-question covered before answering.\n{plan.render()}",
                      metadata=plan.stats())


def _cover(ctx: ToolContext, sub_id: str, source_ids: list[str]) -> ToolResult:
    plan = ctx.extra.get("research_plan")
    if not isinstance(plan, CoveragePlan):
        return ToolResult("No active plan; call plan_research first.", is_error=True)
    if plan.get(sub_id) is None:
        return ToolResult(f"Unknown sub-question {sub_id!r}; valid ids: {', '.join(s.id for s in plan.subs)}.",
                          is_error=True)
    ledger = _ledger(ctx)
    unknown = [sid for sid in source_ids if sid not in ledger.sources]
    if unknown:
        return ToolResult(f"Unknown source ids: {', '.join(unknown)}. Record them with add_source first.",
                          is_error=True)
    sub = plan.mark(sub_id, source_ids)
    ctx.emit("research_cover", sub_question=sub_id, sources=sub.covered_by)
    return ToolResult(f"Marked {sub_id} covered by {', '.join(sub.covered_by)}.\n{plan.render()}",
                      metadata=plan.stats())


def _sources(ctx: ToolContext) -> ToolResult:
    return ToolResult(_ledger(ctx).render_sources())


def register(reg: ToolRegistry) -> None:
    reg.add("add_source",
            "Record a fetched source in the evidence ledger before using it. Returns a stable id (S1, S2, ...) "
            "to cite. Deduplicates by URL.",
            {"type": "object", "properties": {
                "url": {"type": "string"},
                "content": {"type": "string", "description": "Relevant text from the source (verbatim)."},
                "title": {"type": "string"}},
             "required": ["url", "content"]},
            _add_source, read_only=True, category="research")
    reg.add("add_claim",
            "Record one factual claim with the source ids it relies on. Every claim in the final report must "
            "exist here; a claim can only cite ids returned by add_source.",
            {"type": "object", "properties": {
                "text": {"type": "string"},
                "source_ids": {"type": "array", "items": {"type": "string"}},
                "quote": {"type": "string", "description": "Verbatim span that supports the claim."}},
             "required": ["text", "source_ids"]},
            _add_claim, read_only=True, category="research")
    reg.add("verify_claims",
            "Check every recorded claim against its cited source. Returns per-claim verdicts and support-rate "
            "stats; fix or drop claims that are not 'supported' before writing the report.",
            {"type": "object", "properties": {}}, _verify, read_only=True, timeout=600, category="research")
    reg.add("research_report",
            "Render the citation-checked report from the ledger (markdown with source ids and verdict marks).",
            {"type": "object", "properties": {"title": {"type": "string"}}}, _report,
            read_only=True, category="research")
    reg.add("list_sources", "Show recorded sources and their ids.", {"type": "object", "properties": {}},
            _sources, read_only=True, category="research")
    reg.add("plan_research",
            "Decompose a multi-hop research question into sub-questions before searching. The run cannot finish "
            "until every sub-question is marked covered with recorded source ids.",
            {"type": "object", "properties": {
                "question": {"type": "string"},
                "subquestions": {"type": "array", "items": {"type": "string"},
                                 "description": "One atomic fact-seeking sub-question per entry."}},
             "required": ["question", "subquestions"]},
            _plan, read_only=True, category="research")
    reg.add("mark_covered",
            "Mark a plan sub-question as answered by source ids already recorded with add_source.",
            {"type": "object", "properties": {
                "sub_id": {"type": "string", "description": "Q1, Q2, ... from the plan."},
                "source_ids": {"type": "array", "items": {"type": "string"}}},
             "required": ["sub_id", "source_ids"]},
            _cover, read_only=True, category="research")
