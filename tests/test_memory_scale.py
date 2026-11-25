"""Offline checks for the scalable memory lesson generator used by memory_loop.py."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location("memory_loop_bench", ROOT / "benchmarks" / "memory_loop.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_synthetic_lessons_are_deterministic_and_unique():
    m = load()
    a = m.synthetic_lessons(40, seed=3)
    b = m.synthetic_lessons(40, seed=3)
    assert a == b and len(a) == 40
    assert len({x["query"] for x in a}) == 40
    assert all(x["must_keep"] != x["must_drop"] for x in a)


def test_synthetic_lessons_include_duplicates_and_contradictions():
    m = load()
    lessons = m.synthetic_lessons(15)
    assert any(x["duplicate"] for x in lessons)
    assert any(x["intermediate"] for x in lessons)
