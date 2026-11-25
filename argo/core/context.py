"""Context engineering: budgeting, microcompaction, structured compaction, context resets.

Three escalating mechanisms, matching what works in frontier harnesses:

1. Microcompaction - replace old, large tool outputs with a short digest + spool pointer.
2. Compaction       - summarize the oldest span into a structured STATE block in place
                      (preserves continuity, keeps one agent).
3. Context reset    - write a handoff artifact and start a fresh window with it
                      (fixes "context anxiety" and long-horizon drift; costs a handoff).

The system prompt and tool schemas stay byte-stable at the front to maximize
prompt-cache hits; context is append-only until compaction.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .tokens import estimate_messages, estimate_tokens
from .types import Message, ToolCall

SUMMARY_PROMPT = """You are compacting the conversation history of an autonomous agent so it can keep working.

Write a dense, structured state document that preserves everything needed to continue the task:
1. GOAL & CONSTRAINTS: the user's actual request, acceptance criteria, explicit preferences.
2. PROGRESS: what has been completed and verified (with file paths / commands).
3. KEY FINDINGS: important facts, data, decisions, and why (include numbers, names, URLs).
4. FILES & ARTIFACTS: every file created/modified and what it contains.
5. TOOLS & ENVIRONMENT: relevant commands, APIs, credentials *names* (never values), quirks.
6. OPEN ISSUES: errors, blockers, failed attempts (do not repeat them), and next steps.
7. USER PREFERENCES: anything the user asked for about style, format, or behavior.

Rules:
- Be specific and information-dense; this replaces the raw history.
- Preserve exact identifiers, paths, numbers, and quotes that matter.
- Do not invent. Mark uncertain items as unverified.
- Output markdown only, no preamble.

CONVERSATION:
{history}
"""

FACT_LINE_RE = re.compile(r"^\s{0,4}([A-Za-z][A-Za-z0-9_.\-/ ]{0,47})\s*(?::|=)\s*(\S.{0,160})$", re.M)
URL_RE = re.compile(r"https?://[^\s)<>\"']+")
# Identifiers that appear in running prose rather than `key: value` lines. The live 36-step benchmark lost
# checkpoint ids ("Checkpoint id T03-613ad6") because the ledger only matched key/value lines; these patterns
# generalize that case to UUIDs, commit hashes and emails (all paraphrased away by narrative summaries).
IDENTIFIER_RES = [
    re.compile(r"\b[A-Z][A-Z0-9]{0,7}[-_][0-9a-fA-F]{4,32}\b"),      # T03-613ad6, REQ-9f2c11
    re.compile(r"\b[A-Z]{2,8}-\d{2,8}\b"),                          # ticket ids: ENG-1042
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
    re.compile(r"\b(?:sha|commit|rev(?:ision)?)[:=\s]+([0-9a-f]{7,40})\b", re.I),
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b"),
]
MAX_IDENTIFIERS = 60
FACT_LEDGER_CHARS = 6000


def extract_fact_lines(messages: Sequence[Message], limit_chars: int = FACT_LEDGER_CHARS) -> str:
    """Pull verbatim key/value facts out of untrusted tool output before summarization.

    Narrative summaries paraphrase and drop identifiers (measured: compaction cut fact recall from
    12/12 to 2/12). Keeping the raw ``key: value`` lines, newest-wins and bounded, restores the
    details at a fraction of the space. See benchmarks/context_bench.py.
    """
    seen: dict[str, str] = {}
    order: list[str] = []
    for msg in messages:
        if msg.role not in ("tool", "user") or not msg.content:
            continue
        for key, value in FACT_LINE_RE.findall(msg.content):
            key = " ".join(key.split())
            if len(key) < 2 or value.lstrip().startswith(("//", "#")):
                continue
            norm = key.casefold()
            if norm not in seen:
                order.append(norm)
            seen[norm] = f"{key}: {value.strip()}"
        for url in URL_RE.findall(msg.content):
            norm = "url:" + url
            if norm not in seen:
                order.append(norm)
            seen[norm] = f"URL: {url}"
        for pattern in IDENTIFIER_RES:
            for match in pattern.findall(msg.content):
                value = match if isinstance(match, str) else match[0]
                if len(value) < 5 or value.casefold() in seen:
                    continue
                norm = "id:" + value.casefold()
                if norm not in seen:
                    order.append(norm)
                seen[norm] = f"ID: {value}"
        if len(order) > MAX_IDENTIFIERS * 4:  # bound scanning memory on huge tool dumps
            order = order[-MAX_IDENTIFIERS * 4:]
    lines: list[str] = []
    used = 0
    for norm in reversed(order):
        line = seen[norm]
        if used + len(line) + 3 > limit_chars:
            continue
        lines.append(f"- {line}")
        used += len(line) + 3
    lines.reverse()
    return "\n".join(lines)

HANDOFF_PROMPT = """Create a handoff briefing for a FRESH agent instance that will continue this task with an empty context window.

It must be self-contained and actionable:
- The goal and acceptance criteria.
- What is done and verified vs. what is assumed.
- Exact state: files, commands, servers, data locations, branch names.
- Next 3-10 concrete steps, ordered.
- Pitfalls already discovered and how to avoid them.
- Anything the user said about preferences or constraints.

Output markdown only. Be concise but complete. An agent with no prior context must be able to continue immediately.

STATE SUMMARY:
{summary}

RECENT MESSAGES:
{recent}
"""


@dataclass
class ContextStats:
    compactions: int = 0
    resets: int = 0
    microcompactions: int = 0
    truncated_chars: int = 0
    peak_tokens: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)


class ContextManager:
    def __init__(self, cfg: Any, context_window: int | None = None, llm: Any = None,
                 events: Any = None):
        self.cfg = cfg
        self.context_window = context_window or cfg.models.context_window
        self.llm = llm
        self.events = events
        self.stats = ContextStats()
        self.reserve_output = cfg.models.max_output_tokens
        self.tool_tokens = 0
        self._last_usage: int | None = None
        self._last_activity = time.time()
        self._compact_failures = 0
        self._compact_disabled = False
        self._compact_insufficient = False
        self._compact_after = 0  # tokens right after the last successful compaction (hysteresis)
        self._ineffective_compactions = 0
        self.cache_evictions = 0

    def budget(self) -> int:
        return max(4096, self.context_window - self.reserve_output)

    def compaction_threshold(self) -> int:
        """Token count that triggers compaction.

        Config keeps a fraction of the window, but never less headroom than ``compact_reserve_tokens``
        below the window, so a summary is always requested before the provider rejects the prompt.
        """
        budget = self.budget()
        reserve = getattr(self.cfg, "compact_reserve_tokens", 13_000)
        by_fraction = int(budget * self.cfg.compaction_threshold)
        by_reserve = budget - reserve
        if by_reserve <= 0:
            return by_fraction
        return max(1024, min(by_fraction, by_reserve))

    def tokens(self, messages: Sequence[Message]) -> int:
        est = estimate_messages(messages) + self.tool_tokens
        # calibrate: provider-reported prompt tokens are authoritative when available
        if self._last_usage and estimate_messages(messages) > 0:
            return self._last_usage
        return est

    def observe_usage(self, prompt_tokens: int, message_count: int) -> None:
        if prompt_tokens:
            self._last_usage = prompt_tokens
            self.stats.peak_tokens = max(self.stats.peak_tokens, prompt_tokens)

    def _emit(self, kind: str, **data: Any) -> None:
        if self.events:
            self.events.emit(kind, **data)

    def _spill(self, content: str, label: str) -> Path:
        d = Path(self.cfg.spool_dir)
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"ctx_{int(time.time())}_{abs(hash(label)) % 10**6}.txt"
        p.write_text(content, encoding="utf-8", errors="replace")
        return p

    # ------------------------------------------------------------ microcompact
    def evict_cached_prefix(self, messages: list[Message], now: float | None = None) -> int:
        """Clear old tool results once the prompt cache is certainly cold.

        Mirrors Claude Code's time-based microcompact: the server cache TTL is one hour, so after a
        longer idle gap the prefix will be rewritten regardless. Shrinking it first saves input tokens.
        """
        if not getattr(self.cfg, "time_based_microcompact", True):
            return 0
        gap = (now or time.time()) - self._last_activity
        if gap < float(getattr(self.cfg, "microcompact_gap_minutes", 60.0)) * 60:
            return 0
        keep = int(getattr(self.cfg, "microcompact_keep_recent", 5))
        tool_idx = [i for i, m in enumerate(messages) if m.role == "tool"]
        stale = tool_idx[:-keep] if keep else tool_idx
        changed = 0
        for i in stale:
            msg = messages[i]
            if msg.content and msg.content != "[Old tool result content cleared]":
                msg.content = "[Old tool result content cleared]"
                changed += 1
        if changed:
            self.cache_evictions += changed
            self._last_usage = None
            self._emit("context_cache_eviction", cleared=changed, gap_minutes=round(gap / 60, 1))
        return changed

    def microcompact(self, messages: list[Message], keep_recent: int | None = None,
                     limit: int = 1400) -> int:
        if keep_recent is None:
            keep_recent = 8
        changed = 0
        for msg in messages[:-keep_recent]:
            if msg.role == "tool" and msg.content and len(msg.content) > limit:
                spilled = len(msg.content) - limit
                path = self._spill(msg.content, "tool")
                msg.content = (msg.content[: int(limit * 0.7)] +
                               f"\n\n[... {spilled:,} chars elided during compaction; full output: {path} ...]\n\n" +
                               msg.content[-int(limit * 0.2):])
                self.stats.truncated_chars += spilled
                changed += 1
            elif msg.role == "assistant" and msg.reasoning and len(msg.reasoning) > 4000:
                msg.reasoning = msg.reasoning[:2000] + "\n…[reasoning elided]"
                changed += 1
        # drop stale reasoning entirely (not sent to provider, but reclaims memory)
        for msg in messages[:-2]:
            if msg.role == "assistant":
                msg.reasoning = None
        self.stats.microcompactions += changed
        if changed:
            self._emit("context_microcompact", replaced=changed)
        return changed

    # ------------------------------------------------------------- compaction
    async def compact(self, messages: list[Message], keep_recent_tokens: int | None = None) -> list[Message]:
        keep_recent_tokens = keep_recent_tokens or self.cfg.keep_recent_tokens
        if not self.llm or len(messages) < 4:
            return messages
        # split: system(s) + oldest span to summarize + recent tail
        sys_msgs = [m for m in messages if m.role == "system"]
        convo = [m for m in messages if m.role != "system"]
        tail: list[Message] = []
        tokens = 0
        for m in reversed(convo):
            t = estimate_tokens(m.content or "") + estimate_tokens(m.reasoning or "") + 8
            if tokens + t > keep_recent_tokens and tail:
                break
            tail.insert(0, m)
            tokens += t
        head = convo[: len(convo) - len(tail)]
        # Summarizing a 1-2 message head costs a full summary call and buys almost nothing; require a
        # substantive span. This prevents the live-run churn where every turn re-summarized 2 messages.
        if len(head) < int(getattr(self.cfg, "compact_min_head_messages", 3)):
            self._compact_insufficient = True
            return messages
        history = self._render_history(head)
        prompt = SUMMARY_PROMPT.format(history=history[:120_000])
        try:
            resp = await self.llm.chat([Message(role="user", content=prompt)],
                                       tools=None, temperature=0.1,
                                       max_tokens=min(4000, self.cfg.models.max_output_tokens),
                                       model=getattr(self.cfg, "cheap_model", "") or None)
            summary = (resp.message.content or "").strip()
        except Exception:
            summary = ""
        if not summary:
            return messages
        facts = extract_fact_lines(head) if getattr(self.cfg, "compact_fact_ledger", True) else ""
        block_text = ("<compacted_context>\nThe earlier part of this session was compacted. "
                      "State summary:\n\n" + summary)
        if facts:
            block_text += ("\n\nVERBATIM FACTS (extracted before summarization; authoritative "
                           "for exact values):\n" + facts)
            self._emit("context_fact_ledger", lines=facts.count("\n") + 1, chars=len(facts))
        block_text += "\n</compacted_context>"
        block = Message(role="system", content=block_text)
        new_messages = sys_msgs[:1] + [block] + sys_msgs[1:] + tail
        self._last_usage = None
        self.stats.compactions += 1
        self.stats.history.append({"type": "compaction", "summarized_messages": len(head),
                                   "summary_chars": len(summary), "ts": time.time()})
        self._emit("context_compacted", summarized=len(head), kept=len(tail), chars=len(summary))
        return new_messages

    # ---------------------------------------------------------------- reset
    async def make_handoff(self, messages: list[Message], state_summary: str = "") -> tuple[str, list[Message]]:
        convo = [m for m in messages if m.role != "system"]
        recent = self._render_history(convo[-12:])[:30_000]
        prompt = HANDOFF_PROMPT.format(summary=state_summary[:4000] or "(see recent messages)", recent=recent)
        try:
            resp = await self.llm.chat([Message(role="user", content=prompt)], tools=None,
                                       temperature=0.1, max_tokens=min(3000, self.cfg.models.max_output_tokens),
                                       model=getattr(self.cfg, "cheap_model", "") or None)
            handoff = (resp.message.content or "").strip()
        except Exception:
            handoff = ""
        if not handoff:
            handoff = state_summary or "Continue the task from the recent messages."
        facts = extract_fact_lines(convo) if getattr(self.cfg, "compact_fact_ledger", True) else ""
        if facts:
            handoff += ("\n\n## VERBATIM FACTS (authoritative; copied from raw history, do not paraphrase)\n"
                        + facts)
        path = Path(self.cfg.session_dir) / f"handoff_{int(time.time())}.md"
        path.write_text(handoff, encoding="utf-8")
        keep = convo[-4:] if convo else []
        note = Message(role="system", content=(
            "<handoff>\nYou are continuing a task in a fresh context window. "
            f"A full handoff was written to {path}. Read it with read_file if you need details.\n\n"
            + handoff + "\n</handoff>"))
        self.stats.resets += 1
        self.stats.history.append({"type": "reset", "handoff": str(path), "ts": time.time()})
        self._emit("context_reset", handoff=str(path))
        return handoff, keep + [note]

    # ---------------------------------------------------------------- policy
    async def manage(self, messages: list[Message], state_summary: str = "") -> list[Message]:
        if not getattr(self.cfg, "compaction_enabled", True):
            return messages
        limit = self.budget()
        threshold = self.compaction_threshold()
        self.evict_cached_prefix(messages)
        tokens = self.tokens(messages)
        growth = int(getattr(self.cfg, "compact_min_growth_tokens", 1200))
        over_budget = tokens >= limit  # hard ceiling: act even inside the hysteresis window
        rearm = max(threshold, self._compact_after + growth)
        if not over_budget and tokens < rearm:
            self._last_activity = time.time()
            return messages
        # 1) reclaim space cheaply
        self.microcompact(messages)
        self._last_usage = None
        tokens = estimate_messages(messages) + self.tool_tokens
        if tokens < threshold:
            self._last_activity = time.time()
            return messages
        if self._compact_disabled:
            self._last_activity = time.time()
            return messages
        # 2) summarize in place (bounded retries: a failing summary must not burn a call every turn)
        before = messages
        self._compact_insufficient = False
        messages = await self.compact(messages)
        if messages is before and over_budget and not self._compact_insufficient:
            # still full and nothing summarized: shrink the retained tail before giving up
            messages = await self.compact(before, keep_recent_tokens=max(600, int(self.cfg.keep_recent_tokens // 2)))
        if messages is before:
            if not self._compact_insufficient:  # a real failure (empty summary), not "nothing to compact"
                self._compact_failures += 1
                if self._compact_failures >= int(getattr(self.cfg, "compact_max_failures", 3)):
                    self._compact_disabled = True
                    self._emit("context_compact_disabled", failures=self._compact_failures)
        else:
            self._compact_failures = 0
            self._compact_after = estimate_messages(messages) + self.tool_tokens
            if self._compact_after >= threshold:
                # Compaction did not get below the trigger: one more is fine, endless churn is not.
                self._ineffective_compactions += 1
                if self._ineffective_compactions >= 2:
                    self._compact_disabled = True
                    self._emit("context_compact_ineffective", after=self._compact_after, threshold=threshold)
            else:
                self._ineffective_compactions = 0
        self._last_usage = None
        tokens = estimate_messages(messages) + self.tool_tokens
        # 3) Reset is a last resort: only when the prompt is still above the hard budget after compaction.
        # The live 12k run reset whenever the prompt crossed reset_threshold — including turns where compaction
        # was deliberately deferred by hysteresis — and that discarded recall.
        if tokens > limit:
            _handoff, tail = await self.make_handoff(messages, state_summary)
            system = [m for m in messages if m.role == "system"][:1]
            messages = system + tail
            self._last_usage = None
        self._last_activity = time.time()
        return messages

    # -------------------------------------------------------------- rendering
    def _render_history(self, messages: Sequence[Message], limit: int = 60) -> str:
        lines: list[str] = []
        for m in messages[-limit:]:
            if m.role == "tool":
                body = (m.content or "")[:1200]
                lines.append(f"TOOL[{m.name or m.tool_call_id}]: {body}")
            elif m.role == "assistant":
                if m.content:
                    lines.append(f"ASSISTANT: {m.content[:1600]}")
                for tc in m.tool_calls:
                    args = tc.arguments[:400]
                    lines.append(f"ASSISTANT->TOOL_CALL {tc.name}({args})")
            else:
                lines.append(f"{m.role.upper()}: {(m.content or '')[:1600]}")
        return "\n".join(lines)
