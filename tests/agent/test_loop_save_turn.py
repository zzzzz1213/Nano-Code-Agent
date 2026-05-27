import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMResponse
from nanobot.session.goal_state import GOAL_STATE_KEY
from nanobot.session.manager import Session, SessionManager
from nanobot.session.webui_turns import (
    TITLE_GENERATION_MAX_TOKENS,
    TITLE_GENERATION_REASONING_EFFORT,
    WEBUI_SESSION_METADATA_KEY,
    WEBUI_TITLE_METADATA_KEY,
    WebuiTurnCoordinator,
    build_turn_checkpoint,
    maybe_generate_webui_title,
)
from nanobot.utils.llm_runtime import LLMRuntime


def _mk_loop() -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    from nanobot.config.schema import AgentDefaults

    loop.max_tool_result_chars = AgentDefaults().max_tool_result_chars
    return loop


def _make_full_loop(tmp_path: Path) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="Test title"))
    return AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")


def test_agent_loop_llm_runtime_reflects_current_provider_and_model(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    runtime = loop.llm_runtime()

    assert runtime.provider is loop.provider
    assert runtime.model == "test-model"

    next_provider = MagicMock()
    loop.provider = next_provider
    loop.model = "next-model"
    runtime = loop.llm_runtime()

    assert runtime.provider is next_provider
    assert runtime.model == "next-model"


@pytest.mark.asyncio
async def test_generate_webui_title_only_for_marked_webui_sessions(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    loop.provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content='"优化 WebUI 侧边栏。"', finish_reason="stop")
    )
    session = loop.sessions.get_or_create("websocket:chat-title")
    session.metadata[WEBUI_SESSION_METADATA_KEY] = True
    session.add_message("user", "帮我优化一下 webui 的 sidebar")
    session.add_message("assistant", "可以，我会先调整布局和视觉层级。")
    loop.sessions.save(session)

    generated = await maybe_generate_webui_title(
        sessions=loop.sessions,
        session_key="websocket:chat-title",
        provider=loop.provider,
        model=loop.model,
    )

    assert generated is True
    assert session.metadata[WEBUI_TITLE_METADATA_KEY] == "优化 WebUI 侧边栏"
    loop.provider.chat_with_retry.assert_awaited_once()
    assert loop.provider.chat_with_retry.await_args.kwargs["max_tokens"] == TITLE_GENERATION_MAX_TOKENS
    assert (
        loop.provider.chat_with_retry.await_args.kwargs["reasoning_effort"]
        == TITLE_GENERATION_REASONING_EFFORT
    )


@pytest.mark.asyncio
async def test_generate_webui_title_skips_plain_websocket_sessions(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    loop.provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="Plain websocket title", finish_reason="stop")
    )
    session = loop.sessions.get_or_create("websocket:custom-client")
    session.add_message("user", "hello from a custom websocket client")
    loop.sessions.save(session)

    generated = await maybe_generate_webui_title(
        sessions=loop.sessions,
        session_key="websocket:custom-client",
        provider=loop.provider,
        model=loop.model,
    )

    assert generated is False
    assert WEBUI_TITLE_METADATA_KEY not in session.metadata
    loop.provider.chat_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_generate_webui_title_ignores_command_only_sessions(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    session = loop.sessions.get_or_create("websocket:command-title")
    session.metadata[WEBUI_SESSION_METADATA_KEY] = True
    session.add_message("user", "/model deep", _command=True)
    session.add_message(
        "assistant",
        "Switched model preset to `deep`.\n- Model: `deepseek-v4-pro`",
        _command=True,
    )
    loop.sessions.save(session)

    generated = await maybe_generate_webui_title(
        sessions=loop.sessions,
        session_key="websocket:command-title",
        provider=loop.provider,
        model=loop.model,
    )

    assert generated is False
    assert WEBUI_TITLE_METADATA_KEY not in session.metadata
    loop.provider.chat_with_retry.assert_not_awaited()


def test_webui_title_update_uses_captured_llm_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = MessageBus()
    sessions = SessionManager(tmp_path)
    scheduled: list[object] = []
    captured: dict[str, object] = {}

    async def fake_title_after_turn(**kwargs: object) -> bool:
        captured.update(kwargs)
        return False

    monkeypatch.setattr(
        "nanobot.session.webui_turns.maybe_generate_webui_title_after_turn",
        fake_title_after_turn,
    )
    coordinator = WebuiTurnCoordinator(
        bus=bus,
        sessions=sessions,
        schedule_background=lambda coro: scheduled.append(coro),
    )
    provider = MagicMock()
    msg = InboundMessage(
        channel="websocket",
        sender_id="u1",
        chat_id="chat1",
        content="say hello",
        metadata={"webui": True},
    )

    coordinator.capture_title_context(
        "websocket:chat1",
        msg,
        LLMRuntime(provider, "turn-model"),
    )
    asyncio.run(coordinator.handle_turn_end(
        msg,
        session_key="websocket:chat1",
        latency_ms=None,
    ))

    assert len(scheduled) == 1
    asyncio.run(scheduled[0])  # type: ignore[arg-type]

    assert captured["provider"] is provider
    assert captured["model"] == "turn-model"


def test_save_turn_skips_multimodal_user_when_only_runtime_context() -> None:
    loop = _mk_loop()
    session = Session(key="test:runtime-only")
    runtime = ContextBuilder._RUNTIME_CONTEXT_TAG + "\nCurrent Time: now (UTC)"

    loop._save_turn(
        session,
        [{"role": "user", "content": [{"type": "text", "text": runtime}]}],
        skip=0,
    )
    assert session.messages == []


def test_save_turn_keeps_image_placeholder_with_path_after_runtime_strip() -> None:
    loop = _mk_loop()
    session = Session(key="test:image")
    runtime = ContextBuilder._RUNTIME_CONTEXT_TAG + "\nCurrent Time: now (UTC)"

    loop._save_turn(
        session,
        [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}, "_meta": {"path": "/media/feishu/photo.jpg"}},
                {"type": "text", "text": runtime},
            ],
        }],
        skip=0,
    )
    assert session.messages[0]["content"] == [{"type": "text", "text": "[image: /media/feishu/photo.jpg]"}]


def test_save_turn_keeps_image_placeholder_without_meta() -> None:
    loop = _mk_loop()
    session = Session(key="test:image-no-meta")
    runtime = ContextBuilder._RUNTIME_CONTEXT_TAG + "\nCurrent Time: now (UTC)"

    loop._save_turn(
        session,
        [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                {"type": "text", "text": runtime},
            ],
        }],
        skip=0,
    )
    assert session.messages[0]["content"] == [{"type": "text", "text": "[image]"}]


def test_save_turn_strips_runtime_context_suffix_from_string() -> None:
    loop = _mk_loop()
    session = Session(key="test:suffix-strip")
    runtime = (
        ContextBuilder._RUNTIME_CONTEXT_TAG
        + "\nCurrent Time: now\n"
        + ContextBuilder._RUNTIME_CONTEXT_END
    )

    loop._save_turn(
        session,
        [{"role": "user", "content": f"hello world\n\n{runtime}"}],
        skip=0,
    )
    assert session.messages[0]["content"] == "hello world"


def test_save_turn_skips_string_user_when_only_runtime_context_suffix() -> None:
    loop = _mk_loop()
    session = Session(key="test:suffix-only")
    runtime = (
        ContextBuilder._RUNTIME_CONTEXT_TAG
        + "\nCurrent Time: now\n"
        + ContextBuilder._RUNTIME_CONTEXT_END
    )

    loop._save_turn(
        session,
        [{"role": "user", "content": runtime}],
        skip=0,
    )
    assert session.messages == []


def test_save_turn_keeps_tool_results_under_16k() -> None:
    loop = _mk_loop()
    session = Session(key="test:tool-result")
    content = "x" * 12_000

    loop._save_turn(
        session,
        [{"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": content}],
        skip=0,
    )

    assert session.messages[0]["content"] == content


def test_save_turn_stamps_latency_on_last_assistant() -> None:
    loop = _mk_loop()
    session = Session(key="test:latency")

    loop._save_turn(
        session,
        [
            {"role": "assistant", "content": "hello", "tool_calls": [{"id": "c1"}]},
            {"role": "assistant", "content": "final answer"},
        ],
        skip=0,
        turn_latency_ms=12345,
    )

    assert session.messages[-1]["role"] == "assistant"
    assert session.messages[-1]["content"] == "final answer"
    assert session.messages[-1]["latency_ms"] == 12345


def test_restore_runtime_checkpoint_rehydrates_completed_and_pending_tools() -> None:
    loop = _mk_loop()
    session = Session(
        key="test:checkpoint",
        metadata={
            AgentLoop._RUNTIME_CHECKPOINT_KEY: {
                "assistant_message": {
                    "role": "assistant",
                    "content": "working",
                    "tool_calls": [
                        {
                            "id": "call_done",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"},
                        },
                        {
                            "id": "call_pending",
                            "type": "function",
                            "function": {"name": "exec", "arguments": "{}"},
                        },
                    ],
                },
                "completed_tool_results": [
                    {
                        "role": "tool",
                        "tool_call_id": "call_done",
                        "name": "read_file",
                        "content": "ok",
                    }
                ],
                "pending_tool_calls": [
                    {
                        "id": "call_pending",
                        "type": "function",
                        "function": {"name": "exec", "arguments": "{}"},
                    }
                ],
            }
        },
    )

    restored = loop._restore_runtime_checkpoint(session)

    assert restored is True
    assert session.metadata.get(AgentLoop._RUNTIME_CHECKPOINT_KEY) is not None
    assert session.metadata.get(AgentLoop._RUNTIME_CHECKPOINT_MATERIALIZED_KEY) is True
    assert session.messages[0]["role"] == "assistant"
    assert session.messages[1]["tool_call_id"] == "call_done"
    assert session.messages[2]["tool_call_id"] == "call_pending"
    assert "interrupted before this tool finished" in session.messages[2]["content"].lower()
    recovered = loop._consume_restored_runtime_checkpoint(session)
    assert recovered is not None
    assert recovered["pending_tool_calls"][0]["id"] == "call_pending"
    assert recovered["reused_tool_call_ids"] == ["call_done"]
    assert recovered["reused_tool_count"] == 1
    assert recovered["compensation_tool_call_ids"] == ["call_pending"]
    assert recovered["compensation_tool_count"] == 1
    assert loop._consume_restored_runtime_checkpoint(session) is None


def test_restore_runtime_checkpoint_skips_duplicate_tool_results_by_id() -> None:
    loop = _mk_loop()
    session = Session(
        key="test:checkpoint-duplicate",
        messages=[
            {
                "role": "assistant",
                "content": "working",
                "tool_calls": [
                    {
                        "id": "call_done",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_done",
                "name": "read_file",
                "content": "already restored",
            },
        ],
        metadata={
            AgentLoop._RUNTIME_CHECKPOINT_KEY: {
                "assistant_message": {
                    "role": "assistant",
                    "content": "working",
                    "tool_calls": [
                        {
                            "id": "call_done",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"},
                        },
                        {
                            "id": "call_pending",
                            "type": "function",
                            "function": {"name": "exec", "arguments": "{}"},
                        },
                    ],
                },
                "completed_tool_results": [
                    {
                        "role": "tool",
                        "tool_call_id": "call_done",
                        "name": "read_file",
                        "content": "ok",
                    }
                ],
                "pending_tool_calls": [
                    {
                        "id": "call_done",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    },
                    {
                        "id": "call_pending",
                        "type": "function",
                        "function": {"name": "exec", "arguments": "{}"},
                    },
                ],
            }
        },
    )

    restored = loop._restore_runtime_checkpoint(session)

    assert restored is True
    done_results = [m for m in session.messages if m.get("tool_call_id") == "call_done"]
    assert len(done_results) == 1
    pending_results = [m for m in session.messages if m.get("tool_call_id") == "call_pending"]
    assert len(pending_results) == 1
    assert "interrupted before this tool finished" in pending_results[0]["content"].lower()
    recovered = loop._consume_restored_runtime_checkpoint(session)
    assert recovered is not None
    assert recovered["reused_tool_call_ids"] == ["call_done"]
    assert recovered["compensation_tool_call_ids"] == ["call_pending"]
    assert recovered["skipped_duplicate_tool_call_ids"].count("call_done") >= 1


def test_restore_runtime_checkpoint_records_pending_recovery_strategy() -> None:
    loop = _mk_loop()
    session = Session(
        key="test:checkpoint-recovery",
        metadata={
            AgentLoop._RUNTIME_CHECKPOINT_KEY: {
                "assistant_message": {
                    "role": "assistant",
                    "content": "working",
                    "tool_calls": [
                        {
                            "id": "call_retry",
                            "type": "function",
                            "function": {"name": "exec", "arguments": "{}"},
                        },
                        {
                            "id": "call_user",
                            "type": "function",
                            "function": {"name": "exec", "arguments": "{}"},
                        },
                    ],
                },
                "completed_tool_results": [],
                "pending_tool_calls": [
                    {
                        "id": "call_retry",
                        "type": "function",
                        "function": {"name": "exec", "arguments": "{}"},
                        "failure_category": "tool_exception",
                        "recovery_action": "retry",
                        "retryable": True,
                    },
                    {
                        "id": "call_user",
                        "type": "function",
                        "function": {"name": "exec", "arguments": "{}"},
                        "failure_category": "safety_block",
                        "recovery_action": "revise_request",
                        "needs_user_input": True,
                    },
                ],
            }
        },
    )

    restored = loop._restore_runtime_checkpoint(session)

    assert restored is True
    retry_result = [m for m in session.messages if m.get("tool_call_id") == "call_retry"][0]
    user_result = [m for m in session.messages if m.get("tool_call_id") == "call_user"][0]
    assert "retryable" in retry_result["content"].lower()
    assert "requires user input" in user_result["content"].lower()
    recovered = loop._consume_restored_runtime_checkpoint(session)
    assert recovered is not None
    assert recovered["compensation_tool_call_ids"] == ["call_retry", "call_user"]
    assert recovered["retryable_tool_call_ids"] == ["call_retry"]
    assert recovered["retryable_tool_count"] == 1
    assert recovered["requires_user_tool_call_ids"] == ["call_user"]
    assert recovered["requires_user_tool_count"] == 1


def test_restore_runtime_checkpoint_marks_safe_read_only_tools_resumable() -> None:
    loop = _mk_loop()
    loop.tools = MagicMock()
    loop.tools.get_metadata.side_effect = lambda name: {
        "read_file": {
            "read_only": True,
            "concurrency_safe": True,
            "exclusive": False,
            "config_key": "",
            "scopes": ("core",),
        },
        "exec": {
            "read_only": False,
            "concurrency_safe": False,
            "exclusive": True,
            "config_key": "exec",
            "scopes": ("core",),
        },
    }.get(name)
    session = Session(
        key="test:checkpoint-resumable",
        metadata={
            AgentLoop._RUNTIME_CHECKPOINT_KEY: {
                "assistant_message": {
                    "role": "assistant",
                    "content": "working",
                    "tool_calls": [
                        {
                            "id": "call_read",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{\"path\":\"README.md\"}"},
                        },
                        {
                            "id": "call_shell",
                            "type": "function",
                            "function": {"name": "exec", "arguments": "{\"command\":\"npm test\"}"},
                        },
                    ],
                },
                "completed_tool_results": [],
                "pending_tool_calls": [
                    {
                        "id": "call_read",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{\"path\":\"README.md\"}"},
                    },
                    {
                        "id": "call_shell",
                        "type": "function",
                        "function": {"name": "exec", "arguments": "{\"command\":\"npm test\"}"},
                    },
                ],
            }
        },
    )

    restored = loop._restore_runtime_checkpoint(session)

    assert restored is True
    read_result = [m for m in session.messages if m.get("tool_call_id") == "call_read"][0]
    shell_result = [m for m in session.messages if m.get("tool_call_id") == "call_shell"][0]
    assert "safe resume candidate" in read_result["content"].lower()
    assert "safe resume candidate" not in shell_result["content"].lower()
    recovered = loop._consume_restored_runtime_checkpoint(session)
    assert recovered is not None
    assert recovered["compensation_tool_call_ids"] == ["call_read", "call_shell"]
    assert recovered["resumable_tool_call_ids"] == ["call_read"]
    assert recovered["resumable_tool_count"] == 1
    assert recovered["safe_resume_tool_call_ids"] == ["call_read"]
    assert recovered["safe_resume_tool_count"] == 1
    assert recovered["review_required_tool_call_ids"] == ["call_shell"]
    assert recovered["review_required_tool_count"] == 1
    assert recovered["recovery_review_items"] == [
        {
            "tool_call_id": "call_read",
            "name": "read_file",
            "group": "safe_resume",
            "reason": "read_only_safe_candidate",
            "recovery_action": "resume_safe",
        },
        {
            "tool_call_id": "call_shell",
            "name": "exec",
            "group": "review_required",
            "reason": "shell_command_requires_review",
            "recovery_action": "review_before_retry",
        },
    ]
    assert recovered["retryable_tool_call_ids"] == []
    assert recovered["requires_user_tool_call_ids"] == []


def test_restore_runtime_checkpoint_groups_review_only_tools() -> None:
    loop = _mk_loop()
    loop.tools = MagicMock()
    loop.tools.get_metadata.side_effect = lambda name: {
        "mcp_read_issue": {
            "read_only": True,
            "concurrency_safe": True,
            "exclusive": False,
            "config_key": "mcp",
            "scopes": ("mcp",),
        },
        "mcp_write_issue": {
            "read_only": False,
            "concurrency_safe": False,
            "exclusive": True,
            "config_key": "mcp",
            "scopes": ("mcp",),
        },
    }.get(name)
    pending = [
        {
            "id": "call_write",
            "type": "function",
            "function": {"name": "write_file", "arguments": "{\"path\":\"a.py\"}"},
        },
        {
            "id": "call_mcp_write",
            "type": "function",
            "function": {"name": "mcp_write_issue", "arguments": "{}"},
        },
        {
            "id": "call_mcp_read",
            "type": "function",
            "function": {"name": "mcp_read_issue", "arguments": "{}"},
        },
        {
            "id": "call_needs_input",
            "type": "function",
            "function": {"name": "exec", "arguments": "{}"},
            "recovery_action": "ask_user",
            "needs_user_input": True,
        },
        {
            "id": "call_blocked",
            "type": "function",
            "function": {"name": "exec", "arguments": "{}"},
            "failure_category": "safety_block",
        },
    ]
    session = Session(
        key="test:checkpoint-review-groups",
        metadata={
            AgentLoop._RUNTIME_CHECKPOINT_KEY: {
                "assistant_message": {
                    "role": "assistant",
                    "content": "working",
                    "tool_calls": pending,
                },
                "completed_tool_results": [],
                "pending_tool_calls": pending,
            }
        },
    )

    restored = loop._restore_runtime_checkpoint(session)

    assert restored is True
    recovered = loop._consume_restored_runtime_checkpoint(session)
    assert recovered is not None
    assert recovered["safe_resume_tool_call_ids"] == ["call_mcp_read"]
    assert recovered["review_required_tool_call_ids"] == ["call_write", "call_mcp_write"]
    assert recovered["needs_input_tool_call_ids"] == ["call_needs_input"]
    assert recovered["blocked_tool_call_ids"] == ["call_blocked"]
    reasons = {
        item["tool_call_id"]: item["reason"]
        for item in recovered["recovery_review_items"]
    }
    assert reasons["call_write"] == "write_tool_requires_review"
    assert reasons["call_mcp_write"] == "mcp_mutating_tool_requires_review"
    assert reasons["call_needs_input"] == "requires_user_input"
    assert reasons["call_blocked"] == "safety_block"


@pytest.mark.asyncio
async def test_resume_safe_runtime_checkpoint_executes_read_only_candidates(tmp_path: Path) -> None:
    loop = _mk_loop()
    loop.tools = MagicMock()
    loop.tools.get_metadata.side_effect = lambda name: {
        "read_file": {
            "read_only": True,
            "concurrency_safe": True,
            "exclusive": False,
            "config_key": "",
            "scopes": ("core",),
        },
        "exec": {
            "read_only": False,
            "concurrency_safe": False,
            "exclusive": True,
            "config_key": "exec",
            "scopes": ("core",),
        },
    }.get(name)
    loop.tools.execute = AsyncMock(return_value="README content")
    loop.sessions = SessionManager(tmp_path)
    session = Session(
        key="test:checkpoint-resume-safe",
        metadata={
            AgentLoop._RUNTIME_CHECKPOINT_KEY: {
                "assistant_message": {
                    "role": "assistant",
                    "content": "working",
                    "tool_calls": [
                        {
                            "id": "call_read",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{\"path\":\"README.md\"}"},
                        },
                        {
                            "id": "call_shell",
                            "type": "function",
                            "function": {"name": "exec", "arguments": "{\"command\":\"npm test\"}"},
                        },
                    ],
                },
                "completed_tool_results": [],
                "pending_tool_calls": [
                    {
                        "id": "call_read",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{\"path\":\"README.md\"}"},
                    },
                    {
                        "id": "call_shell",
                        "type": "function",
                        "function": {"name": "exec", "arguments": "{\"command\":\"npm test\"}"},
                    },
                ],
            }
        },
    )

    checkpoint = await loop._resume_safe_runtime_checkpoint(session)

    assert checkpoint is not None
    loop.tools.execute.assert_awaited_once_with("read_file", {"path": "README.md"})
    read_result = [m for m in session.messages if m.get("tool_call_id") == "call_read"][0]
    shell_result = [m for m in session.messages if m.get("tool_call_id") == "call_shell"][0]
    assert read_result["content"] == "README content"
    assert "interrupted before this tool finished" in shell_result["content"].lower()
    assert checkpoint["recovered_executed_tool_call_ids"] == ["call_read"]
    assert checkpoint["recovered_skipped_tool_call_ids"] == ["call_shell"]
    assert checkpoint["recovered_requires_user_tool_call_ids"] == []
    assert checkpoint["pending_tool_call_ids"] == ["call_shell"]
    assert checkpoint["resumable_tool_count"] == 0
    assert checkpoint["safe_resume_tool_count"] == 0
    assert checkpoint["review_required_tool_call_ids"] == ["call_shell"]
    assert checkpoint["review_required_tool_count"] == 1
    assert AgentLoop._RUNTIME_CHECKPOINT_KEY not in session.metadata


def test_build_turn_checkpoint_exposes_recovery_tool_ids() -> None:
    checkpoint = build_turn_checkpoint(
        {
            "checkpoint_id": "chk_abc",
            "phase": "tools_completed",
            "updated_at": "2026-05-22T00:00:00+00:00",
            "recoverable": True,
            "completed_tool_results": [
                {
                    "role": "tool",
                    "tool_call_id": "call_read",
                    "name": "read_file",
                    "content": "ok",
                }
            ],
            "completed_tool_call_ids": ["call_read"],
            "executed_tool_call_ids": ["call_read"],
            "pending_tool_calls": [
                {
                    "id": "call_shell",
                    "type": "function",
                    "function": {"name": "exec", "arguments": "{\"command\":\"npm test\"}"},
                }
            ],
            "pending_tool_call_ids": ["call_shell"],
            "reused_tool_call_ids": ["call_read"],
            "reused_tool_count": 1,
            "compensation_tool_call_ids": ["call_shell"],
            "compensation_tool_count": 1,
            "retryable_tool_call_ids": ["call_shell"],
            "retryable_tool_count": 1,
            "requires_user_tool_call_ids": ["call_approval"],
            "requires_user_tool_count": 1,
            "resumable_tool_call_ids": ["call_read_retry"],
            "resumable_tool_count": 1,
            "recovered_executed_tool_call_ids": ["call_read_resume"],
            "recovered_executed_tool_count": 1,
            "recovered_skipped_tool_call_ids": ["call_shell_skip"],
            "recovered_skipped_tool_count": 1,
            "recovered_requires_user_tool_call_ids": ["call_approval_resume"],
            "recovered_requires_user_tool_count": 1,
            "safe_resume_tool_call_ids": ["call_read_retry"],
            "safe_resume_tool_count": 1,
            "review_required_tool_call_ids": ["call_shell"],
            "review_required_tool_count": 1,
            "needs_input_tool_call_ids": ["call_approval"],
            "needs_input_tool_count": 1,
            "blocked_tool_call_ids": ["call_blocked"],
            "blocked_tool_count": 1,
            "recovery_review_items": [
                {
                    "tool_call_id": "call_shell",
                    "name": "exec",
                    "group": "review_required",
                    "reason": "shell_command_requires_review",
                    "recovery_action": "review_before_retry",
                }
            ],
            "recovery_review_count": 1,
            "skipped_duplicate_tool_call_ids": ["call_old"],
            "skipped_duplicate_tool_count": 1,
        },
        turn_id="turn-1",
    )

    assert checkpoint["checkpoint_id"] == "chk_abc"
    assert checkpoint["pending_tool_call_ids"] == ["call_shell"]
    assert checkpoint["completed_tool_call_ids"] == ["call_read"]
    assert checkpoint["executed_tool_call_ids"] == ["call_read"]
    assert checkpoint["pending_tool_count"] == 1
    assert checkpoint["completed_tool_count"] == 1
    assert checkpoint["reused_tool_call_ids"] == ["call_read"]
    assert checkpoint["reused_tool_count"] == 1
    assert checkpoint["compensation_tool_call_ids"] == ["call_shell"]
    assert checkpoint["compensation_tool_count"] == 1
    assert checkpoint["retryable_tool_call_ids"] == ["call_shell"]
    assert checkpoint["retryable_tool_count"] == 1
    assert checkpoint["requires_user_tool_call_ids"] == ["call_approval"]
    assert checkpoint["requires_user_tool_count"] == 1
    assert checkpoint["resumable_tool_call_ids"] == ["call_read_retry"]
    assert checkpoint["resumable_tool_count"] == 1
    assert checkpoint["recovered_executed_tool_call_ids"] == ["call_read_resume"]
    assert checkpoint["recovered_executed_tool_count"] == 1
    assert checkpoint["recovered_skipped_tool_call_ids"] == ["call_shell_skip"]
    assert checkpoint["recovered_skipped_tool_count"] == 1
    assert checkpoint["recovered_requires_user_tool_call_ids"] == ["call_approval_resume"]
    assert checkpoint["recovered_requires_user_tool_count"] == 1
    assert checkpoint["safe_resume_tool_call_ids"] == ["call_read_retry"]
    assert checkpoint["safe_resume_tool_count"] == 1
    assert checkpoint["review_required_tool_call_ids"] == ["call_shell"]
    assert checkpoint["review_required_tool_count"] == 1
    assert checkpoint["needs_input_tool_call_ids"] == ["call_approval"]
    assert checkpoint["needs_input_tool_count"] == 1
    assert checkpoint["blocked_tool_call_ids"] == ["call_blocked"]
    assert checkpoint["blocked_tool_count"] == 1
    assert checkpoint["recovery_review_items"] == [
        {
            "tool_call_id": "call_shell",
            "name": "exec",
            "group": "review_required",
            "reason": "shell_command_requires_review",
            "recovery_action": "review_before_retry",
        }
    ]
    assert checkpoint["recovery_review_count"] == 1
    assert checkpoint["skipped_duplicate_tool_call_ids"] == ["call_old"]
    assert checkpoint["skipped_duplicate_tool_count"] == 1
    assert checkpoint["recoverable"] is True
    assert checkpoint["updated_at"] == "2026-05-22T00:00:00+00:00"


def test_restore_runtime_checkpoint_dedupes_overlapping_tail() -> None:
    loop = _mk_loop()
    session = Session(
        key="test:checkpoint-overlap",
        messages=[
            {
                "role": "assistant",
                "content": "working",
                "tool_calls": [
                    {
                        "id": "call_done",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    },
                    {
                        "id": "call_pending",
                        "type": "function",
                        "function": {"name": "exec", "arguments": "{}"},
                    },
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_done",
                "name": "read_file",
                "content": "ok",
            },
        ],
        metadata={
            AgentLoop._RUNTIME_CHECKPOINT_KEY: {
                "assistant_message": {
                    "role": "assistant",
                    "content": "working",
                    "tool_calls": [
                        {
                            "id": "call_done",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"},
                        },
                        {
                            "id": "call_pending",
                            "type": "function",
                            "function": {"name": "exec", "arguments": "{}"},
                        },
                    ],
                },
                "completed_tool_results": [
                    {
                        "role": "tool",
                        "tool_call_id": "call_done",
                        "name": "read_file",
                        "content": "ok",
                    }
                ],
                "pending_tool_calls": [
                    {
                        "id": "call_pending",
                        "type": "function",
                        "function": {"name": "exec", "arguments": "{}"},
                    }
                ],
            }
        },
    )

    restored = loop._restore_runtime_checkpoint(session)

    assert restored is True
    assert session.metadata.get(AgentLoop._RUNTIME_CHECKPOINT_KEY) is not None
    assert len(session.messages) == 3
    assert session.messages[0]["role"] == "assistant"
    assert session.messages[1]["tool_call_id"] == "call_done"
    assert session.messages[2]["tool_call_id"] == "call_pending"


@pytest.mark.asyncio
async def test_process_message_persists_user_message_before_turn_completes(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]
    loop._run_agent_loop = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]

    msg = InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="persist me")
    with pytest.raises(RuntimeError, match="boom"):
        await loop._process_message(msg)

    loop.sessions.invalidate("feishu:c1")
    persisted = loop.sessions.get_or_create("feishu:c1")
    assert [m["role"] for m in persisted.messages] == ["user"]
    assert persisted.messages[0]["content"] == "persist me"
    assert persisted.metadata.get(AgentLoop._PENDING_USER_TURN_KEY) is True
    assert persisted.updated_at >= persisted.created_at


# 1x1 PNG used by the media-persistence tests. ``extract_documents`` runs
# at the top of ``_process_message`` and filters ``msg.media`` down to
# paths that magic-byte-sniff as images, so the test fixture needs real
# bytes on disk (not just placeholder paths).
_PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc\x00\x00\x00\x02\x00\x01"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.mark.asyncio
async def test_process_message_persists_media_paths_on_user_turn(tmp_path: Path) -> None:
    """User turns that attach images must record the media paths alongside
    the text so the webui can rehydrate previews on session replay.

    This is the producer half of the signed-media-URL round-trip: paths are
    stored here, then :meth:`WebSocketChannel._augment_media_urls` maps them
    onto signed URLs on the way out.
    """
    img_a = tmp_path / "uuid-1.png"
    img_a.write_bytes(_PNG_1X1)
    img_b = tmp_path / "uuid-2.png"
    img_b.write_bytes(_PNG_1X1)

    loop = _make_full_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]
    loop._run_agent_loop = AsyncMock(side_effect=RuntimeError("interrupt"))  # type: ignore[method-assign]

    msg = InboundMessage(
        channel="websocket",
        sender_id="u1",
        chat_id="c-media",
        content="look",
        media=[str(img_a), str(img_b)],
    )
    with pytest.raises(RuntimeError, match="interrupt"):
        await loop._process_message(msg)

    loop.sessions.invalidate("websocket:c-media")
    persisted = loop.sessions.get_or_create("websocket:c-media")
    assert [m["role"] for m in persisted.messages] == ["user"]
    assert persisted.messages[0]["content"] == "look"
    assert persisted.messages[0]["media"] == [str(img_a), str(img_b)]


@pytest.mark.asyncio
async def test_process_message_persists_media_only_turn_without_text(tmp_path: Path) -> None:
    """A turn with images but no text still persists (previously silent-dropped).

    The old early-persist gate skipped messages without text, leaving pure
    image turns un-checkpointed. They now materialise as an empty-content
    user row with ``media`` attached.
    """
    img = tmp_path / "only.png"
    img.write_bytes(_PNG_1X1)

    loop = _make_full_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]
    loop._run_agent_loop = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]

    msg = InboundMessage(
        channel="websocket",
        sender_id="u1",
        chat_id="c-images-only",
        content="",
        media=[str(img)],
    )
    with pytest.raises(RuntimeError):
        await loop._process_message(msg)

    loop.sessions.invalidate("websocket:c-images-only")
    persisted = loop.sessions.get_or_create("websocket:c-images-only")
    assert len(persisted.messages) == 1
    assert persisted.messages[0]["role"] == "user"
    assert persisted.messages[0]["content"] == ""
    assert persisted.messages[0]["media"] == [str(img)]


@pytest.mark.asyncio
async def test_process_message_does_not_duplicate_early_persisted_user_message(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]
    loop._run_agent_loop = AsyncMock(return_value=(
        "done",
        None,
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "done"},
        ],
        "stop",
        False,
    ))  # type: ignore[method-assign]

    result = await loop._process_message(
        InboundMessage(channel="feishu", sender_id="u1", chat_id="c2", content="hello")
    )

    assert result is not None
    assert result.content == "done"
    session = loop.sessions.get_or_create("feishu:c2")
    assert [
        {k: v for k, v in m.items() if k in {"role", "content"}}
        for m in session.messages
    ] == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "done"},
    ]
    assert AgentLoop._PENDING_USER_TURN_KEY not in session.metadata


@pytest.mark.asyncio
async def test_process_message_uses_context_chat_id_for_runtime_prompt(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]
    loop.context.build_messages = MagicMock(  # type: ignore[method-assign]
        return_value=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "runtime + hello"},
        ]
    )
    loop._run_agent_loop = AsyncMock(return_value=(  # type: ignore[method-assign]
        "done",
        [],
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "runtime + hello"},
            {"role": "assistant", "content": "done"},
        ],
        "stop",
        False,
    ))

    result = await loop._process_message(
        InboundMessage(
            channel="discord",
            sender_id="u1",
            chat_id="thread-777",
            content="hello",
            metadata={"context_chat_id": "parent-456"},
            session_key_override="discord:parent-456:thread:thread-777",
        )
    )

    assert result is not None
    assert result.chat_id == "thread-777"
    assert loop.context.build_messages.call_args.kwargs["chat_id"] == "parent-456"
    assert loop._run_agent_loop.call_args.kwargs["chat_id"] == "thread-777"


@pytest.mark.asyncio
async def test_process_message_uses_explicit_session_metadata_for_goal_context(
    tmp_path: Path,
) -> None:
    loop = _make_full_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]
    chat_session = loop.sessions.get_or_create("websocket:chat-with-goal")
    chat_session.metadata[GOAL_STATE_KEY] = {
        "status": "active",
        "objective": "This chat goal must not leak into heartbeat.",
    }
    loop.sessions.save(chat_session)
    system_session = loop.sessions.get_or_create("heartbeat")
    system_session.metadata = {}
    loop.sessions.save(system_session)

    loop.context.build_messages = MagicMock(  # type: ignore[method-assign]
        return_value=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "runtime + heartbeat"},
        ]
    )
    loop._run_agent_loop = AsyncMock(return_value=(  # type: ignore[method-assign]
        "ok",
        [],
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "runtime + heartbeat"},
            {"role": "assistant", "content": "ok"},
        ],
        "stop",
        False,
    ))

    result = await loop._process_message(
        InboundMessage(
            channel="websocket",
            sender_id="heartbeat",
            chat_id="chat-with-goal",
            content="heartbeat work",
        ),
        session_key="heartbeat",
    )

    assert result is not None
    assert result.content == "ok"
    kwargs = loop.context.build_messages.call_args.kwargs
    assert kwargs["chat_id"] == "chat-with-goal"
    assert kwargs["session_metadata"] is system_session.metadata
    assert GOAL_STATE_KEY not in kwargs["session_metadata"]


def test_set_tool_context_uses_effective_key_for_spawn_tool(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    spawn_tool = loop.tools.get("spawn")
    assert spawn_tool is not None

    loop._set_tool_context(
        "discord",
        "thread-777",
        session_key="discord:parent-456:thread:thread-777",
    )

    assert spawn_tool._origin_channel.get() == "discord"  # type: ignore[attr-defined]
    assert spawn_tool._origin_chat_id.get() == "thread-777"  # type: ignore[attr-defined]
    assert spawn_tool._session_key.get() == "discord:parent-456:thread:thread-777"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_next_turn_after_crash_closes_pending_user_turn_before_new_input(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]
    loop.provider.chat_with_retry = AsyncMock(return_value=MagicMock())  # unused because _run_agent_loop is stubbed

    session = loop.sessions.get_or_create("feishu:c3")
    session.add_message("user", "old question")
    session.metadata[AgentLoop._PENDING_USER_TURN_KEY] = True
    loop.sessions.save(session)

    loop._run_agent_loop = AsyncMock(return_value=(
        "new answer",
        None,
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "Error: Task interrupted before a response was generated."},
            {"role": "user", "content": "new question"},
            {"role": "assistant", "content": "new answer"},
        ],
        "stop",
        False,
    ))  # type: ignore[method-assign]

    result = await loop._process_message(
        InboundMessage(channel="feishu", sender_id="u1", chat_id="c3", content="new question")
    )

    assert result is not None
    assert result.content == "new answer"
    session = loop.sessions.get_or_create("feishu:c3")
    assert [
        {k: v for k, v in m.items() if k in {"role", "content"}}
        for m in session.messages
    ] == [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "Error: Task interrupted before a response was generated."},
        {"role": "user", "content": "new question"},
        {"role": "assistant", "content": "new answer"},
    ]
    assert AgentLoop._PENDING_USER_TURN_KEY not in session.metadata


@pytest.mark.asyncio
async def test_stop_preserves_runtime_checkpoint_for_next_turn(tmp_path: Path) -> None:
    from nanobot.command.builtin import cmd_stop
    from nanobot.command.router import CommandContext

    loop = _make_full_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]

    checkpoint_saved = asyncio.Event()

    async def interrupted_run_agent_loop(_initial_messages, *, session=None, **_kwargs):
        assert session is not None
        loop._set_runtime_checkpoint(
            session,
            {
                "assistant_message": {
                    "role": "assistant",
                    "content": "working",
                    "tool_calls": [
                        {
                            "id": "call_done",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"},
                        },
                        {
                            "id": "call_pending",
                            "type": "function",
                            "function": {"name": "exec", "arguments": "{}"},
                        },
                    ],
                },
                "completed_tool_results": [
                    {
                        "role": "tool",
                        "tool_call_id": "call_done",
                        "name": "read_file",
                        "content": "ok",
                    }
                ],
                "pending_tool_calls": [
                    {
                        "id": "call_pending",
                        "type": "function",
                        "function": {"name": "exec", "arguments": "{}"},
                    }
                ],
            },
        )
        checkpoint_saved.set()
        await asyncio.Event().wait()

    loop._run_agent_loop = interrupted_run_agent_loop  # type: ignore[method-assign]

    first_msg = InboundMessage(channel="feishu", sender_id="u1", chat_id="c4", content="keep progress")
    task = asyncio.create_task(loop._process_message(first_msg))
    loop._active_tasks[first_msg.session_key] = [task]
    await asyncio.wait_for(checkpoint_saved.wait(), timeout=1.0)

    stop_msg = InboundMessage(channel="feishu", sender_id="u1", chat_id="c4", content="/stop")
    stop_ctx = CommandContext(msg=stop_msg, session=None, key=stop_msg.session_key, raw="/stop", loop=loop)
    stop_result = await cmd_stop(stop_ctx)

    assert "Stopped 1 task" in stop_result.content
    assert task.done()

    loop.sessions.invalidate("feishu:c4")
    interrupted = loop.sessions.get_or_create("feishu:c4")
    assert interrupted.metadata.get(AgentLoop._PENDING_USER_TURN_KEY) is True
    assert interrupted.metadata.get(AgentLoop._RUNTIME_CHECKPOINT_KEY) is not None

    async def resumed_run_agent_loop(initial_messages, **_kwargs):
        return (
            "next answer",
            None,
            [*initial_messages, {"role": "assistant", "content": "next answer"}],
            "stop",
            False,
        )

    loop._run_agent_loop = resumed_run_agent_loop  # type: ignore[method-assign]
    result = await loop._process_message(
        InboundMessage(channel="feishu", sender_id="u1", chat_id="c4", content="continue here")
    )

    assert result is not None
    assert result.content == "next answer"

    session = loop.sessions.get_or_create("feishu:c4")
    assert [
        {k: v for k, v in m.items() if k in {"role", "content", "tool_call_id", "name"}}
        for m in session.messages
    ] == [
        {"role": "user", "content": "keep progress"},
        {"role": "assistant", "content": "working"},
        {"role": "tool", "tool_call_id": "call_done", "name": "read_file", "content": "ok"},
        {
            "role": "tool",
            "tool_call_id": "call_pending",
            "name": "exec",
            "content": "Error: Task interrupted before this tool finished.",
        },
        {"role": "user", "content": "continue here"},
        {"role": "assistant", "content": "next answer"},
    ]
    assert AgentLoop._PENDING_USER_TURN_KEY not in session.metadata
    assert AgentLoop._RUNTIME_CHECKPOINT_KEY not in session.metadata


@pytest.mark.asyncio
async def test_system_subagent_followup_is_persisted_before_prompt_assembly(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]

    session = loop.sessions.get_or_create("cli:test")
    session.add_message("user", "question")
    session.add_message("assistant", "working")
    loop.sessions.save(session)

    seen: dict[str, list[dict]] = {}

    async def fake_run_agent_loop(initial_messages, **_kwargs):
        seen["initial_messages"] = initial_messages
        return (
            "done",
            [],
            [*initial_messages, {"role": "assistant", "content": "done"}],
            "stop",
            False,
        )

    loop._run_agent_loop = fake_run_agent_loop  # type: ignore[method-assign]

    await loop._process_message(
        InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id="cli:test",
            content="subagent result",
            metadata={"subagent_task_id": "sub-1"},
        )
    )

    non_system = [m for m in seen["initial_messages"] if m.get("role") != "system"]
    assert "question" in non_system[0]["content"]
    assert "working" in non_system[1]["content"]
    # User turns carry the timestamp prefix so the model can reason about
    # relative time. Assistant turns do NOT, otherwise the model treats those
    # past replies as in-context examples and starts its own outputs with
    # ``[Message Time: ...]`` (which then leaks back to the user).
    assert "[Message Time:" in non_system[0]["content"]
    assert "[Message Time:" not in non_system[1]["content"]
    assert non_system[2]["content"].count("subagent result") == 1
    assert "Current Time:" in non_system[2]["content"]

    loop.sessions.invalidate("cli:test")
    persisted = loop.sessions.get_or_create("cli:test")
    assert [
        {k: v for k, v in m.items() if k in {"role", "content", "injected_event", "subagent_task_id"}}
        for m in persisted.messages
    ] == [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "working"},
        {
            "role": "assistant",
            "content": "subagent result",
            "injected_event": "subagent_result",
            "subagent_task_id": "sub-1",
        },
        {"role": "assistant", "content": "done"},
    ]


@pytest.mark.asyncio
async def test_multiple_subagent_followups_all_persist_as_standalone_history(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]

    async def fake_run_agent_loop(initial_messages, **_kwargs):
        return (
            "ack",
            [],
            [*initial_messages, {"role": "assistant", "content": "ack"}],
            "stop",
            False,
        )

    loop._run_agent_loop = fake_run_agent_loop  # type: ignore[method-assign]

    for idx in range(3):
        await loop._process_message(
            InboundMessage(
                channel="system",
                sender_id="subagent",
                chat_id="cli:multi",
                content=f"subagent result {idx}",
                metadata={"subagent_task_id": f"sub-{idx}"},
            )
        )

    loop.sessions.invalidate("cli:multi")
    persisted = loop.sessions.get_or_create("cli:multi")
    followups = [m for m in persisted.messages if m.get("injected_event") == "subagent_result"]
    assert [m["content"] for m in followups] == [
        "subagent result 0",
        "subagent result 1",
        "subagent result 2",
    ]


def test_prompt_merge_does_not_replace_standalone_subagent_history_entry(tmp_path: Path) -> None:
    loop = _mk_loop()
    session = Session(key="cli:merge")
    session.add_message("assistant", "previous assistant")

    inserted = loop._persist_subagent_followup(
        session,
        InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id="cli:merge",
            content="subagent result",
            metadata={"subagent_task_id": "sub-1"},
        ),
    )

    assert inserted is True

    builder = ContextBuilder(tmp_path)
    projected = builder.build_messages(
        history=session.get_history(max_messages=0),
        current_message="",
        current_role="assistant",
        channel="cli",
        chat_id="merge",
    )

    non_system = [m for m in projected if m.get("role") != "system"]
    assert len(non_system) == 2
    assert "subagent result" in non_system[-1]["content"]
    assert session.messages[-1]["content"] == "subagent result"
    assert session.messages[-1]["injected_event"] == "subagent_result"


def test_subagent_followup_dedupes_by_task_id() -> None:
    loop = _mk_loop()
    session = Session(key="cli:dedupe")
    msg = InboundMessage(
        channel="system",
        sender_id="subagent",
        chat_id="cli:dedupe",
        content="subagent result",
        metadata={"subagent_task_id": "sub-1"},
    )

    assert loop._persist_subagent_followup(session, msg) is True
    assert loop._persist_subagent_followup(session, msg) is False
    assert len(session.messages) == 1


def test_subagent_followup_skips_empty_content() -> None:
    loop = _mk_loop()
    session = Session(key="cli:empty")
    msg = InboundMessage(
        channel="system",
        sender_id="subagent",
        chat_id="cli:empty",
        content="",
        metadata={"subagent_task_id": "sub-empty"},
    )

    assert loop._persist_subagent_followup(session, msg) is False
    assert session.messages == []


def test_set_tool_context_passes_thread_session_key_to_spawn(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)

    loop._set_tool_context(
        "slack",
        "C123",
        message_id="msg-123",
        metadata={"slack": {"thread_ts": "1700.42", "channel_type": "channel"}},
        session_key="slack:C123:1700.42",
    )

    spawn_tool = loop.tools.get("spawn")
    assert spawn_tool is not None
    assert spawn_tool._session_key.get() == "slack:C123:1700.42"
    assert spawn_tool._origin_message_id.get() == "msg-123"


@pytest.mark.asyncio
async def test_system_subagent_followup_uses_thread_session_and_slack_metadata(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]

    thread_session = loop.sessions.get_or_create("slack:C123:1700.42")
    thread_session.add_message("user", "thread question")
    loop.sessions.save(thread_session)

    seen: dict[str, list[dict]] = {}

    async def fake_run_agent_loop(initial_messages, **_kwargs):
        seen["initial_messages"] = initial_messages
        return (
            "done",
            [],
            [*initial_messages, {"role": "assistant", "content": "done"}],
            "stop",
            False,
        )

    loop._run_agent_loop = fake_run_agent_loop  # type: ignore[method-assign]

    outbound = await loop._process_message(
        InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id="slack:C123",
            content="subagent result",
            session_key_override="slack:C123:1700.42",
            metadata={"subagent_task_id": "sub-1", "origin_message_id": "msg-123"},
        )
    )

    assert outbound is not None
    assert outbound.channel == "slack"
    assert outbound.chat_id == "C123"
    assert outbound.metadata == {
        "slack": {"thread_ts": "1700.42"},
        "origin_message_id": "msg-123",
    }
    assert "thread question" in seen["initial_messages"][1]["content"]

    loop.sessions.invalidate("slack:C123:1700.42")
    persisted = loop.sessions.get_or_create("slack:C123:1700.42")
    assert any(m.get("subagent_task_id") == "sub-1" for m in persisted.messages)
