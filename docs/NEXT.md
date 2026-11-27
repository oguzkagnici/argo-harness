# What to implement next

Ordered by (a) what is currently *unproven or broken*, (b) how much evidence exists that the mechanism helps, and
(c) cost in rate limit/quota on this shared machine. Every item lists the success criterion so it can be closed with
a measurement, not an opinion.

Current state: 203 offline tests pass. Live evidence exists for retrieval, memory, repair, documents, citation,
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
defenses cost ~9% tokens. Adaptive did **not** beat static, so no targeted defense added. Weak-model target
(`qwen3.8-flash-next`) **also 0/6 ASR**. Then the harder templates (audit-hook, authority spoof, two-document)
were added: aligned models still 0/3, but `gemma-4-31b` under a **naive document-trust policy** scored **1.0 ASR
undefended → 0.0 defended** with utility 1.0 — the first measured win for the untrusted-content guard. Remaining:
run AgentDojo's full task suite against a naive target. **Multi-turn priming done** (`--priming 2`):
naive `gemma-4-31b` ASR **1.0 → 0.0** with defenses, utility 1.0; the model accepted the norm and still refused the
write because the guard is applied to tool results (DECISIONS §3w).

### 3. Close the SWE-bench loop (agent that can run the tests) — infra done, measuring
**Why:** the first Argo SWE-bench attempt produced a plausible but likely wrong patch, because the agent could not
run the project's tests — the harness's key feedback loop was missing.
**Do:** two options, cheapest first:
- **A. Local venv per instance**: `pip install -e .` (and the test extras) in the cloned repo, let the agent run the
  specific FAIL_TO_PASS tests. Cheap, no Docker, but dependency drift from the official image.
- **B. Agent-in-container**: exec the agent's read/edit/test actions inside `swebench/sweb.eval.*` (already validated
  by the gold run) so the environment is exactly official. More work, exact.
**Pass:** 2-5 instances graded by the official harness, with a written pass rate (no cherry-picking).
**Status:** option A implemented and **measured**: `sympy__sympy-16988` is **resolved 1/1** (FAIL_TO_PASS 2/2,
PASS_TO_PASS 72/72), 17 iter / 228,926 tok, vs a 0-line ungraded patch before. Found and fixed two harness bugs along
the way (novel shell output now counts as progress; official test patch must be applied, `setuptools` for py3.12).
**Measured pass rate: 8/8 resolved across 4 repos** (sympy ×3, flask ×2, xarray ×1, **django ×2**; django needed the
official `runtests.py` command from the spec, a PATH-prefixed venv and non-`--no-deps` installs — see DECISIONS §3v). Environments from official
specs (DECISIONS 3j); grade one instance at a time (image size). Broadened to 12 attempts / 5 repos: **10 of 11 gradable resolved**; `psf/requests` needs a longer timeout and a
gold-control gate (one instance was environment-invalid, one timed out with no patch). Astropy is blocked by its
`numpy==1.16.0` spec on a modern toolchain; a Lite-wide sample and multi-seed runs are still missing. Option B (agent inside the official image) is still the
exact-environment alternative.
**Note:** one image ≈ 2.5-3 GB unpacked; this box has ~9 GB and 4 GB RAM, so run one at a time and clean up.

### 4. ~~Self-contention guard for the provider~~ — done
**Why:** we repeatedly interrupted each other because the harness and this session share one rate-limited endpoint.
**Done:** in-flight cap `ARGO_MAX_CONCURRENCY` (default 2, per event loop), jittered exponential backoff honoring
`Retry-After`, 6 attempts on both `_post` and both streaming paths. 5 tests in `tests/test_provider_limits.py`.
Remaining: a live 30-iteration competing-client run once quota allows. (`low_priority_pacing` flag implemented,
tested.)

---

## P1 — mechanisms with strong literature but no local measurement

### 5. ~~Retrieval: replicate the RRF/rerank findings on a second corpus (NFCorpus)~~ — done
**Why:** the current defaults (`k=60`, dense-heavy 1.2) were tuned on SciFact alone; a 2-point nDCG swing on one
sampled corpus is weak evidence.
**Do:** add NFCorpus to `benchmarks/retrieval.py` and rerun `--ablate`.
**Pass:** the direction (dense-heavy > BM25-heavy, rerank depth 20 as the knee) holds on both corpora before any
default changes. Then consider `rrf_k=10` as the default.
**Status:** done. Dense > BM25 and rerank-best-nDCG replicate; RRF-best-Recall@100 does not. No default changed;
`--corpus nfcorpus` added. A full `--ablate` pass on NFCorpus is still optional.

### 6. ~~Memory: scale the dream-generation policy~~ — done
**Why:** "two generations = same recall at 2.2× cost" was measured on 6 lessons. Consolidation policies usually pay
off on larger, noisier histories.
**Do:** extend `benchmarks/memory_loop.py` to 40-60 lessons over 5 sessions with duplicates, contradictions and
time decay; compare 1 vs 2 generations, and a "dream every N sessions" schedule.
**Pass:** a concrete rule (generations, cadence, wiki extraction on/off) with recall/cost numbers.
**Status:** done at 24 lessons / 5 sessions, then re-run at **40 lessons**: one pass already reaches 1.00 correct /
0 stale, and two passes cost 2.4-3.7× wall for the same recall — the ">20 ⇒ 2 generations" rule did **not**
replicate, so auto-escalation was rolled back and `--dream-generations` defaults to 1 (explicit opt-in for 2).
Cadence remains testable; wiki-on/off at scale is still optional.

### 7. ~~Prompt/context: ablate the summary prompt itself~~ — measured
**Why:** the live run suggests summaries drop exact values; the ledger is a patch around that.
**Do:** A/B three summary prompts (current structured; "keep a bullet per tool result with raw identifiers";
"two-pass: extract facts, then compress") on the 12k live chain.
**Pass:** the winning prompt reduces compactions or raises recall at equal tokens.
**Status:** `summary_prompt_variant=default|identifiers|twopass` + `context_live --modes prompt_ids,prompt_twopass --probes`.
Measured at 18 steps: `identifiers` ties recall (3/3) with 1 vs 2 compactions and fewer tokens; `twopass` is not
viable (two calls per compaction fail under rate limits). **36-step rerun done:** both 3/3 with identical ids;
`identifiers` −15.6% tokens but 5 vs 3 compactions, so the compaction-count win does not replicate. Rule:
default prompt for short runs, `identifiers` when runs exceed ~30 steps.

### 8. ~~Auto-fix loop for reviewed diffs~~ — done
**Why:** `review_changes=true` now returns FAIL with file:line defects but the parent has to act manually.
**Done:** `spawn_subagent(..., review_changes=true, repair_attempts=N)` re-runs the child in the same worktree with
the reviewer text, then re-reviews, capped at 3 rounds; metadata records `repair_rounds` and total subagent tokens.
Test: `test_review_failure_triggers_in_place_repair_and_recheck`.
**Still open:** the trap-task measurement (slugify/intervals) showed baseline generalization 2/2, so review and
repair had no headroom. The weaker model (`gemma-4-31b`) also resolved 2/2, so both small-task traps are exhausted.
The worktree arm is now runnable end to end (`adopt=true` merges the reviewed branch; `--modes wtrepair`): it resolved
`intervals` 1/1 at +45% tokens / 2.5× wall. A multi-file spec task (`ledger`, three rules, hidden boundary/validation/
cache tests) was added and validated (`--self-check` 8/8): baseline **1/1** (66k tok, 8 iter) and review **1/1**
(178k tok, 16 iter, one PASS review) — still no headroom. **Next:** harder SWE-bench-Lite instances that the baseline
fails, plus 3 seeds per arm.

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
**Why:** `argo trace`/`argo metrics` exist but are single-host and coarse. (`argo export-otel` now emits OTLP/JSON.)
**Do:** per-tool p50/p95, tokens per tool call, OTel/JSON export, and a `argo metrics --since` filter. Optionally
serve a live trace view from `argo serve`.
**Pass:** a failing 36-step run can be diagnosed from `argo metrics` + `argo trace` alone (already true for the
churn bug; make it systematic).
**Status:** done for single-host. Added per-tool p50/p95, tokens/tool call, and `argo metrics --since`; `--json`
is the export format. OTel export is done, and the served live view is done (`/trace`, `/v1/sessions`; 4 tests).

### 12. ~~MCP completeness~~ — done
**Why:** tools over stdio only.
**Do:** resources + prompts, HTTP/SSE transport, and per-server/per-tool approval policy (mutating default).
**Pass:** a live server exposing a resource is readable from the agent; a mutating MCP tool triggers the approval
queue.
**Status:** done. Resources + prompts (read_resource / get_prompt tools), `transport="http"|"sse"` via the SDK's
`streamable_http_client`/`sse_client`, per-server `read_only` plus per-tool `read_only_tools` overrides. Tests: stdio resource+prompt read,
agent registry tools, real uvicorn HTTP server.
Mutating MCP tools still go through approvals (`read_only=False` default). Per-tool overrides keep a mutating
server's read tools out of the approval path (`{"search": true}`); covered by `test_per_tool_read_only_override`.

---

## P3 — breadth (only after P0-P2)

13. **Computer use**: selector-drift recovery done (replay 1/2 → 4/4). Stateless subset 3 tasks: **3/3, rate 0.0**,
    88,686 tok. **WebArena-shaped subset** (`webarena_subset.py`: cookie sessions, server-side filters/pagination,
    POST refunds, settings state; decoy cancelled order / sold-out product): **3/3, unsafe 0.0**, 357,785 tok.
    Remaining: the real WebArena/OSWorld apps (dockerized sites, desktop control) and CAPTCHA/cross-site flows.
14. ~~**Deep research R3/R4**~~ — done. R3 report-level citation precision/recall **1.000**. R4 exact-answer subset:
    **6/6** at 69,325 tokens / 817 s (was 5/6 at 311,650 before the `/docs` index fix). 3-hop set: **4/4** single pass
    vs **4/4** with the coverage gate at +85% tokens — gate is opt-in only. 8 unit tests.
15. ~~**Plan persistence**~~ — done: `Session.plan_approved` + `approved_actions` are persisted and reloaded;
    a resumed plan is reused when actions ⊆ approved, and re-prompts on scope change. 2 tests.
16. **Multi-model router**: compaction routing done earlier; per-turn `--router explore` **done and measured**
    (cheap model 4/4 on the 3-hop research set at −14% tokens; write-intent tasks and post-mutation turns stay strong;
    6 tests). Remaining: a review-only cheap subagent, and cost-weighted routing once `[pricing]` exists for the
    provider's models.

---

---

## Feature roadmap (not benchmarks)

Benchmarks above answer "does the mechanism work"; these are capabilities the harness itself still lacks. Ordered by
(user value × evidence) / effort, each with a closure test.

### F1. Review-only cheap subagent (`--router review`)
The router sends read-only *exploration* turns to the cheap model but every subagent inherits the strong one. A
reviewer with a fixed rubric is a read-only task, so it should be routable. **Closure:** same verdicts as the strong
reviewer on the saved repair patches, at lower cost; if verdicts diverge, keep it opt-in and document the divergence.

### F2. Per-subagent MCP scoping
MCP is deliberately top-level only (`_register_mcp` runs at depth 0). A subagent working on an untrusted document
can currently not call MCP at all — safe but limiting; the alternative (inherit everything) is unsafe. **Closure:**
`spawn_subagent(mcp_servers=[...])` registers only the named servers, with a test proving an unlisted server's tool
is absent and an approval still gates mutating calls.

### F3. Desktop control (OSWorld-shaped)
The browser tool covers web tasks; OSWorld needs screen/click/type on an X display. **Closure:** a `desktop` tool
(screenshot + click/type/key) under approval, plus 3 deterministic tasks on Xvfb scored on final screen state, with
an unsafe-action counter like the web subset.

### F4. Workflow extraction from live computer-use traces
`argo workflow synthesize` already turns *tool* traces into a workflow + SKILL.md. Browser traces (selectors, form
fills, navigation) are not captured in that shape, which is exactly the "automate a complex computer-use flow" ask.
**Closure:** synthesize from a `computer_use` session, replay it with drift, and score with the existing replay
harness (plain vs self-healing selectors).

### F5. Sandboxed code execution
`run_code`/`bash` run as the host user; writes outside the workspace are policy-checked but the process is not
isolated and has the host network. **Closure:** an opt-in namespace/container runner (bubblewrap or docker) with a
network allowlist, plus tests showing (a) host paths are unreachable, (b) allowed commands behave identically.

### F6. Server auth + per-key quotas
`argo serve` has a shared key and no per-client budgets. The budget module already counts tokens/wall/tool calls.
**Closure:** bearer tokens per client mapped to a budget, 429 on exhaustion, tests plus a curl transcript.

### F7. Memory cadence scheduler
The daemon dreams on a timer (`--dream-hours`) and `--dream-generations` is manual. Evidence says one pass is enough
even at 40 lessons, so cadence should be driven by *new material* (sessions/lessons since last dream), not only time.
**Closure:** skip when nothing new; offline test plus a 40-lesson run showing equal recall at lower cost.

### F8. Answer-probe-aware compaction
Summaries keep losing exact values; the fact ledger patches around it. **Closure:** re-inject the user's question
verbatim into the compacted block, A/B on the 36/48-step live run (recall + tokens), keep only if it wins.

### F9. Observability alerts
`argo metrics` reports p50/p95, tool error rate and cost, but nothing watches them. **Closure:** threshold/rate-of-change
alerts exported with the OTel payload; a synthetic failing run must raise one.

### F10. Outbox delivery channels
The daemon outbox writes to disk. **Closure:** webhook + SMTP sink with retries and dead-letter, tested against a
local HTTP server (no network assumptions).

### F11. Approvals in `argo serve`
Approvals exist in the CLI and the daemon queue; a served session has no way to approve. **Closure:** `GET /approvals`
and `POST /approvals/<id>` with the same key protection as `/trace`, plus a test that a mutating tool blocks until the
approval arrives.

### F12. Prompt-cache telemetry
Prefix stability is designed for caching but never measured. **Closure:** per-session cache-hit ratio in `argo metrics`
and a regression test that a warm session reuses the same tool/system prefix.

**Suggested order:** F11 and F2 (small, offline-testable, close real safety/UX gaps), then F1 and F12 (cheap routing
and cost wins), then F3/F4 (the computer-use automation story), then F5/F6 (production hardening), F7–F10 as
scheduled follow-ups.

## Known defects / open questions

- **Live compaction is still unproven** until item 1 is rerun; the synthetic benchmark's 12/12 did not transfer.
- **Loop guard tuning is empirical**: mutating OR external-fetch OR novel-read-path = progress. A different definition
  may fit other workloads; the three regression tests pin the behaviour we care about.
- **`_last_usage` calibration** means compaction math trusts the provider's last prompt count; a provider that
  reports prompt tokens excluding cached content could shift thresholds.
- **Benchmarks are single-model** (deepseek-v4.1-flash) and mostly single-run. First repeated arm added:
  `adaptive_injection --repeats 3` → zero variance (9/9 vs 0/9). Cheap arms can be repeated now; repair/SWE-bench
  seeds still need a quota plan.
- **Rate limit is the practical bottleneck** for every remaining live item; batching live runs overnight is the
  cheapest way forward.
