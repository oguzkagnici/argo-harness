"""Observability tests: JSONL summary metrics, trace rendering, tool duration capture."""
from __future__ import annotations

import json
from pathlib import Path

from argo.core.observability import load_events, render_trace, summarize
from argo.core.session import Session


def write_session(path: Path, events: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


EVENTS = [
    {"ts": 100.0, "type": "run_start", "task": "research"},
    {"ts": 100.5, "type": "message", "role": "assistant", "content": "fetching",
     "tool_calls": [{"name": "fetch_url"}]},
    {"ts": 102.0, "type": "tool", "name": "fetch_url", "args": {"url": "u"}, "result": "ok",
     "is_error": False, "duration_s": 1.5},
    {"ts": 103.0, "type": "tool", "name": "bash", "args": {}, "result": "boom", "is_error": True,
     "duration_s": 4.25},
    {"ts": 104.0, "type": "context_compacted", "tokens": 1000},
    {"ts": 105.0, "type": "usage", "prompt_tokens": 900, "completion_tokens": 100, "total_tokens": 1000},
    {"ts": 106.0, "type": "run_end", "stop_reason": "final", "iterations": 2, "tokens": 1000},
]


def test_load_events_skips_bad_lines(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text('{"type":"a"}\nnot json\n{"type":"b"}\n', encoding="utf-8")
    assert [e["type"] for e in load_events(p)] == ["a", "b"]
    assert load_events(tmp_path / "missing.jsonl") == []


def test_summarize_aggregates_tokens_errors_and_slowest(tmp_path):
    p1 = write_session(tmp_path / "a.jsonl", EVENTS)
    p2 = write_session(tmp_path / "b.jsonl", [EVENTS[5], dict(EVENTS[5], total_tokens=500),
                                              dict(EVENTS[6], tokens=500)])
    stats = summarize([p1, p2])

    assert stats["sessions"] == 2 and stats["runs"] == 2
    assert stats["tokens"]["total"] == 2500  # p1: 1000, p2: two usage events (1000 + 500)
    assert stats["tokens_per_run"]["max"] == 1500
    assert {k: stats["tools"][k] for k in ("calls", "errors", "error_rate", "slowest")} == {
        "calls": 2, "errors": 1, "error_rate": 0.5,
        "slowest": [("bash", 4.25, 1), ("fetch_url", 1.5, 1)],
    }
    assert stats["tools"]["by_tool"]["bash"] == {"calls": 1, "p50": 4.25, "p95": 4.25,
                                                 "mean": 4.25, "max": 4.25}
    assert stats["tokens_per_tool_call"] == 1250.0  # 2500 tokens / 2 tool calls
    assert stats["compactions"] == 1 and stats["resets"] == 0
    assert stats["stop_reasons"] == {"final": 2}  # both files have a run_end


def test_summarize_ignores_empty_files(tmp_path):
    (tmp_path / "empty.jsonl").write_text("", encoding="utf-8")
    assert summarize([tmp_path / "empty.jsonl"])["sessions"] == 0


def test_render_trace_orders_and_formats():
    lines = render_trace(EVENTS)
    assert lines[0].startswith("   0.00s  run_start")
    assert any("tool   fetch_url 1.50s" in line for line in lines)
    assert any("tool   bash 4.25s ERROR" in line for line in lines)
    assert any("usage  1000 tok" in line for line in lines)
    assert any("assistant" in line and "fetch_url" in line for line in lines)
    assert render_trace([]) == ["(no events)"]


def test_render_trace_limit_keeps_tail():
    lines = render_trace(EVENTS, limit=2)
    assert len(lines) == 2


def test_session_records_tool_duration(tmp_path):
    from argo.config import ArgoConfig

    cfg = ArgoConfig(workspace=tmp_path, data_dir=tmp_path / "d", session_dir=tmp_path / "s",
                     index_dir=tmp_path / "i", workflow_dir=tmp_path / "w", skill_dir=tmp_path / "k",
                     spool_dir=tmp_path / "sp")
    cfg.ensure_dirs()
    sess = Session.create(cfg, None)
    sess.log_tool("bash", {"command": "sleep 1"}, "done", False, duration_s=1.25)
    sess.log_tool("bash", {"command": "false"}, "err", True)
    events = [e for e in sess.events() if e["type"] == "tool"]
    assert events[0]["duration_s"] == 1.25
    assert events[1]["is_error"] is True and events[1]["duration_s"] is None


def test_summarize_reads_short_usage_keys(tmp_path):
    p = write_session(tmp_path / "short.jsonl", [
        {"ts": 1.0, "type": "usage", "prompt": 120, "completion": 30, "total": 150},
        {"ts": 2.0, "type": "run_end", "stop_reason": "final"},
    ])
    stats = summarize([p])
    assert stats["tokens"] == {"prompt": 120, "completion": 30, "total": 150}
    assert any("150 tok" in line for line in render_trace(load_events(p)))


async def test_agent_mirrors_compaction_events_to_session(tmp_path):
    import pytest

    from argo.config import ArgoConfig
    from argo.core.agent import Agent
    from argo.core.types import LLMResponse, Message, Usage

    class OneShot:
        bus = None

        async def chat(self, messages, tools=None, **kwargs):
            if OneShot.bus is not None:
                OneShot.bus.emit("context_compacted", summarized=3, kept=2, chars=100)
                OneShot.bus.emit("context_reset", handoff="/tmp/handoff.md")
                OneShot.bus = None
            return LLMResponse(message=Message(role="assistant", content="ok"),
                               usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2))

        async def embed(self, texts, model=None):
            return [[0.0] for _ in texts]

        async def rerank(self, query, docs, top_n=None):
            return list(range(len(docs)))

    cfg = ArgoConfig(workspace=tmp_path, data_dir=tmp_path / "d", session_dir=tmp_path / "s",
                     index_dir=tmp_path / "i", workflow_dir=tmp_path / "w", skill_dir=tmp_path / "k",
                     spool_dir=tmp_path / "sp", daily_token_budget=0)
    cfg.ensure_dirs()
    async with Agent(cfg, llm=OneShot()) as agent:
        OneShot.bus = agent.events
        result = await agent.run("hi")

    types = [e["type"] for e in agent.session.events()]
    assert "context_compacted" in types and "context_reset" in types
    assert [t for t in types if t == "run_end"] == ["run_end"]
    assert result.stop_reason == "final"


def test_summarize_since_filters_old_events(tmp_path):
    events = [
        {"ts": 50.0, "type": "tool", "name": "bash", "is_error": True, "duration_s": 9.0},
        {"ts": 150.0, "type": "tool", "name": "bash", "is_error": False, "duration_s": 1.0},
        {"ts": 160.0, "type": "usage", "prompt": 10, "completion": 5, "total": 15},
        {"ts": 170.0, "type": "run_end", "stop_reason": "final"},
    ]
    p = write_session(tmp_path / "s.jsonl", events)
    stats = summarize([p], since=100.0)
    assert stats["tools"]["calls"] == 1 and stats["tools"]["errors"] == 0
    assert stats["tokens"]["total"] == 15 and stats["runs"] == 1


def test_parse_since_units_and_dates():
    import time as _time
    from argo.cli import _parse_since
    now = _time.time()
    assert 3500 < now - _parse_since("1h") < 3700
    assert _parse_since("7d") < now - 6 * 86400
    assert _parse_since(None) is None
    dated = _parse_since("2020-01-01")
    assert dated > 1_500_000_000 and dated < 1_700_000_000
