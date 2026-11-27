"""Git worktree isolation for subagents.

A subagent that edits code can corrupt the parent's working tree mid-task. Codex and Claude Code both
isolate such work (separate checkout / worktree). Argo creates a throwaway branch plus a linked worktree,
points the child agent at it, and offers adoption to the parent (review + merge via `adopt_worktree`), which is
opt-in per delegation.

Worktrees live under ``<data_dir>/worktrees/<name>`` so they never pollute the user's repository listing.
"""
from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


async def _git(cwd: Path, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(cwd), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {err.decode(errors='replace').strip()[:400]}")
    return out.decode(errors="replace").strip()


async def git_root(path: Path) -> Path | None:
    try:
        return Path(await _git(path, "rev-parse", "--show-toplevel")).resolve()
    except Exception:
        return None


async def create_worktree(repo: Path, label: str, data_dir: Path) -> tuple[Path, str, Path]:
    """Create ``argo/<label>-<id>`` at HEAD and a worktree for it. Returns (worktree, branch, repo_root)."""
    root = await git_root(repo)
    if root is None:
        raise RuntimeError(f"{repo} is not inside a git repository; isolate requires git")
    await _git(root, "rev-parse", "HEAD")  # fail early on an empty repository
    slug = _SAFE.sub("-", label.strip().lower())[:40].strip("-") or "task"
    suffix = uuid.uuid4().hex[:6]
    branch = f"argo/{slug}-{suffix}"
    wt = Path(data_dir) / "worktrees" / f"{slug}-{suffix}"
    wt.parent.mkdir(parents=True, exist_ok=True)
    await _git(root, "worktree", "add", "-b", branch, str(wt), "HEAD")
    return wt, branch, root


async def remove_worktree(repo_root: Path, worktree: Path, branch: str | None = None) -> None:
    try:
        await _git(repo_root, "worktree", "remove", "--force", str(worktree))
    except Exception:
        pass
    if branch:
        try:
            await _git(repo_root, "branch", "-D", branch)
        except Exception:
            pass


async def _commit_all(worktree: Path, message: str) -> bool:
    """Commit any pending work in the worktree so the branch actually carries it."""
    status = await _git(worktree, "status", "--porcelain")
    if not status.strip():
        return False
    await _git(worktree, "add", "-A")
    await _git(worktree, "commit", "-m", message)
    return True


async def adopt_worktree(worktree: Path, branch: str, repo_root: Path,
                         message: str | None = None) -> dict:
    """Merge a subagent branch back into the parent checkout.

    The child usually leaves its edits uncommitted, so they are committed first; the merge is a normal
    ``--no-ff`` merge on the parent. A conflict aborts the merge and raises, leaving the parent exactly as it was
    (the branch and worktree survive, so nothing is lost). Adoption is refused if the parent is not on a branch
    or has staged changes that the merge would mix in.
    """
    try:
        await _git(repo_root, "symbolic-ref", "--short", "HEAD")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("cannot adopt into a detached HEAD; check out a branch first") from exc
    staged = await _git(repo_root, "diff", "--cached", "--name-only")
    if staged.strip():
        raise RuntimeError("parent has staged changes; commit or unstage them before adopting a subagent branch")
    committed = await _commit_all(worktree, message or f"argo: subagent work ({branch})")
    before = await _git(repo_root, "rev-parse", "HEAD")
    try:
        await _git(repo_root, "merge", "--no-ff", "--no-edit", branch)
    except Exception as exc:  # noqa: BLE001
        try:
            await _git(repo_root, "merge", "--abort")
        except Exception:
            pass
        raise RuntimeError(f"merge of {branch} failed and was aborted; parent unchanged: {exc}") from exc
    after = await _git(repo_root, "rev-parse", "HEAD")
    return {"adopted": True, "branch": branch, "committed_child_work": committed,
            "parent_before": before[:12], "parent_after": after[:12]}


async def changed_files(worktree: Path, repo_root: Path, branch: str) -> list[str]:
    """Files the subagent touched: uncommitted worktree edits plus anything committed on the branch."""
    files: set[str] = set()
    for args in (("diff", "--name-only", "HEAD"), ("ls-files", "--others", "--exclude-standard")):
        try:
            out = await _git(worktree, *args)
            files.update(line.strip() for line in out.splitlines() if line.strip())
        except Exception:
            pass
    try:
        out = await _git(repo_root, "diff", "--name-only", f"HEAD...{branch}")
        files.update(line.strip() for line in out.splitlines() if line.strip())
    except Exception:
        pass
    return sorted(files)
