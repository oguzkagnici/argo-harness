"""Git tools against a real temporary repository (no network)."""
from __future__ import annotations

import subprocess

import pytest

from argo.config import load_config
from argo.tools import git as git_tools
from argo.tools.base import ToolContext, ToolRegistry


def run(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    run(root, "init", "-q")
    run(root, "config", "user.email", "argo@example.com")
    run(root, "config", "user.name", "Argo Test")
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    run(root, "add", "-A")
    run(root, "commit", "-qm", "initial commit")
    return root


def ctx_for(root):
    cfg = load_config({"workspace": str(root), "data_dir": str(root / ".argo")})
    return ToolContext(cfg=cfg)


def registry(root):
    reg = ToolRegistry(ctx_for(root))
    git_tools.register(reg)
    return reg


@pytest.mark.asyncio
async def test_status_diff_log_show_and_branches(repo):
    (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "new.txt").write_text("hi\n", encoding="utf-8")
    reg = registry(repo)

    status = await reg.execute("git_status", {})
    assert not status.is_error and "app.py" in status.content and "new.txt" in status.content
    assert status.metadata["changed"] == 2

    diff = await reg.execute("git_diff", {"path": "app.py"})
    assert not diff.is_error and "-x = 1" in diff.content and "+x = 2" in diff.content

    stat = await reg.execute("git_diff", {"stat": True})
    assert not stat.is_error and "app.py" in stat.content

    log = await reg.execute("git_log", {"limit": 5})
    assert not log.is_error and "initial commit" in log.content

    show = await reg.execute("git_show", {"rev": "HEAD"})
    assert not show.is_error and "initial commit" in show.content

    branches = await reg.execute("git_branches", {})
    assert not branches.is_error and "HEAD:" in branches.content


@pytest.mark.asyncio
async def test_commit_stages_lists_and_reports_sha(repo):
    (repo / "app.py").write_text("x = 3\n", encoding="utf-8")
    reg = registry(repo)

    empty = await reg.execute("git_commit", {"message": "  "})
    assert empty.is_error

    nothing = await reg.execute("git_commit", {"message": "no changes"})
    assert nothing.is_error and "Nothing staged" in nothing.content

    committed = await reg.execute("git_commit", {"message": "bump x", "add_all": True})
    assert not committed.is_error, committed.content
    assert committed.metadata["sha"]
    assert "bump x" in run(repo, "log", "-1", "--pretty=%s")
    assert run(repo, "status", "--short").strip() == ""

    # a second commit with explicit paths only stages those
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    partial = await reg.execute("git_commit", {"message": "only a", "paths": "a.txt"})
    assert not partial.is_error, partial.content
    assert "a.txt" in run(repo, "show", "--stat", "--oneline", "HEAD")
    assert "b.txt" in run(repo, "status", "--short")


@pytest.mark.asyncio
async def test_rejects_bad_inputs_and_non_repo(tmp_path):
    reg = registry(tmp_path)
    status = await reg.execute("git_status", {})
    assert status.is_error and "not a git repository" in status.content


@pytest.mark.asyncio
async def test_refuses_option_like_revisions(repo):
    reg = registry(repo)
    bad = await reg.execute("git_show", {"rev": "--upload-pack=evil"})
    assert bad.is_error
    bad_path = await reg.execute("git_diff", {"path": "--no-index"})
    assert bad_path.is_error


@pytest.mark.asyncio
async def test_commit_is_marked_mutating_and_reads_are_read_only(tmp_path):
    reg = registry(tmp_path)
    assert reg.tools["git_commit"].read_only is False
    for name in ("git_status", "git_diff", "git_log", "git_show", "git_branches"):
        assert reg.tools[name].read_only is True
