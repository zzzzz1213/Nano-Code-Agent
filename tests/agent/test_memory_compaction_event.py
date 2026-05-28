from pathlib import Path

import pytest

from nanobot.agent.memory import (
    Consolidator,
    MemoryStore,
    _build_compaction_event,
    _parse_summary_sections,
)
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


def test_parse_summary_sections_keeps_recent_items() -> None:
    summary = "\n".join(
        [
            "Commands run:",
            *[f"- pytest tests/agent/test_case_{i}.py -q" for i in range(10)],
        ]
    )

    sections = _parse_summary_sections(summary)

    assert len(sections["commands_run"]) == 8
    assert sections["commands_run"][0] == "pytest tests/agent/test_case_2.py -q"
    assert sections["commands_run"][-1] == "pytest tests/agent/test_case_9.py -q"


def test_compaction_event_prioritizes_recent_signals_and_mixed_failures() -> None:
    archived_messages = [
        {"role": "assistant", "content": "Decision: keep the old path around for now."},
        {"role": "user", "content": "确认，按这个方案继续"},
        {
            "role": "assistant",
            "content": "\n".join(
                [
                    "Edited nanobot/agent/loop.py and webui/src/lib/types.ts",
                    "pytest tests/agent/test_memory_compaction_event.py -q",
                    "Traceback (most recent call last):",
                    "RuntimeError: request timed out while rebuilding checkpoint",
                    "最终错误: 构建失败，需要修复超时逻辑",
                    "Next steps: add compaction regression coverage",
                ]
            ),
        },
    ]

    event = _build_compaction_event(
        reason="token_budget",
        source="token_consolidator",
        before_messages=archived_messages,
        after_messages=[],
        archived_messages=archived_messages,
        summary="Overview: previous summary text.",
    )

    decisions = event["summary_sections"]["decisions"]
    failures = event["summary_sections"]["failures"]
    files_touched = event["summary_sections"]["files_touched"]

    assert "User confirmed: 确认，按这个方案继续" in decisions
    assert "nanobot/agent/loop.py" in files_touched
    assert "webui/src/lib/types.ts" in files_touched
    assert any("Command: pytest tests/agent/test_memory_compaction_event.py -q" in item for item in failures)
    assert any("构建失败" in item for item in failures)
    assert event["summary_preview"].startswith("## Goal") or "## Decisions" in event["summary_preview"]
