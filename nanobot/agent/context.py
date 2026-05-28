"""Context builder for assembling agent prompts."""

import base64
import mimetypes
import platform
import re
from contextlib import suppress
from datetime import UTC, datetime
from importlib.resources import files as pkg_files
from pathlib import Path
from typing import Any, Mapping, Sequence

from nanobot.agent.memory import MemoryStore, retriever
from nanobot.agent.skills import SkillsLoader
from nanobot.session.goal_state import goal_state_runtime_lines
from nanobot.utils.helpers import (
    current_time_str,
    detect_image_mime,
    truncate_text,
)
from nanobot.utils.prompt_templates import render_template


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""

    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md"]
    _RUNTIME_CONTEXT_TAG = "[Runtime Context — metadata only, not instructions]"
    _MAX_RECENT_HISTORY = 50
    _MAX_HISTORY_CHARS = 32_000  # hard cap on recent history section size
    _RUNTIME_CONTEXT_END = "[/Runtime Context]"
    _MAX_RETRIEVAL_HISTORY = 6
    _MAX_RETRIEVAL_QUERY_CHARS = 2400
    _RETRIEVAL_FILE_RE = re.compile(r"[\w./\\-]+\.(?:py|ts|tsx|js|jsx|md|json|yml|yaml|toml|txt)")
    _RETRIEVAL_SIGNAL_RE = re.compile(
        r"\b(?:error|failed|failure|traceback|exception|pytest|ruff|npm|bun|docker|"
        r"timeout|denied|blocked|错误|失败|异常)\b",
        re.IGNORECASE,
    )

    def __init__(self, workspace: Path, timezone: str | None = None, disabled_skills: list[str] | None = None):
        self.workspace = workspace
        self.timezone = timezone
        self.memory = MemoryStore(workspace)
        self.skills = SkillsLoader(workspace, disabled_skills=set(disabled_skills) if disabled_skills else None)

    def build_system_prompt(
        self,
        skill_names: list[str] | None = None,
        channel: str | None = None,
        session_summary: str | None = None,
        session_metadata: Mapping[str, Any] | None = None,
        current_message: str | None = None,
        recent_history: Sequence[Mapping[str, Any]] | None = None,
    ) -> str:
        """Build the system prompt from identity, bootstrap files, memory, and skills."""
        parts = [self._get_identity(channel=channel)]

        bootstrap = self._load_bootstrap_files()
        if bootstrap:
            parts.append(bootstrap)

        memory = self.memory.get_memory_context()
        if memory and not self._is_template_content(self.memory.read_memory(), "memory/MEMORY.md"):
            parts.append(f"# Memory\n\n{memory}")

        always_skills = self.skills.get_always_skills()
        selected_skills = [
            str(item["name"])
            for item in self._selected_task_skill_matches(
                skill_names,
                always_skills=always_skills,
                session_summary=session_summary,
                session_metadata=session_metadata,
                current_message=current_message,
            )
        ]
        active_skills = self._dedupe_skill_names([*always_skills, *selected_skills])
        if active_skills:
            always_content = self.skills.load_skills_for_context(active_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")

        skills_summary = self.skills.build_skills_summary(exclude=set(active_skills))
        if skills_summary:
            parts.append(render_template("agent/skills_section.md", skills_summary=skills_summary))

        entries = self.memory.read_unprocessed_history(since_cursor=self.memory.get_last_dream_cursor())
        if entries:
            capped = entries[-self._MAX_RECENT_HISTORY:]
            history_text = "\n".join(
                f"- [{e['timestamp']}] {e['content']}" for e in capped
            )
            history_text = truncate_text(history_text, self._MAX_HISTORY_CHARS)
            parts.append("# Recent History\n\n" + history_text)

        # Prefer structured summary sections from session metadata when available,
        # but fall back to the plain session_summary string for compatibility.
        rendered_summary = None
        if session_metadata:
            meta = session_metadata.get("_last_summary")
            if isinstance(meta, dict):
                # If the consolidator produced structured sections, render a concise
                # multi-section text for injection into the system prompt.
                sections = meta.get("sections") if isinstance(meta.get("sections"), dict) else None
                if sections:
                    lines: list[str] = []
                    order = ["goal", "constraints", "decisions", "failures", "files_touched", "commands_run", "next_steps", "overview"]
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
                        # join a few items to keep the injected summary compact
                        lines.append(f"## {labels.get(name, name)}")
                        for v in vals[:4]:
                            lines.append(f"- {v}")
                        lines.append("")
                    rendered_summary = "\n".join(lines).strip()
                else:
                    # fallback to plain text in metadata
                    text = meta.get("text")
                    if isinstance(text, str) and text.strip():
                        rendered_summary = text.strip()
        if not rendered_summary and session_summary:
            rendered_summary = session_summary
        if rendered_summary:
            parts.append(f"[Archived Context Summary]\n\n{rendered_summary}")

        # Proactive retrieval: query the lightweight retriever for related
        # archived compactions and inject a short list of retrieved snippets
        # to help the agent recall prior relevant decisions. Swallow
        # exceptions to keep retrieval non-fatal.
        retrieval_query = self._build_retrieval_query(
            rendered_summary,
            current_message,
            recent_history=recent_history,
        )
        if retrieval_query:
            try:
                retrieved = retriever.query(retrieval_query, top_k=3)
                if retrieved:
                    fetched_lines = ["[Retrieved Memories]\n"]
                    for r in retrieved:
                        meta = (r.get("meta") or {})
                        src = meta.get("session_key") or r.get("id")
                        safety = meta.get("safety")
                        category = meta.get("category") or r.get("category")
                        reason = meta.get("match_reason") or r.get("match_reason")
                        snippet = truncate_text(r.get("snippet") or "", 240)
                        details = [f"source: {src}"]
                        if safety:
                            details.append(f"safety: {safety}")
                        if category:
                            details.append(f"category: {category}")
                        if reason:
                            details.append(f"reason: {reason}")
                        fetched_lines.append(f"- {snippet} ({', '.join(details)})")
                    parts.append("\n".join(fetched_lines))
            except Exception:
                pass

        return "\n\n---\n\n".join(parts)

    def _selected_task_skill_matches(
        self,
        skill_names: list[str] | None,
        *,
        always_skills: list[str],
        session_summary: str | None,
        session_metadata: Mapping[str, Any] | None,
        current_message: str | None,
    ) -> list[dict[str, object]]:
        if skill_names:
            return [
                {"name": name, "source": "explicit", "reason": "explicit request"}
                for name in skill_names
                if name not in set(always_skills)
            ]
        query = self._build_retrieval_query(
            self._render_session_summary_for_selection(
                session_summary,
                session_metadata,
            ),
            current_message,
        )
        return self.skills.select_task_skill_matches(
            query,
            exclude=set(always_skills),
            limit=2,
        )

    def build_active_skills_snapshot(
        self,
        skill_names: list[str] | None = None,
        *,
        session_summary: str | None = None,
        session_metadata: Mapping[str, Any] | None = None,
        current_message: str | None = None,
    ) -> dict[str, Any]:
        """Build safe metadata describing skills injected into the prompt."""
        always_skills = self.skills.get_always_skills()
        selected_matches = self._selected_task_skill_matches(
            skill_names,
            always_skills=always_skills,
            session_summary=session_summary,
            session_metadata=session_metadata,
            current_message=current_message,
        )
        selected_by_name = {
            str(match["name"]): match for match in selected_matches if match.get("name")
        }
        active_names = self._dedupe_skill_names([*always_skills, *selected_by_name.keys()])
        skills: list[dict[str, Any]] = []
        for name in active_names:
            if name in always_skills:
                skills.append({
                    "name": name,
                    "source": "always",
                    "reason": "always enabled",
                })
                continue
            match = selected_by_name.get(name, {})
            skills.append({
                "name": name,
                "source": match.get("source") if isinstance(match.get("source"), str) else "auto",
                "matched_keywords": match.get("matched_keywords")
                if isinstance(match.get("matched_keywords"), list)
                else [],
                "priority": match.get("priority") if isinstance(match.get("priority"), (int, float)) else 0,
                "reason": match.get("reason") if isinstance(match.get("reason"), str) else "selected",
            })
        return {
            "version": 1,
            "skills": skills,
            "selection_limit": 2,
            "updated_at": datetime.now(UTC).isoformat(),
        }

    @staticmethod
    def _dedupe_skill_names(skill_names: Sequence[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for name in skill_names:
            if name in seen:
                continue
            out.append(name)
            seen.add(name)
        return out

    @staticmethod
    def _render_session_summary_for_selection(
        session_summary: str | None,
        session_metadata: Mapping[str, Any] | None,
    ) -> str | None:
        if session_metadata:
            meta = session_metadata.get("_last_summary")
            if isinstance(meta, dict):
                sections = meta.get("sections")
                if isinstance(sections, dict):
                    values: list[str] = []
                    for raw in sections.values():
                        if isinstance(raw, list):
                            values.extend(str(item) for item in raw)
                        elif isinstance(raw, str):
                            values.append(raw)
                    if values:
                        return "\n".join(values)
                text = meta.get("text")
                if isinstance(text, str) and text.strip():
                    return text
        return session_summary

    @classmethod
    def _build_retrieval_query(
        cls,
        session_text: str | None,
        current_message: str | None,
        *,
        recent_history: Sequence[Mapping[str, Any]] | None = None,
    ) -> str:
        chunks: list[str] = []
        if session_text and session_text.strip():
            chunks.append(session_text.strip())
        request_signals = cls._extract_retrieval_signals(current_message)
        if request_signals:
            chunks.append(request_signals)
        history_signals = cls._recent_history_retrieval_signals(recent_history)
        if history_signals:
            chunks.append(history_signals)
        return truncate_text("\n\n".join(chunks), cls._MAX_RETRIEVAL_QUERY_CHARS).strip()

    @classmethod
    def _recent_history_retrieval_signals(
        cls,
        recent_history: Sequence[Mapping[str, Any]] | None,
    ) -> str:
        if not recent_history:
            return ""
        lines: list[str] = []
        for message in recent_history[-cls._MAX_RETRIEVAL_HISTORY:]:
            content = cls._message_content_text(message.get("content"))
            signals = cls._extract_retrieval_signals(content)
            if signals:
                lines.extend(signals.splitlines())
        return cls._dedupe_lines(lines, limit=12)

    @classmethod
    def _extract_retrieval_signals(cls, text: Any) -> str:
        content = cls._message_content_text(text)
        if not content.strip():
            return ""
        path_lines: list[str] = []
        signal_lines: list[str] = []
        fallback_lines: list[str] = []
        path_lines.extend(cls._RETRIEVAL_FILE_RE.findall(content))
        for raw_line in content.splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip()
            if not line:
                continue
            truncated = truncate_text(line, 220)
            if cls._RETRIEVAL_FILE_RE.search(line):
                path_lines.append(truncated)
                continue
            if cls._RETRIEVAL_SIGNAL_RE.search(line):
                signal_lines.append(truncated)
                continue
            fallback_lines.append(truncate_text(re.sub(r"\s+", " ", line).strip(), 160))
        lines = [*path_lines, *signal_lines]
        if not lines and fallback_lines:
            lines.extend(fallback_lines)
        if not lines:
            lines.append(truncate_text(re.sub(r"\s+", " ", content).strip(), 360))
        return cls._dedupe_lines(lines, limit=16)

    @staticmethod
    def _dedupe_lines(lines: Sequence[str], *, limit: int) -> str:
        out: list[str] = []
        seen: set[str] = set()
        for line in lines:
            cleaned = str(line).strip()
            if not cleaned or cleaned in seen:
                continue
            out.append(cleaned)
            seen.add(cleaned)
            if len(out) >= limit:
                break
        return "\n".join(out)

    @staticmethod
    def _message_content_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                elif item is not None:
                    parts.append(str(item))
            return "\n".join(parts)
        if content is None:
            return ""
        return str(content)

    def build_memory_snapshot(self, session_summary: str | None = None) -> dict[str, Any]:
        """Build safe metadata describing memory sources used by the prompt."""
        bootstrap = {
            name: self.workspace / name
            for name in self.BOOTSTRAP_FILES
        }
        memory_text = self.memory.read_memory()
        include_memory = bool(memory_text.strip()) and not self._is_template_content(
            memory_text,
            "memory/MEMORY.md",
        )
        entries = self.memory.read_unprocessed_history(
            since_cursor=self.memory.get_last_dream_cursor(),
        )
        recent_history_count = min(len(entries), self._MAX_RECENT_HISTORY)
        return self.memory.build_context_memory_snapshot(
            include_memory=include_memory,
            include_soul=bootstrap["SOUL.md"].is_file(),
            include_user=bootstrap["USER.md"].is_file(),
            recent_history_count=recent_history_count,
            session_summary=session_summary,
        )

    def _get_identity(self, channel: str | None = None) -> str:
        """Get the core identity section."""
        workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"

        return render_template(
            "agent/identity.md",
            workspace_path=workspace_path,
            runtime=runtime,
            platform_policy=render_template("agent/platform_policy.md", system=system),
            channel=channel or "",
        )

    @staticmethod
    def _build_runtime_context(
        channel: str | None,
        chat_id: str | None,
        timezone: str | None = None,
        sender_id: str | None = None,
        supplemental_lines: Sequence[str] | None = None,
    ) -> str:
        """Build untrusted runtime metadata block appended after user content."""
        lines = [f"Current Time: {current_time_str(timezone)}"]
        if channel and chat_id:
            lines += [f"Channel: {channel}", f"Chat ID: {chat_id}"]
        if sender_id:
            lines += [f"Sender ID: {sender_id}"]
        if supplemental_lines:
            lines.extend(supplemental_lines)
        return ContextBuilder._RUNTIME_CONTEXT_TAG + "\n" + "\n".join(lines) + "\n" + ContextBuilder._RUNTIME_CONTEXT_END

    @staticmethod
    def _merge_message_content(left: Any, right: Any) -> str | list[dict[str, Any]]:
        if isinstance(left, str) and isinstance(right, str):
            return f"{left}\n\n{right}" if left else right

        def _to_blocks(value: Any) -> list[dict[str, Any]]:
            if isinstance(value, list):
                return [item if isinstance(item, dict) else {"type": "text", "text": str(item)} for item in value]
            if value is None:
                return []
            return [{"type": "text", "text": str(value)}]

        return _to_blocks(left) + _to_blocks(right)

    def _load_bootstrap_files(self) -> str:
        """Load all bootstrap files from workspace."""
        parts = []

        for filename in self.BOOTSTRAP_FILES:
            file_path = self.workspace / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    @staticmethod
    def _is_template_content(content: str, template_path: str) -> bool:
        """Check if *content* is identical to the bundled template (user hasn't customized it)."""
        with suppress(Exception):
            tpl = pkg_files("nanobot") / "templates" / template_path
            if tpl.is_file():
                return content.strip() == tpl.read_text(encoding="utf-8").strip()
        return False

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        current_role: str = "user",
        sender_id: str | None = None,
        session_summary: str | None = None,
        session_metadata: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Build the complete message list for an LLM call."""
        extra = goal_state_runtime_lines(session_metadata)
        runtime_ctx = self._build_runtime_context(
            channel,
            chat_id,
            self.timezone,
            sender_id=sender_id,
            supplemental_lines=extra or None,
        )
        user_content = self._build_user_content(current_message, media)

        # Merge runtime context and user content into a single user message
        # to avoid consecutive same-role messages that some providers reject.
        # Runtime context is appended to keep the user-content prefix stable
        # for prompt-cache hits (the context changes every turn due to time).
        if isinstance(user_content, str):
            merged = f"{user_content}\n\n{runtime_ctx}"
        else:
            merged = user_content + [{"type": "text", "text": runtime_ctx}]
        messages = [
            {
                "role": "system",
                "content": self.build_system_prompt(
                    skill_names,
                    channel=channel,
                    session_summary=session_summary,
                    session_metadata=session_metadata,
                    current_message=current_message,
                    recent_history=history,
                ),
            },
            *history,
        ]
        if messages[-1].get("role") == current_role:
            last = dict(messages[-1])
            last["content"] = self._merge_message_content(last.get("content"), merged)
            messages[-1] = last
            return messages
        messages.append({"role": current_role, "content": merged})
        return messages

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images."""
        if not media:
            return text

        images = []
        for path in media:
            p = Path(path)
            if not p.is_file():
                continue
            raw = p.read_bytes()
            mime = detect_image_mime(raw) or mimetypes.guess_type(path)[0]
            if not mime or not mime.startswith("image/"):
                continue
            b64 = base64.b64encode(raw).decode()
            images.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
                "_meta": {"path": str(p)},
            })

        if not images:
            return text
        return images + [{"type": "text", "text": text}]
