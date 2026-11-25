# Deep research: benchmark landscape and Argo's plan

Status: **R1 and R2 implemented; R3/R4 planned.** `argo/research/` contains the evidence ledger and the
claim verifier, with `benchmarks/citation.py` measuring them. This memo records what "deep research" is measured against, which
metrics translate into engineering requirements, and the staged plan already summarized in
[`ROADMAP.md`](ROADMAP.md) §2. Nothing here is claimed as a current Argo capability.

## 1. What the benchmarks actually measure

| Benchmark | Shape | What it stresses | Primary metric | Engineering implication |
|---|---|---|---|---|
| **GAIA** (Mialon et al., 2023) | 466 real questions, 3 difficulty levels, tools + web + files | Multi-step tool use, file handling, planning | Exact-match accuracy by level | Tool coverage and loop robustness matter more than prose quality |
| **BrowseComp** (OpenAI, 2025) | ~1.3k hard-to-find-answer questions, browsing only | Persistence, obscure-source retrieval, search strategy | Accuracy **and calibration** (confidence vs correctness) | Needs many search rounds with query reformulation and budget control |
| **DeepResearch Bench** (2025) | 100 expert research tasks across 22 domains | Long-form report quality | RACE rubric: comprehensiveness, insight, instruction-following, readability | Report planning + section-level evidence, not just an answer string |
| **ALCE** (Gao et al., 2023) | ASQA / QAMPARI / ELI5 with retrieved passages | Attribution | Correctness, **citation precision/recall**, fluency | Claim-level citations must be extracted and verified mechanically |
| **FreshQA / FreshLLMs** (Vu et al., 2023) | Time-sensitive and false-premise questions | Freshness, resisting invalid premises | Accuracy split by fast-changing / never-changing / false-premise | Retrieval must prefer recent sources and the agent must challenge premises |
| **WebArena / OSWorld / Mind2Web** | Realistic sites and OS tasks | Computer-use reliability | Task success, unsafe-action rate | Browser workflows need replayable selectors and assertions (Argo already has both) |

Two lessons from published results are consistent across these suites: (1) single-pass "search once, then write"
systems plateau far below multi-round agents, and (2) unverified citations are the dominant failure mode in
long-form output. Both are addressable in the harness rather than by prompt wording alone.

## 2. Metrics Argo will optimize

1. **Answer correctness** — task-specific exact match or rubric score, never self-reported.
2. **Claim support rate** — fraction of report claims whose cited source actually entails the claim. Requires a
   claim→source pair, so the evidence ledger comes before prose generation.
3. **Citation precision / recall** — ALCE-style; recall punishes omitted sources, precision punishes decorative
   citations.
4. **Coverage vs budget** — distinct sub-questions answered per N searches/tokens. The useful tradeoff curve, not
   a single number.
5. **Calibration** — confidence attached to answers, scored Brier-style when the benchmark allows abstention.
6. **Freshness** — recency of the median cited source for time-sensitive questions.
7. **Cost and latency per completed task**, including failed branches.

## 3. Required capabilities (R1–R4)

- **R1 — Evidence ledger (done).** `argo/research/ledger.py`: sources get stable ids (`S1…`), deduplicated by
  normalized URL; claims get ids (`C1…`) and may only cite existing source ids; reports render from the ledger.
  Tools: `add_source`, `add_claim`, `list_sources`, `research_report`.
- **R2 — Citation verifier (done).** `argo/research/verify.py` checks each claim against its cited passage with a
  conservative JSON verdict (supported / partial / unsupported), retries rate limits and non-JSON replies, and
  marks uncited or dangling-citation claims unsupported without an LLM call. Tool: `verify_claims`.
  Measured on a local 12-claim set: **false-support 1.000 → 0.000**, bad claims caught **1.000**, supported
  claims retained **1.000** (`benchmarks/citation.py`).
- **R3 — Adaptive search controller.** Decompose the question into sub-questions, allocate search/read budget per
  branch, detect saturation (no new information), and reformulate instead of repeating. Reuse the existing loop
  guard and progress signals rather than adding a second controller.
- **R4 — Report synthesis.** Outline → sections → claims, with the ledger as the only source of facts; then a
  read-only reviewer pass against a rubric (RACE-like) and the verifier results.

## 4. How we will evaluate without unbounded cost

- Start with **ALCE-style citation metrics on a cached corpus** (local documents + archived pages) so verification
  is deterministic and affordable.
- Add a **small GAIA subset** (level 1–2) for end-to-end tool/planning behavior; report per-level accuracy.
- Sample **DeepResearch Bench** tasks only after R1–R2 pass; rubric grading needs an independent model and is
  expensive, so it stays a periodic check, not a per-commit test.
- BrowseComp-style calibration is the last step: it requires browsing at scale and a held-out answer set.

## 5. What would count as success

| Milestone | Evidence required |
|---|---|
| R1 | **Met:** 5 unit tests (dedupe, ids, dangling citations, report rendering, end-to-end tools) |
| R2 | **Met locally:** false-support 0.000 vs 1.000 baseline; supported retention 1.000; needs a larger corpus |
| R3 | Coverage-per-search improves over a fixed-round baseline on a local task set |
| R4 | Rubric review + verifier pass on a small, fixed task set; no fabricated citations |

Until R3/R4 exist, Argo should not claim full deep-research capability. It now has the verification layer that
separates research from plausible prose, but not yet the adaptive search controller, the multi-section report
pipeline, or evaluation on GAIA/BrowseComp/DeepResearch Bench.
