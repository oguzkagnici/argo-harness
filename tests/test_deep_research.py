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


def test_matcher_rejects_refusals_and_accepts_glossed_answers():
    m = load()
    omega = next(q for q in m.QUESTIONS if q["id"] == "omega_budget")
    beta = next(q for q in m.QUESTIONS if q["id"] == "beta_budget")
    # a refusal that quotes the decoy value must score as wrong
    assert not m.answer_matches(
        "Not stated in any authoritative document (only a decoy claims $9.9M)", omega)
    assert not m.answer_matches("The answer is not mentioned; maybe 2.4M", beta)
    # a short trailing gloss is still the answer
    assert m.answer_matches("$9.9M (proposed budget)", omega)
    assert m.answer_matches("The answer is $2.4M", beta)
    # an answer buried in a long reasoning line is not an exact match
    assert not m.answer_matches("$2.4M in Q3 and $3.3M in Q4, total $5.7M", beta)


def test_page_paths_do_not_leak_answer_role():
    m = load()
    # fixture hygiene: no page path may hint that it is a decoy/stale page by name
    for path in m.PAGES:
        assert not any(word in path for word in ("distractor", "decoy", "fake", "wrong")), path


def test_docs_index_lists_every_page():
    m = load()
    import urllib.request
    srv, base = m.serve(m.load_pages())
    try:
        index = urllib.request.urlopen(f"{base}/docs", timeout=5).read().decode()
        for path in m.load_pages():
            assert f"/{path}" in index, path
    finally:
        srv.shutdown()
