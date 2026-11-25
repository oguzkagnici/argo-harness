"""Layered, generational memory store backed by SQLite.

Layers (mirrors Anthropic's memory hierarchy):
  transcript  raw session event log (searchable ground truth)
  note        atomic salient observations written during work
  wiki        consolidated topic pages / representations with links
  profile     durable user & environment facts
  procedure   learned reusable procedures (workflows, skills)

Dreaming reads generation G, writes a *new* generation G+1 (input is never
mutated), and activation is an explicit, reversible step.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Sequence

from .retrieval import (BM25, Hit, blob_to_vec, chunk_text, dense_search, rrf,
                        recency_score, vec_to_blob)

EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]
RerankFn = Callable[[str, list[str], int], Awaitable[list[tuple[int, float]]]]

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS memory_items (
  id TEXT PRIMARY KEY, layer TEXT NOT NULL, kind TEXT NOT NULL,
  title TEXT DEFAULT '', text TEXT NOT NULL, tags TEXT DEFAULT '[]',
  importance REAL DEFAULT 0.5, confidence REAL DEFAULT 0.7,
  status TEXT DEFAULT 'active', source TEXT DEFAULT '', source_ref TEXT DEFAULT '',
  evidence TEXT DEFAULT '', created_at REAL, updated_at REAL,
  last_accessed REAL DEFAULT 0, access_count INTEGER DEFAULT 0,
  generation INTEGER DEFAULT 0, superseded_by TEXT, hash TEXT, embedding BLOB
);
CREATE INDEX IF NOT EXISTS idx_items_gen ON memory_items(generation, status, layer);
CREATE TABLE IF NOT EXISTS links (
  src TEXT, dst TEXT, rel TEXT DEFAULT 'related', PRIMARY KEY(src, dst, rel)
);
CREATE TABLE IF NOT EXISTS transcripts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,
  meta TEXT DEFAULT '{}', ts REAL, generation INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tr_session ON transcripts(session_id, ts);
CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ref TEXT, source_type TEXT, path TEXT,
  text TEXT, start_line INTEGER, end_line INTEGER, heading TEXT DEFAULT '',
  hash TEXT UNIQUE, updated_at REAL, embedding BLOB
);
CREATE TABLE IF NOT EXISTS dreams (
  id TEXT PRIMARY KEY, created_at REAL, status TEXT, input_generation INTEGER,
  output_generation INTEGER, instructions TEXT, summary TEXT, report TEXT, sessions TEXT
);
CREATE TABLE IF NOT EXISTS embedding_cache (
  hash TEXT, model TEXT, dim INTEGER, vector BLOB, PRIMARY KEY(hash, model)
);
"""


@dataclass(slots=True)
class MemoryItem:
    id: str
    layer: str
    kind: str
    title: str
    text: str
    tags: list[str]
    importance: float
    confidence: float
    status: str
    source: str
    source_ref: str
    evidence: str
    created_at: float
    updated_at: float
    access_count: int
    generation: int
    superseded_by: str | None = None


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _hash(text: str) -> str:
    return hashlib.sha1(text.strip().lower().encode("utf-8", "replace")).hexdigest()


class MemoryStore:
    def __init__(self, path: str | Path, embed: EmbedFn | None = None, rerank: RerankFn | None = None,
                 model: str = "default"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.embed = embed
        self.rerank = rerank
        self.model = model
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.commit()
        if self._meta("active_generation") is None:
            self._set_meta("active_generation", "0")

    # ------------------------------------------------------------------ meta
    def _meta(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def _set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, value))
        self.db.commit()

    @property
    def active_generation(self) -> int:
        return int(self._meta("active_generation", "0") or 0)

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------- embeddings
    async def _embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not self.embed or not texts:
            return []
        out: list[list[float]] = [None] * len(texts)  # type: ignore[list-item]
        missing: list[tuple[int, str]] = []
        for i, t in enumerate(texts):
            row = self.db.execute("SELECT vector FROM embedding_cache WHERE hash=? AND model=?",
                                  (_hash(t), self.model)).fetchone()
            if row:
                out[i] = blob_to_vec(row[0])
            else:
                missing.append((i, t))
        if missing:
            vectors = await self.embed([t for _, t in missing])
            for (i, t), v in zip(missing, vectors):
                out[i] = v
                self.db.execute("INSERT OR REPLACE INTO embedding_cache(hash,model,dim,vector) VALUES(?,?,?,?)",
                                (_hash(t), self.model, len(v), vec_to_blob(v)))
            self.db.commit()
        return out

    # ------------------------------------------------------------------ items
    async def add(self, text: str, layer: str = "note", kind: str = "fact", title: str = "",
                  tags: Sequence[str] | None = None, importance: float = 0.5,
                  confidence: float = 0.7, source: str = "", source_ref: str = "",
                  evidence: str = "", generation: int | None = None, embed_now: bool = True) -> str:
        text = (text or "").strip()
        if not text:
            raise ValueError("empty memory text")
        if not title:
            title = text.split("\n")[0][:80]
        now = time.time()
        gen = self.active_generation if generation is None else generation
        dup = self.db.execute("SELECT id FROM memory_items WHERE hash=? AND layer=? AND generation=? "
                              "AND status='active'", (_hash(text), layer, gen)).fetchone()
        if dup:
            self.db.execute("UPDATE memory_items SET importance=MAX(importance,?), "
                            "confidence=MAX(confidence,?), updated_at=? WHERE id=?",
                            (float(importance), float(confidence), now, dup[0]))
            self.db.commit()
            return dup[0]
        vec = (await self._embed([text]))[0] if (self.embed and embed_now) else None
        item_id = f"m_{uuid.uuid4().hex[:12]}"
        self.db.execute("""INSERT INTO memory_items(id,layer,kind,title,text,tags,importance,confidence,
                           status,source,source_ref,evidence,created_at,updated_at,generation,hash,embedding)
                           VALUES(?,?,?,?,?,?,?,?,'active',?,?,?,?,?,?,?,?)""",
                        (item_id, layer, kind, title, text, json.dumps(list(tags or [])),
                         float(importance), float(confidence), source, source_ref, evidence,
                         now, now, gen, _hash(text), vec_to_blob(vec) if vec else None))
        self.db.commit()
        return item_id

    async def embed_pending(self, generation: int | None = None, batch: int = 64) -> int:
        gen = self.active_generation if generation is None else generation
        rows = self.db.execute("SELECT id,text FROM memory_items WHERE generation=? AND embedding IS NULL "
                               "AND status='active'", (gen,)).fetchall()
        if not rows or not self.embed:
            return 0
        done = 0
        for i in range(0, len(rows), batch):
            chunk = rows[i:i + batch]
            vecs = await self._embed([r["text"] for r in chunk])
            for r, v in zip(chunk, vecs):
                self.db.execute("UPDATE memory_items SET embedding=? WHERE id=?", (vec_to_blob(v), r["id"]))
                done += 1
        self.db.commit()
        return done

    def dedupe(self, generation: int | None = None, threshold: float = 0.97) -> int:
        """Archive near-duplicate active items within a generation, keeping the highest-value copy."""
        gen = self.active_generation if generation is None else generation
        rows = self.db.execute("SELECT * FROM memory_items WHERE generation=? AND status='active'", (gen,)).fetchall()
        items = [self._row_to_item(r) for r in rows]
        vecs = {r["id"]: blob_to_vec(r["embedding"]) for r in rows}
        removed: set[str] = set()
        rank = lambda it: (it.confidence + it.importance + min(1.0, it.access_count / 5), it.updated_at)
        by_layer: dict[str, list[MemoryItem]] = {}
        for it in items:
            by_layer.setdefault(it.layer, []).append(it)
        for layer_items in by_layer.values():
            layer_items.sort(key=rank, reverse=True)
            kept: list[MemoryItem] = []
            for it in layer_items:
                norm = _norm(it.text)
                dup = False
                for k in kept:
                    if it.id in removed:
                        break
                    if norm == _norm(k.text):
                        dup = True
                    elif vecs.get(it.id) and vecs.get(k.id):
                        # cosine above threshold and comparable length
                        import math as _m
                        a, b = vecs[it.id], vecs[k.id]
                        dot = sum(x * y for x, y in zip(a, b))
                        na = _m.sqrt(sum(x * x for x in a)); nb = _m.sqrt(sum(y * y for y in b))
                        if na and nb and dot / (na * nb) > threshold:
                            dup = True
                    if dup:
                        self.db.execute("UPDATE memory_items SET status='superseded',superseded_by=?,updated_at=? WHERE id=?",
                                        (k.id, time.time(), it.id))
                        removed.add(it.id)
                        break
                if it.id not in removed:
                    kept.append(it)
        self.db.commit()
        return len(removed)

    def get(self, item_id: str) -> MemoryItem | None:
        row = self.db.execute("SELECT * FROM memory_items WHERE id=?", (item_id,)).fetchone()
        return self._row_to_item(row) if row else None

    def update(self, item_id: str, **fields: Any) -> None:
        allowed = {"text", "title", "importance", "confidence", "status", "tags", "evidence",
                   "superseded_by", "layer", "kind", "source", "source_ref"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "tags":
                v = json.dumps(list(v))
            sets.append(f"{k}=?")
            vals.append(v)
        if not sets:
            return
        sets.append("updated_at=?")
        vals.append(time.time())
        vals.append(item_id)
        self.db.execute(f"UPDATE memory_items SET {', '.join(sets)} WHERE id=?", vals)
        self.db.commit()

    def supersede(self, old_id: str, new_id: str, reason: str = "") -> None:
        self.db.execute("UPDATE memory_items SET status='superseded', superseded_by=?, updated_at=? WHERE id=?",
                        (new_id, time.time(), old_id))
        self.db.commit()

    def archive(self, item_id: str) -> None:
        self.update(item_id, status="archived")

    def delete(self, item_id: str) -> None:
        self.db.execute("DELETE FROM memory_items WHERE id=?", (item_id,))
        self.db.commit()

    def list_items(self, layer: str | None = None, kind: str | None = None,
                   status: str = "active", generation: int | None = None, limit: int = 500) -> list[MemoryItem]:
        gen = self.active_generation if generation is None else generation
        q = "SELECT * FROM memory_items WHERE generation=?"
        params: list[Any] = [gen]
        if status:
            q += " AND status=?"; params.append(status)
        if layer:
            q += " AND layer=?"; params.append(layer)
        if kind:
            q += " AND kind=?"; params.append(kind)
        q += " ORDER BY importance DESC, updated_at DESC LIMIT ?"
        params.append(limit)
        return [self._row_to_item(r) for r in self.db.execute(q, params).fetchall()]

    def stats(self) -> dict[str, Any]:
        gen = self.active_generation
        rows = self.db.execute("SELECT layer, status, COUNT(*) FROM memory_items WHERE generation=? GROUP BY layer,status",
                               (gen,)).fetchall()
        counts: dict[str, dict[str, int]] = {}
        for layer, status, n in rows:
            counts.setdefault(layer, {})[status] = n
        return {"generation": gen, "layers": counts,
                "transcripts": self.db.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0],
                "chunks": self.db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
                "dreams": self.db.execute("SELECT COUNT(*) FROM dreams").fetchone()[0],
                "path": str(self.path)}

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> MemoryItem:
        return MemoryItem(
            id=row["id"], layer=row["layer"], kind=row["kind"], title=row["title"] or "",
            text=row["text"], tags=json.loads(row["tags"] or "[]"), importance=row["importance"],
            confidence=row["confidence"], status=row["status"], source=row["source"] or "",
            source_ref=row["source_ref"] or "", evidence=row["evidence"] or "",
            created_at=row["created_at"], updated_at=row["updated_at"],
            access_count=row["access_count"], generation=row["generation"],
            superseded_by=row["superseded_by"],
        )

    # ------------------------------------------------------------------ links
    def link(self, src: str, dst: str, rel: str = "related") -> None:
        self.db.execute("INSERT OR IGNORE INTO links(src,dst,rel) VALUES(?,?,?)", (src, dst, rel))
        self.db.commit()

    def neighbors(self, item_id: str) -> list[dict[str, str]]:
        rows = self.db.execute("SELECT src,dst,rel FROM links WHERE src=? OR dst=?", (item_id, item_id)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------- transcripts
    def log(self, session_id: str, role: str, content: str, meta: dict[str, Any] | None = None) -> None:
        if not content:
            return
        self.db.execute("INSERT INTO transcripts(session_id,role,content,meta,ts,generation) VALUES(?,?,?,?,?,?)",
                        (session_id, role, content[:100_000], json.dumps(meta or {}), time.time(),
                         self.active_generation))
        self.db.commit()

    def transcripts(self, session_ids: Sequence[str] | None = None, since: float | None = None,
                    limit: int = 2000) -> list[dict[str, Any]]:
        q = "SELECT * FROM transcripts WHERE 1=1"
        params: list[Any] = []
        if session_ids:
            q += f" AND session_id IN ({','.join('?' * len(session_ids))})"
            params.extend(session_ids)
        if since:
            q += " AND ts>=?"; params.append(since)
        q += " ORDER BY ts ASC LIMIT ?"; params.append(limit)
        return [dict(r) for r in self.db.execute(q, params).fetchall()]

    def session_ids(self, limit: int = 30) -> list[str]:
        rows = self.db.execute("SELECT session_id, MAX(ts) t FROM transcripts GROUP BY session_id ORDER BY t DESC LIMIT ?",
                               (limit,)).fetchall()
        return [r[0] for r in rows]

    # -------------------------------------------------------------- file index
    async def index_file(self, path: Path, text: str, source_type: str = "file", ref: str | None = None) -> int:
        ref = ref or str(path)
        chunks = chunk_text(text)
        vecs = await self._embed([c.text for c in chunks]) if self.embed else [None] * len(chunks)
        self.db.execute("DELETE FROM chunks WHERE ref=?", (ref,))
        now = time.time()
        for c, v in zip(chunks, vecs):
            h = _hash(f"{ref}:{c.start_line}:{c.text}")
            self.db.execute("""INSERT OR REPLACE INTO chunks(ref,source_type,path,text,start_line,end_line,
                               heading,hash,updated_at,embedding) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                            (ref, source_type, str(path), c.text, c.start_line, c.end_line, c.heading, h, now,
                             vec_to_blob(v) if v else None))
        self.db.commit()
        return len(chunks)

    async def index_workspace(self, root: Path, max_bytes: int = 300_000,
                              exts: set[str] | None = None) -> dict[str, int]:
        exts = exts or {".md", ".txt", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".toml",
                        ".yaml", ".yml", ".html", ".css", ".sql", ".sh", ".rs", ".go", ".java",
                        ".c", ".h", ".cpp", ".ipynb", ".csv"}
        skip = {".git", "node_modules", "__pycache__", ".venv", ".argo", "dist", "build", ".next"}
        files = indexed = chunks = 0
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in exts:
                continue
            if any(part in skip for part in p.parts):
                continue
            try:
                if p.stat().st_size > max_bytes:
                    continue
                raw = p.read_bytes()
                if b"\0" in raw[:2048]:
                    continue
                text = raw.decode("utf-8", "replace")
            except OSError:
                continue
            files += 1
            chunks += await self.index_file(p, text, ref=str(p.relative_to(root)))
            indexed += 1
        return {"files": indexed, "chunks": chunks}

    def indexed_files(self) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT ref, COUNT(*) n, MAX(updated_at) t FROM chunks GROUP BY ref ORDER BY t DESC").fetchall()
        return [{"ref": r[0], "chunks": r[1], "updated_at": r[2]} for r in rows]

    # ---------------------------------------------------------------- retrieve
    async def retrieve(self, query: str, k: int = 8, include_files: bool = True,
                       layers: Sequence[str] | None = None, quality_boost: bool = True,
                       rerank: bool = True, candidate_pool: int = 40) -> list[Hit]:
        gen = self.active_generation
        q = ("SELECT id,layer,kind,title,text,importance,confidence,created_at,access_count,embedding "
             "FROM memory_items WHERE generation=? AND status='active'")
        params: list[Any] = [gen]
        if layers:
            q += f" AND layer IN ({','.join('?' * len(layers))})"
            params.extend(layers)
        rows = self.db.execute(q, params).fetchall()
        docs: list[dict[str, Any]] = []
        for r in rows:
            docs.append({"id": r["id"], "text": (r["title"] + "\n" if r["title"] else "") + r["text"],
                         "vector": blob_to_vec(r["embedding"]), "kind": "memory", "layer": r["layer"],
                         "item_kind": r["kind"], "title": r["title"], "importance": r["importance"],
                         "created_at": r["created_at"], "access_count": r["access_count"]})
        if include_files:
            for r in self.db.execute("SELECT ref,path,text,start_line,end_line,heading,embedding FROM chunks"):
                docs.append({"id": f"f{r['ref']}:{r['start_line']}", "text": r["text"], "vector": blob_to_vec(r["embedding"]),
                             "kind": "file", "ref": r["ref"], "line_start": r["start_line"],
                             "line_end": r["end_line"], "title": r["heading"] or ""})
        if not docs:
            return []

        lexical = BM25(docs).search(query, candidate_pool)
        rankings = [lexical]
        if self.embed:
            try:
                qvec = (await self._embed([query]))[0]
                rankings.append(dense_search(qvec, docs, candidate_pool))
            except Exception:
                pass
        fused = rrf(rankings, weights=[0.8] + [1.2] * (len(rankings) - 1))
        fused = fused[:candidate_pool]
        candidates = [docs[i] | {"rrf": s} for i, s in fused]

        if rerank and self.rerank and candidates:
            try:
                rr = await self.rerank(query, [c["text"] for c in candidates], min(24, len(candidates)))
                order = {idx: score for idx, score in rr}
                for i, c in enumerate(candidates):
                    c["ce"] = order.get(i, 0.0)
                candidates.sort(key=lambda c: -c.get("ce", 0.0))
            except Exception:
                pass

        now = time.time()
        hits: list[Hit] = []
        for c in candidates[:max(k * 3, k)]:
            base = c.get("rrf", 0.0) + 0.6 * max(0.0, c.get("ce", 0.0))
            if c["kind"] == "memory" and quality_boost:
                base += 0.05 * float(c.get("importance", 0.5))
                base += 0.04 * recency_score(float(c.get("created_at") or 0), now)
                base += 0.02 * min(1.0, math.log1p(c.get("access_count", 0)) / 3)
            hits.append(Hit(
                id=c["id"], text=c["text"], score=base, source=c["kind"],
                layer=c.get("layer", ""), kind=c.get("item_kind", ""), title=c.get("title", ""),
                ref=c.get("ref", ""), line_start=c.get("line_start", 0), line_end=c.get("line_end", 0),
                ts=c.get("created_at", 0.0), metadata={"rrf": c.get("rrf"), "ce": c.get("ce")},
            ))
        hits.sort(key=lambda h: -h.score)
        hits = hits[:k]
        for h in hits:
            if h.source == "memory" and h.id and not h.id.startswith("f"):
                self.db.execute("UPDATE memory_items SET last_accessed=?, access_count=access_count+1 WHERE id=?",
                                (now, h.id))
        self.db.commit()
        return hits

    def inject_block(self, budget_chars: int = 5000, query: str | None = None) -> str:
        """Build the always-loaded context: profile + high-value notes + wiki index."""
        gen = self.active_generation
        parts: list[str] = []
        profile = self.list_items(layer="profile", status="active", generation=gen, limit=40)
        if profile:
            parts.append("## Durable facts\n" + "\n".join(f"- {p.text}" for p in profile))
        injected = self.list_items(layer="injected", status="active", generation=gen, limit=20)
        if injected:
            parts.append("## Standing context\n" + "\n".join(f"- {p.text}" for p in injected))
        notes = self.list_items(layer="note", status="active", generation=gen, limit=200)
        scored = sorted(notes, key=lambda n: -(n.importance * (0.5 + 0.5 * recency_score(n.created_at))))
        if scored:
            parts.append("## High-value notes\n" + "\n".join(f"- {n.text[:400]}" for n in scored[:25]))
        wiki = self.list_items(layer="wiki", status="active", generation=gen, limit=50)
        if wiki:
            parts.append("## Topic index\n" + "\n".join(f"- {w.title}" for w in wiki))
        procs = self.list_items(layer="procedure", status="active", generation=gen, limit=20)
        if procs:
            parts.append("## Learned procedures\n" + "\n".join(f"- {p.title}: {p.text[:200]}" for p in procs))
        block = "\n\n".join(parts)
        if len(block) > budget_chars:
            block = block[:budget_chars].rsplit("\n", 1)[0] + "\n…"
        return block

    # ------------------------------------------------------------------ export
    def export_markdown(self, out_dir: Path) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        groups: dict[str, list[MemoryItem]] = {}
        for item in self.list_items(status="active", limit=5000):
            groups.setdefault(item.layer, []).append(item)
        index = ["# Argo memory", "", f"Generation: {self.active_generation}",
                 f"Exported: {time.strftime('%Y-%m-%d %H:%M')}", ""]
        for layer, items in sorted(groups.items()):
            fname = f"{layer}.md"
            lines = [f"# {layer}", ""]
            for it in items:
                links = self.neighbors(it.id)
                lines.append("---")
                lines.append(f"id: {it.id}")
                lines.append(f"layer: {it.layer}")
                lines.append(f"kind: {it.kind}")
                lines.append(f"importance: {it.importance:.2f}")
                lines.append(f"confidence: {it.confidence:.2f}")
                lines.append(f"status: {it.status}")
                lines.append(f"created: {time.strftime('%Y-%m-%d', time.localtime(it.created_at or 0))}")
                lines.append(f"updated: {time.strftime('%Y-%m-%d', time.localtime(it.updated_at or 0))}")
                lines.append(f"tags: {json.dumps(it.tags)}")
                if it.evidence:
                    lines.append(f"evidence: {json.dumps(it.evidence[:300])}")
                lines.append("---")
                lines.append(f"## {it.title or it.id}")
                lines.append("")
                lines.append(it.text)
                if links:
                    lines.append("")
                    lines.append("Links: " + ", ".join(f"[[{l['dst'] if l['src'] == it.id else l['src']}]]"
                                                        for l in links))
                lines.append("")
            (out_dir / fname).write_text("\n".join(lines), encoding="utf-8")
            index.append(f"- [{layer}]({fname}) — {len(items)} items")
        (out_dir / "MEMORY.md").write_text("\n".join(index), encoding="utf-8")

        profile = groups.get("profile", [])
        notes = sorted(groups.get("note", []), key=lambda n: -(n.importance * (0.5 + 0.5 * recency_score(n.created_at))))[:20]
        dash = ["# MEMORY", "", "Always-loaded dashboard (bounded).", ""]
        if profile:
            dash += ["## Profile"] + [f"- {p.text.splitlines()[0][:300]}" for p in profile]
            dash.append("")
        if notes:
            dash += ["## High-value notes"] + [f"- {n.text.splitlines()[0][:300]}" for n in notes]
            dash.append("")
        if groups.get("procedure"):
            dash += ["## Procedures"] + [f"- {p.title}" for p in groups["procedure"]]
        dash += ["", "Full layers: " + ", ".join(f"[{k}]({k}.md)" for k in sorted(groups))]
        (out_dir / "_dashboard.md").write_text("\n".join(dash), encoding="utf-8")
        return out_dir / "MEMORY.md"


import math  # noqa: E402  (used in retrieve scoring)
