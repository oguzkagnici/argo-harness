# Argo

A general-purpose, provider-agnostic **agent harness** for LLMs: web research, file/code execution, document
and slide-deck generation, hybrid retrieval, layered long-term memory with offline **dreaming**, subagents,
workflow extraction from computer-use traces, an OpenAI-compatible API, and an always-on daemon.

Argo runs against any OpenAI-compatible endpoint (`/chat/completions`, optional `/embeddings`, `/rerank`),
bring-your-own-key. It auto-inherits a provider from `~/.codex/config.toml` if present.

```bash
uv venv .venv --python 3.12 && . .venv/bin/activate
uv pip install -e ".[dev,computer]"
python -m playwright install chromium
argo doctor
argo run "Research anthropic agent memory, write report.docx, and build a 6-slide deck.pptx"
```

> Argo is a harness, not a model. Capability comes from the loop, tools, context engineering, retrieval,
> memory, and verification around the model.

---

## What it does

| Capability | How |
|---|---|
| Web research | `web_search` (DuckDuckGo/Tavily/Brave/SearxNG) + `fetch_url` (HTML→markdown, PDF, JSON), `wikipedia_search`, `arxiv_search` |
| Files & code | `read/write/edit/apply_patch`, `glob/grep`, `run_code` (Python/Node/bash), `bash` |
| Git | `git_status/diff/log/show/branches` (read-only) and approval-gated `git_commit`; no remote or history-rewriting operations |
| Documents | `create_document` → DOCX, PPTX, XLSX, PDF, MD, HTML, CSV from one structured spec; `read_document` verifies |
| Browser automation | `computer` (Playwright): screenshots for vision models + compact DOM, selectors/text/coords, persistent auth state |
| Long-term memory | SQLite, layered (`transcript → note → wiki → profile → procedure`), generational and reversible |
| Dreaming | Offline consolidation: dedupe, contradiction resolution against transcripts, wiki/insight/procedure extraction |
| Retrieval | Heading-aware chunks, BM25 + dense embeddings, RRF fusion, cross-encoder rerank, recency/importance boosts |
| Context management | Microcompaction → structured compaction → context reset with handoff artifacts |
| Subagents | Isolated context windows; parallel research/verification; independent evaluator (`review_work`) |
| Skills | Progressive-disclosure markdown skills (`.argo/skills/*/SKILL.md`) |
| Workflows | Extract a parameterized, replayable workflow + skill from a successful tool/browser trace |
| OpenAI API | `POST /v1/chat/completions` (stream + non-stream), `/v1/models`, `/v1/embeddings`, `/v1/rerank` |
| Always-on | Trigger inbox, durable goal queue, approvals, outbox, one-shot timers, scheduled dreaming |

## Architecture at a glance

```
                       ┌──────────────────────────── always-on daemon ───────────────────────────┐
                       │ triggers (interval/daily/webhook/once) → durable goal inbox → worker     │
                       │ approvals · outbox · follow-up timers · scheduled dream                  │
                       └───────────────────────────────┬──────────────────────────────────────────┘
                                                       │
 user / OpenAI client ──► CLI · HTTP API ──► AGENT LOOP │
                                                       ▼
   ┌─────────────── context manager ───────────────┐   ReAct-style loop
   │ microcompact → compact → reset + handoff      │   ├─ model call (tools, streaming, reasoning)
   │ stable system+tools prefix (prompt cache)     │   ├─ parallel read-only tool calls
   └───────────────────────┬───────────────────────┘   └─ tool results, images, usage accounting
                           ▼
   ┌──── memory ────┐  ┌──── retrieval ────┐  ┌──────── tools ────────┐
   │ transcript     │  │ BM25 + dense      │  │ files · shell         │
   │ notes          │  │ + RRF + rerank    │  │ web · docs · computer │
   │ wiki/profile   │  │ + recency/importance│ │ memory · plan · skills│
   │ procedures     │  └───────────────────┘  │ subagents · workflows │
   └───────┬────────┘                         └───────────────────────┘
           ▼
   dream engine (generation G → G+1, evidence-grounded, reversible)
```

## Install & configure

Python 3.11+. `uv` recommended.

```bash
uv venv .venv --python 3.12
. .venv/bin/activate
uv pip install -e ".[dev,computer]"
python -m playwright install chromium      # optional, for browser automation
```

### Bring your own key

Argo resolves configuration in this order: CLI flags → env → `~/.argo/config.toml` → `~/.codex/config.toml`.

```bash
export ARGO_API_KEY=sk-...                 # or OPENAI_API_KEY / LLM_API_KEY
export ARGO_BASE_URL=https://api.openai.com/v1
export ARGO_MODEL=gpt-4o-mini
export ARGO_MAX_CONCURRENCY=2              # hard cap on in-flight provider requests (default 2)
```

or `~/.argo/config.toml`:

```toml
[provider]
base_url = "https://your-endpoint.example/v1"
model = "your-model"
env_key = "MY_API_KEY"          # read key from this env var
# api_key = "..."                # discouraged: prefer env

[models]
context_window = 200000
max_output_tokens = 8192
embedding_model = "qwen3-embedding-8b"   # optional; enables dense retrieval
rerank_model = "qwen3-reranker-8b"       # optional; enables cross-encoder rerank

[search]
provider = "duckduckgo"                   # duckduckgo | tavily | brave | searxng
# tavily_api_key = "tvly-..."             # strongly recommended on cloud IPs
```

### MCP servers

Any [Model Context Protocol](https://modelcontextprotocol.io) server can contribute tools. Each server is
launched over stdio; tools appear as `mcp__<server>__<tool>` and are marked mutating unless you opt in to
`read_only`. A server that fails to start is reported and skipped.

```toml
[[mcp]]
name = "filesystem"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/data"]
read_only = true
# or keep the server mutating but exempt specific tools:
# read_only_tools = { search = true, write = false }
```

`read_only_tools` overrides the server default per tool (unknown tools stay mutating). `argo doctor` connects
configured servers and reports tool counts.

If no config exists, Argo inherits `model`, `base_url`, and `env_key` from Codex's config, so the same
provider/key works out of the box.

## CLI

```bash
argo run "task..."                  # one-shot, streamed
argo run --no-stream "task..."      # final answer only
argo run --plan "task..."           # read-only until one plan approval unlocks mutations
argo run --review adaptive "task..." # auto-review risky artifacts (docs/code) before finishing
argo chat                           # multi-turn REPL (/dream /memory /plan /new /stats)
argo serve --port 8000              # OpenAI-compatible + native API
argo daemon                         # always-on: triggers, queue, approvals, dreaming
argo dream --apply                  # consolidate memory now
argo memory list|search|add|stats|export --git|activate|dreams
argo index ./docs                   # embed workspace files for recall
argo workflow list|show|extract ID --name NAME|run NAME -p key=value
argo session list|show ID
argo trace ID                       # timeline of tools/usage/compactions for a session
argo metrics [--dir DIR]            # tokens, tool error rate, slowest tools, stop reasons
argo trigger add NAME "goal" --kind interval --seconds 3600 --read-only
argo goals | argo outbox | argo approvals list
argo doctor                         # end-to-end connectivity check
```

## OpenAI-compatible API

```bash
argo serve --port 8000
curl http://127.0.0.1:8000/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "argo",
  "messages": [{"role":"user","content":"Research X and write report.docx"}]
}'
```

Any OpenAI SDK works: point `base_url` at `http://127.0.0.1:8000/v1`. The response includes an `argo`
object with session id, iterations, files, compactions/resets, and token usage. Streaming uses standard SSE
chunks. Native `POST /v1/agent` streams structured events. Optional `--api-key` enforces a bearer token.

The same server exposes a live trace view: `GET /trace` lists recent sessions (stop reason, iterations, tokens,
tool calls/errors) and `GET /trace/<session>` shows the timeline, auto-refreshing every few seconds. JSON
equivalents are `GET /v1/sessions` and `GET /v1/sessions/<id>`; with `--api-key`, append `?key=…` for the HTML
pages or send the bearer header.

## Documents

One structured spec renders to DOCX, PPTX, XLSX, PDF, MD, HTML, CSV:

```python
{"title": "Q3 Review", "subtitle": "Results and next steps",
 "blocks": [
   {"type": "heading", "text": "Summary", "level": 1},
   {"type": "paragraph", "text": "Revenue grew **18%** QoQ."},
   {"type": "bullets", "items": ["APAC led growth", "Churn flat"]},
   {"type": "table", "rows": [["Metric", "Q2", "Q3"], ["Revenue", "1.2M", "1.42M"]]},
   {"type": "image", "path": "chart.png", "caption": "Quarterly revenue"}]}
```

PPTX decks use `slides` with `layout` in `title | bullets | section | two_column | quote | image | table | cards`,
themes (`slate | light | warm | forest | mono`), and speaker notes. The agent verifies outputs with
`read_document` and can run `review_work` with explicit acceptance criteria.

## Memory, dreaming, retrieval

- **Layers:** raw transcripts (ground truth, searchable) → atomic notes → wiki topic pages → durable profile →
  learned procedures. This follows Anthropic's published memory hierarchy.
- **Retrieval:** BM25 + embeddings fused with reciprocal-rank fusion, then reranked; memory hits get small
  recency/importance/usage boosts (Generative Agents scoring).
- **Dreaming:** an offline job reads recent transcripts + the active memory store, reconciles duplicates and
  contradictions, grounds claims in verbatim transcript evidence, builds wiki pages/procedures, and writes a
  **new generation** (`G → G+1`). The input generation is never mutated. `argo dream` reviews; `--apply`
  activates; `argo memory activate N` rolls back.
- **Compaction keeps facts:** a bounded verbatim key/value ledger survives summarization (measured 5/12 → 12/12
  fact recall for +768 tokens), so long runs can compact early without losing exact values.
- **Research integrity:** an evidence ledger assigns source ids and forbids claims that cite anything else;
  a claim verifier checks each citation and blocks unsupported findings (measured false-support 0.000 vs 1.000).
- **Safe delegation:** subagents can run in isolated git worktrees (`isolate=true`), so parallel code edits never
  collide with the parent checkout; plan mode (`--plan`) gates all mutations behind one approved plan.
- **Background work:** long research branches run as in-session background jobs; a single blocking
  `check_background` call replaces polling.
- **Extensible tools:** MCP servers are first-class, namespaced `mcp__server__tool`, with failure isolation
  and read-only annotations; the official Python SDK is used rather than a bespoke transport.
- **Bounded injection:** a small always-loaded block (profile + high-value notes + wiki index + procedures)
  mirrors Instinct/Hermes-style bounded memory files, with the full store searched on demand.

## Security model

- Writes outside the workspace are blocked unless explicitly allowed.
- Dangerous shell/code patterns (`rm -rf /`, `curl | bash`, `sudo`, force-push, destructive SQL, …) require
  interactive approval in CLI and are blocked + queued for approval in the daemon.
- The daemon is unattended: risky actions are deferred to an approvals queue; trigger jobs can be restricted
  to read-only tools.
- API keys are never written to transcripts, memory, logs, or tool output (redaction on events).
- Web, file, and document output is wrapped as untrusted content, with prompt rules against following embedded
  instructions and a hard block on URLs containing credentials or canaries.
- Browser automation is **not** a security sandbox. Use a dedicated profile/container for untrusted sites.
- The HTTP server is not multi-tenant; add auth, quotas, and per-user workspaces before exposing it.

## Evidence base

Every major design choice is tied to a proven harness or paper — see [`docs/RESEARCH.md`](docs/RESEARCH.md)
and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Highlights: ReAct, CodeAct, Reflexion, Generative Agents,
MemGPT, Letta sleep-time compute, Anthropic's memory/dreaming and long-running-harness work, Claude Code,
Codex, DeepSeek Harness, OpenHands, Instinct, Hermes, and Anthropic's always-on agent primitives.

## Docs

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — components, data model, design decisions and tradeoffs
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — decision log with evidence and measured performance impact
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — next capabilities, deep-research plan, benchmarks and milestones
- [`docs/REFERENCE_REVIEW.md`](docs/REFERENCE_REVIEW.md) — review of the leaked Claude Code reference tree and alternatives considered
- [`docs/RESEARCH.md`](docs/RESEARCH.md) — harness + always-on agent landscape, citations, what we adopted
- [`docs/COMPUTER_USE.md`](docs/COMPUTER_USE.md) — browser automation and workflow extraction
- [`docs/EVALS.md`](docs/EVALS.md) — verification runs, test coverage, measured benchmarks
- [`examples/`](examples/) — runnable demos

## Tests

```bash
pytest -q            # 203 unit/integration tests, no network required
argo doctor          # live provider check
```

## Benchmarks

Published protocols, runnable scripts and JSON records in [`benchmarks/`](benchmarks/README.md):

| Benchmark | Result (deepseek-v4.1-flash) |
|---|---|
| HumanEval, 20-task seeded subset | 20/20 pass@1 |
| BEIR SciFact, 100 queries / 1,000 docs | BM25 nDCG@10 0.779 · dense 0.885 · RRF 0.846 · RRF+rerank **0.916** |
| Memory learning loop, 6 corrections + dream | contradictory stale 1.00 → **0.00**; corrected recall 0.00 → **1.00** |
| Workflow replay, extracted `demo_login` × 5 | **5/5** success |
| Repair loop, 5 local repos + hidden tests | baseline **5/5** at 47k tok / 163s; with review **5/5** at 63k tok / 527s (+35% tokens, no accuracy gain) |
| Provider parity, chat vs responses | both paths pass tool calls, tool-result replay, and streaming; same token accounting |
| Live context, 36-step token chain (12k window) | **3/3** with compaction (275,722 tok / 3 compactions / 0 resets) vs **3/3** unmanaged (288,569 tok); ledger-off also 3/3 (265,657 tok). Churn bug found en route: 0/3, 11 compactions |
| Adaptive prompt injection (red-team loop) | static 0.0 / adaptive 0.0 ASR, utility 1.0 (1 case run; suite truncated by rate limits) |
| SWE-bench-Lite (official harness) | Docker path validated with the gold patch: **1/1 resolved**; Argo patch generated for 1 instance, not yet graded |

Next steps and open defects are tracked in [`docs/NEXT.md`](docs/NEXT.md).

## Known limitations

- Keyless web search is rate-limited from many cloud IPs; configure Tavily/Brave for reliability.
- Workflow extraction is LLM-induced: replay validates it, but DOM selectors remain brittle; expectations and
  retries are your friend.
- Dreaming can still hallucinate; evidence grounding and non-destructive generations reduce, not eliminate, risk.
- Both wire APIs are supported (`/chat/completions` and `/responses`); provider-specific quirks are covered by parity tests.
- MCP tools work over stdio (resources/prompts and HTTP transport are not implemented); no distributed worker/lease;
  cron is limited to interval/daily/weekly/once.
- Reasoning tokens count toward `max_tokens` on some providers; Argo defaults to a generous output budget.

## Layout

```
argo/
  config.py            provider/BYOK/workspace config (Codex inheritance)
  cli.py               run/chat/serve/daemon/dream/memory/workflow/trigger
  server.py            OpenAI-compatible + native HTTP API
  daemon.py            always-on runtime: inbox, triggers, goals, approvals, outbox
  core/                agent loop, context manager, sessions, prompts, events, tokens
  llm/client.py        chat/stream/tools/embeddings/rerank, retries
  memory/              store.py, dream.py, retrieval.py
  tools/               base, files, git, shell, web, docs, memory, plan, skills, computer
  workflows/           core (schema/runner), synth (trace→workflow), tools
benchmarks/            HumanEval, BEIR SciFact, memory loop, workflow replay
skills/                built-in progressive-disclosure skills
tests/                 unit + integration tests
docs/                  architecture, research, computer use, evals
```
