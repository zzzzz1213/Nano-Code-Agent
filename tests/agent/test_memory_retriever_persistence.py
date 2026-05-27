from pathlib import Path
import json
import tempfile

from nanobot.agent.retriever import MemoryRetriever


def test_persist_and_load(tmp_path: Path):
    r = MemoryRetriever()
    comp = {
        "id": "c1",
        "summary_full": "Overview:\n- Decided to refactor X\nNext steps:\n- run tests",
        "meta": {"session_key": "websocket:abc123"},
    }
    r.index_compactions([comp])
    assert r.query("refactor")
    out = tmp_path / "idx.json"
    r.persist_index(out)

    r2 = MemoryRetriever()
    # initially empty
    assert not r2.query("refactor")
    r2.load_index(out)
    res = r2.query("refactor")
    assert len(res) >= 1
    assert res[0]["meta"]["session_key"] == "websocket:abc123"
