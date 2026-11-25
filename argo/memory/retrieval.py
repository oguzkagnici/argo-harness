"""Retrieval: heading-aware chunking, BM25, dense vectors, RRF fusion, reranking.

Design notes
- Lexical and dense retrieval fail in different ways, so we fuse with Reciprocal
  Rank Fusion (RRF, k=60) rather than mixing incomparable raw scores.
- A cross-encoder reranker then reorders the fused top-N.
- Memory hits get a small score uplift from importance, recency and usage
  (Generative-Agents-style retrieval: relevance x recency x importance).
"""
from __future__ import annotations

import array
import hashlib
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

STOPWORDS = set("""a an and are as at be but by for from has have he her his i in is it its
of on or that the their them they this to was were will with you your we our not no can do does
""".split())
TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u9fff]")


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in TOKEN_RE.findall(text or "") if t.lower() not in STOPWORDS and len(t) > 1]


def vec_to_blob(vec: Sequence[float]) -> bytes:
    return array.array("f", vec).tobytes()


def blob_to_vec(blob: bytes | None) -> list[float]:
    if not blob:
        return []
    arr = array.array("f")
    arr.frombytes(blob)
    return arr.tolist()


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class BM25:
    def __init__(self, docs: list[dict[str, Any]], text_key: str = "text", k1: float = 1.5, b: float = 0.75):
        self.docs = docs
        self.k1, self.b = k1, b
        self.tokens = [tokenize(d.get(text_key, "")) for d in docs]
        self.lengths = [len(t) for t in self.tokens]
        self.avgdl = (sum(self.lengths) / len(self.lengths)) if self.lengths else 0.0
        self.df: dict[str, int] = {}
        self.tf: list[dict[str, int]] = []
        for toks in self.tokens:
            counts: dict[str, int] = {}
            for t in toks:
                counts[t] = counts.get(t, 0) + 1
            self.tf.append(counts)
            for t in counts:
                self.df[t] = self.df.get(t, 0) + 1
        self.n = len(docs)

    def search(self, query: str, k: int = 20) -> list[tuple[int, float]]:
        if not self.n:
            return []
        q = tokenize(query)
        scores: list[tuple[int, float]] = []
        for i, counts in enumerate(self.tf):
            s = 0.0
            dl = self.lengths[i] or 1
            for term in q:
                f = counts.get(term, 0)
                if not f:
                    continue
                idf = math.log(1 + (self.n - self.df.get(term, 0) + 0.5) / (self.df.get(term, 0) + 0.5))
                s += idf * (f * (self.k1 + 1)) / (f + self.k1 * (1 - self.b + self.b * dl / max(self.avgdl, 1e-9)))
            if s:
                scores.append((i, s))
        scores.sort(key=lambda x: -x[1])
        return scores[:k]


def dense_search(query_vec: Sequence[float], docs: list[dict[str, Any]], k: int = 20) -> list[tuple[int, float]]:
    scored = []
    for i, d in enumerate(docs):
        v = d.get("vector")
        if v:
            scored.append((i, cosine(query_vec, v)))
    scored.sort(key=lambda x: -x[1])
    return scored[:k]


def rrf(rankings: Sequence[Sequence[tuple[int, float]]], k: int = 60, weights: Sequence[float] | None = None) -> list[tuple[int, float]]:
    weights = weights or [1.0] * len(rankings)
    fused: dict[int, float] = {}
    for ranking, w in zip(rankings, weights):
        for rank, (idx, _score) in enumerate(ranking):
            fused[idx] = fused.get(idx, 0.0) + w / (k + rank + 1)
    return sorted(fused.items(), key=lambda x: -x[1])


def recency_score(ts: float, now: float | None = None, half_life_days: float = 30.0) -> float:
    if not ts:
        return 0.0
    now = now or time.time()
    age_days = max(0.0, (now - ts) / 86400.0)
    return math.exp(-math.log(2) * age_days / max(half_life_days, 0.1))


# --------------------------------------------------------------------- chunking
@dataclass(slots=True)
class Chunk:
    text: str
    start_line: int
    end_line: int
    heading: str = ""


def chunk_text(text: str, max_chars: int = 1800, overlap: int = 200,
               heading_aware: bool = True) -> list[Chunk]:
    lines = text.splitlines()
    chunks: list[Chunk] = []
    buf: list[str] = []
    start = 1
    heading = ""

    def flush(end_line: int) -> None:
        nonlocal buf, start
        body = "\n".join(buf).strip()
        if body:
            chunks.append(Chunk(body, start, end_line, heading))
        buf = []

    for i, line in enumerate(lines, 1):
        if heading_aware and re.match(r"^#{1,6}\s+", line):
            if buf:
                flush(i - 1)
                start = i
            heading = re.sub(r"^#+\s+", "", line).strip()
        buf.append(line)
        if sum(len(x) + 1 for x in buf) >= max_chars:
            flush(i)
            start = i + 1
    flush(len(lines))

    # add overlap by merging tail of previous into next chunk
    if overlap > 0 and len(chunks) > 1:
        for i in range(1, len(chunks)):
            tail = chunks[i - 1].text[-overlap:]
            chunks[i].text = tail + "\n" + chunks[i].text
    return chunks or [Chunk(text.strip(), 1, len(lines))]


@dataclass(slots=True)
class Hit:
    id: str
    text: str
    score: float
    source: str = "memory"          # memory | file
    layer: str = ""
    kind: str = ""
    title: str = ""
    ref: str = ""
    line_start: int = 0
    line_end: int = 0
    ts: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def citation(self) -> str:
        if self.source == "file":
            loc = f"{self.ref}:{self.line_start}" if self.line_start else self.ref
            return f"[{loc}]"
        return f"[mem:{self.id[:8]}]"

    def render(self, max_chars: int = 1200) -> str:
        head = f"- ({self.citation()}) "
        if self.title:
            head += f"**{self.title}** — "
        body = self.text.strip().replace("\n", " ")
        if len(body) > max_chars:
            body = body[:max_chars] + "…"
        return head + body
