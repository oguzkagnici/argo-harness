"""Async OpenAI-compatible client: chat (streaming + tools), embeddings, rerank.

Works with any endpoint implementing /chat/completions, /embeddings, /rerank
(vLLM, OpenAI, DeepSeek, Together, OpenRouter, Ollama, vLLM gateways...).
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
import uuid
from typing import Any, AsyncIterator, Sequence

import httpx

from ..config import ArgoConfig
from ..core.types import LLMResponse, Message, ToolCall, Usage


_SEMAPHORES: dict[tuple[int, int], asyncio.Semaphore] = {}


def provider_concurrency() -> int:
    """Max in-flight provider requests per event loop (``ARGO_MAX_CONCURRENCY``, default 2).

    The harness and an interactive session often share one rate-limited key; a hard cap plus jittered
    backoff is what keeps one process from starving the other (429 storms observed in practice).
    """
    try:
        return max(1, int(os.environ.get("ARGO_MAX_CONCURRENCY", "2")))
    except ValueError:
        return 2


def _limiter() -> asyncio.Semaphore:
    limit = provider_concurrency()
    key = (id(asyncio.get_running_loop()), limit)
    sem = _SEMAPHORES.get(key)
    if sem is None:
        sem = asyncio.Semaphore(limit)
        _SEMAPHORES[key] = sem
    return sem


def backoff_delay(attempt: int, retry_after: str | None = None) -> float:
    """Honor Retry-After when present, else exponential backoff with jitter (capped at 60 s)."""
    if retry_after:
        try:
            return min(max(float(retry_after), 0.5), 60.0)
        except ValueError:
            pass
    base = min(60.0, float(2 ** attempt))
    return min(60.0, base * (0.75 + random.random() * 0.5))


class LLMError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class StreamDelta:
    __slots__ = ("text", "reasoning", "tool_call", "done", "final")

    def __init__(self, text: str = "", reasoning: str = "", tool_call: dict | None = None, done: bool = False):
        self.text = text
        self.reasoning = reasoning
        self.tool_call = tool_call
        self.done = done
        self.final = None


def responses_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Convert chat-completions tool schemas to the Responses API flat tool shape."""
    out: list[dict[str, Any]] = []
    for tool in tools or []:
        fn = tool.get("function", tool)
        if not fn.get("name"):
            continue
        out.append({
            "type": "function",
            "name": fn["name"],
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def messages_to_responses(messages: Sequence[Message]) -> tuple[str, list[dict[str, Any]]]:
    """Convert internal messages to (instructions, Responses input items).

    System messages become top-level instructions; assistant tool calls replay as
    function_call items and tool results as function_call_output items.
    """
    instructions: list[str] = []
    items: list[dict[str, Any]] = []
    for msg in messages:
        if msg.internal:
            continue
        if msg.role == "system":
            if msg.content:
                instructions.append(msg.content)
        elif msg.role == "user":
            content: list[dict[str, Any]] = []
            if msg.content:
                content.append({"type": "input_text", "text": msg.content})
            for image in msg.images:
                content.append({"type": "input_image", "image_url": image, "detail": "auto"})
            items.append({"role": "user", "content": content or [{"type": "input_text", "text": ""}]})
        elif msg.role == "assistant":
            if msg.content:
                items.append({"role": "assistant",
                              "content": [{"type": "input_text", "text": msg.content}]})
            for call in msg.tool_calls:
                items.append({"type": "function_call", "call_id": call.id, "name": call.name,
                              "arguments": call.arguments or "{}"})
        elif msg.role == "tool":
            items.append({"type": "function_call_output", "call_id": msg.tool_call_id or "",
                          "output": msg.content or ""})
    return "\n\n".join(instructions), items


def usage_from_responses(raw: dict[str, Any] | None) -> Usage:
    raw = raw or {}
    prompt = int(raw.get("input_tokens") or 0)
    completion = int(raw.get("output_tokens") or 0)
    idet = raw.get("input_tokens_details") or {}
    odet = raw.get("output_tokens_details") or {}
    return Usage(prompt_tokens=prompt, completion_tokens=completion,
                 total_tokens=int(raw.get("total_tokens") or (prompt + completion)),
                 cached_tokens=int(idet.get("cached_tokens") or 0),
                 reasoning_tokens=int(odet.get("reasoning_tokens") or 0))


def parse_responses(data: dict[str, Any]) -> tuple[Message, str]:
    """Assemble a Message and finish_reason from a Responses API object."""
    texts: list[str] = []
    reasoning: list[str] = []
    calls: list[ToolCall] = []
    for item in data.get("output") or []:
        kind = item.get("type")
        if kind == "message":
            for part in item.get("content") or []:
                if part.get("type") in ("output_text", "text") and part.get("text"):
                    texts.append(part["text"])
        elif kind == "reasoning":
            for part in item.get("content") or []:
                if part.get("type") in ("reasoning_text", "text") and part.get("text"):
                    reasoning.append(part["text"])
            for part in item.get("summary") or []:
                if part.get("text"):
                    reasoning.append(part["text"])
        elif kind == "function_call":
            calls.append(ToolCall(id=item.get("call_id") or item.get("id") or "",
                                  name=item.get("name", ""),
                                  arguments=item.get("arguments") or "{}"))
    msg = Message(role="assistant", content="".join(texts) or None,
                  tool_calls=calls, reasoning="".join(reasoning) or None)
    status = data.get("status") or "completed"
    if status == "incomplete" or data.get("incomplete_details"):
        finish = "length"
    elif calls:
        finish = "tool_calls"
    else:
        finish = "stop"
    return msg, finish


class LLMClient:
    def __init__(self, cfg: ArgoConfig, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self._client = client
        self._owns = client is None

    async def __aenter__(self) -> "LLMClient":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=30.0))
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._owns and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=30.0))
            self._owns = True
        return self._client

    def _headers(self) -> dict[str, str]:
        return self.cfg.provider.headers

    async def _post(self, path: str, payload: dict[str, Any], retries: int = 6) -> dict[str, Any]:
        url = f"{self.cfg.provider.base_url}{path}"
        last: Exception | None = None
        for attempt in range(retries):
            try:
                async with _limiter():
                    resp = await self.client.post(url, headers=self._headers(), json=payload)
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                    await asyncio.sleep(backoff_delay(attempt, resp.headers.get("retry-after")))
                    continue
                if resp.status_code >= 400:
                    raise LLMError(f"{resp.status_code} from {url}", resp.status_code, resp.text[:2000])
                return resp.json()
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = exc
                if attempt < retries - 1:
                    await asyncio.sleep(min(2 ** attempt, 15))
                    continue
                raise LLMError(f"transport error: {exc}") from exc
        raise LLMError(f"request failed: {last}")

    def _base_payload(self, messages: Sequence[Message], tools: list[dict[str, Any]] | None,
                      **kwargs: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": kwargs.get("model") or self.cfg.provider.model,
            "messages": [m.to_openai() for m in messages if not m.internal],
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = kwargs.get("tool_choice", "auto")
        temp = kwargs.get("temperature", self.cfg.models.temperature)
        if temp is not None:
            payload["temperature"] = temp
        if kwargs.get("max_tokens") or self.cfg.models.max_output_tokens:
            payload["max_tokens"] = kwargs.get("max_tokens") or self.cfg.models.max_output_tokens
        for key in ("top_p", "stop", "seed", "presence_penalty", "frequency_penalty", "response_format"):
            if kwargs.get(key) is not None:
                payload[key] = kwargs[key]
        effort = kwargs.get("reasoning_effort") or self.cfg.models.reasoning_effort
        if effort:
            payload["reasoning_effort"] = effort
        return payload

    async def chat(self, messages: Sequence[Message], tools: list[dict[str, Any]] | None = None,
                   **kwargs: Any) -> LLMResponse:
        if self.cfg.provider.wire_api == "responses":
            return await self._chat_responses(messages, tools, **kwargs)
        payload = self._base_payload(messages, tools, **kwargs)
        started = time.monotonic()
        data = await self._post("/chat/completions", payload)
        choice = (data.get("choices") or [{}])[0]
        msg = Message.from_openai(choice.get("message") or {})
        return LLMResponse(
            message=msg,
            usage=Usage.from_openai(data.get("usage")),
            finish_reason=choice.get("finish_reason") or "stop",
            raw=data,
            latency_s=time.monotonic() - started,
            model=data.get("model", payload["model"]),
        )

    def _responses_payload(self, messages: Sequence[Message], tools: list[dict[str, Any]] | None,
                           **kwargs: Any) -> dict[str, Any]:
        instructions, items = messages_to_responses(messages)
        payload: dict[str, Any] = {
            "model": kwargs.get("model") or self.cfg.provider.model,
            "input": items,
        }
        if instructions:
            payload["instructions"] = instructions
        converted = responses_tools(tools)
        if converted:
            payload["tools"] = converted
            payload["tool_choice"] = kwargs.get("tool_choice", "auto")
        temp = kwargs.get("temperature", self.cfg.models.temperature)
        if temp is not None:
            payload["temperature"] = temp
        max_out = kwargs.get("max_tokens") or self.cfg.models.max_output_tokens
        if max_out:
            payload["max_output_tokens"] = max_out
        if kwargs.get("top_p") is not None:
            payload["top_p"] = kwargs["top_p"]
        effort = kwargs.get("reasoning_effort") or self.cfg.models.reasoning_effort
        if effort:
            payload["reasoning"] = {"effort": effort}
        if kwargs.get("response_format") is not None:
            payload["text"] = {"format": kwargs["response_format"]}
        return payload

    async def _chat_responses(self, messages: Sequence[Message], tools: list[dict[str, Any]] | None,
                              **kwargs: Any) -> LLMResponse:
        payload = self._responses_payload(messages, tools, **kwargs)
        started = time.monotonic()
        data = await self._post("/responses", payload)
        msg, finish = parse_responses(data)
        return LLMResponse(message=msg, usage=usage_from_responses(data.get("usage")),
                           finish_reason=finish, raw=data, latency_s=time.monotonic() - started,
                           model=data.get("model", payload["model"]))

    async def _stream_responses(self, messages: Sequence[Message], tools: list[dict[str, Any]] | None,
                                **kwargs: Any) -> AsyncIterator[StreamDelta]:
        payload = self._responses_payload(messages, tools, **kwargs)
        payload["stream"] = True
        url = f"{self.cfg.provider.base_url}/responses"
        started = time.monotonic()
        text_parts: list[str] = []
        reason_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        completed: dict[str, Any] | None = None
        model = payload["model"]

        for attempt in range(6):
            try:
                async with _limiter(), self.client.stream("POST", url, headers=self._headers(),
                                                          json=payload) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", "replace")[:2000]
                        if resp.status_code in (429, 500, 502, 503, 504) and attempt < 5:
                            await asyncio.sleep(backoff_delay(attempt, resp.headers.get("retry-after")))
                            continue
                        raise LLMError(f"{resp.status_code} from {url}", resp.status_code, body)
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        chunk = line[5:].strip()
                        if chunk == "[DONE]":
                            break
                        try:
                            event = json.loads(chunk)
                        except json.JSONDecodeError:
                            continue
                        kind = event.get("type", "")
                        if kind == "response.reasoning_text.delta" and event.get("delta"):
                            reason_parts.append(event["delta"])
                            yield StreamDelta(reasoning=event["delta"])
                        elif kind == "response.output_text.delta" and event.get("delta"):
                            text_parts.append(event["delta"])
                            yield StreamDelta(text=event["delta"])
                        elif kind == "response.output_item.added":
                            item = event.get("item") or {}
                            if item.get("type") == "function_call":
                                idx = int(event.get("output_index", len(calls)))
                                calls[idx] = {"id": item.get("call_id") or item.get("id") or "",
                                              "name": item.get("name", ""), "arguments": item.get("arguments") or ""}
                                yield StreamDelta(tool_call={"index": idx, **calls[idx]})
                        elif kind == "response.function_call_arguments.delta":
                            idx = int(event.get("output_index", 0))
                            slot = calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                            slot["arguments"] += event.get("delta") or ""
                            yield StreamDelta(tool_call={"index": idx, **slot})
                        elif kind == "response.output_item.done":
                            item = event.get("item") or {}
                            if item.get("type") == "function_call":
                                idx = int(event.get("output_index", 0))
                                slot = calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                                slot.update({"id": item.get("call_id") or item.get("id") or slot["id"],
                                             "name": item.get("name") or slot["name"],
                                             "arguments": item.get("arguments") or slot["arguments"]})
                        elif kind == "response.completed":
                            completed = event.get("response") or {}
                            model = completed.get("model", model)
                    break
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < 5:
                    text_parts.clear()
                    reason_parts.clear()
                    calls.clear()
                    await asyncio.sleep(backoff_delay(attempt))
                    continue
                raise LLMError(f"transport error: {exc}") from exc

        if completed is not None:
            msg, finish = parse_responses(completed)
            usage = usage_from_responses(completed.get("usage"))
        else:
            tool_calls = [ToolCall(id=c["id"] or f"call_{uuid.uuid4().hex[:8]}", name=c["name"],
                                   arguments=c["arguments"] or "{}")
                          for _, c in sorted(calls.items())]
            msg = Message(role="assistant", content="".join(text_parts) or None,
                          tool_calls=tool_calls, reasoning="".join(reason_parts) or None)
            finish = "tool_calls" if tool_calls else "stop"
            usage = Usage()
        usage.total_tokens = usage.total_tokens or (usage.prompt_tokens + usage.completion_tokens)
        final = LLMResponse(message=msg, usage=usage, finish_reason=finish, raw=completed or {},
                            latency_s=time.monotonic() - started, model=model)
        done = StreamDelta(done=True)
        done.final = final  # type: ignore[attr-defined]
        yield done

    async def stream_chat(self, messages: Sequence[Message], tools: list[dict[str, Any]] | None = None,
                          **kwargs: Any) -> AsyncIterator[StreamDelta]:
        """Yield text/reasoning deltas, then a final delta with .done and assembled response.

        The assembled final LLMResponse is available as delta.final_response on the done delta
        via attribute `done_delta` returned by `collect_stream`.
        """
        if self.cfg.provider.wire_api == "responses":
            async for delta in self._stream_responses(messages, tools, **kwargs):
                yield delta
            return
        payload = self._base_payload(messages, tools, **kwargs)
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        url = f"{self.cfg.provider.base_url}/chat/completions"
        started = time.monotonic()

        text_parts: list[str] = []
        reason_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        usage = Usage()
        finish = "stop"
        model = payload["model"]

        for attempt in range(6):
            try:
                async with _limiter(), self.client.stream("POST", url, headers=self._headers(),
                                                          json=payload) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", "replace")[:2000]
                        if resp.status_code in (429, 500, 502, 503, 504) and attempt < 5:
                            await asyncio.sleep(backoff_delay(attempt, resp.headers.get("retry-after")))
                            continue
                        raise LLMError(f"{resp.status_code} from {url}", resp.status_code, body)
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        chunk = line[5:].strip()
                        if chunk == "[DONE]":
                            break
                        try:
                            data = json.loads(chunk)
                        except json.JSONDecodeError:
                            continue
                        if data.get("usage"):
                            usage = Usage.from_openai(data["usage"])
                        model = data.get("model", model)
                        for choice in data.get("choices") or []:
                            finish = choice.get("finish_reason") or finish
                            delta = choice.get("delta") or {}
                            if delta.get("reasoning") or delta.get("reasoning_content"):
                                piece = delta.get("reasoning") or delta.get("reasoning_content") or ""
                                reason_parts.append(piece)
                                yield StreamDelta(reasoning=piece)
                            if delta.get("content"):
                                piece = delta["content"]
                                text_parts.append(piece)
                                yield StreamDelta(text=piece)
                            for tc in delta.get("tool_calls") or []:
                                idx = int(tc.get("index", 0))
                                slot = calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                                if tc.get("id"):
                                    slot["id"] = tc["id"]
                                fn = tc.get("function") or {}
                                if fn.get("name"):
                                    slot["name"] += fn["name"]
                                if fn.get("arguments"):
                                    slot["arguments"] += fn["arguments"]
                                yield StreamDelta(tool_call={"index": idx, **slot})
                    break
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < 3:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise LLMError(f"transport error: {exc}") from exc

        tool_calls = [
            ToolCall(id=c["id"] or f"call_{uuid.uuid4().hex[:8]}", name=c["name"], arguments=c["arguments"] or "{}")
            for _, c in sorted(calls.items())
        ]
        msg = Message(role="assistant", content="".join(text_parts) or None,
                      tool_calls=tool_calls, reasoning="".join(reason_parts) or None)
        usage.total_tokens = usage.total_tokens or (usage.prompt_tokens + usage.completion_tokens)
        final = LLMResponse(message=msg, usage=usage, finish_reason=finish, raw={},
                            latency_s=time.monotonic() - started, model=model)
        done = StreamDelta(done=True)
        done.final = final  # type: ignore[attr-defined]
        yield done

    async def collect_stream(self, messages: Sequence[Message], tools: list[dict[str, Any]] | None = None,
                             on_delta=None, **kwargs: Any) -> LLMResponse:
        final: LLMResponse | None = None
        async for delta in self.stream_chat(messages, tools, **kwargs):
            if delta.done:
                final = delta.final  # type: ignore[attr-defined]
            elif on_delta:
                await on_delta(delta)
        if final is None:
            raise LLMError("stream ended without completion")
        return final

    async def embed(self, texts: Sequence[str], model: str | None = None, batch: int = 32) -> list[list[float]]:
        model = model or self.cfg.models.embedding_model
        if not model or not texts:
            return []
        out: list[list[float]] = []
        for i in range(0, len(texts), batch):
            chunk = list(texts[i:i + batch])
            data = await self._post("/embeddings", {"model": model, "input": chunk})
            rows = sorted(data.get("data", []), key=lambda r: r.get("index", 0))
            out.extend(r["embedding"] for r in rows)
        if out and not self.cfg.models.embedding_dim:
            self.cfg.models.embedding_dim = len(out[0])
        return out

    async def rerank(self, query: str, documents: Sequence[str], model: str | None = None,
                     top_n: int | None = None) -> list[tuple[int, float]]:
        model = model or self.cfg.models.rerank_model
        if not model or not documents:
            return []
        payload: dict[str, Any] = {"model": model, "query": query, "documents": list(documents)}
        if top_n:
            payload["top_n"] = top_n
        data = await self._post("/rerank", payload)
        results = data.get("results") or []
        return [(int(r.get("index", 0)), float(r.get("relevance_score", 0.0))) for r in results]

    async def models(self) -> list[dict[str, Any]]:
        resp = await self.client.get(f"{self.cfg.provider.base_url}/models", headers=self._headers())
        resp.raise_for_status()
        return resp.json().get("data", [])
