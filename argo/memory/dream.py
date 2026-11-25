"""Dreaming: offline memory consolidation over session transcripts.

Modeled on Anthropic's described memory hierarchy and "Dreams" feature:

  transcripts -> notes -> representations (wiki) -> injected context

A dream:
  1. reads the active memory generation + recent session transcripts,
  2. extracts candidate notes/insights/procedures,
  3. reconciles them against existing notes (dedupe, merge, supersede, archive),
  4. grounds claims in transcript evidence (unverified claims get lower confidence),
  5. writes a *new* generation (input is never mutated),
  6. optionally activates the new generation.

Activation is separate so the output can be reviewed and discarded.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .store import MemoryItem, MemoryStore

SYSTEM = """You are the offline consolidation ("dreaming") process of an AI agent's memory system.

You receive:
- CURRENT MEMORY: the agent's active memory items (with IDs), accumulated incrementally during work.
- TRANSCRIPTS: recent session logs, which are ground truth for what actually happened.
- OPTIONAL INSTRUCTIONS: focus areas from the operator.

Your job is to produce a cleaner, smaller, more useful memory generation:
1. Verify existing notes against transcripts. If a note is contradicted by newer evidence, correct or supersede it.
2. Merge duplicates and near-duplicates into one canonical note. Prefer fewer, denser notes.
3. Record only durable, reusable knowledge: user preferences, verified facts, decisions and their rationale,
   environment/config facts, rules that worked, pitfalls and traps, procedures. Do NOT record transient state,
   one-off debugging noise, or anything visible/derivable at runtime.
4. Surface genuinely new cross-session insights (patterns, hypotheses) with calibrated confidence.
5. Build or update compact topic pages ("wiki") that index related knowledge and link related topics.
6. Extract reusable procedures (when_to_use + ordered steps) from workflows that succeeded.
7. Record a small number of durable user/profile facts.

Rules:
- Ground every factual claim with a short verbatim quote in "evidence" copied from the transcripts.
  If you cannot ground it, still report it but set confidence <= 0.5 and evidence="".
- Confidence: 0.9+ directly observed and verified; 0.7 observed once; 0.5 inferred/unverified; <0.4 speculation.
- Importance: 0.8+ affects future work broadly; 0.5 useful context; 0.2 trivia.
- Be ruthless about size. A small, current memory beats a large stale archive.

Return ONLY valid JSON with this shape:
{
  "observations": [{"text": "...", "kind": "fact|preference|decision|lesson|rule|pitfall|event",
                    "importance": 0.0, "confidence": 0.0, "evidence": "verbatim quote", "tags": ["..."]}],
  "profile": [{"text": "...", "confidence": 0.0, "evidence": "..."}],
  "reconcile": [{"target_ids": ["m_..."], "action": "merge|supersede|archive",
                 "new_text": "...", "reason": "...", "confidence": 0.0}],
  "wiki": [{"title": "...", "text": "...", "links": ["other title"], "source_ids": ["m_..."]}],
  "insights": [{"text": "...", "confidence": 0.0, "basis": "...", "tags": ["..."]}],
  "procedures": [{"title": "...", "when_to_use": "...", "steps": ["..."], "confidence": 0.0}],
  "summary": "2-4 sentence summary of what changed and why"
}
"""


@dataclass
class DreamReport:
    id: str
    input_generation: int
    output_generation: int
    status: str
    summary: str = ""
    added: int = 0
    merged: int = 0
    superseded: int = 0
    archived: int = 0
    wiki: int = 0
    insights: int = 0
    procedures: int = 0
    unverified: int = 0
    sessions: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def to_markdown(self) -> str:
        lines = [
            f"# Dream {self.id}",
            "",
            f"- Status: **{self.status}**",
            f"- Generation: {self.input_generation} → **{self.output_generation}**",
            f"- Sessions reviewed: {len(self.sessions)}",
            f"- Added notes: {self.added} | Merged: {self.merged} | Superseded: {self.superseded} | Archived: {self.archived}",
            f"- Wiki pages: {self.wiki} | Insights: {self.insights} | Procedures: {self.procedures}",
            f"- Unverified claims downgraded: {self.unverified}",
            "",
            "## Summary",
            self.summary or "(none)",
        ]
        if self.details.get("insights"):
            lines += ["", "## Insights surfaced"] + [f"- {i['text']}  _(confidence {i.get('confidence', 0):.2f})_"
                                                       for i in self.details["insights"]]
        if self.details.get("procedures"):
            lines += ["", "## Procedures learned"] + [f"- **{p['title']}** — {p.get('when_to_use', '')}"
                                                       for p in self.details["procedures"]]
        if self.details.get("warnings"):
            lines += ["", "## Warnings"] + [f"- {w}" for w in self.details["warnings"]]
        return "\n".join(lines) + "\n"


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, depth, in_str, esc = -1, 0, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        start = -1
    raise ValueError("no JSON object found in model output")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _grounded(evidence: str, corpus: str) -> bool:
    ev = _norm(evidence)
    if len(ev) < 12:
        return False
    return ev in corpus


class DreamEngine:
    def __init__(self, store: MemoryStore, llm: Any, cfg: Any):
        self.store = store
        self.llm = llm
        self.cfg = cfg

    def _transcript_digest(self, session_ids: Sequence[str], char_budget: int = 60_000) -> tuple[str, list[str]]:
        rows = self.store.transcripts(session_ids=list(session_ids), limit=4000)
        per_session: dict[str, list[str]] = {}
        for r in rows:
            content = re.sub(r"\n{3,}", "\n\n", r["content"] or "").strip()
            if len(content) > 2500:
                content = content[:1800] + "\n…[truncated]…\n" + content[-500:]
            per_session.setdefault(r["session_id"], []).append(f"[{r['role']}] {content}")
        chunks: list[str] = []
        used = 0
        used_sessions: list[str] = []
        for sid in reversed(list(per_session)):
            body = "\n".join(per_session[sid])
            if used + len(body) > char_budget:
                body = body[: max(0, char_budget - used)]
            if not body.strip():
                break
            chunks.append(f"\n===== SESSION {sid} =====\n{body}")
            used_sessions.append(sid)
            used += len(body)
            if used >= char_budget:
                break
        return "\n".join(reversed(chunks)), list(reversed(used_sessions))

    def _memory_digest(self, items: list[MemoryItem], budget: int = 24_000) -> str:
        lines = []
        for it in items:
            lines.append(f"[{it.id}] ({it.layer}/{it.kind}, imp={it.importance:.1f}, conf={it.confidence:.1f}) "
                         f"{it.title}: {it.text[:600]}")
        out = "\n".join(lines)
        return out[:budget]

    # ------------------------------------------------------------------- run
    async def run(self, session_ids: Sequence[str] | None = None, instructions: str = "",
                  apply: bool = False, model: str | None = None, max_sessions: int = 30) -> DreamReport:
        active = self.store.active_generation
        out_gen = active + 1
        ids = list(session_ids) if session_ids else self.store.session_ids(limit=max_sessions)
        digest, used_sessions = self._transcript_digest(ids)
        items = self.store.list_items(status="active", limit=2000)
        report_id = f"drm_{uuid.uuid4().hex[:10]}"
        report = DreamReport(id=report_id, input_generation=active, output_generation=out_gen,
                             status="running", sessions=used_sessions)
        self.store.db.execute("""INSERT INTO dreams(id,created_at,status,input_generation,output_generation,
                                instructions,summary,report,sessions) VALUES(?,?,?,?,?,?,?,?,?)""",
                              (report_id, time.time(), "running", active, out_gen, instructions, "",
                               "{}", json.dumps(used_sessions)))
        self.store.db.commit()

        if not digest.strip():
            report.status = "skipped"
            report.summary = "No transcripts available to consolidate."
            self._finish(report)
            return report

        user = (f"CURRENT MEMORY (generation {active}, {len(items)} items):\n{self._memory_digest(items)}\n\n"
                f"TRANSCRIPTS:\n{digest}\n\n"
                + (f"OPERATOR INSTRUCTIONS: {instructions}\n" if instructions else "")
                + f"Produce the consolidated memory for generation {out_gen}. JSON only.")
        from ..core.types import Message
        resp = await self.llm.chat(
            [Message(role="system", content=SYSTEM), Message(role="user", content=user)],
            model=model, temperature=0.2, max_tokens=min(16000, self.cfg.models.max_output_tokens),
            response_format={"type": "json_object"},
        )
        raw = resp.message.content or resp.message.reasoning or ""
        warnings: list[str] = []
        try:
            plan = _extract_json(raw)
        except ValueError as exc:
            report.status = "failed"
            report.summary = f"Could not parse dream JSON: {exc}"
            self._finish(report)
            return report

        corpus = _norm(digest)
        # ---------------------------------------------------------- clone input
        self.store.db.execute("DELETE FROM memory_items WHERE generation=?", (out_gen,))
        id_map: dict[str, str] = {}
        clone = self.store.db.execute("SELECT * FROM memory_items WHERE generation=? AND status='active'", (active,)).fetchall()
        for row in clone:
            new_id = f"m_{uuid.uuid4().hex[:12]}"
            id_map[row["id"]] = new_id
            self.store.db.execute("""INSERT INTO memory_items(id,layer,kind,title,text,tags,importance,confidence,
                status,source,source_ref,evidence,created_at,updated_at,last_accessed,access_count,generation,
                superseded_by,hash,embedding) VALUES(?,?,?,?,?,?,?,?,'active',?,?,?,?,?,?,?,?,NULL,?,?)""",
                                  (new_id, row["layer"], row["kind"], row["title"], row["text"], row["tags"],
                                   row["importance"], row["confidence"], row["source"], row["source_ref"],
                                   row["evidence"], row["created_at"], time.time(), row["last_accessed"],
                                   row["access_count"], out_gen, row["hash"], row["embedding"]))
        self.store.db.commit()

        existing_ids = set(id_map.values())
        new_items: list[tuple[str, str, str, float, float, str, str]] = []  # text, layer, kind, imp, conf, evidence, tags
        reconcile_actions = plan.get("reconcile") or []
        merged_targets: set[str] = set()

        for action in reconcile_actions:
            targets = [id_map.get(t, t) for t in action.get("target_ids", [])]
            act = (action.get("action") or "").lower()
            if act == "archive":
                for t in targets:
                    self.store.db.execute("UPDATE memory_items SET status='archived',updated_at=? WHERE id=?", (time.time(), t))
                    report.archived += 1
            elif act in ("supersede", "merge"):
                new_text = (action.get("new_text") or "").strip()
                if new_text and targets:
                    mid = await self.store.add(new_text, layer="note", kind="fact",
                                               importance=max([0.5] + [i.importance for i in items if i.id in action.get("target_ids", [])]),
                                               confidence=float(action.get("confidence", 0.7)),
                                               source="dream", source_ref=report_id, generation=out_gen,
                                               embed_now=False)
                    existing_ids.add(mid)
                    for t in targets:
                        self.store.db.execute("UPDATE memory_items SET status='superseded',superseded_by=?,updated_at=? WHERE id=?",
                                              (mid, time.time(), t))
                        merged_targets.add(t)
                        report.merged += 1
                        if act == "supersede":
                            report.superseded += 1
                elif targets:
                    for t in targets:
                        self.store.db.execute("UPDATE memory_items SET status='archived',updated_at=? WHERE id=?", (time.time(), t))
                        report.archived += 1

        async def add_checked(text: str, layer: str, kind: str, importance: float, confidence: float,
                              evidence: str, tags: list[str] | None = None, title: str = "", extra: str = "") -> str:
            text = (text or "").strip()
            if not text:
                return ""
            conf = max(0.0, min(1.0, float(confidence or 0.5)))
            if evidence and not _grounded(evidence, corpus):
                conf = min(conf, 0.45)
                report.unverified += 1
            elif not evidence and conf > 0.5:
                conf = 0.5
            body = text + (f"\n\nEvidence: “{evidence}”" if evidence else (f"\n\n{extra}" if extra else ""))
            mid = await self.store.add(body, layer=layer, kind=kind, title=title or text.split("\n")[0][:80],
                                       tags=tags or [], importance=max(0.0, min(1.0, float(importance or 0.5))),
                                       confidence=conf, source="dream", source_ref=report_id,
                                       evidence=evidence, generation=out_gen, embed_now=False)
            existing_ids.add(mid)
            return mid

        # batch embeddings later to avoid one round-trip per item
        for obs in plan.get("observations") or []:
            await add_checked(obs.get("text", ""), "note", obs.get("kind", "fact"),
                              obs.get("importance", 0.5), obs.get("confidence", 0.6),
                              obs.get("evidence", ""), obs.get("tags"))
            report.added += 1
        for prof in plan.get("profile") or []:
            await add_checked(prof.get("text", ""), "profile", "profile", 0.9,
                              prof.get("confidence", 0.7), prof.get("evidence", ""))
        for ins in plan.get("insights") or []:
            await add_checked(ins.get("text", ""), "note", "insight", 0.7,
                              ins.get("confidence", 0.5), "", ins.get("tags"),
                              title="Insight: " + (ins.get("text", "")[:60]),
                              extra=f"Basis: {ins.get('basis', '')}" if ins.get("basis") else "")
            report.insights += 1

        title_to_id: dict[str, str] = {}
        for w in plan.get("wiki") or []:
            text = (w.get("text") or "").strip()
            if not text:
                continue
            wid = await self.store.add(text, layer="wiki", kind="topic", title=w.get("title", "Topic")[:120],
                                       importance=0.75, confidence=0.75, source="dream",
                                       source_ref=report_id, generation=out_gen, embed_now=False)
            existing_ids.add(wid)
            title_to_id[_norm(w.get("title", ""))] = wid
            for sid in w.get("source_ids", []):
                mapped = id_map.get(sid, sid)
                if mapped in existing_ids:
                    self.store.link(wid, mapped, "cites")
            report.wiki += 1
        for w in plan.get("wiki") or []:
            src = title_to_id.get(_norm(w.get("title", "")))
            for link_title in w.get("links", []):
                dst = title_to_id.get(_norm(link_title))
                if src and dst and src != dst:
                    self.store.link(src, dst, "related")

        for proc in plan.get("procedures") or []:
            steps = proc.get("steps") or []
            if not steps:
                continue
            body = f"When to use: {proc.get('when_to_use', '')}\nSteps:\n" + "\n".join(
                f"{i}. {s}" for i, s in enumerate(steps, 1))
            await self.store.add(body, layer="procedure", kind="procedure",
                                 title=proc.get("title", "Procedure")[:120], importance=0.8,
                                 confidence=float(proc.get("confidence", 0.7)), source="dream",
                                 source_ref=report_id, generation=out_gen, embed_now=False)
            report.procedures += 1

        if self.store.embed:
            await self.store.embed_pending(out_gen)
        dups = self.store.dedupe(out_gen)
        if dups:
            warnings.append(f"dedupe archived {dups} near-duplicate item(s)")
        # copy links from prior generation
        self.store.db.commit()
        report.summary = plan.get("summary", "")
        report.details = {"insights": plan.get("insights", []), "procedures": plan.get("procedures", []),
                          "warnings": warnings}
        report.status = "completed"
        if apply:
            self.store._set_meta("active_generation", str(out_gen))
            report.status = "applied"
        self._finish(report)
        return report

    def _finish(self, report: DreamReport) -> None:
        self.store.db.execute("UPDATE dreams SET status=?, summary=?, report=? WHERE id=?",
                              (report.status, report.summary,
                               json.dumps({"added": report.added, "merged": report.merged,
                                           "superseded": report.superseded, "archived": report.archived,
                                           "wiki": report.wiki, "insights": report.insights,
                                           "procedures": report.procedures, "unverified": report.unverified,
                                           "details": report.details}), report.id))
        self.store.db.commit()

    def list_dreams(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.store.db.execute("SELECT * FROM dreams ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def rollback(self, generation: int) -> None:
        """Activate any prior generation (dreams are reversible by construction)."""
        self.store._set_meta("active_generation", str(generation))
