"""Typed messages, tool calls, results, and usage. Serializes to OpenAI wire format."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str = "{}"

    def args(self) -> dict[str, Any]:
        try:
            data = json.loads(self.arguments or "{}")
            return data if isinstance(data, dict) else {"value": data}
        except json.JSONDecodeError:
            return {}

    def to_openai(self) -> dict[str, Any]:
        return {"id": self.id, "type": "function",
                "function": {"name": self.name, "arguments": self.arguments or "{}"}}


@dataclass(slots=True)
class Message:
    role: str                      # system | user | assistant | tool
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    reasoning: str | None = None
    images: list[str] = field(default_factory=list)   # data URLs / http URLs
    internal: bool = False         # excluded from provider payload (e.g. summaries)

    def to_openai(self, include_reasoning: bool = False) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": self.role}
        if self.images:
            parts: list[dict[str, Any]] = []
            if self.content:
                parts.append({"type": "text", "text": self.content})
            for url in self.images:
                parts.append({"type": "image_url", "image_url": {"url": url}})
            msg["content"] = parts
        else:
            msg["content"] = self.content
        if self.tool_calls:
            msg["tool_calls"] = [tc.to_openai() for tc in self.tool_calls]
            if msg["content"] is None:
                msg["content"] = None
        if self.tool_call_id:
            msg["tool_call_id"] = self.tool_call_id
        if self.name:
            msg["name"] = self.name
        if include_reasoning and self.reasoning:
            msg["reasoning_content"] = self.reasoning
        return msg

    @classmethod
    def from_openai(cls, raw: dict[str, Any]) -> "Message":
        calls = []
        for tc in raw.get("tool_calls") or []:
            fn = tc.get("function", {})
            calls.append(ToolCall(id=tc.get("id", ""), name=fn.get("name", ""),
                                  arguments=fn.get("arguments") or "{}"))
        content = raw.get("content")
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return cls(
            role=raw.get("role", "assistant"),
            content=content,
            tool_calls=calls,
            tool_call_id=raw.get("tool_call_id"),
            name=raw.get("name"),
            reasoning=raw.get("reasoning") or raw.get("reasoning_content"),
        )


@dataclass(slots=True)
class ToolResult:
    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    display: str | None = None
    truncation: dict[str, Any] | None = None


@dataclass(slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens
        self.cached_tokens += other.cached_tokens
        self.reasoning_tokens += other.reasoning_tokens

    @classmethod
    def from_openai(cls, raw: dict[str, Any] | None) -> "Usage":
        raw = raw or {}
        pd = raw.get("prompt_tokens_details") or {}
        cd = raw.get("completion_tokens_details") or {}
        return cls(
            prompt_tokens=int(raw.get("prompt_tokens") or 0),
            completion_tokens=int(raw.get("completion_tokens") or 0),
            total_tokens=int(raw.get("total_tokens") or 0),
            cached_tokens=int(pd.get("cached_tokens") or 0),
            reasoning_tokens=int(cd.get("reasoning_tokens") or 0),
        )

    def as_dict(self) -> dict[str, int]:
        return {"prompt": self.prompt_tokens, "completion": self.completion_tokens,
                "total": self.total_tokens, "cached": self.cached_tokens,
                "reasoning": self.reasoning_tokens}


@dataclass(slots=True)
class LLMResponse:
    message: Message
    usage: Usage
    finish_reason: str = "stop"
    raw: dict[str, Any] = field(default_factory=dict)
    latency_s: float = 0.0
    model: str = ""
