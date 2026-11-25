"""Memory tools exposed to the model."""
from __future__ import annotations

from ..core.types import ToolResult
from .base import ToolContext, ToolRegistry


async def _remember(ctx: ToolContext, text: str, kind: str = "fact", importance: float = 0.6,
                    confidence: float = 0.8, tags: list[str] | None = None,
                    layer: str | None = None, title: str = "") -> ToolResult:
    if ctx.memory is None:
        return ToolResult("Memory is not enabled.", is_error=True)
    layer = layer or {"preference": "profile", "profile": "profile", "procedure": "procedure",
                      "insight": "note", "lesson": "note", "pitfall": "note", "rule": "note"}.get(kind, "note")
    mid = await ctx.memory.add(text.strip(), layer=layer, kind=kind, title=title,
                               tags=tags or [], importance=max(0.0, min(1.0, float(importance))),
                               confidence=max(0.0, min(1.0, float(confidence))),
                               source=f"session:{ctx.session.id if ctx.session else 'unknown'}")
    return ToolResult(f"Remembered ({layer}/{kind}, id={mid}).", metadata={"id": mid})


async def _recall(ctx: ToolContext, query: str, k: int = 6, include_files: bool = True) -> ToolResult:
    if ctx.memory is None:
        return ToolResult("Memory is not enabled.", is_error=True)
    hits = await ctx.memory.retrieve(query, k=max(1, min(int(k), 20)), include_files=include_files)
    if not hits:
        return ToolResult(f"No memories or indexed files matched: {query}")
    lines = [f"Retrieved {len(hits)} hit(s) for: {query}",
             "Cite with the bracketed reference when using these facts."]
    for h in hits:
        lines.append(h.render())
    return ToolResult("\n".join(lines), metadata={"count": len(hits)})


async def _forget(ctx: ToolContext, target: str) -> ToolResult:
    if ctx.memory is None:
        return ToolResult("Memory is not enabled.", is_error=True)
    if target.startswith("m_"):
        item = ctx.memory.get(target)
        if not item:
            return ToolResult(f"No memory item {target}", is_error=True)
        ctx.memory.archive(target)
        return ToolResult(f"Archived {target}: {item.title[:80]}")
    hits = await ctx.memory.retrieve(target, k=1, include_files=False)
    if not hits:
        return ToolResult(f"No memory matched {target!r}", is_error=True)
    ctx.memory.archive(hits[0].id)
    return ToolResult(f"Archived closest match {hits[0].id}: {hits[0].text[:100]}")


def _stats(ctx: ToolContext) -> ToolResult:
    if ctx.memory is None:
        return ToolResult("Memory is not enabled.", is_error=True)
    st = ctx.memory.stats()
    lines = [f"Memory generation {st['generation']} at {st['path']}",
             f"Transcripts: {st['transcripts']} | Indexed chunks: {st['chunks']} | Dreams: {st['dreams']}"]
    for layer, counts in st["layers"].items():
        parts = ", ".join(f"{k}={v}" for k, v in counts.items())
        lines.append(f"- {layer}: {parts}")
    return ToolResult("\n".join(lines))


def register(reg: ToolRegistry) -> None:
    def params(props: dict, required: list[str] | None = None) -> dict:
        return {"type": "object", "properties": props, "required": required or []}

    reg.add("remember",
            "Write durable knowledge to long-term memory. Use for user preferences, verified environment facts, "
            "decisions and rationale, rules that worked, and pitfalls. Never store secrets or transient state.",
            params({"text": {"type": "string"},
                    "kind": {"type": "string", "enum": ["fact", "preference", "decision", "lesson", "rule",
                                                       "pitfall", "insight", "event", "procedure"], "default": "fact"},
                    "importance": {"type": "number", "default": 0.6},
                    "confidence": {"type": "number", "default": 0.8},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "layer": {"type": "string", "enum": ["note", "profile", "wiki", "procedure", "injected"]},
                    "title": {"type": "string"}}, ["text"]),
            _remember, read_only=False)
    reg.add("recall",
            "Search long-term memory + indexed workspace files (hybrid lexical+dense+rerank). "
            "Returns cited snippets. Call before work when prior context may matter.",
            params({"query": {"type": "string"}, "k": {"type": "integer", "default": 6},
                    "include_files": {"type": "boolean", "default": True}}, ["query"]),
            _recall, read_only=True, timeout=90)
    reg.add("forget", "Archive a memory item by id (m_...) or by closest natural-language match.",
            params({"target": {"type": "string"}}, ["target"]), _forget, read_only=False)
    reg.add("memory_stats", "Show memory layer counts, generation, transcript and index stats.",
            params({}), _stats, read_only=True)
    register_index(reg)


async def _index_workspace(ctx: ToolContext, path: str = ".", max_files: int = 400) -> ToolResult:
    if ctx.memory is None:
        return ToolResult("Memory is not enabled.", is_error=True)
    root = ctx.resolve(path)
    result = await ctx.memory.index_workspace(root)
    return ToolResult(f"Indexed {result['files']} files into {result['chunks']} chunks under {root}.",
                      metadata=result)


def register_index(reg: ToolRegistry) -> None:
    reg.add("index_workspace",
            "Chunk and embed workspace files so `recall` can search them (hybrid retrieval). "
            "Run after creating a corpus of documents/code.",
            {"type": "object", "properties": {"path": {"type": "string", "default": "."},
                                              "max_files": {"type": "integer", "default": 400}}},
            _index_workspace, read_only=False, timeout=600)
