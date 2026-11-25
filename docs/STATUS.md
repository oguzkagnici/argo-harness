# Implementation status

Snapshot: 2026-11-25. Offline suite: **136 tests passing** (`.venv/bin/python -m pytest -q`).
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
| Memory: dream consolidation, wiki extraction | Measured (small scale) | `memory_loop.py` |
| Research: evidence ledger + citation verifier | Done | `benchmarks/citation.py`, `tests/test_research.py` |
| Documents/slides (DOCX/PPTX) | Done | document QA benchmark |

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
| Adaptive injection (AgentDojo-style) | Measured | static 0/3, adaptive 0/6, defenses +9% tokens (ceiling) |
| Provider self-contention guard (concurrency cap, backoff, pacing) | Done + tested | `tests/test_provider_limits.py` |

## Evals

| Benchmark | Status | Headline |
|---|---|---|
| HumanEval subset | Measured | 20/20 pass@1 |
| BEIR SciFact | Measured | rerank nDCG 0.9155 |
| BEIR NFCorpus | Measured | rerank nDCG 0.4451 (dense 0.3936, BM25 0.3486) |
| Memory learning loop | Measured | see README §3 |
| SWE-bench-Lite | Measured (n=3 attempts) | 16988 **resolved** with test feedback; 2 more graded |
| Document QA / citation / repair / workflow replay | Measured | see README |
| Long-horizon context (live) | Measured | 3/3 completion |
| Observability (trace/metrics/p50/p95/--since/--cost) | Done | `tests/test_observability.py` |

## Still open

- Grade 2–5 SWE-bench instances and publish a pass rate (n=1 resolved so far).
- Memory scale test: 40–60 lessons, dream cadence, recall/cost rule.
- Summary-prompt A/B ("keep raw identifiers" vs current).
- Live measurement of the repair loop (does in-place repair improve resolution/token?).
- Deep research R3/R4: report-level citation precision/recall and a GAIA/BrowseComp-style subset.
- Computer-use subset (WebArena/OSWorld) and unsafe-action rate.
- MCP prompts + per-server approval policy; OTel export; served live trace view.
- Multi-model router: per-turn exploration/final routing and review-only cheap subagent (compaction routing done).
- Statistical rigor: multiple seeds / confidence intervals; weaker target model for injection.
