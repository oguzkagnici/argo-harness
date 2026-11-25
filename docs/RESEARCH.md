# Research: harnesses, always-on agents, and what Argo adopts

**Scope.** This document records the evidence behind Argo's design. It distinguishes:
- **Primary sources** — vendor docs/engineering posts, papers, official product pages.
- **Secondary sources** — third-party benchmarks, teardowns, guides. Treated as weaker evidence.
- **Inference** — our reading of how a product likely works when internals are not public.

Research date: **2026-11-24**. Product details change quickly; treat marketing claims as claims, not facts.

---

## 1. Executive summary

1. **Harnesses converge on a small set of primitives:** a ReAct/CodeAct loop, a plan, context compaction,
   tool-output bounds, subagents, permission gates, and memory. The differences are mostly in orchestration,
   context policy, and product surface.
2. **The most important lever is context engineering, not prompt wording.** Anthropic's long-running harness
   work found context resets with structured handoffs can beat compaction alone when models become
   "context-anxious"; compaction remains the cheaper first step.
3. **Memory is converging on a layered, file-like design plus offline consolidation.** Anthropic describes
   `transcripts → notes → wiki → injected context` with *dreaming*; Instinct uses git-tracked markdown and grep;
   Hermes uses bounded `MEMORY.md`/`USER.md`; Letta/UC Berkeley formalize "sleep-time compute".
4. **Always-on agents are the same loop with different surrounding architecture:** event inbox, timers, memory,
   permissions, and an outbox. The agent spends most of its time asleep; runs are short and event-triggered.
5. **Retrieval works best as hybrid + rerank + agentic search.** BM25 and dense retrieval fail differently;
   RRF fusion is robust; a cross-encoder reranker improves precision; exact grep remains essential for identifiers.
6. **The model cannot reliably evaluate its own work.** A separate evaluator with tools and explicit criteria
   materially improves artifacts. Argo implements this as `review_work`.
7. **Computer-use automation should be recorded, parameterized, and replayed declaratively.** Free-form replay is
   brittle and unsafe; an inspectable workflow with assertions is testable and permission-checked.

---

## 2. Coding/general harness landscape

| Harness | Architecture (verified/inferred) | What we adopted | What we did not |
|---|---|---|---|
| **Claude Code** (Anthropic) | Terminal agent; file read/edit, shell, search; plan/TODO; subagents; auto-compaction; `CLAUDE.md` + auto memory; skills; hooks; permission modes (plan/default/auto); MCP; background tasks | Plan tool, subagents, compaction, skills, permission gates, markdown memory | Full MCP, hooks system, IDE integration |
| **Codex** (OpenAI) | Open-source CLI; `AGENTS.md`; `apply_patch`; sandboxed execution + approvals; compaction; tool-output limits; MCP; cloud tasks | `apply_patch`, output spilling, approval policy, provider inheritance from Codex config | Sandboxed FS/network enforcement (we use policy checks), cloud runner |
| **DeepSeek Harness** (`dsh`) | "Agent = Model + Harness"; Cordis plugin kernel; everything is a plugin; 4 preset modes; TypeScript tool contracts; Docker sandbox; append-only trajectory logs with fork/replay; DSBench eval suite; prefix-cache alignment | Plugin-style tool modules, append-only JSONL traces, cache-stable prefix, eval mindset | Full plugin kernel, Docker-per-run, fork/replay UI |
| **OpenHands** | Event-stream architecture; Docker sandbox runtime; CodeAct-style executable actions; microagents; SWE-bench evaluation | Event bus, code as action, sandboxable shell, trajectory logging | Microagent ecosystem, container runtime |
| **Muse Code** (Meta) | Terminal coding agent; "multi-agent by default" (parallel workers + background reviewers); auditable actions; 1M-context Muse models; co-trained for tool use | Generator/reviewer separation, parallel subagents, audit trail | Model co-training, 1M-context-specific tuning |
| **ZCode** (Z.ai) | Official harness for GLM-5.4; multi-agent; skills; terminal + browser; "control from anywhere"; 20+ agent tools | Skills, browser tool, multi-agent orchestration | GLM-specific tuning, remote control UI |
| **Hermes Agent** (Nous) | Open-source MIT; persistent `MEMORY.md`/`USER.md` bounded (2,200/1,375 chars); 39 skills; cron; messaging gateways; self-host or hosted | Bounded always-loaded memory, skills, cron-style triggers, BYO runtime | 1,000+ app connectors, messaging gateways |
| **Argo** (this project) | ReAct+CodeAct loop; plan; microcompact/compact/reset; layered memory + dreaming; hybrid retrieval; subagents + evaluator; documents; browser; OpenAI API; always-on daemon | — | Production multi-tenancy, full MCP, distributed workers |

**Evidence notes.**
- DeepSeek Harness's vendor page claims a **99.93% prefix-cache hit rate** and **80%+ token savings**. Treat as a
  vendor claim. Its structure (stable prefix + append-only trajectory) is independently sensible and is what Argo
  copies. Artificial Analysis measured `dsh` with GLM-5.3-Flash (Max) at **37** on its Coding Agent Index
  (median 40), **~1.1h/task**, **~$0.70/task** — a useful caution that harness design alone does not guarantee
  top performance.
- Muse Code's "multi-agent by default" and "reviewers in the background" are vendor claims; the pattern matches
  Anthropic's generator/evaluator finding.
- OpenHands' event-stream + Docker runtime is documented in its paper and docs.

---

## 3. Always-on agents

### 3.1 The five primitives (Anthropic, Nov 2026)

Anthropic's "Building always-on agents" is the clearest architecture reference we found. Key points:

- An always-on agent is **not actually always on**: it sleeps, wakes for a *run*, writes state, sleeps.
- **Event inbox:** durable table + HTTP handler + worker. Events are deduplicated; one active worker per inbox;
  pending events can be batched. The handler acknowledges fast; deep checks happen in the run.
- **Timers:** separate durable store; follow-ups (agent-set deadlines) and schedules (recurring). A scheduler
  polls due timers and enqueues events.
- **Memory:** durable directory/DB; only store what cannot be looked up again; save pointers for external state.
  Load `instructions`, an index, and recent messages at the start of every run; load notes/progress on demand.
- **Permissions:** credential proxy outside the agent; auto mode with classifiers; **deferred tool calls** that
  end the run, save the session, ask the user, and resume from an inbox event.
- **Outbox:** runs end by sending a message, deciding nothing needs sending, or deferring for approval. A
  dispatcher delivers; pacing matters (start quiet, earn attention).

**Argo mapping:** `goals` = inbox, `triggers` = timers, layered memory = memory, danger guard + approvals =
permissions, `outbox` + `notify` = outbox. Deferred-call *resumption* is not fully implemented (we fail safe and
queue an approval); see roadmap.

### 3.2 Product comparison

| Product | Surface | Computer | Memory | Proactivity | Approval model | Notable difference |
|---|---|---|---|---|---|---|
| **Claude Duet** | iOS/Android/claude.ai; one ongoing thread; voice | Cloud VM with its own browser | Carries over Claude memory, skills, connected apps | Works while away; checks connected accounts every few hours; message when needed | Asks before sending, paying, signing, deleting | Same memory/skills as Claude account; cost-aware polling |
| **OpenAI Dots** | ChatGPT desktop/web/mobile, Slack, Teams | Own cloud computer + browser; can connect your laptop | Persistent per-dot context; notes | Runs multiple projects; "proactive research" using **read-only** tools | Auto-review against rules; sensitive tasks always human; Custom Rules; Activity View | Per-dot computer isolation; channel continuity |
| **Grok Bot** (xAI) | Chat with named bots; group chats; schedules | Persistent cloud Linux computer (shared per account, not per bot) | Memory across turns; learns a routine by watching once | Scheduled tasks; hands work between bots | Undisclosed defaults; shared computer is not a security boundary | Routine learning from demonstration; model not selectable |
| **Instinct** | No UI: iMessage/WhatsApp/voice | Cloud VM; vision-language models; app integrations | Git-tracked markdown; ~4.25k-token profile + ~10k-token compaction recap; grep retrieval | Anticipates needs; background work; follow-ups | Privacy/security concerns reported; zero-UI raises consent questions | Memory is inspectable markdown with git history |
| **Hermes Agent** | CLI, web dashboard, Telegram/Discord/Slack, GitHub | Hosted or self-hosted | Bounded `MEMORY.md` + `USER.md`; skills | Cron jobs; 24/7 hosted option | Write approval option in CLI | Open-source, self-hostable, 1,000+ Composio apps (hosted) |
| **Muse Code** | Terminal (Meta) | Local tool execution | Session/agent context | Not a general always-on product | Auditable actions | Coding agent, multi-agent by default |

**Caveats.**
- Grok Bot, Duet, Dots, Instinct details are largely vendor/marketing or third-party reporting. The shared-computer
  and model-undisclosed claims come from a secondary guide; verify against current docs before relying on them.
- "Muse" appears in some always-on lists, but the primary source we found is **Muse Code**, Meta's terminal coding
  agent, not a general always-on assistant. We treat "always-on Muse" as **unverified**.
- Instinct's memory design is a **black-box reconstruction** by Supermemory's CEO, explicitly caveated. It is
  strong evidence of a *pattern*, not a specification.

### 3.3 How always-on differs from a regular harness

| Dimension | Regular harness | Always-on agent |
|---|---|---|
| Trigger | Human turn | Event inbox: webhook, schedule, timer, message, reply |
| Lifetime | Session-scoped | Indefinite; many disjoint runs |
| State | Conversation + workspace | Durable goals, memory, progress files, outbox |
| Output | Streamed synchronously | Asynchronous notification; outbox + pacing |
| Permissions | Interactive approval | Auto mode + classifier/approval queue + credential proxy |
| Cost control | Per-turn | Budgets, polling cadence, batching, sleep |
| Failure | User retries | Retryable inbox events, dedupe, idempotency, resumable sessions |
| Evaluation | Human reads output | Activity view, audits, scheduled self-review/dreaming |

The agent loop is unchanged. The **inbox, timers, durable memory, permissions, and outbox** are what make it
always-on.

---

## 4. Memory: what the evidence says

### 4.1 Anthropic: hierarchy + dreaming

"Components of agent memory" (Nov 2026) describes:
- **Transcripts** (searchable event log) → **memory notes** (salient observations) → **memory representations**
  (wikis/pages) → **injected context**.
- Memory is a prediction about what the future will need; a verified fact is worth storing, transient state is not.
- **Salience matters:** in Pokémon, one model wrote 413 files/978k chars and earned one badge; another wrote 10
  files/43k chars and earned four badges. The small memory focused on strategy, plans, and tested rules.
- **Dreaming** corrects notes against transcripts (the Mt. Moon example), builds wiki representations, and can
  shape what is pre-injected.
- The Dreams API clones the input store, produces a separate output store, and never mutates the input.

**Argo implementation:** exact layer names, generational clone, evidence grounding, wiki links, bounded injection,
salience rules in the dream prompt.

### 4.2 Letta/UC Berkeley: sleep-time compute

Sleep-time Compute (arXiv:2504.13171) lets a model think offline about context before queries arrive. Reported
results: **~5× less test-time compute** for the same accuracy on Stateful GSM-Symbolic and Stateful AIME, and up
to **+13% / +18%** accuracy with more sleep-time compute. This is the research basis for treating dreaming as a
first-class, scheduled job rather than an afterthought.

### 4.3 Instinct: markdown, grep, profiles, reconciliation

A black-box teardown reports:
- Memory is **git-tracked markdown**, found with **grep rather than vectors**.
- A ~**4,250-token profile** (life context, autonomy calibration, channel style) and ~**10k tokens of compaction
  recap** are injected.
- Files are a graph: IDs, aliases, `[[links]]`; folders for entities, knowledge, comms, timeline, workstreams.
- "Reconciliation commits" compress narrative, generalize examples, remove incidentals, and replace incorrect
  facts with dated corrections.
- Git history preserves old versions; forgetting is not fully automatic.

**Argo implementation:** markdown export with frontmatter/IDs/links, bounded dashboard, generational history,
dream reconciliation (merge/supersede/archive), explicit `forget`. We use BM25+dense+rerank rather than pure grep
for scale, but keep `grep_files` as the exact-match path.

### 4.4 Hermes: bounded files

Hermes caps `MEMORY.md` at 2,200 chars and `USER.md` at 1,375 chars, and **fails over-limit writes rather than
silently evicting facts**. This is a strong argument for bounded always-loaded memory and explicit curation.

**Argo implementation:** `memory_inject_chars` budget (default 6,000), profile/notes/wiki/procedure sections,
dedupe, dream pruning. We do not silently drop facts; the full store remains searchable.

### 4.5 MemGPT / Generative Agents

- **MemGPT** (arXiv:2310.08560) frames memory as an OS: tiered storage, paging, and self-editing. Argo's layers +
  retrieval + explicit `remember`/`forget` are a simplified version.
- **Generative Agents** (arXiv:2304.03442) retrieves by relevance, recency, and importance. Argo uses the same
  three signals in its memory scoring.

---

## 5. Context management: what the evidence says

- **Anthropic, "Effective context engineering" (Sep 2025):** optimize the *set of tokens*, not just wording;
  use minimal high-signal context; prefer just-in-time retrieval; compact long histories.
- **Anthropic, "Harness design for long-running apps" (Mar 2026):** long tasks hit context limits and models show
  "context anxiety", wrapping up early. **Compaction** preserves continuity but not a clean slate; **context
  resets** with structured handoff artifacts fix anxiety but require good artifacts. The winning harness used
  planner/generator/evaluator roles, a task list, and Playwright-based evaluation.
- **DeepSeek Harness:** claims large savings from prefix-cache alignment; stable prefixes matter.
- **Lost in the Middle (arXiv:2307.03172):** models use information at the beginning/end better than the middle.
  Keep critical instructions at the edges; do not bury them.

**Argo implementation:** three-stage context management, handoff files, stable prefix, durable plan, evaluator
with read-only tool access, and re-injection of critical state after compaction.

---

## 6. Retrieval: what the evidence says

| Mechanism | Evidence | Argo use |
|---|---|---|
| BM25 | Robertson & Zaragoza (2009), decades of IR results; strong exact-term baseline | Lexical ranking |
| Dense embeddings | Strong semantic recall, weak on exact IDs/numbers | Dense ranking if endpoint supports it |
| RRF | Cormack et al. (2009); robust rank fusion without score calibration | Fuse lexical+dense, k=60 |
| Cross-encoder rerank | Consistently improves top-k precision | `/rerank` over fused candidates |
| Agentic search (grep/glob) | Claude Code/Codex rely on fast exact search for code | `grep_files`, `glob_files` |
| Recency/importance | Generative Agents | Memory score boost |

Chunking is heading-aware with overlap and line ranges, so citations are actionable (`file:line`). Long tool
outputs are spilled, not silently dropped.

---

## 7. Workflow extraction & computer use

- **Grok Bot** reportedly "learns a routine by watching you do it once". This validates the product need; the
  implementation is opaque.
- **Anthropic's harness work** used Playwright in the evaluator loop to click through pages, screenshot, and
  critique; evaluation with tools beats self-assessment.
- **DeepSeek Harness** emphasizes append-only trajectory logs with fork/replay, implying trace-based debugging
  and replay are first-class.
- **OpenHands** logs action/observation event streams, enabling inspection.

**Argo implementation:** record tool calls in append-only session JSONL → LLM synthesizes a parameterized
workflow constrained to the real tool catalog → replay through the same permission-checked registry → export a
skill. Assertions (`expect_contains`, `expect_exit_zero`) and retries make replays verifiable. This is safer and
more inspectable than replaying free-form code or raw mouse coordinates.

---

## 8. Decision ledger

| Decision | Evidence | Implementation |
|---|---|---|
| ReAct-style loop with tool observations | ReAct (2022) | `core/agent.py` |
| Code execution as a first-class action | CodeAct (2024) | `tools/shell.py` |
| Plan tool with one active step | Claude Code/Codex; Anthropic harness | `tools/plan.py`, session |
| Microcompact → compaction → reset | Anthropic context engineering + harness design | `core/context.py` |
| Stable system+tools prefix | DeepSeek Harness prefix-cache claim | `core/prompts.py`, request assembly |
| Layered memory hierarchy | Anthropic memory components | `memory/store.py` |
| Generational dreaming with evidence grounding | Anthropic Dreams; sleep-time compute | `memory/dream.py` |
| Bounded always-loaded memory | Hermes; Instinct profile budget | `memory/store.py`, `memory_inject_chars` |
| Hybrid BM25+dense+RRF+rerank | IR literature; RRF; cross-encoder practice | `memory/retrieval.py` |
| Recency/importance boosts | Generative Agents | `memory/store.py` |
| Subagents for context isolation | Claude Code; Anthropic harness | `core/agent.py` |
| Independent evaluator | Anthropic generator/evaluator finding | `review_work` |
| Declarative workflows + skills | Grok Bot routine learning; Claude Code skills | `workflows/` |
| Event inbox/timers/outbox/approvals | Anthropic always-on primitives | `daemon.py` |
| Read-only proactive research | OpenAI Dots | `--read-only` triggers |
| Browser screenshots + DOM text | Anthropic Playwright evaluator; computer-use practice | `tools/computer.py` |
| OpenAI-compatible API | Ecosystem compatibility | `server.py` |

---

## 9. What we deliberately did not copy

- **Per-product proprietary model tuning** (Muse/ZCode/GLM). Argo is provider-agnostic.
- **Shared cloud computer per account** (Grok Bot). It is a documented security hazard; Argo uses a local browser
  profile and encourages isolation.
- **Zero-UI, always-listening personal agent** (Instinct). High consent/privacy risk; Argo requires explicit
  triggers and approvals.
- **Free-form replay of generated scripts** for computer use. Declarative workflows are safer and testable.
- **Unbounded memory writes** (early Pokémon behavior). Argo dedupes, caps injection, and curates via dreaming.
- **A vector-only memory store.** Instinct's grep-based approach and IR literature both argue for hybrid/exact
  retrieval; identifiers are better served by BM25/grep.

---

## 10. Open questions / roadmap

1. **Deferred tool-call resumption** (save session mid-call, resume from approval event) — the missing always-on primitive.
2. **MCP client** (stdio/SSE) for the broader tool ecosystem.
3. **Prefix-cache instrumentation** per provider; measure hit rate and cost, not just claim alignment.
4. **Evals:** port a subset of SWE-bench/WebArena/OSWorld-style tasks; add a document-quality rubric and
   workflow-replay success rate.
5. **Memory evals:** CL-Bench-style learning loops (answer → correction → rule → improve).
6. **Multi-tenant server:** per-user workspaces, quotas, audit logs, secret manager integration.
7. **Sandbox hardening:** optional container/namespace execution for shell/browser.
8. **Dream scheduling policies:** event-driven triggers (after N sessions, on conflict detection) versus fixed time.
9. **Workflow robustness:** selector healing, DOM-diff assertions, and human-in-the-loop repair.
10. **Always-on "Muse"** verification: no primary source found for a general always-on product named Muse.

---

## 11. Sources

**Anthropic / Claude**
1. Anthropic, *The components of agent memory*, Nov 6 2026. https://www.anthropic.com/research/components-of-agent-memory
2. Claude Platform Docs, *Dreams* (Managed Agents). https://platform.claude.com/docs/en/managed-agents/dreams
3. Anthropic, *Effective context engineering for AI agents*, Sep 29 2025. https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
4. Anthropic, *Harness design for long-running application development*, Mar 24 2026. https://www.anthropic.com/engineering/harness-design-long-running-apps
5. Anthropic, *Building always-on agents*, Nov 13 2026. https://claude.com/resources/articles/building-always-on-agents
6. Anthropic, *Introducing Claude Duet*, Oct 21 2026. https://claude.com/resources/articles/claude-duet
7. Claude Code docs. https://docs.claude.com/en/docs/claude-code/overview

**OpenAI**
8. OpenAI, *Introducing dots*, Sep 29 2026. https://openai.com/index/introducing-dots/
9. OpenAI Codex docs. https://developers.openai.com/codex

**DeepSeek / OpenHands / Meta / Z.ai / Nous**
10. DeepSeek Harness guide. https://deepseekharness.dev/
11. Artificial Analysis, *DeepSeek Harness: Coding Agent Index*. https://artificialanalysis.ai/agents/coding-agents/deepseek-harness
12. OpenHands paper, arXiv:2407.16741. https://arxiv.org/abs/2407.16741
13. OpenHands docs. https://docs.all-hands.dev/
14. Meta, *Muse Code*. https://dev.meta.ai/products/muse-code
15. Z.ai, *ZCode*. https://zcode.z.ai/en
16. Nous Research, *Hermes Agent*. https://hermes-agent.ai/

**Always-on products**
17. Continuum, *What is Grok Bot? xAI's always-on AI agents, explained*, Aug 2026. https://continuumcode.ai/guides/what-is-grok-bot/
18. Supermemory, *I reverse-engineered Instinct's memory*, Sep/Oct 2026. https://supermemory.ai/blog/reverse-engineering-instinct-memory/
19. Built In, *What Is Instinct AI?*, Oct 21 2026. https://builtin.com/articles/instinct-ai

**Papers**
20. Yao et al., *ReAct: Synergizing Reasoning and Acting in Language Models*, arXiv:2210.03629.
21. Shinn et al., *Reflexion: Language Agents with Verbal Reinforcement Learning*, arXiv:2303.11366.
22. Park et al., *Generative Agents: Interactive Simulacra of Human Behavior*, arXiv:2304.03442.
23. Packer et al., *MemGPT: Towards LLMs as Operating Systems*, arXiv:2310.08560.
24. Wang et al., *Executable Code Actions Elicit Better LLM Agents* (CodeAct), arXiv:2402.01030.
25. Liu et al., *Lost in the Middle: How Language Models Use Long Contexts*, arXiv:2307.03172.
26. Lin et al., *Sleep-time Compute: Beyond Inference Scaling at Test-time*, arXiv:2504.13171.
27. Cormack, Clarke, Buettcher, *Reciprocal Rank Fusion outperforms Condorcet and individual rank learning methods*, SIGIR 2009.
28. Robertson & Zaragoza, *The Probabilistic Relevance Framework: BM25 and Beyond*, 2009.
