"""Offline tests for report-level citation parsing and ALCE-style scoring."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location("citation_bench", ROOT / "benchmarks" / "citation.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_parse_report_lines_extracts_refs_and_strips_markers():
    m = load()
    rows = m.parse_report_lines("- Throughput rose 12%. [S1]\n* Cache hit ratio was 91%. [s3][S2]\n\nno cite here")
    assert rows[0] == {"text": "Throughput rose 12%.", "refs": [1]}
    assert rows[1]["refs"] == [2, 3] and "[S3]" not in rows[1]["text"]
    assert rows[2]["refs"] == []


def test_citation_scores_precision_and_recall():
    m = load()
    rows = [
        {"refs": [1], "verdict": "supported"},
        {"refs": [2], "verdict": "partial"},
        {"refs": [], "verdict": "uncited"},
        {"refs": [3, 4], "verdict": "supported"},
    ]
    s = m.citation_scores(rows, {1, 2, 3, 4})
    assert s["cited_lines"] == 3 and s["uncited_lines"] == 1
    assert s["citation_precision"] == round(2 / 3, 3)
    assert s["citation_recall"] == 1.0 and s["missing_required"] == []
    assert s["sources_cited"] == [1, 2, 3, 4]


def test_citation_scores_empty_report():
    m = load()
    s = m.citation_scores([], {1, 2})
    assert s["citation_precision"] == 0.0 and s["citation_recall"] == 0.0
    assert s["missing_required"] == [1, 2]
