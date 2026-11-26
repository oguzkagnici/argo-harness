# Roadmap

This is the working plan after the first full implementation + benchmark pass. It prioritizes capabilities that
are both high-value and measurable. Every item should ship with an eval or a reproducible check.

## 1. Where Argo stands

Working and tested: ReAct/CodeAct agent loop, tool registry with approvals/allowlists/parallelism, context
microcompaction/compaction/reset, hybrid retrieval + reranking, layered memory with generational dreaming,
documents (DOCX/PPTX/XLSX/PDF), browser/computer use, workflow extraction and replay, subagents, review_work,
OpenAI-compatible server (chat + native SSE), always-on daemon, and both `/chat/completions` and `/responses`
wire APIs.

Measured: HumanEval 20/20; SciFact RRF+rerank nDCG@10 0.916; memory loop stale 1.00→0.00 / corrected 0.00→1.00;
workflow replay 5/5; local repair loop 5/5; review overhead +35% tokens / 3.2× wall on easy fixes.

## 2. Deep research track (planned, not yet implemented)

Benchmark landscape, metrics and cost-aware evaluation plan: [`DEEP_RESEARCH.md`](DEEP_RESEARCH.md).

Deep research is a different workload from coding: open-ended planning, many searches, source triage, evidence
management, citation accuracy, and long-form synthesis under a budget.

### 2.1 Capabilities to build

1. **Research planner.** Decompose a question into sub-questions, set a source/time budget, and decide when to
   stop. Re-plan after each evidence round; keep the plan in the session state.
2. **Source acquisition.** Provider chain (Tavily/Brave/SearXNG, then keyless fallbacks), arXiv/Wikipedia,
   direct fetch, and a Playwright fallback for JS-heavy pages. Cache pages with timestamps and content hashes.
3. **Evidence ledger.** Store claim → supporting quote → source URL/timestamp/span outside the context window.
   Retrieve evidence per report section instead of keeping all pages in context.
4. **Citation verification.** Before finalizing, re-check that every cited claim is supported by the stored
   snapshot; flag unsupported or stale claims. This mirrors ALCE-style citation evaluation.
5. **Synthesis.** Section-by-section writing with a stable outline, then a compression/edit pass. Use
   `review_work` with citation criteria for high-stakes reports.
6. **Budget and safety controls.** Per-task search/token/time caps, deduplication of sources, prompt-injection
   defenses for fetched content, and explicit "unknown/insufficient evidence" outcomes.

### 2.2 Benchmarks and metrics

| Benchmark | What it measures | Intended use |
|---|---|---|
| **DeepResearch Bench** (Du et al., 2025) | PhD-level research reports across domains; RACE + FACT rubric | Final report quality, citation/factuality |
| **BrowseComp** (OpenAI, 2025) | Hard-to-find factual answers requiring browsing | Search persistence and query formulation |
| **GAIA** (Mialon et al., 2023) | Multi-step real-world tool use with exact answers | Tool orchestration end-to-end |
| **ALCE** (Gao et al., 2023) | Citation precision/recall and answer correctness | Citation verification subsystem |
| **AssistantBench / WebWalkerQA** | Web navigation and multi-hop retrieval | Browser/search robustness |

Metrics: task success, citation precision/recall, unsupported-claim rate, source diversity, tokens, wall time,
and cost per completed report. LLM judges should be paired with URL-snapshot checks and human spot-checks.

### 2.3 Milestones

- **R1:** evidence ledger + citation verifier + a 10-task internal research set with cached pages.
- **R2:** research workflow (plan → search → read → ledger → draft → verify) with a hard budget.
- **R3:** ALCE-style citation metrics and a GAIA/BrowseComp-style exact-answer subset.
- **R4:** DeepResearch Bench subset with dual-judge rubric and human review; report cost/latency.

Risks: search-provider rate limits on cloud IPs, web drift between runs, judge unreliability, and source-access
restrictions. Cache snapshots and record retrieval timestamps to make runs reproducible.

## 3. Engineering track (ordered)

| Order | Item | Why | Proof |
|---|---|---|---|
| 1 | **Responses API adapter** | Provider compatibility | Done: offline tests + live chat/responses parity |
| 2 | **Prompt-injection / tool-safety suite** | Argo ingests untrusted web/file content | AgentDojo-style attack success rate before/after |
| 3 | ~~**Long-horizon context benchmark**~~ — done: `benchmarks/context_bench.py` | Result: autocompact+ledger 7.5k tok / 12-12 facts vs full 29.6k / 12-12; reset 1.2k / 1-12; ledger ablation 5-12 → 12-12 | Next: live 40–60-step run with images and large diffs |
| 4 | ~~**MCP client**~~ — done: `argo/tools/mcp.py` | 3 unit tests + live call; `argo doctor` reports servers/tools | Next: MCP resources/prompts, HTTP transport, per-tool approval policy |
| 5 | **Official SWE-bench-Lite subset** | Real-repo credibility | 10–20 containerized instances |
| 6 | ~~**Worktree isolation for subagents**~~ — done: `argo/core/worktree.py` | 5 tests (isolation, non-git, agent spawn, diff review, committed-diff review) + live run: branch `argo/...` created, parent tree clean | ~~auto-review of `changed_files` before merge~~ → done via `review_changes=true` (PASS/FAIL gate) |
| 7 | ~~**Plan mode + batch approval**~~ — done: `--plan`, `propose_plan` | 3 tests + live run (7 iter / 45.9k tok, no write before approval) | Next: persist approved plan across sessions |
| 8 | ~~**Background tasks**~~ — done: `start_background`/`check_background` | 2 tests + live runs; wait-based check cut 10 iter/71,428 tok → **3/19,284** | Next: observability (per-tool latency/token, session replay) |
| 9a | ~~**Memory ablations**~~ — done: `--arms` on `benchmarks/memory_loop.py` | no-dream 1.00→0.00 correct; wiki pages cut stale mentions 0.67→0.50; two generations = no gain at 2.2× cost | Next: multi-generation policy test at scale |
| 9b | ~~**Retrieval ablations**~~ — done: `--ablate` on `benchmarks/retrieval.py` | rerank-20 knee; k=10 and dense-heavy +2 nDCG on SciFact; knobs added to config | Next: NFCorpus replication before changing defaults |
| 9c | ~~**Document QA**~~ — done: `benchmarks/document_qa.py` | tool 9/10 at 6,748 tok / 2.9 iter (1 stall); bash 10/10 at 8,351 / 3.8 | Next: chart-heavy and scanned PDFs |
| 9d | ~~**Adaptive review policy**~~ — done: `--review adaptive`, risk scoring | 6 tests; live doc run auto-reviewed (27.8k tok) and found defects | Next: measure cost/quality on the repair suite |
| 9 | **Deep research implementation** — R1+R2 done (`argo/research/`), R3 report citations + exact-answer subset done | false-support 1.000 → 0.000, report citation P/R 1.000, exact answers 6/6 @ 69k tok | Next: R4 multi-section pipeline, full GAIA/BrowseComp, adaptive search |

## 4. Non-goals (for now)

- Rewriting the harness in TypeScript/Bun or coupling tools to a terminal UI.
- Copying leaked proprietary source; only architectural patterns are used.
- Distributed multi-host workers before single-host durability and observability are proven.
- Adding more tools before injection safety and long-context behavior are measured.
