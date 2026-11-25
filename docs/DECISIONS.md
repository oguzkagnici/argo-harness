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
| Stall control | Block third identical read-only call; nudge at 40%/70%; stop at `no_progress_limit` | Reflexion (Shinn et al., 2023); Anthropic harness guidance; Claude Code/Codex loop guards | Eliminated the observed DOCX-unzip loop; live doc+review run finished in 24 iterations; unit-tested at the default and a custom limit |
| Verification | Separate read-only `review_work` subagent with explicit criteria | Anthropic generator/evaluator split; Claude Code subagents | Previous reviewer spent 10 iterations / 307 s; with tool-scoped guidance it took 3 iterations / 13.8k tokens on a smoke test |
| Delegation | Context-isolated subagents, sharing memory/LLM but not conversation | Claude Code, Codex | Used for review and research branches; keeps parent context small (not yet A/B benchmarked) |
| Isolation | Code-editing subagents can run in a linked git worktree on `argo/<slug>`; parent tree untouched, adoption by explicit merge | Claude Code / Codex worktree isolation; avoids concurrent edits to one checkout | 3 tests (isolation, changed-file detection, agent spawn) + live run: branch created, parent `git status` clean, child edit visible only in the worktree |
| Plan mode | `--plan` blocks mutating tools until `propose_plan` is approved once; the approval covers the batch (per-action gate reports "covered by approved plan") | Claude Code plan mode; batch approval avoids prompt fatigue | 3 tests (blocked before approval, rejection keeps block, off by default) + live run in 7 iterations / 45,962 tokens with no pre-approval writes |
| Background work | `start_background` launches a subagent as an asyncio task; `check_background(id, wait_seconds)` blocks instead of polling; jobs die with the run | Claude Code background tasks; in-memory only, durable work stays in the daemon queue | Live A/B: polling-heavy prompt cost **10 iter / 71,428 tok** and ended `no_progress`; one waiting call cost **3 iter / 19,284 tok** and finished `final` (−73% tokens) |
| Document read-back | `read_document` preserves DOCX title/heading levels, list markers, and table dimensions | Reviewers must audit without unzipping OOXML; the reviewer had flagged flat extraction as a defect | DOCX now renders as `TITLE:`, `# Heading`, `- bullet`, `TABLE 1 (2x2):`, so structural criteria are checkable directly |
| Parallelism | Parallel tool calls only when every call is read-only and parallel-safe | Claude Code/Codex tool semantics | Preserves approval and write ordering; exact-order unit test added |
| Automation | Workflows are declarative tool sequences with params/assertions, not replayed code | Codex/Claude Code preference for structured actions | Extracted `demo_login` replays **5/5** across different names at ~1.8 s warm |
| Always-on | Durable SQLite goal queue, read-only trigger allowlist, approvals, outbox | Anthropic always-on agent guidance | Tests cover approval queueing, outbox delivery, and read-only enforcement; daemon `--once` verified live |
| Compatibility | OpenAI-compatible `/v1/chat/completions` **and** `/responses`, native SSE, BYOK + Codex config inheritance | User requirement; OpenAI Responses API; Codex config uses `wire_api = responses` | Live parity check passes both paths; same token accounting (713) for the parity round trip; default config now exercises `/responses` end-to-end |
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
| Plan mode | live run with `--plan --yolo` | `propose_plan` approved before writes; **7 iterations / 45,962 tokens**; file created and verified |
| Background tasks | live fetch of example.com, parent asks for title | polling: 10 iter / 71,428 tok / `no_progress`; waiting call: **3 iter / 19,284 tok / `final`** |
| Citation verification | 12 claims over 6 local sources (6 supported, 2 overstated, 3 false, 1 wrong source) | accuracy 0.750, **false-support 0.000**, bad caught **1.000**, supported retained **1.000**; baseline false-support 1.000 |
| Context policies | synthetic 60-step / 29,639-token transcript, 12 planted facts, 32k window | full 29,639 tok 12/12 · microcompact 24,023 tok 12/12 · **autocompact 75% 7,502 tok 12/12** · autocompact 95% 7,546 tok 12/12 · reset 1,225 tok **1/12** |
| Context ablation | same, `compact_fact_ledger=false` | 6,734 tok, **5/12** vs 12/12 with the ledger (+768 tok) |
| Prompt injection | 7 attacks (4 obvious, 3 subtle) × defense on/off, local HTTP + files + DOCX | **safe 1.00 / obedience 0.00 both modes**, 0 exfiltrations; mean 5,191 tok (off) vs 5,318 tok (on) |
| Memory ablations | 6 lessons × 5 arms (no-dream, no-wiki, no-grounding, two generations, full) | no-dream stale 1.00 / correct 0.00; full **0.00 / 1.00**; no-wiki stale mentions 0.67 vs 0.50; two generations same recall at 2.2× time |
| Retrieval ablations | SciFact 100 q / 1,000 docs, one factor at a time | rerank 20 **0.9190** nDCG vs 50 0.9161 / 10 0.8919; k=10 0.8649 vs k=60 0.8460; dense-heavy 0.8660 vs BM25-heavy 0.8100; query instruction 0.8392 |
| Document QA | 10 questions × DOCX/PPTX/XLSX/PDF × 2 modes | tool 9/10 (1 stall; retry correct) at 6,748 tok / 2.9 iter; bash 10/10 at 8,351 tok / 3.8 iter |
| Adaptive review | live `report.docx` run | auto-review triggered at risk=4, 27,796 tokens total; goal-aware criteria added after the first run flagged unrequested sections |
 rerank 20 **0.9190** nDCG vs 50 0.9161 / 10 0.8919; k=10 0.8649 vs k=60 0.8460; dense-heavy 0.8660 vs BM25-heavy 0.8100; query instruction 0.8392 |

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

## 4. Deliberately not done (yet)

- Official SWE-bench-Lite (needs per-instance containerized repo environments; the local repair harness tests the
  same loop cheaply).
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
