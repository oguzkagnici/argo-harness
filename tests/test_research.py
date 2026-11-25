import json

import pytest

from argo.config import load_config
from argo.core.agent import Agent
from argo.core.types import LLMResponse, Message, Usage
from argo.research.ledger import EvidenceLedger
from argo.research.verify import verify_ledger


def test_ledger_ids_dedupe_and_report():
    led = EvidenceLedger()
    a = led.add_source("https://Example.com/docs/", "Throughput rose 12% in Q3.", "Ops review")
    b = led.add_source("https://example.com/docs", "Throughput rose 12% in Q3 and errors fell.", "Ops review")
    assert a.id == b.id == "S1" and len(led.sources) == 1
    assert "errors fell" in a.text  # longer content wins
    c1 = led.add_claim("Throughput rose 12%.", ["S1"])
    c2 = led.add_claim("Errors fell 40%.", ["S9"])
    assert (c1.id, c2.id) == ("C1", "C2")
    assert led.unknown_citations() == ["C2->S9"]
    md = led.to_markdown("Q3")
    assert "C1" not in md and "[OK] Throughput rose 12%. (S1)" in md.replace("[?]", "[OK]")
    assert "S1" in md and "## Sources" in md


class VerifyLLM:
    def __init__(self, verdict="supported"):
        self.verdict = verdict
        self.prompts = 0

    async def chat(self, messages, tools=None, **kwargs):
        self.prompts += 1
        body = json.dumps({"verdict": self.verdict, "reason": "checked", "evidence": "Throughput rose 12%"})
        return LLMResponse(message=Message(role="assistant", content=body), usage=Usage())


@pytest.mark.asyncio
async def test_verifier_marks_claims_and_handles_missing_citations():
    led = EvidenceLedger()
    led.add_source("https://a.example", "Throughput rose 12% in Q3.")
    ok = led.add_claim("Throughput rose 12% in Q3.", ["S1"])
    missing = led.add_claim("Latency halved.", ["S1"])
    uncited = led.add_claim("Revenue doubled.")
    llm = VerifyLLM("supported")
    stats = await verify_ledger(led, llm)
    assert ok.verdict == "supported" and ok.checked
    assert missing.verdict == "supported"
    assert uncited.verdict == "unsupported" and "no citation" in uncited.reason
    assert stats["support_rate"] == round(2 / 3, 4)
    assert stats["uncited"] == 1


@pytest.mark.asyncio
async def test_verifier_unsupported_and_bad_json():
    led = EvidenceLedger()
    led.add_source("https://a.example", "Throughput rose 12% in Q3.")
    claim = led.add_claim("Throughput tripled in Q3.", ["S1"])
    await verify_ledger(led, VerifyLLM("unsupported"))
    assert claim.verdict == "unsupported"

    class BadLLM:
        async def chat(self, messages, tools=None, **kwargs):
            return LLMResponse(message=Message(role="assistant", content="no json here"), usage=Usage())

    led2 = EvidenceLedger()
    led2.add_source("https://b.example", "text")
    claim2 = led2.add_claim("something", ["S1"])
    await verify_ledger(led2, BadLLM())
    assert claim2.verdict == "unverified"


class FlakyLLM:
    def __init__(self):
        self.calls = 0

    async def chat(self, messages, tools=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("429 from provider")
        return LLMResponse(message=Message(role="assistant", content=json.dumps(
            {"verdict": "supported", "reason": "ok", "evidence": "x"})), usage=Usage())


@pytest.mark.asyncio
async def test_verifier_retries_rate_limits(monkeypatch):
    import asyncio as real_asyncio
    import argo.research.verify as verify_mod
    original_sleep = real_asyncio.sleep
    monkeypatch.setattr(verify_mod.asyncio, "sleep", lambda _s: original_sleep(0))
    led = EvidenceLedger()
    led.add_source("https://c.example", "Throughput rose 12%.")
    claim = led.add_claim("Throughput rose 12%.", ["S1"])
    await verify_ledger(led, FlakyLLM())
    assert claim.verdict == "supported"


class ToolLLM:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def chat(self, messages, tools=None, **kwargs):
        text = "\n".join(m.content or "" for m in messages)
        if "CLAIM:" in text:
            return LLMResponse(message=Message(role="assistant", content=json.dumps(
                {"verdict": "supported", "reason": "matches", "evidence": "12%"})), usage=Usage())
        return LLMResponse(message=Message(role="assistant", content="done"), usage=Usage())

    async def embed(self, texts):
        return [[0.0] * 8 for _ in texts]

    async def rerank(self, query, docs, top_n=None):
        return [(float(len(docs) - i), i) for i in range(len(docs))][: top_n or len(docs)]


@pytest.mark.asyncio
async def test_research_tools_end_to_end(tmp_path):
    cfg = load_config({"workspace": str(tmp_path), "data_dir": str(tmp_path / ".argo")})
    async with Agent(cfg, llm=ToolLLM(), max_iterations=2) as agent:
        await agent.run("hi")
        src = await agent.registry.execute("add_source", {"url": "https://ops.example/q3",
                                                          "content": "Throughput rose 12% in Q3.",
                                                          "title": "Q3 ops"})
        assert src.metadata["source_id"] == "S1"
        claim = await agent.registry.execute("add_claim", {"text": "Throughput rose 12% in Q3.",
                                                           "source_ids": ["S1"]})
        assert claim.metadata["claim_id"] == "C1"
        bad = await agent.registry.execute("add_claim", {"text": "x", "source_ids": ["S7"]})
        assert bad.is_error
        verified = await agent.registry.execute("verify_claims", {})
        assert "supported" in verified.content and verified.metadata["support_rate"] == 1.0
        report = await agent.registry.execute("research_report", {"title": "Q3 findings"})
        assert "# Q3 findings" in report.content and "[OK] Throughput rose 12% in Q3. (S1)" in report.content
