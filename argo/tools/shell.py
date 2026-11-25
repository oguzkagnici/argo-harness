"""Code execution tools: run_code (python/node/bash) and bash, with timeouts and output limits."""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from ..core.types import ToolResult
from .base import ToolContext, ToolRegistry, truncate_output

LANG_CMD = {
    "python": ([sys.executable or "python3"], ".py"),
    "python3": ([sys.executable or "python3"], ".py"),
    "node": (["node"], ".mjs"),
    "javascript": (["node"], ".mjs"),
    "bash": (["bash"], ".sh"),
    "sh": (["bash"], ".sh"),
}


async def _exec(cmd: list[str], cwd: Path, timeout: float, env: dict | None = None,
                stdin: str | None = None) -> tuple[int, str, str]:
    full_env = {**os.environ, "PYTHONUNBUFFERED": "1", "NO_COLOR": "1", **(env or {})}
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE if stdin else None, env=full_env,
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin.encode() if stdin else None), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except ProcessLookupError:
            pass
        await proc.wait()
        raise
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _format(exit_code: int, out: str, err: str, duration: float) -> str:
    parts = [f"exit_code: {exit_code}  duration: {duration:.2f}s"]
    if out:
        parts.append(f"--- stdout ---\n{out.rstrip()}")
    if err:
        parts.append(f"--- stderr ---\n{err.rstrip()}")
    if not out and not err:
        parts.append("(no output)")
    return "\n".join(parts)


async def _run_code(ctx: ToolContext, code: str, language: str = "python", timeout: int = 120,
                    cwd: str | None = None, env: dict | None = None) -> ToolResult:
    lang = language.lower()
    if lang not in LANG_CMD:
        return ToolResult(f"Unsupported language {language}. Use one of {sorted(LANG_CMD)}.", is_error=True)
    cmd, ext = LANG_CMD[lang]
    workdir = ctx.resolve(cwd or ".")
    workdir.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(suffix=ext, dir=str(workdir), prefix="_argo_run_")
    os.close(fd)
    script = Path(path)
    script.write_text(code, encoding="utf-8")
    started = time.monotonic()
    try:
        exit_code, out, err = await _exec(cmd + [str(script)], workdir, min(timeout, ctx.cfg.policy.max_shell_timeout), env)
    except asyncio.TimeoutError:
        return ToolResult(f"Execution timed out after {timeout}s", is_error=True)
    finally:
        try:
            script.unlink()
        except OSError:
            pass
    text = _format(exit_code, out, err, time.monotonic() - started)
    text, meta = truncate_output(ctx, text, 30_000, label="run_code")
    return ToolResult(text, is_error=exit_code != 0, metadata=meta or {})


async def _bash(ctx: ToolContext, command: str, timeout: int = 120, cwd: str | None = None,
                env: dict | None = None) -> ToolResult:
    workdir = ctx.resolve(cwd or ".")
    started = time.monotonic()
    try:
        exit_code, out, err = await _exec(["bash", "-lc", command], workdir,
                                          min(timeout, ctx.cfg.policy.max_shell_timeout), env)
    except asyncio.TimeoutError:
        return ToolResult(f"Command timed out after {timeout}s: {command[:200]}", is_error=True)
    text = _format(exit_code, out, err, time.monotonic() - started)
    text, meta = truncate_output(ctx, text, 30_000, label="bash")
    return ToolResult(text, is_error=exit_code != 0, metadata=meta or {})


def register(reg: ToolRegistry) -> None:
    def params(props: dict, required: list[str] | None = None) -> dict:
        return {"type": "object", "properties": props, "required": required or []}

    reg.add(
        "run_code",
        "Write a code snippet to a temp file in the workspace, execute it, and return stdout/stderr/exit code. "
        "Use for calculations, data analysis, generating files, and testing. State persists only via files.",
        params({"code": {"type": "string"}, "language": {"type": "string", "enum": sorted(LANG_CMD), "default": "python"},
                "timeout": {"type": "integer", "default": 120}, "cwd": {"type": "string"},
                "env": {"type": "object", "additionalProperties": {"type": "string"}}}, ["code"]),
        _run_code, read_only=False, timeout=320,
    )
    reg.add(
        "bash",
        "Run a shell command in the workspace. Prefer run_code for scripts. Returns stdout/stderr/exit code.",
        params({"command": {"type": "string"}, "timeout": {"type": "integer", "default": 120},
                "cwd": {"type": "string"}, "env": {"type": "object", "additionalProperties": {"type": "string"}}}, ["command"]),
        _bash, read_only=False, timeout=320,
    )
    reg.add(
        "which",
        "Check whether an executable exists on PATH (e.g. python, node, libreoffice, ffmpeg).",
        params({"name": {"type": "string"}}, ["name"]),
        lambda ctx, name: ToolResult(f"{name}: {shutil.which(name) or 'not found'}"),
        read_only=True,
    )
