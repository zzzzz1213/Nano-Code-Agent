from pathlib import Path

from nanobot.agent.context import ContextBuilder
from nanobot.agent.memory import retriever


def test_context_memory_snapshot_reports_sources_without_raw_text(tmp_path: Path) -> None:
    retriever.index_compactions([], replace=True)
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "MEMORY.md").write_text(
        "Project fact: use the existing tool registry.",
        encoding="utf-8",
    )
    (tmp_path / "USER.md").write_text(
        "User prefers concise Chinese summaries.",
        encoding="utf-8",
    )
    builder = ContextBuilder(tmp_path)

    snapshot = builder.build_memory_snapshot(session_summary="Prior turn summary")

    assert snapshot["sources"]["memory"]["included"] is True
    assert snapshot["sources"]["user"]["included"] is True
    assert snapshot["sources"]["soul"]["included"] is False
    assert snapshot["sources"]["session_summary"]["included"] is True
    as_text = str(snapshot)
    assert "tool registry" not in as_text
    assert "concise Chinese" not in as_text
    assert "Prior turn summary" not in as_text


def test_context_memory_snapshot_reports_retrieved_metadata_without_snippets(
    tmp_path: Path,
) -> None:
    retriever.index_compactions(
        [
            {
                "id": "doc-decision",
                "summary_full": "Decision: keep the websocket retry adapter in api_gateway.py",
                "summary_sections": {
                    "decisions": ["Keep the websocket retry adapter in api_gateway.py"],
                },
                "updated_at": "2026-05-21T00:00:00",
                "meta": {"session_key": "websocket:thread-1"},
            }
        ],
        replace=True,
    )
    builder = ContextBuilder(tmp_path)

    snapshot = builder.build_memory_snapshot(
        session_summary="Need websocket retry adapter details for api_gateway.py",
    )

    retrieved = snapshot["retrieved"]
    assert retrieved["included"] is True
    assert retrieved["entry_count"] == 1
    assert retrieved["categories"] == {"decision": 1}
    assert retrieved["reasons"] == ["path:api_gateway.py"]
    assert retrieved["items"][0]["source"] == "websocket:thread-1"
    assert retrieved["items"][0]["category"] == "decision"
    assert retrieved["items"][0]["reason"] == "path:api_gateway.py"

    as_text = str(snapshot)
    assert "Keep the websocket retry adapter" not in as_text
    assert "summary_full" not in as_text
    assert "snippet" not in as_text
    retriever.index_compactions([], replace=True)
