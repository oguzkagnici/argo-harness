import subprocess
from pathlib import Path

import pytest

from argo.config import load_config
from argo.core.agent import Agent
from argo.core.types import LLMResponse, Message, ToolCall, Usage
from argo.core.worktree import changed_files, create_worktree, git_root, remove_worktree


def init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@example.com"),
                 ("config", "user.name", "Test"),):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "app.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True, capture_output=True)
    return repo


@pytest.mark.asyncio
async def test_worktree_isolates_edits_and_reports_changes(tmp_path):
    repo = init_repo(tmp_path)
    wt, branch, root = await create_worktree(repo, "Fix Bug", tmp_path / "data")
    try:
        assert (wt / "app.txt").read_text() == "one\n"
        assert branch.startswith("argo/fix-bug-")
        assert root == repo.resolve()
        (wt / "app.txt").write_text("two\n", encoding="utf-8")
        (wt / "new.txt").write_text("x\n", encoding="utf-8")
        assert (repo / "app.txt").read_text() == "one\n"
        assert not (repo / "new.txt").exists()
        assert await changed_files(wt, root, branch) == ["app.txt", "new.txt"]
    finally:
        await remove_worktree(root, wt, branch)
    assert not wt.exists()
    assert not subprocess.run(["git", "-C", str(repo), "branch", "--list", branch],
                              capture_output=True, text=True).stdout.strip()


@pytest.mark.asyncio
async def test_worktree_requires_git(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert await git_root(plain) is None
    with pytest.raises(RuntimeError, match="not inside a git repository"):
        await create_worktree(plain, "x", tmp_path / "data")


class ScriptedLLM:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def chat(self, messages, tools=None, **kwargs):
        child = any(m.role == "user" and "ISOLATED_CHILD" in (m.content or "") for m in messages)
        if child:
            return LLMResponse(message=Message(role="assistant", content="child finished"),
                               usage=Usage())
        if any(m.role == "tool" for m in messages):
            return LLMResponse(message=Message(role="assistant", content="parent done"), usage=Usage())
        return LLMResponse(message=Message(role="assistant", tool_calls=[ToolCall(
            id="c1", name="spawn_subagent",
            arguments='{"task": "ISOLATED_CHILD: attempt the fix", "isolate": true}')]), usage=Usage())

    async def embed(self, texts):
        return [[0.0] * 8 for _ in texts]

    async def rerank(self, query, docs, top_n=None):
        return [(float(len(docs) - i), i) for i in range(len(docs))][: top_n or len(docs)]


@pytest.mark.asyncio
async def test_agent_spawns_subagent_in_worktree(tmp_path):
    repo = init_repo(tmp_path)
    data = tmp_path / "data"
    cfg = load_config({"workspace": str(repo), "data_dir": str(data)})
    async with Agent(cfg, llm=ScriptedLLM(), max_iterations=4) as agent:
        result = await agent.run("delegate a child task")
    assert result.stop_reason == "final"
    tool_msgs = [m for m in agent.messages if m.role == "tool"]
    assert tool_msgs and "WORKTREE ISOLATED" in tool_msgs[0].content
    assert "child finished" in tool_msgs[0].content
    assert "Changed files: none" in tool_msgs[0].content
    worktrees = list((data / "worktrees").iterdir())
    assert len(worktrees) == 1 and (worktrees[0] / "app.txt").read_text() == "one\n"
    assert (repo / "app.txt").read_text() == "one\n"
