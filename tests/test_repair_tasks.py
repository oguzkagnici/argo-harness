"""Offline validation of the repair benchmark task definitions.

Each task must have a reference fix that turns its buggy repo green, and whose
hidden suite the buggy code fails. This keeps ``--self-check`` honest without
spending provider tokens.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_bench():
    spec = importlib.util.spec_from_file_location("repair_bench", ROOT / "benchmarks" / "repair.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


BENCH = load_bench()


def test_task_definitions_are_well_formed():
    ids = [task["id"] for task in BENCH.TASKS]
    assert len(ids) == len(set(ids))
    for task in BENCH.TASKS:
        assert task["id"] and task["source"]
        assert BENCH.test_files(task), f"{task['id']} has no visible tests"
        fixes = task.get("fix_files") or {task["source"]: task["fix"]}
        for rel in fixes:
            assert rel in task["files"], f"{task['id']} fixes a file it never creates: {rel}"
        assert "tests/" not in task["source"], "the reference fix must not touch tests"


def test_ledger_task_is_multi_file():
    task = next(t for t in BENCH.TASKS if t["id"] == "ledger")
    assert set(task["fix_files"]) == {"app/pricing.py", "app/store.py"}
