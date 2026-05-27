"""Structured progress-event helpers shared by agent runtimes."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from nanobot.agent.hook import AgentHookContext

_READ_TOOLS = frozenset({"read_file", "list_dir", "grep"})
_WRITE_TOOLS = frozenset({"write_file", "edit_file", "notebook_edit"})
_NETWORK_TOOLS = frozenset({"web_search", "web_fetch", "search"})
_SHELL_TOOLS = frozenset({"exec", "shell"})


def on_progress_accepts_tool_events(cb: Callable[..., Any]) -> bool:
    return _on_progress_accepts(cb, "tool_events")


def on_progress_accepts_file_edit_events(cb: Callable[..., Any]) -> bool:
    return _on_progress_accepts(cb, "file_edit_events")


def _on_progress_accepts(cb: Callable[..., Any], name: str) -> bool:
    try:
        sig = inspect.signature(cb)
    except (TypeError, ValueError):
        return False
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return True
    return name in sig.parameters


async def invoke_on_progress(
    on_progress: Callable[..., Awaitable[None]],
    content: str,
    *,
    tool_hint: bool = False,
    tool_events: list[dict[str, Any]] | None = None,
) -> None:
    if tool_events and on_progress_accepts_tool_events(on_progress):
        await on_progress(content, tool_hint=tool_hint, tool_events=tool_events)
        return
    await on_progress(content, tool_hint=tool_hint)


async def invoke_file_edit_progress(
    on_progress: Callable[..., Awaitable[None]],
    file_edit_events: list[dict[str, Any]],
) -> None:
    if not file_edit_events or not on_progress_accepts_file_edit_events(on_progress):
        return
    await on_progress("", file_edit_events=file_edit_events)


def build_tool_event_start_payload(
    tool_call: Any,
    *,
    checkpoint_id: str | None = None,
    scheduling: dict[str, Any] | None = None,
    capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    name = getattr(tool_call, "name", "")
    arguments = getattr(tool_call, "arguments", {}) or {}
    payload = {
        "version": 1,
        "phase": "start",
        "call_id": str(getattr(tool_call, "id", "") or ""),
        "name": name,
        "arguments": arguments,
        "result": None,
        "error": None,
        "files": [],
        "embeds": [],
        "started_at": datetime.now(UTC).isoformat(),
    }
    if checkpoint_id:
        payload["checkpoint_id"] = checkpoint_id
    if scheduling:
        payload.update(scheduling)
    payload.update(tool_risk_metadata(name, arguments))
    payload.update(tool_capability_metadata(capabilities))
    return payload


def build_tool_event_queued_payload(
    tool_call: Any,
    *,
    checkpoint_id: str | None = None,
    scheduling: dict[str, Any] | None = None,
    capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = build_tool_event_start_payload(
        tool_call,
        checkpoint_id=checkpoint_id,
        scheduling=scheduling,
        capabilities=capabilities,
    )
    payload["phase"] = "queued"
    payload["started_at"] = None
    payload["queued_at"] = datetime.now(UTC).isoformat()
    return payload


def tool_capability_metadata(metadata: Any | None = None) -> dict[str, Any]:
    """Return UI-safe capability metadata from a registered tool record."""
    if not isinstance(metadata, dict):
        return {}
    out: dict[str, Any] = {}
    if isinstance(metadata.get("read_only"), bool):
        out["read_only"] = metadata["read_only"]
    if isinstance(metadata.get("concurrency_safe"), bool):
        out["concurrency_safe"] = metadata["concurrency_safe"]
    if isinstance(metadata.get("exclusive"), bool):
        out["exclusive"] = metadata["exclusive"]
    if isinstance(metadata.get("config_key"), str):
        out["config_key"] = metadata["config_key"]
    scopes = metadata.get("scopes")
    if isinstance(scopes, (list, tuple)) and all(isinstance(scope, str) for scope in scopes):
        out["scopes"] = list(scopes)
    return out


def tool_risk_metadata(name: str, arguments: Any | None = None) -> dict[str, Any]:
    """Return stable, UI-safe risk metadata for a tool invocation."""
    category = _tool_risk_category(name)
    level = _tool_risk_level(category, arguments)
    safety: dict[str, Any] = {
        "category": category,
        "level": level,
    }
    if category == "shell":
        safety["requires_sandbox"] = True
    return {
        "risk_category": category,
        "risk_level": level,
        "safety": safety,
    }


def _tool_risk_category(name: str) -> str:
    if name.startswith("mcp_"):
        return "mcp"
    if name in _READ_TOOLS:
        return "read"
    if name in _WRITE_TOOLS:
        return "write"
    if name in _NETWORK_TOOLS:
        return "network"
    if name in _SHELL_TOOLS:
        return "shell"
    return "tool"


def _tool_risk_level(category: str, arguments: Any | None = None) -> str:
    if category == "read":
        return "low"
    if category in {"write", "shell"}:
        return "high"
    if category in {"network", "mcp"}:
        return "medium"
    return "medium"


def tool_event_result_extras(result: Any) -> tuple[list[Any], list[Any]]:
    if not isinstance(result, dict):
        return [], []
    files = result.get("files") if isinstance(result.get("files"), list) else []
    embeds = result.get("embeds") if isinstance(result.get("embeds"), list) else []
    return files, embeds


def build_tool_event_finish_payloads(context: AgentHookContext) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    count = min(len(context.tool_calls), len(context.tool_results), len(context.tool_events))
    for idx in range(count):
        tool_call = context.tool_calls[idx]
        result = context.tool_results[idx]
        event = context.tool_events[idx] if isinstance(context.tool_events[idx], dict) else {}
        status = event.get("status")
        phase = "end" if status == "ok" else "error"
        files, embeds = tool_event_result_extras(result)
        payload = {
            "version": 1,
            "phase": phase,
            "call_id": str(getattr(tool_call, "id", "") or ""),
            "name": getattr(tool_call, "name", ""),
            "arguments": getattr(tool_call, "arguments", {}) or {},
            "result": result if phase == "end" else None,
            "error": None,
            "files": files,
            "embeds": embeds,
        }
        for key in (
            "queued_at",
            "started_at",
            "completed_at",
            "duration_ms",
            "elapsed_ms",
            "batch_id",
            "batch_index",
            "batch_count",
            "batch_size",
            "concurrency_limit",
            "queue_position",
            "failure_category",
            "recovery_action",
            "retryable",
            "needs_user_input",
            "read_only",
            "concurrency_safe",
            "exclusive",
            "config_key",
            "scopes",
        ):
            if key in event:
                payload[key] = event[key]
        if context.checkpoint_id:
            payload["checkpoint_id"] = context.checkpoint_id
        payload.update(tool_risk_metadata(payload["name"], payload["arguments"]))
        if isinstance(event.get("safety"), dict):
            payload["safety"] = {**payload["safety"], **event["safety"]}
        if isinstance(event.get("risk_category"), str):
            payload["risk_category"] = event["risk_category"]
        if isinstance(event.get("risk_level"), str):
            payload["risk_level"] = event["risk_level"]
            payload["safety"]["level"] = event["risk_level"]
        if phase == "error":
            if isinstance(result, str) and result.strip():
                payload["error"] = result.strip()
            else:
                payload["error"] = str(event.get("detail") or "Tool execution failed")
        payloads.append(payload)
    return payloads
