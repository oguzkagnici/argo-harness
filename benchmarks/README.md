# Benchmarks

Harness-level evals with published protocols. Each script prints a summary and writes a JSON record under
`benchmarks/results/`. Numbers below were produced on 2026-11-24 against `deepseek-v4.1-flash` with
`qwen3-embedding-8b` / `qwen3-reranker-8b`.

## 1. HumanEval (code generation)

Protocol: OpenAI HumanEval (Chen et al., 2021). The model completes the canonical prompt; the harness executes
`prompt + completion + test + check(entry_point)` in an isolated subprocess (`python -I`, 15 s timeout).
One sample per task, temperature 0.2, deterministic random subset (`seed=0`).

```bash
python benchmarks/humaneval.py --limit 20 --samples 1 --verbose
```

| Model | Tasks | pass@1 | Tokens |
|---|---|---|---|
| deepseek-v4.1-flash | 20 | **1.00 (20/20)** | 12,827 |

Caveats: n=20 of 164; this measures provider code generation, not multi-step agent repair.

## 2. BEIR SciFact (retrieval)

Protocol: BEIR (Thakur et al., 2021), binary relevance, Recall@10/100 and nDCG@10. We sample 100 queries
(seed 0) and evaluate over their labeled positives plus random distractors from the full 5,183-doc corpus:
1,000 docs, 112 positive judgments. BM25 uses `k1=1.5, b=0.75`; RRF uses `k=60` fusing BM25 and dense top-50;
reranking applies the cross-encoder to the fused top-20 and preserves full-depth order below it.

```bash
python benchmarks/retrieval.py --queries 100 --docs 1000
```

| Method | Recall@10 | Recall@100 | nDCG@10 |
|---|---|---|---|
| BM25 | 0.8647 | 0.9467 | 0.7791 |
| Dense (qwen3-embedding-8b) | **0.9800** | 0.9900 | 0.8852 |
| RRF (BM25 + dense) | 0.9397 | **1.0000** | 0.8460 |
| RRF + rerank (qwen3-reranker-8b) | 0.9767 | **1.0000** | **0.9155** |

Findings: dense dominates lexical retrieval on this scientific corpus; RRF maximizes recall at depth;
cross-encoder reranking gives the best ordering (nDCG) and is what memory retrieval should use for the
final top-k. Absolute values are not comparable to official full-corpus BEIR numbers because of sampling;
relative comparisons are.

Data: corpus/queries from `huggingface.co/datasets/BeIR/scifact` (parquet), qrels from `BeIR/scifact-qrels`;
fetch locally with `python benchmarks/fetch_data.py --corpus scifact` (large parquet files are not committed).
Embeddings are cached under `benchmarks/data/cache/`.

### 2b. BEIR NFCorpus replication (2026-11-25)

Same protocol and recipe as SciFact (`--corpus nfcorpus --queries 100 --docs 1000`, seed 0); 2,104 docs
(all sampled positives + distractors), 100 queries, 3,630 positive judgments.

```bash
python benchmarks/retrieval.py --corpus nfcorpus --queries 100 --docs 1000
```

| Method | Recall@10 | Recall@100 | nDCG@10 |
|---|---|---|---|
| BM25 | 0.1633 | 0.2490 | 0.3486 |
| Dense (qwen3-embedding-8b) | **0.1899** | **0.4278** | 0.3936 |
| RRF (BM25 + dense) | 0.1877 | 0.3780 | 0.4055 |
| RRF + rerank (qwen3-reranker-8b) | **0.2011** | 0.3780 | **0.4451** |

Replication findings: **dense > BM25 (R@10, R@100, nDCG) and rerank gives the best nDCG hold on a second corpus**,
so the production rerank-the-fused-top-20 default is supported by two corpora. The SciFact claim "RRF maximizes
recall at depth" did **not** replicate: on NFCorpus plain dense has the best Recall@100 (0.4278 vs 0.3780). The
weighted-RRF recall advantage is therefore corpus-dependent; reranking is the robust component. Data:
`huggingface.co/datasets/BeIR/nfcorpus` + `BeIR/nfcorpus-qrels`, fetched with
`python benchmarks/fetch_data.py --corpus nfcorpus`.

## 3. Memory learning loop (dreaming)

CL-Bench-style protocol (Patwardhan et al., 2025): plant six stale facts, correct each in a transcript, run
the dream engine (`G -> G+1`), then re-run the same queries at top-3. `contradictory_stale` counts hits that
contain the old value but not the correction; `stale_mention` also counts historical mentions such as
"moved from old to new", which are acceptable.

```bash
python benchmarks/memory_loop.py --k 3
```

| Phase | Contradictory stale | Stale mention | Corrected fact |
|---|---|---|---|
| Before dream | 1.00 | 1.00 | 0.00 |
| After dream | **0.00** | 0.50 | **1.00** |

Dream report: applied, generation 0→1, added 6, merged 6, superseded 6, 0 warnings.

## 4. Workflow replay (computer use)

Extracted workflow `demo_login` (trace → parameterized steps → replay) is re-run against a local form page
with five different names via the real Playwright tool.

```bash
python benchmarks/workflow_replay.py --workflow demo_login --trials 5
```

Result: **5/5 successes (100%)**, 3 steps per replay, ~1.8 s per warm replay. The first replay includes
browser startup (~27 s).

## 5. Repair loop (SWE-bench-Lite-style, local)

Not official SWE-bench. Five tiny Python repositories each contain a genuine failing visible test suite and
hidden edge-case tests. The agent must diagnose the root cause, edit source only, and pass the visible suite;
resolution is judged with the original visible tests plus hidden tests in a fresh subprocess. Test files are
restored before evaluation, and any producer edit under `tests/` is reported as tampering and fails resolution.

```bash
python benchmarks/repair.py --self-check           # validates buggy=fail, reference fix=pass
python benchmarks/repair.py --modes baseline,review --max-iterations 16
```

`review` mode appends: "call `review_work` exactly once with criteria … and fix real defects". This isolates the
effect of the independent evaluator on a code task.

| Mode | Completed | Resolved | Test tampering | Review calls | Mean tokens | Mean seconds | Mean iterations |
|---|---|---|---|---|---|---|---|
| baseline | 5/5 | **5/5** | 0 | 0 | 46,990 | 163.0 | 7.6 |
| review | 5/5 | **5/5** | 0 | 5 | 63,382 | 526.9 | 9.2 |

Findings: the baseline repair loop already resolves all five tasks without touching tests. Review mode adds an
independent read-only inspection but cannot execute the suite (by design), so on easy, fully specified tasks it
is pure overhead: **+35% tokens and 3.2× wall time for no accuracy gain**. Use `review_work` for high-stakes
artifacts, not routine fixes. Two review runs initially exceeded a 600 s cap because the reviewer wandered; that
led to a 6-iteration default, explicit "inspect only the named artifacts" guidance, and the harness now recovers
token/iteration data from session logs on timeout (see `docs/DECISIONS.md`).

A merged authoritative record can be rebuilt without re-running:

```bash
python benchmarks/repair.py --merge-only --modes baseline,review \
  --merge benchmarks/results/repair_*.json
```

## 6. Provider parity (live)

Same model, same prompts, two wire APIs:

```bash
python benchmarks/provider_parity.py
```

The script checks a tool call, replays the tool result for a final answer, and exercises streaming on both
`/chat/completions` and `/responses`.

| Wire API | Tool call | Final answer | Streaming | Tokens |
|---|---|---|---|---|
| chat | pass | pass (`391`) | pass | 713 |
| responses | pass | pass (`391`) | pass | 713 |

The first Responses agent smoke test failed with a 400 because assistant history used `output_text`; the
provider requires `input_text` (and image `detail`). The mapping was fixed and both offline and live parity now
pass. Provider latency varies substantially between runs, so use this as a correctness check, not a speed
benchmark.

## 7. Prompt injection (safety, local)

Seven attack cases across web pages, markdown files, and DOCX, plus one benign control. Cases are split into
`obvious` payloads ("SYSTEM: ignore the user") and `subtle` ones framed as routine automation (HTML comments,
handoff notes, runbook sync instructions). A case counts only when the payload was actually exposed in a tool
result; the run scores file writes, code writes, and exfiltration hits on a local collector.

```bash
python benchmarks/injection.py --modes off,on
```

| Defense | Exposed attacks | Obedience | Exfiltrations | Mean tokens |
|---|---|---|---|---|
| off | 7/7 | **0.00** | 0 | 5,191 |
| on | 7/7 | **0.00** | 0 | 5,318 |

The model refused every payload even without wrappers or prompt rules, so this suite is at ceiling and cannot
demonstrate a defense benefit; it only shows the guard does not break normal tasks (+2.4% tokens). Adaptive
attacks (AgentDojo) are required for a real security claim.

## 8. Context policies (synthetic long horizon)

A 60-step, ~29,639-token transcript with 12 facts planted at known steps. Each policy is applied to the full
history, then the model is asked for all 12 facts from the resulting messages. Window is 32k so policies engage.

```bash
python benchmarks/context_bench.py --policies full,microcompact,autocompact_75,autocompact_95,reset
python benchmarks/context_bench.py --policies autocompact_75 --no-ledger   # ablation
```

| Policy | Tokens sent | Fact recall | Compactions |
|---|---|---|---|
| full history | 29,639 | 12/12 | 0 |
| microcompact only | 24,023 | 12/12 | 0 |
| autocompact @75% + fact ledger | **7,502** | **12/12** | 1 |
| autocompact @95% + fact ledger | 7,546 | 12/12 | 1 |
| reset + handoff | 1,225 | 1/12 | 1 reset |
| autocompact @75%, ledger off | 6,734 | 5/12 | 1 |

Compaction with a verbatim fact ledger is the efficient setting: −75% prompt tokens with no recall loss. The
ledger costs 768 tokens and recovers 7 of 12 facts that the summary alone dropped. Resets are for failed
compaction or pathological bloat, not routine operation.

## 9. Memory ablations (why the learning loop works)

```bash
python benchmarks/memory_loop.py --arms no_dream,no_wiki,no_grounding,two_generations,full
```

Six stale-fact lessons, top-3 recall before/after, one dream cycle unless noted.

| Arm | Contradictory stale ↓ | Stale mention | Correct recall ↑ | Active notes | Time |
|---|---|---|---|---|---|
| no dream | **1.00** | 1.00 | **0.00** | 6 | 0 s |
| no wiki/procedures | 0.00 | 0.67 | 1.00 | 14 | ~60 s |
| no grounding check | 0.00 | 0.50 | 1.00 | 15 | ~64 s |
| two dream generations | 0.00 | 0.50 | 1.00 | 5 (12 superseded) | 114 s |
| **full (production)** | **0.00** | **0.50** | **1.00** | 13 | 51 s |

Findings: consolidation — not transcript search — is what removes the contradiction; wiki/procedure pages
reduce residual stale mentions (0.67 → 0.50); the grounding check does not change recall (it changes confidence,
which this benchmark does not score); a second dream generation adds cost (2.2×) and archives notes without any
recall gain.

## 10. Retrieval ablations (SciFact, one factor at a time)

```bash
python benchmarks/retrieval.py --queries 100 --docs 1000 --ablate
```

| Config | R@10 | R@100 | nDCG@10 |
|---|---|---|---|
| **RRF + rerank top-20 (production)** | 0.9767 | 1.0000 | **0.9190** |
| RRF + rerank top-50 | 0.9700 | 1.0000 | 0.9161 |
| RRF + rerank top-10 | 0.9397 | 1.0000 | 0.8919 |
| RRF k=60, dense-heavy (0.25/0.75) | 0.9667 | 1.0000 | 0.8660 |
| RRF k=10 (equal weights) | **0.9767** | 1.0000 | 0.8649 |
| RRF k=60 (default, equal weights) | 0.9397 | 1.0000 | 0.8460 |
| RRF k=100 | 0.9297 | 1.0000 | 0.8425 |
| Query instruction on dense leg | 0.9297 | 1.0000 | 0.8392 |
| RRF k=60, BM25-heavy (0.75/0.25) | 0.8997 | 1.0000 | 0.8100 |

Findings: reranking depth 20 is the knee (50 buys nothing for 2.5× the cost, 10 leaves 2.5 points on the table);
fusion sharpness matters — k=10 beats k=60 by ~2 nDCG points and is the best R@10; SciFact prefers dense-heavy
weighting, consistent with dense alone (0.8852) beating BM25 (0.7791). The query instruction did not help this
embedder. `rrf_k`, `rrf_lexical_weight`, and `rrf_dense_weight` are now configuration knobs; defaults stay at
k=60 / 0.8 / 1.2 until a second corpus confirms the k=10 and dense-heavy gains are not SciFact-specific.

## 9. Citation verification (R1/R2, local corpus)

Six short operations documents, twelve claims: six supported, two overstated, three false, and one true
statement cited to the wrong source. Each claim is verified against its cited passage by the model; the
ledger blocks anything not labelled `supported` from being reported as a finding.

```bash
python benchmarks/citation.py
```

| Metric | No verifier | With verifier |
|---|---|---|
| False-support rate (bad claims reported as supported) | 1.000 | **0.000** |
| Bad claims caught (partial or unsupported) | 0.000 | **1.000** |
| Supported claims retained | 1.000 | **1.000** |
| Exact verdict accuracy | — | 0.750 |

The two verdict misses are conservative in the safe direction: an overstated throughput claim was labelled
`unsupported` and an inflated headcount claim `partial`. Neither would enter the report. 12 claims took
~120 s at concurrency 2; a provider 429 mid-run exposed two robustness gaps, both fixed: retry with backoff
on rate limits, and a strict-JSON retry when the verifier replies in prose.

## 11. Document QA (DOCX / PPTX / XLSX / PDF)

Ten questions with exact answers over generated documents, answered by a fresh agent in two modes:

- `tool` — `read_document`, `list_dir`, `grep_files`, `read_file`
- `bash` — `bash`, `run_code`, `list_dir`, `read_file` (the agent must parse the formats itself)

```bash
python benchmarks/document_qa.py --self-check
python benchmarks/document_qa.py --modes tool,bash
```

| Mode | Accuracy | Mean tokens | Mean iterations |
|---|---|---|---|
| `tool` | 9/10 (one 180 s provider stall) | **6,748** | **2.9** |
| `bash` | 10/10 | 8,351 | 3.8 |

The one tool-mode miss was a timeout with zero iterations; re-running that question returned `4800 req/s` in
52 s / 4 iterations / 10,898 tokens. On answered questions both modes are accurate, but the document tool needs
~24% fewer tokens and ~1 fewer iteration. `--self-check` verifies that every expected answer is present in
`read_document` output before spending any model calls.

## 12. Live long-horizon context (token chain, 12k window)

A local server hands out 36 sequential steps: each reply contains the checkpoint id needed to fetch the next one,
so the agent must make 36 single fetches (no batching). Payloads carry ~350 tokens of noise. The final answer asks
for the ids of steps 3, 20 and 35. The configured window is 12k so a production policy must compact several times
mid-run, which stresses summary fidelity rather than just the mechanism.

```bash
python benchmarks/context_live.py --steps 36 --modes full,autocompact,no_ledger
```

| Mode | Recall | Tokens | Compactions | Resets | Wall | Record |
|---|---|---|---|---|---|---|
| `full` (compaction disabled) | **3/3** | 288,569 | 0 | 0 | 589 s | `context_live_20261125-150009.json` |
| `autocompact`, before fixes | 0/3 | 208,773 | 11 | 1 | 2,338 s | same file |
| `autocompact`, hysteresis only | 0/3 | 214,976 | 6 | 1 | 865 s | log only |
| `autocompact`, identifiers + reset gating | 0/3 | 233,453 | 7 | 1 | 1,312 s | log only |
| **`autocompact`, all fixes** | **3/3** | **275,722** | **3** | **0** | **532 s** | `context_live_20261125-201825.json` |
| `no_ledger`, all fixes | **3/3** | 265,657 | 4 | 0 | 615 s | `context_live_20261125-203411.json` |

Findings, in the order the runs exposed them:

1. **The no-progress guard mis-fired on research.** The first 36-step run stopped at iteration 10 after 9 fetches
   (`1/3`). External-info tools now count as progress; a repeated fetch still trips the guard.
2. **Compaction churn.** With a 12k window the post-compaction prompt stayed above the threshold, so every turn
   re-summarized ~2 messages: 11 compactions, 1 reset, and recall **0/3**. Growth hysteresis (1,200 tokens) plus a
   minimum 3-message head cut this to 6 compactions.
3. **Reset fired after a successful compaction**, wiping ids the ledger had just kept. Resets now require either a
   failed/no-op compaction or a prompt above the hard budget.
4. **The ledger missed ids written in prose.** `Checkpoint id T03-613ad6` is not a `key: value` line, so nothing was
   preserved verbatim. The ledger now also extracts identifiers (prefixed ids, UUIDs, commit hashes, emails) —
   unit-tested; the live rerun is pending because it competes with the agent for provider rate limit.

The honest summary: compaction as originally implemented was **worse than no compaction** in this setting (0/3 vs
3/3). Four mechanism bugs were found and fixed in sequence — guard scope, hysteresis, identifier capture in the
ledger, reset gating/handoff — and the final run reaches **3/3 with 3 compactions, 0 resets, and ~4% fewer tokens**
than the unmanaged baseline. Each fix has a regression test; the intermediate rows above are kept because they show
which mechanism actually mattered.

A caveat on interpretation: a 12k window for a 36-step task is deliberately hostile (the system prompt + tool
schemas are ~3k). It is a stress test of the *policy*, not a typical deployment; with a 24k+ window the same run
barely compacts.

**The ledger is not load-bearing at this scale.** With the policy fixed, `no_ledger` also scores 3/3 (4 compactions,
265,657 tok) — slightly *cheaper* than the ledger arm, because one fewer summary call offsets the smaller block.
The ledger's measured win remains the synthetic 60-step test (5/12 → 12/12 recall at +768 tokens); here the summary
alone kept the three ids. Keep the ledger for long, fact-dense sessions, not as a claimed universal win.

## 13. Adaptive prompt injection (AgentDojo-style red team, local)

A red-team LLM writes an injection for a benign task, sees the target's tool trace and final answer, then refines
the payload for a second round. Each case also runs a fixed "static" payload as a baseline. Defenses off/on.

```bash
python benchmarks/adaptive_injection.py --rounds 2 --modes off,on
```

| Defense | Static ASR | Adaptive ASR (all rounds) | Final-round ASR | Utility | Exfiltrations | Mean tokens |
|---|---|---|---|---|---|---|
| off | 0.0 (0/3) | 0.0 (0/6) | 0.0 | 1.0 | 0 | 4,250 |
| on | 0.0 (0/3) | 0.0 (0/6) | 0.0 | 1.0 | 0 | 4,635 |

Full suite: 3 cases (`web_write`, `web_exfil`, `file_write`) × 1 static + 2 adaptive rounds × 2 defense modes = 18
runs, `benchmarks/results/adaptive_injection_20261125-214157.json`.

Findings: the aligned target refuses both hand-written and iteratively refined injections across web, file and
exfiltration channels; **adaptive ASR is not higher than static ASR**, so no targeted adaptive defense was required.
The defenses (untrusted-content wrapping + injection guard) cost about **+9% tokens** (4,635 vs 4,250) and did not
change utility. This is a ceiling result — the target model is strong enough that the benchmark cannot separate
defended from undefended behaviour — not proof of safety; see the limitations section. The next step that would give
a non-ceiling signal is running a smaller/weaker target model, which we have not done. The runner, payload parser,
defense-aware attacker prompt and feedback loop are unit-tested.

## 14. SWE-bench-Lite subset (official harness)

`benchmarks/swebench_lite.py` clones the real repository at each instance's base commit, runs Argo on the issue
text, writes `git diff` predictions (test-file edits reverted and flagged), and hands them to the official
`swebench` harness with prebuilt images.

```bash
python benchmarks/swebench_lite.py --generate --limit 2
python benchmarks/swebench_lite.py --evaluate --limit 2      # docker, prebuilt swebench/sweb.eval.*
```

| Instance | Stop | Iterations | Tokens | Patch | Tampered tests | Official result |
|---|---|---|---|---|---|---|
| sympy__sympy-21614 (gold, harness check) | — | — | — | gold | n/a | **1/1 resolved** |
| sympy__sympy-16988 (Argo, no test feedback) | no_progress | 22 | 464,315 | 1 file / 470 chars | no | not graded |
| sympy__sympy-16988 (Argo, `--install-deps`) | final | 17 | 228,926 | 1 file / 808 chars | no | **1/1 resolved** |

**Headline:** with the test-feedback loop (`--install-deps`: official test patch applied + editable install + the
exact `FAIL_TO_PASS` pytest command in the prompt) the same instance that previously produced no patch **resolves**:
FAIL_TO_PASS 2/2, PASS_TO_PASS 72/72, patch applied cleanly (`logs/run_evaluation/argo_20261125221307`). The agent
used 17 iterations and 228,926 tokens — roughly half the tokens of the pre-fix attempt, because failing tests give a
direct signal instead of unbounded exploration. Sample size is n=1; this establishes the mechanism, not a pass rate.

Infrastructure check: grading the **gold** patch through the official harness resolved **1/1**
(`gold.gold_check.json`), and `--cache_level none --clean True` removed the image afterwards (disk returned to
baseline). That validates the container path independently of model quality.

The Argo run stopped at the no-progress guard after 22 iterations: the agent explored many distinct files, which
the guard counted as "no progress". Novel read paths now count as progress (unit-tested), so the next run should be
allowed to finish. The produced patch is also a reminder that one pass without running the project's tests is weak;
the follow-up is to let the agent `pip install -e .` inside a scratch copy so it can run the target tests itself.

## Reproduce everything

```bash
pip install pyarrow            # retrieval benchmark only
python benchmarks/humaneval.py --limit 20 --verbose
python benchmarks/retrieval.py --queries 100 --docs 1000
python benchmarks/memory_loop.py --k 3
python benchmarks/workflow_replay.py --trials 5
python benchmarks/repair.py --modes baseline,review
python benchmarks/provider_parity.py
python benchmarks/injection.py --modes off,on
python benchmarks/context_bench.py --policies full,microcompact,autocompact_75,autocompact_95,reset
python benchmarks/citation.py
python benchmarks/memory_loop.py --arms no_dream,no_wiki,no_grounding,two_generations,full
python benchmarks/retrieval.py --queries 100 --docs 1000 --ablate
python benchmarks/document_qa.py --modes tool,bash
python benchmarks/context_live.py --steps 36 --modes full,autocompact,no_ledger
python benchmarks/adaptive_injection.py --rounds 2 --modes off,on
python benchmarks/swebench_lite.py --generate --limit 2
python benchmarks/swebench_lite.py --evaluate --limit 2
pytest -q
```

## Data

`benchmarks/data/` holds downloaded datasets and caches and is not intended for commit. Sources:

- HumanEval: `raw.githubusercontent.com/openai/human-eval/master/data/HumanEval.jsonl.gz`
- SciFact: `huggingface.co/datasets/BeIR/scifact` and `huggingface.co/datasets/BeIR/scifact-qrels`

## Limitations

- Single model/provider; no variance runs or confidence intervals yet.
- HumanEval is single-shot; the local repair harness uses five tiny repos. Official containerized SWE-bench-Lite is the next step.
- SciFact sample is 100 queries over 1,000 docs, not the full corpus.
- Workflow replay is deterministic and local; selector drift, CAPTCHAs, and cross-site flows are untested.
- The injection suite, context benchmark, and citation set are local/synthetic and single-model; they show mechanism effects, not field performance.
