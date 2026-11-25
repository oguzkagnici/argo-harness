"""Evidence ledger: the single source of truth for a research run.

Reports must be generated from sources recorded here, and every claim carries explicit citations. This is
the R1 primitive from docs/DEEP_RESEARCH.md: it makes fabricated citations structurally hard (a claim can only
cite a source id that exists) and makes verification (R2) a per-claim operation.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

VERDICT_MARK = {"supported": "OK", "partial": "PARTIAL", "unsupported": "NO", "unverified": "?"}


def _norm_url(url: str) -> str:
    try:
        parts = urlsplit(url.strip())
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, ""))
    except ValueError:
        return url.strip()


@dataclass
class Source:
    id: str
    url: str
    title: str = ""
    text: str = ""
    fetched_at: float = field(default_factory=time.time)


@dataclass
class Claim:
    id: str
    text: str
    citations: list[str] = field(default_factory=list)
    quote: str = ""
    verdict: str = "unverified"
    reason: str = ""
    evidence: str = ""
    checked: bool = False


class EvidenceLedger:
    def __init__(self) -> None:
        self.sources: dict[str, Source] = {}
        self.claims: dict[str, Claim] = {}
        self._by_url: dict[str, str] = {}
        self._n_sources = 0
        self._n_claims = 0

    # ---------------------------------------------------------------- sources
    def add_source(self, url: str, text: str, title: str = "") -> Source:
        key = _norm_url(url)
        if key in self._by_url:
            src = self.sources[self._by_url[key]]
            if len(text or "") > len(src.text):
                src.text = text
            if title and not src.title:
                src.title = title
            return src
        self._n_sources += 1
        src = Source(id=f"S{self._n_sources}", url=url.strip(), title=title.strip(), text=text or "")
        self.sources[src.id] = src
        self._by_url[key] = src.id
        return src

    # ----------------------------------------------------------------- claims
    def add_claim(self, text: str, citations: list[str] | None = None, quote: str = "") -> Claim:
        self._n_claims += 1
        claim = Claim(id=f"C{self._n_claims}", text=text.strip(),
                      citations=[c.strip() for c in (citations or []) if c.strip()], quote=quote or "")
        self.claims[claim.id] = claim
        return claim

    def unknown_citations(self) -> list[str]:
        bad = []
        for claim in self.claims.values():
            for cid in claim.citations:
                if cid not in self.sources:
                    bad.append(f"{claim.id}->{cid}")
        return bad

    # ---------------------------------------------------------------- reports
    def stats(self) -> dict[str, object]:
        claims = list(self.claims.values())
        checked = [c for c in claims if c.checked]
        supported = [c for c in claims if c.verdict == "supported"]
        return {
            "sources": len(self.sources),
            "claims": len(claims),
            "checked": len(checked),
            "supported": len(supported),
            "partial": sum(c.verdict == "partial" for c in claims),
            "unsupported": sum(c.verdict == "unsupported" for c in claims),
            "unverified": sum(c.verdict == "unverified" for c in claims),
            "unchecked": sum(not c.checked for c in claims),
            "uncited": sum(not c.citations for c in claims),
            "dangling_citations": self.unknown_citations(),
            "support_rate": round(len(supported) / len(checked), 4) if checked else 0.0,
        }

    def render_sources(self, limit: int = 2000) -> str:
        if not self.sources:
            return "(no sources recorded)"
        lines = []
        for src in self.sources.values():
            label = f"[{src.id}] {src.title + ' — ' if src.title else ''}{src.url}"
            lines.append(label + (f"\n    {src.text[:limit]}" if src.text else ""))
        return "\n".join(lines)

    def to_markdown(self, title: str = "Research report") -> str:
        lines = [f"# {title}", "", "## Findings", ""]
        for claim in self.claims.values():
            mark = VERDICT_MARK.get(claim.verdict, "?")
            cites = ", ".join(claim.citations) or "UNCITED"
            lines.append(f"- [{mark}] {claim.text} ({cites})")
            if claim.verdict in ("partial", "unsupported") and claim.reason:
                lines.append(f"    - verification: {claim.reason}")
        lines += ["", "## Sources", ""]
        for src in self.sources.values():
            lines.append(f"- [{src.id}] {src.title + ' — ' if src.title else ''}{src.url}")
        return "\n".join(lines)
