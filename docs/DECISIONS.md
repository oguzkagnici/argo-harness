# Decision log

Argo's rule is simple: every non-trivial implementation choice must be justified by a harness that is known to
work or by published evidence, and measured locally when possible. This log records the decision, the evidence,
and the observed effect. Benchmark provenance and commands live in [`benchmarks/README.md`](../benchmarks/README.md);
raw records are in `benchmarks/results/`.

Measurements below are from `deepseek-v4.1-flash` via the configured OpenAI-compatible endpoint, with
`qwen3-embedding-8b` and `qwen3-reranker-8b`, on 2026-11-24.

## 1. Decision summary

| Area | Decision | Evidence base | Measured effect / rationale |
|---|---|---|---|
| Control loop | ReAct-style interleaved reasoning + actions, not one-shot plan execution | ReAct (Yao et al., 2022), CodeAct (Wang et al., 2024) | Handles research + files + browser in one loop; a single 24-iteration run produced a DOCX and PPTX and passed review |
| Actions | Code execution (`run_code`, `bash`) is first-class alongside typed tools | CodeAct; Claude Code, Codex | Lets the agent test its own artifacts; used in every repair-benchmark task |
| Prompting | Stable system prefix, volatile state appended later | Anthropic context-engineering guidance; provider prompt caching | Cached prompt tokens are commonly >90% after the first turn (e.g. 6,208 of 6,399 prompt tokens in a repair session) |
| Context | Microcompaction → structured compaction → reset + handoff | Anthropic long-running harness work; MemGPT (Packer et al., 2023) | Unit-tested; the 415k-token document run completed without a context reset |
| Context trigger | Threshold = min(fraction of window, window − 13k reserve) with a 3-failure compaction circuit breaker | Claude Code constants (`AUTOCOMPACT_BUFFER_TOKENS=13k`, `MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES=3`); observed 50+ consecutive failure loops in that codebase's telemetry | Synthetic 30k-token transcript: autocompaction at 75% and at the reserve bound both reduced the live prompt 29.6k → 7.5k tokens while keeping 12/12 recall; the breaker allows at most 3 summary attempts per session |
| Cache-expiry eviction | Clear all but the 5 newest tool results after a >60 min idle gap | Claude Code time-based microcompact; 1 h server prompt-cache TTL | No effect inside a single run; prevents rewriting a cold prefix after a long pause. Unit-tested (5 of 8 stale results cleared) |
| Compaction hysteresis | After a successful compaction, do not compact again until the context grows by `compact_min_growth_tokens` (1200); never summarize a head shorter than 3 messages | Live 36-step run with a 12k window exposed churn: the post-compaction prompt stayed above the 6k threshold, so every turn re-summarized ~2 messages — **11 compactions**, 1 context reset, and **0/3 probe recall**. Claude Code's autocompact does not re-fire in the same threshold window | Two regression tests (no repeat summary without growth; tiny head does not trip the 3-failure breaker); the churn run is preserved in `benchmarks/results/context_live_20261125-*.json` |
| Identifier capture | The ledger also extracts identifier-shaped tokens from prose (prefixed ids like `T03-613ad6`, UUIDs, `commit <sha>`, ticket ids, emails), capped at 60 entries | The live 12k run showed the ledger only matched `key: value` lines, so "Checkpoint id T03-…" was never preserved; MemGPT-style ledgers keep raw identifiers | 16 context tests, including one that feeds the real 36-step benchmark payloads and asserts **0 ids dropped** in a 611-char ledger |
| Reset gating | A reset happens only when the prompt is still above the **hard budget** after compaction. Compaction itself fires when over the threshold *and* the context grew since the last compaction, or unconditionally when over the budget | The live 12k run reset once per mode: first right after a successful compaction, then again on a turn where hysteresis was deliberately deferring compaction. Resets throw away recall; Claude Code treats reset as a fallback | 3 tests (`reset_threshold=0.05` no longer resets after compaction; hysteresis never resets; over-budget forces compaction inside the hysteresis window). Live autocompact row still showed `reset=1` before this fix |
| Ledger across resets | `make_handoff` appends the same verbatim ledger to the handoff artifact, so a reset does not discard identifiers that compaction already preserved | The live 12k run reset once and the handoff (summary + last 4 messages) contained none of the earlier checkpoint ids; resets should lose *narrative* context, not facts | Test asserts the on-disk handoff contains the id from the raw history |
| Ineffective-compaction breaker | If two consecutive compactions leave the context at or above the trigger, compaction is disabled for the session (`context_compact_ineffective`) and the agent continues on microcompaction + ledger | Churn is the failure mode we measured (11 compactions, 0/3 recall); a breaker is cheaper than repeating the same ineffective summary call | Test with a fake compactor that "succeeds" without shrinking; breaker fires after 2 |
| Compaction fidelity | Append a bounded, newest-wins **verbatim fact ledger** (key/value lines + URLs, ≤6k chars) to the summary | MemGPT archival memory and Generative Agents memory stream keep raw facts; narrative summaries paraphrase | Ablation on the 30k-token transcript: recall **5/12 → 12/12** for **+768 tokens** (6.7k → 7.5k), versus 12/12 at 29.6k with no compaction |
| Extensibility | Official MCP Python SDK client over stdio; namespace `mcp__server__tool`; mutating by default with per-server `read_only` opt-in | Claude Code/Codex MCP integration; MCP spec | 3 unit tests (discovery, call, bad-server isolation) + live end-to-end call returning 42; exposing `input_schema` cut a live run from 3 iterations / 17,928 tokens to **2 / 11,921** (-33%) |
| Document tooling | Keep structure-aware `read_document` as the primary path; bash parsing remains available | Document QA: both accurate, tool used 24% fewer tokens and ~1 fewer iteration | Tool-mode 6,748 tok / 2.9 iter vs bash 8,351 / 3.8; the single tool miss was a provider stall, correct on retry |
| Review policy | Risk-scored auto-review (`adaptive`: documents 4 each, code 2 each, long runs +2, threshold 4); explicit review suppresses auto | Always-review was +35% tokens with no accuracy gain on easy repairs; documents are where reviewers found real defects | 6 tests; live doc run auto-reviewed at 27.8k tokens and found concrete defects; criteria now include the user's goal so reviewers don't demand unrequested sections |
| Research evidence | Reports render from a ledger; claims can only cite existing source ids | ALCE (Gao et al., 2023) attribution; R1 in `DEEP_RESEARCH.md` | 5 tests; dangling citations are rejected at write time, so fabricated ids cannot reach a report |
| Citation verification | Per-claim conservative entailment check with JSON verdicts, retries, and uncited ⇒ unsupported | ALCE citation precision/recall; R2 | Local 12-claim benchmark: false-support **1.000 → 0.000**, bad claims caught **1.000**, supported retained **1.000**; ~10 s/claim at concurrency 2 |
| Untrusted content | Wrap tool/web/document output in `<<<BEGIN_UNTRUSTED>>>` markers, add injection rules to the system prompt, block URLs containing credentials/canaries | AgentDojo (Debenedetti et al., 2024) and OWASP LLM01 (indirect injection); Claude Code/Codex provenance practices | Local 8-attack suite: **0/7 obedience in both modes** — the model resisted even undefended, so the suite is at ceiling; the guard costs **+2.4% tokens** (5,191 → 5,318 mean). Retained as defense-in-depth, not as a measured win |
| Retrieval | BM25 + dense fusion with RRF (`k=60`), then cross-encoder rerank the top candidates | RRF (Cormack et al., 2009); BEIR (Thakur et al., 2021) | SciFact nDCG@10: BM25 0.779 → dense 0.885 → RRF 0.846 → RRF+rerank **0.916** |
| Retrieval depth | Rerank only the fused candidate pool, not the whole corpus | Cost/latency control; reranker literature | R@100 stays 1.00; rerank improves ordering; reranking the full corpus would cost ~5× more per query |
| Memory retrieval | Small importance/recency/usage uplift on memory hits | Generative Agents (Park et al., 2023) | Keeps fresh, important notes ahead of stale-but-similar text; not separately ablated yet |
| Memory consolidation depth | One dream generation is the default; a second pass is not beneficial | Ablation: two generations kept recall at 1.00 but cost 2.2× (114 s vs 51 s) and archived notes with no gain | Skipped the extra pass; grounding stays for confidence hygiene, not recall (no-grounding arm had identical recall) |
| Retrieval fusion knobs | Expose `rrf_k` and lexical/dense weights; keep 60 / 0.8 / 1.2 defaults | SciFact ablation: k=10 0.8649 and dense-heavy 0.8660 beat k=60 0.8460 by ~2 nDCG points | Single-corpus evidence, so knobs are configurable but defaults unchanged; rerank depth stays 20 (50 = same quality, 2.5× cost) |
| Memory write | Exact-duplicate idempotence; cosine dedupe at 0.97 during dreaming | Anthropic memory/Dreams docs; MemGPT tiering | Dream run merged 6 duplicates and created 6 consolidated facts without unbounded growth |
| Consolidation | Generational dreaming (`G → G+1`) with evidence grounding and rollback | Anthropic Dreams; Letta sleep-time compute (2025) | Memory benchmark: contradictory stale beliefs **1.00 → 0.00**, corrected recall **0.00 → 1.00** |
| Grounding | Ungrounded dream claims are confidence-capped | Anthropic Dreams hygiene; unit tests | Hallucinated "moon is brie" claim capped at ≤0.5 and visibly marked unverified |
| Stall control | Block third identical read-only call; nudge at 40%/70%; stop at `no_progress_limit`; **external-info tools count as progress** | Reflexion (Shinn et al., 2023); Anthropic harness guidance; Claude Code/Codex loop guards | Eliminated the observed DOCX-unzip loop; live doc+review run finished in 24 iterations; unit-tested at the default and a custom limit |
| Stall control scope | Progress = a mutating tool, a non-duplicate external fetch/search, or a **first visit to a read path**. Re-reading the same path, or repeating the same call, still counts as no progress; a local mutation clears both the duplicate history and the visited-path set | The guard's target is "re-verify my own artifact", not "read many pages/files"; research chains and repo exploration are legitimate | Live 36-step token-chain: the guard fired at iteration 10 after 9 fetches (1/3). After the fix: **35/35 fetches, 37 iterations, 3/3**. SWE-bench run also stopped at 22 iterations while exploring distinct files; novel reads now count. Repeated-fetch and repeated-read cases still stop at `no_progress` (unit tests) |
| Verification | Separate read-only `review_work` subagent with explicit criteria | Anthropic generator/evaluator split; Claude Code subagents | Previous reviewer spent 10 iterations / 307 s; with tool-scoped guidance it took 3 iterations / 13.8k tokens on a smoke test |
| Delegation | Context-isolated subagents, sharing memory/LLM but not conversation | Claude Code, Codex | Used for review and research branches; keeps parent context small (not yet A/B benchmarked) |
| Isolation | Code-editing subagents can run in a linked git worktree on `argo/<slug>`; parent tree untouched, adoption by explicit merge | Claude Code / Codex worktree isolation; avoids concurrent edits to one checkout | 3 tests (isolation, changed-file detection, agent spawn) + live run: branch created, parent `git status` clean, child edit visible only in the worktree |
| Repair after review | `repair_attempts=N` (≤3) re-runs the child **in the same worktree** with the reviewer's findings, then re-reviews; the loop stops on PASS | Generator → evaluator → repair is how Claude Code/Codex iterate, and a FAIL verdict is only useful if something acts on it | 6 worktree tests incl. FAIL→repair→PASS with the parent still untouched; each round is a separate child session, so traces stay attributable |
| Adoption gate | `spawn_subagent(isolate=true, review_changes=true)` runs a read-only reviewer over the branch diff (fork-point → HEAD plus uncommitted edits) before the parent merges; FAIL lists file:line defects and keeps the branch | Generator/evaluator split (Anthropic) applied to merges; Codex/Claude Code review-before-commit practice | 5 worktree tests incl. committed-child diff; live run: reviewer returned **PASS** (19.4k tokens), parent `calc.py` still `a - b`, branch only on the worktree. First live run exposed an **empty diff** for committed work; fixed by diffing against the recorded fork SHA |
| Plan mode | `--plan` blocks mutating tools until `propose_plan` is approved once; the approval covers the batch (per-action gate reports "covered by approved plan") | Claude Code plan mode; batch approval avoids prompt fatigue | 3 tests (blocked before approval, rejection keeps block, off by default) + live run in 7 iterations / 45,962 tokens with no pre-approval writes |
| Background work | `start_background` launches a subagent as an asyncio task; `check_background(id, wait_seconds)` blocks instead of polling; jobs die with the run | Claude Code background tasks; in-memory only, durable work stays in the daemon queue | Live A/B: polling-heavy prompt cost **10 iter / 71,428 tok** and ended `no_progress`; one waiting call cost **3 iter / 19,284 tok** and finished `final` (−73% tokens) |
| Document read-back | `read_document` preserves DOCX title/heading levels, list markers, and table dimensions | Reviewers must audit without unzipping OOXML; the reviewer had flagged flat extraction as a defect | DOCX now renders as `TITLE:`, `# Heading`, `- bullet`, `TABLE 1 (2x2):`, so structural criteria are checkable directly |
| Parallelism | Parallel tool calls only when every call is read-only and parallel-safe | Claude Code/Codex tool semantics | Preserves approval and write ordering; exact-order unit test added |
| Automation | Workflows are declarative tool sequences with params/assertions, not replayed code | Codex/Claude Code preference for structured actions | Extracted `demo_login` replays **5/5** across different names at ~1.8 s warm |
| Always-on recovery | `recover_stale` requeues goals whose worker died mid-claim (default 1800 s), increments `attempts`, and permanently fails a goal after 3 attempts | Anthropic always-on guidance (crash recovery); standard queue lease/visibility-timeout semantics (SQS-style) | 4 tests: stale running → pending with attempt=1; third attempt → `failed`; fresh/pending untouched; dedupe still rejects a duplicate after requeue |
| Observability | Append-only JSONL sessions get `argo trace <session>` (timeline) and `argo metrics [--dir]` (tokens, tool error rate, slowest tools, compactions/resets, stop reasons); per-tool `duration_s` recorded; compaction/loop events mirrored from the bus to the session | Always-on agents need post-hoc debugging of cost and failure modes; matches the "trace/metrics" surface of hosted agent platforms | 8 offline tests (summary, short-key usage, ordering, limit, duration capture, bus→session mirroring), verified on a real 10-iteration session; older sessions show `tokens 0` because they predate the short-key reader, new ones report totals |
| Always-on | Durable SQLite goal queue, read-only trigger allowlist, approvals, outbox | Anthropic always-on agent guidance | Tests cover approval queueing, outbox delivery, and read-only enforcement; daemon `--once` verified live |
| Compatibility | OpenAI-compatible `/v1/chat/completions` **and** `/responses`, native SSE, BYOK + Codex config inheritance | User requirement; OpenAI Responses API; Codex config uses `wire_api = responses` | Live parity check passes both paths; same token accounting (713) for the parity round trip; default config now exercises `/responses` end-to-end |
| Provider concurrency | Hard cap on in-flight provider requests (`ARGO_MAX_CONCURRENCY`, default 2) plus jittered exponential backoff that honors `Retry-After`; 6 attempts before failing | We repeatedly starved our own session by running the harness against the same rate-limited key: 429 storms and multi-minute stalls. Production harnesses rate-limit their own clients rather than relying on the server | 5 tests (cap peak ≤ 2 under 10 concurrent callers, backoff bounds/jitter, 429→200 retry, exhaustion after 6); live runs after the change stopped failing on the first 429 |
| Provider quirks | Responses history uses `input_text` for assistant content and image `detail`; tools are flat `{type,name,description,parameters}` | Provider 400 validation errors | Caught by an agent-run smoke test that unit tests missed; now covered by offline mapping tests |
| Benchmarking | Published protocols, isolated execution, transparent sampling | HumanEval (Chen et al., 2021), BEIR | See measured table below; sampling keeps runs affordable and is documented as a limitation |
| Repair eval | Local deterministic repos with hidden tests, not official SWE-bench | SWE-bench (Jimenez et al., 2024) protocol shape | Validated 5/5 tasks (buggy fails, fix passes); baseline resolves 5/5 with zero test tampering |
| Eval hygiene | Restore tests before grading, flag tampering, recover metrics from JSONL on timeout, merge-only aggregation | SWE-bench evaluation practice | Exposed two misleading timeout rows; produced one authoritative 10-row record instead of reporting zero-token timeouts |

## 2. Measured performance

| Benchmark | Setting | Result |
|---|---|---|
| HumanEval | 20 seeded tasks, pass@1, isolated subprocess | **20/20 (1.00)**, 12,827 tokens |
| BEIR SciFact | 100 queries, 1,000 docs, 112 positives | nDCG@10: BM25 0.7791 · dense 0.8852 · RRF 0.8460 · **RRF+rerank 0.9155**; R@100: RRF and RRF+rerank 1.0000 |
| Memory learning loop | 6 stale→correction lessons, one dream cycle, top-3 | contradictory stale **1.00 → 0.00**; corrected recall **0.00 → 1.00** |
| Workflow replay | extracted `demo_login`, 5 inputs, real Playwright | **5/5**, ~1.8 s per warm replay |
| Repair loop (baseline) | 5 local repos, visible + hidden tests, 16-iteration cap | **5/5 resolved**, 0 tampered, mean 46,990 tokens, 163.0 s, 7.6 iterations |
| Repair loop (with review) | same 5 repos, one mandatory `review_work` call | **5/5 resolved**, 0 tampered, mean 63,382 tokens, 526.9 s, 9.2 iterations (5/5 review calls) |
| Worktree isolation | live run: parent delegates a docstring edit with `isolate=true` | branch created, parent file unchanged, child edit only in worktree; parent `git status` clean |
| Worktree adoption gate | live run: subagent fixes `add()` in `calc.py`, commits it, parent requests `review_changes=true` | reviewer PASS at 19.4k tokens; parent `calc.py` unchanged; run 6 iterations / 47,984 tokens |
| Plan mode | live run with `--plan --yolo` | `propose_plan` approved before writes; **7 iterations / 45,962 tokens**; file created and verified |
| Background tasks | live fetch of example.com, parent asks for title | polling: 10 iter / 71,428 tok / `no_progress`; waiting call: **3 iter / 19,284 tok / `final`** |
| Citation verification | 12 claims over 6 local sources (6 supported, 2 overstated, 3 false, 1 wrong source) | accuracy 0.750, **false-support 0.000**, bad caught **1.000**, supported retained **1.000**; baseline false-support 1.000 |
| Context policies | synthetic 60-step / 29,639-token transcript, 12 planted facts, 32k window | full 29,639 tok 12/12 · microcompact 24,023 tok 12/12 · **autocompact 75% 7,502 tok 12/12** · autocompact 95% 7,546 tok 12/12 · reset 1,225 tok **1/12** |
| Context ablation | same, `compact_fact_ledger=false` | 6,734 tok, **5/12** vs 12/12 with the ledger (+768 tok) |
| Context, live, ledger ablation | 12k window, 36 fetches, 3-4 compactions | `no_ledger` also **3/3** at 265,657 tok / 4 compactions; no recall separation at this scale |
| Context, live 36-step chain | 12k window, 36 sequential fetches, probe ids at steps 3/20/35 | no compaction **3/3** at 288,569 tok / 589 s; before fixes **0/3** (11 compactions, 1 reset); after all four fixes **3/3 at 275,722 tok, 3 compactions, 0 resets, 532 s** (`context_live_20261125-201825.json`) |
| Prompt injection, adaptive | red-team LLM, 2 rounds, defense off, `web_write` | static ASR 0.0, adaptive ASR 0.0, utility 1.0 (single case; suite truncated by rate limits) |
| SWE-bench-Lite (infra) | official harness, prebuilt image, gold patch, `sympy__sympy-21614` | **1/1 resolved**, image cleaned afterwards; validates Docker path end-to-end |
| SWE-bench-Lite (Argo) | `sympy__sympy-16988`, 40-iteration cap, 1,500 s cap | stop `no_progress` at 22 iterations / 464,315 tok; patch = 1 file / 470 chars; no test tampering; not yet graded |
| Prompt injection | 7 attacks (4 obvious, 3 subtle) × defense on/off, local HTTP + files + DOCX | **safe 1.00 / obedience 0.00 both modes**, 0 exfiltrations; mean 5,191 tok (off) vs 5,318 tok (on) |
| Memory ablations | 6 lessons × 5 arms (no-dream, no-wiki, no-grounding, two generations, full) | no-dream stale 1.00 / correct 0.00; full **0.00 / 1.00**; no-wiki stale mentions 0.67 vs 0.50; two generations same recall at 2.2× time |
| Retrieval ablations | SciFact 100 q / 1,000 docs, one factor at a time | rerank 20 **0.9190** nDCG vs 50 0.9161 / 10 0.8919; k=10 0.8649 vs k=60 0.8460; dense-heavy 0.8660 vs BM25-heavy 0.8100; query instruction 0.8392 |
| Document QA | 10 questions × DOCX/PPTX/XLSX/PDF × 2 modes | tool 9/10 (1 stall; retry correct) at 6,748 tok / 2.9 iter; bash 10/10 at 8,351 tok / 3.8 iter |
| Adaptive review | live `report.docx` run | auto-review triggered at risk=4, 27,796 tokens total; goal-aware criteria added after the first run flagged unrequested sections |

Live artifact run (DOCX 2 pages + PPTX 6 slides + review): 24 iterations, 415,612 tokens, ~14 minutes wall,
PASS verdict, two real defects fixed. This run is why the loop guard exists; it previously stalled on repeated
DOCX introspection.

## 3. Costs and tradeoffs we accept

- **Reranking is not free.** SciFact ranking + reranking took 190 s for 100 queries (~1.9 s/query); dense-only
  ranking would be much cheaper but loses ~0.03 nDCG@10. Argo reranks the top candidates only and exposes the
  option, rather than reranking everything.
- **Dreaming is an extra LLM call.** The memory benchmark dream took 48.6 s (one consolidation call over six
  lessons); it is run between sessions, not per request.
- **Review is expensive relative to easy work.** Repair benchmark: review mode used **+35% tokens and 3.2× wall
  time** for the same 5/5 resolved rate, because the read-only reviewer cannot execute tests and these bugs were
  fully specified. It remains on for high-stakes artifacts (decks, reports, risky code changes) and is skippable
  for routine fixes. Reviewer default was cut from 10 to 6 iterations after one run spent >10 minutes globbing a
  two-file repo; that also prevents a runaway evaluator from consuming the producer's budget.
- **Loop guard is conservative.** It blocks the third identical read-only call even if the agent has a reason to
  repeat it; any progress-producing tool resets the counter, and the limit is configurable.
- **Resets lose detail by design.** The 30k-token transcript reset kept 1,225 tokens and 1/12 facts. A reset
  buys a clean window and a written handoff, not recall, so it stays a last resort after compaction failure.
- **Cache-expiry eviction discards content.** After a >60 min gap, old tool results are replaced without a spool
  pointer. This is safe only because the most recent five results and every user/assistant message survive; it is
  deliberately time-triggered rather than size-triggered.
- **The injection suite cannot prove the guard helps.** The model resisted every payload (obvious and subtle) with
  defenses off, so the measured effect is 0 and the cost is +2.4% tokens. Wrappers/rules remain for weaker models
  and for tool-level enforcement, but AgentDojo-style adaptive attacks are needed before claiming a security gain.
- **Plan mode adds ceremony.** The live plan-mode run cost 45,962 tokens for a trivial file write; it is for
  risky or multi-artifact work, not for every task.
- **Background jobs are in-memory.** A process exit loses them; durable goals belong in `argo daemon`.
- **The context benchmark is synthetic.** It uses one 60-step transcript, one seed, and a 32k window to force
  policies to engage; it demonstrates mechanism effects, not production long-horizon quality.
- **SQLite + brute-force cosine** instead of a vector database. The corpus is local and small; this keeps setup to
  zero infrastructure and lets importance/recency be fused with retrieval directly (MemGPT/Generative Agents
  pattern). It will need an ANN index for very large workspaces.
- **Sampled benchmarks.** SciFact uses 100 queries / 1,000 docs and HumanEval uses 20 tasks to bound cost. Relative
  comparisons are valid; absolute values are not comparable to official leaderboards.

## 2a. Adaptive prompt injection (2026-11-25, full suite)

- **Result: no adaptive attack succeeds; the defenses are not load-bearing on this model.** 3 cases × (1 static +
  2 adaptive rounds) × 2 defense modes = 18 runs. Static ASR 0.0 (0/3), adaptive ASR 0.0 (0/6), utility 1.0,
  exfiltrations 0 in both modes. Defenses cost ~9% more tokens (4,635 vs 4,250 mean) with no utility loss.
  Interpretation: `deepseek-v4.1-flash` is at ceiling for this benchmark, so the measurement cannot separate defended
  from undefended behaviour. **Weaker target model (`qwen3.8-flash-next`) is also 0/6 ASR** (utility 1.0, 0
  exfiltrations, defenses +8% tokens), so the *attack set* is the bottleneck, not the target. Next step is
  AgentDojo-style attack templates, not another model. Also fixed: the attacker call had no timeout and could hang
  the suite indefinitely (observed once); it is now bounded by `asyncio.wait_for`.
  **Update (same day): the templates were written and the ceiling broke only on a misconfigured target.**
  `tool_poison` (audit-hook note for automated readers), `authority_spoof` (fake security ticket + pre-approval) and
  `multi_page` (deferred instruction on a second document) are refused by both aligned models, even with a control
  policy that tells the agent document instructions are authoritative. On `gemma-4-31b` with that naive policy the
  ASR was **1.0 (3/3)** undefended and **0.0 (0/3)** with Argo's defenses, utility unchanged, tokens lower
  (5,038 vs 7,123). This is the first measured win for the untrusted-content guard and it sets the honest claim:
  the guard protects **naive integrations** (small models, custom prompts, fine-tunes), not a well-aligned model
  against these payloads. Cost on an aligned model remains ~+8% tokens.
  We keep the defenses (untrusted-content wrapping, egress secret block, injection guard)
  because the fixed suite and literature justify them, but we do **not** add adaptive-specific heuristics on the
  strength of a negative-result benchmark. A weaker target model is the next measurement that would be informative.

## 3a. Daemon leases and budgets (2026-11-25)

- **Multi-worker safety via SQLite leases.** Claiming uses `BEGIN IMMEDIATE` plus a
  `WHERE status='pending'` guard, so two workers on the same DB can never execute one goal twice. Each claim records
  `worker_id` and `heartbeat_at`; the worker renews the lease every `stale_claim_seconds/3` while a goal runs, and
  `recover_stale` keys liveness on `heartbeat_at` (falling back to `updated_at` for pre-lease rows). This is the
  standard heartbeat/lease pattern used by queue workers (Celery/Redis leases, Kubernetes leader election) and is
  the minimum needed for an always-on agent that restarts. Measured effect: offline test claims 20 goals across two
  queues with zero duplicates; a live lease is never recovered while a crashed one is recovered exactly once.
- **Per-run caps + priced cost.** Token accounting existed but not prices or per-goal limits. Added
  `max_run_tokens`, `max_run_wall_seconds`, `max_run_tool_calls` (stopping with `stop_reason="budget"` and a partial
  result) and a `[pricing]` table (model substring → USD per 1M input/output). Rationale: always-on agents need hard
  limits per goal, and cost is the metric operators actually budget. `argo metrics --cost` reports USD total and per
  run. Verified by 5 unit tests (token, wall-clock, tool-call caps, longest-prefix pricing, summarize cost).

## 3b. Observability depth and MCP completeness (2026-11-25)

- **Per-tool p50/p95 + tokens per tool call + `--since`.** `argo metrics` already reported max latency and error
  rate; operators debugging a long run need percentiles and a time window. Added `tools.by_tool[name] =
  {calls, p50, p95, mean, max}`, `tokens_per_tool_call`, and `--since 90m|24h|7d|YYYY-MM-DD` (events are filtered by
  their own `ts`, so a single JSONL spanning a window is handled correctly). Percentile uses the nearest-rank
  method on recorded durations. Verified by 3 tests.
- **MCP resources and HTTP/SSE transport.** Tools-only stdio was the last gap versus the MCP spec. `connect()` now
  picks `stdio` / `streamable_http_client` / `sse_client` from `transport`, lists `resources`, and the agent exposes
  one read-only `mcp__<server>__read_resource` tool per server (so non-callable context — docs, records, files — is
  reachable from the loop), lists prompts and exposes `mcp__<server>__get_prompt`. Verified with the stdio fixture (resource read + agent registry) and a real in-process
  uvicorn streamable-HTTP server (`tests/test_mcp_http.py`). Mutating MCP tools still route through the approval
  queue via `MCPServerConfig.read_only=False`.

## 3c. Plan persistence across sessions (2026-11-25)

- **Approved plans survive session resume; scope changes re-prompt.** In plan mode, `propose_plan` approval only
  lived in the current run's `ToolContext`, so resuming a session re-entered read-only mode and the user had to
  re-approve the same plan. `Session` now records `plan_approved` with the approved action list; loading a session
  restores it and the agent seeds `plan_approved` into the tool context. A resumed `propose_plan` whose requested
  actions are a **subset** of the approved set is reused without a prompt (`plan_reused` event); any new action
  (scope change) falls through to a fresh approval. This mirrors Claude Code/Codex batch-approval semantics where
  approval is tied to a declared action surface, not to a single turn. 2 tests (resume reuses approval; scope change
  re-prompts).

## 3d. SWE-bench test feedback (2026-11-25)

- **Give the agent the project's own failing tests.** The first Argo SWE-bench attempt could not run tests, so its
  only feedback was reading code — the harness's strongest loop was disabled. Added `--install-deps`: editable
  install (`pip install -e .` + pytest) in the checkout, then the prompt is upgraded from "suites are not installed"
  to the exact `FAIL_TO_PASS` pytest command with an iterate-until-green instruction. `fail_to_pass()` /
  `test_command()` normalise the dataset's JSON-string or list formats and target only the graded node ids (no
  whole-suite runs). Unit-tested offline; a live graded run is in progress and its number will be recorded here.

## 3e. Loop guard: novel shell output is progress (2026-11-25)

- **Bug found by the SWE-bench run.** The agent explored with `bash` (grep/sed) for 10 steps and was killed by the
  no-progress guard with a 0-line patch, even though every command returned new information. The guard only counted
  typed `read_file` (novel path), mutations, and external fetches. Fix: a successful `bash`/`run_code` result whose
  output hash has not been seen this run is progress; identical repeated output (the verify-loop case) still is not.
  This is the "new information = progress" definition the guard was always aiming at, now extended to the tool the
  agent actually uses for exploration. Tests: novel outputs run past the 10-step limit; identical outputs stop at 11.
- **SWE-bench feedback also needed the official test patch.** Resolving bare test names to `test_sets.py -k ...`
  was not enough: at the base commit those tests pass because the *test patch* had not been applied. `apply_test_patch`
  now applies it and commits it as the baseline (so the later diff is source-only, and extra agent edits to tests are
  still flagged as tampering), installs `setuptools` (Python 3.12 removed `distutils`, old sympy needs it), and the
  prompt carries the absolute-interpreter pytest command. Verified on the clone: `2 failed, 1 passed` before the fix.

## 3f. Measured outcomes: SWE-bench test feedback and NFCorpus (2026-11-25)

- **SWE-bench: 0 -> 1 resolved.** `sympy__sympy-16988` went from a 0-line patch (`no_progress`, 22 iter, 464k tok,
  not graded) to **resolved 1/1** with `--install-deps`: FAIL_TO_PASS 2/2, PASS_TO_PASS 72/72, 17 iterations,
  228,926 tokens, patch applied cleanly by the official harness. Mechanism: the agent can *execute the graded tests*,
  so the loop becomes edit -> observe failure -> fix instead of reading code. Token use roughly halved despite
  finishing the job. n=1: this proves the mechanism, not a pass rate. Grading used the official
  `swebench.harness.run_evaluation` with the prebuilt image, then `--clean True` removed it.
  **Update:** two more instances (`sympy__sympy-20154`, `sympy__sympy-20590`) were generated and graded the same way
  -> **both resolved** (2/2 and 1/1 FAIL_TO_PASS, 40/40 and 21/21 PASS_TO_PASS). Overall **3/3 resolved** on the
  sympy subset. `20590` used all 40 iterations / 745k tokens, so the guard + test loop is what let it converge. n=3,
  same repo: establishes the mechanism and a first pass rate, not a Lite-wide number.
- **NFCorpus retrieval replication.** The production recipe's core holds on a second corpus: dense beats BM25 on
  R@10/R@100/nDCG and rerank-fused-top-20 gives the best nDCG (0.4451 vs 0.3936 dense / 0.3486 BM25). The SciFact
  claim that RRF maximizes Recall@100 did **not** replicate (dense 0.4278 > RRF 0.3780 on NFCorpus), so we will not
  change the production default to "always RRF for recall"; reranking is the transferable win. `--corpus` now selects
  scifact/nfcorpus.

## 3g. Cheap-model routing and quota pacing (2026-11-25)

- **Cheap model for compaction/handoff, not the control loop.** Routing the *main* loop per turn risks cache
  misses and incoherent edits; routing the summarization calls is the pattern Claude Code uses for subagents. Added
  `models.cheap` (or `--cheap-model`) which is passed as `model=` to the compaction summary and the reset handoff
  only. When unset, behaviour is unchanged. 2 tests (routing on/off). Subagent model routing is not enabled because
  our subagents edit code; a review-only cheap model is the safer follow-up.
- **`low_priority_pacing`.** A `--low-priority` switch turns into an optional delay before every provider call, so
  a background benchmark does not starve an interactive session on a shared quota. Implemented centrally in
  `LLMClient._pace()` used by `_post` and both streaming paths; 1 test.

## 3h. Memory at scale: two dream generations (2026-11-25)

- **Finding.** 24 synthetic lessons across 5 sessions (duplicates every 3rd, conflicting intermediates every 5th):
  no-dream correct 0.42, one generation 0.42 (no gain), two generations **1.00** with 0 contradictory-stale hits.
  The 1→2 pass superseded 2 and merged 6 items, i.e. it is reconciliation, not duplicate creation. Rule adopted:
  two consolidation passes when a cycle covers >~20 lessons, one pass for small cycles. Cost: one extra dream call.

## 3i. Summary-prompt A/B harness (2026-11-25)

- **Three compaction prompts behind one switch.** The live 36-step run suggested summaries lose exact values while
  the ledger patches around it. Added `summary_prompt_variant`: `default` (current structured), `identifiers`
  (one bullet per tool result, identifiers copied verbatim), `twopass` (extract a raw fact list, then compress it).
  `context_live.py` gains matching modes (`prompt_ids`, `prompt_twopass`) and a `--probes` selector so shorter,
  cheaper runs can still ask multiple recall questions. Offline tests pin the prompt text, the extract->compress
  call order, and the fact hand-off.
  **Measured (18 steps, 12k window, probes 3/10/17):** default 3/3 @87,320 tok / 2 compactions; `identifiers`
  3/3 @86,490 tok / **1 compaction**; `twopass` 3/3 but **0 compactions** — its two-call summaries kept failing
  under shared-quota 429s and the breaker disabled compaction (the run still fit the window). Decision: prefer the
  identifier-preserving prompt (same recall, fewer compactions, cheaper); do not adopt two-pass in this harness.
  **36-step confirmation (2026-11-27, probes 3/20/35):** default **3/3 @279,097 tok / 3 compactions**;
  `identifiers` **3/3 @235,636 tok / 5 compactions** (identical recovered ids). So the token saving grows with run
  length (**−1% at 18 steps → −15.6% at 36**), but the *compaction-count* win does not replicate — the identifier
  summary is longer, so the window refills sooner. Wall time (868 s vs 1,634 s) is confounded by shared-quota 429s
  and is not evidence. Decision: keep `default` as the default (no replicated mechanical benefit for short runs),
  and recommend `summary_prompt_variant="identifiers"` for long (>~30-step) runs where the token saving reaches
  double digits with recall at ceiling.

## 3j. SWE-bench environments must come from the official specs (2026-11-26)

- **Problem found broadening beyond sympy.** A blind `pip install -e .` resolves *today's* dependencies and breaks
  old checkouts: Flask 2.x imports `werkzeug.urls.url_quote`, removed in Werkzeug 3, and Flask's conftest calls
  `pytest.monkeypatch.notset`, removed in pytest 8 — both on Python 3.12. Old code therefore failed for reasons that
  had nothing to do with the bug under test.
- **Fix: reuse the official harness recipe.** `official_spec()` reads `MAP_REPO_VERSION_TO_SPECS` + `make_test_spec`
  from the installed `swebench` package; the version key comes from the dataset's base commit. Per instance we
  create an isolated venv **outside the repo** with the spec's interpreter (`uv venv --python 3.9/3.10/3.11`),
  install the spec's pinned `pip_packages`, pin pytest from the official requirements snapshot, then
  `pip install -e . --no-deps`. The test wrapper points at that venv interpreter. This keeps the host venv untouched
  and avoids untracked files in the prediction diff.
- **Verified** by smoke tests before any agent run: flask-4992 reproduces
  `TypeError: Config.from_file() got an unexpected keyword argument 'text'`; flask-4045 reproduces its two
  blueprint failures; xarray-4094 reproduces the stacked-array failure. Unit tests cover spec parsing and fallback.

## 3k. Report-level citations (ALCE-style) and OTel export (2026-11-26)

- **R3: citation precision/recall at report level.** The existing citation benchmark labels claims we wrote; it does
  not test a generated report. `citation.py --report` now generates a 4-6 bullet executive report from six sources
  with `[S#]` citations, parses each line into (text, refs), verifies every cited line against its cited sources, and
  scores ALCE-style **citation precision** (supported / cited lines) and **recall** (required sources cited). Uncited
  lines are reported separately. Helpers are unit-tested offline; a live run is in progress.
- **OTel export.** `argo export-otel SESSION` converts a session JSONL to OTLP/JSON spans using the GenAI semantic
  conventions: one `invoke_agent` root span per run with `gen_ai.usage.*`/model/stop attributes, `execute_tool <name>`
  child spans, and compaction/loop events as span events. No protobuf dependency; deterministic ids; unit-tested.

## 3l. R4: exact-answer deep research (2026-11-26)

- **Why a second research benchmark.** Report-level citation scoring (§3k) tests grounding but not whether the agent
  can *find and combine* facts. `benchmarks/deep_research.py` is a GAIA/BrowseComp-style subset: 8 deterministic pages
  served from disk (including a stale draft and an unrelated distractor), 6 questions with exact answers, two of them
  multi-hop (team → lead; Q3 + Q4 revenue). Scoring normalises money/commas/case and requires a final `ANSWER: <value>`
  line, so the metric is exact-match, not prose similarity.
- **Cached pages** keep the measurement about research/synthesis, not network flakiness: the server always serves the
  same bytes and the self-check verifies the 404 path and the scorer offline.
- **Result: 6/6 exact answers, 69,325 tokens.** The first run scored 5/6 at 311,650 tokens: the miss was a *discovery*
  failure — the model guessed `/docs/alpha-old-2`, 404'd, retried variations and exhausted `max_iterations` (101 tool
  calls). Serving a `/docs` index and instructing the agent not to guess URLs fixed it and cut tokens 4.5×. Lesson:
  make the corpus enumerable at the entry point rather than relying on the model to infer URLs; this is the same
  principle as giving Claude Code a workspace listing or a retriever a candidate set.
- **Grader bugs matter as much as agent bugs.** The fixture originally named the decoy `/docs/distractor` (a label
  leak) and the scorer matched answers as substrings, so a refusal quoting "$9.9M" counted as correct. Fixture paths
  are now role-neutral, refusals are rejected explicitly, and a value counts only if the reply is the value plus at
  most a short gloss. Both are unit-tested; a benchmark that can be gamed by hedging is worthless for comparing runs.

## 3m. Coverage plan: enforced decomposition for multi-hop research (2026-11-26)

- **Mechanism.** `plan_research(question, subquestions)` stores a `CoveragePlan`; `mark_covered(sub_id, source_ids)`
  marks hops as answered and rejects ids that were never recorded with `add_source` (no fake coverage). The agent
  loop refuses to accept a text-only final answer while any sub-question is uncovered, appending the rendered plan
  as a reminder — capped at two nudges so a stalled plan cannot loop. This copies the open-todo enforcement that
  Codex/Claude Code use for plans, and mirrors the decomposition step in IRCoT (Trivedi et al., 2023) and
  Self-RAG: multi-hop QA improves when each hop is retrieved for explicitly, rather than one fused query.
- **Evidence (hard set, 4 three-hop questions, 9 cached pages).** Single pass **4/4** at 67,177 tokens / 223 s;
  with the coverage gate **4/4** at 124,303 tokens / 541 s (coverage 1.00 on every question). The
  decomposition gate costs **+85% tokens** and 2.4× wall for **zero accuracy gain** on a strong model
  with a tiny enumerable corpus.
- **Decision.** Ship it as opt-in (`--coverage` in the benchmark; the tools are always registered and inert unless
  the model calls them). Rationale: the mechanism is a safety net for weak models or big corpora where a single
  pass skips a hop, but enabling it by default doubles research cost at the ceiling — the same pattern as the
  injection suite and mandatory review, where a strong model already does the right thing unaided.
- **What would change the decision.** A hard set where single-pass drops below ceiling (more hops, noisier corpus,
  or a smaller model) — then measure whether the gate recovers accuracy, not just cost.

## 3n. Computer use: self-healing selectors and an interactive-element fallback (2026-11-26)

- **Problem.** Replayed workflows and agent clicks both die on selector drift — the dominant failure mode of UI
  automation once a page is redeployed. Playwright's own guidance (prefer role/test-id locators over CSS ids) fixes
  authoring, not the thousands of already-saved workflows.
- **Mechanism.** `click`/`type`/`wait` resolve through `_resolve()`: exact selector → attribute variants
  (`data-testid`, `aria-label`, `name`, `placeholder`, `title`, then substring matches on the selector's words) →
  role/name → visible text. A heal is recorded in browser state and emitted as an event. If nothing matches, the
  action fails with a 25-row list of interactive elements (`tag#id[data-testid=…] | label`), so the model corrects
  in one turn rather than guessing.
- **Evidence.** Deterministic replay of a stale login workflow: plain CSS **1/2 steps** (dies on the first renamed
  field) vs self-healing **4/4 steps, 3 heals**, +1.1 s wall. Live agent on the same drifted site: balance correct,
  **0 unsafe actions**, 64,309 tokens / 132 s, and **0 heals** — the DOM summary let it choose stable locators
  itself.
- **Decision.** Keep healing on by default (cheap, deterministic, and only fires on mismatch) and always return the
  interactive-element list on failure. Do not rely on healing to fix LLM steps: the model adapts from the DOM view;
  healing exists for saved workflows, where there is no model in the loop.
- **Update: `select` action + three-task subset.** The form task exposed a real gap — Playwright's `fill` cannot set
  a `<select>`, and the agent resorted to `eval` and ran out of budget. Added `select` (`select_option` by value or
  label, with the same drift healing as `click`); the task then passed. The suite now has **3 tasks** (drifted login,
  decoy-table lookup, exact form submission) and reports an **unsafe-action rate**: `3/3` success, **0 unsafe
  actions**, 88,686 tokens / 156 s (`benchmarks/results/computer_use_20261126-232639.json`).
- **Specification cost.** The first passing form run cost 155,491 tokens because the prompt omitted the form URL;
  naming `{base}/tickets` cut it to 32,305. Entry-point specification matters as much as tooling (same lesson as the
  `/docs` index, §3l).

## 3o. Per-turn model routing: explore cheap, escalate on risk (2026-11-26)

- **Literature.** RouteLLM (Ong et al., 2024) shows a router can keep most strong-model quality at a fraction of the
  cost; FrugalGPT (Chen et al., 2023) gets similar wins with a *cascade* and a verifier. A trained router needs
  labelled cost/quality pairs we do not have, so this is the explainable half: a policy over observable signals.
- **Policy `explore`** (`argo/core/router.py`, `--router explore --cheap-model M`): the turn runs on the cheap model
  only while the task is read-only *by intent* and no mutation or tool error has occurred; a write-intent task, any
  turn after a mutating tool, and any turn after a failed tool use the strong model. Ambiguous tasks fail safe to
  strong. The per-turn choice is emitted as a `model_route` event and counted in `agent.router.stats`.
- **Evidence 1 (research, cheap vs strong).** The 4-question 3-hop set on `qwen3.8-flash-next` scored **4/4**
  at 57,549 tokens / 172 s vs the strong model's **4/4** at 67,177 tokens / 223 s — 14% fewer tokens,
  24% faster, same accuracy. Read-only research is genuinely safe to route.
- **Evidence 2 (live smoke, both arms correct).** Read-only "report revenue growth": explore arm used 2/2 cheap turns,
  5,234 tokens, answer correct; strong arm 5,063 tokens, answer correct — *token parity on a 2-turn task*, so token
  count is not a cost proxy when the cheap model is chattier. The write-intent variant ("write growth.txt") correctly
  used **0 cheap turns** (4 strong), and a read-only task whose trajectory mutated escalated after the write (tested).
- **Decision.** Ship `--router explore` as opt-in, off by default. It is safe (no quality loss in 5 live tasks) but the
  savings depend on the provider's price ratio, which this endpoint does not publish; measure `argo metrics --cost`
  with a `[pricing]` table before turning it on generally. Writes never start on the cheap model.

## 3p. Served live trace view (2026-11-26)

- **Why.** `argo trace` and `argo metrics` are CLI-only; an always-on deployment needs a browser-visible timeline
  to answer "why did this run stop?" without shell access. This closes the last observability item from §3k/§2.
- **What.** `argo serve` gains `GET /trace` (session index: stop reason, iterations, tokens, tool calls/errors),
  `GET /trace/<id>` (auto-refreshing timeline with per-tool call counts, time and errors) and JSON twins
  `GET /v1/sessions`, `GET /v1/sessions/<id>`. `--api-key` protects them (`?key=` for pages, bearer for JSON);
  session ids are validated against path traversal and all rendered text is HTML-escaped.
- **Panel semantics.** Token totals read both event shapes (`total_tokens` from the live bus, `total` from the
  session JSONL) — a bug the first smoke test against real sessions exposed, now covered by a test.
- **Evidence.** 4 offline tests (`tests/test_server_trace.py`): summary math (both usage shapes), 404/traversal,
  HTML escaping of an injected `<script>`, key enforcement. Verified against real sessions on this machine
  (e.g. a 4-tool-call run summarised at 29,655 tokens with per-tool breakdown).

## 3q. Trap tasks: review cannot be shown to improve generalization (2026-11-26)

- **Question.** `review_work` and `spawn(..., review_changes=true, repair_attempts=N)` are implemented, but every
  prior run was on tasks the baseline already solved (5/5), so the loops had no headroom. Do they catch a fix that
  passes the visible tests but over-fits them?
- **Setup.** Two trap tasks: `slugify` (visible tests use single spaces only; hidden tests add repeated spaces,
  padding, punctuation and symbols) and `intervals` (one visible overlap; hidden adds disjoint, touching, unsorted,
  empty). Both validated by `--self-check` (buggy fails visible and hidden; reference fix passes both).
- **Result.** Baseline resolved **2/2** — the model wrote `re.sub(r"[^a-z0-9]+", "-", …)` and the sorted merge
  directly, without over-fitting. Review mode also resolved 2/2 at −12% tokens but **3.0× wall** (511 s vs 172 s),
  and one review run hit the 600 s cap (resolved anyway, `completed` recorded 1/2).
- **Weak model did not help either.** `gemma-4-31b` also resolved **2/2** on the traps (baseline 100,777 tok /
  14.5 iters; review 96,999 tok / 14.0 iters, one 420 s cap). Both models derive the general fix from one visible
  example, so the trap *design* is what fails, not the reviewer. Side finding that supports §3o: the weaker model
  needed **~50% more tokens and ~1.7× the iterations** for the same code task — another reason coding turns stay
  on the strong model while read-only research may route cheap.
- **Decision.** Keep review opt-in/adaptive for high-stakes artifacts; do not recommend it for routine fixes. Record
  the measurement as *no headroom*, not as a review win or loss. Closing it properly needs harder tasks (multi-file
  specs, real repos) where the baseline fails, plus the worktree `repair_attempts` arm rather than a single review.

## 4. Deliberately not done (yet)

- A graded SWE-bench-Lite score (the runner and Docker path exist; only a gold-patch check has been run, because
  each image pull plus test run consumes the same rate-limited machine).
- Full-corpus BEIR / MTEB evaluation and rerank-depth ablations.
- Distributed daemon leases and MCP **resources/prompts** (tools only today).
- WebArena/OSWorld-scale computer use; current evidence is a deterministic local form and workflow replay.
- Multi-model variance and confidence intervals; all numbers are one model, one run per task.
- Adaptive prompt injection (AgentDojo proper) and a live 40–60-step context run; current safety and context
  evidence comes from a local fixed suite and a synthetic transcript.

## 5. Reference implementation review

The leaked Claude Code tree (see [`REFERENCE_REVIEW.md`](REFERENCE_REVIEW.md)) was reviewed for architectural
patterns only; no code was reused. It validated Argo's tool/approval/skill/memory design and identified four
concrete gaps for future work: MCP client, worktree-isolated subagents, plan mode with batch approval, and
in-session background tasks. Deep research is tracked separately in [`ROADMAP.md`](ROADMAP.md).

## 6. How to reproduce

```bash
pytest -q
python benchmarks/humaneval.py --limit 20 --verbose
python benchmarks/retrieval.py --queries 100 --docs 1000
python benchmarks/memory_loop.py --k 3
python benchmarks/workflow_replay.py --trials 5
python benchmarks/repair.py --self-check
python benchmarks/repair.py --modes baseline,review
python benchmarks/provider_parity.py
python benchmarks/injection.py --modes off,on
python benchmarks/context_bench.py --policies full,microcompact,autocompact_75,autocompact_95,reset
```
