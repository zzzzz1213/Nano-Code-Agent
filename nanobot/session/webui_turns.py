"""Session turn helpers for WebUI-capable WebSocket sessions.

AgentLoop uses these without importing a concrete channel plugin; only
``channel == "websocket"`` messages are affected.
"""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider
from nanobot.session.goal_state import goal_state_ws_blob
from nanobot.session.manager import Session, SessionManager
from nanobot.utils.helpers import truncate_text
from nanobot.utils.llm_runtime import LLMRuntime

WEBUI_SESSION_METADATA_KEY = "webui"
WEBUI_TITLE_METADATA_KEY = "title"
WEBUI_TITLE_USER_EDITED_METADATA_KEY = "title_user_edited"
TITLE_MAX_CHARS = 60
TITLE_GENERATION_MAX_TOKENS = 96
TITLE_GENERATION_REASONING_EFFORT = "none"

# Wall-clock turn start per ``chat_id`` (websocket only). Survives browser refresh while the
# gateway process stays up; cleared on idle/stop and implicitly dropped on restart.
_WEBSOCKET_TURN_WALL_STARTED_AT: dict[str, float] = {}
_CHECK_COMMAND_MARKERS = (
    "pytest",
    "ruff",
    "mypy",
    "npm test",
    "npm run test",
    "npm run build",
    "bun test",
    "bun run test",
    "bun run build",
    "pnpm test",
    "pnpm build",
    "yarn test",
    "yarn build",
)
_FILE_EDIT_TOOLS = {
    "edit",
    "edit_file",
    "write",
    "write_file",
    "create_file",
    "delete_file",
    "replace_file",
    "notebook_edit",
}


def mark_webui_session(session: Session, metadata: dict[str, Any]) -> bool:
    """Persist a WebUI marker only when the inbound websocket frame opted in."""
    if metadata.get(WEBUI_SESSION_METADATA_KEY) is not True:
        return False
    session.metadata[WEBUI_SESSION_METADATA_KEY] = True
    return True


def clean_generated_title(raw: str | None) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    text = re.sub(r"^\s*(title|标题)\s*[:：]\s*", "", text, flags=re.IGNORECASE)
    text = text.strip().strip("\"'`“”‘’")
    text = re.sub(r"\s+", " ", text).strip()
    text = text.rstrip("。.!！?？,，;；:")
    if len(text) > TITLE_MAX_CHARS:
        text = text[: TITLE_MAX_CHARS - 1].rstrip() + "…"
    return text


def _title_inputs(session: Session) -> tuple[str, str]:
    user_text = ""
    assistant_text = ""
    for message in session.messages:
        if message.get("_command") is True:
            continue
        role = message.get("role")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if role == "user" and not user_text:
            user_text = content.strip()
        elif role == "assistant" and not assistant_text:
            assistant_text = content.strip()
        if user_text and assistant_text:
            break
    return user_text, assistant_text


async def maybe_generate_webui_title(
    *,
    sessions: SessionManager,
    session_key: str,
    provider: LLMProvider,
    model: str,
) -> bool:
    """Generate and persist a short title for WebUI-owned sessions only."""
    session = sessions.get_or_create(session_key)
    if session.metadata.get(WEBUI_SESSION_METADATA_KEY) is not True:
        return False
    if session.metadata.get(WEBUI_TITLE_USER_EDITED_METADATA_KEY) is True:
        return False
    current_title = session.metadata.get(WEBUI_TITLE_METADATA_KEY)
    if isinstance(current_title, str) and current_title.strip():
        return False

    user_text, assistant_text = _title_inputs(session)
    if not user_text:
        return False

    prompt = (
        "Generate a concise title for this chat.\n"
        "Rules:\n"
        "- Use the same language as the user when practical.\n"
        "- 3 to 8 words.\n"
        "- No quotes.\n"
        "- No punctuation at the end.\n"
        "- Return only the title.\n\n"
        f"User: {truncate_text(user_text, 1_000)}"
    )
    if assistant_text:
        prompt += f"\nAssistant: {truncate_text(assistant_text, 1_000)}"

    try:
        response = await provider.chat_with_retry(
            [
                {
                    "role": "system",
                    "content": (
                        "You write short, neutral chat titles. "
                        "Return only the title text."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            tools=None,
            model=model,
            max_tokens=TITLE_GENERATION_MAX_TOKENS,
            temperature=0.2,
            reasoning_effort=TITLE_GENERATION_REASONING_EFFORT,
            retry_mode="standard",
        )
    except Exception:
        logger.debug("Failed to generate webui session title for {}", session_key, exc_info=True)
        return False

    title = clean_generated_title(response.content)
    if not title or title.lower().startswith("error"):
        logger.debug(
            "WebUI title generation returned no usable title for {} (finish_reason={})",
            session_key,
            response.finish_reason,
        )
        return False
    session.metadata[WEBUI_TITLE_METADATA_KEY] = title
    sessions.save(session)
    return True


async def maybe_generate_webui_title_after_turn(
    *,
    channel: str,
    metadata: dict[str, Any],
    sessions: SessionManager,
    session_key: str,
    provider: LLMProvider,
    model: str,
) -> bool:
    if channel != "websocket" or metadata.get(WEBUI_SESSION_METADATA_KEY) is not True:
        return False
    return await maybe_generate_webui_title(
        sessions=sessions,
        session_key=session_key,
        provider=provider,
        model=model,
    )


def websocket_turn_wall_started_at(chat_id: str) -> float | None:
    """Return ``time.time()`` when the active user turn began, if still running."""
    return _WEBSOCKET_TURN_WALL_STARTED_AT.get(chat_id)


def _tool_call_name(call: Any) -> str:
    if not isinstance(call, dict):
        return ""
    fn = call.get("function")
    if isinstance(fn, dict) and isinstance(fn.get("name"), str):
        return fn["name"]
    name = call.get("name")
    return name if isinstance(name, str) else ""


def _tool_call_id(call: Any) -> str:
    if not isinstance(call, dict):
        return ""
    raw = call.get("tool_call_id") or call.get("id") or call.get("call_id")
    return raw if isinstance(raw, str) else ""


def _tool_call_arguments_text(call: Any) -> str:
    if not isinstance(call, dict):
        return ""
    fn = call.get("function")
    args = fn.get("arguments") if isinstance(fn, dict) else call.get("arguments")
    if isinstance(args, str):
        return args
    if isinstance(args, dict):
        return " ".join(str(v) for v in args.values())
    return ""


def _is_file_edit_tool(name: str) -> bool:
    lowered = name.lower()
    return lowered in _FILE_EDIT_TOOLS or "edit" in lowered or "write" in lowered


def _is_check_tool(name: str, args_text: str = "") -> bool:
    lowered_name = name.lower()
    if any(marker in lowered_name for marker in ("test", "lint", "build", "check")):
        return True
    haystack = f"{lowered_name} {args_text.lower()}"
    return any(marker in haystack for marker in _CHECK_COMMAND_MARKERS)


def _tool_result_failed(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    content = str(result.get("content") or "").lower()
    return (
        content.startswith("error:")
        or "exit code 1" in content
        or "exit_code\":1" in content
        or "returncode\":1" in content
    )


def build_turn_checkpoint(payload: dict[str, Any], *, turn_id: str) -> dict[str, Any]:
    """Condense an internal runner checkpoint into the stable WebUI shape."""
    pending = payload.get("pending_tool_calls")
    completed = payload.get("completed_tool_results")
    pending_calls = pending if isinstance(pending, list) else []
    completed_results = completed if isinstance(completed, list) else []
    phase = payload.get("phase")
    pending_names = [_tool_call_name(call) for call in pending_calls]
    completed_names = [_tool_call_name(result) for result in completed_results]
    tool_call_count = len(pending_calls) + len(completed_results)
    last_tool_call_id = ""
    if completed_results:
        last_tool_call_id = _tool_call_id(completed_results[-1])
    if not last_tool_call_id and pending_calls:
        last_tool_call_id = _tool_call_id(pending_calls[-1])
    pending_tool_call_ids = _string_list(payload.get("pending_tool_call_ids"))
    completed_tool_call_ids = _string_list(payload.get("completed_tool_call_ids"))
    executed_tool_call_ids = _string_list(payload.get("executed_tool_call_ids"))
    reused_tool_call_ids = _string_list(payload.get("reused_tool_call_ids"))
    compensation_tool_call_ids = _string_list(payload.get("compensation_tool_call_ids"))
    retryable_tool_call_ids = _string_list(payload.get("retryable_tool_call_ids"))
    requires_user_tool_call_ids = _string_list(payload.get("requires_user_tool_call_ids"))
    resumable_tool_call_ids = _string_list(payload.get("resumable_tool_call_ids"))
    skipped_duplicate_tool_call_ids = _string_list(payload.get("skipped_duplicate_tool_call_ids"))
    recovered_executed_tool_call_ids = _string_list(payload.get("recovered_executed_tool_call_ids"))
    recovered_skipped_tool_call_ids = _string_list(payload.get("recovered_skipped_tool_call_ids"))
    recovered_requires_user_tool_call_ids = _string_list(payload.get("recovered_requires_user_tool_call_ids"))
    safe_resume_tool_call_ids = _string_list(payload.get("safe_resume_tool_call_ids"))
    review_required_tool_call_ids = _string_list(payload.get("review_required_tool_call_ids"))
    needs_input_tool_call_ids = _string_list(payload.get("needs_input_tool_call_ids"))
    blocked_tool_call_ids = _string_list(payload.get("blocked_tool_call_ids"))
    recovery_review_items = _dict_list(payload.get("recovery_review_items"))
    if not pending_tool_call_ids:
        pending_tool_call_ids = [
            call_id for call in pending_calls if (call_id := _tool_call_id(call))
        ]
    if not completed_tool_call_ids:
        completed_tool_call_ids = [
            call_id for result in completed_results if (call_id := _tool_call_id(result))
        ]
    if not executed_tool_call_ids:
        executed_tool_call_ids = list(completed_tool_call_ids)

    file_edit_count = sum(
        1 for name in [*pending_names, *completed_names] if _is_file_edit_tool(name)
    )
    pending_checks = [
        call for call in pending_calls if _is_check_tool(_tool_call_name(call), _tool_call_arguments_text(call))
    ]
    completed_checks = [
        result
        for result in completed_results
        if _is_check_tool(_tool_call_name(result), _tool_call_arguments_text(result))
    ]
    if pending_checks:
        check_state = "running"
    elif completed_checks:
        check_state = "failed" if any(_tool_result_failed(result) for result in completed_checks) else "passed"
    else:
        check_state = "none"

    return {
        "version": 1,
        "checkpoint_id": payload.get("checkpoint_id") if isinstance(payload.get("checkpoint_id"), str) else None,
        "turn_id": turn_id,
        "phase": phase if isinstance(phase, str) and phase else "unknown",
        "tool_call_count": tool_call_count,
        "pending_tool_count": len(pending_calls),
        "completed_tool_count": len(completed_results),
        "pending_tool_call_ids": pending_tool_call_ids,
        "completed_tool_call_ids": completed_tool_call_ids,
        "executed_tool_call_ids": executed_tool_call_ids,
        "reused_tool_call_ids": reused_tool_call_ids,
        "reused_tool_count": finite_count(payload.get("reused_tool_count"), len(reused_tool_call_ids)),
        "compensation_tool_call_ids": compensation_tool_call_ids,
        "compensation_tool_count": finite_count(
            payload.get("compensation_tool_count"),
            len(compensation_tool_call_ids),
        ),
        "retryable_tool_call_ids": retryable_tool_call_ids,
        "retryable_tool_count": finite_count(
            payload.get("retryable_tool_count"),
            len(retryable_tool_call_ids),
        ),
        "requires_user_tool_call_ids": requires_user_tool_call_ids,
        "requires_user_tool_count": finite_count(
            payload.get("requires_user_tool_count"),
            len(requires_user_tool_call_ids),
        ),
        "resumable_tool_call_ids": resumable_tool_call_ids,
        "resumable_tool_count": finite_count(
            payload.get("resumable_tool_count"),
            len(resumable_tool_call_ids),
        ),
        "recovered_executed_tool_call_ids": recovered_executed_tool_call_ids,
        "recovered_executed_tool_count": finite_count(
            payload.get("recovered_executed_tool_count"),
            len(recovered_executed_tool_call_ids),
        ),
        "recovered_skipped_tool_call_ids": recovered_skipped_tool_call_ids,
        "recovered_skipped_tool_count": finite_count(
            payload.get("recovered_skipped_tool_count"),
            len(recovered_skipped_tool_call_ids),
        ),
        "recovered_requires_user_tool_call_ids": recovered_requires_user_tool_call_ids,
        "recovered_requires_user_tool_count": finite_count(
            payload.get("recovered_requires_user_tool_count"),
            len(recovered_requires_user_tool_call_ids),
        ),
        "safe_resume_tool_call_ids": safe_resume_tool_call_ids,
        "safe_resume_tool_count": finite_count(
            payload.get("safe_resume_tool_count"),
            len(safe_resume_tool_call_ids),
        ),
        "review_required_tool_call_ids": review_required_tool_call_ids,
        "review_required_tool_count": finite_count(
            payload.get("review_required_tool_count"),
            len(review_required_tool_call_ids),
        ),
        "needs_input_tool_call_ids": needs_input_tool_call_ids,
        "needs_input_tool_count": finite_count(
            payload.get("needs_input_tool_count"),
            len(needs_input_tool_call_ids),
        ),
        "blocked_tool_call_ids": blocked_tool_call_ids,
        "blocked_tool_count": finite_count(
            payload.get("blocked_tool_count"),
            len(blocked_tool_call_ids),
        ),
        "recovery_review_items": recovery_review_items,
        "recovery_review_count": finite_count(
            payload.get("recovery_review_count"),
            len(recovery_review_items),
        ),
        "skipped_duplicate_tool_call_ids": skipped_duplicate_tool_call_ids,
        "skipped_duplicate_tool_count": finite_count(
            payload.get("skipped_duplicate_tool_count"),
            len(skipped_duplicate_tool_call_ids),
        ),
        "last_tool_call_id": last_tool_call_id or None,
        "file_edit_count": file_edit_count,
        "check_state": check_state,
        "recoverable": payload.get("recoverable") is True,
        "updated_at": payload.get("updated_at")
        if isinstance(payload.get("updated_at"), str)
        else datetime.now(UTC).isoformat(),
    }


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def finite_count(value: Any, fallback: int) -> int:
    return value if isinstance(value, int) and value >= 0 else fallback


async def publish_turn_run_status(bus: MessageBus, msg: InboundMessage, status: str) -> None:
    """Notify WebSocket clients while a user turn is executing (timing strip)."""
    if msg.channel != "websocket":
        return
    cid = str(msg.chat_id)
    meta: dict[str, Any] = {
        **dict(msg.metadata or {}),
        "_goal_status": True,
        "goal_status": status,
    }
    if status == "running":
        t0 = time.time()
        meta["started_at"] = t0
        _WEBSOCKET_TURN_WALL_STARTED_AT[cid] = t0
    else:
        _WEBSOCKET_TURN_WALL_STARTED_AT.pop(cid, None)
    await bus.publish_outbound(
        OutboundMessage(
            channel=msg.channel,
            chat_id=cid,
            content="",
            metadata=meta,
        ),
    )


def build_bus_progress_callback(
    bus: MessageBus,
    msg: InboundMessage,
) -> Callable[..., Awaitable[None]]:
    """Return the bus progress callback for agent runtime events."""

    async def _publish_progress(
        content: str,
        *,
        tool_hint: bool = False,
        tool_events: list[dict[str, Any]] | None = None,
        file_edit_events: list[dict[str, Any]] | None = None,
        reasoning: bool = False,
        reasoning_end: bool = False,
    ) -> None:
        meta = dict(msg.metadata or {})
        meta["_progress"] = True
        meta["_tool_hint"] = tool_hint
        if reasoning:
            meta["_reasoning_delta"] = True
        if reasoning_end:
            meta["_reasoning_end"] = True
        if tool_events:
            meta["_tool_events"] = tool_events
        if file_edit_events:
            meta["_file_edit_events"] = file_edit_events
        await bus.publish_outbound(
            OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=content,
                metadata=meta,
            )
        )

    if msg.channel == "websocket":
        async def _websocket_progress(
            content: str,
            *,
            tool_hint: bool = False,
            tool_events: list[dict[str, Any]] | None = None,
            file_edit_events: list[dict[str, Any]] | None = None,
            reasoning: bool = False,
            reasoning_end: bool = False,
        ) -> None:
            await _publish_progress(
                content,
                tool_hint=tool_hint,
                tool_events=tool_events,
                file_edit_events=file_edit_events,
                reasoning=reasoning,
                reasoning_end=reasoning_end,
            )

        return _websocket_progress

    async def _bus_progress(
        content: str,
        *,
        tool_hint: bool = False,
        tool_events: list[dict[str, Any]] | None = None,
        reasoning: bool = False,
        reasoning_end: bool = False,
    ) -> None:
        await _publish_progress(
            content,
            tool_hint=tool_hint,
            tool_events=tool_events,
            reasoning=reasoning,
            reasoning_end=reasoning_end,
        )

    return _bus_progress


@dataclass
class WebuiTurnCoordinator:
    """Own the WebUI/WebSocket wire details that hang off AgentLoop turns."""

    bus: MessageBus
    sessions: SessionManager
    schedule_background: Callable[[Awaitable[None]], None]
    _title_contexts: dict[str, LLMRuntime] = field(default_factory=dict)

    def capture_title_context(
        self,
        session_key: str,
        msg: InboundMessage,
        llm: LLMRuntime,
    ) -> None:
        if msg.channel == "websocket" and msg.metadata.get("webui") is True:
            self._title_contexts[session_key] = llm

    def discard(self, session_key: str) -> None:
        self._title_contexts.pop(session_key, None)

    async def publish_run_status(self, msg: InboundMessage, status: str) -> None:
        await publish_turn_run_status(self.bus, msg, status)

    async def publish_turn_checkpoint(
        self,
        msg: InboundMessage,
        *,
        checkpoint: dict[str, Any],
    ) -> None:
        if msg.channel != "websocket":
            return
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content="",
            metadata={
                **dict(msg.metadata or {}),
                "_turn_checkpoint": True,
                "turn_checkpoint": checkpoint,
            },
        ))

    async def publish_context_compaction(
        self,
        msg: InboundMessage,
        *,
        compaction: dict[str, Any],
    ) -> None:
        if msg.channel != "websocket":
            return
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content="",
            metadata={
                **dict(msg.metadata or {}),
                "_context_compaction": True,
                "context_compaction": compaction,
            },
        ))

    async def publish_memory_snapshot(
        self,
        msg: InboundMessage,
        *,
        snapshot: dict[str, Any],
    ) -> None:
        if msg.channel != "websocket":
            return
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content="",
            metadata={
                **dict(msg.metadata or {}),
                "_memory_snapshot": True,
                "memory_snapshot": snapshot,
            },
        ))

    async def publish_active_skills(
        self,
        msg: InboundMessage,
        *,
        skills: dict[str, Any],
    ) -> None:
        if msg.channel != "websocket":
            return
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content="",
            metadata={
                **dict(msg.metadata or {}),
                "_active_skills": True,
                "active_skills": skills,
            },
        ))

    async def publish_memory_candidate(
        self,
        msg: InboundMessage,
        *,
        candidate: dict[str, Any],
    ) -> None:
        if msg.channel != "websocket":
            return
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content="",
            metadata={
                **dict(msg.metadata or {}),
                "_memory_candidate": True,
                "memory_candidate": candidate,
            },
        ))

    async def handle_turn_end(
        self,
        msg: InboundMessage,
        *,
        session_key: str,
        latency_ms: int | None,
    ) -> None:
        if msg.channel != "websocket":
            return

        turn_metadata: dict[str, Any] = {**msg.metadata, "_turn_end": True}
        if latency_ms is not None:
            turn_metadata["latency_ms"] = int(latency_ms)
        session = self.sessions.get_or_create(session_key)
        turn_metadata["goal_state"] = goal_state_ws_blob(session.metadata)
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content="",
            metadata=turn_metadata,
        ))
        self._schedule_title_update(msg, session_key=session_key)

    def _schedule_title_update(self, msg: InboundMessage, *, session_key: str) -> None:
        title_context = self._title_contexts.pop(session_key, None)
        if msg.metadata.get("webui") is not True or title_context is None:
            return

        async def _generate_title_and_notify(
            title_llm: LLMRuntime = title_context,
        ) -> None:
            generated = await maybe_generate_webui_title_after_turn(
                channel=msg.channel,
                metadata=msg.metadata,
                sessions=self.sessions,
                session_key=session_key,
                provider=title_llm.provider,
                model=title_llm.model,
            )
            if generated:
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="",
                    metadata={
                        **msg.metadata,
                        "_session_updated": True,
                        "_session_update_scope": "metadata",
                    },
                ))

        self.schedule_background(_generate_title_and_notify())
