"""Auto compact: proactive compression of idle sessions to reduce token cost and latency."""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Coroutine

from loguru import logger

from nanobot.session.manager import Session, SessionManager

if TYPE_CHECKING:
    from nanobot.agent.memory import Consolidator


class AutoCompact:
    _RECENT_SUFFIX_MESSAGES = 8

    def __init__(self, sessions: SessionManager, consolidator: Consolidator,
                 session_ttl_minutes: int = 0):
        self.sessions = sessions
        self.consolidator = consolidator
        self._ttl = session_ttl_minutes
        self._archiving: set[str] = set()
        self._summaries: dict[str, tuple[str, datetime]] = {}

    def _is_expired(self, ts: datetime | str | None,
                    now: datetime | None = None) -> bool:
        if self._ttl <= 0 or not ts:
            return False
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        return ((now or datetime.now()) - ts).total_seconds() >= self._ttl * 60

    @staticmethod
    def _format_summary(text: str, last_active: datetime) -> str:
        return f"Previous conversation summary (last active {last_active.isoformat()}):\n{text}"

    def check_expired(self, schedule_background: Callable[[Coroutine], None],
                      active_session_keys: Collection[str] = ()) -> None:
        """Schedule archival for idle sessions, skipping those with in-flight agent tasks."""
        now = datetime.now()
        for info in self.sessions.list_sessions():
            key = info.get("key", "")
            if not key or key in self._archiving:
                continue
            if key in active_session_keys:
                continue
            if self._is_expired(info.get("updated_at"), now):
                self._archiving.add(key)
                schedule_background(self._archive(key))

    async def _archive(self, key: str) -> None:
        try:
            summary = await self.consolidator.compact_idle_session(
                key, self._RECENT_SUFFIX_MESSAGES,
            )
            if summary and summary != "(nothing)":
                session = self.sessions.get_or_create(key)
                meta = session.metadata.get("_last_summary")
                if isinstance(meta, dict):
                    self._summaries[key] = (
                        meta["text"],
                        datetime.fromisoformat(meta["last_active"]),
                    )
        except Exception:
            logger.exception("Auto-compact: failed for {}", key)
        finally:
            self._archiving.discard(key)

    def prepare_session(self, session: Session, key: str) -> tuple[Session, str | None]:
        if key in self._archiving or self._is_expired(session.updated_at):
            logger.info("Auto-compact: reloading session {} (archiving={})", key, key in self._archiving)
            session = self.sessions.get_or_create(key)
        # Hot path: summary from in-memory dict (process hasn't restarted).
        entry = self._summaries.pop(key, None)
        if entry:
            return session, self._format_summary(entry[0], entry[1])
        # Cold path: summary persisted in session metadata (process restarted).
        meta = session.metadata.get("_last_summary")
        if isinstance(meta, dict):
            # Prefer structured sections when available to build a compact
            # human-readable summary for injection.
            text = None
            sections = meta.get("sections") if isinstance(meta.get("sections"), dict) else None
            if sections:
                parts: list[str] = []
                order = ["overview", "goal", "constraints", "files_touched", "commands_run", "failures", "decisions", "next_steps"]
                labels = {
                    "overview": "Overview",
                    "goal": "Goal",
                    "constraints": "Constraints",
                    "files_touched": "Files touched",
                    "commands_run": "Commands run",
                    "failures": "Failures",
                    "decisions": "Decisions",
                    "next_steps": "Next steps",
                }
                for name in order:
                    vals = sections.get(name)
                    if not vals:
                        continue
                    parts.append(f"## {labels.get(name, name)}")
                    for v in vals[:6]:
                        parts.append(f"- {v}")
                    parts.append("")
                text = "\n".join(parts).strip()
            if not text:
                text = meta.get("text")
            if text:
                return session, self._format_summary(text, datetime.fromisoformat(meta["last_active"]))
        return session, None
