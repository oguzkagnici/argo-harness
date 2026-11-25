"""Filesystem tools: read, write, edit, list, glob, grep, patch."""
from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..core.types import ToolResult
from .base import ToolContext, ToolRegistry

MAX_READ_LINES = 4000


def _read_file(ctx: ToolContext, path: str, offset: int = 0, limit: int = 2000) -> ToolResult:
    p = ctx.resolve(path)
    if not p.exists():
        return ToolResult(f"File not found: {p}", is_error=True)
    if p.is_dir():
        return ToolResult(f"{p} is a directory; use list_dir", is_error=True)
    try:
        raw = p.read_bytes()
    except OSError as exc:
        return ToolResult(f"Cannot read {p}: {exc}", is_error=True)
    if b"\0" in raw[:4096]:
        return ToolResult(f"Binary file ({len(raw)} bytes): {p}. Use a code/doc tool instead.", is_error=True)
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    start = max(0, int(offset))
    end = min(len(lines), start + max(1, min(int(limit), MAX_READ_LINES)))
    numbered = "\n".join(f"{i + 1:>6}  {lines[i]}" for i in range(start, end))
    footer = ""
    if end < len(lines):
        footer = f"\n\n[showing lines {start + 1}-{end} of {len(lines)}; call read_file with offset={end}]"
    return ToolResult(f"# {p} ({len(lines)} lines, {len(raw)} bytes)\n{numbered}{footer}",
                      metadata={"path": str(p), "lines": len(lines)})


def _write_file(ctx: ToolContext, path: str, content: str, mode: str = "overwrite") -> ToolResult:
    p = ctx.resolve(path, write=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    if mode == "append":
        with p.open("a", encoding="utf-8") as fh:
            fh.write(content)
    else:
        p.write_text(content, encoding="utf-8")
    ctx.emit("file_written", path=str(p), chars=len(content))
    return ToolResult(f"Wrote {len(content)} chars to {p}", metadata={"path": str(p)})


def _edit_file(ctx: ToolContext, path: str, old_text: str, new_text: str, replace_all: bool = False) -> ToolResult:
    p = ctx.resolve(path, write=True)
    if not p.exists():
        return ToolResult(f"File not found: {p}", is_error=True)
    text = p.read_text(encoding="utf-8", errors="replace")
    count = text.count(old_text)
    if count == 0:
        return ToolResult("old_text not found. Read the file and match exactly, including whitespace.", is_error=True)
    if count > 1 and not replace_all:
        return ToolResult(f"old_text occurs {count} times; add context to make it unique or set replace_all=true.", is_error=True)
    updated = text.replace(old_text, new_text, -1 if replace_all else 1)
    p.write_text(updated, encoding="utf-8")
    ctx.emit("file_written", path=str(p), chars=len(updated))
    return ToolResult(f"Edited {p}: replaced {count if replace_all else 1} occurrence(s).", metadata={"path": str(p)})


def _list_dir(ctx: ToolContext, path: str = ".", depth: int = 1) -> ToolResult:
    root = ctx.resolve(path)
    if not root.exists():
        return ToolResult(f"Not found: {root}", is_error=True)
    if not root.is_dir():
        return _read_file(ctx, str(root))
    depth = max(0, min(int(depth), 6))
    lines: list[str] = []
    base_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        rel_depth = len(Path(dirpath).parts) - base_depth
        if rel_depth >= depth:
            dirnames[:] = []
        dirnames[:] = sorted(d for d in dirnames if d not in {".git", "node_modules", "__pycache__", ".venv", ".argo"})
        indent = "  " * rel_depth
        if rel_depth == 0:
            lines.append(f"{root}/")
        for d in dirnames:
            lines.append(f"{indent}{d}/")
        for f in sorted(filenames)[:200]:
            try:
                size = (Path(dirpath) / f).stat().st_size
            except OSError:
                size = 0
            lines.append(f"{indent}{f}  ({size:,}B)")
        if len(lines) > 2000:
            lines.append("... (truncated)")
            break
    return ToolResult("\n".join(lines))


def _glob_files(ctx: ToolContext, pattern: str, path: str = ".") -> ToolResult:
    root = ctx.resolve(path)
    matches = sorted(
        str(p.relative_to(root)) for p in root.rglob("*")
        if p.is_file() and fnmatch.fnmatch(str(p.relative_to(root)), pattern)
        and not any(part in {".git", "node_modules", "__pycache__", ".venv"} for part in p.parts)
    )
    if not matches:
        return ToolResult(f"No files matching {pattern!r} under {root}")
    body = "\n".join(matches[:500])
    return ToolResult(f"{len(matches)} match(es):\n{body}")


def _grep_files(ctx: ToolContext, pattern: str, path: str = ".", glob: str | None = None,
                max_results: int = 100, ignore_case: bool = False) -> ToolResult:
    root = ctx.resolve(path)
    flags = re.IGNORECASE if ignore_case else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        return ToolResult(f"Invalid regex: {exc}", is_error=True)
    hits: list[str] = []
    files = [root] if root.is_file() else root.rglob(glob or "*")
    scanned = 0
    for f in files:
        if not f.is_file() or any(part in {".git", "node_modules", "__pycache__", ".venv"} for part in f.parts):
            continue
        scanned += 1
        try:
            with f.open("r", encoding="utf-8", errors="ignore") as fh:
                for i, line in enumerate(fh, 1):
                    if regex.search(line):
                        hits.append(f"{f.relative_to(root) if root.is_dir() else f.name}:{i}: {line.rstrip()[:300]}")
                        if len(hits) >= max_results:
                            break
        except OSError:
            continue
        if len(hits) >= max_results:
            break
    if not hits:
        return ToolResult(f"No matches for {pattern!r} in {scanned} files.")
    return ToolResult(f"{len(hits)} match(es) across {scanned} files:\n" + "\n".join(hits))


def _apply_patch(ctx: ToolContext, patch: str, strip: int = 0) -> ToolResult:
    """Apply a unified diff. Tries git apply, then patch -pN."""
    root = ctx.workspace
    with tempfile.NamedTemporaryFile("w", suffix=".diff", delete=False, encoding="utf-8") as fh:
        fh.write(patch if patch.endswith("\n") else patch + "\n")
        diff_path = fh.name
    try:
        attempts = [
            ["git", "apply", "--whitespace=nowarn", f"-p{strip}", diff_path],
            ["patch", "--batch", "--forward", "--reject-file=-", f"-p{strip}", "-i", diff_path],
            ["patch", "--batch", "--forward", "--reject-file=-", "--dry-run", f"-p{strip}", "-i", diff_path],
        ]
        last = ""
        for cmd in attempts:
            try:
                proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=30)
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                last = str(exc)
                continue
            last = (proc.stdout or "") + (proc.stderr or "")
            if proc.returncode == 0:
                return ToolResult(f"Patch applied to {root}:\n{last.strip()[:1000]}", metadata={"path": str(root)})
        return ToolResult(f"Patch failed:\n{last[:2000]}", is_error=True)
    finally:
        Path(diff_path).unlink(missing_ok=True)


def _make_dir(ctx: ToolContext, path: str) -> ToolResult:
    p = ctx.resolve(path, write=True)
    p.mkdir(parents=True, exist_ok=True)
    return ToolResult(f"Created directory {p}")


def _delete_path(ctx: ToolContext, path: str, recursive: bool = False) -> ToolResult:
    p = ctx.resolve(path, write=True)
    if not p.exists():
        return ToolResult(f"Not found: {p}", is_error=True)
    if p.is_dir():
        if not recursive:
            return ToolResult("Refusing to delete a non-empty directory without recursive=true", is_error=True)
        shutil.rmtree(p)
    else:
        p.unlink()
    return ToolResult(f"Deleted {p}")


def register(reg: ToolRegistry) -> None:
    P = {"type": "object", "properties": {}, "required": []}

    def params(props: dict, required: list[str] | None = None) -> dict:
        return {"type": "object", "properties": props, "required": required or []}

    reg.add("read_file", "Read a text file with line numbers. Supports offset/limit for large files.",
            params({"path": {"type": "string"}, "offset": {"type": "integer", "default": 0},
                    "limit": {"type": "integer", "default": 2000}}, ["path"]),
            _read_file, read_only=True, max_output=40_000)
    reg.add("write_file", "Create or overwrite a text file (parents created automatically). Use mode=append to add.",
            params({"path": {"type": "string"}, "content": {"type": "string"},
                    "mode": {"type": "string", "enum": ["overwrite", "append"], "default": "overwrite"}}, ["path", "content"]),
            _write_file, read_only=False)
    reg.add("edit_file", "Exact string replacement in a file. old_text must be unique unless replace_all=true.",
            params({"path": {"type": "string"}, "old_text": {"type": "string"},
                    "new_text": {"type": "string"}, "replace_all": {"type": "boolean", "default": False}},
                   ["path", "old_text", "new_text"]),
            _edit_file, read_only=False)
    reg.add("list_dir", "List directory contents as a tree (depth 1 by default).",
            params({"path": {"type": "string", "default": "."}, "depth": {"type": "integer", "default": 1}}),
            _list_dir, read_only=True, max_output=30_000)
    reg.add("glob_files", "Find files by glob pattern, e.g. '**/*.py'.",
            params({"pattern": {"type": "string"}, "path": {"type": "string", "default": "."}}, ["pattern"]),
            _glob_files, read_only=True)
    reg.add("grep_files", "Regex search file contents. Returns path:line: text.",
            params({"pattern": {"type": "string"}, "path": {"type": "string", "default": "."},
                    "glob": {"type": "string"}, "max_results": {"type": "integer", "default": 100},
                    "ignore_case": {"type": "boolean", "default": False}}, ["pattern"]),
            _grep_files, read_only=True)
    reg.add("apply_patch", "Apply a unified diff (git/patch format) inside the workspace.",
            params({"patch": {"type": "string"}, "strip": {"type": "integer", "default": 0}}, ["patch"]),
            _apply_patch, read_only=False)
    reg.add("make_dir", "Create a directory (and parents).",
            params({"path": {"type": "string"}}, ["path"]), _make_dir, read_only=False)
    reg.add("delete_path", "Delete a file or directory (recursive=true required for directories).",
            params({"path": {"type": "string"}, "recursive": {"type": "boolean", "default": False}}, ["path"]),
            _delete_path, read_only=False)
