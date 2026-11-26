# Implementation status

Snapshot: 2026-11-26. Offline suite: **179 tests passing** (`.venv/bin/python -m pytest -q`).
Live numbers live in [`benchmarks/README.md`](../benchmarks/README.md); rationale in
[`DECISIONS.md`](DECISIONS.md). "Done" means implemented *and* tested; "measured" means we have a live number.

## Core harness

| Area | Status | Evidence |
|---|---|---|
| ReAct control loop + typed tools + code execution | Done | HumanEval 20/20 pass@1 |
| OpenAI-compatible endpoints, BYOK, chat + responses wire APIs | Done | `tests/test_responses_api.py`, provider parity runs |
| Context: microcompact → structured compaction → reset + handoff + fact ledger | Measured | live 36-step: autocompact 3/3 @275,722 tok, 0 resets |
| Loop guard (no-progress) incl. novel shell output | Done + measured | `tests/test_agent_loop.py`, `tests/test_loop_shell_progress.py`, SWE-bench |
| Subagents (isolated worktrees, review + in-place repair) | Done | `tests/test_worktree.py`, `tests/test_review.py` |
| Plan mode + plan persistence across sessions | Done | `tests/test_plan_mode.py` (5 tests) |
| Retrieval: BM25 + dense + weighted RRF + cross-encoder rerank | Measured on 2 corpora | SciFact + NFCorpus |
| Memory: dream consolidation, wiki extraction | Measured + scaled | 6-lesson + 24-lesson/5-session runs; 2 generations rule |
| Research: evidence ledger + citation verifier | Done | `benchmarks/citation.py`, `tests/test_research.py` |
| Deep research (exact-answer, cached pages) | Measured | `benchmarks/deep_research.py` (R4): 6/6 base, 4/4 3-hop |
| Research coverage plan (enforced decomposition) | Measured, opt-in | `argo/research/plan.py`, `tests/test_coverage_plan.py` |
| Per-turn model routing (`--router explore`) | Measured, opt-in | `argo/core/router.py`, `tests/test_router.py` |
| Documents/slides (DOCX/PPTX) | Done | document QA benchmark |
| Computer use: browser tool (click/type/select), self-healing selectors, workflow replay | Measured | 3-task subset **3/3**, unsafe rate 0.0; replay drift 1/2 → 4/4 |

## Always-on runtime

| Area | Status | Evidence |
|---|---|---|
| Durable goal queue, triggers, outbox, approvals | Done | `tests/test_daemon.py` |
| Multi-worker leases (heartbeat, atomic claim, stale recovery) | Done + tested | 20-goal 2-worker race, crash recovery |
| Per-run budgets (tokens/wall/tool-calls) + priced cost | Done + tested | `tests/test_budget.py`, `argo metrics --cost` |
| Dream cycle scheduling | Done | daemon `_dream` |

## Safety / robustness

| Area | Status | Evidence |
|---|---|---|
| Fixed prompt-injection suite | Measured | ASR 0.0, utility 1.0 |
| Adaptive injection (AgentDojo-style) | Measured | static 0/3, adaptive 0/6, defenses +9% tokens. Harder payloads: aligned models 0/3; **naive `gemma-4-31b` ASR 1.0 → 0.0 with defenses** (first measured guard win) |
| Provider self-contention guard (concurrency cap, backoff, pacing) | Done + tested | `tests/test_provider_limits.py` |

## Evals

| Benchmark | Status | Headline |
|---|---|---|
| HumanEval subset | Measured | 20/20 pass@1 |
| BEIR SciFact | Measured | rerank nDCG 0.9155 |
| BEIR NFCorpus | Measured | rerank nDCG 0.4451 (dense 0.3936, BM25 0.3486) |
| Memory learning loop | Measured | see README §3 |
| SWE-bench-Lite | Measured | **6/6 resolved** across sympy, flask, xarray |
| Document QA / citation / repair / workflow replay | Measured | see README |
| Long-horizon context (live) | Measured | 3/3 completion |
| Observability (trace/metrics/p50/p95/--since/--cost) | Done | `tests/test_observability.py` |
| Served live trace view (`/trace`, `/v1/sessions`) | Done + tested | `tests/test_server_trace.py` |

## Still open

- Add harder repos (django/astropy) and a larger sample before quoting a Lite-wide rate.
- Memory cadence/wiki ablations at 40–60 lessons (24-lesson scale test + two-generation rule done).
- Summary-prompt A/B: `identifiers` wins at 18 steps (3/3, 1 vs 2 compactions); confirm at 36 steps.
- Repair loop: trap tasks on two models — both generalize 2/2, so review/repair still show no resolution gain; needs harder (multi-file/real-repo) tasks.
- Deep research: larger/noisier corpus and multi-source synthesis beyond 2 hops.
- Computer use: WebArena/OSWorld-scale subset; current evidence is a 3-task local site (login/decoy-table/form) with an unsafe-action rate.
- MCP: per-tool approval policy (per-server `read_only` and the served trace view are done).
- Multi-model router: per-turn `explore` routing done and measured; a review-only cheap subagent remains.
- Statistical rigor: multiple seeds / confidence intervals; weaker target model for injection.
