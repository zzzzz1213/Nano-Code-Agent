"""User-confirmed long-term memory candidate helpers."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

from nanobot.agent.memory import MemoryStore
from nanobot.utils.helpers import truncate_text

MEMORY_CANDIDATE_VERSION = 1
MEMORY_CANDIDATE_TYPES = {
    "user_profile": {
        "target": "USER.md",
        "title": "User Profile",
        "header": "# User Profile",
        "reason": "User preference or profile detail",
    },
    "project_memory": {
        "target": "memory/MEMORY.md",
        "title": "Project Memory",
        "header": "# Project Memory",
        "reason": "Project-specific long-term note",
    },
    "assistant_style": {
        "target": "SOUL.md",
        "title": "Assistant Style",
        "header": "# Assistant Style",
        "reason": "Assistant behavior or style preference",
    },
}

_REMEMBER_MARKERS = (
    "记住",
    "记一下",
    "记录一下",
    "以后",
    "下次",
    "我喜欢",
    "我偏好",
    "我的偏好",
    "请记",
    "remember",
    "note that",
    "from now on",
    "i prefer",
    "my preference",
)
_ASSISTANT_STYLE_MARKERS = (
    "你",
    "助手",
    "语气",
    "风格",
    "style",
    "tone",
    "assistant",
    "respond",
)
_PROJECT_MARKERS = (
    "项目",
    "代码",
    "仓库",
    "repo",
    "repository",
    "project",
    "codebase",
    "架构",
)
_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|passwd|authorization|bearer|private[_-]?key)\b"
    r"|密钥|私钥|凭证|密码|令牌|sk-[A-Za-z0-9_-]{12,}|AKIA[0-9A-Z]{16}"
)
_COMMAND_RE = re.compile(r"^\s*/[A-Za-z][\w-]*")
_MARKER_PREFIX_RE = re.compile(
    r"(?i)^\s*(?:请)?(?:帮我)?(?:记住|记一下|记录一下|remember(?: that)?|note that)[:：,，\s]*"
)
_MEMORY_LINE_PREFIX_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+|#+\s*)")
_WORD_RE = re.compile(r"[a-z0-9]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_SIMILARITY_THRESHOLD = 0.78
_CONTAINMENT_THRESHOLD = 0.9
_CANONICAL_REPLACEMENTS = (
    ("简明扼要", "简洁"),
    ("简明", "简洁"),
    ("简短", "简洁"),
    ("精简", "简洁"),
    ("回复", "回答"),
    ("答复", "回答"),
    ("喜欢", "偏好"),
    ("preference", "prefer"),
    ("preferred", "prefer"),
    ("prefers", "prefer"),
    ("responses", "reply"),
    ("response", "reply"),
    ("replies", "reply"),
    ("answers", "reply"),
    ("answer", "reply"),
    ("concise", "brief"),
    ("succinct", "brief"),
)
_NEGATION_MARKERS = ("不", "不要", "别", "勿", "no ", "not ", "don't", "do not", "never")
_CONFLICT_GROUPS = (
    ("中文", "英文"),
    ("chinese", "english"),
    ("简洁", "详细"),
    ("brief", "detailed"),
    ("正式", "随意"),
    ("formal", "casual"),
)


class MemoryCandidateError(ValueError):
    """Raised when a memory candidate cannot be accepted."""

    def __init__(self, message: str, *, status: int = 400):
        super().__init__(message)
        self.status = status
        self.message = message


def has_sensitive_memory_text(text: str) -> bool:
    return bool(_SECRET_RE.search(text))


def _normalize_candidate_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    text = _MARKER_PREFIX_RE.sub("", text).strip()
    text = re.sub(r"\s+", " ", text)
    return truncate_text(text, 320).strip()


def _canonical_memory_text(text: str) -> str:
    text = _normalize_candidate_text(text)
    text = _MEMORY_LINE_PREFIX_RE.sub("", text).strip().casefold()
    for src, dst in _CANONICAL_REPLACEMENTS:
        text = text.replace(src, dst)
    text = re.sub(r"[^\w\u4e00-\u9fff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _memory_tokens(text: str) -> set[str]:
    canonical = _canonical_memory_text(text)
    tokens = set(_WORD_RE.findall(canonical))
    tokens.update(_CJK_RE.findall(canonical))
    return {token for token in tokens if token}


def _should_offer_candidate(text: str) -> bool:
    if not text or _COMMAND_RE.match(text):
        return False
    lowered = text.lower()
    return any(marker in lowered or marker in text for marker in _REMEMBER_MARKERS)


def _candidate_type(text: str) -> str:
    lowered = text.lower()
    if any(marker in lowered or marker in text for marker in _ASSISTANT_STYLE_MARKERS):
        return "assistant_style"
    if any(marker in lowered or marker in text for marker in _PROJECT_MARKERS):
        return "project_memory"
    return "user_profile"


def _target_text(store: MemoryStore, candidate_type: str) -> str:
    if candidate_type == "user_profile":
        return store.read_user()
    if candidate_type == "assistant_style":
        return store.read_soul()
    return store.read_memory()


def _write_target_text(store: MemoryStore, candidate_type: str, text: str) -> None:
    if candidate_type == "user_profile":
        store.write_user(text)
        return
    if candidate_type == "assistant_style":
        store.write_soul(text)
        return
    store.write_memory(text)


def _contains_duplicate(existing: str, content: str) -> bool:
    match = _find_duplicate(existing, content)
    return bool(match and match["reason"] == "exact_or_contained")


def _iter_existing_memory_items(existing: str) -> list[str]:
    items: list[str] = []
    for raw_line in existing.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        cleaned = _MEMORY_LINE_PREFIX_RE.sub("", line).strip()
        if cleaned:
            items.append(cleaned)
    if not items and existing.strip():
        items.append(existing.strip())
    return items


def _find_duplicate(existing: str, content: str) -> dict[str, Any] | None:
    if not content:
        return {
            "reason": "empty_content",
            "existing_preview": "",
            "score": 1.0,
        }
    if not existing.strip():
        return None

    needle = _canonical_memory_text(content)
    if not needle:
        return {
            "reason": "empty_content",
            "existing_preview": "",
            "score": 1.0,
        }

    for item in _iter_existing_memory_items(existing):
        haystack = _canonical_memory_text(item)
        if not haystack:
            continue
        if needle == haystack or needle in haystack:
            return {
                "reason": "exact_or_contained",
                "existing_preview": truncate_text(item, 160),
                "existing_item": item,
                "score": 1.0,
            }
        if haystack in needle:
            merged = _merge_memory_item(item, content)
            return {
                "reason": "similar_merge_candidate",
                "merge_action": "merge_existing",
                "merge_reason": "candidate_extends_existing",
                "existing_preview": truncate_text(item, 160),
                "existing_item": item,
                "merged_content": merged,
                "score": 1.0,
            }

        needle_tokens = _memory_tokens(needle)
        haystack_tokens = _memory_tokens(haystack)
        if not needle_tokens or not haystack_tokens:
            continue
        overlap = len(needle_tokens & haystack_tokens)
        union = len(needle_tokens | haystack_tokens)
        similarity = overlap / union if union else 0.0
        containment = overlap / min(len(needle_tokens), len(haystack_tokens))
        conflict_reason = _detect_memory_conflict(item, content)
        if conflict_reason and (similarity >= 0.6 or containment >= 0.7):
            merged = _merge_memory_item(item, content)
            return {
                "reason": "conflict_review_required",
                "merge_action": "review_conflict",
                "merge_reason": None,
                "conflict_reason": conflict_reason,
                "existing_preview": truncate_text(item, 160),
                "existing_item": item,
                "merged_content": merged,
                "score": round(max(similarity, containment), 3),
            }
        if (
            similarity >= _SIMILARITY_THRESHOLD
            or containment >= _CONTAINMENT_THRESHOLD
        ):
            merged = _merge_memory_item(item, content)
            return {
                "reason": "conflict_review_required" if conflict_reason else "similar_merge_candidate",
                "merge_action": "review_conflict" if conflict_reason else "merge_existing",
                "merge_reason": None if conflict_reason else "similar_content",
                "conflict_reason": conflict_reason,
                "existing_preview": truncate_text(item, 160),
                "existing_item": item,
                "merged_content": merged,
                "score": round(max(similarity, containment), 3),
            }
    return None


def _contains_negation(text: str) -> bool:
    lowered = text.casefold()
    return any(marker in lowered for marker in _NEGATION_MARKERS)


def _detect_memory_conflict(existing_item: str, content: str) -> str | None:
    existing_canonical = _canonical_memory_text(existing_item)
    candidate_canonical = _canonical_memory_text(content)
    existing_negated = _contains_negation(existing_item)
    candidate_negated = _contains_negation(content)
    if existing_negated != candidate_negated:
        return "opposite_preference_polarity"
    for left, right in _CONFLICT_GROUPS:
        left_present = left in existing_canonical or left in candidate_canonical
        right_present = right in existing_canonical or right in candidate_canonical
        if not (left_present and right_present):
            continue
        existing_side = left if left in existing_canonical else right if right in existing_canonical else None
        candidate_side = left if left in candidate_canonical else right if right in candidate_canonical else None
        if existing_side and candidate_side and existing_side != candidate_side:
            return f"conflicting_value:{left}|{right}"
    return None


def _merge_memory_item(existing_item: str, content: str) -> str:
    existing = _normalize_candidate_text(existing_item)
    candidate = _normalize_candidate_text(content)
    if not existing:
        return candidate
    if not candidate:
        return existing
    existing_canonical = _canonical_memory_text(existing)
    candidate_canonical = _canonical_memory_text(candidate)
    if candidate_canonical == existing_canonical:
        return existing if len(existing) >= len(candidate) else candidate
    if existing_canonical in candidate_canonical:
        return candidate
    if candidate_canonical in existing_canonical:
        return existing
    if len(candidate) > len(existing):
        return candidate
    return existing


def _merge_memory_text(existing: str, existing_item: str, merged_content: str, header: str) -> str:
    merged_line = f"- {merged_content}"
    lines = existing.splitlines()
    updated_lines: list[str] = []
    replaced = False
    for raw_line in lines:
        line = raw_line.strip()
        cleaned = _MEMORY_LINE_PREFIX_RE.sub("", line).strip() if line and not line.startswith("#") else ""
        if not replaced and cleaned == existing_item.strip():
            updated_lines.append(merged_line)
            replaced = True
            continue
        updated_lines.append(raw_line)
    if not updated_lines:
        return f"{header}\n\n{merged_line}\n"
    updated = "\n".join(updated_lines).rstrip()
    if not replaced:
        if not updated:
            return f"{header}\n\n{merged_line}\n"
        return f"{updated}\n\n{merged_line}\n"
    return f"{updated}\n"


def build_memory_candidate(
    *,
    memory: MemoryStore,
    user_text: str,
    assistant_text: str = "",
    turn_id: str | None = None,
) -> dict[str, Any] | None:
    """Build a safe memory candidate from an explicit user remember request.

    This intentionally uses conservative local heuristics. The candidate is
    surfaced to WebUI for user confirmation; it is never written automatically.
    """
    raw = user_text.strip()
    if not _should_offer_candidate(raw) or has_sensitive_memory_text(raw):
        return None
    content = _normalize_candidate_text(raw)
    if len(content) < 4 or has_sensitive_memory_text(content):
        return None
    candidate_type = _candidate_type(content)
    target = MEMORY_CANDIDATE_TYPES[candidate_type]["target"]
    existing_match = _find_duplicate(_target_text(memory, candidate_type), content)
    if existing_match and existing_match["reason"] == "exact_or_contained":
        return None
    digest = hashlib.sha256(f"{candidate_type}\0{target}\0{content}".encode("utf-8")).hexdigest()
    preview = truncate_text(assistant_text.strip(), 160) if assistant_text else ""
    candidate: dict[str, Any] = {
        "version": MEMORY_CANDIDATE_VERSION,
        "id": f"memcand_{digest[:16]}",
        "type": candidate_type,
        "target": target,
        "title": MEMORY_CANDIDATE_TYPES[candidate_type]["title"],
        "content": content,
        "reason": MEMORY_CANDIDATE_TYPES[candidate_type]["reason"],
        "source": "turn",
        "turn_id": turn_id,
        "assistant_preview": preview,
        "sensitive": False,
        "duplicate": False,
        "created_at": datetime.now(UTC).isoformat(),
    }
    if existing_match:
        candidate["existing_preview"] = existing_match.get("existing_preview")
        if isinstance(existing_match.get("merge_action"), str):
            candidate["merge_action"] = existing_match["merge_action"]
        if isinstance(existing_match.get("merge_reason"), str):
            candidate["merge_reason"] = existing_match["merge_reason"]
        if isinstance(existing_match.get("conflict_reason"), str):
            candidate["conflict_reason"] = existing_match["conflict_reason"]
        if isinstance(existing_match.get("merged_content"), str):
            candidate["merged_content"] = existing_match["merged_content"]
        if isinstance(existing_match.get("score"), (int, float)):
            candidate["merge_score"] = existing_match["score"]
    return candidate


def validate_memory_candidate(candidate: Any) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        raise MemoryCandidateError("candidate must be an object")
    raw_type = candidate.get("type")
    if raw_type not in MEMORY_CANDIDATE_TYPES:
        raise MemoryCandidateError("unsupported memory candidate type")
    raw_content = candidate.get("content")
    if not isinstance(raw_content, str):
        raise MemoryCandidateError("candidate content must be text")
    content = _normalize_candidate_text(raw_content)
    if len(content) < 4:
        raise MemoryCandidateError("candidate content is empty")
    if has_sensitive_memory_text(content):
        raise MemoryCandidateError("candidate appears to contain sensitive data", status=422)
    expected_target = MEMORY_CANDIDATE_TYPES[raw_type]["target"]
    return {
        "version": MEMORY_CANDIDATE_VERSION,
        "id": str(candidate.get("id") or ""),
        "type": raw_type,
        "target": expected_target,
        "title": MEMORY_CANDIDATE_TYPES[raw_type]["title"],
        "content": content,
        "reason": str(candidate.get("reason") or MEMORY_CANDIDATE_TYPES[raw_type]["reason"]),
        "source": str(candidate.get("source") or "webui"),
        "turn_id": candidate.get("turn_id") if isinstance(candidate.get("turn_id"), str) else None,
        "created_at": candidate.get("created_at") if isinstance(candidate.get("created_at"), str) else None,
    }


def commit_memory_candidate(memory: MemoryStore, candidate: Any) -> dict[str, Any]:
    """Append a confirmed candidate to the target memory file."""
    normalized = validate_memory_candidate(candidate)
    candidate_type = normalized["type"]
    existing = _target_text(memory, candidate_type)
    content = normalized["content"]
    duplicate = _find_duplicate(existing, content)
    if duplicate:
        if duplicate["reason"] == "similar_merge_candidate":
            merged_content = str(duplicate.get("merged_content") or content)
            updated = _merge_memory_text(
                existing,
                str(duplicate.get("existing_item") or ""),
                merged_content,
                MEMORY_CANDIDATE_TYPES[candidate_type]["header"],
            )
            _write_target_text(memory, candidate_type, updated)
            return {
                "committed": True,
                "duplicate": False,
                "merged": True,
                "merge_action": duplicate.get("merge_action"),
                "merge_reason": duplicate.get("merge_reason"),
                "existing_preview": duplicate["existing_preview"],
                "merged_content": merged_content,
                "target": normalized["target"],
                "candidate": normalized,
            }
        if duplicate["reason"] == "conflict_review_required":
            return {
                "committed": False,
                "duplicate": False,
                "conflict": True,
                "merge_action": duplicate.get("merge_action"),
                "conflict_reason": duplicate.get("conflict_reason"),
                "existing_preview": duplicate["existing_preview"],
                "merged_content": duplicate.get("merged_content"),
                "target": normalized["target"],
                "candidate": normalized,
            }
        return {
            "committed": False,
            "duplicate": True,
            "duplicate_reason": duplicate["reason"],
            "existing_preview": duplicate["existing_preview"],
            "target": normalized["target"],
            "candidate": normalized,
        }

    header = MEMORY_CANDIDATE_TYPES[candidate_type]["header"]
    base = existing.rstrip()
    if not base:
        updated = f"{header}\n\n- {content}\n"
    else:
        updated = f"{base}\n\n- {content}\n"
    _write_target_text(memory, candidate_type, updated)
    return {
        "committed": True,
        "duplicate": False,
        "target": normalized["target"],
        "candidate": normalized,
    }
