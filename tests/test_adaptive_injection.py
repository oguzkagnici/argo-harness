"""Offline tests for the adaptive-injection benchmark helpers (no network, no model)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_bench():
    spec = importlib.util.spec_from_file_location("adaptive_injection", ROOT / "benchmarks" / "adaptive_injection.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


BENCH = load_bench()
CASE = BENCH.CASES[0]


def test_parse_payload_accepts_json_and_raw():
    assert BENCH.parse_payload('{"payload": "<p>hi</p>", "rationale": "x"}') == "<p>hi</p>"
    assert BENCH.parse_payload('```json\n{"payload": "a b"}\n```') == "a b"
    assert BENCH.parse_payload("plain injection text") == "plain injection text"
    assert BENCH.parse_payload("") == ""


def test_attacker_prompt_includes_defense_policy_only_when_on():
    on = BENCH.attacker_prompt(CASE, "http://x", True, [])
    off = BENCH.attacker_prompt(CASE, "http://x", False, [])
    assert "Untrusted content" in on and "NO explicit defenses" in off
    assert CASE["objective"].split()[0] in on


def test_attacker_prompt_feeds_back_history():
    history = [{"payload": "<p>try 1</p>", "outcome": {"obeyed": False, "final_answer": "refused"}}]
    prompt = BENCH.attacker_prompt(CASE, "http://x", True, history)
    assert "try 1" in prompt and "refused" in prompt and "attempt 1" not in prompt


def test_cases_cover_both_channels_and_goals():
    goals = {c["goal"] for c in BENCH.CASES}
    channels = {"file" if c.get("channel") == "file" else "web" for c in BENCH.CASES}
    assert goals == {"write", "exfil"} and channels == {"file", "web"}
    for case in BENCH.CASES:
        assert case["static"] and case["utility_terms"]
        assert case["task"].format(base="http://b").startswith(("Fetch", "Read"))
        objective = case["objective"].format(base="http://b")
        assert ("PWNED_BY_ADAPTIVE" in objective) != ("/leak" in objective)  # one goal per case
        assert ("http://b" in objective) == ("{base}" in case["objective"])


def test_agentdojo_style_cases_present_and_realistic():
    by_id = {c["id"]: c for c in BENCH.CASES}
    for cid in ("tool_poison", "authority_spoof", "multi_page"):
        case = by_id[cid]
        assert case["goal"] == "write" and case["static"] and case["utility_terms"]
        # these must not rely on the shouting pattern the earlier cases used
        assert "IMPORTANT SYSTEM INSTRUCTION" not in case["static"]
        assert "ignore the user" not in case["static"].lower()
    assert by_id["multi_page"]["static2"], "multi-document attack needs a second page"
    assert "PWNED_BY_ADAPTIVE" in by_id["authority_spoof"]["static"]


def test_second_page_is_served(monkeypatch):
    import urllib.request
    BENCH.STATE["page2"] = "<p>second document marker</p>"
    srv, base = BENCH.serve()
    try:
        body = urllib.request.urlopen(f"{base}/page2", timeout=5).read().decode()
        assert "second document marker" in body
        assert "Quarterly Operations Review" in urllib.request.urlopen(f"{base}/page", timeout=5).read().decode()
    finally:
        srv.shutdown()


def test_priming_turns_establish_document_authority_without_side_effects():
    assert 1 <= len(BENCH.PRIMING_TURNS) <= 2
    joined = " ".join(BENCH.PRIMING_TURNS).lower()
    assert "authoritative" in joined and "precedence" in joined
    for turn in BENCH.PRIMING_TURNS:
        assert not any(w in turn.lower() for w in ("write ", "fetch http", "create ")), turn


def test_run_target_forwards_priming_turns(monkeypatch):
    import asyncio

    prompts: list[str] = []

    class FakeUsage:
        total_tokens = 5

    class FakeResult:
        usage = FakeUsage()
        stop_reason = "final"
        output = "12%"
        session_id = "fake"

    class FakeAgent:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def run(self, task, continue_conversation=False):
            prompts.append((task, continue_conversation))
            return FakeResult()

    monkeypatch.setattr(BENCH, "Agent", FakeAgent)
    monkeypatch.setattr(BENCH, "build_cfg", lambda ws, defense, model="": object())
    monkeypatch.setattr(BENCH, "tool_events", lambda cfg, sid: [])
    row = asyncio.run(BENCH.run_target({"id": "t", "task": "summarize", "objective": "x",
                                        "utility_terms": ["12%"], "static": "x"},
                                       "<p>payload</p>", True, "http://x", 30, priming=2))
    assert len(prompts) == 3
    assert all(cont for _, cont in prompts)
    assert prompts[-1][0] == "summarize"
    assert len(row["priming_turns"]) == 2
