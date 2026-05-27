from pathlib import Path

import pytest

from nanobot.agent.memory import Consolidator, MemoryStore, _build_compaction_event
from nanobot.providers.base import LLMResponse
from nanobot.session.manager import SessionManager


class _Provider:
    async def chat_with_retry(self, **kwargs):
        return LLMResponse(
            content="Archived older context while keeping open implementation decisions.",
            finish_reason="stop",
        )


@pytest.mark.asyncio
async def test_idle_compaction_persists_observable_metadata(tmp_path: Path) -> None:
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("websocket:ctx")
    for i in range(12):
        session.add_message("user", f"message {i}")
    sessions.save(session)
    consolidator = Consolidator(
        store=MemoryStore(tmp_path),
        provider=_Provider(),
        model="test-model",
        sessions=sessions,
        context_window_tokens=16_000,
        build_messages=lambda **kwargs: [],
        get_tool_definitions=lambda: [],
        max_completion_tokens=1024,
    )

    summary = await consolidator.compact_idle_session("websocket:ctx", max_suffix=3)

    compacted = sessions.get_or_create("websocket:ctx")
    event = compacted.metadata["_last_compaction"]
    assert summary == "Archived older context while keeping open implementation decisions."
    assert event["reason"] == "idle_ttl"
    assert event["source"] == "auto_compact"
    assert event["before_message_count"] == 12
    assert event["after_message_count"] == 3
    assert event["archived_message_count"] == 9
    assert event["saved_token_estimate"] > 0
    assert "implementation decisions" in event["summary_preview"]
    assert event["summary_sections"]["overview"] == [
        "Archived older context while keeping open implementation decisions"
    ]


def test_compaction_event_redacts_secrets_and_bounds_logs() -> None:
    long_log = "\n".join(
        [
            "Traceback (most recent call last):",
            "RuntimeError: build failed with token=sk-live-should-not-leak",
            *[f"noise line {i}" for i in range(80)],
            "Final error: npm run build failed",
        ]
    )
    summary = """
Overview: Fixed the failing build with api_key=abc123SECRET.
Commands run:
- npm run build --token super-secret-token
Failures:
- RuntimeError: build failed with password=hunter2 and many details that should be bounded
Next steps:
- Re-run pytest
"""

    event = _build_compaction_event(
        reason="token_budget",
        source="token_consolidator",
        before_messages=[
            {"role": "assistant", "content": long_log},
        ],
        after_messages=[],
        archived_messages=[
            {"role": "assistant", "content": long_log},
        ],
        summary=summary,
    )

    rendered = str(event["summary_preview"]) + str(event["summary_sections"])
    assert "abc123SECRET" not in rendered
    assert "super-secret-token" not in rendered
    assert "hunter2" not in rendered
    assert "sk-live-should-not-leak" not in rendered
    assert "[REDACTED]" in rendered
    assert "npm run build --token [REDACTED]" in event["summary_sections"]["commands_run"]
    failures = event["summary_sections"]["failures"]
    assert any("RuntimeError: build failed" in failure for failure in failures)
    assert all(len(failure) <= 320 for failure in failures)
