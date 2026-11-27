"""Offline tests for the SWE-bench-Lite runner: test-file protection, patch extraction, and untracked files."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_bench():
    spec = importlib.util.spec_from_file_location(
        "swebench_lite", ROOT / "benchmarks" / "swebench_lite.py")
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


def test_fail_to_pass_parses_json_string_and_list():
    import json as _json
    assert BENCH.fail_to_pass({"FAIL_TO_PASS": _json.dumps(["a::b", "c::d"])}) == ["a::b", "c::d"]
    assert BENCH.fail_to_pass({"FAIL_TO_PASS": ["x::y"]}) == ["x::y"]
    assert BENCH.fail_to_pass({"fail_to_pass": "z::w"}) == ["z::w"]
    assert BENCH.fail_to_pass({}) == []


def test_test_command_targets_only_failing_tests():
    cmd = BENCH.test_command({"FAIL_TO_PASS": '["tests/t.py::test_a", "tests/t.py::test_b"]'})
    assert cmd.startswith("python -m pytest -q ")
    assert '"tests/t.py::test_a"' in cmd and '"tests/t.py::test_b"' in cmd
    assert BENCH.test_command({}) == ""


def test_test_command_resolves_bare_names_to_test_patch_file():
    instance = inst()
    instance["FAIL_TO_PASS"] = '["test_imageset", "test_intersection"]'
    cmd = BENCH.test_command(instance)
    assert '"tests/test_core.py"' in cmd
    assert '-k "test_imageset or test_intersection"' in cmd


def test_feedback_prompt_includes_command():
    text = BENCH.PROMPT_FEEDBACK.format(repo="pkg/pkg", problem="bug", test_command="python -m pytest -q x")
    assert "python -m pytest -q x" in text
    assert "MUST run the failing tests" in text


def test_spec_python_and_pytest_pin_from_official_scripts():
    spec = {"_scripts": ["conda create -n testbed python=3.9 -y",
                         "python -m pip install pytest==7.3.0 flask==2.0"]}
    assert BENCH.spec_python(spec) == "3.9"
    assert BENCH.pytest_pin(spec) == "pytest==7.3.0"
    assert BENCH.spec_python({}) == "3.11"
    assert BENCH.pytest_pin({}) == "pytest"


def test_official_spec_falls_back_gracefully():
    assert BENCH.official_spec({"repo": "nope/nope", "base_commit": "x"}) == {}


def test_test_command_uses_official_runner(monkeypatch):
    monkeypatch.setattr(BENCH, "official_spec",
                        lambda inst: {"test_cmd": "./tests/runtests.py --verbosity 2 --settings=test_sqlite"})
    inst = {"FAIL_TO_PASS": ["test_a (app.tests.Tests)", "test_b (app.tests.Tests)"],
            "test_patch": "diff --git a/app/tests.py b/app/tests.py\n"}
    cmd = BENCH.test_command(inst)
    assert cmd.startswith("./tests/runtests.py --verbosity 2 --settings=test_sqlite")
    assert "test_a (app.tests.Tests)" in cmd and "test_b (app.tests.Tests)" in cmd


def test_test_command_falls_back_to_pytest(monkeypatch):
    monkeypatch.setattr(BENCH, "official_spec", lambda inst: {})
    inst = {"FAIL_TO_PASS": ["pkg/tests/test_x.py::test_one"],
            "test_patch": "diff --git a/pkg/tests/test_x.py b/pkg/tests/test_x.py\n"}
    cmd = BENCH.test_command(inst)
    assert cmd.startswith("python -m pytest -q")
    assert "pkg/tests/test_x.py::test_one" in cmd
