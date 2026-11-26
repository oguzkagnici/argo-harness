"""Served trace view: JSON endpoints and the HTML timeline (no network, no model)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from argo.config import ArgoConfig  # noqa: E402
from argo.server import create_app  # noqa: E402

EVENTS = [
    {"ts": 100.0, "type": "run_start", "task": "fix the parser <script>alert(1)</script>"},
    {"ts": 100.5, "type": "message", "role": "assistant", "content": "looking", "tool_calls": [{"name": "bash"}]},
    {"ts": 101.0, "type": "tool", "name": "bash", "duration_s": 0.5, "is_error": False, "result": "ok"},
    {"ts": 101.5, "type": "tool", "name": "bash", "duration_s": 0.25, "is_error": True, "result": "boom"},
    {"ts": 102.0, "type": "usage", "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    {"ts": 102.5, "type": "usage", "prompt": 50, "completion": 10, "total": 60},  # session JSONL shape
    {"ts": 103.0, "type": "run_end", "stop_reason": "final", "iterations": 4},
]


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    cfg = ArgoConfig(workspace=tmp_path, data_dir=tmp_path / "data", session_dir=tmp_path / "sessions",
                     index_dir=tmp_path / "i", workflow_dir=tmp_path / "w", skill_dir=tmp_path / "k",
                     spool_dir=tmp_path / "p")
    cfg.ensure_dirs()
    lines = [json.dumps({"type": "_meta", "workspace": str(tmp_path)})] + [json.dumps(e) for e in EVENTS]
    (Path(cfg.session_dir) / "sess-abc.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return TestClient(create_app(cfg))


def test_session_list_and_detail(client: TestClient):
    rows = client.get("/v1/sessions").json()["data"]
    assert [r["id"] for r in rows] == ["sess-abc"]
    row = rows[0]
    assert row["tokens"] == 180, "both usage shapes are counted"
    assert row["stop_reason"] == "final" and row["iterations"] == 4
    assert row["tool_calls"] == 2 and row["tool_errors"] == 1
    assert row["tools"]["bash"] == {"calls": 2, "seconds": 0.75, "errors": 1}

    detail = client.get("/v1/sessions/sess-abc").json()
    assert detail["session"]["id"] == "sess-abc"
    assert any(e["type"] == "run_end" for e in detail["events"])


def test_unknown_and_path_traversal_sessions_rejected(client: TestClient):
    assert client.get("/v1/sessions/nope").status_code == 404
    assert client.get("/v1/sessions/..%2Fsecret").status_code in (400, 404)


def test_html_trace_escapes_content(client: TestClient):
    index = client.get("/trace")
    assert index.status_code == 200 and "sess-abc" in index.text
    page = client.get("/trace/sess-abc")
    assert page.status_code == 200
    assert "<script>alert(1)</script>" not in page.text, "injected markup must be escaped"
    assert "&lt;script&gt;" in page.text
    assert "timeline" in page.text and "120 tok" in page.text


def test_key_protects_endpoints(tmp_path: Path):
    cfg = ArgoConfig(workspace=tmp_path, data_dir=tmp_path / "d", session_dir=tmp_path / "s",
                     index_dir=tmp_path / "i", workflow_dir=tmp_path / "w", skill_dir=tmp_path / "k",
                     spool_dir=tmp_path / "p")
    cfg.ensure_dirs()
    app = create_app(cfg, require_key="secret")
    c = TestClient(app)
    assert c.get("/v1/sessions").status_code == 401
    assert c.get("/v1/sessions", headers={"Authorization": "Bearer secret"}).status_code == 200
    assert c.get("/trace?key=secret").status_code == 200
    assert c.get("/trace").status_code == 401
