# -*- coding: utf-8 -*-
"""Regression tests for packaged builtin skill pool import."""

import operator

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import frontmatter
import pytest

from lantai import constant
from lantai.agents import skills_manager


def _older_builtin_copy(content: str) -> str:
    version = str(frontmatter.loads(content).metadata["version"])
    assert version != "2.0.0"
    current_label = f"version: {version}"
    assert current_label in content
    return content.replace(current_label, "version: 2.0.0", 1)


def test_import_builtin_skills_processes_all_requested_builtins(
    monkeypatch,
    tmp_path,
):
    (
        "Fresh installs must import every requested builtin skill"
        ", not just one."
    )
    monkeypatch.setattr(constant, "WORKING_DIR", tmp_path)
    getattr(skills_manager, "_builtin_cache").clear()

    requested = [
        {"skill_name": "guidance", "language": "zh"},
        {"skill_name": "QA_source_index", "language": "zh"},
        {"skill_name": "americano", "language": "zh"},
    ]

    result = skills_manager.import_builtin_skills(requested)
    manifest = skills_manager.read_skill_pool_manifest()

    assert operator.eq(result["conflicts"], [])
    assert result["imported"] == [
        "guidance",
        "QA_source_index",
        "americano",
    ]
    assert set(request["skill_name"] for request in requested).issubset(
        manifest["skills"],
    )


def test_import_without_language_falls_back_to_an_available_variant(
    monkeypatch,
    tmp_path,
):
    """Removed translations must not break repair of an existing builtin."""
    monkeypatch.setattr(constant, "WORKING_DIR", tmp_path)
    monkeypatch.setattr(
        skills_manager,
        "get_builtin_skill_language_preference",
        lambda: "en",
    )
    getattr(skills_manager, "_builtin_cache").clear()

    result = skills_manager.import_builtin_skills(
        [{"skill_name": "americano"}],
    )
    manifest = skills_manager.read_skill_pool_manifest()

    assert operator.eq(result["conflicts"], [])
    assert result["imported"] == ["americano"]
    assert manifest["skills"]["americano"]["builtin_language"] == "zh"


def test_builtin_pool_repairs_content_drift_even_when_version_is_unchanged(
    monkeypatch,
    tmp_path,
):
    """System-owned builtins must not stay stale behind an unchanged label."""
    monkeypatch.setattr(constant, "WORKING_DIR", tmp_path)
    getattr(skills_manager, "_builtin_cache").clear()

    first = skills_manager.import_builtin_skills(
        [{"skill_name": "pptx", "language": "en"}],
    )
    installed = tmp_path / "skill_pool" / "pptx" / "SKILL.md"
    expected = installed.read_text(encoding="utf-8")
    installed.write_text(
        expected + "\nSTALE LOCAL BUILTIN COPY\n",
        encoding="utf-8",
    )

    repaired = skills_manager.import_builtin_skills(
        [{"skill_name": "pptx", "language": "en"}],
    )

    assert first["imported"] == ["pptx"]
    assert repaired["updated"] == ["pptx"]
    assert operator.eq(repaired["unchanged"], [])
    assert installed.read_text(encoding="utf-8") == expected


def test_builtin_pool_repair_failure_preserves_previous_copy(
    monkeypatch,
    tmp_path,
):
    """A failed pool refresh must not erase the last usable builtin."""
    monkeypatch.setattr(constant, "WORKING_DIR", tmp_path)
    getattr(skills_manager, "_builtin_cache").clear()
    skills_manager.import_builtin_skills(
        [{"skill_name": "pptx", "language": "en"}],
    )
    installed = tmp_path / "skill_pool" / "pptx" / "SKILL.md"
    stale = installed.read_text(encoding="utf-8") + "\nSTALE COPY\n"
    installed.write_text(stale, encoding="utf-8")

    def fail_copytree(*_args, **_kwargs):
        raise OSError("simulated pool copy failure")

    monkeypatch.setattr(skills_manager.shutil, "copytree", fail_copytree)

    with pytest.raises(OSError, match="simulated pool copy failure"):
        skills_manager.import_builtin_skills(
            [{"skill_name": "pptx", "language": "en"}],
        )

    assert installed.read_text(encoding="utf-8") == stale


def test_runtime_initialization_repairs_stale_builtin_workspace_copy(
    monkeypatch,
    tmp_path,
):
    """Enabled workspaces should consume the packaged builtin update."""
    monkeypatch.setattr(constant, "WORKING_DIR", tmp_path)
    getattr(skills_manager, "_builtin_cache").clear()
    skills_manager.import_builtin_skills(
        [{"skill_name": "pptx", "language": "en"}],
    )
    workspace = tmp_path / "workspaces" / "default"
    service = skills_manager.SkillPoolService()
    installed = service.download_to_workspace("pptx", workspace)
    assert installed["success"] is True

    skill_path = workspace / "skills" / "pptx" / "SKILL.md"
    expected = (tmp_path / "skill_pool" / "pptx" / "SKILL.md").read_text(
        encoding="utf-8",
    )
    stale = _older_builtin_copy(expected)
    skill_path.write_text(stale, encoding="utf-8")
    manifest_path = workspace / "skill.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["skills"]["pptx"].update(
        {
            "enabled": False,
            "channels": ["console"],
            "config": {"quality": "strict"},
        },
    )
    manifest["skills"]["pptx"]["metadata"]["version_text"] = "2.0.0"
    manifest["skills"]["pptx"]["builtin_content_fingerprint"] = getattr(
        skills_manager,
        "_skill_tree_fingerprint",
    )(skill_path.parent)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    skills_manager.ensure_skills_initialized(workspace)

    repaired = json.loads(manifest_path.read_text(encoding="utf-8"))["skills"][
        "pptx"
    ]
    assert skill_path.read_text(encoding="utf-8") == expected
    assert repaired["enabled"] is False
    assert repaired["channels"] == ["console"]
    assert repaired["config"] == {"quality": "strict"}
    backups = list((workspace / ".skill-backups").glob("pptx-2.0.0-*"))
    assert len(backups) == 1
    assert (backups[0] / "SKILL.md").read_text(encoding="utf-8") == stale


def test_runtime_initialization_does_not_switch_legacy_builtin_language(
    monkeypatch,
    tmp_path,
):
    (
        "A legacy copy without language metadata must keep its de"
        "tected locale."
    )
    monkeypatch.setattr(constant, "WORKING_DIR", tmp_path)
    getattr(skills_manager, "_builtin_cache").clear()
    skills_manager.import_builtin_skills(
        [{"skill_name": "pptx", "language": "en"}],
    )
    workspace = tmp_path / "workspaces" / "default"
    service = skills_manager.SkillPoolService()
    assert service.download_to_workspace("pptx", workspace)["success"] is True

    skill_path = workspace / "skills" / "pptx" / "SKILL.md"
    legacy_zh = skill_path.read_text(encoding="utf-8").replace(
        "version: 3.0.0",
        "version: 2.0.0",
        1,
    )
    legacy_zh += "\n这是旧版中文内置技能的说明，必须保留语言选择。\n" * 8
    skill_path.write_text(legacy_zh, encoding="utf-8")
    manifest_path = workspace / "skill.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["skills"]["pptx"].pop("builtin_language", None)
    manifest["skills"]["pptx"]["metadata"]["version_text"] = "2.0.0"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    skills_manager.ensure_skills_initialized(workspace)

    assert skill_path.read_text(encoding="utf-8") == legacy_zh


def test_runtime_builtin_upgrade_copy_failure_preserves_previous_skill(
    monkeypatch,
    tmp_path,
):
    """A failed automatic upgrade must leave the active skill intact."""
    monkeypatch.setattr(constant, "WORKING_DIR", tmp_path)
    getattr(skills_manager, "_builtin_cache").clear()
    skills_manager.import_builtin_skills(
        [{"skill_name": "pptx", "language": "en"}],
    )
    workspace = tmp_path / "workspaces" / "default"
    service = skills_manager.SkillPoolService()
    assert service.download_to_workspace("pptx", workspace)["success"] is True
    skill_path = workspace / "skills" / "pptx" / "SKILL.md"
    stale = _older_builtin_copy(skill_path.read_text(encoding="utf-8"))
    skill_path.write_text(stale, encoding="utf-8")
    manifest_path = workspace / "skill.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["skills"]["pptx"]["metadata"]["version_text"] = "2.0.0"
    manifest["skills"]["pptx"]["builtin_content_fingerprint"] = getattr(
        skills_manager,
        "_skill_tree_fingerprint",
    )(skill_path.parent)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def fail_copytree(*_args, **_kwargs):
        raise OSError("simulated copy failure")

    monkeypatch.setattr(skills_manager.shutil, "copytree", fail_copytree)

    skills_manager.ensure_skills_initialized(workspace)

    assert skill_path.read_text(encoding="utf-8") == stale


def test_runtime_builtin_upgrade_preserves_manual_disk_edits(
    monkeypatch,
    tmp_path,
):
    (
        "A changed system copy becomes customized instead of bein"
        "g overwritten."
    )
    monkeypatch.setattr(constant, "WORKING_DIR", tmp_path)
    getattr(skills_manager, "_builtin_cache").clear()
    skills_manager.import_builtin_skills(
        [{"skill_name": "pptx", "language": "en"}],
    )
    workspace = tmp_path / "workspaces" / "default"
    service = skills_manager.SkillPoolService()
    assert service.download_to_workspace("pptx", workspace)["success"] is True
    manifest_path = workspace / "skill.json"
    installed_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert installed_manifest["skills"]["pptx"]["builtin_content_fingerprint"]

    skill_path = workspace / "skills" / "pptx" / "SKILL.md"
    customized = _older_builtin_copy(skill_path.read_text(encoding="utf-8"))
    customized += "\nManual workspace customization that must survive.\n"
    skill_path.write_text(customized, encoding="utf-8")

    skills_manager.ensure_skills_initialized(workspace)

    repaired_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert skill_path.read_text(encoding="utf-8") == customized
    assert repaired_manifest["skills"]["pptx"]["source"] == "customized"


def test_concurrent_runtime_initialization_performs_one_builtin_upgrade(
    monkeypatch,
    tmp_path,
):
    """Concurrent startup must serialize and re-check one workspace upgrade."""
    monkeypatch.setattr(constant, "WORKING_DIR", tmp_path)
    getattr(skills_manager, "_builtin_cache").clear()
    skills_manager.import_builtin_skills(
        [{"skill_name": "pptx", "language": "en"}],
    )
    workspace = tmp_path / "workspaces" / "default"
    service = skills_manager.SkillPoolService()
    assert service.download_to_workspace("pptx", workspace)["success"] is True
    skill_path = workspace / "skills" / "pptx" / "SKILL.md"
    expected = skill_path.read_text(encoding="utf-8")
    stale = _older_builtin_copy(expected)
    skill_path.write_text(stale, encoding="utf-8")
    manifest_path = workspace / "skill.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["skills"]["pptx"]["metadata"]["version_text"] = "2.0.0"
    manifest["skills"]["pptx"]["builtin_content_fingerprint"] = getattr(
        skills_manager,
        "_skill_tree_fingerprint",
    )(skill_path.parent)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    original_replace = getattr(
        skills_manager,
        "_replace_skill_dir_recoverably",
    )
    calls = 0
    calls_lock = threading.Lock()

    def slow_replace(*args, **kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.05)
        return original_replace(*args, **kwargs)

    monkeypatch.setattr(
        skills_manager,
        "_replace_skill_dir_recoverably",
        slow_replace,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _index: skills_manager.sync_workspace_builtin_upgrades(
                    workspace,
                ),
                range(2),
            ),
        )

    assert calls == 1
    assert sum(result == ["pptx"] for result in results) == 1
    assert skill_path.read_text(encoding="utf-8") == expected


def test_liaison_share_builtin_is_importable(monkeypatch, tmp_path):
    """The local sharing skill should be available to fresh installs."""
    monkeypatch.setattr(constant, "WORKING_DIR", tmp_path)
    getattr(skills_manager, "_builtin_cache").clear()

    candidates = skills_manager.list_builtin_import_candidates()
    liaison = next(
        candidate
        for candidate in candidates
        if candidate["name"] == "liaison-share"
    )

    assert set(liaison["available_languages"]) == {"en", "zh"}

    result = skills_manager.import_builtin_skills(
        [{"skill_name": "liaison-share", "language": "zh"}],
    )
    manifest = skills_manager.read_skill_pool_manifest()

    assert operator.eq(result["conflicts"], [])
    assert result["imported"] == ["liaison-share"]
    assert manifest["skills"]["liaison-share"]["builtin_language"] == "zh"


def test_official_wps_connector_skills_are_importable(monkeypatch, tmp_path):
    """KDocs and WPS Note must ship as complete official connector skills."""
    monkeypatch.setattr(constant, "WORKING_DIR", tmp_path)
    getattr(skills_manager, "_builtin_cache").clear()

    result = skills_manager.import_builtin_skills(
        [
            {"skill_name": "kdocs", "language": "zh"},
            {"skill_name": "wps-note", "language": "zh"},
        ],
    )

    assert operator.eq(result["conflicts"], [])
    assert result["imported"] == ["kdocs", "wps-note"]
    kdocs = tmp_path / "skill_pool" / "kdocs"
    wps_note = tmp_path / "skill_pool" / "wps-note"
    assert "version: 2.6.7" in (kdocs / "SKILL.md").read_text(encoding="utf-8")
    assert (kdocs / "references" / "drive" / "read_and_download.md").is_file()
    assert (wps_note / "references" / "CLI_REFERENCE.md").is_file()
    assert (wps_note / "agents" / "openai.yaml").is_file()


def test_karpathy_guidelines_builtin_is_importable(monkeypatch, tmp_path):
    """Karpathy-style coding guidance should ship as a bilingual builtin."""
    monkeypatch.setattr(constant, "WORKING_DIR", tmp_path)
    getattr(skills_manager, "_builtin_cache").clear()

    candidates = skills_manager.list_builtin_import_candidates()
    karpathy = next(
        candidate
        for candidate in candidates
        if candidate["name"] == "karpathy-guidelines"
    )

    assert set(karpathy["available_languages"]) == {"en", "zh"}

    result = skills_manager.import_builtin_skills(
        [{"skill_name": "karpathy-guidelines", "language": "zh"}],
    )
    manifest = skills_manager.read_skill_pool_manifest()
    content = (
        tmp_path / "skill_pool" / "karpathy-guidelines" / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert operator.eq(result["conflicts"], [])
    assert result["imported"] == ["karpathy-guidelines"]
    assert (
        manifest["skills"]["karpathy-guidelines"]["builtin_language"] == "zh"
    )
    assert "目标驱动执行" in content
    assert "multica-ai/andrej-karpathy-skills" in content
    assert 'builtin_skill_version: "1.2"' in content
    assert "动手前的精简阶梯" in content
    assert "DietrichGebert/ponytail" in content


def test_karpathy_guidelines_builtin_keeps_pony_tail_safety_floor():
    """Both language variants must retain the compact-code safety rules."""
    skill_root = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "lantai"
        / "agents"
        / "skills"
    )
    expected = {
        "karpathy-guidelines-zh": (
            "动手前的精简阶梯",
            "信任边界验证",
            "至少保留一条能运行的验证",
        ),
        "karpathy-guidelines-en": (
            "The Simplification Ladder",
            "trust-boundary validation",
            "at least one runnable check",
        ),
    }

    for directory, phrases in expected.items():
        content = (skill_root / directory / "SKILL.md").read_text(
            encoding="utf-8",
        )
        post = frontmatter.loads(content)
        metadata = post.get("metadata") or {}

        assert str(metadata.get("builtin_skill_version")) == "1.2"
        assert metadata.get("influenced_by", {}).get("name") == (
            "DietrichGebert/ponytail"
        )
        for phrase in phrases:
            assert phrase in content


def test_lean_code_review_builtin_is_importable(monkeypatch, tmp_path):
    """The optional complexity-only review must ship in both languages."""
    monkeypatch.setattr(constant, "WORKING_DIR", tmp_path)
    getattr(skills_manager, "_builtin_cache").clear()

    candidates = skills_manager.list_builtin_import_candidates()
    review = next(
        candidate
        for candidate in candidates
        if candidate["name"] == "lean-code-review"
    )

    assert set(review["available_languages"]) == {"en", "zh"}

    result = skills_manager.import_builtin_skills(
        [{"skill_name": "lean-code-review", "language": "zh"}],
    )
    content = (
        tmp_path / "skill_pool" / "lean-code-review" / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert operator.eq(result["conflicts"], [])
    assert result["imported"] == ["lean-code-review"]
    assert "只找可删除的复杂度" in content
    assert "不直接修改代码" in content


def test_kdocs_builtin_preserves_official_runtime_resources(
    monkeypatch,
    tmp_path,
):
    (
        "KDocs must ship as a complete builtin, including setup a"
        "nd references."
    )
    monkeypatch.setattr(constant, "WORKING_DIR", tmp_path)
    getattr(skills_manager, "_builtin_cache").clear()

    candidates = skills_manager.list_builtin_import_candidates()
    kdocs = next(
        candidate for candidate in candidates if candidate["name"] == "kdocs"
    )

    assert kdocs["available_languages"] == ["zh"]

    result = skills_manager.import_builtin_skills(
        [{"skill_name": "kdocs", "language": "zh"}],
    )
    installed = tmp_path / "skill_pool" / "kdocs"
    content = (installed / "SKILL.md").read_text(encoding="utf-8")
    name, description = getattr(skills_manager, "_validate_skill_content")(
        content,
    )

    assert operator.eq(result["conflicts"], [])
    assert result["imported"] == ["kdocs"]
    assert name == "kdocs"
    assert len(description) >= 30
    assert "[TODO" not in content
    assert (installed / "scripts" / "setup.sh").is_file()
    assert (installed / "scripts" / "setup.ps1").is_file()
    assert (installed / "references" / "auth.md").is_file()
    assert (installed / "references" / "drive.md").is_file()
