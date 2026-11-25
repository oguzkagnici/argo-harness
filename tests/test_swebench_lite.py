"""Offline tests for the SWE-bench-Lite runner: test-file protection, patch extraction, and untracked files."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_bench():
    spec = importlib.util.spec_from_file_location("swebench_lite", ROOT / "benchmarks" / "swebench_lite.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


BENCH = load_bench()


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@e.com")
    git(repo, "config", "user.name", "T")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "core.py").write_text("def f():\n    return 1\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_core.py").write_text("from pkg.core import f\n\ndef test_f():\n    assert f() == 2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    return repo


def inst() -> dict:
    return {
        "instance_id": "pkg__pkg-1",
        "repo": "pkg/pkg",
        "base_commit": "deadbeef",
        "test_patch": (
            "diff --git a/tests/test_core.py b/tests/test_core.py\n"
            "--- a/tests/test_core.py\n+++ b/tests/test_core.py\n"
            "@@ -3,2 +3,2 @@\n-    assert f() == 1\n+    assert f() == 2\n"),
    }


def test_test_files_parsed_from_test_patch():
    assert BENCH.test_files(inst()) == {"tests/test_core.py"}


def test_make_patch_extracts_source_change_and_reverts_tests(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "pkg" / "core.py").write_text("def f():\n    return 2\n")
    (repo / "tests" / "test_core.py").write_text("def test_f():\n    assert True\n")  # tampering

    patch, tampered = BENCH.make_patch(repo, inst())

    assert tampered is True
    assert "pkg/core.py" in patch and "+    return 2" in patch
    assert "tests/test_core.py" not in patch
    assert (repo / "tests" / "test_core.py").read_text().startswith("from pkg.core import f")


def test_make_patch_includes_new_files(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "pkg" / "helper.py").write_text("HELPER = 1\n")

    patch, tampered = BENCH.make_patch(repo, inst())

    assert tampered is False
    assert "pkg/helper.py" in patch and "+HELPER = 1" in patch


def test_make_patch_excludes_argo_state(tmp_path):
    repo = make_repo(tmp_path)
    (repo / ".argo" / "sessions").mkdir(parents=True)
    (repo / ".argo" / "sessions" / "s.jsonl").write_text("{}\n")

    patch, _ = BENCH.make_patch(repo, inst())

    assert ".argo" not in patch
