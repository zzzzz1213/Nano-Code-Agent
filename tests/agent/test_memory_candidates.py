from __future__ import annotations

from nanobot.agent.memory import MemoryStore
from nanobot.agent.memory_candidates import (
    MemoryCandidateError,
    build_memory_candidate,
    commit_memory_candidate,
)


def test_build_memory_candidate_from_explicit_preference(tmp_path) -> None:
    store = MemoryStore(tmp_path)

    candidate = build_memory_candidate(
        memory=store,
        user_text="请记住：我偏好简明扼要的中文回答",
        assistant_text="好的，之后会更简洁。",
        turn_id="turn-1",
    )

    assert candidate is not None
    assert candidate["type"] == "user_profile"
    assert candidate["target"] == "USER.md"
    assert candidate["content"] == "我偏好简明扼要的中文回答"
    assert candidate["turn_id"] == "turn-1"


def test_build_memory_candidate_blocks_sensitive_text(tmp_path) -> None:
    store = MemoryStore(tmp_path)

    candidate = build_memory_candidate(
        memory=store,
        user_text="请记住我的 token 是 sk-test-secret-value",
    )

    assert candidate is None


def test_commit_memory_candidate_appends_to_target_and_dedupes(tmp_path) -> None:
    store = MemoryStore(tmp_path)
    candidate = {
        "type": "project_memory",
        "content": "这个项目优先保持小规模 diff",
    }

    result = commit_memory_candidate(store, candidate)
    duplicate = commit_memory_candidate(store, candidate)

    assert result["committed"] is True
    assert result["target"] == "memory/MEMORY.md"
    assert "- 这个项目优先保持小规模 diff" in store.read_memory()
    assert duplicate["committed"] is False
    assert duplicate["duplicate"] is True
    assert duplicate["duplicate_reason"] == "exact_or_contained"


def test_build_memory_candidate_returns_merge_candidate_for_similar_existing_memory(tmp_path) -> None:
    store = MemoryStore(tmp_path)
    store.write_user("# User Profile\n\n- 我偏好简洁中文回答\n")

    candidate = build_memory_candidate(
        memory=store,
        user_text="请记住：我喜欢简明扼要的中文回复",
    )

    assert candidate is not None
    assert candidate["merge_action"] == "merge_existing"
    assert candidate["merge_reason"] == "similar_content"
    assert candidate["existing_preview"] == "我偏好简洁中文回答"
    assert candidate["merged_content"] in {"我偏好简洁中文回答", "我喜欢简明扼要的中文回复"}


def test_commit_memory_candidate_merges_similar_duplicate_with_preview(tmp_path) -> None:
    store = MemoryStore(tmp_path)
    store.write_user("# User Profile\n\n- 我偏好简洁中文回答\n")

    result = commit_memory_candidate(
        store,
        {"type": "user_profile", "content": "我喜欢简明扼要的中文回复"},
    )

    assert result["committed"] is True
    assert result["duplicate"] is False
    assert result["merged"] is True
    assert result["merge_action"] == "merge_existing"
    assert result["merge_reason"] == "similar_content"
    assert result["existing_preview"] == "我偏好简洁中文回答"
    assert result["merged_content"] in store.read_user()


def test_commit_memory_candidate_merges_extended_existing_content(tmp_path) -> None:
    store = MemoryStore(tmp_path)
    store.write_memory("# Project Memory\n\n- 这个项目优先保持小规模 diff\n")

    result = commit_memory_candidate(
        store,
        {"type": "project_memory", "content": "这个项目优先保持小规模 diff，并补充回归测试"},
    )

    assert result["committed"] is True
    assert result["merged"] is True
    assert result["merge_reason"] == "candidate_extends_existing"
    assert result["merged_content"] == "这个项目优先保持小规模 diff，并补充回归测试"
    assert "并补充回归测试" in store.read_memory()
    assert store.read_memory().count("- ") == 1


def test_build_memory_candidate_surfaces_conflict_review_for_opposite_preference(tmp_path) -> None:
    store = MemoryStore(tmp_path)
    store.write_user("# User Profile\n\n- 我偏好中文回答\n")

    candidate = build_memory_candidate(
        memory=store,
        user_text="请记住：我偏好英文回答",
    )

    assert candidate is not None
    assert candidate["merge_action"] == "review_conflict"
    assert candidate["conflict_reason"] == "conflicting_value:中文|英文"
    assert candidate["existing_preview"] == "我偏好中文回答"


def test_commit_memory_candidate_blocks_conflict_review_required(tmp_path) -> None:
    store = MemoryStore(tmp_path)
    store.write_user("# User Profile\n\n- 我偏好中文回答\n")

    result = commit_memory_candidate(
        store,
        {"type": "user_profile", "content": "我偏好英文回答"},
    )

    assert result["committed"] is False
    assert result["duplicate"] is False
    assert result["conflict"] is True
    assert result["merge_action"] == "review_conflict"
    assert result["conflict_reason"] == "conflicting_value:中文|英文"
    assert "英文回答" not in store.read_user()


def test_memory_candidate_duplicate_check_is_target_scoped(tmp_path) -> None:
    store = MemoryStore(tmp_path)
    store.write_user("# User Profile\n\n- 我偏好简洁中文回答\n")

    result = commit_memory_candidate(
        store,
        {"type": "project_memory", "content": "我偏好简洁中文回答"},
    )

    assert result["committed"] is True
    assert result["duplicate"] is False
    assert "- 我偏好简洁中文回答" in store.read_memory()


def test_commit_memory_candidate_allows_unrelated_content(tmp_path) -> None:
    store = MemoryStore(tmp_path)
    store.write_memory("# Project Memory\n\n- 这个项目优先保持小规模 diff\n")

    result = commit_memory_candidate(
        store,
        {"type": "project_memory", "content": "这个项目的 WebUI 使用 Vite 构建"},
    )

    assert result["committed"] is True
    assert result["duplicate"] is False
    assert "WebUI 使用 Vite 构建" in store.read_memory()


def test_commit_memory_candidate_rejects_secrets(tmp_path) -> None:
    store = MemoryStore(tmp_path)

    try:
        commit_memory_candidate(store, {"type": "user_profile", "content": "password=123456"})
    except MemoryCandidateError as exc:
        assert exc.status == 422
    else:
        raise AssertionError("expected sensitive candidate rejection")
