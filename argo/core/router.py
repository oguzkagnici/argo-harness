"""Per-turn model routing: explore on the cheap model, escalate to the strong one under risk.

Literature: RouteLLM (Ong et al., 2024) shows most of the strong-model quality can be kept by routing easy
queries to a weak model; FrugalGPT (Chen et al., 2023) shows a cascade with a verifier is a cheap way to get
there without a trained router. Training a router needs labelled cost/quality pairs we do not have yet, so this
implements the cheap, explainable half: a *policy* over observable trajectory signals.

Policy "explore":
  - read-only exploration turns (no mutating tool has run yet, last tool did not fail) -> cheap model
  - anything after a mutation or after a tool error -> strong model (recovery and final answers are the
    turns where a weak model hurts most)
Routing never applies to subagents or compaction: subagents inherit the parent client and compaction already
has its own cheap-model path.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

POLICIES = ("off", "explore")

_MUTATING_INTENT = re.compile(
    r"\b(writes?|wrote|creates?|creates?|edits?|modif(y|ies)|updates?|deletes?|removes?|renames?|"
    r"fix(es)?|implements?|refactors?|patches?|install(s|ed)?|deploys?|commits?|generates?|saves?|"
    r"appends?|moves?|mkdir|chmod|scaffolds?|adds?\s+(a|an|the)\s+file)\b", re.I)
_READ_ONLY_TASK = re.compile(r"\b(research|summari[sz]e|find|look ?up|explain|who|what|when|where|"
                             r"how many|report on|compare|answer|investigate|read)\b", re.I)


def intent_is_mutating(task: str) -> bool:
    """True when the task asks for a change on disk.

    Evidence for this split: on a read-only 3-hop research set the cheap model matched the strong model
    4/4 at 14% fewer tokens, but mutation/recovery turns are exactly where weak-model errors cost most.
    When intent is ambiguous we stay on the strong model (fail safe).
    """
    text = (task or "").strip()
    if not text:
        return False
    return bool(_MUTATING_INTENT.search(text)) and not (_READ_ONLY_TASK.search(text) and
                                                        not re.search(r"\b(edit|fix|implement|refactor|create|write)\b", text, re.I))


@dataclass
class TurnSignal:
    turn: int = 0
    last_tool: str = ""
    last_tool_error: bool = False
    wrote: bool = False           # a mutating tool has run in this task
    task_writes: bool = False     # the task itself asks for a change
    tool_calls: int = 0


class ModelRouter:
    def __init__(self, strong: str, cheap: str, policy: str = "explore") -> None:
        if policy not in POLICIES:
            raise ValueError(f"unknown router policy {policy!r}; choose {POLICIES}")
        self.strong = strong
        self.cheap = cheap
        self.policy = policy
        self.stats = {"cheap": 0, "strong": 0}

    @property
    def enabled(self) -> bool:
        return self.policy != "off" and bool(self.cheap) and self.cheap != self.strong

    def intent_is_mutating(self, task: str) -> bool:
        return self.enabled and intent_is_mutating(task)

    def choose(self, sig: TurnSignal) -> str | None:
        """Return the model for this turn, or None to use the configured default."""
        if not self.enabled or self.policy == "off":
            self.stats["strong"] += 1
            return None
        if sig.wrote or sig.task_writes or sig.last_tool_error:
            self.stats["strong"] += 1
            return None
        self.stats["cheap"] += 1
        return self.cheap

    def reason(self, sig: TurnSignal) -> str:
        if not self.enabled:
            return "router disabled"
        if sig.task_writes:
            return "strong model: task mutates state"
        if sig.wrote:
            return "escalated: a mutating tool has run"
        if sig.last_tool_error:
            return "escalated: previous tool failed"
        return "exploring on cheap model"
