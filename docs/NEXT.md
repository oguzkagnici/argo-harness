# What to implement next

Ordered by (a) what is currently *unproven or broken*, (b) how much evidence exists that the mechanism helps, and
(c) cost in rate limit/quota on this shared machine. Every item lists the success criterion so it can be closed with
a measurement, not an opinion.

Current state: 130 offline tests pass. Live evidence exists for retrieval, memory, repair, documents, citation,
injection (fixed + partial adaptive), context (synthetic + live), observability, worktree review, and the SWE-bench
Docker path (gold patch 1/1).

---

## P0 — close loops that are already open

### 1. Re-measure the live 36-step context run after the three fixes
**Why:** the pre-fix run is a *negative* result (compaction 0/3 vs no compaction 3/3) and contradicts the synthetic
benchmark (12/12). Three mechanism bugs were found and fixed: identifier capture in the fact ledger, reset gating,
and compaction hysteresis.
**Do:** `python benchmarks/context_live.py --steps 36 --modes full,autocompact,no_ledger` (12k window).
**Status:** done. `autocompact` **3/3** at 275,722 tok / 3 compactions / 0 resets vs unmanaged **3/3** at 288,569 tok;
`no_ledger` also **3/3** at 265,657 tok. The 30%-token-savings target was **not** met (~4-8% saved): with only 3-4
compactions, summarization cost roughly offsets the reclaimed context. That is an honest result — the policy is now
*not worse* than no compaction, which is the bar that matters.
**If it still fails:** the summary prompt, not the ledger, is the bottleneck — try "answer-probe-aware" compaction
(re-inject the user's exact question verbatim into the compacted block).

### 2. ~~Finish the adaptive-injection suite~~ — done
**Why:** the fixed suite is at ceiling (model refuses everything undefended); only one adaptive case has run.
**Do:** `python benchmarks/adaptive_injection.py --rounds 2 --modes off,on` (3 cases × 2 rounds × 2 modes).
**Pass:** documented ASR per round; if adaptive ASR > static ASR, add a targeted defense (e.g. strip instructions
from HTML comments, or require confirmation for writes whose content came from a tool result).
**Better:** add a second, smaller/weaker model as the target to see a non-ceiling result.
**Status:** done (full 18-run suite). Static 0/3, adaptive 0/6 ASR, utility 1.0, 0 exfiltrations, both modes;
defenses cost ~9% tokens. Adaptive did **not** beat static, so no targeted defense added. Remaining: weak-model target
for a non-ceiling signal.

### 3. Close the SWE-bench loop (agent that can run the tests) — infra done, measuring
**Why:** the first Argo SWE-bench attempt produced a plausible but likely wrong patch, because the agent could not
run the project's tests — the harness's key feedback loop was missing.
**Do:** two options, cheapest first:
- **A. Local venv per instance**: `pip install -e .` (and the test extras) in the cloned repo, let the agent run the
  specific FAIL_TO_PASS tests. Cheap, no Docker, but dependency drift from the official image.
- **B. Agent-in-container**: exec the agent's read/edit/test actions inside `swebench/sweb.eval.*` (already validated
  by the gold run) so the environment is exactly official. More work, exact.
**Pass:** 2-5 instances graded by the official harness, with a written pass rate (no cherry-picking).
**Status:** option A implemented (`--install-deps`: editable install + FAIL_TO_PASS command in the prompt, unit-tested).
A live run on `sympy__sympy-16988` is running; grade it with `--evaluate` and record pass rate. Option B (agent inside
the official image) is still the exact-environment alternative.
**Note:** one image ≈ 2.5-3 GB unpacked; this box has ~9 GB and 4 GB RAM, so run one at a time and clean up.

### 4. ~~Self-contention guard for the provider~~ — done
**Why:** we repeatedly interrupted each other because the harness and this session share one rate-limited endpoint.
**Done:** in-flight cap `ARGO_MAX_CONCURRENCY` (default 2, per event loop), jittered exponential backoff honoring
`Retry-After`, 6 attempts on both `_post` and both streaming paths. 5 tests in `tests/test_provider_limits.py`.
Remaining: a `--low-priority` pacing flag, and a live 30-iteration competing-client run once quota allows.

---

## P1 — mechanisms with strong literature but no local measurement

### 5. Retrieval: replicate the RRF/rerank findings on a second corpus (NFCorpus)
**Why:** the current defaults (`k=60`, dense-heavy 1.2) were tuned on SciFact alone; a 2-point nDCG swing on one
sampled corpus is weak evidence.
**Do:** add NFCorpus to `benchmarks/retrieval.py` and rerun `--ablate`.
**Pass:** the direction (dense-heavy > BM25-heavy, rerank depth 20 as the knee) holds on both corpora before any
default changes. Then consider `rrf_k=10` as the default.

### 6. Memory: scale the dream-generation policy
**Why:** "two generations = same recall at 2.2× cost" was measured on 6 lessons. Consolidation policies usually pay
off on larger, noisier histories.
**Do:** extend `benchmarks/memory_loop.py` to 40-60 lessons over 5 sessions with duplicates, contradictions and
time decay; compare 1 vs 2 generations, and a "dream every N sessions" schedule.
**Pass:** a concrete rule (generations, cadence, wiki extraction on/off) with recall/cost numbers.

### 7. Prompt/context: ablate the summary prompt itself
**Why:** the live run suggests summaries drop exact values; the ledger is a patch around that.
**Do:** A/B three summary prompts (current structured; "keep a bullet per tool result with raw identifiers";
"two-pass: extract facts, then compress") on the 12k live chain.
**Pass:** the winning prompt reduces compactions or raises recall at equal tokens.

### 8. ~~Auto-fix loop for reviewed diffs~~ — done
**Why:** `review_changes=true` now returns FAIL with file:line defects but the parent has to act manually.
**Done:** `spawn_subagent(..., review_changes=true, repair_attempts=N)` re-runs the child in the same worktree with
the reviewer text, then re-reviews, capped at 3 rounds; metadata records `repair_rounds` and total subagent tokens.
Test: `test_review_failure_triggers_in_place_repair_and_recheck`.
**Still open:** a live measurement on the repair benchmark (does repair actually improve resolution per token?).

---

## P2 — always-on and operations

### 9. ~~Multi-worker daemon safety~~ — done
**Why:** `recover_stale` assumes one worker per queue; two daemons would double-claim or falsely recover.
**Do:** SQLite advisory lock (`BEGIN IMMEDIATE`) around claim, worker id + heartbeat column, lease renewal every
N seconds, and recovery keyed on `heartbeat_at` rather than `updated_at`.
**Pass:** two workers, 20 goals, zero double-executions; kill -9 a worker mid-goal → exactly one requeue.
**Status:** done. `BEGIN IMMEDIATE` claim + `worker_id`/`heartbeat_at` leases renewed every t/3; recovery keyed on
`heartbeat_at`. 5 tests (`tests/test_daemon.py`): 20-goal two-queue race, live lease not stolen, crashed lease requeued,
stolen heartbeat rejected, in-run renewal.

### 10. ~~Cost and budget controls~~ — done
**Why:** tokens are accounted but not priced; always-on agents need per-goal caps.
**Do:** model price table, per-goal `max_tokens`/`max_wall`/`max_tool_calls`, `--budget` on `argo run`, and a
`argo metrics --cost` column. Stop reason `budget` already exists.
**Pass:** a goal that exceeds its cap ends `budget` with a partial result and an outbox note.
**Status:** done. `max_run_tokens` / `max_run_wall_seconds` / `max_run_tool_calls`, `[pricing]` table,
`argo run --budget/--max-wall/--max-tool-calls`, `argo metrics --cost`. 5 tests in `tests/test_budget.py`.

### 11. ~~Observability depth~~ — done
**Why:** `argo trace`/`argo metrics` exist but are single-host and coarse.
**Do:** per-tool p50/p95, tokens per tool call, OTel/JSON export, and a `argo metrics --since` filter. Optionally
serve a live trace view from `argo serve`.
**Pass:** a failing 36-step run can be diagnosed from `argo metrics` + `argo trace` alone (already true for the
churn bug; make it systematic).
**Status:** done for single-host. Added per-tool p50/p95, tokens/tool call, and `argo metrics --since`; `--json`
is the export format. OTel export and a served live view remain optional and are not required for the pass bar.

### 12. ~~MCP completeness~~ — done
**Why:** tools over stdio only.
**Do:** resources + prompts, HTTP/SSE transport, and per-server/per-tool approval policy (mutating default).
**Pass:** a live server exposing a resource is readable from the agent; a mutating MCP tool triggers the approval
queue.
**Status:** done. Resources listed + `mcp__<server>__read_resource` tool; `transport="http"|"sse"` via the SDK's
`streamable_http_client`/`sse_client`. Tests: stdio resource read, agent registry read tool, real uvicorn HTTP server.
Mutating MCP tools still go through approvals (`read_only=False` default).

---

## P3 — breadth (only after P0-P2)

13. **Computer use**: selector-drift recovery and a tiny WebArena/OSWorld subset; measure unsafe-action rate.
14. **Deep research R3/R4**: ALCE-style citation precision/recall at report level, then a GAIA/BrowseComp-style
    exact-answer subset with cached pages.
15. ~~**Plan persistence**~~ — done: `Session.plan_approved` + `approved_actions` are persisted and reloaded;
    a resumed plan is reused when actions ⊆ approved, and re-prompts on scope change. 2 tests.
16. **Multi-model router**: cheap model for exploration, strong model for final edits; ablate on repair/SWE-bench.

---

## Known defects / open questions

- **Live compaction is still unproven** until item 1 is rerun; the synthetic benchmark's 12/12 did not transfer.
- **Loop guard tuning is empirical**: mutating OR external-fetch OR novel-read-path = progress. A different definition
  may fit other workloads; the three regression tests pin the behaviour we care about.
- **`_last_usage` calibration** means compaction math trusts the provider's last prompt count; a provider that
  reports prompt tokens excluding cached content could shift thresholds.
- **Benchmarks are single-model, single-run** (deepseek-v4.1-flash). No confidence intervals anywhere.
- **Rate limit is the practical bottleneck** for every remaining live item; batching live runs overnight is the
  cheapest way forward.
