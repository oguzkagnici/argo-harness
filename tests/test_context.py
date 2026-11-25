import pytest

from argo.core.context import ContextManager
from argo.core.types import LLMResponse, Message, Usage


class FakeCfg:
    class models:
        context_window = 2000
        max_output_tokens = 200
    compaction_threshold = 0.5
    reset_threshold = 0.95
    keep_recent_tokens = 100
    class spool_dir:
        pass


class FakeLLM:
    async def chat(self, messages, tools=None, **kwargs):
        return LLMResponse(message=Message(role="assistant", content="## GOAL\nfinish the task\n## NEXT\nstep 2"),
                           usage=Usage())


@pytest.mark.asyncio
async def test_microcompact_replaces_large_tool_output(tmp_path):
    cfg = FakeCfg()
    cfg.spool_dir = tmp_path
    cm = ContextManager(cfg, llm=FakeLLM())
    msgs = [Message(role="system", content="sys")]
    msgs += [Message(role="tool", content="x" * 5000, name="read_file") for _ in range(10)]
    msgs += [Message(role="user", content="latest")]
    changed = cm.microcompact(msgs, keep_recent=2, limit=500)
    assert changed >= 1
    assert any("elided during compaction" in (m.content or "") for m in msgs)
    assert list(tmp_path.glob("ctx_*.txt"))


@pytest.mark.asyncio
async def test_compaction_inserts_state_block():
    cm = ContextManager(FakeCfg(), llm=FakeLLM())
    msgs = [Message(role="system", content="sys")]
    for i in range(20):
        msgs.append(Message(role="user", content=f"message {i} " + "word " * 100))
    out = await cm.compact(msgs)
    assert any("compacted_context" in (m.content or "") for m in out)
    assert len(out) < len(msgs)


class FailingLLM:
    def __init__(self):
        self.calls = 0

    async def chat(self, messages, tools=None, **kwargs):
        self.calls += 1
        return LLMResponse(message=Message(role="assistant", content=""), usage=Usage())


class BigWindowCfg(FakeCfg):
    class models:
        context_window = 40_000
        max_output_tokens = 2_000
    compaction_threshold = 0.75
    reset_threshold = 0.95
    keep_recent_tokens = 100
    compact_reserve_tokens = 13_000
    compact_max_failures = 3
    time_based_microcompact = False
    microcompact_gap_minutes = 60
    microcompact_keep_recent = 5


def test_compaction_threshold_respects_reserve():
    cm = ContextManager(BigWindowCfg(), llm=FakeLLM())
    # budget 38k: fraction bound 28.5k, reserve bound 25k -> reserve wins
    assert cm.compaction_threshold() == 25_000


def test_compaction_threshold_fraction_binds_on_large_windows():
    cfg = BigWindowCfg()
    cfg.models.context_window = 1_000_000
    cfg.models.max_output_tokens = 8_000
    cm = ContextManager(cfg, llm=FakeLLM())
    assert cm.compaction_threshold() == int(992_000 * 0.75)


@pytest.mark.asyncio
async def test_compaction_circuit_breaker_stops_retrying():
    cfg = BigWindowCfg()
    cfg.models.context_window = 20_000
    cfg.models.max_output_tokens = 1_000
    llm = FailingLLM()
    cm = ContextManager(cfg, llm=llm)
    cm._last_usage = None
    msgs = [Message(role="system", content="sys")]
    msgs += [Message(role="tool", content="x" * 6000, name="read_file") for _ in range(8)]
    for _ in range(6):
        cm._last_usage = None
        cm.tool_tokens = 0
        msgs = await cm.manage(msgs)
    assert cm._compact_disabled
    assert llm.calls <= 3


@pytest.mark.asyncio
async def test_time_based_eviction_clears_stale_tool_results(tmp_path):
    cfg = BigWindowCfg()
    cfg.spool_dir = tmp_path
    cfg.time_based_microcompact = True
    cfg.microcompact_keep_recent = 3
    cm = ContextManager(cfg, llm=FakeLLM())
    msgs = [Message(role="system", content="sys")]
    msgs += [Message(role="tool", content=f"payload-{i}", name="read_file") for i in range(8)]
    cm._last_activity -= 3600 * 2
    changed = cm.evict_cached_prefix(msgs)
    assert changed == 5
    assert [m.content for m in msgs[1:]][:5] == ["[Old tool result content cleared]"] * 5
    assert msgs[-1].content == "payload-7"
    # a fresh session does not evict
    cm._last_activity = __import__("time").time()
    assert cm.evict_cached_prefix(msgs, now=cm._last_activity) == 0


def test_extract_fact_lines_newest_wins_and_bounded():
    from argo.core.context import extract_fact_lines
    msgs = [
        Message(role="tool", content="DB_HOST: old-db\nPORT: 5432\n"),
        Message(role="assistant", content="SHOULD_NOT_APPEAR: 1"),
        Message(role="tool", content="DB_HOST: new-db\nFACT-07: 123456-C\n"
                                     "see https://example.com/runbook\n"),
    ]
    ledger = extract_fact_lines(msgs)
    assert "DB_HOST: new-db" in ledger and "old-db" not in ledger
    assert "PORT: 5432" in ledger
    assert "SHOULD_NOT_APPEAR" not in ledger
    assert "https://example.com/runbook" in ledger
    assert len(extract_fact_lines(msgs, limit_chars=40)) <= 40


@pytest.mark.asyncio
async def test_compaction_keeps_verbatim_fact_ledger():
    cm = ContextManager(FakeCfg(), llm=FakeLLM())
    msgs = [Message(role="system", content="sys")]
    msgs += [Message(role="user", content="Check the database settings.")]
    msgs += [Message(role="tool", content="DB_HOST: prod-db-17\n" + "x" * 200, name="read_file")]
    head = [Message(role="tool", content="DB_HOST: prod-db-17\nFACT-11: 998877-Z\n" + "y" * 400, name="bash")]
    msgs_head = [msgs[0], msgs[1], msgs[2]]
    out = await cm.compact(msgs_head + head + [Message(role="user", content="continue")],
                           keep_recent_tokens=10)
    block = [m for m in out if m.role == "system" and "compacted_context" in (m.content or "")]
    assert block and "VERBATIM FACTS" in block[0].content
    assert "FACT-11: 998877-Z" in block[0].content
