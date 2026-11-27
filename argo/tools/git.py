"""Structured git tools.

The shell tool can already run ``git``; these tools exist so the agent has a
typed, inspectable surface for the operations a coding workflow actually needs
(status, diff, log, show, branches) and an *approval-gated* commit instead of
building one out of raw shell strings. Deliberately absent: anything that talks
to a remote (``push``/``pull``/``fetch``) and history rewriting (``rebase``,
``reset --hard``, ``commit --amend``).
"""
from __future__ import annotations

import asyncio
import shlex
from pathlib import Path

from ..core.types import ToolResult
from .base import ToolContext, ToolRegistry


async def _git(root: Path, *args: str, timeout: float = 30.0) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(root), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, f"git {' '.join(args)} timed out"
    text = (out.decode(errors="replace") + err.decode(errors="replace")).strip()
    return proc.returncode or 0, text


async def _repo(ctx: ToolContext) -> Path | None:
    code, out = await _git(ctx.workspace, "rev-parse", "--show-toplevel")
    return Path(out).resolve() if code == 0 and out else None


def _safe_ref(value: str) -> bool:
    return bool(value) and not value.startswith("-") and "\n" not in value


async def _status(ctx: ToolContext, short: bool = True) -> ToolResult:
    root = await _repo(ctx)
    if root is None:
        return ToolResult(f"{ctx.workspace} is not a git repository.", is_error=True)
    code, out = await _git(root, "status", *(("--short", "--branch") if short else ()))
    if code != 0:
        return ToolResult(out or "git status failed", is_error=True)
    if not out:
        return ToolResult("Working tree clean.")
    lines = out.splitlines()
    changed = sum(1 for line in lines[1:] if line.strip())
    return ToolResult(f"{out}\n\n({changed} changed path(s); repo root {root})",
                      metadata={"repo": str(root), "changed": changed})


async def _diff(ctx: ToolContext, path: str = "", staged: bool = False, stat: bool = False) -> ToolResult:
    root = await _repo(ctx)
    if root is None:
        return ToolResult(f"{ctx.workspace} is not a git repository.", is_error=True)
    args = ["diff"]
    if staged:
        args.append("--cached")
    if stat:
        args.append("--stat")
    if path:
        if not _safe_ref(path):
            return ToolResult("invalid path", is_error=True)
        args += ["--", path]
    code, out = await _git(root, *args)
    if code != 0:
        return ToolResult(out or "git diff failed", is_error=True)
    if not out:
        return ToolResult("No changes." if not staged else "No staged changes.")
    return ToolResult(out, metadata={"chars": len(out)})


async def _log(ctx: ToolContext, limit: int = 10, path: str = "") -> ToolResult:
    root = await _repo(ctx)
    if root is None:
        return ToolResult(f"{ctx.workspace} is not a git repository.", is_error=True)
    args = ["log", f"--max-count={max(1, min(int(limit), 200))}", "--date=short",
            "--pretty=%h %ad %s"]
    if path:
        if not _safe_ref(path):
            return ToolResult("invalid path", is_error=True)
        args += ["--", path]
    code, out = await _git(root, *args)
    if code != 0:
        return ToolResult(out or "git log failed (no commits yet?)", is_error=True)
    return ToolResult(out)


async def _show(ctx: ToolContext, rev: str = "HEAD", stat: bool = False) -> ToolResult:
    root = await _repo(ctx)
    if root is None:
        return ToolResult(f"{ctx.workspace} is not a git repository.", is_error=True)
    if not _safe_ref(rev):
        return ToolResult("invalid revision", is_error=True)
    args = ["show", rev] + (["--stat", "--oneline"] if stat else ["--no-patch", "--stat", "--oneline"])
    code, out = await _git(root, *args)
    if code != 0:
        return ToolResult(out or f"cannot show {rev}", is_error=True)
    return ToolResult(out, metadata={"rev": rev})


async def _branches(ctx: ToolContext) -> ToolResult:
    root = await _repo(ctx)
    if root is None:
        return ToolResult(f"{ctx.workspace} is not a git repository.", is_error=True)
    code, out = await _git(root, "branch", "--all", "--no-color")
    if code != 0:
        return ToolResult(out or "git branch failed", is_error=True)
    code2, head = await _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    return ToolResult(f"HEAD: {head if code2 == 0 else '?'}\n{out}")


async def _commit(ctx: ToolContext, message: str, add_all: bool = False, paths: str = "") -> ToolResult:
    root = await _repo(ctx)
    if root is None:
        return ToolResult(f"{ctx.workspace} is not a git repository.", is_error=True)
    message = (message or "").strip()
    if not message:
        return ToolResult("commit message is required", is_error=True)
    path_list = shlex.split(paths) if paths else []
    for p in path_list:
        if not _safe_ref(p):
            return ToolResult(f"invalid path: {p}", is_error=True)
    if add_all:
        code, out = await _git(root, "add", "-A")
        if code != 0:
            return ToolResult(f"git add failed:\n{out}", is_error=True)
    elif path_list:
        code, out = await _git(root, "add", "--", *path_list)
        if code != 0:
            return ToolResult(f"git add failed:\n{out}", is_error=True)
    code, staged = await _git(root, "diff", "--cached", "--name-only")
    if code != 0 or not staged.strip():
        return ToolResult("Nothing staged to commit.", is_error=True)
    code, out = await _git(root, "commit", "-m", message)
    if code != 0:
        return ToolResult(f"git commit failed:\n{out}", is_error=True)
    code2, sha = await _git(root, "rev-parse", "--short", "HEAD")
    code3, dirty = await _git(root, "status", "--short")
    summary = f"{out}\n\ncommit {sha if code2 == 0 else '?'}"
    if code3 == 0 and dirty.strip():
        summary += f"\n\nremaining uncommitted paths:\n{dirty}"
    ctx.emit("git_commit", sha=sha, message=message)
    return ToolResult(summary, metadata={"sha": sha, "staged": staged.splitlines()})


def register(reg: ToolRegistry) -> None:
    def params(props: dict, required: list[str] | None = None) -> dict:
        return {"type": "object", "properties": props, "required": required or []}

    reg.add("git_status", "Show git status (short + branch) and the repository root.",
            params({"short": {"type": "boolean", "default": True}}), _status,
            read_only=True, category="git", max_output=20_000)
    reg.add("git_diff", "Show the working-tree or staged diff. Optionally limit to a path or show --stat.",
            params({"path": {"type": "string", "default": ""},
                    "staged": {"type": "boolean", "default": False},
                    "stat": {"type": "boolean", "default": False}}), _diff,
            read_only=True, category="git", max_output=40_000)
    reg.add("git_log", "Recent commits as 'sha date subject' (most recent first).",
            params({"limit": {"type": "integer", "default": 10}, "path": {"type": "string", "default": ""}}),
            _log, read_only=True, category="git")
    reg.add("git_show", "Show one commit (default HEAD) with file stats.",
            params({"rev": {"type": "string", "default": "HEAD"},
                    "stat": {"type": "boolean", "default": True}}), _show,
            read_only=True, category="git", max_output=30_000)
    reg.add("git_branches", "List local and remote branches plus the current HEAD.",
            params({}), _branches, read_only=True, category="git")
    reg.add("git_commit", "Stage (optionally) and commit. Requires a message; no amend, no remote operations.",
            params({"message": {"type": "string"}, "add_all": {"type": "boolean", "default": False},
                    "paths": {"type": "string", "default": "",
                              "description": "Space-separated paths to stage (ignored when add_all=true)."}},
                   ["message"]),
            _commit, read_only=False, category="git", max_output=20_000)
