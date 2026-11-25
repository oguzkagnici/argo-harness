"""Tool definition, registry, policy enforcement, truncation and spooling."""
from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..core.events import EventBus
from ..core.types import ToolResult

Handler = Callable[..., Any]


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    read_only: bool = False
    parallel_safe: bool = True
    timeout: float = 120.0
    max_output: int = 24_000
    category: str = "general"

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }}


@dataclass
class ToolContext:
    cfg: Any
    memory: Any = None
    session: Any = None
    events: EventBus = field(default_factory=EventBus)
    approvals: Any = None          # callable(tool, args, reason) -> bool | awaitable
    depth: int = 0                 # subagent nesting depth
    extra: dict[str, Any] = field(default_factory=dict)

    def emit(self, type: str, **data: Any) -> None:
        self.events.emit(type, **data)

    @property
    def workspace(self) -> Path:
        return Path(self.cfg.workspace)

    def resolve(self, path: str, write: bool = False) -> Path:
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self.workspace / p
        try:
            resolved = p.resolve()
        except OSError:
            resolved = p
        if write and not _within(resolved, self.workspace) and self.cfg.policy.write_outside_workspace != "auto":
            raise PermissionError(f"refusing to write outside workspace: {resolved}")
        return resolved

    def spool(self, name: str, content: str) -> Path:
        d = Path(self.cfg.spool_dir)
        d.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:60]
        p = d / f"{int(time.time())}_{uuid.uuid4().hex[:6]}_{safe}"
        p.write_text(content, encoding="utf-8", errors="replace")
        return p


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def truncate_output(ctx: ToolContext, content: str, limit: int, label: str = "output") -> tuple[str, dict[str, Any] | None]:
    if len(content) <= limit:
        return content, None
    head = content[: int(limit * 0.6)]
    tail = content[-int(limit * 0.3):]
    path = ctx.spool(label, content)
    notice = (f"\n\n[... {len(content) - len(head) - len(tail):,} chars truncated. "
              f"Full {label} saved to {path} — use read_file with offset/limit or grep to inspect. ...]\n\n")
    return head + notice + tail, {"spool": str(path), "original_chars": len(content), "truncated": True}


class ToolRegistry:
    def __init__(self, ctx: ToolContext, allow: set[str] | None = None):
        self.ctx = ctx
        self.tools: dict[str, Tool] = {}
        self.allow = allow

    def register(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def add(self, name: str, description: str, parameters: dict[str, Any], handler: Handler,
            **kwargs: Any) -> None:
        self.register(Tool(name=name, description=description, parameters=parameters,
                           handler=handler, **kwargs))

    def schemas(self, allow: set[str] | None = None) -> list[dict[str, Any]]:
        effective = self.allow if allow is None else allow
        return [t.schema() for name, t in self.tools.items()
                if (effective is None or name in effective)]

    def describe(self) -> str:
        return "\n".join(f"- {n}: {t.description.splitlines()[0]}" for n, t in self.tools.items())

    async def execute(self, name: str, arguments: dict[str, Any] | str) -> ToolResult:
        tool = self.tools.get(name)
        if tool is not None and self.allow is not None and name not in self.allow:
            return ToolResult(f"Tool '{name}' is not permitted in this context.", is_error=True)
        if tool is None:
            return ToolResult(f"Unknown tool: {name}. Available: {', '.join(sorted(self.tools))}", is_error=True)
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except json.JSONDecodeError as exc:
                return ToolResult(f"Invalid JSON arguments for {name}: {exc}", is_error=True)
        if (not tool.read_only and tool.name != "propose_plan"
                and self.ctx.extra.get("plan_mode") and not self.ctx.extra.get("plan_approved")):
            return ToolResult(
                "Plan mode is on: mutating tools are blocked until the plan is approved. Inspect with "
                "read-only tools, then call propose_plan(steps=[...], actions=[...]) to request approval.",
                is_error=True)
        if not tool.read_only and self.ctx.approvals is not None:
            covered = "covered by approved plan" if self.ctx.extra.get("plan_approved") else "mutating tool"
            try:
                approved = self.ctx.approvals(tool, arguments, covered)
                if inspect.isawaitable(approved):
                    approved = await approved
            except Exception:
                approved = False
            if not approved:
                return ToolResult(f"Tool call '{name}' was denied by policy.", is_error=True)

        started = time.monotonic()
        self.ctx.emit("tool_start", name=name, args=_redact(arguments), tool_call_id=None)
        try:
            result = tool.handler(self.ctx, **arguments)
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, timeout=tool.timeout)
            if not isinstance(result, ToolResult):
                result = ToolResult(str(result))
        except asyncio.TimeoutError:
            result = ToolResult(f"Tool '{name}' timed out after {tool.timeout:.0f}s", is_error=True)
        except TypeError as exc:
            result = ToolResult(f"Bad arguments for '{name}': {exc}", is_error=True)
        except Exception as exc:  # noqa: BLE001 — tool errors are data, never crash the loop
            result = ToolResult(f"{type(exc).__name__}: {exc}", is_error=True)

        if len(result.content) > tool.max_output:
            text, meta = truncate_output(self.ctx, result.content, tool.max_output, label=name)
            result.content = text
            result.truncation = meta
        result.metadata.setdefault("duration_s", round(time.monotonic() - started, 3))
        result.metadata.setdefault("tool", name)
        self.ctx.emit("tool_end", name=name, is_error=result.is_error,
                      duration_s=result.metadata["duration_s"], chars=len(result.content))
        return result


_SECRET = re.compile(r"(sk-[A-Za-z0-9_-]{6})[A-Za-z0-9_-]+")


def _redact(obj: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "..."
    if isinstance(obj, str):
        return _SECRET.sub(r"\1***", obj[:400])
    if isinstance(obj, dict):
        return {k: ("***" if "key" in k.lower() or "token" in k.lower() or "secret" in k.lower() else _redact(v, depth + 1))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v, depth + 1) for v in obj[:20]]
    return obj
