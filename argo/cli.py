"""Argo command line interface."""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from .config import load_config
from .core.agent import Agent
from .core.events import Event
from .core.session import Session, list_sessions
from .llm.client import LLMClient
from .memory.dream import DreamEngine
from .memory.store import MemoryStore

app = typer.Typer(add_completion=False, help="Argo — a general-purpose agent harness.")
memory_app = typer.Typer(help="Inspect and curate long-term memory.")
workflow_app = typer.Typer(help="Record, inspect, and replay workflows.")
session_app = typer.Typer(help="Inspect sessions and traces.")
app.add_typer(memory_app, name="memory")
app.add_typer(workflow_app, name="workflow")
app.add_typer(session_app, name="session")

console = Console()
err_console = Console(stderr=True)


def _cfg(model: Optional[str], base_url: Optional[str], workspace: Optional[str]):
    return load_config({"model": model, "base_url": base_url,
                        "workspace": workspace or str(Path.cwd())})


def _approval_fn(yolo: bool = False):
    """Interactive approval for dangerous shell/code actions, mirroring Codex/Claude Code."""
    from .daemon import danger_check

    def approve(tool, args, reason):
        if getattr(tool, "name", "") == "propose_plan":
            if yolo:
                return True
            if not sys.stdin.isatty():
                err_console.print("[red]plan mode requires interactive approval[/] (use --yolo to auto-approve)")
                return False
            console.print("[bold]Plan for approval[/]")
            for i, step in enumerate(args.get("steps", []), 1):
                console.print(f"  {i}. {step}")
            for action in args.get("actions", []):
                console.print(f"  [dim]action:[/] {action}")
            return typer.confirm("Approve this plan and enable mutating tools?", default=False)
        danger = danger_check(tool.name, args)
        if not danger:
            return True
        if yolo:
            console.print(f"[yellow]⚠ allowing risky action (--yolo):[/] {danger}")
            return True
        if not sys.stdin.isatty():
            err_console.print(f"[red]blocked risky action:[/] {danger} (use --yolo to allow)")
            return False
        console.print(f"[yellow]⚠ risky action:[/] {tool.name}: {danger}")
        return typer.confirm("Allow this action?", default=False)
    return approve


def _print_event(ev: Event) -> None:
    if ev.type == "tool_call":
        args = ev.data.get("args") or {}
        preview = json.dumps(args, ensure_ascii=False)
        if len(preview) > 220:
            preview = preview[:220] + "…"
        console.print(f"[bold cyan]→ {ev.data.get('name')}[/] [dim]{preview}[/]")
    elif ev.type == "tool_end":
        mark = "[red]✗[/]" if ev.data.get("is_error") else "[green]✓[/]"
        console.print(f"  {mark} [dim]{ev.data.get('name')} · {ev.data.get('duration_s')}s · "
                      f"{ev.data.get('chars')} chars[/]")
    elif ev.type == "context_compacted":
        console.print(f"[yellow]◐ context compacted[/] [dim]({ev.data.get('summarized')} msgs → summary)[/]")
    elif ev.type == "context_reset":
        console.print(f"[yellow]↻ context reset[/] [dim]handoff: {ev.data.get('handoff')}[/]")
    elif ev.type == "subagent_start":
        console.print(f"[magenta]⤷ subagent[/] [dim]{ev.data.get('task')}[/]")
    elif ev.type == "subagent_end":
        console.print(f"[magenta]⤶ subagent done[/] [dim]{ev.data.get('tokens')} tok[/]")
    elif ev.type == "file_written":
        console.print(f"  [dim]saved {ev.data.get('path')}[/]")


def _printer(show_reasoning: bool):
    state = {"thinking": False, "text": False}

    async def on_delta(delta) -> None:
        if delta.done:
            if state["text"]:
                sys.stdout.write("\n")
                sys.stdout.flush()
                state["text"] = False
            return
        if delta.reasoning:
            if show_reasoning:
                if not state["thinking"]:
                    sys.stdout.write("\n\033[2m[thinking] ")
                    state["thinking"] = True
                sys.stdout.write(delta.reasoning)
                sys.stdout.flush()
        if delta.text:
            if state["thinking"]:
                sys.stdout.write("\033[0m\n")
                state["thinking"] = False
            state["text"] = True
            sys.stdout.write(delta.text)
            sys.stdout.flush()

    return on_delta


@app.command()
def run(task: str = typer.Argument(..., help="Task for the agent."),
        model: Optional[str] = typer.Option(None, "--model", "-m"),
        base_url: Optional[str] = typer.Option(None, "--base-url"),
        workspace: Optional[str] = typer.Option(None, "--workspace", "-w"),
        session: Optional[str] = typer.Option(None, "--session"),
        stream: bool = typer.Option(True, "--stream/--no-stream"),
        show_reasoning: bool = typer.Option(False, "--reasoning", help="Show reasoning deltas."),
        max_iterations: Optional[int] = typer.Option(None, "--max-iterations"),
        cheap_model: Optional[str] = typer.Option(None, "--cheap-model",
                                                  help="Cheaper model for compaction/handoff summaries."),
        router: Optional[str] = typer.Option(None, "--router",
                                             help="off|explore — route read-only turns to --cheap-model."),
        low_priority: bool = typer.Option(False, "--low-priority",
                                          help="Pace provider calls to avoid starving other clients."),
        budget: Optional[int] = typer.Option(None, "--budget", help="Max tokens for this run."),
        max_wall: Optional[float] = typer.Option(None, "--max-wall", help="Max wall-clock seconds for this run."),
        max_tool_calls: Optional[int] = typer.Option(None, "--max-tool-calls",
                                                     help="Max tool calls for this run."),
        yolo: bool = typer.Option(False, "--yolo", help="Allow dangerous shell/code actions without confirmation."),
        plan: bool = typer.Option(False, "--plan", help="Read-only until a plan is approved."),
        review: Optional[str] = typer.Option(None, "--review",
                                             help="manual|adaptive|always|off — automatic review policy.")) -> None:
    """Run one task to completion."""
    cfg = _cfg(model, base_url, workspace)
    if max_iterations:
        cfg.max_iterations = max_iterations
    if cheap_model:
        cfg.cheap_model = cheap_model
    if router:
        cfg.router_policy = router
    if low_priority:
        cfg.low_priority_pacing = 2.0
    if budget:
        cfg.max_run_tokens = budget
    if max_wall:
        cfg.max_run_wall_seconds = max_wall
    if max_tool_calls:
        cfg.max_run_tool_calls = max_tool_calls
    cfg.plan_mode = plan or cfg.plan_mode
    if review:
        cfg.review_policy = review
    asyncio.run(_run_task(cfg, task, session, stream, show_reasoning, yolo))


async def _run_task(cfg, task: str, session_id: str | None, stream: bool, show_reasoning: bool,
                    yolo: bool = False) -> None:
    agent = Agent(cfg, on_delta=_printer(show_reasoning) if stream else None, approvals=_approval_fn(yolo))
    agent.events.subscribe(_print_event)
    try:
        async with agent:
            sess = Session.load(cfg, session_id) if session_id else None
            result = await agent.run(task, session=sess, stream=stream)
    finally:
        await agent.close()
    console.print()
    if result.output and not stream:
        console.print(Markdown(result.output))
    console.print(f"[dim]{escape(result.brief())} · files: {escape(', '.join(result.files) or 'none')}[/]")


@app.command()
def chat(model: Optional[str] = typer.Option(None, "--model", "-m"),
         base_url: Optional[str] = typer.Option(None, "--base-url"),
         workspace: Optional[str] = typer.Option(None, "--workspace", "-w"),
         resume: Optional[str] = typer.Option(None, "--resume"),
         show_reasoning: bool = typer.Option(False, "--reasoning"),
         stream: bool = typer.Option(True, "--stream/--no-stream"),
         yolo: bool = typer.Option(False, "--yolo")) -> None:
    """Interactive multi-turn session."""
    cfg = _cfg(model, base_url, workspace)
    asyncio.run(_chat(cfg, resume, show_reasoning, stream, yolo))


async def _chat(cfg, resume: str | None, show_reasoning: bool, stream: bool, yolo: bool = False) -> None:
    agent = Agent(cfg, on_delta=_printer(show_reasoning) if stream else None, approvals=_approval_fn(yolo))
    agent.events.subscribe(_print_event)
    mem = MemoryStore(cfg.memory_db, model=cfg.models.embedding_model or "default")
    st = mem.stats()
    mem.close()
    console.print(Panel.fit(
        f"[bold]Argo[/] · {cfg.provider.model}\n[dim]{cfg.provider.base_url}\n"
        f"workspace: {cfg.workspace}\nmemory: gen {st['generation']}, "
        f"{sum(sum(v.values()) for v in st['layers'].values())} items, {st['transcripts']} transcripts\n"
        "commands: /dream, /memory <q>, /plan, /new, /files, /stats, /quit[/]",
        border_style="cyan"))
    try:
        async with agent:
            if resume:
                agent.session = Session.load(cfg, resume, agent.memory)
                transcript = agent.session.transcript_messages()
                if transcript:
                    agent.messages = [agent.messages[0] if agent.messages else
                                      (await agent._build_context(resume))[0]] + transcript[-40:]
                console.print(f"[dim]resumed {resume} ({len(transcript)} messages)[/]")
            while True:
                try:
                    text = await asyncio.to_thread(console.input, "\n[bold cyan]you ›[/] ")
                except (EOFError, KeyboardInterrupt):
                    break
                text = text.strip()
                if not text:
                    continue
                if text in ("/quit", "/exit", "exit", "quit"):
                    break
                if text == "/new":
                    agent.session = None
                    agent.messages = []
                    agent.ctxmgr = None
                    console.print("[dim]new session[/]")
                    continue
                if text.startswith("/dream"):
                    apply = "--apply" in text
                    rep = await DreamEngine(agent.memory, agent.llm, cfg).run(apply=apply)
                    console.print(Markdown(rep.to_markdown()))
                    continue
                if text.startswith("/memory"):
                    q = text[len("/memory"):].strip() or "recent important context"
                    hits = await agent.memory.retrieve(q, k=8, include_files=False)
                    for h in hits:
                        console.print(f"[dim]{h.citation()} {h.score:.2f}[/] {h.text[:300]}")
                    continue
                if text == "/plan":
                    if agent.session and agent.session.plan:
                        for i, s in enumerate(agent.session.plan, 1):
                            console.print(f"{i}. [{s.get('status')}] {s.get('step')}")
                    else:
                        console.print("[dim]no plan yet[/]")
                    continue
                if text == "/files":
                    console.print("files: " + (", ".join(agent.session.files_touched) if agent.session else "none"))
                    continue
                if text == "/stats":
                    if agent.session:
                        console.print(json.dumps(agent.session.usage.as_dict()))
                    console.print(json.dumps(agent.memory.stats()))
                    continue
                try:
                    result = await agent.run(text, continue_conversation=True, stream=stream)
                except Exception as exc:  # noqa: BLE001
                    err_console.print(f"[red]error:[/] {type(exc).__name__}: {exc}")
                    continue
                console.print()
                if not stream and result.output:
                    console.print(Markdown(result.output))
                console.print(f"[dim]{escape(result.brief())}[/]")
    finally:
        await agent.close()


@app.command()
def serve(host: str = typer.Option("127.0.0.1", "--host"), port: int = typer.Option(8000, "--port"),
          model: Optional[str] = typer.Option(None, "--model"), workspace: Optional[str] = typer.Option(None, "--workspace"),
          api_key: Optional[str] = typer.Option(None, "--api-key", help="Require this bearer token from clients.")) -> None:
    """Serve an OpenAI-compatible API plus native agent endpoints."""
    import uvicorn
    from .server import create_app
    cfg = _cfg(model, None, workspace)
    application = create_app(cfg, require_key=api_key)
    console.print(f"[bold]Argo server[/] → http://{host}:{port}  (model: {cfg.provider.model})")
    uvicorn.run(application, host=host, port=port, log_level="info")


@app.command()
def daemon(once: bool = typer.Option(False, "--once", help="Run one scheduling tick and exit."),
           interval: int = typer.Option(30, "--interval", help="Scheduler poll seconds."),
           dream_hours: float = typer.Option(24.0, "--dream-hours", help="Hours between dream cycles."),
           dream_generations: int = typer.Option(1, "--dream-generations",
                                                help="Consolidation passes per cycle (2 costs ~3x for no measured gain)."),
           workspace: Optional[str] = typer.Option(None, "--workspace"),
           model: Optional[str] = typer.Option(None, "--model")) -> None:
    """Always-on mode: triggers, goal queue, approvals, scheduled dreaming."""
    from .daemon import Daemon
    cfg = _cfg(model, None, workspace)
    asyncio.run(Daemon(cfg, once=once, poll_seconds=interval, dream_every_hours=dream_hours,
                       dream_generations=dream_generations).run())


@app.command()
def dream(apply: bool = typer.Option(False, "--apply", help="Activate the new memory generation."),
          sessions: int = typer.Option(30, "--sessions", "-n", help="How many recent sessions to review."),
          instructions: str = typer.Option("", "--instructions", "-i"),
          model: Optional[str] = typer.Option(None, "--model"),
          workspace: Optional[str] = typer.Option(None, "--workspace"),
          show: bool = typer.Option(True, "--show/--no-show")) -> None:
    """Consolidate memory offline (dedupe, correct against transcripts, build wiki/insights)."""
    cfg = _cfg(model, None, workspace)
    asyncio.run(_dream(cfg, apply, sessions, instructions, show))


async def _dream(cfg, apply: bool, sessions: int, instructions: str, show: bool) -> None:
    async with LLMClient(cfg) as llm:
        mem = MemoryStore(cfg.memory_db, embed=llm.embed,
                          rerank=lambda q, d, n: llm.rerank(q, d, top_n=n),
                          model=cfg.models.embedding_model or "default")
        with console.status("[cyan]dreaming over transcripts…[/]"):
            report = await DreamEngine(mem, llm, cfg).run(instructions=instructions, apply=apply,
                                                          max_sessions=sessions)
        out_dir = Path(cfg.data_dir) / "dreams"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{report.id}.md").write_text(report.to_markdown(), encoding="utf-8")
        if show:
            console.print(Markdown(report.to_markdown()))
        console.print(f"[dim]report: {out_dir / (report.id + '.md')}[/]")
        if not apply and report.status == "completed":
            console.print("[yellow]Not activated.[/] Review, then run `argo memory activate "
                          f"{report.output_generation}` (or re-run with --apply).[/]")
        mem.close()


@memory_app.command("stats")
def mem_stats(workspace: Optional[str] = typer.Option(None, "--workspace")) -> None:
    cfg = _cfg(None, None, workspace)
    mem = MemoryStore(cfg.memory_db)
    st = mem.stats()
    console.print(json.dumps(st, indent=2))
    mem.close()


@memory_app.command("list")
def mem_list(layer: Optional[str] = typer.Option(None, "--layer"), limit: int = typer.Option(50, "--limit"),
             workspace: Optional[str] = typer.Option(None, "--workspace")) -> None:
    cfg = _cfg(None, None, workspace)
    mem = MemoryStore(cfg.memory_db)
    table = Table("id", "layer", "kind", "imp", "conf", "text")
    for it in mem.list_items(layer=layer, limit=limit):
        table.add_row(it.id[:10], it.layer, it.kind, f"{it.importance:.1f}", f"{it.confidence:.1f}",
                      (it.title or it.text)[:90])
    console.print(table)
    mem.close()


@memory_app.command("search")
def mem_search(query: str = typer.Argument(...), k: int = typer.Option(8, "--k"),
               files: bool = typer.Option(False, "--files"),
               workspace: Optional[str] = typer.Option(None, "--workspace")) -> None:
    cfg = _cfg(None, None, workspace)
    asyncio.run(_mem_search(cfg, query, k, files))


async def _mem_search(cfg, query: str, k: int, files: bool) -> None:
    async with LLMClient(cfg) as llm:
        mem = MemoryStore(cfg.memory_db, embed=llm.embed,
                          rerank=lambda q, d, n: llm.rerank(q, d, top_n=n),
                          model=cfg.models.embedding_model or "default")
        hits = await mem.retrieve(query, k=k, include_files=files)
        for h in hits:
            console.print(f"[cyan]{h.citation()}[/] [dim]score={h.score:.3f} {h.layer}/{h.kind}[/]")
            console.print(h.text[:600] + ("…" if len(h.text) > 600 else ""))
        mem.close()


@memory_app.command("add")
def mem_add(text: str = typer.Argument(...), layer: str = typer.Option("note", "--layer"),
            kind: str = typer.Option("fact", "--kind"), importance: float = typer.Option(0.7, "--importance"),
            workspace: Optional[str] = typer.Option(None, "--workspace")) -> None:
    cfg = _cfg(None, None, workspace)
    asyncio.run(_mem_add(cfg, text, layer, kind, importance))


async def _mem_add(cfg, text: str, layer: str, kind: str, importance: float) -> None:
    async with LLMClient(cfg) as llm:
        mem = MemoryStore(cfg.memory_db, embed=llm.embed, model=cfg.models.embedding_model or "default")
        mid = await mem.add(text, layer=layer, kind=kind, importance=importance)
        console.print(f"added {mid}")
        mem.close()


@memory_app.command("export")
def mem_export(out: str = typer.Option("", "--out"), git: bool = typer.Option(False, "--git"),
               workspace: Optional[str] = typer.Option(None, "--workspace")) -> None:
    """Export memory as inspectable markdown (optionally git-committed, Instinct-style)."""
    import subprocess
    cfg = _cfg(None, None, workspace)
    mem = MemoryStore(cfg.memory_db)
    target = Path(out) if out else Path(cfg.data_dir) / "memory_export"
    path = mem.export_markdown(target)
    mem.close()
    console.print(f"exported → {path}")
    if git:
        try:
            if not (target / ".git").exists():
                subprocess.run(["git", "init", "-q"], cwd=target, check=True)
            subprocess.run(["git", "add", "-A"], cwd=target, check=True)
            subprocess.run(["git", "-c", "user.email=argo@local", "-c", "user.name=Argo",
                            "commit", "-qm", f"memory generation {time.strftime('%Y-%m-%d %H:%M')}"],
                           cwd=target, check=False)
            console.print(f"[dim]committed memory snapshot in {target}/.git[/]")
        except (OSError, subprocess.CalledProcessError) as exc:
            err_console.print(f"[yellow]git export skipped: {exc}[/]")


@memory_app.command("activate")
def mem_activate(generation: int = typer.Argument(...), workspace: Optional[str] = typer.Option(None, "--workspace")) -> None:
    cfg = _cfg(None, None, workspace)
    mem = MemoryStore(cfg.memory_db)
    mem._set_meta("active_generation", str(generation))
    console.print(f"active generation → {generation}")
    mem.close()


@memory_app.command("dreams")
def mem_dreams(workspace: Optional[str] = typer.Option(None, "--workspace")) -> None:
    cfg = _cfg(None, None, workspace)
    mem = MemoryStore(cfg.memory_db)
    table = Table("id", "status", "gen", "added", "merged", "archived", "summary")
    for d in DreamEngine(mem, None, cfg).list_dreams():
        rep = json.loads(d.get("report") or "{}")
        table.add_row(d["id"], d["status"], f"{d['input_generation']}→{d['output_generation']}",
                      str(rep.get("added", "")), str(rep.get("merged", "")), str(rep.get("archived", "")),
                      (d.get("summary") or "")[:70])
    console.print(table)
    mem.close()


@app.command()
def index(path: str = typer.Argument("."), workspace: Optional[str] = typer.Option(None, "--workspace")) -> None:
    """Chunk + embed a directory for hybrid retrieval."""
    cfg = _cfg(None, None, workspace)
    asyncio.run(_index(cfg, path))


async def _index(cfg, path: str) -> None:
    async with LLMClient(cfg) as llm:
        mem = MemoryStore(cfg.memory_db, embed=llm.embed, model=cfg.models.embedding_model or "default")
        with console.status("[cyan]indexing…[/]"):
            res = await mem.index_workspace(Path(path).resolve())
        console.print(f"indexed {res['files']} files → {res['chunks']} chunks")
        mem.close()


@workflow_app.command("list")
def wf_list(workspace: Optional[str] = typer.Option(None, "--workspace")) -> None:
    from .workflows.core import list_workflows
    cfg = _cfg(None, None, workspace)
    wfs = list_workflows(Path(cfg.workflow_dir))
    if not wfs:
        console.print("[dim]no workflows yet — run `argo workflow extract` after a successful session[/]")
    for w in wfs:
        console.print(f"[bold]{w.name}[/] [dim]({len(w.steps)} steps, {len(w.params)} params)[/]\n  {w.description}")


@workflow_app.command("show")
def wf_show(name: str = typer.Argument(...), workspace: Optional[str] = typer.Option(None, "--workspace")) -> None:
    from .workflows.core import load_workflow
    cfg = _cfg(None, None, workspace)
    wf = load_workflow(Path(cfg.workflow_dir), name)
    if not wf:
        raise typer.Exit(1)
    console.print_json(json.dumps(wf.to_dict()))


@workflow_app.command("extract")
def wf_extract(session: str = typer.Argument(..., help="Session id (see `argo session list`)."),
               name: str = typer.Option("", "--name", help="Workflow name."),
               instructions: str = typer.Option("", "--instructions", "-i"),
               workspace: Optional[str] = typer.Option(None, "--workspace")) -> None:
    """Turn a successful session trace into a replayable, parameterized workflow + skill."""
    cfg = _cfg(None, None, workspace)
    asyncio.run(_wf_extract(cfg, session, name, instructions))


async def _wf_extract(cfg, session_id: str, name: str, instructions: str) -> None:
    from .tools.base import ToolContext
    from .workflows.synth import synthesize_workflow, workflow_to_skill
    sess = Session.load(cfg, session_id)
    events = sess.events()
    if not any(e.get("type") == "tool" for e in events):
        err_console.print("[red]session has no tool calls[/]")
        raise typer.Exit(1)
    async with Agent(cfg) as agent:
        agent.session = sess
        agent.tool_ctx = ToolContext(cfg=cfg, memory=agent.memory, session=sess, events=agent.events)
        reg = agent._build_registry()
        catalog = reg.schemas()
        with console.status("[cyan]synthesizing workflow…[/]"):
            wf = await synthesize_workflow(agent.llm, events, catalog, name_hint=name or session_id,
                                           instructions=instructions)
        path = wf.save(Path(cfg.workflow_dir))
        skill_dir = Path(cfg.skill_dir) / wf.name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(workflow_to_skill(wf), encoding="utf-8")
        console.print(f"workflow → {path}")
        console.print(f"skill    → {skill_dir / 'SKILL.md'}")
        console.print(Markdown(f"**{wf.name}** — {wf.description}\n\n{len(wf.steps)} steps, "
                               f"params: {', '.join(p['name'] for p in wf.params) or 'none'}"))


@workflow_app.command("run")
def wf_run(name: str = typer.Argument(...), params: list[str] = typer.Option([], "--param", "-p",
                                                                             help="key=value"),
           workspace: Optional[str] = typer.Option(None, "--workspace")) -> None:
    cfg = _cfg(None, None, workspace)
    kv = {}
    for item in params:
        if "=" not in item:
            raise typer.BadParameter(f"expected key=value, got {item!r}")
        k, v = item.split("=", 1)
        try:
            kv[k] = json.loads(v)
        except json.JSONDecodeError:
            kv[k] = v
    asyncio.run(_wf_run(cfg, name, kv))


async def _wf_run(cfg, name: str, params: dict) -> None:
    from .tools.base import ToolContext
    from .workflows.core import load_workflow, run_workflow
    wf = load_workflow(Path(cfg.workflow_dir), name)
    if not wf:
        err_console.print(f"[red]workflow {name!r} not found[/]")
        raise typer.Exit(1)
    async with Agent(cfg) as agent:
        agent.session = Session.create(cfg, agent.memory)
        agent.tool_ctx = ToolContext(cfg=cfg, memory=agent.memory, session=agent.session, events=agent.events)
        reg = agent._build_registry()
        run = await run_workflow(wf, params, reg)
        console.print(Markdown("```\n" + run.summary() + "\n```"))
        if not run.ok:
            raise typer.Exit(1)


@session_app.command("list")
def sess_list(workspace: Optional[str] = typer.Option(None, "--workspace")) -> None:
    cfg = _cfg(None, None, workspace)
    table = Table("session", "modified", "workspace")
    for s in list_sessions(cfg):
        table.add_row(s["id"], time.strftime("%Y-%m-%d %H:%M", time.localtime(s["mtime"])), s["workspace"])
    console.print(table)


@session_app.command("show")
def sess_show(session: str = typer.Argument(...), workspace: Optional[str] = typer.Option(None, "--workspace")) -> None:
    cfg = _cfg(None, None, workspace)
    sess = Session.load(cfg, session)
    for ev in sess.events():
        t = ev.get("type")
        if t == "tool":
            console.print(f"[cyan]{ev.get('name')}[/] {json.dumps(ev.get('args'), ensure_ascii=False)[:160]}")
        elif t == "message":
            console.print(f"[bold]{ev.get('role')}[/]: {(ev.get('content') or '')[:300]}")
        elif t in ("plan", "run_start", "run_end", "usage"):
            console.print(f"[dim]{t}: {json.dumps({k: v for k, v in ev.items() if k not in ('ts',)}, default=str)[:200]}[/]")


@app.command()
def trace(session: str = typer.Argument(..., help="Session id (see `argo session list`)."),
          limit: int = typer.Option(200, "--limit", help="Show the last N events."),
          workspace: Optional[str] = typer.Option(None, "--workspace")) -> None:
    """Render a session transcript as a timeline (tools, usage, compactions)."""
    from .core.observability import load_events, render_trace

    cfg = _cfg(None, None, workspace)
    path = Path(cfg.session_dir) / f"{session}.jsonl"
    if not path.exists():
        err_console.print(f"[red]session {session!r} not found[/] ({path})")
        raise typer.Exit(1)
    for line in render_trace(load_events(path), limit=limit):
        console.print(line, markup=False, highlight=False)


@app.command("export-otel")
def export_otel(session: str = typer.Argument(..., help="Session id."),
                out: Optional[str] = typer.Option(None, "--out", "-o", help="Write OTLP/JSON here."),
                service: str = typer.Option("argo", "--service", help="OTel service.name."),
                workspace: Optional[str] = typer.Option(None, "--workspace")) -> None:
    """Export a session as OpenTelemetry spans (OTLP/JSON, GenAI semantic conventions)."""
    import json as _json
    from .core.observability import load_events, otel_payload, to_otel_spans

    cfg = _cfg(None, None, workspace)
    path = Path(cfg.session_dir) / f"{session}.jsonl"
    if not path.exists():
        err_console.print(f"[red]session {session!r} not found[/] ({path})")
        raise typer.Exit(1)
    spans = to_otel_spans(load_events(path), session)
    body = _json.dumps(otel_payload(spans, service), indent=2)
    if out:
        Path(out).write_text(body, encoding="utf-8")
        console.print(f"[green]wrote[/] {len(spans)} spans → {out}")
    else:
        console.print_json(body)


@app.command()
def metrics(dir: Optional[str] = typer.Option(None, "--dir", help="Directory of session JSONL files."),
            limit: int = typer.Option(500, "--limit", help="Most recent N sessions."),
            workspace: Optional[str] = typer.Option(None, "--workspace"),
            cost: bool = typer.Option(False, "--cost", help="Show estimated USD cost (needs [pricing] config)."),
            since: Optional[str] = typer.Option(None, "--since",
                                                help="Only events after this point: 90m, 24h, 7d, or YYYY-MM-DD."),
            json_out: bool = typer.Option(False, "--json", help="Print raw JSON.")) -> None:
    """Aggregate tokens, tool error rate, slowest tools, and stop reasons across sessions."""
    from .core.observability import summarize

    cfg = _cfg(None, None, workspace)
    root = Path(dir) if dir else Path(cfg.session_dir)
    paths = sorted(root.glob("*.jsonl"), key=lambda x: -x.stat().st_mtime)[:limit] if root.exists() else []
    cutoff = _parse_since(since)
    stats = summarize(paths, model_prices=cfg.model_prices if cost else None, since=cutoff)
    if json_out:
        console.print_json(data=stats)
        return
    console.print(f"[bold]sessions[/] {stats['sessions']} · [bold]runs[/] {stats['runs']} · "
                  f"[bold]tokens[/] {stats['tokens']['total']:,} "
                  f"(mean/run {stats['tokens_per_run']['mean']:,}, max {stats['tokens_per_run']['max']:,})")
    tools = stats["tools"]
    console.print(f"[bold]tool calls[/] {tools['calls']} · [bold]errors[/] {tools['errors']} "
                  f"({tools['error_rate'] * 100:.1f}%) · [bold]compactions[/] {stats['compactions']} · "
                  f"[bold]resets[/] {stats['resets']}")
    if tools["slowest"]:
        table = Table("tool", "calls", "p50 s", "p95 s", "max s")
        for name, seconds, calls in tools["slowest"]:
            row = tools["by_tool"][name]
            table.add_row(name, str(calls), f"{row['p50']:.2f}", f"{row['p95']:.2f}", f"{seconds:.2f}")
        console.print(table)
    if stats["tokens_per_tool_call"]:
        console.print(f"[bold]tokens / tool call[/] {stats['tokens_per_tool_call']:,.1f}")
    if stats["stop_reasons"]:
        console.print("[bold]stop reasons[/] " + ", ".join(f"{k}={v}" for k, v in stats["stop_reasons"].items()))
    if cost:
        money = stats["cost"]
        console.print(f"[bold]cost[/] ${money['usd']:.4f} total · ${money['per_run']:.4f}/run"
                      + ("" if money["priced"] else " [yellow](no \\[pricing] table configured)[/]"))


@app.command()
def doctor(model: Optional[str] = typer.Option(None, "--model"), workspace: Optional[str] = typer.Option(None, "--workspace")) -> None:
    """Verify provider, models, tools, and retrieval end to end."""
    cfg = _cfg(model, None, workspace)
    asyncio.run(_doctor(cfg))


async def _doctor(cfg) -> None:
    table = Table("check", "result")
    table.add_row("provider", f"{cfg.provider.base_url}  model={cfg.provider.model}")
    table.add_row("api key", "set" if cfg.provider.api_key else "[red]MISSING[/]")
    table.add_row("context window", f"{cfg.models.context_window:,}")
    try:
        async with LLMClient(cfg) as llm:
            t0 = time.monotonic()
            resp = await llm.chat([__import__("argo.core.types", fromlist=["Message"]).Message(
                role="user", content="Reply with the single word: OK")], max_tokens=200)
            table.add_row("chat", f"ok ({(time.monotonic() - t0):.1f}s): {str(resp.message.content)[:40]!r}")
            try:
                vec = await llm.embed(["hello"])
                table.add_row("embeddings", f"ok dim={len(vec[0])} model={cfg.models.embedding_model}")
            except Exception as exc:  # noqa: BLE001
                table.add_row("embeddings", f"[yellow]{type(exc).__name__}: {exc}[/]")
            try:
                rr = await llm.rerank("memory", ["cats", "agent memory"], top_n=2)
                table.add_row("rerank", f"ok {rr}")
            except Exception as exc:  # noqa: BLE001
                table.add_row("rerank", f"[yellow]{type(exc).__name__}[/]")
            try:
                info = await llm.models()
                table.add_row("models", f"{len(info)} available")
            except Exception as exc:  # noqa: BLE001
                table.add_row("models", f"[yellow]{exc}[/]")
    except Exception as exc:  # noqa: BLE001
        table.add_row("chat", f"[red]{type(exc).__name__}: {exc}[/]")
    import shutil
    for binary in ("python3", "node", "bash", "git", "curl"):
        table.add_row(f"bin:{binary}", shutil.which(binary) or "[yellow]missing[/]")
    try:
        import playwright  # noqa: F401
        table.add_row("playwright", "installed")
    except ImportError:
        table.add_row("playwright", "[dim]not installed (optional: computer use)[/]")
    if getattr(cfg, "mcp_servers", None):
        from .tools.mcp import MCPClient
        client = await MCPClient(cfg.mcp_servers).connect()
        try:
            if client.errors:
                table.add_row("mcp", f"[yellow]{client.errors}[/]")
            else:
                table.add_row("mcp", f"ok {len(client.sessions)} server(s), {len(client.tools)} tool(s)")
        finally:
            await client.close()
    else:
        table.add_row("mcp", "[dim]no servers configured[/]")
    table.add_row("memory db", cfg.memory_db.__str__())
    table.add_row("search provider", cfg.search.provider)
    console.print(table)


@app.command("version")
def version() -> None:
    console.print("argo 0.1.0")




@app.command("goals")
def goals_cmd(limit: int = typer.Option(30, "--limit"),
              workspace: Optional[str] = typer.Option(None, "--workspace")) -> None:
    """List the durable goal queue."""
    from .daemon import GoalQueue
    cfg = _cfg(None, None, workspace)
    q = GoalQueue(Path(cfg.data_dir) / "daemon.sqlite")
    table = Table("id", "status", "source", "created", "goal")
    for g in q.list_goals(limit):
        table.add_row(g["id"][:10], g["status"], g["source"],
                      time.strftime("%m-%d %H:%M", time.localtime(g["created_at"])), (g["goal"] or "")[:70])
    console.print(table)


@app.command("enqueue")
def enqueue_cmd(goal: str = typer.Argument(...), priority: int = typer.Option(5, "--priority"),
                workspace: Optional[str] = typer.Option(None, "--workspace")) -> None:
    """Add a goal to the always-on queue."""
    from .daemon import GoalQueue
    cfg = _cfg(None, None, workspace)
    q = GoalQueue(Path(cfg.data_dir) / "daemon.sqlite")
    gid = q.enqueue(goal, source="cli", priority=priority)
    console.print(f"queued {gid}" if gid else "duplicate pending goal; not queued")


@app.command("outbox")
def outbox_cmd(workspace: Optional[str] = typer.Option(None, "--workspace")) -> None:
    """Show messages the always-on agent sent to you."""
    from .daemon import GoalQueue
    cfg = _cfg(None, None, workspace)
    q = GoalQueue(Path(cfg.data_dir) / "daemon.sqlite")
    for m in q.outbox(status="pending", limit=100):
        console.print(Panel(m["message"], title=f"{m['channel']} · {m['id']}"))
        q.mark_sent(m["id"])


approvals_app = typer.Typer(help="Review actions the always-on agent blocked.")
app.add_typer(approvals_app, name="approvals")


@approvals_app.command("list")
def approvals_list(workspace: Optional[str] = typer.Option(None, "--workspace")) -> None:
    from .daemon import GoalQueue
    cfg = _cfg(None, None, workspace)
    q = GoalQueue(Path(cfg.data_dir) / "daemon.sqlite")
    for a in q.pending_approvals():
        console.print(f"[yellow]{a['id']}[/] {a['tool']} — {a['reason']}\n  args: {a['args'][:300]}")


@approvals_app.command("decide")
def approvals_decide(approval_id: str = typer.Argument(...), approve: bool = typer.Option(True, "--approve/--deny"),
                     workspace: Optional[str] = typer.Option(None, "--workspace")) -> None:
    from .daemon import GoalQueue
    cfg = _cfg(None, None, workspace)
    q = GoalQueue(Path(cfg.data_dir) / "daemon.sqlite")
    q.decide(approval_id, approve)
    console.print(f"{approval_id} → {'approved' if approve else 'denied'}")


trigger_app = typer.Typer(help="Manage always-on triggers (schedule/webhook).")
app.add_typer(trigger_app, name="trigger")


@trigger_app.command("add")
def trigger_add(name: str = typer.Argument(...), goal: str = typer.Argument(...),
                kind: str = typer.Option("interval", "--kind", help="interval | daily | weekly | webhook"),
                seconds: int = typer.Option(3600, "--seconds"), at: str = typer.Option("09:00", "--at"),
                read_only: bool = typer.Option(False, "--read-only", help="Restrict the run to read-only tools."),
                workspace: Optional[str] = typer.Option(None, "--workspace")) -> None:
    """Schedule a recurring task or register a webhook name."""
    from .daemon import GoalQueue
    cfg = _cfg(None, None, workspace)
    q = GoalQueue(Path(cfg.data_dir) / "daemon.sqlite")
    spec = {"seconds": seconds, "at": at, "read_only": read_only}
    tid = q.add_trigger(name, kind, spec, goal)
    console.print(f"trigger {name} ({kind}) → {tid}")


@trigger_app.command("list")
def trigger_list(workspace: Optional[str] = typer.Option(None, "--workspace")) -> None:
    from .daemon import GoalQueue
    cfg = _cfg(None, None, workspace)
    q = GoalQueue(Path(cfg.data_dir) / "daemon.sqlite")
    table = Table("name", "kind", "enabled", "next_run", "goal")
    for t in q.triggers():
        table.add_row(t["name"], t["kind"], str(bool(t["enabled"])),
                      time.strftime("%m-%d %H:%M", time.localtime(t["next_run"])) if t["next_run"] else "-",
                      (t["goal"] or "")[:60])
    console.print(table)


@trigger_app.command("run")
def trigger_run(name: str = typer.Argument(...), workspace: Optional[str] = typer.Option(None, "--workspace")) -> None:
    """Enqueue a trigger's goal now (does not change its schedule)."""
    from .daemon import GoalQueue
    cfg = _cfg(None, None, workspace)
    q = GoalQueue(Path(cfg.data_dir) / "daemon.sqlite")
    tr = q.webhook_trigger(name)
    if not tr:
        err_console.print(f"[red]trigger {name!r} not found[/]")
        raise typer.Exit(1)
    gid = q.enqueue(tr["goal"], source=f"manual:{name}", trigger_id=tr["id"],
                    dedupe_key=f"{tr['id']}:{int(time.time())}")
    console.print(f"queued {gid}" if gid else "duplicate pending goal; not queued")


@trigger_app.command("rm")
def trigger_rm(name: str = typer.Argument(...), workspace: Optional[str] = typer.Option(None, "--workspace")) -> None:
    from .daemon import GoalQueue
    cfg = _cfg(None, None, workspace)
    q = GoalQueue(Path(cfg.data_dir) / "daemon.sqlite")
    console.print("removed" if q.remove_trigger(name) else "not found")


def main() -> None:
    app()


if __name__ == "__main__":
    main()


def _parse_since(value: Optional[str]) -> Optional[float]:
    """Parse '90m' / '24h' / '7d' / 'YYYY-MM-DD' into an epoch cutoff."""
    if not value:
        return None
    import time as _time
    value = value.strip().lower()
    units = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    if value[-1:] in units and value[:-1].replace(".", "", 1).isdigit():
        return _time.time() - float(value[:-1]) * units[value[-1]]
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
        try:
            return _time.mktime(_time.strptime(value, fmt))
        except ValueError:
            continue
    raise typer.BadParameter(f"cannot parse --since {value!r} (use 90m, 24h, 7d, or YYYY-MM-DD)")
