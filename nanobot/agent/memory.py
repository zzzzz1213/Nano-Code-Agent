"""Memory system: pure file I/O store, lightweight Consolidator, and Dream processor."""

from __future__ import annotations

import asyncio
import json
import os
import re
import weakref
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator

import tiktoken
from loguru import logger

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.session.manager import Session
from nanobot.utils.gitstore import GitStore
from nanobot.utils.helpers import (
    ensure_dir,
    estimate_message_tokens,
    estimate_prompt_tokens_chain,
    find_legal_message_start,
    strip_think,
    truncate_text,
)
from nanobot.utils.prompt_templates import render_template
from nanobot.agent.retriever import MemoryRetriever

# Module-level retriever instance for lightweight in-memory retrieval of
# compaction summaries. This is an MVP and can be replaced by a more
# sophisticated backend later.
retriever = MemoryRetriever()

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider
    from nanobot.session.manager import SessionManager


def _messages_token_estimate(messages: list[dict[str, Any]]) -> int:
    return sum(estimate_message_tokens(message) for message in messages)


_SUMMARY_SECTION_ORDER = (
    "goal",
    "constraints",
    "decisions",
    "failures",
    "files_touched",
    "commands_run",
    "next_steps",
    "overview",
)
_SUMMARY_SECTION_LABELS = {
    "overview": "Overview",
    "goal": "Goal",
    "constraints": "Constraints",
    "files_touched": "Files touched",
    "commands_run": "Commands run",
    "failures": "Failures",
    "decisions": "Decisions",
    "next_steps": "Next steps",
}
_SUMMARY_SECTION_ALIASES = {
    "summary": "overview",
    "overview": "overview",
    "goal": "goal",
    "objective": "goal",
    "objectives": "goal",
    "constraints": "constraints",
    "constraint": "constraints",
    "files": "files_touched",
    "files touched": "files_touched",
    "files_touched": "files_touched",
    "commands": "commands_run",
    "commands run": "commands_run",
    "commands_run": "commands_run",
    "failures": "failures",
    "failure": "failures",
    "errors": "failures",
    "decisions": "decisions",
    "decision": "decisions",
    "next": "next_steps",
    "next steps": "next_steps",
    "next_steps": "next_steps",
}
_SUMMARY_SECTION_HEADER_RE = re.compile(r"^(?:#{1,6}\s*)?([A-Za-z_ ]+?)(?:\s*[:\-]\s*)?(.*)$")
_SUMMARY_SECTION_MAX_ITEMS = 8
_SUMMARY_SECTION_MAX_CHARS = {
    "commands_run": 180,
    "failures": 320,
}
_SUMMARY_SECTION_DEFAULT_MAX_CHARS = 240
_FILE_PATH_RE = re.compile(
    r"[\w./\\-]+\.(?:py|ts|tsx|js|jsx|md|json|yml|yaml|toml|ini|cfg|txt|sh|ps1|sql|html|css|scss|java|go|rs|c|cpp)",
    re.IGNORECASE,
)
_FAILURE_MARKER_RE = re.compile(
    r"(traceback|exception|error|errors|failed|failure|exit code|timed out|timeout|"
    r"permission denied|access denied|not found|assertion|错误|失败|异常|超时|拒绝访问|未找到|权限)",
    re.IGNORECASE,
)
_NEXT_STEP_RE = re.compile(
    r"^(TODO|Next steps|Next:|Action:|Decide:|下一步|后续|待办|TODO:)",
    re.IGNORECASE,
)
_DECISION_RE = re.compile(
    r"^(Decision:|We should|Let's|User confirmed:|Confirmed:|决定|决定:)",
    re.IGNORECASE,
)
_CONFIRMATION_RE = re.compile(
    r"^(确认|已确认|可以|按这个|就这样|继续|同意|yes\b|yep\b|confirmed\b|go ahead\b|ship it\b)",
    re.IGNORECASE,
)
_COMMAND_INLINE_RE = re.compile(
    r"(pytest\b.*|ruff\s+check\b.*|npm\s+run\b.*|bun\s+run\b.*|pnpm(?:\s+run)?\b.*|"
    r"yarn\b.*|cargo\s+test\b.*|go\s+test\b.*|python(?:3)?\s+-m\b.*|"
    r"git\s+(?:status|diff|apply|checkout|add|commit|merge|rebase|pull|push)\b.*)",
    re.IGNORECASE,
)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_AUTH_BEARER_RE = re.compile(
    r"\b(authorization\s*:\s*bearer)\s+[A-Za-z0-9._~+/=-]+",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"\b(api[_-]?key|secret|token|password|passwd|pwd|access[_-]?key|client[_-]?secret)"
    r"(\s*[:=]\s*)([\"']?)([^\s,\"';&]+)([\"']?)",
    re.IGNORECASE,
)
_SECRET_FLAG_RE = re.compile(
    r"(\s--(?:api-key|token|password|secret|client-secret)(?:=|\s+))([^\s]+)",
    re.IGNORECASE,
)
_ENV_SECRET_RE = re.compile(
    r"\b(?:export|set|setx)\s+([A-Z0-9_]*(?:TOKEN|SECRET|KEY|PASSWORD)[A-Z0-9_]*)=([^\s]+)",
    re.IGNORECASE,
)
_HEADER_SECRET_RE = re.compile(
    r"\b(x-api-key|x-auth-token|api-key)\s*:\s*([^\s]+)",
    re.IGNORECASE,
)


def _redact_summary_text(text: str) -> str:
    """Remove sensitive values before summary metadata reaches future prompts."""
    redacted = _PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", text)
    redacted = _AUTH_BEARER_RE.sub(lambda m: f"{m.group(1)} [REDACTED]", redacted)
    redacted = _HEADER_SECRET_RE.sub(lambda m: f"{m.group(1)}: [REDACTED]", redacted)
    redacted = _SECRET_ASSIGNMENT_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]",
        redacted,
    )
    redacted = _SECRET_FLAG_RE.sub(lambda m: f"{m.group(1)}[REDACTED]", redacted)
    redacted = _ENV_SECRET_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", redacted)
    return redacted


def _push_recent_unique(values: list[str], item: str, *, limit: int = _SUMMARY_SECTION_MAX_ITEMS) -> None:
    if item in values:
        values.remove(item)
    values.append(item)
    if len(values) > limit:
        del values[0]


def _extract_file_paths(text: str) -> list[str]:
    if not text:
        return []
    paths: list[str] = []
    seen: set[str] = set()
    for match in _FILE_PATH_RE.finditer(text):
        path = match.group(0)
        if path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return paths


def _extract_command_lines(text: str) -> list[str]:
    if not isinstance(text, str) or not text.strip():
        return []
    commands: list[str] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip().strip("`")
        if not line:
            continue
        match = _COMMAND_INLINE_RE.search(line)
        if not match:
            continue
        command = match.group(1).strip()
        if command in seen:
            continue
        seen.add(command)
        commands.append(command)
    return commands


def _failure_priority(line: str) -> tuple[int, int]:
    lowered = line.lower()
    if (
        "traceback" in lowered
        and not re.search(r"(runtimeerror|valueerror|typeerror|assertion|error:|failed|失败|错误|异常)", lowered)
    ):
        return (2, len(line))
    if re.search(
        r"(exit code|failed|failure|error:|exception|runtimeerror|assertion|timeout|timed out|"
        r"permission denied|not found|失败|错误|异常|超时|未找到|权限)",
        lowered,
    ):
        return (0, len(line))
    return (1, len(line))


def _extract_failure_summary(text: str) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return None
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    failure_lines = [line for line in lines if _FAILURE_MARKER_RE.search(line)]
    if not failure_lines:
        return None
    ordered = sorted(
        enumerate(failure_lines),
        key=lambda item: (_failure_priority(item[1]), item[0]),
    )
    chosen: list[str] = []
    commands = _extract_command_lines(text)
    if commands:
        chosen.append(f"Command: {commands[-1]}")
    for _, line in ordered:
        if line not in chosen:
            chosen.append(line)
        if len(chosen) >= 3:
            break
    if failure_lines[-1] not in chosen and len(chosen) < 3:
        chosen.append(failure_lines[-1])
    summary = " | ".join(chosen[:3]).strip()
    return summary or None


def _clean_summary_item(section: str, value: str) -> str:
    cleaned = _redact_summary_text(value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(".;")
    limit = _SUMMARY_SECTION_MAX_CHARS.get(section, _SUMMARY_SECTION_DEFAULT_MAX_CHARS)
    if len(cleaned) > limit:
        cleaned = truncate_text(cleaned, limit)
    return cleaned


def _normalize_summary_section_name(raw: str) -> str | None:
    key = raw.strip().lower().replace("-", " ").replace("_", " ")
    key = re.sub(r"\s+", " ", key).strip()
    return _SUMMARY_SECTION_ALIASES.get(key)


def _match_summary_section_heading(line: str) -> tuple[str, str] | None:
    if line.startswith("#"):
        heading = line.lstrip("#").strip()
        match = re.match(r"^([A-Za-z_ ]+?)(?:\s*[:\-]\s*(.*))?$", heading)
        if not match:
            return None
        section = _normalize_summary_section_name(match.group(1))
        if section:
            return section, (match.group(2) or "").strip()
        return None
    match = re.match(r"^([A-Za-z_ ]+?)\s*[:\-]\s*(.*)$", line)
    if not match:
        return None
    section = _normalize_summary_section_name(match.group(1))
    if section:
        return section, match.group(2).strip()
    return None


def _parse_summary_sections(summary_text: str) -> dict[str, list[str]]:
    summary_text = _redact_summary_text(summary_text)
    sections: dict[str, list[str]] = {name: [] for name in _SUMMARY_SECTION_ORDER}
    current = "overview"
    seen_heading = False

    def _append(section: str, value: str) -> None:
        cleaned = _clean_summary_item(section, value)
        if cleaned:
            _push_recent_unique(sections[section], cleaned)

    for raw_line in summary_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        heading = _match_summary_section_heading(line)
        if heading:
            current, remainder = heading
            seen_heading = True
            if remainder and not re.fullmatch(r"[:\-]+", remainder):
                _append(current, remainder)
            continue
        if not seen_heading:
            _append("overview", line)
            continue
        bullet = re.match(r"^[-*+•]\s+(.*)$", line)
        numbered = re.match(r"^\d+[.)]\s+(.*)$", line)
        if bullet:
            _append(current, bullet.group(1))
        elif numbered:
            _append(current, numbered.group(1))
        else:
            _append(current, line)

    if not any(sections.values()):
        return {}
    if not sections["overview"]:
        fallback = _clean_summary_item("overview", summary_text.strip())
        if fallback:
            sections["overview"] = [fallback]
    return {name: values for name, values in sections.items() if values}


def _infer_sections_from_messages(messages: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Heuristically extract structured section items from archived messages.

    Looks for tool call traces, exec/read/write commands, file edit markers,
    and assistant decisions phrased as short lines.
    """
    inferred: dict[str, list[str]] = {name: [] for name in _SUMMARY_SECTION_ORDER}

    def _add(section: str, item: str) -> None:
        item = _clean_summary_item(section, str(item))
        if not item:
            return
        _push_recent_unique(inferred[section], item)

    for m in messages:
        # Tool call metadata convention: message may have 'tool_calls' list
        for tc in m.get("tool_calls", []) or []:
            name = tc.get("id") or tc.get("type") or tc.get("tool") or tc.get("name")
            if name:
                _add("commands_run", f"{name}")
            # inspect function / arguments for file paths
            func = tc.get("function") or tc.get("tool") or {}
            if isinstance(func, dict):
                args = func.get("arguments") or func.get("args") or ""
                try:
                    if isinstance(args, str):
                        for path in _extract_file_paths(args):
                            _add("files_touched", path)
                except Exception:
                    pass
        # File edit markers: common keys
        if m.get("file_edits"):
            for fe in m.get("file_edits"):
                path = fe.get("path") or fe.get("file") or fe.get("filename")
                if path:
                    _add("files_touched", path)
                status = fe.get("status")
                if status:
                    _add("decisions", f"file {path} {status}")
        # Exec-like assistant content often contains commands; capture simple patterns
        content = m.get("content") or ""
        if isinstance(content, str) and content:
            for path in _extract_file_paths(content):
                _add("files_touched", path)
            for cmd in _extract_command_lines(content):
                _add("commands_run", cmd)
            # common failure indicators
            failure_summary = _extract_failure_summary(content)
            if failure_summary:
                _add("failures", failure_summary)
            # decisions / next steps often start with verbs or bullets
            for line in content.splitlines():
                line = line.strip()
                if not line:
                    continue
                if re.match(r"^(TODO|Next steps|Next:|Action:|Decide:|决定:)", line, re.IGNORECASE):
                    _add("next_steps", line)
                if re.match(r"^(决定|决定:|Decision:|We should|Let's)", line, re.IGNORECASE):
                    _add("decisions", line)

    for m in messages:
        content = m.get("content") or ""
        if not isinstance(content, str) or not content:
            continue
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if m.get("role") == "user" and _CONFIRMATION_RE.match(line):
                _add("decisions", f"User confirmed: {line}")
            if _NEXT_STEP_RE.match(line):
                _add("next_steps", line)
            if _DECISION_RE.match(line):
                _add("decisions", line)

    # prune empty lists
    return {k: v for k, v in inferred.items() if v}


def _format_summary_sections(sections: dict[str, list[str]] | None) -> str:
    if not sections:
        return ""
    lines: list[str] = []
    for name in _SUMMARY_SECTION_ORDER:
        values = [value.strip() for value in (sections.get(name) or []) if value and value.strip()]
        if not values:
            continue
        lines.append(f"## {_SUMMARY_SECTION_LABELS[name]}")
        for value in values:
            lines.append(f"- {value}")
        lines.append("")
    return "\n".join(lines).strip()


def _sanitize_summary_sections(sections: dict[str, list[str]] | None) -> dict[str, list[str]]:
    if not sections:
        return {}
    sanitized: dict[str, list[str]] = {}
    for name in _SUMMARY_SECTION_ORDER:
        values: list[str] = []
        seen: set[str] = set()
        for value in sections.get(name, []) or []:
            cleaned = _clean_summary_item(name, str(value))
            if not cleaned or cleaned in seen:
                continue
            _push_recent_unique(values, cleaned)
            seen.add(cleaned)
        if values:
            sanitized[name] = values
    return sanitized


def _sanitize_summary_text(summary_text: str) -> str:
    cleaned = _redact_summary_text(summary_text)
    cleaned = "\n".join(
        truncate_text(re.sub(r"\s+", " ", line).strip(), _SUMMARY_SECTION_DEFAULT_MAX_CHARS)
        for line in cleaned.splitlines()
        if line.strip()
    )
    return truncate_text(cleaned, 1600)


def _render_summary_text(summary: Any) -> str | None:
    if summary is None or summary == "(nothing)":
        return None
    if not isinstance(summary, str):
        summary = str(summary)
    summary = summary.strip()
    if not summary:
        return None
    return _sanitize_summary_text(summary) or None


def _summary_metadata(summary: str | None, *, last_active: str) -> dict[str, Any] | None:
    rendered = _render_summary_text(summary)
    if not rendered:
        return None
    sections = _parse_summary_sections(rendered)
    meta: dict[str, Any] = {
        "text": rendered,
        "last_active": last_active,
    }
    if sections:
        meta["sections"] = sections
    return meta


def _render_summary_metadata(meta: Any) -> str | None:
    if isinstance(meta, dict):
        text = meta.get("text")
        if isinstance(text, str) and text.strip():
            return text
        sections = meta.get("sections")
        if isinstance(sections, dict):
            rendered = _format_summary_sections({
                name: [str(value) for value in values]
                for name, values in sections.items()
                if isinstance(values, list)
            })
            if rendered:
                return rendered
    elif isinstance(meta, str) and meta.strip():
        return meta
    return None


def _build_compaction_event(
    *,
    reason: str,
    source: str,
    before_messages: list[dict[str, Any]],
    after_messages: list[dict[str, Any]],
    archived_messages: list[dict[str, Any]],
    summary: str | None,
    token_source: str | None = None,
) -> dict[str, Any]:
    before_tokens = _messages_token_estimate(before_messages)
    after_tokens = _messages_token_estimate(after_messages)
    summary_text = _render_summary_text(summary) or ""
    summary_sections = _parse_summary_sections(summary_text)
    # Heuristically infer sections from archived messages and merge with LLM-produced sections.
    inferred = _infer_sections_from_messages(archived_messages or [])
    # merge lists and deduplicate while preserving order
    merged: dict[str, list[str]] = {}
    for name in _SUMMARY_SECTION_ORDER:
        vals: list[str] = []
        seen: set[str] = set()
        for src in (summary_sections.get(name, []), inferred.get(name, [])):
            for v in src:
                cleaned = _clean_summary_item(name, v)
                if cleaned and cleaned not in seen:
                    _push_recent_unique(vals, cleaned)
                    seen.add(cleaned)
        if vals:
            merged[name] = vals
    summary_sections = _sanitize_summary_sections(merged)
    if summary_sections:
        summary_text = _format_summary_sections(summary_sections)
    event: dict[str, Any] = {
        "version": 1,
        "reason": reason,
        "source": source,
        "before_message_count": len(before_messages),
        "after_message_count": len(after_messages),
        "archived_message_count": len(archived_messages),
        "kept_message_count": len(after_messages),
        "before_token_estimate": before_tokens,
        "after_token_estimate": after_tokens,
        "saved_token_estimate": max(0, before_tokens - after_tokens),
        "summary_token_estimate": estimate_message_tokens({
            "role": "system",
            "content": summary_text,
        }) if summary_text else 0,
        "summary_preview": truncate_text(summary_text, 240) if summary_text else "",
        "summary_sections": summary_sections,
        "updated_at": datetime.now().isoformat(),
    }
    if token_source:
        event["token_source"] = token_source
    return event


def _index_compaction_event(event: dict[str, Any], session: Session | None = None) -> None:
    """Index a compaction event into the module-level retriever.

    This creates a compact doc for the retriever and calls `retriever.index_compactions`.
    Swallows any exceptions to avoid breaking consolidation flow.
    """
    try:
        doc_id = None
        if session is not None:
            doc_id = f"{session.key}:{event.get('updated_at')}"
        else:
            doc_id = event.get("updated_at") or str(time.time())

        summary_full = None
        # Attempt to prefer a richer last-summary stored on the session metadata
        if session is not None:
            meta = session.metadata.get("_last_summary")
            if meta:
                summary_full = _render_summary_metadata(meta)
        if not summary_full:
            summary_full = event.get("summary_preview") or ""

        doc = {
            "id": doc_id,
            "summary_full": summary_full,
            "summary_sections": event.get("summary_sections"),
            "updated_at": event.get("updated_at"),
            "meta": {"session_key": session.key if session is not None else None},
        }
        # index incrementally (don't replace existing index)
        retriever.index_compactions([doc], replace=False)
        # attempt to persist index to disk; swallow errors to avoid breaking
        # consolidation flow. Use a small number of retries.
        try:
            idx = globals().get("_retriever_index_file")
            if idx:
                try:
                    retriever.persist_index(idx, retries=3, backoff=0.2)
                except Exception:
                    logger.exception("Failed to persist retriever index after compaction")
        except Exception:
            logger.exception("Unexpected error while persisting retriever index")
    except Exception:
        logger.exception("Failed to index compaction event into retriever")


# ---------------------------------------------------------------------------
# MemoryStore — pure file I/O layer
# ---------------------------------------------------------------------------

class MemoryStore:
    """Pure file I/O for memory files: MEMORY.md, history.jsonl, SOUL.md, USER.md."""

    _DEFAULT_MAX_HISTORY = 1000
    _LEGACY_ENTRY_START_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}[^\]]*)\]\s*")
    _LEGACY_TIMESTAMP_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]\s*")
    _LEGACY_RAW_MESSAGE_RE = re.compile(
        r"^\[\d{4}-\d{2}-\d{2}[^\]]*\]\s+[A-Z][A-Z0-9_]*(?:\s+\[tools:\s*[^\]]+\])?:"
    )

    def __init__(self, workspace: Path, max_history_entries: int = _DEFAULT_MAX_HISTORY):
        self.workspace = workspace
        self.max_history_entries = max_history_entries
        self.memory_dir = ensure_dir(workspace / "memory")
        # register a default retriever index file in the memory dir so the
        # module-level retriever can persist/load its state across restarts.
        try:
            global _retriever_index_file
            _retriever_index_file = self.memory_dir / "retriever_index.json"
            try:
                retriever.load_index(_retriever_index_file)
            except Exception:
                logger.exception("Failed to load retriever index at startup")
                # Attempt a best-effort rebuild from history.jsonl entries.
                try:
                    entries = self._read_entries()
                    docs: list[dict[str, Any]] = []
                    for e in entries:
                        doc = {
                            "id": e.get("cursor") or str(time.time()),
                            "summary_full": e.get("content") or "",
                            "updated_at": e.get("timestamp") or None,
                            "meta": {},
                        }
                        docs.append(doc)
                    if docs:
                        retriever.rebuild_index_from_docs(docs)
                        # schedule async persist of rebuilt index
                        try:
                            retriever.schedule_persist(_retriever_index_file, retries=3, backoff=0.2)
                        except Exception:
                            logger.exception("Failed to schedule persist for rebuilt retriever index")
                except Exception:
                    logger.exception("Failed to rebuild retriever index from history.jsonl")
        except Exception:
            _retriever_index_file = None
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "history.jsonl"
        self.legacy_history_file = self.memory_dir / "HISTORY.md"
        self.soul_file = workspace / "SOUL.md"
        self.user_file = workspace / "USER.md"
        self._cursor_file = self.memory_dir / ".cursor"
        self._dream_cursor_file = self.memory_dir / ".dream_cursor"
        self._corruption_logged = False  # rate-limit non-int cursor warning
        self._oversize_logged = False  # rate-limit oversized-entry warning
        self._git = GitStore(workspace, tracked_files=[
            "SOUL.md", "USER.md", "memory/MEMORY.md", "memory/.dream_cursor",
        ])
        self._maybe_migrate_legacy_history()

    @property
    def git(self) -> GitStore:
        return self._git

    # -- generic helpers -----------------------------------------------------

    @staticmethod
    def read_file(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def _maybe_migrate_legacy_history(self) -> None:
        """One-time upgrade from legacy HISTORY.md to history.jsonl.

        The migration is best-effort and prioritizes preserving as much content
        as possible over perfect parsing.
        """
        if not self.legacy_history_file.exists():
            return
        if self.history_file.exists() and self.history_file.stat().st_size > 0:
            return

        try:
            legacy_text = self.legacy_history_file.read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            logger.exception("Failed to read legacy HISTORY.md for migration")
            return

        entries = self._parse_legacy_history(legacy_text)
        try:
            if entries:
                self._write_entries(entries)
                last_cursor = entries[-1]["cursor"]
                self._cursor_file.write_text(str(last_cursor), encoding="utf-8")
                # Default to "already processed" so upgrades do not replay the
                # user's entire historical archive into Dream on first start.
                self._dream_cursor_file.write_text(str(last_cursor), encoding="utf-8")

            backup_path = self._next_legacy_backup_path()
            self.legacy_history_file.replace(backup_path)
            logger.info(
                "Migrated legacy HISTORY.md to history.jsonl ({} entries)",
                len(entries),
            )
        except Exception:
            logger.exception("Failed to migrate legacy HISTORY.md")

    def _parse_legacy_history(self, text: str) -> list[dict[str, Any]]:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            return []

        fallback_timestamp = self._legacy_fallback_timestamp()
        entries: list[dict[str, Any]] = []
        chunks = self._split_legacy_history_chunks(normalized)

        for cursor, chunk in enumerate(chunks, start=1):
            timestamp = fallback_timestamp
            content = chunk
            match = self._LEGACY_TIMESTAMP_RE.match(chunk)
            if match:
                timestamp = match.group(1)
                remainder = chunk[match.end():].lstrip()
                if remainder:
                    content = remainder

            entries.append({
                "cursor": cursor,
                "timestamp": timestamp,
                "content": content,
            })
        return entries

    def _split_legacy_history_chunks(self, text: str) -> list[str]:
        lines = text.split("\n")
        chunks: list[str] = []
        current: list[str] = []
        saw_blank_separator = False

        for line in lines:
            if saw_blank_separator and line.strip() and current:
                chunks.append("\n".join(current).strip())
                current = [line]
                saw_blank_separator = False
                continue
            if self._should_start_new_legacy_chunk(line, current):
                chunks.append("\n".join(current).strip())
                current = [line]
                saw_blank_separator = False
                continue
            current.append(line)
            saw_blank_separator = not line.strip()

        if current:
            chunks.append("\n".join(current).strip())
        return [chunk for chunk in chunks if chunk]

    def _should_start_new_legacy_chunk(self, line: str, current: list[str]) -> bool:
        if not current:
            return False
        if not self._LEGACY_ENTRY_START_RE.match(line):
            return False
        if self._is_raw_legacy_chunk(current) and self._LEGACY_RAW_MESSAGE_RE.match(line):
            return False
        return True

    def _is_raw_legacy_chunk(self, lines: list[str]) -> bool:
        first_nonempty = next((line for line in lines if line.strip()), "")
        match = self._LEGACY_TIMESTAMP_RE.match(first_nonempty)
        if not match:
            return False
        return first_nonempty[match.end():].lstrip().startswith("[RAW]")

    def _legacy_fallback_timestamp(self) -> str:
        try:
            return datetime.fromtimestamp(
                self.legacy_history_file.stat().st_mtime,
            ).strftime("%Y-%m-%d %H:%M")
        except OSError:
            return datetime.now().strftime("%Y-%m-%d %H:%M")

    def _next_legacy_backup_path(self) -> Path:
        candidate = self.memory_dir / "HISTORY.md.bak"
        suffix = 2
        while candidate.exists():
            candidate = self.memory_dir / f"HISTORY.md.bak.{suffix}"
            suffix += 1
        return candidate

    # -- MEMORY.md (long-term facts) -----------------------------------------

    def read_memory(self) -> str:
        return self.read_file(self.memory_file)

    def write_memory(self, content: str) -> None:
        self.memory_file.write_text(content, encoding="utf-8")

    # -- SOUL.md -------------------------------------------------------------

    def read_soul(self) -> str:
        return self.read_file(self.soul_file)

    def write_soul(self, content: str) -> None:
        self.soul_file.write_text(content, encoding="utf-8")

    # -- USER.md -------------------------------------------------------------

    def read_user(self) -> str:
        return self.read_file(self.user_file)

    def write_user(self, content: str) -> None:
        self.user_file.write_text(content, encoding="utf-8")

    # -- context injection (used by context.py) ------------------------------

    def get_memory_context(self) -> str:
        long_term = self.read_memory()
        return f"## Long-term Memory\n{long_term}" if long_term else ""

    @staticmethod
    def _memory_source_stats(path: Path, *, included: bool) -> dict[str, Any]:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
            mtime = None
        except OSError:
            text = ""
            mtime = None
        else:
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime).isoformat()
            except OSError:
                mtime = None
        return {
            "included": included and bool(text.strip()),
            "exists": bool(text.strip()),
            "char_count": len(text),
            "token_estimate": estimate_message_tokens({"role": "system", "content": text}) if text else 0,
            "updated_at": mtime,
        }

    @staticmethod
    def _empty_retrieved_memory_snapshot() -> dict[str, Any]:
        return {
            "included": False,
            "entry_count": 0,
            "categories": {},
            "reasons": [],
            "items": [],
        }

    @staticmethod
    def _safe_snapshot_label(value: Any, *, limit: int = 96) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        return truncate_text(re.sub(r"\s+", " ", text), limit)

    def _retrieved_memory_snapshot(self, summary: str) -> dict[str, Any]:
        """Return retriever hit metadata without exposing retrieved text."""
        if not summary.strip():
            return self._empty_retrieved_memory_snapshot()
        try:
            hits = retriever.query(summary, top_k=3)
        except Exception:
            logger.exception("Failed to build retrieved memory snapshot")
            return self._empty_retrieved_memory_snapshot()

        items: list[dict[str, Any]] = []
        categories: dict[str, int] = {}
        reasons: list[str] = []
        for hit in hits:
            meta = hit.get("meta") if isinstance(hit.get("meta"), dict) else {}
            category = (
                self._safe_snapshot_label(hit.get("category") or meta.get("category"), limit=48)
                or "project_fact"
            )
            reason = (
                self._safe_snapshot_label(
                    hit.get("match_reason") or meta.get("match_reason"),
                    limit=80,
                )
                or "unknown"
            )
            categories[category] = categories.get(category, 0) + 1
            if reason not in reasons:
                reasons.append(reason)

            item: dict[str, Any] = {
                "id": self._safe_snapshot_label(hit.get("id"), limit=96),
                "source": self._safe_snapshot_label(
                    meta.get("session_key") or meta.get("source") or hit.get("id"),
                    limit=96,
                ),
                "category": category,
                "reason": reason,
            }
            safety = self._safe_snapshot_label(meta.get("safety"), limit=48)
            updated_at = self._safe_snapshot_label(hit.get("updated_at"), limit=64)
            if safety:
                item["safety"] = safety
            if updated_at:
                item["updated_at"] = updated_at
            items.append({key: value for key, value in item.items() if value is not None})

        return {
            "included": bool(items),
            "entry_count": len(items),
            "categories": categories,
            "reasons": reasons[:5],
            "items": items,
        }

    def build_context_memory_snapshot(
        self,
        *,
        include_memory: bool,
        include_soul: bool,
        include_user: bool,
        recent_history_count: int,
        session_summary: str | None = None,
    ) -> dict[str, Any]:
        """Return safe observability metadata for memory injected into context.

        The snapshot intentionally reports source names, sizes, timestamps and
        inclusion flags only. It does not expose the raw memory/profile text.
        """
        summary = session_summary or ""
        return {
            "version": 1,
            "sources": {
                "memory": self._memory_source_stats(self.memory_file, included=include_memory),
                "soul": self._memory_source_stats(self.soul_file, included=include_soul),
                "user": self._memory_source_stats(self.user_file, included=include_user),
                "recent_history": {
                    "included": recent_history_count > 0,
                    "entry_count": max(0, recent_history_count),
                },
                "session_summary": {
                    "included": bool(summary.strip()),
                    "char_count": len(summary),
                    "token_estimate": estimate_message_tokens({
                        "role": "system",
                        "content": summary,
                    }) if summary else 0,
                },
            },
            "retrieved": self._retrieved_memory_snapshot(summary),
            "updated_at": datetime.now().isoformat(),
        }

    # -- history.jsonl — append-only, JSONL format ---------------------------

    def append_history(self, entry: str, *, max_chars: int | None = None) -> int:
        """Append *entry* to history.jsonl and return its auto-incrementing cursor.

        Entries are passed through `strip_think` to drop template-level leaks
        (e.g. unclosed `<think` prefixes, `<channel|>` markers) before being
        persisted. If the cleaned content is empty but the raw entry wasn't,
        the record is persisted with an empty string rather than falling back
        to the raw leak — otherwise `strip_think`'s guarantees would be
        undone by history replay / consolidation downstream.

        A defensive cap (*max_chars*, default ``_HISTORY_ENTRY_HARD_CAP``) is
        applied as a final safety net: individual callers should cap their own
        content more tightly; this default only exists to catch unintentional
        large writes (e.g. an LLM echoing its input back as a "summary").
        """
        limit = max_chars if max_chars is not None else _HISTORY_ENTRY_HARD_CAP
        cursor = self._next_cursor()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        raw = entry.rstrip()
        if len(raw) > limit:
            if not self._oversize_logged:
                self._oversize_logged = True
                logger.warning(
                    "history entry exceeds {} chars ({}); truncating. "
                    "Usually means a caller forgot its own cap; "
                    "further occurrences suppressed.",
                    limit, len(raw),
                )
            raw = truncate_text(raw, limit)
        content = strip_think(raw)
        if raw and not content:
            logger.debug(
                "history entry {} stripped to empty (likely template leak); "
                "persisting empty content to avoid re-polluting context",
                cursor,
            )
        record = {"cursor": cursor, "timestamp": ts, "content": content}
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._cursor_file.write_text(str(cursor), encoding="utf-8")
        return cursor

    @staticmethod
    def _valid_cursor(value: Any) -> int | None:
        """Int cursors only — reject bool (``isinstance(True, int)`` is True)."""
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    def _iter_valid_entries(self) -> Iterator[tuple[dict[str, Any], int]]:
        """Yield ``(entry, cursor)`` for entries with int cursors; warn once on corruption."""
        poisoned: Any = None
        for entry in self._read_entries():
            raw = entry.get("cursor")
            if raw is None:
                continue
            cursor = self._valid_cursor(raw)
            if cursor is None:
                poisoned = raw
                continue
            yield entry, cursor
        if poisoned is not None and not self._corruption_logged:
            self._corruption_logged = True
            logger.warning(
                "history.jsonl contains a non-int cursor ({!r}); dropping it. "
                "Usually caused by an external writer; further occurrences suppressed.",
                poisoned,
            )

    def _next_cursor(self) -> int:
        """Read the current cursor counter and return the next value."""
        if self._cursor_file.exists():
            with suppress(ValueError, OSError):
                return int(self._cursor_file.read_text(encoding="utf-8").strip()) + 1
        # Fast path: trust the tail when intact.  Otherwise scan the whole
        # file and take ``max`` — that stays correct even if the monotonic
        # invariant was broken by external writes.
        last = self._read_last_entry() or {}
        cursor = self._valid_cursor(last.get("cursor"))
        if cursor is not None:
            return cursor + 1
        return max((c for _, c in self._iter_valid_entries()), default=0) + 1

    def read_unprocessed_history(self, since_cursor: int) -> list[dict[str, Any]]:
        """Return history entries with a valid cursor > *since_cursor*."""
        return [e for e, c in self._iter_valid_entries() if c > since_cursor]

    def compact_history(self) -> None:
        """Drop oldest entries if the file exceeds *max_history_entries*."""
        if self.max_history_entries <= 0:
            return
        entries = self._read_entries()
        if len(entries) <= self.max_history_entries:
            return
        kept = entries[-self.max_history_entries:]
        self._write_entries(kept)

    # -- JSONL helpers -------------------------------------------------------

    def _read_entries(self) -> list[dict[str, Any]]:
        """Read all entries from history.jsonl."""
        entries: list[dict[str, Any]] = []
        with suppress(FileNotFoundError):
            with open(self.history_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue

        return entries

    def _read_last_entry(self) -> dict[str, Any] | None:
        """Read the last entry from the JSONL file efficiently."""
        try:
            with open(self.history_file, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                if size == 0:
                    return None
                read_size = min(size, 4096)
                f.seek(size - read_size)
                data = f.read().decode("utf-8")
                lines = [line for line in data.split("\n") if line.strip()]
                if not lines:
                    return None
                return json.loads(lines[-1])
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _write_entries(self, entries: list[dict[str, Any]]) -> None:
        """Overwrite history.jsonl with the given entries (atomic write)."""
        tmp_path = self.history_file.with_suffix(self.history_file.suffix + ".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                for entry in entries:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.history_file)

            # fsync the directory so the rename is durable.
            # On Windows, opening a directory with O_RDONLY raises
            # PermissionError — skip the dir sync there (NTFS
            # journals metadata synchronously).
            with suppress(PermissionError):
                fd = os.open(str(self.history_file.parent), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    # -- dream cursor --------------------------------------------------------

    def get_last_dream_cursor(self) -> int:
        if self._dream_cursor_file.exists():
            with suppress(ValueError, OSError):
                return int(self._dream_cursor_file.read_text(encoding="utf-8").strip())
        return 0

    def set_last_dream_cursor(self, cursor: int) -> None:
        self._dream_cursor_file.write_text(str(cursor), encoding="utf-8")

    # -- message formatting utility ------------------------------------------

    @staticmethod
    def _format_messages(messages: list[dict]) -> str:
        lines = []
        for message in messages:
            if not message.get("content"):
                continue
            tools = f" [tools: {', '.join(message['tools_used'])}]" if message.get("tools_used") else ""
            lines.append(
                f"[{message.get('timestamp', '?')[:16]}] {message['role'].upper()}{tools}: {message['content']}"
            )
        return "\n".join(lines)

    def raw_archive(self, messages: list[dict], *, max_chars: int | None = None) -> None:
        """Fallback: dump raw messages to history.jsonl without LLM summarization."""
        limit = max_chars if max_chars is not None else _RAW_ARCHIVE_MAX_CHARS
        formatted = truncate_text(self._format_messages(messages), limit)
        self.append_history(
            f"[RAW] {len(messages)} messages\n"
            f"{formatted}"
        )
        logger.warning(
            "Memory consolidation degraded: raw-archived {} messages", len(messages)
        )



# ---------------------------------------------------------------------------
# Consolidator — lightweight token-budget triggered consolidation
# ---------------------------------------------------------------------------


# Individual history.jsonl writers cap their own payloads tightly; the
# _HISTORY_ENTRY_HARD_CAP at append_history() is a belt-and-suspenders default
# that catches any new caller that forgot to set its own cap.
_RAW_ARCHIVE_MAX_CHARS = 16_000       # fallback dump (LLM failed)
_ARCHIVE_SUMMARY_MAX_CHARS = 8_000    # LLM-produced consolidation summary
_HISTORY_ENTRY_HARD_CAP = 64_000      # emergency cap in append_history


class Consolidator:
    """Lightweight consolidation: summarizes evicted messages into history.jsonl."""

    _MAX_CONSOLIDATION_ROUNDS = 5

    _SAFETY_BUFFER = 1024  # extra headroom for tokenizer estimation drift

    def __init__(
        self,
        store: MemoryStore,
        provider: LLMProvider,
        model: str,
        sessions: SessionManager,
        context_window_tokens: int,
        build_messages: Callable[..., list[dict[str, Any]]],
        get_tool_definitions: Callable[[], list[dict[str, Any]]],
        max_completion_tokens: int = 4096,
        consolidation_ratio: float = 0.5,
    ):
        self.store = store
        self.provider = provider
        self.model = model
        self.sessions = sessions
        self.context_window_tokens = context_window_tokens
        self.max_completion_tokens = max_completion_tokens
        self.consolidation_ratio = consolidation_ratio
        self._build_messages = build_messages
        self._get_tool_definitions = get_tool_definitions
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    def set_provider(
        self,
        provider: LLMProvider,
        model: str,
        context_window_tokens: int,
    ) -> None:
        self.provider = provider
        self.model = model
        self.context_window_tokens = context_window_tokens
        self.max_completion_tokens = provider.generation.max_tokens

    def get_lock(self, session_key: str) -> asyncio.Lock:
        """Return the shared consolidation lock for one session."""
        return self._locks.setdefault(session_key, asyncio.Lock())

    def pick_consolidation_boundary(
        self,
        session: Session,
        tokens_to_remove: int,
    ) -> tuple[int, int] | None:
        """Pick a user-turn boundary that removes enough old prompt tokens."""
        start = session.last_consolidated
        if start >= len(session.messages) or tokens_to_remove <= 0:
            return None

        removed_tokens = 0
        last_boundary: tuple[int, int] | None = None
        for idx in range(start, len(session.messages)):
            message = session.messages[idx]
            if idx > start and message.get("role") == "user":
                last_boundary = (idx, removed_tokens)
                if removed_tokens >= tokens_to_remove:
                    return last_boundary
            removed_tokens += estimate_message_tokens(message)

        return last_boundary

    @staticmethod
    def _full_unconsolidated_history(
        session: Session,
        *,
        include_timestamps: bool = False,
    ) -> list[dict[str, Any]]:
        """Return the whole unconsolidated tail for consolidation decisions."""
        unconsolidated_count = len(session.messages) - session.last_consolidated
        if unconsolidated_count <= 0:
            return []
        return session.get_history(
            max_messages=unconsolidated_count,
            include_timestamps=include_timestamps,
        )

    @staticmethod
    def _replay_overflow_boundary(
        session: Session,
        replay_max_messages: int | None,
    ) -> int | None:
        if not replay_max_messages or replay_max_messages <= 0:
            return None
        tail = list(enumerate(session.messages[session.last_consolidated:], session.last_consolidated))
        if len(tail) <= replay_max_messages:
            return None

        sliced = tail[-replay_max_messages:]
        for i, (_idx, message) in enumerate(sliced):
            if message.get("role") == "user":
                start = i
                if i > 0 and sliced[i - 1][1].get("_channel_delivery"):
                    start = i - 1
                sliced = sliced[start:]
                break

        legal_start = find_legal_message_start([message for _idx, message in sliced])
        if legal_start:
            sliced = sliced[legal_start:]
        if not sliced:
            return len(session.messages)

        first_visible_idx = sliced[0][0]
        if first_visible_idx <= session.last_consolidated:
            return None
        return first_visible_idx

    async def _consolidate_replay_overflow(
        self,
        session: Session,
        replay_max_messages: int | None,
    ) -> tuple[str | None, dict[str, Any] | None]:
        """Archive messages that would be hidden by the replay message window."""
        end_idx = self._replay_overflow_boundary(session, replay_max_messages)
        if end_idx is None:
            return None, None
        chunk = session.messages[session.last_consolidated:end_idx]
        if not chunk:
            return None, None
        before_messages = list(session.messages[session.last_consolidated:])
        logger.info(
            "Replay-window consolidation for {}: chunk={} msgs, replay_max={}",
            session.key,
            len(chunk),
            replay_max_messages,
        )
        summary = await self.archive(chunk)
        session.last_consolidated = end_idx
        self.sessions.save(session)
        after_messages = list(session.messages[session.last_consolidated:])
        event = _build_compaction_event(
            reason="replay_window",
            source="token_consolidator",
            before_messages=before_messages,
            after_messages=after_messages,
            archived_messages=chunk,
            summary=summary,
        )
        return summary, event

    def _persist_last_summary(self, session: Session, summary: str | None) -> None:
        meta = _summary_metadata(summary, last_active=session.updated_at.isoformat())
        if meta is not None:
            session.metadata["_last_summary"] = meta
            self.sessions.save(session)

    def estimate_session_prompt_tokens(
        self,
        session: Session,
    ) -> tuple[int, str]:
        """Estimate prompt size from the full unconsolidated session tail."""
        history = self._full_unconsolidated_history(session, include_timestamps=True)
        channel, chat_id = (session.key.split(":", 1) if ":" in session.key else (None, None))
        # Include archived summary in estimation so the budget accounts for it.
        meta = session.metadata.get("_last_summary")
        summary = _render_summary_metadata(meta)
        probe_messages = self._build_messages(
            history=history,
            current_message="[token-probe]",
            channel=channel,
            chat_id=chat_id,
            sender_id=None,
            session_summary=summary,
            session_metadata=session.metadata,
        )
        return estimate_prompt_tokens_chain(
            self.provider,
            self.model,
            probe_messages,
            self._get_tool_definitions(),
        )

    @property
    def _input_token_budget(self) -> int:
        """Available input token budget for consolidation LLM."""
        return self.context_window_tokens - self.max_completion_tokens - self._SAFETY_BUFFER

    def _truncate_to_token_budget(self, text: str) -> str:
        """Truncate text so it fits within the consolidation LLM's token budget."""
        budget = self._input_token_budget
        if budget <= 0:
            return truncate_text(text, _RAW_ARCHIVE_MAX_CHARS)
        try:
            enc = tiktoken.get_encoding("cl100k_base")
            tokens = enc.encode(text)
            if len(tokens) <= budget:
                return text
            return enc.decode(tokens[:budget]) + "\n... (truncated)"
        except Exception:
            return truncate_text(text, budget * 4)

    async def archive(self, messages: list[dict]) -> str | None:
        """Summarize messages via LLM and append to history.jsonl.

        Returns the summary text on success, None if nothing to archive.
        """
        if not messages:
            return None
        try:
            formatted = MemoryStore._format_messages(messages)
            formatted = self._truncate_to_token_budget(formatted)
            response = await self.provider.chat_with_retry(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": render_template(
                            "agent/consolidator_archive.md",
                            strip=True,
                        ),
                    },
                    {"role": "user", "content": formatted},
                ],
                tools=None,
                tool_choice=None,
            )
            if response.finish_reason == "error":
                raise RuntimeError(f"LLM returned error: {response.content}")
            if response.content == "(nothing)":
                summary = "(nothing)"
            else:
                summary = _render_summary_text(response.content or "[no summary]") or "[no summary]"
            self.store.append_history(summary, max_chars=_ARCHIVE_SUMMARY_MAX_CHARS)
            return summary
        except Exception:
            logger.warning("Consolidation LLM call failed, raw-dumping to history")
            self.store.raw_archive(messages)
            return None

    async def maybe_consolidate_by_tokens(
        self,
        session: Session,
        *,
        replay_max_messages: int | None = None,
    ) -> list[dict[str, Any]]:
        """Loop: archive old messages until prompt fits within safe budget.

        The budget reserves space for completion tokens and a safety buffer
        so the LLM request never exceeds the context window.
        """
        events: list[dict[str, Any]] = []
        if self.context_window_tokens <= 0:
            return events

        lock = self.get_lock(session.key)
        async with lock:
            # Refresh session reference: AutoCompact may have replaced it.
            fresh = self.sessions.get_or_create(session.key)
            if fresh is not session:
                session = fresh
            if not session.messages:
                return events

            budget = self._input_token_budget
            target = int(budget * self.consolidation_ratio)
            last_summary, replay_event = await self._consolidate_replay_overflow(
                session,
                replay_max_messages,
            )
            if replay_event:
                events.append(replay_event)
                # index replay compaction for retrieval
                _index_compaction_event(replay_event, session)
            try:
                estimated, source = self.estimate_session_prompt_tokens(
                    session,
                )
            except Exception:
                logger.exception("Token estimation failed for {}", session.key)
                estimated, source = 0, "error"
            if estimated <= 0:
                self._persist_last_summary(session, last_summary)
                return events
            if estimated < budget:
                unconsolidated_count = len(session.messages) - session.last_consolidated
                logger.debug(
                    "Token consolidation idle {}: {}/{} via {}, msgs={}",
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    source,
                    unconsolidated_count,
                )
                self._persist_last_summary(session, last_summary)
                return events

            for round_num in range(self._MAX_CONSOLIDATION_ROUNDS):
                if estimated <= target:
                    break

                boundary = self.pick_consolidation_boundary(session, max(1, estimated - target))
                if boundary is None:
                    logger.debug(
                        "Token consolidation: no safe boundary for {} (round {})",
                        session.key,
                        round_num,
                    )
                    break

                end_idx = boundary[0]

                chunk = session.messages[session.last_consolidated:end_idx]
                if not chunk:
                    break
                before_messages = list(session.messages[session.last_consolidated:])

                logger.info(
                    "Token consolidation round {} for {}: {}/{} via {}, chunk={} msgs",
                    round_num,
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    source,
                    len(chunk),
                )
                summary = await self.archive(chunk)
                # Advance the cursor either way: on success the chunk was
                # summarized; on failure archive() already raw-archived it as
                # a breadcrumb. Re-archiving the same chunk on the next call
                # would just emit duplicate [RAW] entries.
                if summary:
                    last_summary = summary
                session.last_consolidated = end_idx
                self.sessions.save(session)
                after_messages = list(session.messages[session.last_consolidated:])
                events.append(_build_compaction_event(
                    reason="token_budget",
                    source="token_consolidator",
                    before_messages=before_messages,
                    after_messages=after_messages,
                    archived_messages=chunk,
                    summary=summary,
                    token_source=source,
                ))
                # index the newly created compaction event
                _index_compaction_event(events[-1], session)
                if not summary:
                    # LLM is degraded — stop hammering it this call;
                    # the next invocation can retry a fresh chunk.
                    break

                try:
                    estimated, source = self.estimate_session_prompt_tokens(
                        session,
                    )
                except Exception:
                    logger.exception("Token estimation failed for {}", session.key)
                    estimated, source = 0, "error"
                if estimated <= 0:
                    break

            # Persist the last summary to session metadata so it can be injected
            # into the runtime context on the next prepare_session() call, aligning
            # the summary injection strategy with AutoCompact._archive().
            self._persist_last_summary(session, last_summary)
            return events

    async def compact_idle_session(
        self,
        session_key: str,
        max_suffix: int = 8,
    ) -> str | None:
        """Hard-truncate an idle session under the consolidation lock.

        Used by AutoCompact so all session mutation goes through a single
        lock-protected path.  Returns the summary text on success, ``None``
        if the LLM failed (raw_archive fallback), or ``""`` if there was
        nothing to archive.
        """
        lock = self.get_lock(session_key)
        async with lock:
            self.sessions.invalidate(session_key)
            session = self.sessions.get_or_create(session_key)

            tail = list(session.messages[session.last_consolidated:])
            if not tail:
                session.updated_at = datetime.now()
                self.sessions.save(session)
                return ""

            probe = Session(
                key=session.key,
                messages=tail.copy(),
                created_at=session.created_at,
                updated_at=session.updated_at,
                metadata={},
                last_consolidated=0,
            )
            probe.retain_recent_legal_suffix(max_suffix)
            kept = probe.messages
            cut = len(tail) - len(kept)
            archive_msgs = tail[:cut]

            if not archive_msgs and not kept:
                session.updated_at = datetime.now()
                self.sessions.save(session)
                return ""

            before_messages = list(tail)
            last_active = session.updated_at
            summary: str | None = ""
            if archive_msgs:
                summary = await self.archive(archive_msgs)

            if summary and summary != "(nothing)":
                meta = _summary_metadata(summary, last_active=last_active.isoformat())
                if meta is not None:
                    session.metadata["_last_summary"] = meta
            if archive_msgs:
                session.metadata["_last_compaction"] = _build_compaction_event(
                    reason="idle_ttl",
                    source="auto_compact",
                    before_messages=before_messages,
                    after_messages=kept,
                    archived_messages=archive_msgs,
                    summary=summary,
                )
                # index the idle compaction event
                _index_compaction_event(session.metadata.get("_last_compaction"), session)

            session.messages = kept
            session.last_consolidated = 0
            session.updated_at = datetime.now()
            self.sessions.save(session)

            if archive_msgs:
                logger.info(
                    "Idle-session compact for {}: archived={}, kept={}, summary={}",
                    session_key,
                    len(archive_msgs),
                    len(kept),
                    bool(summary),
                )

            return summary


# ---------------------------------------------------------------------------
# Dream — heavyweight cron-scheduled memory consolidation
# ---------------------------------------------------------------------------


# Single source of truth for the staleness threshold used in _annotate_with_ages
# *and* in the Phase 1 prompt template (passed as `stale_threshold_days`).
# Keep code and prompt aligned — if you bump this, the LLM's instruction string
# updates automatically.
_STALE_THRESHOLD_DAYS = 14


class Dream:
    """Two-phase memory processor: analyze history.jsonl, then edit files via AgentRunner.

    Phase 1 produces an analysis summary (plain LLM call).
    Phase 2 delegates to AgentRunner with read_file / edit_file tools so the
    LLM can make targeted, incremental edits instead of replacing entire files.
    """

    # Caps on prompt-bound inputs so Dream's LLM calls never exceed the model's
    # context window just because a file (or a legacy large history entry) grew
    # unexpectedly. Each file still appears in full via read_file when the agent
    # needs it in Phase 2 — these caps only bound the Phase 1/2 prompt preview.
    _MEMORY_FILE_MAX_CHARS = 32_000
    _SOUL_FILE_MAX_CHARS = 16_000
    _USER_FILE_MAX_CHARS = 16_000
    _HISTORY_ENTRY_PREVIEW_MAX_CHARS = 4_000

    def __init__(
        self,
        store: MemoryStore,
        provider: LLMProvider,
        model: str,
        max_batch_size: int = 20,
        max_iterations: int = 10,
        max_tool_result_chars: int = 16_000,
        annotate_line_ages: bool = True,
    ):
        self.store = store
        self.provider = provider
        self.model = model
        self.max_batch_size = max_batch_size
        self.max_iterations = max_iterations
        self.max_tool_result_chars = max_tool_result_chars
        # Kill switch for the git-blame-based per-line age annotation in Phase 1.
        # Default True keeps the #3212 behavior; set False to feed MEMORY.md raw
        # (e.g. if a specific LLM reacts poorly to the `← Nd` suffix).
        self.annotate_line_ages = annotate_line_ages
        self._runner = AgentRunner(provider)
        self._tools = self._build_tools()

    def set_provider(self, provider: LLMProvider, model: str) -> None:
        self.provider = provider
        self.model = model
        self._runner.provider = provider

    # -- tool registry -------------------------------------------------------

    def _build_tools(self) -> ToolRegistry:
        """Build a minimal tool registry for the Dream agent."""
        from nanobot.agent.skills import BUILTIN_SKILLS_DIR
        from nanobot.agent.tools.file_state import FileStates
        from nanobot.agent.tools.filesystem import EditFileTool, ReadFileTool, WriteFileTool

        tools = ToolRegistry()
        workspace = self.store.workspace
        # Allow reading builtin skills for reference during skill creation
        extra_read = [BUILTIN_SKILLS_DIR] if BUILTIN_SKILLS_DIR.exists() else None
        # Dream gets its own FileStates so its caches stay isolated from the
        # main loop's sessions (issue #3571).
        file_states = FileStates()
        tools.register(ReadFileTool(
            workspace=workspace,
            allowed_dir=workspace,
            extra_allowed_dirs=extra_read,
            file_states=file_states,
        ))
        tools.register(EditFileTool(workspace=workspace, allowed_dir=workspace, file_states=file_states))
        # write_file resolves relative paths from workspace root, but can only
        # write under skills/ so the prompt can safely use skills/<name>/SKILL.md.
        skills_dir = workspace / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        tools.register(WriteFileTool(workspace=workspace, allowed_dir=skills_dir, file_states=file_states))
        return tools

    # -- skill listing --------------------------------------------------------

    def _list_existing_skills(self) -> list[str]:
        """List existing skills as 'name — description' for dedup context."""
        import re as _re

        from nanobot.agent.skills import BUILTIN_SKILLS_DIR

        desc_re = _re.compile(r"^description:\s*(.+)$", _re.MULTILINE | _re.IGNORECASE)
        entries: dict[str, str] = {}
        for base in (self.store.workspace / "skills", BUILTIN_SKILLS_DIR):
            if not base.exists():
                continue
            for d in base.iterdir():
                if not d.is_dir():
                    continue
                skill_md = d / "SKILL.md"
                if not skill_md.exists():
                    continue
                # Prefer workspace skills over builtin (same name)
                if d.name in entries and base == BUILTIN_SKILLS_DIR:
                    continue
                content = skill_md.read_text(encoding="utf-8")[:500]
                m = desc_re.search(content)
                desc = m.group(1).strip() if m else "(no description)"
                entries[d.name] = desc
        return [f"{name} — {desc}" for name, desc in sorted(entries.items())]

    # -- main entry ----------------------------------------------------------

    def _annotate_with_ages(self, content: str) -> str:
        """Append per-line age suffixes to MEMORY.md content.

        Each non-blank line whose age exceeds ``_STALE_THRESHOLD_DAYS`` gets a
        suffix like ``← 30d`` indicating days since last modification.
        Returns the original content unchanged if git is unavailable,
        annotate fails, or the line count doesn't match the age count
        (which can happen with an uncommitted working-tree edit — better to
        skip annotation than to tag the wrong line).
        SOUL.md and USER.md are never annotated.
        """
        file_path = "memory/MEMORY.md"
        try:
            ages = self.store.git.line_ages(file_path)
        except Exception:
            logger.debug("line_ages failed for {}", file_path)
            return content
        if not ages:
            return content

        had_trailing = content.endswith("\n")
        lines = content.splitlines()
        # If HEAD-blob line count disagrees with the working-tree content we
        # received, ages would be assigned to the wrong lines — skip entirely
        # and feed the LLM un-annotated content rather than misleading data.
        if len(lines) != len(ages):
            logger.debug(
                "line_ages length mismatch for {} (lines={}, ages={}); skipping annotation",
                file_path, len(lines), len(ages),
            )
            return content

        annotated: list[str] = []
        for line, age in zip(lines, ages):
            if not line.strip():
                annotated.append(line)
                continue
            if age.age_days > _STALE_THRESHOLD_DAYS:
                annotated.append(f"{line}  \u2190 {age.age_days}d")
            else:
                annotated.append(line)
        result = "\n".join(annotated)
        if had_trailing:
            result += "\n"
        return result

    async def run(self) -> bool:
        """Process unprocessed history entries. Returns True if work was done."""
        from nanobot.agent.skills import BUILTIN_SKILLS_DIR

        last_cursor = self.store.get_last_dream_cursor()
        entries = self.store.read_unprocessed_history(since_cursor=last_cursor)
        if not entries:
            return False

        batch = entries[: self.max_batch_size]
        logger.info(
            "Dream: processing {} entries (cursor {}→{}), batch={}",
            len(entries), last_cursor, batch[-1]["cursor"], len(batch),
        )

        # Build history text for LLM — cap each entry so a legacy oversized
        # record (e.g. pre-#3412 raw_archive dump) can't blow up the prompt.
        history_text = "\n".join(
            f"[{e['timestamp']}] "
            f"{truncate_text(e['content'], self._HISTORY_ENTRY_PREVIEW_MAX_CHARS)}"
            for e in batch
        )

        # Current file contents + per-line age annotations (MEMORY.md only).
        # Each file is capped in the *prompt preview* only; Phase 2 still sees
        # the full file via the read_file tool.
        current_date = datetime.now().strftime("%Y-%m-%d")
        raw_memory = self.store.read_memory() or "(empty)"
        annotated_memory = (
            self._annotate_with_ages(raw_memory)
            if self.annotate_line_ages
            else raw_memory
        )
        current_memory = truncate_text(annotated_memory, self._MEMORY_FILE_MAX_CHARS)
        current_soul = truncate_text(
            self.store.read_soul() or "(empty)", self._SOUL_FILE_MAX_CHARS,
        )
        current_user = truncate_text(
            self.store.read_user() or "(empty)", self._USER_FILE_MAX_CHARS,
        )

        file_context = (
            f"## Current Date\n{current_date}\n\n"
            f"## Current MEMORY.md ({len(current_memory)} chars)\n{current_memory}\n\n"
            f"## Current SOUL.md ({len(current_soul)} chars)\n{current_soul}\n\n"
            f"## Current USER.md ({len(current_user)} chars)\n{current_user}"
        )

        # Phase 1: Analyze (no skills list — dedup is Phase 2's job)
        phase1_prompt = (
            f"## Conversation History\n{history_text}\n\n{file_context}"
        )

        try:
            phase1_response = await self.provider.chat_with_retry(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": render_template(
                            "agent/dream_phase1.md",
                            strip=True,
                            stale_threshold_days=_STALE_THRESHOLD_DAYS,
                        ),
                    },
                    {"role": "user", "content": phase1_prompt},
                ],
                tools=None,
                tool_choice=None,
            )
            analysis = phase1_response.content or ""
            logger.debug("Dream Phase 1 analysis ({} chars): {}", len(analysis), analysis[:500])
        except Exception:
            logger.exception("Dream Phase 1 failed")
            return False

        # Phase 2: Delegate to AgentRunner with read_file / edit_file
        existing_skills = self._list_existing_skills()
        skills_section = ""
        if existing_skills:
            skills_section = (
                "\n\n## Existing Skills\n"
                + "\n".join(f"- {s}" for s in existing_skills)
            )
        phase2_prompt = f"## Analysis Result\n{analysis}\n\n{file_context}{skills_section}"

        tools = self._tools
        skill_creator_path = BUILTIN_SKILLS_DIR / "skill-creator" / "SKILL.md"
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": render_template(
                    "agent/dream_phase2.md",
                    strip=True,
                    skill_creator_path=str(skill_creator_path),
                ),
            },
            {"role": "user", "content": phase2_prompt},
        ]

        try:
            result = await self._runner.run(AgentRunSpec(
                initial_messages=messages,
                tools=tools,
                model=self.model,
                max_iterations=self.max_iterations,
                max_tool_result_chars=self.max_tool_result_chars,
                fail_on_tool_error=False,
            ))
            logger.debug(
                "Dream Phase 2 complete: stop_reason={}, tool_events={}",
                result.stop_reason, len(result.tool_events),
            )
            for ev in (result.tool_events or []):
                logger.info("Dream tool_event: name={}, status={}, detail={}", ev.get("name"), ev.get("status"), ev.get("detail", "")[:200])
        except Exception:
            logger.exception("Dream Phase 2 failed")
            result = None

        # Build changelog from tool events
        changelog: list[str] = []
        if result and result.tool_events:
            for event in result.tool_events:
                if event["status"] == "ok":
                    changelog.append(f"{event['name']}: {event['detail']}")

        # Only advance cursor on successful completion to prevent silent loss
        if result and result.stop_reason == "completed":
            new_cursor = batch[-1]["cursor"]
            self.store.set_last_dream_cursor(new_cursor)
            logger.info(
                "Dream done: {} change(s), cursor advanced to {}",
                len(changelog), new_cursor,
            )
        else:
            reason = result.stop_reason if result else "exception"
            logger.warning(
                "Dream incomplete ({}): cursor NOT advanced, will retry next cron cycle",
                reason,
            )

        self.store.compact_history()

        # Git auto-commit (only when there are actual changes)
        if changelog and self.store.git.is_initialized():
            ts = batch[-1]["timestamp"]
            summary = f"dream: {ts}, {len(changelog)} change(s)"
            commit_msg = f"{summary}\n\n{analysis.strip()}"
            sha = self.store.git.auto_commit(commit_msg)
            if sha:
                logger.info("Dream commit: {}", sha)

        return True
