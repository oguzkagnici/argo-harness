import json
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


class DiffReviewLLM:
    """Child edits a file; the diff reviewer returns FAIL for a deletion, PASS otherwise."""

    def __init__(self):
        self.reviews: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def chat(self, messages, tools=None, **kwargs):
        joined = "\n".join((m.content or "") for m in messages)
        if "independent code reviewer" in joined:
            self.reviews.append(joined)
            bad = "-one" in joined and "+deleted" in joined
            verdict = "VERDICT: FAIL\napp.txt:1 weakens the test" if bad else "VERDICT: PASS\nlooks scoped"
            return LLMResponse(message=Message(role="assistant", content=verdict), usage=Usage())
        if "ISOLATED_CHILD" in joined:
            edit = ToolCall(id="c2", name="edit_file",
                            arguments='{"path": "app.txt", "old_text": "one", "new_text": "deleted"}')
            if any(m.role == "tool" for m in messages):
                return LLMResponse(message=Message(role="assistant", content="child finished"), usage=Usage())
            return LLMResponse(message=Message(role="assistant", tool_calls=[edit]), usage=Usage())
        if any(m.role == "tool" for m in messages):
            return LLMResponse(message=Message(role="assistant", content="parent done"), usage=Usage())
        return LLMResponse(message=Message(role="assistant", tool_calls=[ToolCall(
            id="c1", name="spawn_subagent",
            arguments='{"task": "ISOLATED_CHILD: change app.txt", "isolate": true, "review_changes": true}')]),
            usage=Usage())

    async def embed(self, texts):
        return [[0.0] * 8 for _ in texts]

    async def rerank(self, query, docs, top_n=None):
        return [(float(len(docs) - i), i) for i in range(len(docs))][: top_n or len(docs)]


@pytest.mark.asyncio
async def test_isolated_spawn_reviews_diff_before_adoption(tmp_path):
    repo = init_repo(tmp_path)
    data = tmp_path / "data"
    cfg = load_config({"workspace": str(repo), "data_dir": str(data)})
    llm = DiffReviewLLM()
    async with Agent(cfg, llm=llm, max_iterations=5) as agent:
        await agent.run("delegate a child task")

    tool_msg = next(m for m in agent.messages if m.role == "tool" and "WORKTREE ISOLATED" in (m.content or ""))
    assert "INDEPENDENT DIFF REVIEW: FAIL" in tool_msg.content
    assert "Adoption is recommended after the fixes above." in tool_msg.content
    assert llm.reviews and "diff --git" in llm.reviews[0].replace("diff\n```", "diff")
    assert (repo / "app.txt").read_text() == "one\n"  # parent untouched until adoption


class CommitThenReviewLLM(DiffReviewLLM):
    """Child commits its edit (as a real subagent often does); the reviewer must still see the diff."""

    async def chat(self, messages, tools=None, **kwargs):
        joined = "\n".join((m.content or "") for m in messages)
        if "ISOLATED_CHILD" in joined and not any(m.role == "tool" for m in messages):
            cmd = ("printf 'two\\n' > app.txt && git add app.txt && "
                   "git -c user.email=t@e.com -c user.name=T commit -qm child-commit")
            return LLMResponse(message=Message(role="assistant", tool_calls=[
                ToolCall(id="c2", name="bash", arguments=json.dumps({"command": cmd}))]),
                usage=Usage())
        return await super().chat(messages, tools, **kwargs)


@pytest.mark.asyncio
async def test_diff_review_sees_committed_child_changes(tmp_path):
    repo = init_repo(tmp_path)
    cfg = load_config({"workspace": str(repo), "data_dir": str(tmp_path / "data")})
    llm = CommitThenReviewLLM()
    async with Agent(cfg, llm=llm, max_iterations=5) as agent:
        await agent.run("delegate a child task")

    tool_msg = next(m for m in agent.messages if m.role == "tool" and "WORKTREE ISOLATED" in (m.content or ""))
    assert "INDEPENDENT DIFF REVIEW" in tool_msg.content
    assert llm.reviews, "reviewer was not invoked"
    diff_block = llm.reviews[0]
    assert "-one" in diff_block and "+two" in diff_block


class RepairLoopLLM:
    """First review FAILs, the repair round fixes the file, the second review PASSes."""

    def __init__(self):
        self.reviews = 0
        self.repair_prompts = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def chat(self, messages, tools=None, **kwargs):
        joined = "\n".join((m.content or "") for m in messages)
        if "independent code reviewer" in joined:
            self.reviews += 1
            bad = "+deleted" in joined
            return LLMResponse(message=Message(
                role="assistant", content=("VERDICT: FAIL\napp.txt:1 value is wrong" if bad
                                           else "VERDICT: PASS\nscoped and correct")), usage=Usage())
        if "Fix them now in place" in joined:
            if messages[-1].role == "tool":
                return LLMResponse(message=Message(role="assistant", content="repair done"), usage=Usage())
            self.repair_prompts += 1
            return LLMResponse(message=Message(role="assistant", tool_calls=[ToolCall(
                id="c9", name="edit_file",
                arguments='{"path": "app.txt", "old_text": "deleted", "new_text": "two"}')]), usage=Usage())
        if "ISOLATED_CHILD" in joined:
            if any(m.role == "tool" for m in messages):
                return LLMResponse(message=Message(role="assistant", content="child finished"), usage=Usage())
            return LLMResponse(message=Message(role="assistant", tool_calls=[ToolCall(
                id="c2", name="edit_file",
                arguments='{"path": "app.txt", "old_text": "one", "new_text": "deleted"}')]), usage=Usage())
        if any(m.role == "tool" for m in messages):
            return LLMResponse(message=Message(role="assistant", content="parent done"), usage=Usage())
        return LLMResponse(message=Message(role="assistant", tool_calls=[ToolCall(
            id="c1", name="spawn_subagent",
            arguments='{"task": "ISOLATED_CHILD: change app.txt", "isolate": true, '
                      '"review_changes": true, "repair_attempts": 1}')]), usage=Usage())

    async def embed(self, texts):
        return [[0.0] * 8 for _ in texts]

    async def rerank(self, query, docs, top_n=None):
        return [(float(len(docs) - i), i) for i in range(len(docs))][: top_n or len(docs)]


@pytest.mark.asyncio
async def test_review_failure_triggers_in_place_repair_and_recheck(tmp_path):
    repo = init_repo(tmp_path)
    cfg = load_config({"workspace": str(repo), "data_dir": str(tmp_path / "data")})
    llm = RepairLoopLLM()
    async with Agent(cfg, llm=llm, max_iterations=6) as agent:
        await agent.run("delegate a child task")

    tool_msg = next(m for m in agent.messages if m.role == "tool" and "WORKTREE ISOLATED" in (m.content or ""))
    assert "after 1 repair round(s)" in tool_msg.content
    assert "INDEPENDENT DIFF REVIEW: PASS" in tool_msg.content
    assert llm.reviews == 2 and llm.repair_prompts == 1
    assert (repo / "app.txt").read_text() == "one\n"  # still isolated from the parent


@pytest.mark.asyncio
async def test_adopt_commits_child_work_and_merges(tmp_path):
    from argo.core.worktree import adopt_worktree

    repo = init_repo(tmp_path)
    wt, branch, root = await create_worktree(repo, "Adopt Me", tmp_path / "data")
    try:
        (wt / "app.txt").write_text("two\n", encoding="utf-8")
        (wt / "added.txt").write_text("new\n", encoding="utf-8")
        assert (repo / "app.txt").read_text() == "one\n"
        info = await adopt_worktree(wt, branch, root)
        assert info["adopted"] and info["committed_child_work"] is True
        assert (repo / "app.txt").read_text() == "two\n"
        assert (repo / "added.txt").read_text() == "new\n"
        log = subprocess.run(["git", "-C", str(repo), "log", "--oneline"], capture_output=True, text=True).stdout
        assert branch in log, "merge commit should name the adopted branch"
        # adopting twice is a no-op for work, and still reports a merge attempt (no new commits child-side)
        info2 = await adopt_worktree(wt, branch, root)
        assert info2["committed_child_work"] is False
    finally:
        await remove_worktree(root, wt, branch)


@pytest.mark.asyncio
async def test_adopt_aborts_cleanly_on_conflict(tmp_path):
    from argo.core.worktree import adopt_worktree

    repo = init_repo(tmp_path)
    wt, branch, root = await create_worktree(repo, "Conflict", tmp_path / "data")
    try:
        (wt / "app.txt").write_text("child\n", encoding="utf-8")
        (repo / "app.txt").write_text("parent\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "commit", "-qam", "parent edit"], check=True)
        with pytest.raises(RuntimeError) as exc:
            await adopt_worktree(wt, branch, root)
        assert "aborted" in str(exc.value)
        assert (repo / "app.txt").read_text() == "parent\n"
        status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                                capture_output=True, text=True).stdout
        assert status.strip() == "", "merge must be fully aborted, not left half-applied"
    finally:
        await remove_worktree(root, wt, branch)


@pytest.mark.asyncio
async def test_adopt_refuses_staged_parent_changes(tmp_path):
    from argo.core.worktree import adopt_worktree

    repo = init_repo(tmp_path)
    wt, branch, root = await create_worktree(repo, "Staged", tmp_path / "data")
    try:
        (wt / "app.txt").write_text("child\n", encoding="utf-8")
        (repo / "other.txt").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "other.txt"], check=True)
        with pytest.raises(RuntimeError) as exc:
            await adopt_worktree(wt, branch, root)
        assert "staged" in str(exc.value)
        assert (repo / "app.txt").read_text() == "one\n"
    finally:
        await remove_worktree(root, wt, branch)


class AdoptLLM(DiffReviewLLM):
    """Parent asks for isolate + review + adopt; child makes a clean edit; reviewer passes."""

    async def chat(self, messages, tools=None, **kwargs):
        joined = "\n".join((m.content or "") for m in messages)
        if "independent code reviewer" in joined:
            return LLMResponse(message=Message(role="assistant", content="VERDICT: PASS\nfine"), usage=Usage())
        if "ISOLATED_CHILD" in joined:
            if any(m.role == "tool" for m in messages):
                return LLMResponse(message=Message(role="assistant", content="child finished"), usage=Usage())
            edit = ToolCall(id="c2", name="edit_file",
                            arguments='{"path": "app.txt", "old_text": "one", "new_text": "three"}')
            return LLMResponse(message=Message(role="assistant", tool_calls=[edit]), usage=Usage())
        if any(m.role == "tool" for m in messages):
            return LLMResponse(message=Message(role="assistant", content="parent done"), usage=Usage())
        return LLMResponse(message=Message(role="assistant", tool_calls=[ToolCall(
            id="c1", name="spawn_subagent",
            arguments='{"task": "ISOLATED_CHILD: change app.txt", "isolate": true, "review_changes": true, '
                      '"adopt": true}')]), usage=Usage())


@pytest.mark.asyncio
async def test_spawn_adopts_branch_into_parent_checkout(tmp_path):
    repo = init_repo(tmp_path)
    data = tmp_path / "data"
    cfg = load_config({"workspace": str(repo), "data_dir": str(data)})
    async with Agent(cfg, llm=AdoptLLM(), max_iterations=4) as agent:
        await agent.run("delegate and adopt")
    assert (repo / "app.txt").read_text() == "three\n", "adopted child edit must be live in the parent"
    tool_msgs = [m for m in agent.messages if m.role == "tool"]
    assert any("ADOPTED" in m.content for m in tool_msgs if m.role == "tool")
    assert any(e.type == "worktree_adopted" for e in agent.events.history)


class AdoptFailingReviewLLM(AdoptLLM):
    """Parent asks for adopt, but the reviewer keeps returning FAIL."""

    async def chat(self, messages, tools=None, **kwargs):
        joined = "\n".join((m.content or "") for m in messages)
        if "independent code reviewer" in joined:
            return LLMResponse(message=Message(role="assistant",
                                               content="VERDICT: FAIL\napp.txt:1 weakens the test"),
                               usage=Usage())
        return await super().chat(messages, tools=tools, **kwargs)


@pytest.mark.asyncio
async def test_spawn_skips_adoption_when_review_fails(tmp_path):
    repo = init_repo(tmp_path)
    data = tmp_path / "data"
    cfg = load_config({"workspace": str(repo), "data_dir": str(data)})
    async with Agent(cfg, llm=AdoptFailingReviewLLM(), max_iterations=4) as agent:
        await agent.run("delegate with review and adopt")
    assert (repo / "app.txt").read_text() == "one\n", "a failing review must not reach the parent checkout"
    msgs = [m.content for m in agent.messages if m.role == "tool"]
    assert any("ADOPTION SKIPPED" in m for m in msgs)
    assert any("INDEPENDENT DIFF REVIEW: FAIL" in m for m in msgs)


class PromptRecorderLLM:
    def __init__(self):
        self.prompts: list[str] = []

    async def chat(self, messages, tools=None, **kwargs):
        joined = "\n".join((m.content or "") for m in messages)
        self.prompts.append(joined)
        if "ISOLATED_CHILD" in joined:
            return LLMResponse(message=Message(role="assistant", content="child finished"), usage=Usage())
        return LLMResponse(message=Message(role="assistant", tool_calls=[ToolCall(
            id="c1", name="spawn_subagent",
            arguments='{"task": "ISOLATED_CHILD: do the thing", "isolate": true}')]), usage=Usage())

    async def embed(self, texts):
        return [[0.0] * 8 for _ in texts]


@pytest.mark.asyncio
async def test_isolated_child_is_told_where_it_is(tmp_path):
    repo = init_repo(tmp_path)
    cfg = load_config({"workspace": str(repo), "data_dir": str(tmp_path / "data")})
    llm = PromptRecorderLLM()
    async with Agent(cfg, llm=llm, max_iterations=3) as agent:
        await agent.run("delegate")
    child_prompts = [p for p in llm.prompts if "ISOLATED_CHILD" in p]
    assert child_prompts, "child never ran"
    assert "isolated git worktree" in child_prompts[0]
    assert "use paths relative to it" in child_prompts[0]
