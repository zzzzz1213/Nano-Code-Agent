from pathlib import Path

from nanobot.agent import memory as memory_module
from nanobot.agent.context import ContextBuilder
from nanobot.agent.retriever import MemoryRetriever


def test_context_builder_injects_retrieved_memories(tmp_path: Path):
    # prepare workspace and memory dir
    workspace = tmp_path
    memdir = workspace / "memory"
    memdir.mkdir(parents=True, exist_ok=True)

    # create a compaction doc that should match query 'refactor'
    comp = {
        "id": "doc-1",
        "summary_full": "Decision: refactor module X to improve testability and speed.",
        "meta": {"session_key": "websocket:test"},
        "updated_at": "2026-05-01T12:00:00",
    }

    # create a local retriever, index and persist to the workspace memory path
    r = MemoryRetriever()
    r.index_compactions([comp], replace=True)
    idx_path = memdir / "retriever_index.json"
    r.persist_index(idx_path)
    # ensure the module-level retriever loads the persisted index
    memory_module.retriever.load_index(idx_path)
    # sanity check: module-level retriever should now return results for 'refactor'
    hits = memory_module.retriever.query("refactor")
    assert hits, f"retriever.query returned no hits; docs={list(memory_module.retriever._docs.keys())}"

    # instantiate ContextBuilder which will cause MemoryStore to load the index
    cb = ContextBuilder(workspace)

    # call build_system_prompt with session_summary that triggers retrieval
    prompt = cb.build_system_prompt(session_summary="refactor")

    assert "[Retrieved Memories]" in prompt
    assert "refactor module X" in prompt
    assert "category: decision" in prompt
    assert "reason: term:refactor" in prompt


def test_context_builder_retrieves_from_structured_session_summary(tmp_path: Path):
    workspace = tmp_path
    memdir = workspace / "memory"
    memdir.mkdir(parents=True, exist_ok=True)
    comp = {
        "id": "doc-failure",
        "summary_sections": {
            "failures": ["pytest failed in tests/agent/test_memory_retriever.py"],
            "commands_run": ["pytest tests/agent/test_memory_retriever.py -q"],
        },
        "meta": {"session_key": "websocket:test"},
        "updated_at": "2026-05-02T12:00:00",
    }
    r = MemoryRetriever()
    r.index_compactions([comp], replace=True)
    idx_path = memdir / "retriever_index.json"
    r.persist_index(idx_path)
    memory_module.retriever.load_index(idx_path)
    cb = ContextBuilder(workspace)

    prompt = cb.build_system_prompt(
        session_metadata={
            "_last_summary": {
                "sections": {
                    "failures": ["pytest failed in test_memory_retriever.py"],
                }
            }
        }
    )

    assert "[Retrieved Memories]" in prompt
    assert "category: command" in prompt
    assert "reason: path:test_memory_retriever.py" in prompt


def test_context_builder_prefers_path_aligned_memory_over_generic_failure(tmp_path: Path):
    workspace = tmp_path
    memdir = workspace / "memory"
    memdir.mkdir(parents=True, exist_ok=True)
    compactions = [
        {
            "id": "generic-timeout",
            "summary_full": "Failure: timeout while syncing gateway metadata.",
            "meta": {"session_key": "websocket:generic"},
            "updated_at": "2026-05-01T12:00:00",
        },
        {
            "id": "websocket-timeout",
            "summary_sections": {
                "failures": ["Timeout in nanobot/channels/websocket.py during reconnect flow."],
                "files_touched": ["nanobot/channels/websocket.py"],
            },
            "meta": {"session_key": "websocket:path"},
            "updated_at": "2026-05-02T12:00:00",
        },
    ]
    r = MemoryRetriever()
    r.index_compactions(compactions, replace=True)
    idx_path = memdir / "retriever_index.json"
    r.persist_index(idx_path)
    memory_module.retriever.load_index(idx_path)
    cb = ContextBuilder(workspace)

    prompt = cb.build_system_prompt(
        current_message="Investigate timeout in nanobot/channels/websocket.py"
    )

    retrieved_lines = [
        line for line in prompt.splitlines()
        if line.startswith("- ") and "source:" in line
    ]
    assert retrieved_lines
    assert "nanobot/channels/websocket.py" in retrieved_lines[0]
    assert "reason: path:nanobot/channels/websocket.py" in retrieved_lines[0]


def test_context_builder_retrieves_from_current_request_signals(tmp_path: Path):
    workspace = tmp_path
    memdir = workspace / "memory"
    memdir.mkdir(parents=True, exist_ok=True)
    comp = {
        "id": "doc-current-request",
        "summary_full": "Decision: fix retry handling in nanobot/api/server.py after timeout failures.",
        "meta": {"session_key": "websocket:api-fix"},
        "updated_at": "2026-05-03T12:00:00",
    }
    r = MemoryRetriever()
    r.index_compactions([comp], replace=True)
    idx_path = memdir / "retriever_index.json"
    r.persist_index(idx_path)
    memory_module.retriever.load_index(idx_path)
    cb = ContextBuilder(workspace)

    messages = cb.build_messages(
        history=[],
        current_message="Please inspect nanobot/api/server.py. The gateway request timed out.",
    )

    prompt = messages[0]["content"]
    assert "[Archived Context Summary]" not in prompt
    assert "[Retrieved Memories]" in prompt
    assert "retry handling in nanobot/api/server.py" in prompt
    assert "category: decision" in prompt
    assert "reason: path:nanobot/api/server.py" in prompt


def test_context_builder_retrieves_from_structured_decisions_and_paths(tmp_path: Path):
    workspace = tmp_path
    memdir = workspace / "memory"
    memdir.mkdir(parents=True, exist_ok=True)
    comp = {
        "id": "doc-websocket-recovery",
        "summary_sections": {
            "decisions": ["Decision: keep websocket recovery diagnostics structured."],
            "files_touched": ["nanobot/channels/websocket.py"],
            "next_steps": ["Add websocket recovery regression coverage."],
        },
        "meta": {"session_key": "websocket:recovery"},
        "updated_at": "2026-05-04T12:00:00",
    }
    r = MemoryRetriever()
    r.index_compactions([comp], replace=True)
    idx_path = memdir / "retriever_index.json"
    r.persist_index(idx_path)
    memory_module.retriever.load_index(idx_path)
    cb = ContextBuilder(workspace)

    prompt = cb.build_system_prompt(
        session_metadata={
            "_last_summary": {
                "sections": {
                    "decisions": ["User confirmed: continue with websocket recovery"],
                    "files_touched": ["nanobot/channels/websocket.py"],
                    "next_steps": ["Add websocket regression coverage"],
                }
            }
        }
    )

    assert "[Retrieved Memories]" in prompt
    assert "websocket recovery diagnostics structured" in prompt
    assert "reason: path:nanobot/channels/websocket.py" in prompt
