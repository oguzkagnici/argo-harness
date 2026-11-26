"""Offline checks for the exact-answer deep-research benchmark."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location("deep_research", ROOT / "benchmarks" / "deep_research.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_questions_are_unique_and_well_formed():
    m = load()
    assert len({q["id"] for q in m.QUESTIONS}) == len(m.QUESTIONS)
    for q in m.QUESTIONS:
        assert q["question"] and q["answer"]
    corpus = " ".join(m.PAGES.values())
    # direct facts are in the corpus; multi-hop answers are derived from two pages
    for needle in ("ZX-441", "$2.4M", "Mira Chen", "$1.2M", "$1.5M", "2027-03-15", "$9.9M"):
        assert needle in corpus, needle


def test_extract_and_score_answers():
    m = load()
    q = m.QUESTIONS[0]
    assert m.extract_answer("blah\nANSWER: ZX-441.") == "ZX-441"
    assert m.answer_matches("ZX-441", q)
    assert m.answer_matches("zx 441", q)
    assert not m.answer_matches("QQ-000", q)
    assert m.answer_matches("It was 2.4 million dollars", m.QUESTIONS[1])
    assert m.answer_matches("March 15, 2027", m.QUESTIONS[4])


def test_normalize_strips_money_and_commas():
    m = load()
    assert m.normalize("$2,400,000 USD") == "2400000"
    assert m.normalize("  Zx-441 ") == "zx-441"


def test_page_server_serves_cache_and_404s():
    m = load()
    import urllib.error
    import urllib.request
    srv, base = m.serve(m.load_pages())
    try:
        assert "ZX-441" in urllib.request.urlopen(f"{base}/docs/alpha", timeout=5).read().decode()
        try:
            urllib.request.urlopen(f"{base}/docs/nope", timeout=5)
            assert False, "expected 404"
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        srv.shutdown()
