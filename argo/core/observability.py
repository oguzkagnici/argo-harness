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


def _price_for(model: str, prices: dict) -> tuple[float, float]:
    best_key, best = "", (0.0, 0.0)
    for key, val in (prices or {}).items():
        if model and str(key).lower() in model.lower() and len(str(key)) > len(best_key):
            best_key = str(key)
            best = (float(val.get("input", 0.0)), float(val.get("output", 0.0)))
    return best


def summarize(paths: Iterable[str | Path], model_prices: dict | None = None,
              since: float | None = None) -> dict[str, Any]:
    sessions = 0
    runs: list[float] = []
    prompt_tokens = completion_tokens = total_tokens = 0
    tool_calls = tool_errors = 0
    tool_times: dict[str, list[float]] = {}
    compactions = resets = 0
    cost_usd = 0.0
    stops: Counter[str] = Counter()
    for path in paths:
        events = load_events(path)
        if not events:
            continue
        sessions += 1
        run_tokens = 0.0
        saw_run_end = False
        for ev in events:
            if since is not None and float(ev.get("ts") or 0) < since:
                continue
            etype = ev.get("type")
            if etype == "usage":
                prompt, completion, total = usage_fields(ev)
                prompt_tokens += prompt
                completion_tokens += completion
                total_tokens += total
                run_tokens += total
                if model_prices:
                    pin, pout = _price_for(str(ev.get("model") or ""), model_prices)
                    cost_usd += (prompt * pin + completion * pout) / 1_000_000
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
                saw_run_end = True
        if not saw_run_end:
            runs.append(run_tokens)
    slowest = sorted(
        ((name, round(max(times), 2), len(times)) for name, times in tool_times.items()),
        key=lambda item: -item[1])[:5]

    def _pct(values: list[float], q: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
        return round(ordered[idx], 3)

    tool_stats = {
        name: {"calls": len(times), "p50": _pct(times, 0.5), "p95": _pct(times, 0.95),
               "mean": round(statistics.mean(times), 3), "max": round(max(times), 3)}
        for name, times in tool_times.items()
    }
    return {
        "sessions": sessions,
        "runs": len(runs),
        "tokens": {"prompt": prompt_tokens, "completion": completion_tokens, "total": total_tokens},
        "tokens_per_run": {"mean": round(statistics.mean(runs), 1) if runs else 0,
                           "max": round(max(runs), 1) if runs else 0},
        "tools": {"calls": tool_calls, "errors": tool_errors,
                  "error_rate": round(tool_errors / tool_calls, 4) if tool_calls else 0.0,
                  "slowest": slowest, "by_tool": tool_stats},
        "compactions": compactions,
        "resets": resets,
        "stop_reasons": dict(stops),
        "tokens_per_tool_call": round(total_tokens / tool_calls, 1) if tool_calls else 0.0,
        "cost": {"usd": round(cost_usd, 4),
                 "per_run": round(cost_usd / len(runs), 6) if runs else 0.0,
                 "priced": bool(model_prices)},
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


def to_otel_spans(events: list[dict[str, Any]], session_id: str) -> list[dict[str, Any]]:
    """Convert a session JSONL into OpenTelemetry spans (GenAI semantic conventions).

    Emits one ``invoke_agent`` root span per run, one ``execute_tool`` span per tool call,
    ``gen_ai.usage.*`` attributes on usage events, and compaction/reset as span events.
    Timestamps are nanoseconds; ``traceId`` is derived deterministically from the session id.
    The output is plain JSON so it can be posted to an OTLP/HTTP collector or inspected offline.
    """
    import hashlib
    import uuid

    def span_id(seed: str, n: int) -> str:
        return hashlib.sha1(f"{session_id}:{seed}:{n}".encode()).hexdigest()[:16]

    def nano(ts: Any) -> int:
        return int(float(ts or 0) * 1_000_000_000)

    trace_id = hashlib.sha1(session_id.encode()).hexdigest()[:32]
    spans: list[dict[str, Any]] = []
    run_seed = 0
    run_start: dict[str, Any] | None = None
    run_attrs: dict[str, Any] = {}
    events_in_run: list[dict[str, Any]] = []

    def close_run(end_ts: Any) -> None:
        nonlocal run_start
        if run_start is None:
            return
        attrs = dict(run_attrs)
        spans.append({
            "traceId": trace_id,
            "spanId": span_id("run", run_seed),
            "name": "invoke_agent",
            "kind": 1,
            "startTimeUnixNano": nano(run_start.get("ts")),
            "endTimeUnixNano": nano(end_ts or run_start.get("ts")),
            "attributes": attrs,
            "events": events_in_run[-50:],
        })
        run_start = None

    for i, ev in enumerate(events):
        etype = ev.get("type")
        if etype == "run_start":
            close_run(ev.get("ts"))
            run_seed += 1
            run_start = ev
            run_attrs = {"gen_ai.operation.name": "invoke_agent",
                         "gen_ai.conversation.id": session_id,
                         "argo.task": str(ev.get("task") or "")[:500]}
            events_in_run = []
        elif etype == "usage":
            _p, _c, total = usage_fields(ev)
            run_attrs["gen_ai.usage.total_tokens"] = int(run_attrs.get("gen_ai.usage.total_tokens", 0)) + total
            if ev.get("model"):
                run_attrs["gen_ai.request.model"] = str(ev["model"])
        elif etype == "tool":
            seconds = float(ev.get("duration_s") or 0.0)
            end = float(ev.get("ts") or 0)
            name = str(ev.get("name") or "tool")
            attrs = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name,
                     "argo.tool.is_error": bool(ev.get("is_error"))}
            if seconds:
                attrs["argo.tool.duration_s"] = round(seconds, 4)
            spans.append({
                "traceId": trace_id,
                "spanId": span_id("tool", i),
                "parentSpanId": span_id("run", run_seed) if run_start is not None else "",
                "name": f"execute_tool {name}",
                "kind": 1,
                "startTimeUnixNano": nano(end - seconds),
                "endTimeUnixNano": nano(end),
                "attributes": attrs,
                "events": [],
            })
        elif etype in ("context_compacted", "context_reset", "loop_stop", "context_compact_disabled"):
            events_in_run.append({"name": str(etype), "timeUnixNano": nano(ev.get("ts")),
                                  "attributes": {k: v for k, v in ev.items() if k not in ("ts", "type")}})
        elif etype == "run_end":
            run_attrs["argo.stop_reason"] = str(ev.get("stop_reason") or "")
            run_attrs["argo.iterations"] = int(ev.get("iterations") or 0)
            close_run(ev.get("ts"))
    close_run(events[-1].get("ts") if events else 0)
    return spans


def otel_payload(spans: list[dict[str, Any]], service_name: str = "argo") -> dict[str, Any]:
    """Wrap spans in an OTLP/JSON ``resourceSpans`` envelope (no protobuf dependency)."""
    return {"resourceSpans": [{
        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service_name}},
                                    {"key": "telemetry.sdk.name", "value": {"stringValue": "argo"}}]},
        "scopeSpans": [{"scope": {"name": "argo.harness"}, "spans": spans}],
    }]}
