# Architecture

This document explains *why* Argo is built this way. Design decisions are grouped by the problem they solve,
with the evidence that motivated them. See `docs/RESEARCH.md` for the full landscape and citations.

## 1. Design goals

1. **General, not coding-only.** Documents, research, data analysis, browser automation, and code share one loop.
2. **Provider-agnostic.** Any OpenAI-compatible `/chat/completions` endpoint, bring-your-own-key, optional
   `/embeddings` and `/rerank`.
3. **Long-horizon capable.** Compaction, resets, durable plans, memory, and subagents instead of one giant context.
4. **Verifiable.** The agent executes and checks; high-stakes artifacts can be audited by an independent evaluator.
5. **Inspectable.** JSONL traces, SQLite memory, markdown skills, declarative workflows — no hidden prompt magic.
6. **Safe by default.** Workspace write policy, dangerous-action approval, unattended approval queue, secret redaction.

Non-goals: model training, multi-tenant SaaS hardening, a full cron implementation, or a marketplace.

---

## 2. The agent loop (`argo/core/agent.py`)

```
messages = [system, user]
repeat up to max_iterations:
    manage context (microcompact → compact → reset)
    response = model(messages, tools)
    if response.tool_calls:
        execute (parallel if all read-only & parallel-safe)
        append tool results (+ screenshots as user image messages)
    else:
        finish
```

**Why ReAct-style interleaving?** ReAct (Yao et al., 2022) showed that alternating reasoning traces with
actions/observations outperforms either pure chain-of-thought or pure acting on knowledge-intensive and
interactive tasks. Argo keeps reasoning in the assistant message (providers expose it as `reasoning` /
`reasoning_content`) and treats every tool result as an observation.

**Why code execution as a first-class action?** CodeAct (Wang et al., 2024) found executable code actions
outperform JSON/text actions for many agent tasks, because one code block can express control flow and
composition that otherwise takes many tool calls. `run_code` and `bash` are therefore first-class, alongside
typed tools for safety and discoverability.

**Parallel tools.** Multiple calls in one assistant turn are executed concurrently only when every call is
declared read-only and parallel-safe. Mutating calls stay sequential to preserve ordering and approval semantics.

**Stall control.** Models can fall into verify-only loops (observed: nine repeated `bash` unzips of a DOCX while
the PPTX was never built). Argo fingerprints every tool name+arguments, blocks the third identical read-only call,
and counts turns with no progress-producing tool (writes, document creation, memory updates, plans, subagents,
workflow runs). At 40% and 70% of the limit it injects a nudge; at the limit it stops and forces a no-tools status
report. This follows Reflexion's insight that failed trajectories need explicit verbal correction, plus the
long-running-harness guidance to bound verification and reset when stuck. Covered by `tests/test_agent_loop.py`.

**Finish behavior.** If the model returns an empty message with `finish_reason=length`, Argo asks it to
continue rather than silently ending. On step exhaustion it requests a no-tools status report, so the user
always gets state + next steps.

**Usage accounting.** Provider usage is authoritative and is recorded per run/session/day. Reasoning tokens
are tracked separately (they consume `max_tokens` on many providers).

---

## 3. Context engineering (`argo/core/context.py`)

Frontier harnesses converge on the same insight: an agent's performance is dominated by *what is in the
window*, not merely by model quality (Anthropic, "Effective context engineering for AI agents", 2025).

Argo uses three escalating mechanisms:

| Level | Trigger | What happens | Cost / benefit |
|---|---|---|---|
| **Microcompaction** | token estimate > 75% of budget | old large tool outputs are replaced by head/tail + spool-file pointer; old reasoning dropped | cheap; preserves recency |
| **Structured compaction** | still over threshold | the oldest span is summarized into a `STATE` block: goal, progress, findings, files, open issues, next steps | one LLM call; preserves continuity, same agent |
| **Context reset** | still > 92% | write a self-contained handoff artifact, start a fresh window with the handoff + last few messages | clean slate; fixes "context anxiety" |

This ordering follows Anthropic's harness work ("Harness design for long-running application development",
2026), which found that compaction preserves continuity but can leave models in a "context-anxious" state,
while resets require a high-quality handoff artifact. Argo does compaction first (cheaper) and resets only
when necessary.

**Prompt-cache alignment.** The system prompt and tool schemas are byte-stable at the front of the request and
the conversation is append-only until compaction. DeepSeek Harness reports large savings from prefix-cache
alignment (`deepseekharness.dev`); the same structure benefits any provider with automatic prefix caching.
Compaction intentionally invalidates the cache once, then stabilizes again.

**Token estimation.** Provider-reported prompt tokens are authoritative; the local estimator is only used for
budget decisions. `argo/core/tokens.py` handles mixed Latin/CJK text.

**What survives compaction.** Durable state lives in three places that are re-injected, not in the transcript:
the plan (`update_plan` → session), memory (`remember`/dreaming), and files/artifacts. The system prompt tells
the model this explicitly.

**Lost-in-the-middle.** Retrieved context and memory are placed near the system prompt, and the current
request at the end, keeping critical instructions at the window edges (Liu et al., 2023).

---

## 4. Memory (`argo/memory/`)

### 4.1 Layers

Anthropic's "components of agent memory" (Nov 2026) describes a hierarchy: **transcripts → memory notes →
memory representations (wikis) → injected context**, with dreaming operating across the layers. Argo implements
exactly that:

| Layer | Purpose | Example |
|---|---|---|
| `transcript` | searchable event log, ground truth for verification | user/assistant/tool events |
| `note` | atomic salient observations | "Staging moved to db2.internal" |
| `profile` | durable user/environment facts | "Prefers uv over pip" |
| `wiki` | consolidated topic pages with links | "Deployment: migrate before start" |
| `procedure` | learned reusable procedures | steps for a deploy/export flow |
| `injected` | operator-curated standing context | "Never touch the production DB" |

Salience follows Anthropic's Pokémon finding: store *strategy, plans, tested rules, and preferences*; do not
store state that is visible on screen or derivable at runtime. The system prompt encodes this rule.

### 4.2 Generational writes (dreaming)

A dream reads generation **G** and writes generation **G+1**. The input is never mutated, so the output can be
reviewed, discarded, or rolled back (`argo memory activate N`). This mirrors the Dreams API's clone semantics.

```
active G ──read──► dream(transcripts + notes + instructions)
                     │  extract observations / profile / insights / wiki / procedures
                     │  reconcile: merge | supersede | archive (with reasons)
                     │  ground every claim against transcripts (evidence quotes)
                     ▼
                  new G+1 ──review──► activate (or roll back)
```

**Evidence grounding.** Each factual claim carries a verbatim `evidence` quote. If the quote is not found in
the transcript corpus, confidence is capped at 0.45; claims with no evidence at all are capped at 0.5. This is a
direct answer to the failure mode Anthropic describes: a stale or wrong note can be read and trusted by a later
session, and dreaming fixes it by testing notes against the original transcript.

**Deduplication & decay.** After writing, near-duplicate active items in the same layer are archived (exact
normalized match or cosine > 0.97), keeping the higher-value copy. Low-value stale items can be archived by
reconciliation actions. Memory is intentionally small and current.

**Repository-style versioning.** Dream generations provide immutable history. `argo memory export --git`
produces Instinct-style git-tracked markdown with IDs, frontmatter, `[[links]]`, and a bounded `_dashboard.md`.

### 4.3 Retrieval scoring

Memory retrieval uses the same hybrid pipeline as file retrieval, plus a quality boost:

```
score = RRF(lexical, dense) + 0.6 · cross_encoder + 0.05·importance + 0.04·recency + 0.02·usage
```

The recency/importance/relevance combination follows Generative Agents (Park et al., 2023), whose retrieval
function weights all three. Access counts increase on use, giving frequently useful memories a small edge.

### 4.4 Why both SQLite and markdown?

SQLite is the operational store (transactions, generations, embeddings, access counts). Markdown export is the
human- and git-facing view, following Instinct's reverse-engineered design (git-tracked markdown, aliases,
links, reconciliation commits). The database is not an opaque vector store: everything can be exported, diffed,
and reviewed.

---

## 5. Retrieval (`argo/memory/retrieval.py`)

1. **Chunking.** Heading-aware, ~1,800 chars with overlap, preserving line ranges for citations. Long tool
   outputs and documents are chunked; source refs are retained.
2. **Lexical.** BM25 (Robertson & Zaragoza, 2009) over tokenized text (stopword-filtered, CJK-aware).
3. **Dense.** Embeddings via the provider's `/embeddings` endpoint if configured; cached by content hash.
4. **Fusion.** Reciprocal Rank Fusion (Cormack et al., 2009), `k=60`, weighted toward dense. RRF avoids mixing
   incomparable raw scores and is robust to one retriever failing.
5. **Rerank.** Cross-encoder `/rerank` over the fused top-N, if available.
6. **Agentic search.** The model also has `grep_files`/`glob_files`/`read_file`, because lexical search of an
   exact identifier is often better than embeddings — the same reason Claude Code and Codex expose ripgrep.

If embeddings/rerank are unavailable, the pipeline degrades gracefully to BM25 (and still returns citations).

---

## 6. Tools and policy (`argo/tools/`)

**Registry.** Every tool is a dataclass with a JSON schema, read-only/parallel flags, timeout, output cap, and
category. The registry validates names and arguments, enforces allowlists (subagents), applies approvals, and
converts exceptions into tool results — a tool failure is data, never a crashed loop.

**Output spilling.** Large outputs are truncated and the full text is written to `.argo/spool/` with a pointer
and a byte count. This is the Codex-style pattern that keeps tool results bounded without losing evidence.

**Path policy.** Reads are workspace-relative; writes outside the workspace require explicit policy. This is a
deliberate simplification of Codex's sandbox model; see limitations.

**Danger guard.** Shell/code is scanned for destructive patterns (`rm -rf /`, remote-pipe-to-shell, `sudo`,
force push, destructive SQL, fork bombs, `mkfs`). In interactive CLI it prompts; in the daemon it blocks and
queues an approval event. This mirrors Claude Code/Codex auto-mode plus Anthropic's always-on permissions
guidance (classifier/approval for risky actions, credential proxying outside the agent).

**Secrets.** Events redact key-like strings; tool arguments are truncated before logging. API keys are never
included in prompts, memory, or transcripts.

**MCP (`argo/tools/mcp.py`).** Configured servers are launched over stdio with the official Python SDK, initialized
once per top-level agent, and their tools are registered as `mcp__<server>__<tool>`. Schemas come straight from
the server (`input_schema`), so the model sees real parameters; tools are treated as mutating unless the server is
declared `read_only` (or listed in its `read_only_tools` map), which keeps them out of parallel batches and
subject to approvals. A server that fails to
start is recorded and skipped rather than failing the run. Subagents do not start their own MCP servers; the
integration is deliberately top-level only until per-subagent scoping is designed.

---

## 7. Subagents and the evaluator (`argo/core/agent.py`)

**Why isolated subagents?** Context is the scarce resource. A research branch or file investigation can consume
tens of thousands of tokens; returning a compact distillation to the parent preserves the parent's window. This
is the same motivation as Claude Code subagents and Codex's delegation.

**Why a separate evaluator?** Anthropic's long-running harness work found that generator agents are poor
self-evaluators, and that a generator/evaluator split with explicit criteria and tool access (Playwright for
visual checks) materially improved output quality. Argo exposes `review_work(criteria, paths)`: it spawns a
read-only reviewer with a skeptical prompt, which inspects actual artifacts and returns PASS/FAIL, per-criterion
scores, concrete defects, and required fixes. The system prompt instructs the producer to use it for decks,
reports, and production code.

**Worktree isolation.** A delegating agent can pass `isolate=true`; the subagent then runs with `workspace`
pointed at a linked git worktree on a fresh `argo/<slug>-<id>` branch under `<data_dir>/worktrees/`. The child
edits only that checkout; the result reports the branch and `changed_files` (uncommitted and committed), and the
parent decides whether to merge. Adoption is never automatic. Non-git workspaces are rejected with a clear error.

**Plan mode.** With `plan_mode` (CLI `--plan`), the registry blocks every mutating tool until `propose_plan`
returns approval. One approval covers the batch: subsequent mutating calls are still passed to the approval
callback with the reason "covered by approved plan", so danger checks remain active. In non-interactive runs the
approval hook refuses unless explicitly auto-approved (`--yolo`).

**Background tasks.** `start_background(task)` runs a subagent as an asyncio task and returns a job id;
`check_background(id, wait_seconds=30)` blocks up to the timeout and returns status/result, and
`list_background` enumerates jobs. Jobs are in-memory and cancelled when the parent closes; durable work belongs
in the daemon queue. Blocking checks replaced polling and cut a live run from 71k to 19k tokens.

**Planner.** `update_plan` is a durable checklist (one `in_progress` step), stored in the session and re-injected
through the plan/state summary. This is the planner role, lightweight enough to run in-loop; subagents provide
the generator/evaluator split.

---

## 8. Workflows (`argo/workflows/`)

A successful trace is messy: exploratory reads, failed commands, retries. The extraction pipeline:

```
session.jsonl tool events
   │  filter to tool calls + outputs (last N)
   ▼
LLM synthesizer (constrained to the real tool catalog)
   │  lift literals into params · drop dead ends · add success assertions
   ▼
.argo/workflows/<name>.json     +     .argo/skills/<name>/SKILL.md
   │
   ▼
replay via the same ToolRegistry (policy, truncation, approvals still apply)
```

**Why declarative JSON, not generated Python?** It is inspectable, diffable, permission-checked, and replayable
through the same executor. Generated code would bypass tool policy and be harder to verify. It also matches the
harness pattern of structured tool actions (Codex patches, Claude Code tool calls) rather than arbitrary replay.

**Expectations and retries** make replays verifiable: `expect_contains`, `expect_exit_zero`, `retries`,
`on_error`. Variables (`save_as`) allow later steps to consume earlier outputs.

**Computer-use extraction** (see `docs/COMPUTER_USE.md`) turns a browser session into a parameterized workflow
(e.g. `url` + `name`), then replays it with new inputs. Grok Bot's advertised "learn a routine by watching you
do it once" is the same product idea; Argo's version is transparent and inspectable.

**Skills.** Each workflow is also exported as a `SKILL.md` with YAML frontmatter, so future sessions discover it
via the skill index and load it on demand — progressive disclosure, as in Claude Code/Codex skills.

---

## 9. Always-on mode (`argo/daemon.py`)

Anthropic's "Building always-on agents" (Nov 2026) defines five primitives: **event inbox, timers, memory,
permissions, outbox**. Argo implements all five:

| Primitive | Argo implementation |
|---|---|
| Event inbox | SQLite `goals` table; webhook/schedule/CLI enqueue; dedupe key; one worker claims an item atomically |
| Timers | `triggers` table (`interval`, `daily`, `weekly`, `once`), scheduler poll, plus agent-set `schedule_followup` |
| Memory | layered SQLite memory + dreaming; durable across runs and restarts |
| Permissions | danger guard + approval queue + read-only trigger allowlists; tool registry policy |
| Outbox | `outbox` table; `notify` tool; `argo outbox` dispatcher; final result enqueued automatically |

**Crash safety.** Goals survive process restarts; a claimed goal is `running` and can be reconciled. Trigger
dedupe prevents storms (a webhook burst does not fan out into duplicate runs). The daemon is a single worker
by default, matching Anthropic's "one active worker per inbox" guidance.

**Proactive research.** A trigger can be marked `--read-only`, which restricts the run to read-only tools
(`web_search`, `fetch_url`, `read_*`, `recall`, …). This mirrors OpenAI Dots' read-only background research.

**Approvals.** Risky actions are blocked and written to `approvals` + the outbox, with the tool/args/reason.
The operator can inspect (`argo approvals list`) and decide. (Resuming the exact deferred call is a roadmap item;
the current behavior is fail-safe denial with a resumable goal path.)

**Scheduled dreaming.** The daemon runs a dream cycle every `--dream-hours` (default 24h) with `apply=True`,
so long-running agents consolidate between runs without a human in the loop.

**How it differs from a regular harness:** a regular harness is a synchronous request/response turn driven by a
human. Always-on agents are event-driven, stateful across runs, proactive, budgeted, permission-gated, and
delivered through an outbox rather than a streaming chat window. The agent loop is the same; the surrounding
architecture is different.

---

## 10. HTTP API (`argo/server.py`)

- `POST /v1/chat/completions` — OpenAI-compatible. Converts the full message list into a task with
  `<conversation_so_far>` context, runs the agent, returns the final message. Streaming emits standard SSE
  chunks plus keepalives during long tool phases (so proxies do not time out).
- `POST /v1/agent` — native task API with SSE events (`tool_call`, `usage`, `run_end`, `result`).
- `/v1/models`, `/v1/embeddings`, `/v1/rerank` — gateway passthrough.
- `/v1/memory/*`, `/v1/dream`, `/hooks/{name}`, `/v1/goals`, `/v1/approvals`, `/v1/triggers`.
- Optional bearer auth; a semaphore bounds concurrent runs.

The design goal is drop-in compatibility: existing OpenAI clients get an agent with tools and memory without
changing their SDK usage.

---

## 11. Provider compatibility

- Chat: `POST /chat/completions` with `tools`, `tool_choice`, streaming, `response_format`, `reasoning_effort`.
- Reasoning fields: accepts `reasoning` or `reasoning_content`.
- Embeddings: `POST /embeddings`, batched, cached by content hash.
- Rerank: vLLM-style `POST /rerank` (`query`, `documents`, `top_n`) → `results[{index, relevance_score}]`.
- Retries: 429/5xx with exponential backoff and `Retry-After`, transport retries.
- Tool-call streaming: arguments are assembled per index; IDs are synthesized if a provider omits them.
- Both `wire_api` modes are implemented: `/chat/completions` and `/responses`. The client maps internal messages to Responses input items (`input_text`, `function_call`, `function_call_output`), flat tool schemas, and Responses SSE events.

Configuration precedence: CLI → env → `~/.argo/config.toml` → `~/.codex/config.toml`. The last step is why the
harness works with an existing Codex provider/key with no extra setup.

---

## 12. Failure modes and mitigations

| Failure | Mitigation |
|---|---|
| Context overflow | microcompact → compaction → reset with handoff |
| Context anxiety / premature wrap-up | resets with clean slate; explicit finish criteria in prompt |
| Stale/contradictory memory | dreaming with evidence grounding, supersede/merge, generational rollback |
| Memory bloat | salience rules, dedupe, importance/recency scoring, bounded injection |
| Hallucinated URLs/facts | search→fetch discipline, citations, review_work, "verified vs assumed" rule |
| Silent tool failure | structured errors, exit codes, verification steps, retries in workflows |
| Runaway cost | daily token budget, iteration cap, keepalive/streaming, daemon polling instead of busy loops |
| Risky unattended action | danger guard, approval queue, read-only triggers, workspace write policy |
| Browser selector drift | expectations + retries + re-extraction; DOM text alongside screenshots |
| Provider quirks | retries, streaming assembly, reasoning-field normalization, graceful degradation |

---

## 13. Extension points

- **New tool:** subclass/add a `Tool` in `argo/tools/*.py` and call `reg.add(...)`; it is instantly available to
  the model, subagents, and workflows.
- **New skill:** drop `.argo/skills/<name>/SKILL.md` with `name`/`description` frontmatter.
- **New provider:** set `base_url`/`model`/env key; implement `/embeddings` and `/rerank` if available.
- **New trigger:** add a kind to `GoalQueue._next_run` + `due_triggers`; the worker and outbox are unchanged.
- **New document format:** add a renderer in `argo/tools/docs.py` and a format enum entry.

---

## 14. Tradeoffs

- **SQLite over a vector DB:** simpler, transactional, portable, inspectable; cosine search is O(n) and fine for
  personal/team memories. Swap in FAISS/pgvector at scale.
- **Declarative workflows over generated code:** less expressive, much safer and more debuggable.
- **Single worker daemon:** no lease/HA; adequate for one user/team, not a fleet.
- **LLM-based compaction:** one extra call, but keeps continuity far better than truncation.
- **Generation cloning:** storage grows with each dream; pruning old generations is a simple `DELETE` away.
- **Optional heavy dependencies:** Playwright and embeddings/rerank are optional; core runs with HTTP only.
