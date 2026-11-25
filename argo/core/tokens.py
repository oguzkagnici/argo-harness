"""Fast, dependency-free token estimation. Calibrated for mixed latin/CJK text.

Exact usage is always taken from provider responses; this estimator is only used
for budgeting decisions (compaction triggers, truncation), where ~5-10% error is fine.
"""
from __future__ import annotations

import json
import re
from typing import Any

_CJK = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_WORD = re.compile(r"\w+|[^\w\s]")


def estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    latin = len(text) - cjk
    return int(cjk * 1.05 + latin / 3.9) + 1


def estimate_message(msg: Any) -> int:
    total = 4
    content = getattr(msg, "content", None)
    if content:
        total += estimate_tokens(content)
    reasoning = getattr(msg, "reasoning", None)
    if reasoning:
        total += estimate_tokens(reasoning)
    for tc in getattr(msg, "tool_calls", []) or []:
        total += estimate_tokens(tc.name) + estimate_tokens(tc.arguments) + 8
    if getattr(msg, "images", None):
        total += 850 * len(msg.images)
    return total


def estimate_messages(messages: list[Any]) -> int:
    return sum(estimate_message(m) for m in messages)


def estimate_tools(tools: list[dict[str, Any]]) -> int:
    if not tools:
        return 0
    return estimate_tokens(json.dumps(tools, separators=(",", ":"))) + 16


def estimate_payload(messages: list[Any], tools: list[dict[str, Any]] | None = None) -> int:
    return estimate_messages(messages) + estimate_tools(tools or [])
