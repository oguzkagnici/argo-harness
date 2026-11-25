"""The agent loop.

Design provenance (see docs/RESEARCH.md):
- ReAct-style interleaved reasoning/acting with tool observations (Yao et al., 2022).
- CodeAct-style executable actions (Wang et al., 2024) via run_code/bash.
- Planner/generator/evaluator decomposition and context resets from Anthropic's
  long-running harness work; subagents and plan tooling from Claude Code/Codex.
- Proactive memory injection + retrieval (Generative Agents; MemGPT; Anthropic memory).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..config import ArgoConfig
from ..llm.client import LLMClient, LLMError, StreamDelta
from ..memory.store import MemoryStore
from ..tools import (computer, docs, files, memory_tools, plan as plan_tool, research as research_tools,
                     shell, skills, web)
from ..tools.base import Tool, ToolContext, ToolRegistry
from ..tools.base import _redact
from ..workflows import tools as workflow_tools
from ..workflows.core import list_workflows
from .context import ContextManager
from .events import EventBus
from .prompts import build_system_prompt
from .session import Session
from .tokens import estimate_tools
from .types import Message, ToolCall, ToolResult, Usage

DeltaHandler = Callable[[StreamDelta], Awaitable[None] | None]
ApprovalFn = Callable[[Any, dict[str, Any], str], Awaitable[bool] | bool]

PROGRESS_TOOLS = {"write_file", "edit_file", "apply_patch", "create_document", "make_dir", "delete_path",
                  "remember", "forget", "save_workflow", "run_workflow", "index_workspace", "update_plan",
                  "spawn_subagent", "notify"}

LOOP_NUDGE = ("You are repeating checks without making progress. Stop inspecting now. If the artifact exists "
              "and the last check passed, move to the remaining deliverables. Use at most ONE verification per "
              "artifact, use read_document for Office files (never unzip DOCX/PPTX internals), and then finish.")


@dataclass
class RunResult:
    output: str
    session_id: str
    iterations: int
    usage: Usage
    stop_reason: str = "final"
    messages: list[Message] = field(default_factory=list)
    plan: list[dict[str, str]] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    compactions: int = 0
    resets: int = 0

    def brief(self) -> str:
        cached = f", {self.usage.cached_tokens:,} cached" if self.usage.cached_tokens else ""
        return (f"[{self.stop_reason}; {self.iterations} iter; {self.usage.total_tokens:,} tok{cached}; "
                f"session {self.session_id}]")


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    def __init__(self, cfg: ArgoConfig):
        self.cfg = cfg
        self.path = Path(cfg.data_dir) / "usage.json"
        self.today = time.strftime("%Y-%m-%d")
        self.data: dict[str, Any] = {}
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.data = {}
        self.data.setdefault(self.today, {"tokens": 0, "calls": 0})

    def check(self) -> None:
        limit = self.cfg.daily_token_budget
        if limit and self.data[self.today]["tokens"] >= limit:
            raise BudgetExceeded(f"daily token budget exhausted ({self.data[self.today]['tokens']:,}/{limit:,})")

    def add(self, usage: Usage) -> None:
        day = self.data.setdefault(self.today, {"tokens": 0, "calls": 0})
        day["tokens"] += usage.total_tokens
        day["calls"] += 1
        try:
            self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        except OSError:
            pass


class Agent:
    def __init__(self, cfg: ArgoConfig, llm: LLMClient | None = None, memory: MemoryStore | None = None,
                 events: EventBus | None = None, approvals: ApprovalFn | None = None,
                 max_iterations: int | None = None, depth: int = 0, on_delta: DeltaHandler | None = None,
                 allow_tools: set[str] | None = None, extra: dict[str, Any] | None = None):
        self.cfg = cfg
        self._llm = llm
        self._owns_llm = llm is None
        self._owns_memory = memory is None
        self.memory = memory
        self.events = events or EventBus()
        self.approvals = approvals
        self.max_iterations = max_iterations or cfg.max_iterations
        self.depth = depth
        self.on_delta = on_delta
        self.allow_tools = allow_tools
        self.extra = dict(extra or {})
        self.budget = Budget(cfg)
        self.messages: list[Message] = []
        self.session: Session | None = None
        self.ctxmgr: ContextManager | None = None
        self.registry: ToolRegistry | None = None
        self.tool_ctx: ToolContext | None = None
        self.last_usage = Usage()
        self.iterations = 0
        self._sig_counts: dict[str, int] = {}
        self._no_progress = 0
        self._nudges = 0
        self.mcp: Any = None

    # ------------------------------------------------------------- lifecycle
    async def __aenter__(self) -> "Agent":
        await self._ensure_clients()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        bg = (self.tool_ctx.extra.get("background") if self.tool_ctx is not None else None)
        if bg is not None:
            await bg.cancel_all()
        if self.mcp is not None:
            await self.mcp.close()
            self.mcp = None
        if self.tool_ctx is not None:
            try:
                await computer.shutdown(self.tool_ctx)
            except Exception:
                pass
        if self._owns_llm and self._llm is not None:
            await self._llm.__aexit__(None, None, None)
            self._llm = None
        if self.memory is not None and self._owns_memory:
            try:
                self.memory.close()
            except Exception:
                pass
            self.memory = None

    async def _ensure_clients(self) -> None:
        if self._llm is None:
            self._llm = LLMClient(self.cfg)
            await self._llm.__aenter__()
        if self.mcp is None and getattr(self.cfg, "mcp_servers", None) and self.depth == 0:
            from ..tools.mcp import MCPClient
            self.mcp = await MCPClient(self.cfg.mcp_servers).connect()
            self.events.emit("mcp_connected", servers=len(self.mcp.sessions),
                             tools=len(self.mcp.tools), errors=self.mcp.errors)
        if self.memory is None:
            self.memory = MemoryStore(self.cfg.memory_db, embed=self._llm.embed,
                                      rerank=lambda q, d, n: self._llm.rerank(q, d, top_n=n),
                                      model=self.cfg.models.embedding_model or "default",
                                      rrf_k=getattr(self.cfg, "rrf_k", 60),
                                      rrf_weights=(getattr(self.cfg, "rrf_lexical_weight", 0.8),
                                                   getattr(self.cfg, "rrf_dense_weight", 1.2)))

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            raise RuntimeError("Agent not started; use 'async with Agent(...)' or await agent._ensure_clients()")
        return self._llm

    # -------------------------------------------------------------- registry
    def _build_registry(self) -> ToolRegistry:
        assert self.tool_ctx is not None
        reg = ToolRegistry(self.tool_ctx, allow=self.allow_tools)
        for module in (files, shell, web, docs, memory_tools, plan_tool, skills, workflow_tools, computer,
                       research_tools):
            module.register(reg)
        if self.depth < 2 and (self.allow_tools is None or "spawn_subagent" in self.allow_tools):
            self._register_subagent(reg)
        if self.depth < 2 and (self.allow_tools is None or "review_work" in self.allow_tools):
            self._register_review(reg)
        if self.depth == 0:
            self._register_background(reg)
        if self.mcp is not None and self.mcp.tools:
            self._register_mcp(reg)
        if "daemon_queue" in self.extra:
            self._register_daemon_tools(reg)
        self.tool_ctx.extra["registry"] = reg
        return reg

    def _register_background(self, reg: ToolRegistry) -> None:
        from .background import BackgroundManager

        assert self.tool_ctx is not None
        manager = self.tool_ctx.extra.setdefault("background", BackgroundManager())

        async def start(ctx: ToolContext, task: str, max_iterations: int = 14) -> ToolResult:
            async def runner():
                child = Agent(self.cfg, llm=self._llm, memory=self.memory, events=self.events,
                              approvals=self.approvals, max_iterations=min(max_iterations, 40),
                              depth=self.depth + 1)
                child.session = Session.create(self.cfg, self.memory,
                                               session_id=f"{self.session.id if self.session else 's'}-bg-"
                                                          f"{uuid.uuid4().hex[:4]}")
                self.events.emit("background_start", task=task[:200])
                try:
                    return await child.run(task)
                finally:
                    await child.close()
                    self.events.emit("background_finished", task=task[:200])

            job = manager.start(task, runner)
            return ToolResult(f"Started background job {job.id}. Continue other work, then call "
                              f"check_background(id='{job.id}') for status and results.",
                              metadata={"job_id": job.id})

        async def check(ctx: ToolContext, id: str, wait_seconds: float = 0) -> ToolResult:  # noqa: A002
            job = manager.get(id)
            if job is None:
                return ToolResult(f"Unknown background job: {id}", is_error=True)
            if wait_seconds and job.status == "running" and job._task is not None:
                await asyncio.wait({job._task}, timeout=min(max(wait_seconds, 0), 300))
            return ToolResult(job.summary(), metadata={"status": job.status, "job_id": id})

        async def listing(ctx: ToolContext) -> ToolResult:
            return ToolResult(manager.listing())

        reg.add("start_background",
                "Start a subagent task in the background and return immediately with a job id. Use for long "
                "research branches or slow builds while you keep working on the critical path.",
                {"type": "object", "properties": {
                    "task": {"type": "string", "description": "Self-contained brief for the background agent."},
                    "max_iterations": {"type": "integer", "default": 14}}, "required": ["task"]},
                start, category="delegation")
        reg.add("check_background",
                "Check a background job. Pass wait_seconds (e.g. 30) to block until it finishes or the timeout "
                "elapses — prefer one waiting call over repeated polling.",
                {"type": "object", "properties": {"id": {"type": "string"},
                                                  "wait_seconds": {"type": "number", "default": 0}},
                 "required": ["id"]},
                check, read_only=True, timeout=320, category="delegation")
        reg.add("list_background", "List background jobs started in this run.",
                {"type": "object", "properties": {}}, listing, read_only=True, category="delegation")

    def _register_mcp(self, reg: ToolRegistry) -> None:
        assert self.mcp is not None
        timeouts = {s.name: getattr(s, "timeout", 120.0) for s in self.cfg.mcp_servers}
        read_only = {s.name: getattr(s, "read_only", False) for s in self.cfg.mcp_servers}
        for info in self.mcp.tools:
            client = self.mcp

            async def handler(ctx: ToolContext, _info=info, **kwargs: Any) -> ToolResult:
                ctx.emit("mcp_call", server=_info.server, tool=_info.name)
                return await client.call(_info.server, _info.name, kwargs)

            reg.register(Tool(
                name=info.exposed_name,
                description=(info.description or f"MCP tool {info.name} from {info.server}")[:1024],
                parameters=info.input_schema,
                handler=handler,
                read_only=bool(read_only.get(info.server, False)),
                parallel_safe=bool(read_only.get(info.server, False)),
                timeout=float(timeouts.get(info.server, 120.0)),
                category="mcp",
            ))

    def _register_subagent(self, reg: ToolRegistry) -> None:
        async def spawn(ctx: ToolContext, task: str, allowed_tools: list[str] | None = None,
                        max_iterations: int = 18, isolate: bool = False,
                        keep_worktree: bool = True) -> ToolResult:
            import copy

            child_cfg = self.cfg
            worktree: Path | None = None
            branch: str | None = None
            repo_root: Path | None = None
            if isolate:
                from .worktree import create_worktree
                try:
                    worktree, branch, repo_root = await create_worktree(
                        Path(self.cfg.workspace), task[:40], Path(self.cfg.data_dir))
                except Exception as exc:  # noqa: BLE001
                    return ToolResult(f"Could not isolate subagent: {exc}", is_error=True)
                child_cfg = copy.copy(self.cfg)
                child_cfg.workspace = worktree
                self.events.emit("worktree_created", path=str(worktree), branch=branch)
            child = Agent(child_cfg, llm=self._llm, memory=self.memory, events=self.events,
                          approvals=self.approvals, max_iterations=min(max_iterations, 40),
                          depth=self.depth + 1, allow_tools=set(allowed_tools) if allowed_tools else None)
            child.session = Session.create(self.cfg, self.memory,
                                           session_id=f"{self.session.id if self.session else 's'}-sub-"
                                                      f"{uuid.uuid4().hex[:4]}")
            self.events.emit("subagent_start", task=task[:200], depth=self.depth + 1,
                             worktree=str(worktree) if worktree else None)
            try:
                result = await child.run(task)
            finally:
                await child.close()
            self.events.emit("subagent_end", stop=result.stop_reason, tokens=result.usage.total_tokens)
            body = (f"Subagent finished ({result.brief()}).\n"
                    f"Files touched: {', '.join(result.files) or 'none'}\n")
            meta: dict[str, Any] = {"session": result.session_id, "iterations": result.iterations}
            if worktree is not None and branch is not None and repo_root is not None:
                from .worktree import changed_files, remove_worktree
                files = await changed_files(worktree, repo_root, branch)
                meta.update({"worktree": str(worktree), "branch": branch, "repo": str(repo_root),
                             "changed_files": files})
                body += (f"WORKTREE ISOLATED: branch {branch} at {worktree}\n"
                         f"Changed files: {', '.join(files) or 'none'}\n"
                         f"The parent workspace was NOT modified. To adopt: review the diff, then "
                         f"`git -C {repo_root} merge {branch}` (or cherry-pick).\n")
                if not keep_worktree:
                    await remove_worktree(repo_root, worktree, branch)
                    body += "Worktree and branch were removed (keep_worktree=false).\n"
            body += f"\nRESULT:\n{result.output}"
            return ToolResult(body, metadata=meta)

        reg.add(
            "spawn_subagent",
            "Delegate a focused, context-heavy subtask to a fresh agent with an isolated context window. "
            "Give it a complete, self-contained brief; it cannot see this conversation. It returns a compact "
            "result and the files it touched. Use for parallel research, independent verification, or large "
            "file investigations. Do critical-path work yourself.",
            {"type": "object", "properties": {
                "task": {"type": "string", "description": "Self-contained brief with goal, constraints, and expected output."},
                "allowed_tools": {"type": "array", "items": {"type": "string"},
                                  "description": "Optional allowlist, e.g. ['web_search','fetch_url','write_file']."},
                "max_iterations": {"type": "integer", "default": 18},
                "isolate": {"type": "boolean",
                            "description": "Run in a fresh git worktree + branch so the parent tree is untouched."},
                "keep_worktree": {"type": "boolean",
                                  "description": "Keep the worktree/branch after the run (default true)."}},
             "required": ["task"]},
            spawn, read_only=False, timeout=1800, max_output=30_000,
        )

    def _register_review(self, reg: ToolRegistry) -> None:
        async def review(ctx: ToolContext, criteria: str, paths: list[str] | None = None,
                         max_iterations: int = 6) -> ToolResult:
            readonly = {n for n, t in reg.tools.items() if t.read_only and n not in ("spawn_subagent",)}
            target = ", ".join(paths or []) or "the workspace artifacts named in the criteria"
            available = ", ".join(sorted(readonly))
            brief = (
                "You are an independent evaluator. A producer agent has completed work; audit it rigorously.\n\n"
                f"ACCEPTANCE CRITERIA:\n{criteria}\n\n"
                f"ARTIFACTS TO INSPECT: {target}\n\n"
                f"AVAILABLE TOOLS (the only permitted tools): {available}.\n"
                "Do not modify anything. Do not trust claims — verify against the actual artifacts.\n"
                "Guidance: use read_document for .docx/.pptx/.pdf/.xlsx (never cat or read_file a binary Office "
                "file); use read_file for text/code; use list_dir/grep_files for structure. bash, run_code and "
                "write tools are NOT available here, so do not attempt them. Inspect only the artifacts named "
                "above; do not glob or search the rest of the workspace.\n\n"
                "Report concisely: (1) verdict PASS/FAIL, (2) per-criterion score 0-10 with evidence, "
                "(3) concrete defects with a precise location, (4) the minimum set of fixes required. "
                "One inspection pass is enough unless a criterion requires a second artifact. "
                "Be specific and skeptical; do not pad with praise.")
            child = Agent(self.cfg, llm=self._llm, memory=self.memory, events=self.events,
                          approvals=self.approvals, max_iterations=max_iterations, depth=self.depth + 1,
                          allow_tools=readonly)
            child.session = Session.create(self.cfg, self.memory,
                                           session_id=f"{self.session.id if self.session else 's'}-rev-"
                                                      f"{uuid.uuid4().hex[:4]}")
            try:
                result = await child.run(brief)
            finally:
                await child.close()
            self.events.emit("review_end", verdict=result.output[:300])
            return ToolResult(f"INDEPENDENT REVIEW ({result.brief()}):\n\n{result.output}",
                              metadata={"reviewer_session": result.session_id})

        reg.add(
            "review_work",
            "Have an independent evaluator agent audit the current deliverable against explicit acceptance "
            "criteria before you finish. Read-only, skeptical, and separate from the producing context. "
            "Use for slide decks, reports, code changes, and any high-stakes artifact.",
            {"type": "object", "properties": {
                "criteria": {"type": "string", "description": "Explicit, checkable acceptance criteria."},
                "paths": {"type": "array", "items": {"type": "string"},
                          "description": "Files/artifacts to inspect."},
                "max_iterations": {"type": "integer", "default": 6}},
             "required": ["criteria"]},
            review, read_only=False, timeout=1200, max_output=30_000,
        )

    def _register_daemon_tools(self, reg: ToolRegistry) -> None:
        async def notify(ctx: ToolContext, message: str, channel: str = "user") -> ToolResult:
            q = ctx.extra.get("daemon_queue")
            gid = ctx.extra.get("goal_id")
            if q is None:
                return ToolResult("(notify) " + message)
            oid = q.send(gid, message, channel)
            return ToolResult(f"Queued notification {oid} to {channel}.", metadata={"outbox_id": oid})

        async def schedule_followup(ctx: ToolContext, task: str, delay_seconds: int = 3600) -> ToolResult:
            q = ctx.extra.get("daemon_queue")
            if q is None:
                return ToolResult("Follow-up timers are only available in always-on mode.", is_error=True)
            name = f"followup_{uuid.uuid4().hex[:8]}"
            q.add_trigger(name, "once", {"seconds": max(60, int(delay_seconds))}, task)
            return ToolResult(f"Scheduled follow-up '{name}' in {delay_seconds}s: {task[:120]}",
                              metadata={"trigger": name})

        reg.add("notify",
                "Send a progress update or result to the user's outbox (always-on mode). Keep messages short; "
                "prefer one message per meaningful milestone over a stream of noise.",
                {"type": "object", "properties": {"message": {"type": "string"},
                                                  "channel": {"type": "string", "default": "user"}},
                 "required": ["message"]},
                notify, read_only=False)
        reg.add("schedule_followup",
                "Set a one-shot timer that wakes you later with a task (e.g. check whether a deployment "
                "finished). Always-on mode only.",
                {"type": "object", "properties": {"task": {"type": "string"},
                                                  "delay_seconds": {"type": "integer", "default": 3600}},
                 "required": ["task"]},
                schedule_followup, read_only=False)

    # ----------------------------------------------------------------- state
    def _state_summary(self) -> str:
        parts = []
        if self.session and self.session.plan:
            parts.append("PLAN:\n" + "\n".join(f"- [{s.get('status')}] {s.get('step')}" for s in self.session.plan))
        if self.session and self.session.files_touched:
            parts.append("FILES TOUCHED:\n" + "\n".join(f"- {f}" for f in self.session.files_touched[-40:]))
        return "\n\n".join(parts)

    async def _build_context(self, task: str) -> list[Message]:
        assert self.memory is not None
        memory_block = self.memory.inject_block(budget_chars=getattr(self.cfg, "memory_inject_chars", 6000),
                                                query=task)
        recalled = ""
        try:
            hits = await self.memory.retrieve(task, k=6, include_files=False)
            if hits:
                recalled = "\n".join(h.text[:400] for h in hits)
        except Exception:
            pass
        skill_index = skills.skill_index(self.tool_ctx) if self.tool_ctx else ""
        system = build_system_prompt(self.cfg, memory_block=memory_block, skill_index=skill_index,
                                     plan=self.session.plan if self.session else None,
                                     recalled=recalled, depth=self.depth)
        return [Message(role="system", content=system)]

    # ------------------------------------------------------------------ run
    async def run(self, task: str, session: Session | None = None,
                  continue_conversation: bool = False, stream: bool | None = None) -> RunResult:
        await self._ensure_clients()
        assert self.memory is not None
        self.budget.check()

        if session is not None:
            self.session = session
        elif self.session is None:
            self.session = Session.create(self.cfg, self.memory)
        self.session.meta["goal"] = task

        self.tool_ctx = ToolContext(cfg=self.cfg, memory=self.memory, session=self.session,
                                    events=self.events, approvals=self.approvals, depth=self.depth,
                                    extra=dict(self.extra, plan_mode=bool(getattr(self.cfg, "plan_mode", False))))
        self.tool_ctx.extra.setdefault("llm", self._llm)
        self.registry = self._build_registry()
        schemas = self.registry.schemas()
        if self.ctxmgr is None:
            self.ctxmgr = ContextManager(self.cfg, llm=self._llm, events=self.events)
        self.ctxmgr.tool_tokens = estimate_tools(schemas)

        if not self.messages or not continue_conversation:
            self.messages = await self._build_context(task)
        user_msg = Message(role="user", content=task)
        self.messages.append(user_msg)
        self.session.log_message(user_msg)
        self.session.record("run_start", task=task, depth=self.depth)

        use_stream = stream if stream is not None else (self.on_delta is not None)
        start_usage = Usage(prompt_tokens=self.last_usage.prompt_tokens,
                            completion_tokens=self.last_usage.completion_tokens,
                            total_tokens=self.last_usage.total_tokens,
                            cached_tokens=self.last_usage.cached_tokens,
                            reasoning_tokens=self.last_usage.reasoning_tokens)
        iterations = 0
        stop_reason = "final"
        final_text = ""
        empty_retries = 0

        while iterations < self.max_iterations:
            iterations += 1
            self.iterations += 1
            try:
                self.budget.check()
            except BudgetExceeded:
                stop_reason = "budget"
                break

            self.messages = await self.ctxmgr.manage(self.messages, state_summary=self._state_summary())
            try:
                if use_stream:
                    resp = await self.llm.collect_stream(self.messages, schemas, on_delta=self.on_delta)
                else:
                    resp = await self.llm.chat(self.messages, schemas)
            except LLMError as exc:
                stop_reason = "error"
                final_text = f"LLM error: {exc}"
                self.events.emit("error", message=str(exc)[:500])
                break

            self.last_usage.add(resp.usage)
            self.budget.add(resp.usage)
            self.session.add_usage(resp.usage)
            if self.ctxmgr:
                self.ctxmgr.observe_usage(resp.usage.prompt_tokens, len(self.messages))
            self.events.emit("usage", **resp.usage.as_dict(), model=resp.model)

            assistant = resp.message
            self.messages.append(assistant)
            self.session.log_message(assistant)
            self.events.emit("assistant", content=assistant.content or "",
                             reasoning=assistant.reasoning or "",
                             tool_calls=[{"name": t.name, "arguments": t.arguments} for t in assistant.tool_calls])

            if not assistant.tool_calls:
                final_text = (assistant.content or "").strip()
                if not final_text and resp.finish_reason == "length" and empty_retries < 2:
                    empty_retries += 1
                    self.messages.append(Message(role="user", content=(
                        "Your previous response was cut off before any content. Continue from exactly where you "
                        "left off. Do not restart the task.")))
                    continue
                break

            results = await self._execute_tool_calls(assistant.tool_calls)
            images: list[str] = []
            for tc, res in results:
                tool_msg = Message(role="tool", content=res.content, tool_call_id=tc.id, name=tc.name)
                self.messages.append(tool_msg)
                if res.metadata.get("image"):
                    images.append(res.metadata["image"])
                self.session.log_tool(tc.name, tc.args(), res.content, res.is_error)
                for key in ("path", "screenshot", "spool"):
                    if res.metadata.get(key) and tc.name not in ("recall",):
                        self.session.touch_file(str(res.metadata[key]))
                if tc.name in ("write_file", "edit_file", "create_document", "apply_patch"):
                    p = tc.args().get("path")
                    if p:
                        self.session.touch_file(str(self.tool_ctx.resolve(p)))
            if images:
                self.messages.append(Message(role="user", images=images,
                                             content="Screenshot(s) from the last computer action."))

            made_progress = any(tc.name in PROGRESS_TOOLS and not res.is_error for tc, res in results)
            if made_progress:
                self._no_progress = 0
                self._sig_counts.clear()
            else:
                self._no_progress += 1
                limit = max(2, int(getattr(self.cfg, "no_progress_limit", 10) or 10))
                nudge_at = {max(2, int(limit * 0.4)), max(3, int(limit * 0.7))}
                if self._no_progress in nudge_at and self._nudges < 2:
                    self._nudges += 1
                    self.messages.append(Message(role="user", content=LOOP_NUDGE))
                    self.events.emit("loop_nudge", reason="no_progress", count=self._no_progress)
                if self._no_progress >= limit:
                    stop_reason = "no_progress"
                    self.events.emit("loop_stop", reason="no_progress")
                    break
        else:
            stop_reason = "max_iterations"

        if not final_text and stop_reason in ("max_iterations", "no_progress"):
            try:
                self.messages.append(Message(role="user", content=(
                    "You reached the step limit. Do not call tools. Report concisely: what is complete, what is "
                    "verified, what remains, and exact next steps for the user.")))
                resp = await self.llm.chat(self.messages, None)
                self.last_usage.add(resp.usage)
                self.budget.add(resp.usage)
                final_text = (resp.message.content or "").strip()
            except Exception:
                final_text = "(stopped at step limit; no summary available)"

        usage = Usage(
            prompt_tokens=self.last_usage.prompt_tokens - start_usage.prompt_tokens,
            completion_tokens=self.last_usage.completion_tokens - start_usage.completion_tokens,
            total_tokens=self.last_usage.total_tokens - start_usage.total_tokens,
        )
        if final_text:
            self.session.record("final", content=final_text)
        self.session.record("run_end", stop_reason=stop_reason, iterations=iterations)
        self.events.emit("run_end", stop_reason=stop_reason, iterations=iterations,
                         tokens=usage.total_tokens, session=self.session.id)
        return RunResult(
            output=final_text or "(no final answer)", session_id=self.session.id, iterations=iterations,
            usage=usage, stop_reason=stop_reason, messages=list(self.messages),
            plan=self.session.plan, files=list(self.session.files_touched),
            compactions=self.ctxmgr.stats.compactions if self.ctxmgr else 0,
            resets=self.ctxmgr.stats.resets if self.ctxmgr else 0,
        )

    # ---------------------------------------------------------- tool execution
    @staticmethod
    def _sig(tc: ToolCall) -> str:
        return tc.name + ":" + hashlib.sha1((tc.arguments or "{}").encode("utf-8")).hexdigest()

    async def _execute_tool_calls(self, calls: list[ToolCall]) -> list[tuple[ToolCall, ToolResult]]:
        assert self.registry is not None
        blocked: dict[int, ToolResult] = {}
        todo: list[tuple[int, ToolCall]] = []
        for i, tc in enumerate(calls):
            tool = self.registry.tools.get(tc.name)
            sig = self._sig(tc)
            if tool is not None and tool.read_only and self._sig_counts.get(sig, 0) >= 2:
                blocked[i] = ToolResult(
                    "Duplicate read-only call blocked: this exact call already ran twice with no intervening "
                    "progress. Use the previous result and proceed.", metadata={"duplicate": True})
                self.events.emit("tool_duplicate", name=tc.name)
            else:
                todo.append((i, tc))
                self._sig_counts[sig] = self._sig_counts.get(sig, 0) + 1

        parallel = len(todo) > 1 and all(
            self.registry.tools.get(tc.name) and self.registry.tools[tc.name].read_only
            and self.registry.tools[tc.name].parallel_safe for _, tc in todo)
        for _, tc in todo:
            self.events.emit("tool_call", name=tc.name, args=_redact(tc.args()))

        executed: list[tuple[int, ToolCall, ToolResult]] = []
        if parallel:
            results = await asyncio.gather(*(self.registry.execute(tc.name, tc.arguments) for _, tc in todo))
            executed = [(i, tc, res) for (i, tc), res in zip(todo, results)]
        else:
            for i, tc in todo:
                executed.append((i, tc, await self.registry.execute(tc.name, tc.arguments)))

        out: list[tuple[ToolCall, ToolResult]] = []
        by_index = {i: res for i, _tc, res in executed}
        for i, tc in enumerate(calls):
            if i in blocked:
                out.append((tc, blocked[i]))
            else:
                out.append((tc, by_index[i]))
        return out


async def run_once(cfg: ArgoConfig, task: str, **kwargs: Any) -> RunResult:
    async with Agent(cfg, **kwargs) as agent:
        return await agent.run(task)
