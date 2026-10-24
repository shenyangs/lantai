# -*- coding: utf-8 -*-
"""Behavior contract for user-owned persona workspace files."""

import hashlib
from pathlib import Path

import pytest

from lantai.agents.persona import workspace_sync as workspace_sync_module
from lantai.agents.persona.workspace_sync import (
    plan_persona_workspace_templates,
    rollback_persona_workspace_template,
    sync_persona_workspace_templates,
)


_OFFICIAL_V2_HASHES = {
    "zh": {
        "AGENTS.md": "4e969bf4260e2533f244aa4422491d9eb29632fc9652da1b063bf80728a451ab",
        "SOUL.md": "51e1be7b0cdfe64f838351367cee1ee902cc456df11eaaebf7b67e98b8c5e19b",
        "PROFILE.md": "06a609b8645bc0495ece008ad25d1d6837db1072e19930b5039b316fc5a839a3",
        "BACKSTORY.md": "7d7c6b11051d7359c5ad621f6ab60f298df21ad026faf4472b969975d52caa0b",
        "SELF_CANON.md": "e3b1a8cb9b90c2a4354288e2bda6adddfc995dd935527d4d68677ac4bdd0808a",
        "WORKBENCH.md": "a74d29b83983a67698368843ee899139a48554787368c0bbb98cc344c960abf3",
        "VISUAL_CANON.md": "0e48d4381fd2130604c451e945b778a7c81fb67b3806bb8edb3077ecaa782fde",
    },
    "en": {
        "AGENTS.md": "1d6c99499baff4857519cbcec9ab0fb246214dd12d99db065ae5eae9249d1572",
        "SOUL.md": "5cb2e8623cb38d78bd2f723b7c9f4b5a54f11fa969339c38f6ec158066070b10",
        "PROFILE.md": "4174b74a89ef70b8718adab05e707fd0f57d14223a2d30dbda50277f373003bc",
        "BACKSTORY.md": "a8fd0b9516b84605191761a222928fe289dab62a8a60e391e6d4eeeba6eab86d",
        "SELF_CANON.md": "2bf9ee84e99129eb77055d099b9dec6b528dc0946e7c45634fb06635f382e0ce",
        "WORKBENCH.md": "d832bef3756a5cbd710857eb4eba1ef1311cc998773946e64af2c64d25a0dab8",
        "VISUAL_CANON.md": "9180c0dce600c18dba7a467bee661eab138f51ee119790532f93f10cd1352014",
    },
}


def test_sync_recognizes_every_unmodified_official_v2_persona_template():
    for language, templates in _OFFICIAL_V2_HASHES.items():
        for filename, digest in templates.items():
            assert digest in workspace_sync_module._LEGACY_OFFICIAL_TEMPLATE_SHA256[
                language
            ][filename]


def test_plan_is_a_dry_run_and_classifies_copy_upgrade_and_preserve(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "zh"
    workspace = tmp_path / "workspace"
    source.mkdir()
    workspace.mkdir()
    legacy = '---\npersona_version: "2.0.0"\n---\n官方旧版\n'
    candidate = '---\npersona_version: "2.1.0"\n---\n官方新版\n'
    custom = "用户自己的资料\n"
    (source / "SOUL.md").write_text(candidate, encoding="utf-8")
    (source / "PROFILE.md").write_text(candidate, encoding="utf-8")
    (source / "WORKBENCH.md").write_text(candidate, encoding="utf-8")
    (workspace / "SOUL.md").write_text(legacy, encoding="utf-8")
    (workspace / "PROFILE.md").write_text(custom, encoding="utf-8")
    legacy_hash = hashlib.sha256(legacy.encode("utf-8")).hexdigest()
    monkeypatch.setattr(
        workspace_sync_module,
        "_LEGACY_OFFICIAL_TEMPLATE_SHA256",
        {"zh": {"SOUL.md": frozenset({legacy_hash})}},
    )
    before = {
        path.name: path.read_bytes()
        for path in workspace.iterdir()
    }

    plan = plan_persona_workspace_templates(source, workspace)

    assert [(item.filename, item.action, item.reason) for item in plan] == [
        ("PROFILE.md", "preserve", "current-or-user-owned"),
        ("SOUL.md", "upgrade", "known-official-template"),
        ("WORKBENCH.md", "copy", "missing"),
    ]
    soul = plan[1]
    assert soul.destination_sha256 == legacy_hash
    assert soul.target_version == "2.1.0"
    assert soul.backup_name == ".SOUL.md.pre-persona-2.1.0.bak"
    assert {
        path.name: path.read_bytes()
        for path in workspace.iterdir()
    } == before
    assert not (workspace / "WORKBENCH.md").exists()
    assert not any(path.name.endswith(".bak") for path in workspace.iterdir())


def test_sync_preserves_existing_user_persona_and_only_fills_missing_files(
    tmp_path,
):
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    source.mkdir()
    workspace.mkdir()
    (source / "SOUL.md").write_text("内置灵魂", encoding="utf-8")
    (source / "PROFILE.md").write_text("内置资料", encoding="utf-8")
    (workspace / "SOUL.md").write_text(
        "用户已确认的人格增量",
        encoding="utf-8",
    )

    result = sync_persona_workspace_templates(source, workspace)

    assert (workspace / "SOUL.md").read_text(encoding="utf-8") == (
        "用户已确认的人格增量"
    )
    assert (workspace / "PROFILE.md").read_text(encoding="utf-8") == "内置资料"
    assert result.copied == ("PROFILE.md",)
    assert result.upgraded == ()
    assert result.preserved == ("SOUL.md",)
    assert result.backups == ()


def test_sync_upgrades_only_an_exact_known_official_template_and_keeps_backup(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "zh"
    workspace = tmp_path / "workspace"
    source.mkdir()
    workspace.mkdir()
    legacy = "官方旧版人格\n"
    current = (
        '---\npersona_version: "2.0.0"\nsummary: "新版"\n---\n\n'
        "温柔、善良、明亮、坦诚、耐心、好奇、可靠。\n"
    )
    (source / "SOUL.md").write_text(current, encoding="utf-8")
    (workspace / "SOUL.md").write_text(legacy, encoding="utf-8")
    legacy_hash = hashlib.sha256(legacy.encode("utf-8")).hexdigest()
    monkeypatch.setattr(
        workspace_sync_module,
        "_LEGACY_OFFICIAL_TEMPLATE_SHA256",
        {"zh": {"SOUL.md": frozenset({legacy_hash})}},
    )

    result = sync_persona_workspace_templates(source, workspace)

    backup = workspace / ".SOUL.md.pre-persona-2.0.0.bak"
    assert (workspace / "SOUL.md").read_text(encoding="utf-8") == current
    assert backup.read_text(encoding="utf-8") == legacy
    assert result.copied == ()
    assert result.upgraded == ("SOUL.md",)
    assert result.preserved == ()
    assert result.backups == (backup.name,)

    second = sync_persona_workspace_templates(source, workspace)
    assert second.upgraded == ()
    assert second.preserved == ("SOUL.md",)
    assert second.backups == ()


def test_sync_never_upgrades_a_user_modified_template_even_with_v2_available(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "zh"
    workspace = tmp_path / "workspace"
    source.mkdir()
    workspace.mkdir()
    official_legacy = "官方旧版人格\n"
    customized = official_legacy + "用户自己加的一句话\n"
    current = '---\npersona_version: "2.0.0"\n---\n新版人格\n'
    (source / "SOUL.md").write_text(current, encoding="utf-8")
    (workspace / "SOUL.md").write_text(customized, encoding="utf-8")
    official_hash = hashlib.sha256(official_legacy.encode("utf-8")).hexdigest()
    monkeypatch.setattr(
        workspace_sync_module,
        "_LEGACY_OFFICIAL_TEMPLATE_SHA256",
        {"zh": {"SOUL.md": frozenset({official_hash})}},
    )

    result = sync_persona_workspace_templates(source, workspace)

    assert (workspace / "SOUL.md").read_text(encoding="utf-8") == customized
    assert not (workspace / ".SOUL.md.pre-persona-2.0.0.bak").exists()
    assert result.upgraded == ()
    assert result.preserved == ("SOUL.md",)


def test_sync_never_replaces_a_profile_with_user_data(tmp_path, monkeypatch):
    source = tmp_path / "zh"
    workspace = tmp_path / "workspace"
    source.mkdir()
    workspace.mkdir()
    official = '---\npersona_version: "2.0.0"\n---\n名字：\n'
    personalized = official + "名字：小林\n"
    candidate = '---\npersona_version: "2.1.0"\n---\n名字：\n'
    (source / "PROFILE.md").write_text(candidate, encoding="utf-8")
    (workspace / "PROFILE.md").write_text(personalized, encoding="utf-8")
    monkeypatch.setattr(
        workspace_sync_module,
        "_LEGACY_OFFICIAL_TEMPLATE_SHA256",
        {
            "zh": {
                "PROFILE.md": frozenset(
                    {hashlib.sha256(official.encode("utf-8")).hexdigest()},
                ),
            },
        },
    )

    result = sync_persona_workspace_templates(source, workspace)

    assert (workspace / "PROFILE.md").read_text(encoding="utf-8") == personalized
    assert result.preserved == ("PROFILE.md",)
    assert result.backups == ()


def test_sync_preserves_a_concurrent_user_save_during_official_upgrade(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "zh"
    workspace = tmp_path / "workspace"
    source.mkdir()
    workspace.mkdir()
    legacy = '---\npersona_version: "2.0.0"\n---\n官方旧版\n'
    candidate = '---\npersona_version: "2.1.0"\n---\n官方新版\n'
    user_save = legacy + "用户并发保存\n"
    template = source / "SOUL.md"
    destination = workspace / "SOUL.md"
    template.write_text(candidate, encoding="utf-8")
    destination.write_text(legacy, encoding="utf-8")
    monkeypatch.setattr(
        workspace_sync_module,
        "_LEGACY_OFFICIAL_TEMPLATE_SHA256",
        {
            "zh": {
                "SOUL.md": frozenset(
                    {hashlib.sha256(legacy.encode("utf-8")).hexdigest()},
                ),
            },
        },
    )
    original_copy2 = workspace_sync_module.shutil.copy2

    def concurrent_save_after_staging(src, dst, *args, **kwargs):
        copied = original_copy2(src, dst, *args, **kwargs)
        if Path(src) == template and str(dst).endswith(".tmp"):
            destination.write_text(user_save, encoding="utf-8")
        return copied

    monkeypatch.setattr(
        workspace_sync_module.shutil,
        "copy2",
        concurrent_save_after_staging,
    )

    result = sync_persona_workspace_templates(source, workspace)

    assert destination.read_text(encoding="utf-8") == user_save
    assert result.upgraded == ()
    assert result.preserved == ("SOUL.md",)


def test_sync_preserves_a_file_created_concurrently_while_filling_missing(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "zh"
    workspace = tmp_path / "workspace"
    source.mkdir()
    workspace.mkdir()
    template = source / "SOUL.md"
    destination = workspace / "SOUL.md"
    template.write_text("官方模板\n", encoding="utf-8")
    original_copy2 = workspace_sync_module.shutil.copy2

    def concurrent_create_after_staging(src, dst, *args, **kwargs):
        copied = original_copy2(src, dst, *args, **kwargs)
        if Path(src) == template and str(dst).endswith(".tmp"):
            destination.write_text("用户刚创建的文件\n", encoding="utf-8")
        return copied

    monkeypatch.setattr(
        workspace_sync_module.shutil,
        "copy2",
        concurrent_create_after_staging,
    )

    result = sync_persona_workspace_templates(source, workspace)

    assert destination.read_text(encoding="utf-8") == "用户刚创建的文件\n"
    assert result.copied == ()
    assert result.preserved == ("SOUL.md",)


def test_language_switch_never_replaces_an_existing_other_language_template(
    tmp_path,
    monkeypatch,
):
    zh_source = tmp_path / "zh"
    en_source = tmp_path / "en"
    workspace = tmp_path / "workspace"
    zh_source.mkdir()
    en_source.mkdir()
    workspace.mkdir()
    zh_legacy = '---\npersona_version: "2.0.0"\n---\n中文官方旧版\n'
    en_legacy = '---\npersona_version: "2.0.0"\n---\nEnglish official v2\n'
    en_candidate = '---\npersona_version: "2.1.0"\n---\nEnglish candidate\n'
    (en_source / "SOUL.md").write_text(en_candidate, encoding="utf-8")
    (workspace / "SOUL.md").write_text(zh_legacy, encoding="utf-8")
    monkeypatch.setattr(
        workspace_sync_module,
        "_LEGACY_OFFICIAL_TEMPLATE_SHA256",
        {
            "zh": {
                "SOUL.md": frozenset(
                    {hashlib.sha256(zh_legacy.encode("utf-8")).hexdigest()},
                ),
            },
            "en": {
                "SOUL.md": frozenset(
                    {hashlib.sha256(en_legacy.encode("utf-8")).hexdigest()},
                ),
            },
        },
    )

    plan = plan_persona_workspace_templates(en_source, workspace)
    result = sync_persona_workspace_templates(en_source, workspace)

    assert [(item.action, item.reason) for item in plan] == [
        ("preserve", "current-or-user-owned"),
    ]
    assert result.preserved == ("SOUL.md",)
    assert result.upgraded == ()
    assert result.backups == ()
    assert (workspace / "SOUL.md").read_text(encoding="utf-8") == zh_legacy


def test_interrupted_backup_publish_keeps_original_and_can_be_retried(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "zh"
    workspace = tmp_path / "workspace"
    source.mkdir()
    workspace.mkdir()
    legacy = '---\npersona_version: "2.0.0"\n---\n官方旧版\n'
    candidate = '---\npersona_version: "2.1.0"\n---\n官方新版\n'
    (source / "SOUL.md").write_text(candidate, encoding="utf-8")
    (workspace / "SOUL.md").write_text(legacy, encoding="utf-8")
    monkeypatch.setattr(
        workspace_sync_module,
        "_LEGACY_OFFICIAL_TEMPLATE_SHA256",
        {
            "zh": {
                "SOUL.md": frozenset(
                    {hashlib.sha256(legacy.encode("utf-8")).hexdigest()},
                ),
            },
        },
    )
    original_link = workspace_sync_module.os.link

    def interrupt_backup_publish(src, dst):
        raise OSError("simulated backup interruption")

    monkeypatch.setattr(
        workspace_sync_module.os,
        "link",
        interrupt_backup_publish,
    )

    with pytest.raises(OSError, match="simulated backup interruption"):
        sync_persona_workspace_templates(source, workspace)

    backup = workspace / ".SOUL.md.pre-persona-2.1.0.bak"
    assert (workspace / "SOUL.md").read_text(encoding="utf-8") == legacy
    assert not backup.exists()
    assert not list(workspace.glob(".*.tmp"))

    monkeypatch.setattr(workspace_sync_module.os, "link", original_link)
    retried = sync_persona_workspace_templates(source, workspace)
    assert retried.upgraded == ("SOUL.md",)
    assert backup.read_text(encoding="utf-8") == legacy
    assert (workspace / "SOUL.md").read_text(encoding="utf-8") == candidate


def test_interrupted_upgrade_keeps_original_and_can_be_retried(tmp_path, monkeypatch):
    source = tmp_path / "zh"
    workspace = tmp_path / "workspace"
    source.mkdir()
    workspace.mkdir()
    legacy = '---\npersona_version: "2.0.0"\n---\n官方旧版\n'
    candidate = '---\npersona_version: "2.1.0"\n---\n官方新版\n'
    (source / "SOUL.md").write_text(candidate, encoding="utf-8")
    (workspace / "SOUL.md").write_text(legacy, encoding="utf-8")
    monkeypatch.setattr(
        workspace_sync_module,
        "_LEGACY_OFFICIAL_TEMPLATE_SHA256",
        {
            "zh": {
                "SOUL.md": frozenset(
                    {hashlib.sha256(legacy.encode("utf-8")).hexdigest()},
                ),
            },
        },
    )
    original_replace = workspace_sync_module.os.replace

    def interrupt_install(src, dst):
        if dst == workspace / "SOUL.md":
            raise OSError("simulated interruption")
        return original_replace(src, dst)

    monkeypatch.setattr(workspace_sync_module.os, "replace", interrupt_install)

    with pytest.raises(OSError, match="simulated interruption"):
        sync_persona_workspace_templates(source, workspace)

    backup = workspace / ".SOUL.md.pre-persona-2.1.0.bak"
    assert (workspace / "SOUL.md").read_text(encoding="utf-8") == legacy
    assert backup.read_text(encoding="utf-8") == legacy

    monkeypatch.setattr(workspace_sync_module.os, "replace", original_replace)
    retried = sync_persona_workspace_templates(source, workspace)
    assert retried.upgraded == ("SOUL.md",)
    assert (workspace / "SOUL.md").read_text(encoding="utf-8") == candidate


def test_partial_file_group_upgrade_converges_on_retry(tmp_path, monkeypatch):
    source = tmp_path / "zh"
    workspace = tmp_path / "workspace"
    source.mkdir()
    workspace.mkdir()
    legacy_profile = '---\npersona_version: "2.0.0"\n---\n旧资料模板\n'
    legacy_soul = '---\npersona_version: "2.0.0"\n---\n旧灵魂模板\n'
    candidate_profile = '---\npersona_version: "2.1.0"\n---\n新资料模板\n'
    candidate_soul = '---\npersona_version: "2.1.0"\n---\n新灵魂模板\n'
    (source / "PROFILE.md").write_text(candidate_profile, encoding="utf-8")
    (source / "SOUL.md").write_text(candidate_soul, encoding="utf-8")
    (workspace / "PROFILE.md").write_text(legacy_profile, encoding="utf-8")
    (workspace / "SOUL.md").write_text(legacy_soul, encoding="utf-8")
    monkeypatch.setattr(
        workspace_sync_module,
        "_LEGACY_OFFICIAL_TEMPLATE_SHA256",
        {
            "zh": {
                "PROFILE.md": frozenset(
                    {hashlib.sha256(legacy_profile.encode("utf-8")).hexdigest()},
                ),
                "SOUL.md": frozenset(
                    {hashlib.sha256(legacy_soul.encode("utf-8")).hexdigest()},
                ),
            },
        },
    )
    original_replace = workspace_sync_module.os.replace

    def interrupt_second_install(src, dst):
        if dst == workspace / "SOUL.md":
            raise OSError("simulated second-file interruption")
        return original_replace(src, dst)

    monkeypatch.setattr(
        workspace_sync_module.os,
        "replace",
        interrupt_second_install,
    )

    with pytest.raises(OSError, match="second-file interruption"):
        sync_persona_workspace_templates(source, workspace)

    assert (workspace / "PROFILE.md").read_text(encoding="utf-8") == (
        candidate_profile
    )
    assert (workspace / "SOUL.md").read_text(encoding="utf-8") == legacy_soul

    monkeypatch.setattr(workspace_sync_module.os, "replace", original_replace)
    retried = sync_persona_workspace_templates(source, workspace)

    assert retried.upgraded == ("SOUL.md",)
    assert retried.preserved == ("PROFILE.md",)
    assert (workspace / "PROFILE.md").read_text(encoding="utf-8") == (
        candidate_profile
    )
    assert (workspace / "SOUL.md").read_text(encoding="utf-8") == candidate_soul
    assert sorted(retried.backups) == []
    assert len(list(workspace.glob(".*.bak"))) == 2


def test_rollback_requires_the_exact_installed_hash_and_preserves_later_user_edits(
    tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    installed = "官方新版\n"
    previous = "官方旧版\n"
    destination = workspace / "SOUL.md"
    backup = workspace / ".SOUL.md.pre-persona-2.1.0.bak"
    destination.write_text(installed, encoding="utf-8")
    backup.write_text(previous, encoding="utf-8")
    installed_hash = hashlib.sha256(installed.encode("utf-8")).hexdigest()

    restored = rollback_persona_workspace_template(
        workspace,
        "SOUL.md",
        installed_sha256=installed_hash,
        backup_name=backup.name,
    )

    assert restored.restored is True
    assert destination.read_text(encoding="utf-8") == previous

    destination.write_text(installed + "用户后续修改\n", encoding="utf-8")
    refused = rollback_persona_workspace_template(
        workspace,
        "SOUL.md",
        installed_sha256=installed_hash,
        backup_name=backup.name,
    )
    assert refused.restored is False
    assert refused.reason == "destination-changed"
    assert destination.read_text(encoding="utf-8") == installed + "用户后续修改\n"


def test_sync_upgrades_known_product_guide_but_preserves_user_edits(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "zh"
    workspace = tmp_path / "workspace"
    source.mkdir()
    workspace.mkdir()
    legacy = "官方旧版产品手册\n"
    current = '---\ntemplate_version: "2.0.0"\n---\n新版能力目录\n'
    (source / "PRODUCT_GUIDE.md").write_text(current, encoding="utf-8")
    (workspace / "PRODUCT_GUIDE.md").write_text(legacy, encoding="utf-8")
    legacy_hash = hashlib.sha256(legacy.encode("utf-8")).hexdigest()
    monkeypatch.setattr(
        workspace_sync_module,
        "_LEGACY_OFFICIAL_TEMPLATE_SHA256",
        {"zh": {"PRODUCT_GUIDE.md": frozenset({legacy_hash})}},
    )

    result = sync_persona_workspace_templates(source, workspace)

    backup = workspace / ".PRODUCT_GUIDE.md.pre-template-2.0.0.bak"
    assert (workspace / "PRODUCT_GUIDE.md").read_text(encoding="utf-8") == current
    assert backup.read_text(encoding="utf-8") == legacy
    assert result.upgraded == ("PRODUCT_GUIDE.md",)

    (workspace / "PRODUCT_GUIDE.md").write_text(
        current + "用户补充\n",
        encoding="utf-8",
    )
    newer = '---\ntemplate_version: "2.1.0"\n---\n更新能力目录\n'
    (source / "PRODUCT_GUIDE.md").write_text(newer, encoding="utf-8")
    preserved = sync_persona_workspace_templates(source, workspace)
    assert preserved.upgraded == ()
    assert preserved.preserved == ("PRODUCT_GUIDE.md",)
