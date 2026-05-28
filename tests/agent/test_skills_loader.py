"""Tests for nanobot.agent.skills.SkillsLoader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.agent.skills import SkillsLoader


def _write_skill(
    base: Path,
    name: str,
    *,
    metadata_json: dict | None = None,
    body: str = "# Skill\n",
) -> Path:
    """Create ``base / name / SKILL.md`` with optional nanobot metadata JSON."""
    skill_dir = base / name
    skill_dir.mkdir(parents=True)
    lines = ["---"]
    if metadata_json is not None:
        payload = json.dumps({"nanobot": metadata_json}, separators=(",", ":"))
        lines.append(f'metadata: {payload}')
    lines.extend(["---", "", body])
    path = skill_dir / "SKILL.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_list_skills_empty_when_skills_dir_missing(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    assert loader.list_skills(filter_unavailable=False) == []


def test_list_skills_empty_when_skills_dir_exists_but_empty(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    (workspace / "skills").mkdir(parents=True)
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    assert loader.list_skills(filter_unavailable=False) == []


def test_list_skills_workspace_entry_shape_and_source(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    skills_root = workspace / "skills"
    skills_root.mkdir(parents=True)
    skill_path = _write_skill(skills_root, "alpha", body="# Alpha")
    builtin = tmp_path / "builtin"
    builtin.mkdir()

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    entries = loader.list_skills(filter_unavailable=False)
    assert entries == [
        {"name": "alpha", "path": str(skill_path), "source": "workspace"},
    ]


def test_list_skills_skips_non_directories_and_missing_skill_md(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    skills_root = workspace / "skills"
    skills_root.mkdir(parents=True)
    (skills_root / "not_a_dir.txt").write_text("x", encoding="utf-8")
    (skills_root / "no_skill_md").mkdir()
    ok_path = _write_skill(skills_root, "ok", body="# Ok")
    builtin = tmp_path / "builtin"
    builtin.mkdir()

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    entries = loader.list_skills(filter_unavailable=False)
    names = {entry["name"] for entry in entries}
    assert names == {"ok"}
    assert entries[0]["path"] == str(ok_path)


def test_list_skills_workspace_shadows_builtin_same_name(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    ws_skills = workspace / "skills"
    ws_skills.mkdir(parents=True)
    ws_path = _write_skill(ws_skills, "dup", body="# Workspace wins")

    builtin = tmp_path / "builtin"
    _write_skill(builtin, "dup", body="# Builtin")

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    entries = loader.list_skills(filter_unavailable=False)
    assert len(entries) == 1
    assert entries[0]["source"] == "workspace"
    assert entries[0]["path"] == str(ws_path)


def test_list_skills_merges_workspace_and_builtin(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    ws_skills = workspace / "skills"
    ws_skills.mkdir(parents=True)
    ws_path = _write_skill(ws_skills, "ws_only", body="# W")
    builtin = tmp_path / "builtin"
    bi_path = _write_skill(builtin, "bi_only", body="# B")

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    entries = sorted(loader.list_skills(filter_unavailable=False), key=lambda item: item["name"])
    assert entries == [
        {"name": "bi_only", "path": str(bi_path), "source": "builtin"},
        {"name": "ws_only", "path": str(ws_path), "source": "workspace"},
    ]


def test_list_skills_builtin_omitted_when_dir_missing(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    ws_skills = workspace / "skills"
    ws_skills.mkdir(parents=True)
    ws_path = _write_skill(ws_skills, "solo", body="# S")
    missing_builtin = tmp_path / "no_such_builtin"

    loader = SkillsLoader(workspace, builtin_skills_dir=missing_builtin)
    entries = loader.list_skills(filter_unavailable=False)
    assert entries == [{"name": "solo", "path": str(ws_path), "source": "workspace"}]


def test_list_skills_filter_unavailable_excludes_unmet_bin_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    skills_root = workspace / "skills"
    skills_root.mkdir(parents=True)
    _write_skill(
        skills_root,
        "needs_bin",
        metadata_json={"requires": {"bins": ["nanobot_test_fake_binary"]}},
    )
    builtin = tmp_path / "builtin"
    builtin.mkdir()

    def fake_which(cmd: str) -> str | None:
        if cmd == "nanobot_test_fake_binary":
            return None
        return "/usr/bin/true"

    monkeypatch.setattr("nanobot.agent.skills.shutil.which", fake_which)

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    assert loader.list_skills(filter_unavailable=True) == []


def test_list_skills_filter_unavailable_includes_when_bin_requirement_met(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    skills_root = workspace / "skills"
    skills_root.mkdir(parents=True)
    skill_path = _write_skill(
        skills_root,
        "has_bin",
        metadata_json={"requires": {"bins": ["nanobot_test_fake_binary"]}},
    )
    builtin = tmp_path / "builtin"
    builtin.mkdir()

    def fake_which(cmd: str) -> str | None:
        if cmd == "nanobot_test_fake_binary":
            return "/fake/nanobot_test_fake_binary"
        return None

    monkeypatch.setattr("nanobot.agent.skills.shutil.which", fake_which)

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    entries = loader.list_skills(filter_unavailable=True)
    assert entries == [
        {"name": "has_bin", "path": str(skill_path), "source": "workspace"},
    ]


def test_list_skills_filter_unavailable_false_keeps_unmet_requirements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    skills_root = workspace / "skills"
    skills_root.mkdir(parents=True)
    skill_path = _write_skill(
        skills_root,
        "blocked",
        metadata_json={"requires": {"bins": ["nanobot_test_fake_binary"]}},
    )
    builtin = tmp_path / "builtin"
    builtin.mkdir()

    monkeypatch.setattr("nanobot.agent.skills.shutil.which", lambda _cmd: None)

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    entries = loader.list_skills(filter_unavailable=False)
    assert entries == [
        {"name": "blocked", "path": str(skill_path), "source": "workspace"},
    ]


def test_list_skills_filter_unavailable_excludes_unmet_env_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    skills_root = workspace / "skills"
    skills_root.mkdir(parents=True)
    _write_skill(
        skills_root,
        "needs_env",
        metadata_json={"requires": {"env": ["NANOBOT_SKILLS_TEST_ENV_VAR"]}},
    )
    builtin = tmp_path / "builtin"
    builtin.mkdir()

    monkeypatch.delenv("NANOBOT_SKILLS_TEST_ENV_VAR", raising=False)

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    assert loader.list_skills(filter_unavailable=True) == []


def test_list_skills_openclaw_metadata_parsed_for_requirements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    skills_root = workspace / "skills"
    skills_root.mkdir(parents=True)
    skill_dir = skills_root / "openclaw_skill"
    skill_dir.mkdir(parents=True)
    skill_path = skill_dir / "SKILL.md"
    oc_payload = json.dumps({"openclaw": {"requires": {"bins": ["nanobot_oc_bin"]}}}, separators=(",", ":"))
    skill_path.write_text(
        "\n".join(["---", f"metadata: {oc_payload}", "---", "", "# OC"]),
        encoding="utf-8",
    )
    builtin = tmp_path / "builtin"
    builtin.mkdir()

    monkeypatch.setattr("nanobot.agent.skills.shutil.which", lambda _cmd: None)

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    assert loader.list_skills(filter_unavailable=True) == []

    monkeypatch.setattr(
        "nanobot.agent.skills.shutil.which",
        lambda cmd: "/x" if cmd == "nanobot_oc_bin" else None,
    )
    entries = loader.list_skills(filter_unavailable=True)
    assert entries == [
        {"name": "openclaw_skill", "path": str(skill_path), "source": "workspace"},
    ]


def test_disabled_skills_excluded_from_list(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    ws_skills = workspace / "skills"
    ws_skills.mkdir(parents=True)
    _write_skill(ws_skills, "alpha", body="# Alpha")
    beta_path = _write_skill(ws_skills, "beta", body="# Beta")
    builtin = tmp_path / "builtin"
    builtin.mkdir()

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin, disabled_skills={"alpha"})
    entries = loader.list_skills(filter_unavailable=False)
    assert len(entries) == 1
    assert entries[0]["name"] == "beta"
    assert entries[0]["path"] == str(beta_path)


def test_disabled_skills_empty_set_no_effect(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    ws_skills = workspace / "skills"
    ws_skills.mkdir(parents=True)
    _write_skill(ws_skills, "alpha", body="# Alpha")
    _write_skill(ws_skills, "beta", body="# Beta")
    builtin = tmp_path / "builtin"
    builtin.mkdir()

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin, disabled_skills=set())
    entries = loader.list_skills(filter_unavailable=False)
    assert len(entries) == 2


def test_disabled_skills_excluded_from_build_skills_summary(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    ws_skills = workspace / "skills"
    ws_skills.mkdir(parents=True)
    _write_skill(ws_skills, "alpha", body="# Alpha")
    _write_skill(ws_skills, "beta", body="# Beta")
    builtin = tmp_path / "builtin"
    builtin.mkdir()

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin, disabled_skills={"alpha"})
    summary = loader.build_skills_summary()
    assert "alpha" not in summary
    assert "beta" in summary


def test_disabled_skills_excluded_from_get_always_skills(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    ws_skills = workspace / "skills"
    ws_skills.mkdir(parents=True)
    _write_skill(ws_skills, "alpha", metadata_json={"always": True}, body="# Alpha")
    _write_skill(ws_skills, "beta", metadata_json={"always": True}, body="# Beta")
    builtin = tmp_path / "builtin"
    builtin.mkdir()

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin, disabled_skills={"alpha"})
    always = loader.get_always_skills()
    assert "alpha" not in always
    assert "beta" in always


# -- multiline description tests (YAML folded > and literal |) -----------------


def test_build_skills_summary_folded_description(tmp_path: Path) -> None:
    """description: > (YAML folded scalar) should be parsed correctly."""
    workspace = tmp_path / "ws"
    ws_skills = workspace / "skills"
    ws_skills.mkdir(parents=True)
    skill_dir = ws_skills / "pdf"
    skill_dir.mkdir(parents=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(
        "---\n"
        "name: pdf\n"
        "description: >\n"
        "  Use this skill when visual quality and design identity matter for a PDF.\n"
        "  CREATE (generate from scratch): \"make a PDF\".\n"
        "---\n\n# PDF Skill\n",
        encoding="utf-8",
    )
    builtin = tmp_path / "builtin"
    builtin.mkdir()

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    summary = loader.build_skills_summary()
    assert "pdf" in summary
    assert "visual quality" in summary


def test_build_skills_summary_literal_description(tmp_path: Path) -> None:
    """description: | (YAML literal scalar) should be parsed correctly."""
    workspace = tmp_path / "ws"
    ws_skills = workspace / "skills"
    ws_skills.mkdir(parents=True)
    skill_dir = ws_skills / "multi"
    skill_dir.mkdir(parents=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(
        "---\n"
        "name: multi\n"
        "description: |\n"
        "  Line one of description.\n"
        "  Line two of description.\n"
        "---\n\n# Multi\n",
        encoding="utf-8",
    )
    builtin = tmp_path / "builtin"
    builtin.mkdir()

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    meta = loader.get_skill_metadata("multi")
    assert meta is not None
    desc = meta.get("description")
    assert isinstance(desc, str)
    assert "Line one" in desc
    assert "Line two" in desc


def test_get_skill_metadata_handles_yaml_types(tmp_path: Path) -> None:
    """yaml.safe_load returns native types; always should be True, not 'true'."""
    workspace = tmp_path / "ws"
    ws_skills = workspace / "skills"
    ws_skills.mkdir(parents=True)
    skill_dir = ws_skills / "typed"
    skill_dir.mkdir(parents=True)
    payload = json.dumps({"nanobot": {"requires": {"bins": ["gh"]}, "always": True}}, separators=(",", ":"))
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(
        "---\n"
        "name: typed\n"
        f"metadata: {payload}\n"
        "always: true\n"
        "---\n\n# Typed\n",
        encoding="utf-8",
    )
    builtin = tmp_path / "builtin"
    builtin.mkdir()

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    meta = loader.get_skill_metadata("typed")
    assert meta is not None
    # YAML parsed 'true' to Python True
    assert meta.get("always") is True
    # metadata is a parsed dict, not a JSON string
    assert isinstance(meta.get("metadata"), dict)


def test_builtin_coding_assistant_is_always_skill(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    loader = SkillsLoader(workspace)
    always = loader.get_always_skills()
    assert "coding-assistant" in always

    content = loader.load_skills_for_context(["coding-assistant"])
    assert "Coding Assistant" in content
    assert "smallest useful change" in content


def test_select_task_skills_matches_engineering_keywords(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    loader = SkillsLoader(workspace)
    selected = loader.select_task_skills(
        "Please review this patch for regression risk.",
        exclude=set(loader.get_always_skills()),
    )

    assert "code-review" in selected
    assert "coding-assistant" not in selected


def test_select_task_skills_uses_priority_then_score(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    skills_root = workspace / "skills"
    skills_root.mkdir(parents=True)
    builtin = tmp_path / "builtin"
    _write_skill(
        builtin,
        "low",
        metadata_json={"task_keywords": ["pytest"], "priority": 1},
        body="# Low",
    )
    _write_skill(
        builtin,
        "high",
        metadata_json={"task_keywords": ["pytest"], "priority": 9},
        body="# High",
    )

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)

    assert loader.select_task_skills("pytest failed") == ["high", "low"]


def test_select_task_skills_filters_conflicting_lower_priority_match(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    skills_root = workspace / "skills"
    skills_root.mkdir(parents=True)
    builtin = tmp_path / "builtin"
    _write_skill(
        builtin,
        "planner",
        metadata_json={
            "task_keywords": ["migration", "rollout"],
            "priority": 6,
            "conflicts_with": ["upgrader"],
        },
        body="# Planner",
    )
    _write_skill(
        builtin,
        "upgrader",
        metadata_json={
            "task_keywords": ["migration", "upgrade"],
            "priority": 9,
            "conflicts_with": ["planner"],
        },
        body="# Upgrader",
    )

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)

    assert loader.select_task_skills("Plan a migration and upgrade rollout") == ["upgrader"]


def test_select_task_skill_matches_prefers_more_specific_keyword_when_priority_ties(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    skills_root = workspace / "skills"
    skills_root.mkdir(parents=True)
    builtin = tmp_path / "builtin"
    _write_skill(
        builtin,
        "generic",
        metadata_json={"task_keywords": ["upgrade"], "priority": 5},
        body="# Generic",
    )
    _write_skill(
        builtin,
        "specific",
        metadata_json={"task_keywords": ["dependency upgrade"], "priority": 5},
        body="# Specific",
    )

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    matches = loader.select_task_skill_matches("Please do a dependency upgrade", limit=2)

    assert [match["name"] for match in matches] == ["specific", "generic"]


def test_select_task_skill_matches_returns_safe_reason_metadata(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    loader = SkillsLoader(workspace)
    matches = loader.select_task_skill_matches(
        "Please review this patch for regression risk.",
        exclude=set(loader.get_always_skills()),
    )

    review = next(match for match in matches if match["name"] == "code-review")
    assert review["source"] == "auto"
    assert review["priority"] == 80.0
    assert "matched_keywords" in review
    assert "review" in review["matched_keywords"]
    assert str(review["reason"]).startswith("matched:")
    assert "content" not in review


def test_select_task_skill_matches_builtin_conflict_keeps_dependency_upgrade_over_migration(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    loader = SkillsLoader(workspace)
    matches = loader.select_task_skill_matches(
        "Plan the migration rollout and upgrade dependency versions safely.",
        exclude=set(loader.get_always_skills()),
        limit=3,
    )

    names = [match["name"] for match in matches]
    assert "dependency-upgrade" in names
    assert "migration-planning" not in names


def test_builtin_engineering_skills_are_discoverable(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    loader = SkillsLoader(workspace)
    names = {entry["name"] for entry in loader.list_skills(filter_unavailable=False)}

    assert "frontend-implementation" in names
    assert "migration-planning" in names
    assert "dependency-upgrade" in names
    assert "docs-sync" in names


def test_select_task_skills_matches_frontend_implementation(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    loader = SkillsLoader(workspace)
    selected = loader.select_task_skills(
        "Implement a responsive React component and refine the page layout CSS.",
        exclude=set(loader.get_always_skills()),
    )

    assert "frontend-implementation" in selected


def test_select_task_skills_matches_migration_planning(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    loader = SkillsLoader(workspace)
    selected = loader.select_task_skills(
        "Plan a schema migration with compatibility and phased rollout steps.",
        exclude=set(loader.get_always_skills()),
    )

    assert "migration-planning" in selected


def test_select_task_skills_matches_dependency_upgrade(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    loader = SkillsLoader(workspace)
    selected = loader.select_task_skills(
        "Upgrade dependency versions and bump the package upgrade safely.",
        exclude=set(loader.get_always_skills()),
    )

    assert "dependency-upgrade" in selected


def test_select_task_skills_matches_docs_sync(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    loader = SkillsLoader(workspace)
    selected = loader.select_task_skills(
        "Update docs, README, and CHANGELOG so the documentation matches the new flow.",
        exclude=set(loader.get_always_skills()),
    )

    assert "docs-sync" in selected
