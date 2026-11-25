import pytest

from argo.memory.store import MemoryStore


@pytest.mark.asyncio
async def test_add_retrieve_generation_and_dedupe(tmp_path):
    store = MemoryStore(tmp_path / "mem.sqlite")
    first = await store.add("Argo uses SQLite for the memory store", layer="note", kind="fact", importance=0.8)
    again = await store.add("Argo uses SQLite for the memory store", layer="note", kind="fact", importance=0.5)
    assert first == again                     # exact duplicates update in place
    await store.add("Cats are unrelated", layer="note", kind="fact", importance=0.1)
    assert store.active_generation == 0
    hits = await store.retrieve("what database does argo use", k=3)
    assert hits and "SQLite" in hits[0].text
    assert len(store.list_items(layer="note")) == 2
    removed = store.dedupe()
    assert removed == 0

    store._set_meta("active_generation", "1")
    assert store.list_items() == []
    store.close()


def test_transcripts_and_sessions(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite")
    store.log("s1", "user", "hello")
    store.log("s1", "assistant", "hi")
    store.log("s2", "user", "other")
    assert store.session_ids() == ["s2", "s1"]
    assert len(store.transcripts(session_ids=["s1"])) == 2
    store.close()


def test_link_graph(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite")
    store.link("a", "b", "related")
    assert store.neighbors("a")[0]["dst"] == "b"
    store.close()
