# Implementation status

Snapshot: 2026-11-27. Offline suite: **202 tests passing** (`.venv/bin/python -m pytest -q`).
Live numbers live in [`benchmarks/README.md`](../benchmarks/README.md); rationale in
[`DECISIONS.md`](DECISIONS.md). "Done" means implemented *and* tested; "measured" means we have a live number.

## Core harness

| Area | Status | Evidence |
|---|---|---|
| ReAct control loop + typed tools + code execution | Done | HumanEval 20/20 pass@1 |
| Git tools (status/diff/log/show/branches + approval-gated commit) | Done | `tests/test_git_tools.py` (5, real temp repos) |
| OpenAI-compatible endpoints, BYOK, chat + responses wire APIs | Done | `tests/test_responses_api.py`, provider parity runs |
| Context: microcompact → structured compaction → reset + handoff + fact ledger | Measured | live 36-step: autocompact 3/3 @275,722 tok, 0 resets |
| Loop guard (no-progress) incl. novel shell output | Done + measured | `tests/test_agent_loop.py`, `tests/test_loop_shell_progress.py`, SWE-bench |
| Subagents (isolated worktrees, review + in-place repair + **branch adoption**) | Done + measured | `tests/test_worktree.py` (12), live `wtrepair` arm |
| Multi-file repair task (`ledger`, spec-driven, 3 rules + hidden suite) | Measured | baseline 1/1 (66k tok), review 1/1 (178k tok, PASS) — no headroom |
| Plan mode + plan persistence across sessions | Done | `tests/test_plan_mode.py` (5 tests) |
| Retrieval: BM25 + dense + weighted RRF + cross-encoder rerank | Measured on 2 corpora | SciFact + NFCorpus |
| Memory: dream consolidation, wiki extraction | Measured + scaled | 6/24/40-lesson runs; at 40 one pass = 1.00 correct, 2 passes 3.7× cost → default 1 pass (`--dream-generations` opt-in) |
| Research: evidence ledger + citation verifier | Done | `benchmarks/citation.py`, `tests/test_research.py` |
| Deep research (exact-answer, cached pages) | Measured | `benchmarks/deep_research.py` (R4): 6/6 base, 4/4 3-hop |
| Research coverage plan (enforced decomposition) | Measured, opt-in | `argo/research/plan.py`, `tests/test_coverage_plan.py` |
| Per-turn model routing (`--router explore`) | Measured, opt-in | `argo/core/router.py`, `tests/test_router.py` |
| Documents/slides (DOCX/PPTX) | Done | document QA benchmark |
| Computer use: browser tool (click/type/select), self-healing selectors, workflow replay | Measured | 3-task stateless subset **3/3**, unsafe rate 0.0; **WebArena-shaped session/pagination/state-checked subset 3/3** (357,785 tok); replay drift 1/2 → 4/4 |

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
| Adaptive injection (AgentDojo-style) | Measured | static 0/3, adaptive 0/6; harder payloads: aligned 0/3, **naive `gemma-4-31b` ASR 1.0 → 0.0 with defenses**; **multi-turn priming (2 turns) also 1.0 → 0.0**, utility 1.0 |
| Provider self-contention guard (concurrency cap, backoff, pacing) | Done + tested | `tests/test_provider_limits.py` |
| Repeated measurements (`--repeats`) | Done + measured | injection naive-target guard: ASR 1.0/1.0/1.0 off vs 0.0/0.0/0.0 on (n=18) |

## Evals

| Benchmark | Status | Headline |
|---|---|---|
| HumanEval subset | Measured | 20/20 pass@1 |
| BEIR SciFact | Measured | rerank nDCG 0.9155 |
| BEIR NFCorpus | Measured | rerank nDCG 0.4451 (dense 0.3936, BM25 0.3486) |
| Memory learning loop | Measured | see README §3 |
| SWE-bench-Lite | Measured | **10/11 gradable resolved** (official harness) across sympy, flask, xarray, django, pytest; gold control exposed one environment-invalid instance |
| Document QA / citation / repair / workflow replay | Measured | see README |
| Long-horizon context (live) | Measured | 3/3 completion |
| Observability (trace/metrics/p50/p95/--since/--cost) | Done | `tests/test_observability.py` |
| Served live trace view (`/trace`, `/v1/sessions`) | Done + tested | `tests/test_server_trace.py` |

## Still open

- SWE-bench: 12 instances attempted, 10/11 gradable resolved; requests needs gold-control + network-safe handling
  (two timeouts, one environment-invalid). Astropy blocked by `numpy==1.16.0`; a Lite-wide rate needs far more
  instances and multi-seed runs.
- Memory cadence/wiki ablations at 40–60 lessons (24-lesson scale test + two-generation rule done).
- Summary-prompt A/B: 36-step rerun done — both 3/3; `identifiers` −15.6% tokens but more compactions (5 vs 3).
- Repair loop: trap tasks (two models) and the new multi-file `ledger` task are all solved at baseline, so review/repair still show no resolution gain (review = 2.7× tokens at 1/1); needs real-repo tasks the baseline fails and multiple seeds.
- Deep research: larger/noisier corpus and multi-source synthesis beyond 2 hops.
- Computer use: WebArena-shaped local subset now covers sessions/pagination/POST/state grading (3/3); real
  WebArena/OSWorld apps and desktop control are still untested.
- Full published AgentDojo suite (other domains/templates); multi-turn priming is covered at the defense level only.
- Multi-model router: per-turn `explore` routing done and measured; a review-only cheap subagent remains.
- Statistical rigor: multiple seeds / confidence intervals; weaker target model for injection.
