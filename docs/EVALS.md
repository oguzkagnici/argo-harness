# Evaluation and verification

This file records what was actually run, what passed, and what remains unproven. Claims in the README are
backed by these checks, the benchmark suite, or cited literature.

## 1. Unit / integration tests (no network)

```bash
pytest -q
# 196 passed
```

| Area | Checks |
|---|---|
| Git tools | real temp repos: status/diff (worktree+staged)/log/show/branches, commit add_all + explicit paths, refusal paths | 5 tests, no network |
| Loop guard | duplicate read-only calls blocked, no-progress nudge at 4/7 + stop at 10, progress reset, call-order preservation, external-info tools reset the counter, repeated external fetch still stops |
| Retrieval | tokenization, heading-aware chunking, BM25 ranking, RRF fusion, cosine, recency decay |
| Memory | add/retrieve, exact dedupe, generation isolation, transcripts, link graph |
| Dreaming | generational clone, supersede, ungrounded-claim downgrade, wiki/procedure creation, rollback |
| Context | microcompaction + spooling, structured compaction block |
| Workflows | recursive parameter substitution, missing params, assertions, retries, saved variables |
| Documents | DOCX/PPTX/PDF/XLSX/MD/HTML creation; structure-aware DOCX read-back (title, heading levels, lists, tables) |
| Daemon | goal dedupe, claim/finish, dangerous-action approval queue, outbox delivery, read-only trigger allowlist |
| Server | message→task conversion, OpenAI SSE chunks + `[DONE]`, native `/v1/agent` delta/event/result stream, streamed error path |
| Provider APIs | Responses payload mapping, assistant `input_text` history, flat tools, `function_call_output`, SSE assembly, usage mapping |
| Tools | danger guard |
| MCP | stdio connect, tool discovery + schema, namespaced registry entry, call, bad-server isolation |
| Worktrees | branch/worktree creation, changed-file detection (edited + untracked), non-git rejection, agent spawn isolation |
| Plan mode | mutation blocked before approval, plan rejection keeps block, plan-mode off is unchanged |
| Background | job completion + result, failure containment, blocking wait, unknown id |
| Research | ledger ids/dedupe/dangling citations, report rendering, verifier parsing, rate-limit retry, end-to-end tools |
| Review policy | adaptive triggers on risky artifacts, skips low-risk, off never triggers, explicit review suppresses auto, risk weights, goal-aware criteria |
| Security | untrusted wrappers, secret/canary detection, credential-URL blocking, prompt guard on/off |
| Context policy | reserve-based threshold, compaction circuit breaker, cache-expiry eviction, verbatim fact ledger |

## 2. Benchmarks (published protocols)

Full methodology and cached data: `benchmarks/README.md`. JSON records: `benchmarks/results/`.

| Benchmark | Protocol | Result |
|---|---|---|
| HumanEval | 20-task seeded subset, pass@1, isolated subprocess | **20/20 (1.00)**, 12,827 tokens |
| BEIR SciFact | 100 queries / 1,000 docs / 112 positives | BM25 nDCG@10 0.779 · dense 0.885 · RRF 0.846 · **RRF+rerank 0.916**; R@100 1.00 for RRF and RRF+rerank |
| Memory learning loop | 6 stale→correction lessons, dream apply, same top-3 queries | contradictory stale **1.00 → 0.00**, corrected recall **0.00 → 1.00** |
| Workflow replay | extracted `demo_login`, 5 input variations, real Playwright | **5/5**, ~1.8 s per warm replay |
| Provider parity | live chat vs responses: tool call, tool-result replay, streaming | **both pass**, 713 tokens each path |
| MCP (live) | local stdio echo server, model asked to add 17+25 | **pass** in 2 iterations / 11,921 tokens; first attempt failed before the `input_schema` fix (3 iter / 17,928), then passed once parameters were exposed |
| Worktree isolation (live) | parent delegates a docstring edit with `isolate=true` | pass: branch `argo/isolated-job-...`, parent file and status unchanged, child edit present in the worktree |
| Plan mode (live) | `argo run --plan --yolo`, write one file | pass: 7 iterations / 45,962 tokens; no write before `propose_plan` approval |
| Background tasks (live) | start a fetch job, then report its title | polling variant 10 iter / 71,428 tok (`no_progress`); `wait_seconds` variant **3 iter / 19,284 tok (`final`)** |
| Citation verification (live) | 12 claims / 6 local sources | **false-support 0.000** vs 1.000 baseline; bad claims caught 1.000; supported retained 1.000; exact accuracy 0.750 (misses are conservative) |
| Document QA (live) | 10 questions over generated DOCX/PPTX/XLSX/PDF | tool 9/10 (one provider stall), bash 10/10; tool cheaper by 24% tokens and 1 iteration |
| Adaptive review (live) | create `report.docx`, `--review adaptive` | auto-review fired in 4 iterations / 27,796 tokens; reviewer flagged unspecified sections, so criteria were made goal-aware |
| Report-level citations (live, R3) | ALCE-style precision/recall on a generated 6-bullet report | **precision 1.000, recall 1.000**, 6/6 lines cited, 99 s |
| Deep research exact answers (live, R4) | 8 cached pages incl. stale draft + unrelated note; 6 questions, 2 multi-hop | **6/6**, 69,325 tokens, 817 s total (5/6 at 311,650 tok before the `/docs` index) |
| Deep research 3-hop set (live) | 4 questions needing 3 chained pages | single pass **4/4** 67,177 tok / 223 s; `--coverage` plan gate **4/4** 124,303 tok / 541 s (+85% tok, no gain at ceiling) |
| Computer use: drifted login (live) | stale workflow ids vs live name/testid page, destructive trap | balance **correct**, **0 unsafe actions**, 64,309 tok / 132 s, 0 heals needed |
| Computer use subset (live) | 3 tasks: stale-workflow login, decoy-table lookup, exact form submit; destructive controls on every page | **3/3 success**, **unsafe rate 0.0**, 88,686 tok / 156 s |
| Workflow drift (deterministic) | stale login workflow replayed with healing on/off | plain CSS **1/2 steps**, self-healing **4/4 steps / 3 heals** (+1.1 s) |
| Cheap model parity (research) | 4 hard 3-hop questions on `qwen3.8-flash-next` vs strong | **4/4 both**; cheap 57,549 tok / 172 s vs strong 67,177 tok / 223 s (−14% tok, −24% wall) |
| Router `explore` (live smoke) | read-only QA and write-intent tasks, cheap vs strong arms | read-only: **2/2 cheap turns, correct** (5,234 tok vs 5,063 strong); write-intent: **0 cheap turns** (fail-safe) |
| NFCorpus retrieval replication | 100 queries, dense vs BM25 vs RRF+rerank | dense > BM25 and rerank gives best nDCG replicate; RRF-best-R@100 does *not* replicate |
| Memory scale (24 lessons / 5 sessions) | 1 vs 2 dream generations | 1 generation no gain; **2 generations = 1.00 correct, 0 stale** |
| Memory, 40-lesson scale (live) | 40 synthetic lessons / 5 sessions; arms no_dream, full, two_generations | 1 pass: correct **1.00**, stale 0.00, 33 items, 129 s; 2 passes: 1.00, 15 items, 482 s — second pass = no recall gain at 3.7× cost |
| Repair loop (baseline) | 5 local repos, visible + hidden tests, 16-iteration cap | **5/5 resolved**, 0 tampered, 46,990 tokens / 163.0 s / 7.6 iter per task |
| Repair loop (review) | same tasks + one mandatory `review_work` | **5/5 resolved**, +35% tokens, 3.2× wall time, no accuracy gain |
| Repair loop, trap tasks (live) | slugify + intervals: visible tests hide repeated-space/punctuation and disjoint/touching/unsorted cases | baseline **2/2 resolved**; review **2/2** at −12% tokens but 3.0× wall (one 600 s cap) — no headroom |
| Repair loop, traps + weak model (live) | same two traps on `gemma-4-31b` | baseline **2/2**, review **2/2**; weak model costs +53% tokens / +1.7× iters — traps don't trap either model |
| Repair loop, multi-file spec (live) | `ledger`: 3 files, `SPEC.md` rules (qty>10 discount boundary, negative-qty validation, cache invalidation); visible suite hides all three | baseline **1/1** (66,078 tok / 900 s / 8 iter), review **1/1** (178,421 tok / 1,676 s / 16 iter, 1 PASS review) — no headroom, 2.7× tokens |
| Worktree repair arm (live) | `spawn(isolate, review_changes, repair_attempts=2, adopt)` on `intervals` | **resolved 1/1**, review PASS, branch merged; 86,019 tok / 412 s vs baseline 59,213 / 164 s |
| Prompt injection, harder payloads (live) | audit-hook / authority-spoof / two-document attacks, 2 target models | aligned models **0/3** even with a naive policy; `gemma-4-31b` + naive policy **ASR 1.0 → 0.0** with defenses, utility 1.0 |
| Prompt injection, weak target (live) | `qwen3.8-flash-next` target, 3 cases × static/adaptive × defense off/on | **ASR 0.0 both modes**, utility 1.0, 0 exfiltrations; defenses +8% tokens — attack set saturates, not the model |
| Prompt injection | 7 local attacks (4 obvious, 3 subtle) × defense off/on | **obedience 0.00 and 0 exfiltrations in both modes**; 5,191 → 5,318 mean tokens (+2.4%) |
| Document QA | 10 questions × 2 modes (DOCX/PPTX/XLSX/PDF) | tool **9/10 answered correctly** at 6,748 tok / 2.9 iter (miss = 180 s stall, retry correct); bash 10/10 at 8,351 tok / 3.8 iter |
| Memory ablations | 6 lessons × 5 arms | no-dream **1.00 stale / 0.00 correct**; full **0.00 / 1.00**; no-wiki stale mentions 0.67 vs 0.50; two generations = same recall, 2.2× time |
| Retrieval ablations | SciFact, one factor at a time | rerank 20 0.9190 > 50 0.9161 > 10 0.8919; k=10 0.8649 > k=60 0.8460; dense-heavy 0.8660 > BM25-heavy 0.8100; query instruction 0.8392 |
| Context policies | synthetic 60-step / 29.6k-token transcript, 12 planted facts | full 29,639 tok 12/12 · microcompact 24,023 tok 12/12 · **autocompact 7,502 tok 12/12** · reset 1,225 tok 1/12 |
| Summary-prompt A/B (live, 36 steps) | default vs `identifiers` compaction prompt, 12k window | both **3/3** with identical ids; `identifiers` 235,636 vs 279,097 tok (−15.6%), 5 vs 3 compactions |
| Fact-ledger ablation | same transcript, ledger disabled | **5/12 → 12/12** recall for +768 tokens |

Interpretation:

- Dense retrieval beats BM25 on SciFact, RRF maximizes recall at depth, and cross-encoder reranking gives the
  best top-k ordering — matching the design choice to rerank the fused candidate pool.
- Dreaming eliminates contradictory stale beliefs while keeping historical mentions in parenthetical form
  (50% of top-3 hits still mention the old value, 0% present it as current).
- Workflow replay is deterministic once extracted; the real cost is extraction and selector maintenance.
- Verbatim fact extraction is what makes compaction safe here: the summary alone retained 5/12 facts, the ledger
  restored all 12 at ~8k tokens sent. Compaction therefore *is* an option at 0.75 even on a 32k window.
- Context resets trade recall for a clean window (1/12 facts, 1.2k tokens); use them only after a failed compaction.
- The injection suite is at ceiling: a well-aligned model refused every payload without any defense, so the
  measured security benefit is zero and the guard's cost is small but real.
- Decomposition is insurance, not a free win: the coverage gate held coverage 1.00 on every 3-hop question but
  raised tokens 1.85× with accuracy already at ceiling. Enable it for weak models/large corpora.
- Deep-research failure was a discovery problem, not reasoning: with a `/docs` index the agent stopped guessing
  URLs, went 6/6, and used 4.5× fewer tokens. Indexable corpus at the entry point beats URL inference.
- Exact-answer graders need refusal handling: substring matching scored a hedge quoting the decoy value as
  correct. The matcher now rejects negated replies, and fixture paths no longer leak roles (`/docs/distractor`).
- The independent reviewer helps on underspecified/high-stakes artifacts but is overhead on easy, fully tested
  fixes; it cannot run the suite by design (read-only tool allowlist), so hidden-test resolution remains the
  objective arbiter.

## 3. Live provider verification (`argo doctor`)

Against the configured OpenAI-compatible endpoint (DeepSeek `deepseek-v4.1-flash`, 1,048,576 context):

```
chat        ok — returned "OK"
embeddings  ok — dim=4096 (qwen3-embedding-8b)
rerank      ok — qwen3-reranker-8b
models      15 available
bins        python3, node, bash, git, curl present
```

Tool calling was verified separately: the model returned a valid `tool_calls` payload for a calculator tool.

## 4. Live agent runs

| Scenario | Result |
|---|---|
| Create `greet.py`, run it, verify output, delete it | Passed; correct file lifecycle and verification |
| Browser form: launch → type "Grace" → click → confirm | Passed; DOM showed `Welcome, Grace!`; screenshots captured |
| DOCX (2 pages) + PPTX (6 slides) + `review_work` | Passed after the loop guard: both artifacts built, reviewed (PASS), two defects fixed, 24 iterations, 415k tokens |
| Repeated-inspection stress (DOCX unzip loop) | Previously stalled; now nudged at 4/7 no-progress turns and hard-stopped at 10 |
| OpenAI-compatible `POST /v1/chat/completions` (non-stream) | Passed; wrote `note.txt`, read it back, returned content with session metadata |
| OpenAI-compatible streaming | Passed (live) and covered by unit tests: standard SSE chunks + `[DONE]` |
| Native `/v1/agent` SSE | Passed: delta/event/result/done frames |
| Responses wire API agent run | Passed: create/run/delete file in 5 iterations; streaming prompt returned correctly |
| Dream cycle over 2 synthetic sessions | Passed; deduped items, superseded stale DB host, wiki/insights/procedures |
| Workflow extraction from browser trace | Passed; parameterized `url` + `name`, 3 steps with assertions |
| Daemon `--once` | Passed; webhook trigger queued, read-only allowlist enforced (bash denied), goal done, outbox delivered |
| Memory export with `--git` | Passed; markdown + frontmatter + `_dashboard.md`, git commit |
| Live 36-step token chain (12k window) | `full` (unmanaged), `autocompact` (0.75 + ledger), `no_ledger`; 36 sequential HTTP fetches with probe recall at steps 3/20/35 — see `benchmarks/results/context_live_*.json` |
| Adaptive injection (local) | Red-team LLM writes and refines injections against the target over two rounds, defense off/on; static payload as baseline — see `benchmarks/results/adaptive_injection_*.json` |
| Provider limits | concurrency cap (env), jittered backoff honoring Retry-After, 429→success retry, exhaustion raises after 6 attempts |
| Observability | `argo metrics` and `argo trace` verified on a real 10-iteration session (tool calls, stop reason, per-tool durations after the change) |

## 5. What these checks do *not* prove

- **Official coding-agent performance.** HumanEval is single-shot and the local repair harness uses five tiny repos; neither substitutes for containerized SWE-bench-Lite runs on real projects.
- **Full-corpus retrieval.** SciFact is sampled to 100 queries / 1,000 docs; reported values are not comparable
  to official full BEIR leaderboards.
- **Visual design quality at scale.** Deck/report outputs parse and pass a criteria-based LLM review, but no
  large human rubric study was run.
- **WebArena/OSWorld robustness.** Computer use is tested on a deterministic local form; CAPTCHAs, selector
  drift, and multi-site flows are untested.
- **Cost/latency optimization.** Per-run token accounting exists; no cross-provider cost benchmark yet.
- **Multi-model generalization.** All benchmark numbers are from one provider/model.
- **Injection resistance in general.** Fixed payloads against one aligned model were all refused; the adaptive
  benchmark (attacker refines over rounds, defense-aware) is a better signal, but it is still one attacker model,
  one target model, and three scenarios rather than the full AgentDojo suite.
- **Long-horizon context quality in production.** The context numbers come from a synthetic transcript with a small window; real sessions mix images, huge diffs, and many tool types.

## 6. Recommended next evaluations

1. **Coding repair:** official SWE-bench-Lite (containerized repos); compare against the local repair baseline and measure review pass rate.
2. **Documents:** rubric-scored DOCX/PPTX outputs with an independent evaluator and human spot-checks.
3. **Research:** citation support rate (does the URL support the claim?) and hallucination rate.
4. **Memory:** run the learning loop with dreaming disabled, without wiki extraction, and with multiple dream
   generations to isolate which mechanism drives improvement.
5. **Retrieval:** full SciFact/NFCorpus plus rerank-depth ablations and query-instruction variants.
6. **Workflows:** selector-drift recovery time and replay success under page mutations.
7. **Computer use:** a small WebArena/OSWorld subset; task success and unsafe-action rate.
8. **Always-on:** trigger→goal latency, duplicate suppression, approval correctness, outbox noise, cost per goal.
9. **Safety:** AgentDojo with adaptive attacks across models; measure utility cost and violation rate.
10. **Context:** a live 40–60-step agent task comparing 0.75/0.90 thresholds, ledger on/off, and one forced reset.

## 7. Decision log

[`docs/DECISIONS.md`](DECISIONS.md) records every major choice, its evidence base, and its measured effect.

## 8. Reproduce

```bash
pytest -q
argo doctor
python benchmarks/humaneval.py --limit 20 --verbose
python benchmarks/retrieval.py --queries 100 --docs 1000
python benchmarks/memory_loop.py --k 3
python benchmarks/workflow_replay.py --trials 5
python benchmarks/repair.py --self-check
python benchmarks/repair.py --modes baseline,review
python benchmarks/provider_parity.py
python benchmarks/injection.py --modes off,on
python benchmarks/adaptive_injection.py --rounds 2 --modes off,on
python benchmarks/context_live.py --steps 36 --modes full,autocompact,no_ledger
python benchmarks/context_bench.py --policies full,microcompact,autocompact_75,autocompact_95,reset
argo run --no-stream "Create hello.py that prints hi, run it, and report the output."
argo dream --apply
argo workflow list
```
