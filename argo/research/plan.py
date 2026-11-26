"""Coverage plan for multi-hop research (IRCoT / Self-RAG style decomposition).

A research question is decomposed into sub-questions. Each sub-question must be marked covered with
the source ids that answer it before the agent is allowed to finish. The agent loop enforces this
(the same "you have open todos" nudge pattern used by Codex/Claude Code), so coverage is a harness
guarantee rather than prompt advice.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SubQuestion:
    id: str
    text: str
    covered_by: list[str] = field(default_factory=list)

    @property
    def covered(self) -> bool:
        return bool(self.covered_by)


@dataclass
class CoveragePlan:
    question: str
    subs: list[SubQuestion] = field(default_factory=list)

    @classmethod
    def from_texts(cls, question: str, subquestions: list[str]) -> "CoveragePlan":
        cleaned = [s.strip() for s in subquestions if s and s.strip()]
        if not cleaned:
            raise ValueError("a coverage plan needs at least one sub-question")
        return cls(question=question, subs=[SubQuestion(id=f"Q{i + 1}", text=s)
                                            for i, s in enumerate(cleaned)])

    def get(self, sub_id: str) -> SubQuestion | None:
        return next((s for s in self.subs if s.id == sub_id), None)

    def mark(self, sub_id: str, source_ids: list[str]) -> SubQuestion:
        sub = self.get(sub_id)
        if sub is None:
            raise KeyError(sub_id)
        for sid in source_ids:
            sid = str(sid).strip()
            if sid and sid not in sub.covered_by:
                sub.covered_by.append(sid)
        return sub

    def uncovered(self) -> list[SubQuestion]:
        return [s for s in self.subs if not s.covered]

    @property
    def complete(self) -> bool:
        return not self.uncovered()

    def stats(self) -> dict:
        total = len(self.subs)
        done = total - len(self.uncovered())
        return {"sub_questions": total, "covered": done, "complete": self.complete,
                "coverage": round(done / total, 3) if total else 0.0}

    def render(self) -> str:
        lines = [f"Coverage plan for: {self.question}"]
        for sub in self.subs:
            mark = "x" if sub.covered else " "
            cites = f"  (sources: {', '.join(sub.covered_by)})" if sub.covered_by else ""
            lines.append(f"[{mark}] {sub.id} {sub.text}{cites}")
        left = self.uncovered()
        lines.append("All sub-questions covered; you may answer." if not left else
                     f"{len(left)} sub-question(s) still uncovered: {', '.join(s.id for s in left)}.")
        return "\n".join(lines)
