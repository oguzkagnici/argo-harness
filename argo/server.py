"""HTTP surface.

Two compatible interfaces:
  * /v1/chat/completions — OpenAI-compatible; drop Argo in front of any OpenAI client.
    Each request runs the full agent (tools, memory, subagents) and returns the final message.
  * /v1/agent — native API with SSE event streaming and run metadata.

Plus memory, dreaming, daemon/trigger, approval, and passthrough embeddings/rerank endpoints.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .config import ArgoConfig
from .core.agent import Agent
from .core.session import Session
from .daemon import GoalQueue
from .llm.client import LLMClient
from .memory.dream import DreamEngine
from .memory.store import MemoryStore


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def messages_to_task(messages: list[dict[str, Any]]) -> str:
    system = "\n".join(_content_text(m.get("content")) for m in messages if m.get("role") == "system").strip()
    convo = [m for m in messages if m.get("role") in ("user", "assistant")]
    if not convo:
        raise HTTPException(400, "no user message")
    last = convo[-1]
    history = convo[:-1]
    parts: list[str] = []
    if system:
        parts.append(f"<operator_instructions>\n{system}\n</operator_instructions>")
    if history:
        lines = []
        for m in history[-20:]:
            text = _content_text(m.get("content")).strip()
            if text:
                lines.append(f"{m['role'].upper()}: {text[:4000]}")
        if lines:
            parts.append("<conversation_so_far>\n" + "\n".join(lines) + "\n</conversation_so_far>")
    parts.append(f"<request>\n{_content_text(last.get('content'))}\n</request>")
    return "\n\n".join(parts)


def _usage_dict(result: Any) -> dict[str, Any]:
    return {"prompt_tokens": result.usage.prompt_tokens, "completion_tokens": result.usage.completion_tokens,
            "total_tokens": result.usage.total_tokens,
            "prompt_tokens_details": {"cached_tokens": result.usage.cached_tokens},
            "completion_tokens_details": {"reasoning_tokens": result.usage.reasoning_tokens}}


def create_app(cfg: ArgoConfig, require_key: str | None = None) -> FastAPI:
    app = FastAPI(title="Argo Harness", version="0.1.0")
    queue = GoalQueue(Path(cfg.data_dir) / "daemon.sqlite")
    semaphore = asyncio.Semaphore(2)

    def authorize(request: Request) -> None:
        if not require_key:
            return
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else request.headers.get("x-api-key", "")
        if token != require_key:
            raise HTTPException(401, "invalid api key")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "model": cfg.provider.model, "workspace": str(cfg.workspace),
                "time": time.time()}

    @app.get("/v1/models")
    async def models(request: Request) -> dict[str, Any]:
        authorize(request)
        return {"object": "list", "data": [{
            "id": cfg.provider.model, "object": "model", "created": int(time.time()),
            "owned_by": "argo", "argo": {"context_length": cfg.models.context_window,
                                         "tools": True, "memory": True, "subagents": True}}]}

    # ------------------------------------------------------- OpenAI-compatible
    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Any:
        authorize(request)
        body = await request.json()
        messages = body.get("messages") or []
        task = messages_to_task(messages)
        stream = bool(body.get("stream"))
        session_id = body.get("session_id") or body.get("argo", {}).get("session_id")
        model = cfg.provider.model
        cmpl_id = f"chatcmpl-{uuid.uuid4().hex[:20]}"
        created = int(time.time())

        async def run_agent(on_delta=None) -> Any:
            agent = Agent(cfg, on_delta=on_delta,
                          max_iterations=body.get("max_iterations") or cfg.max_iterations)
            try:
                async with agent:
                    sess = Session.load(cfg, session_id, agent.memory) if session_id else None
                    return await agent.run(task, session=sess, stream=bool(on_delta))
            finally:
                await agent.close()

        if not stream:
            async with semaphore:
                result = await run_agent()
            return JSONResponse({
                "id": cmpl_id, "object": "chat.completion", "created": created, "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": result.output},
                             "finish_reason": "stop"}],
                "usage": _usage_dict(result),
                "argo": {"session_id": result.session_id, "iterations": result.iterations,
                         "files": result.files, "stop_reason": result.stop_reason,
                         "compactions": result.compactions, "resets": result.resets},
            })

        async def event_stream() -> AsyncIterator[str]:
            q: asyncio.Queue = asyncio.Queue()

            async def on_delta(delta) -> None:
                if delta.text:
                    await q.put(("text", delta.text))

            def chunk(delta: dict[str, Any], finish: str | None = None,
                      usage: dict[str, Any] | None = None) -> str:
                payload = {"id": cmpl_id, "object": "chat.completion.chunk", "created": created,
                           "model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                if usage:
                    payload["usage"] = usage
                return f"data: {json.dumps(payload)}\n\n"

            yield chunk({"role": "assistant", "content": ""})
            holder: dict[str, Any] = {}

            async def runner():
                try:
                    async with semaphore:
                        holder["result"] = await run_agent(on_delta=on_delta)
                except Exception as exc:  # noqa: BLE001
                    holder["error"] = f"{type(exc).__name__}: {exc}"
                finally:
                    await q.put(("done", None))

            task_obj = asyncio.create_task(runner())
            while True:
                try:
                    kind, value = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if kind == "done":
                    break
                yield chunk({"content": value})
            await task_obj
            result = holder.get("result")
            if holder.get("error") or result is None:
                err = holder.get("error", "agent failed")
                yield chunk({"content": f"\n[argo error: {err}]"}, finish="stop")
                yield "data: [DONE]\n\n"
                return
            yield chunk({}, finish="stop", usage=_usage_dict(result))
            yield "data: [DONE]\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # --------------------------------------------------------------- native
    @app.post("/v1/agent")
    async def native_agent(request: Request) -> Any:
        authorize(request)
        body = await request.json()
        task = body.get("task") or body.get("input") or ""
        if not task.strip():
            raise HTTPException(400, "task is required")
        stream = bool(body.get("stream"))
        session_id = body.get("session_id")
        on_event = body.get("events", False) or stream

        if not stream:
            async with semaphore:
                async with Agent(cfg) as agent:
                    sess = Session.load(cfg, session_id, agent.memory) if session_id else None
                    result = await agent.run(task, session=sess)
            return {"output": result.output, "session_id": result.session_id,
                    "stop_reason": result.stop_reason, "iterations": result.iterations,
                    "usage": result.usage.as_dict(), "files": result.files, "plan": result.plan}

        async def sse() -> AsyncIterator[str]:
            q: asyncio.Queue = asyncio.Queue()
            agent = Agent(cfg, on_delta=lambda d: q.put(("delta", d)) if d.text else _noop())

            def listener(ev) -> None:
                if on_event:
                    q.put_nowait(("event", {"type": ev.type, "data": ev.data, "ts": ev.ts}))

            unsubscribe = agent.events.subscribe(listener)

            async def runner():
                try:
                    async with semaphore:
                        async with agent:
                            sess = Session.load(cfg, session_id, agent.memory) if session_id else None
                            result = await agent.run(task, session=sess, stream=True)
                            await q.put(("result", result))
                except Exception as exc:  # noqa: BLE001
                    await q.put(("error", f"{type(exc).__name__}: {exc}"))
                finally:
                    await q.put(("done", None))

            task_obj = asyncio.create_task(runner())
            while True:
                try:
                    kind, value = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if kind == "done":
                    break
                if kind == "delta":
                    yield f"event: delta\ndata: {json.dumps({'text': value.text})}\n\n"
                elif kind == "event":
                    if value["type"] in ("assistant",):
                        value["data"].pop("reasoning", None)
                    yield f"event: {value['type']}\ndata: {json.dumps(value['data'], default=str)[:8000]}\n\n"
                elif kind == "error":
                    yield f"event: error\ndata: {json.dumps({'error': value})}\n\n"
                elif kind == "result":
                    payload = {"output": value.output, "session_id": value.session_id,
                               "stop_reason": value.stop_reason, "iterations": value.iterations,
                               "usage": value.usage.as_dict(), "files": value.files, "plan": value.plan}
                    yield f"event: result\ndata: {json.dumps(payload, default=str)}\n\n"
            await task_obj
            unsubscribe()
            yield "event: done\ndata: {}\n\n"

        return StreamingResponse(sse(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # --------------------------------------------------------------- memory
    @app.get("/v1/memory/stats")
    async def memory_stats(request: Request) -> dict[str, Any]:
        authorize(request)
        mem = MemoryStore(cfg.memory_db)
        try:
            return mem.stats()
        finally:
            mem.close()

    @app.post("/v1/memory/search")
    async def memory_search(request: Request) -> dict[str, Any]:
        authorize(request)
        body = await request.json()
        async with LLMClient(cfg) as llm:
            mem = MemoryStore(cfg.memory_db, embed=llm.embed,
                              rerank=lambda q, d, n: llm.rerank(q, d, top_n=n),
                              model=cfg.models.embedding_model or "default")
            try:
                hits = await mem.retrieve(body.get("query", ""), k=int(body.get("k", 8)),
                                          include_files=bool(body.get("files", False)))
                return {"query": body.get("query"), "hits": [
                    {"id": h.id, "text": h.text, "score": h.score, "source": h.source,
                     "layer": h.layer, "kind": h.kind, "ref": h.ref} for h in hits]}
            finally:
                mem.close()

    @app.post("/v1/dream")
    async def dream(request: Request) -> dict[str, Any]:
        authorize(request)
        body = await request.json()
        async with LLMClient(cfg) as llm:
            mem = MemoryStore(cfg.memory_db, embed=llm.embed,
                              rerank=lambda q, d, n: llm.rerank(q, d, top_n=n),
                              model=cfg.models.embedding_model or "default")
            try:
                report = await DreamEngine(mem, llm, cfg).run(
                    instructions=body.get("instructions", ""), apply=bool(body.get("apply", False)),
                    max_sessions=int(body.get("sessions", 30)))
                return {"id": report.id, "status": report.status, "generation": report.output_generation,
                        "summary": report.summary, "report": report.to_markdown(),
                        "stats": mem.stats()}
            finally:
                mem.close()

    # -------------------------------------------------------------- daemon
    @app.post("/hooks/{name}")
    async def webhook(name: str, request: Request) -> dict[str, Any]:
        authorize(request)
        tr = queue.webhook_trigger(name)
        if not tr:
            raise HTTPException(404, f"unknown trigger {name}")
        payload: dict[str, Any] = {}
        try:
            payload = await request.json()
        except Exception:
            pass
        goal = tr["goal"]
        if payload:
            goal = f"{goal}\n\nWebhook payload JSON:\n{json.dumps(payload, ensure_ascii=False)[:8000]}"
        gid = queue.enqueue(goal, source=f"webhook:{name}", trigger_id=tr["id"])
        return {"queued": bool(gid), "goal_id": gid}

    @app.get("/v1/goals")
    async def goals(request: Request, limit: int = 50) -> dict[str, Any]:
        authorize(request)
        return {"goals": queue.list_goals(limit)}

    @app.get("/v1/approvals")
    async def approvals(request: Request) -> dict[str, Any]:
        authorize(request)
        return {"approvals": queue.pending_approvals()}

    @app.post("/v1/approvals/{approval_id}")
    async def decide(approval_id: str, request: Request) -> dict[str, Any]:
        authorize(request)
        body = await request.json()
        queue.decide(approval_id, bool(body.get("approve", False)))
        return {"ok": True, "approval_id": approval_id}

    @app.post("/v1/triggers")
    async def add_trigger(request: Request) -> dict[str, Any]:
        authorize(request)
        body = await request.json()
        tid = queue.add_trigger(body["name"], body.get("kind", "interval"), body.get("spec", {}),
                                body["goal"], bool(body.get("enabled", True)))
        return {"id": tid}

    @app.get("/v1/triggers")
    async def list_triggers(request: Request) -> dict[str, Any]:
        authorize(request)
        return {"triggers": queue.triggers()}

    # ---------------------------------------------------------- passthrough
    @app.post("/v1/embeddings")
    async def embeddings(request: Request) -> dict[str, Any]:
        authorize(request)
        body = await request.json()
        async with LLMClient(cfg) as llm:
            vecs = await llm.embed(body.get("input") if isinstance(body.get("input"), list) else [body.get("input")])
        return {"object": "list", "model": body.get("model", cfg.models.embedding_model),
                "data": [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vecs)],
                "usage": {"prompt_tokens": 0, "total_tokens": 0}}

    @app.post("/v1/rerank")
    async def rerank(request: Request) -> dict[str, Any]:
        authorize(request)
        body = await request.json()
        async with LLMClient(cfg) as llm:
            results = await llm.rerank(body.get("query", ""), body.get("documents", []),
                                       top_n=body.get("top_n"))
        return {"model": cfg.models.rerank_model,
                "results": [{"index": i, "relevance_score": s,
                             "document": {"text": body.get("documents", [""])[i]}}
                            for i, s in results]}

    return app


async def _noop() -> None:
    return None


def create_default_app() -> FastAPI:
    from .config import load_config
    return create_app(load_config())
