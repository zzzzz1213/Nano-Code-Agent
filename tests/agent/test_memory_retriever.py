from nanobot.agent.retriever import MemoryRetriever


def test_retriever_basic_ranking():
    retr = MemoryRetriever()

    compactions = [
        {
            "id": "c1",
            "summary_full": "Implemented feature X: updated parser and added tests.",
            "updated_at": 100000,
        },
        {
            "id": "c2",
            "summary_full": "Fixed bug in authentication flow; login now succeeds.",
            "updated_at": 200000,
        },
        {
            "id": "c3",
            "summary_full": "Refactor: moved parsing logic into helper module.",
            "updated_at": 150000,
        },
    ]

    retr.index_compactions(compactions)

    # query about parser/feature should return c1 or c3 at top
    res = retr.query("parser feature tests")
    assert len(res) >= 1
    assert res[0]["id"] in {"c1", "c3"}


def test_reindex_replaces_old():
    retr = MemoryRetriever()
    retr.index_compactions([
        {"id": "c1", "summary_full": "alpha beta gamma", "updated_at": 1}
    ])
    res1 = retr.query("alpha")
    assert res1 and res1[0]["id"] == "c1"

    # reindex with different content
    retr.index_compactions([
        {"id": "c2", "summary_full": "delta epsilon", "updated_at": 2}
    ])
    res2 = retr.query("alpha")
    # alpha should no longer be found
    assert res2 == []


def test_retriever_adds_category_and_match_reason():
    retr = MemoryRetriever()
    retr.index_compactions([
        {
            "id": "decision-doc",
            "summary_sections": {
                "decisions": ["Decision: keep AgentLoop changes small."],
                "files_touched": ["nanobot/agent/loop.py"],
            },
            "updated_at": 10,
            "meta": {"session_key": "websocket:ctx"},
        },
        {
            "id": "failure-doc",
            "summary_sections": {
                "failures": ["pytest failed in tests/agent/test_loop_save_turn.py"],
            },
            "updated_at": 20,
        },
    ])

    decision = retr.query("AgentLoop loop.py", top_k=1)[0]
    assert decision["id"] == "decision-doc"
    assert decision["category"] == "decision"
    assert decision["meta"]["category"] == "decision"
    assert decision["match_reason"].startswith(("term:", "section:", "path:"))
    assert decision["meta"]["match_reason"] == decision["match_reason"]

    failure = retr.query("pytest failed", top_k=1)[0]
    assert failure["id"] == "failure-doc"
    assert failure["category"] == "failure"
    assert failure["match_reason"] == "section:failures"


def test_retriever_preserves_path_tokens_and_decision_priority():
    retr = MemoryRetriever()
    retr.index_compactions([
        {
            "id": "path-decision",
            "summary_full": "Decision: fix retry handling in nanobot/api/server.py after timeout failures.",
            "updated_at": 30,
        }
    ])

    result = retr.query("nanobot/api/server.py timed out", top_k=1)[0]

    assert result["id"] == "path-decision"
    assert result["category"] == "decision"
    assert result["match_reason"] == "path:nanobot/api/server.py"


def test_retriever_prioritizes_exact_path_matches_over_generic_keyword_hits():
    retr = MemoryRetriever()
    retr.index_compactions([
        {
            "id": "generic-timeout",
            "summary_full": "Failure: gateway timeout while syncing metadata.",
            "updated_at": 50,
        },
        {
            "id": "path-timeout",
            "summary_sections": {
                "failures": ["Timeout in nanobot/channels/websocket.py while handling reconnects."],
                "files_touched": ["nanobot/channels/websocket.py"],
            },
            "updated_at": 60,
        },
    ])

    results = retr.query("nanobot/channels/websocket.py timeout", top_k=2)

    assert [item["id"] for item in results[:2]] == ["path-timeout", "generic-timeout"]
    assert results[0]["match_reason"] == "path:nanobot/channels/websocket.py"


def test_retriever_prioritizes_failure_section_when_query_has_failure_signals():
    retr = MemoryRetriever()
    retr.index_compactions([
        {
            "id": "decision-doc",
            "summary_sections": {
                "decisions": ["Decision: keep retry handling in agent runner."],
            },
            "updated_at": 20,
        },
        {
            "id": "failure-doc",
            "summary_sections": {
                "failures": ["pytest failed with timeout in tests/agent/test_runner.py"],
                "commands_run": ["pytest tests/agent/test_runner.py -q"],
            },
            "updated_at": 10,
        },
    ])

    result = retr.query("pytest timeout failed", top_k=1)[0]

    assert result["id"] == "failure-doc"
    assert result["match_reason"] == "section:failures"


def test_retriever_prefers_more_recent_doc_when_relevance_is_tied():
    retr = MemoryRetriever()
    retr.index_compactions([
        {
            "id": "older",
            "summary_full": "Decision: refactor websocket checkpoint recovery.",
            "updated_at": 100,
        },
        {
            "id": "newer",
            "summary_full": "Decision: refactor websocket checkpoint recovery.",
            "updated_at": 200,
        },
    ])

    results = retr.query("refactor websocket checkpoint recovery", top_k=2)

    assert [item["id"] for item in results[:2]] == ["newer", "older"]
