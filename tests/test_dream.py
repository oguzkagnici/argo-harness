import json

import pytest

from argo.core.types import LLMResponse, Message, Usage
from argo.memory.dream import DreamEngine
from argo.memory.store import MemoryStore


class FakeLLM:
    def __init__(self, payload):
        self.payload = payload

    async def chat(self, messages, tools=None, **kwargs):
        return LLMResponse(message=Message(role="assistant", content=json.dumps(self.payload)),
                           usage=Usage())


@pytest.mark.asyncio
async def test_dream_clones_generation_supersedes_and_grounds(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite")
    stale = await store.add("Staging DB is db1.internal", layer="note", kind="fact", importance=0.7)
    store.log("s1", "user", "The staging DB moved to db2.internal after the migration.")
    store.log("s1", "assistant", "Updated the connection string to db2.internal and verified it works.")

    payload = {
        "observations": [
            {"text": "Staging DB is db2.internal", "kind": "fact", "importance": 0.8, "confidence": 0.9,
             "evidence": "The staging DB moved to db2.internal"},
            {"text": "The moon is made of brie", "kind": "fact", "importance": 0.9, "confidence": 0.95,
             "evidence": "", "tags": []},
        ],
        "profile": [],
        "reconcile": [{"target_ids": [stale], "action": "supersede",
                       "new_text": "Staging DB is db2.internal", "reason": "moved",
                       "confidence": 0.9}],
        "wiki": [{"title": "Staging", "text": "Staging DB host is db2.internal.", "links": []}],
        "insights": [{"text": "Hosts change; treat endpoints as versioned", "confidence": 0.6, "basis": "s1"}],
        "procedures": [{"title": "Update DB endpoint", "when_to_use": "host changes",
                        "steps": ["edit env", "verify"], "confidence": 0.8}],
        "summary": "superseded stale host",
    }
    eng = DreamEngine(store, FakeLLM(payload), cfg=FakeCfg())
    report = await eng.run(apply=True)

    assert report.status == "applied"
    assert report.input_generation == 0 and report.output_generation == 1
    assert store.active_generation == 1
    active = store.list_items()
    texts = [i.text for i in active]
    assert any("db2" in t for t in texts)
    assert not any("db1.internal" in t and i.status == "active" for i, t in zip(active, texts))
    brie = next(i for i in active if "brie" in i.text)
    assert brie.confidence <= 0.5            # ungrounded claim downgraded
    assert any(i.layer == "wiki" for i in active)
    assert any(i.layer == "procedure" for i in active)


class FakeCfg:
    models = type("M", (), {"max_output_tokens": 8000})()


@pytest.mark.asyncio
async def test_dream_rollback_restores_previous_generation(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite")
    await store.add("Original fact: color is blue", layer="note", kind="fact")
    store.log("s1", "user", "Correction: the color is green now.")
    payload = {"observations": [{"text": "Color is green", "kind": "fact", "importance": 0.8,
                                 "confidence": 0.9, "evidence": "the color is green"}],
               "reconcile": [], "summary": "updated color"}
    eng = DreamEngine(store, FakeLLM(payload), cfg=FakeCfg())
    report = await eng.run(session_ids=["s1"], apply=True)
    assert store.active_generation == 1
    assert any("green" in i.text for i in store.list_items())

    eng.rollback(report.input_generation)
    assert store.active_generation == 0
    assert not any("green" in i.text for i in store.list_items())
    assert any("blue" in i.text for i in store.list_items())
