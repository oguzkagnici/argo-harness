# Reference review: leaked Claude Code source tree

Reference: `https://github.com/codeaashu/claude-code` (repository claims to contain leaked Claude Code source,
~1,900 files / 512k lines of TypeScript, Bun + React/Ink). **This is unverified leaked proprietary code.** We
read only the repository's documentation (`README.md`, `docs/architecture.md`, `docs/tools.md`,
`docs/subsystems.md`) to extract architectural patterns. No code was copied, and Argo remains an independent
Python implementation.

## 1. What the reference describes

- A terminal-native CLI built as a pipeline: CLI → query engine → model API → tool loop → terminal UI.
- A very large `QueryEngine` (~46k lines) handling streaming, tool-call loops, thinking budgets, retries, token
  accounting, and context management.
- ~40 tools, each self-contained with an input schema, permission model, execution logic, UI rendering, and
  concurrency-safety flag.
- ~85 slash commands split into prompt commands, local commands, and JSX-rendering commands.
- Subsystems: MCP client **and** server, permission modes (`default`, `plan`, `bypassPermissions`, `auto`),
  plugin/skill systems, background task system, coordinator/multi-agent teams, memory, IDE bridge, voice,
  feature flags, and migrations.
- Extra tools/patterns: agent teams, worktree isolation, plan mode, background tasks, MCP resource browsing,
  LSP tool, remote triggers, scheduled cron, structured-output tool, user-question tool.

## 2. Patterns worth adopting (conceptually)

| Pattern | Why it matters | Argo status |
|---|---|---|
| Tool contract includes permissions + concurrency + prompt contribution | Keeps safety and model guidance next to the implementation | Partial: `read_only`, `parallel_safe`, allowlist, approvals; no per-tool prompt fragment |
| Explicit plan mode with batch approval | Prevents writes while planning; one approval for a plan | Missing |
| MCP client (and optionally server) | Immediately unlocks external tools/resources | Missing |
| Background task/agent system | Long jobs should not block the main loop | Partial: daemon goals, no in-session task manager |
| Worktree isolation for coding subagents | Parallel edits without collisions | Missing |
| LSP-backed navigation | Precise symbol/reference queries beat grep for code | Missing (grep/glob only) |
| Hooks/plugin lifecycle | Extensibility without forking the loop | Partial: skills + event bus |
| IDE/bridge protocol | Editor integration | Not planned yet |

## 3. Patterns we should not adopt

- The monolithic ~46k-line query engine; Argo's separated loop/context/session/tool modules are easier to test.
- Coupling tool implementations to a UI component model.
- Porting Argo to TypeScript/Bun/React/Ink: the stack is irrelevant to model quality, and the rewrite cost is
  large with no measured benefit.
- Any reuse of leaked source. The legal/ethical risk is unacceptable and the architecture is already documented.

## 4. Comparison summary

| Capability | Reference | Argo today | Decision |
|---|---|---|---|
| Core loop | Huge query engine | ReAct/CodeAct loop with loop guard | Keep Argo design |
| Permissions | 4 modes, per-tool hooks | ask/auto/deny + daemon approvals + allowlists | Add plan mode + per-tool prompt hooks |
| MCP | Client + server + resources | None | **Build MCP client next** |
| Subagents | Agent tool + teams + worktrees | Context-isolated subagents | Add worktree isolation |
| Tasks | Background task tools | Daemon goal queue | Add in-session background tasks |
| Skills | Skill system | Progressive-disclosure SKILL.md | Keep; add hooks later |
| Memory | Memory subsystem | Layered SQLite + dreaming | Keep; benchmark-driven |
| Web/computer | WebFetch/WebSearch, browser | Search/fetch + Playwright tools | Add deep-research evidence ledger |

## 5. Alternatives considered

1. **Port to TypeScript/Bun to mirror the reference.** Rejected: no performance evidence, large rewrite, loses
   Python's document/data ecosystem.
2. **Adopt MCP via the official Python SDK.** Accepted as the next ecosystem step; small surface area, testable
   with a local server.
3. **Worktree isolation via `git worktree`.** Accepted later; the CLI is already available and subagents already
   exist.
4. **Plan mode as a policy flag plus prompt/tool gating.** Accepted later; cheaper than a separate UI.
5. **LSP integration via `pygls`/`multilspy`.** Deferred; grep plus tree-like navigation covers most current
   tasks, and LSP adds process/lifecycle complexity.

## 6. Conclusion

The reference validates Argo's broad feature set and highlights four concrete gaps worth building: **MCP client,
worktree-isolated subagents, plan mode with batch approval, and in-session background tasks**. None require
changing Argo's language or copying source. Deep research remains a separate capability track documented in
[`ROADMAP.md`](ROADMAP.md).

## 7. Follow-up: `codeaashu/claude-code` (checked 2026-11-24)

The requested repository is the same leaked Claude Code tree reviewed above (same `package.json`, same
`src/QueryEngine.ts` / `src/query.ts` layout), republished with added docs. No new architecture was found; the
useful additions were concrete, previously unseen constants and one policy, which we mined for evidence and then
implemented and tested in Argo:

- `AUTOCOMPACT_BUFFER_TOKENS = 13_000` — compaction fires at `effective_window − 13k`, not at a flat percentage.
- `MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3` — circuit breaker after repeated summary failures; their telemetry
  cites sessions with thousands of consecutive failed compaction calls.
- Time-based microcompaction: after a 60-minute gap (server cache TTL), clear all but the 5 newest compactable
  tool results before the next request.
- Ordering: microcompaction runs before autocompaction, and only a fixed set of tools is compactable.

Argo now implements all four (`argo/config.py`, `argo/core/context.py`) with unit tests and the
`benchmarks/context_bench.py` ablation. We did not copy code, prompts, or file structure.
