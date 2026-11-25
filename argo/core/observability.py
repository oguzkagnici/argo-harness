"""Session observability: trace rendering and aggregate metrics from JSONL transcripts.

Session files are append-only JSONL. These helpers turn them into a timeline and into numbers an operator
needs for always-on work: token totals, tool error rate, slowest tools, compaction/reset counts.
"""
from __future__ import annotations

import json
import statistics
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


def usage_fields(ev: dict[str, Any]) -> tuple[int, int, int]:
    """Session JSONL uses short keys (prompt/completion/total); live bus events use *_tokens."""
    return (int(ev.get("prompt_tokens") or ev.get("prompt") or 0),
            int(ev.get("completion_tokens") or ev.get("completion") or 0),
            int(ev.get("total_tokens") or ev.get("total") or 0))


def load_events(path: str | Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return events
    for line in lines:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def summarize(paths: Iterable[str | Path]) -> dict[str, Any]:
    sessions = 0
    runs: list[float] = []
    prompt_tokens = completion_tokens = total_tokens = 0
    tool_calls = tool_errors = 0
    tool_times: dict[str, list[float]] = {}
    compactions = resets = 0
    stops: Counter[str] = Counter()
    for path in paths:
        events = load_events(path)
        if not events:
            continue
        sessions += 1
        run_tokens = 0.0
        for ev in events:
            etype = ev.get("type")
            if etype == "usage":
                prompt, completion, total = usage_fields(ev)
                prompt_tokens += prompt
                completion_tokens += completion
                total_tokens += total
                run_tokens += total
            elif etype == "tool":
                tool_calls += 1
                if ev.get("is_error"):
                    tool_errors += 1
                seconds = ev.get("duration_s")
                if isinstance(seconds, (int, float)):
                    tool_times.setdefault(str(ev.get("name")), []).append(float(seconds))
            elif etype == "context_compacted":
                compactions += 1
            elif etype == "context_reset":
                resets += 1
            elif etype == "run_end":
                stops[str(ev.get("stop_reason"))] += 1
                runs.append(run_tokens)
        if not any(ev.get("type") == "run_end" for ev in events):
            runs.append(run_tokens)
    slowest = sorted(
        ((name, round(max(times), 2), len(times)) for name, times in tool_times.items()),
        key=lambda item: -item[1])[:5]
    return {
        "sessions": sessions,
        "runs": len(runs),
        "tokens": {"prompt": prompt_tokens, "completion": completion_tokens, "total": total_tokens},
        "tokens_per_run": {"mean": round(statistics.mean(runs), 1) if runs else 0,
                           "max": round(max(runs), 1) if runs else 0},
        "tools": {"calls": tool_calls, "errors": tool_errors,
                  "error_rate": round(tool_errors / tool_calls, 4) if tool_calls else 0.0,
                  "slowest": slowest},
        "compactions": compactions,
        "resets": resets,
        "stop_reasons": dict(stops),
    }


def render_trace(events: list[dict[str, Any]], limit: int = 200) -> list[str]:
    if not events:
        return ["(no events)"]
    t0 = float(events[0].get("ts") or 0)
    lines: list[str] = []
    for ev in events[-limit:]:
        offset = float(ev.get("ts") or t0) - t0
        etype = ev.get("type")
        if etype == "tool":
            mark = " ERROR" if ev.get("is_error") else ""
            dur = ev.get("duration_s")
            dur_s = f" {dur:.2f}s" if isinstance(dur, (int, float)) else ""
            lines.append(f"{offset:7.2f}s  tool   {ev.get('name')}{dur_s}{mark}  "
                         f"{(ev.get('result') or '')[:80].replace(chr(10), ' ')}")
        elif etype == "usage":
            prompt, completion, total = usage_fields(ev)
            lines.append(f"{offset:7.2f}s  usage  {total} tok (prompt {prompt}, completion {completion})")
        elif etype == "message":
            role = ev.get("role")
            calls = ", ".join(c.get("name", "?") for c in (ev.get("tool_calls") or []))
            body = (ev.get("content") or "").replace("\n", " ")[:90]
            lines.append(f"{offset:7.2f}s  {role:<6} {body}{(' -> ' + calls) if calls else ''}")
        elif etype in ("run_start", "run_end", "context_compacted", "context_reset", "auto_review",
                       "final", "error"):
            detail = {k: v for k, v in ev.items() if k not in ("ts", "type")}
            lines.append(f"{offset:7.2f}s  {etype:<16} {json.dumps(detail, default=str)[:100]}")
    return lines
