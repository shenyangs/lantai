# -*- coding: utf-8 -*-
"""Integration boundary for default-agent persona template migration."""

from pathlib import Path

import lantai

from lantai.app import migration


def test_default_persona_refresh_preserves_existing_files(tmp_path):
    workspace = tmp_path / "default-agent"
    workspace.mkdir()
    soul = workspace / "SOUL.md"
    soul.write_text("用户自己的沈行知增量", encoding="utf-8")

    migration._refresh_default_agent_md_files(  # pylint: disable=protected-access
        workspace,
        "zh",
    )

    assert soul.read_text(encoding="utf-8") == "用户自己的沈行知增量"
    assert (workspace / "PROFILE.md").exists()
    assert (workspace / "SELF_CANON.md").exists()
    assert (workspace / "VISUAL_CANON.md").exists()


def test_default_persona_refresh_preserves_intentionally_blank_file(tmp_path):
    workspace = tmp_path / "default-agent"
    workspace.mkdir()
    soul = workspace / "SOUL.md"
    soul.write_bytes(b"")

    migration._refresh_default_agent_md_files(  # pylint: disable=protected-access
        workspace,
        "zh",
    )

    assert soul.read_bytes() == b""


def test_default_persona_refresh_does_not_touch_sibling_workspace(tmp_path):
    default_workspace = tmp_path / "default-agent"
    other_workspace = tmp_path / "research-agent"
    default_workspace.mkdir()
    other_workspace.mkdir()
    other_soul = other_workspace / "SOUL.md"
    other_soul.write_text("另一个 Agent 的自定义人格", encoding="utf-8")

    migration._refresh_default_agent_md_files(  # pylint: disable=protected-access
        default_workspace,
        "zh",
    )

    assert other_soul.read_text(encoding="utf-8") == "另一个 Agent 的自定义人格"
    assert sorted(path.name for path in other_workspace.iterdir()) == ["SOUL.md"]


def test_default_persona_refresh_uses_existing_russian_assets(tmp_path):
    workspace = tmp_path / "default-agent"
    workspace.mkdir()
    agents_root = Path(lantai.__file__).parent / "agents"

    migration._refresh_default_agent_md_files(  # pylint: disable=protected-access
        workspace,
        "ru",
    )

    assert (workspace / "SOUL.md").read_bytes() == (
        agents_root / "md_files" / "ru" / "SOUL.md"
    ).read_bytes()
    assert not (workspace / "BACKSTORY.md").exists()


def test_obsolete_harness_prompt_migration_is_surgical(tmp_path):
    workspace = tmp_path / "default-agent"
    workspace.mkdir()
    workbench = workspace / "WORKBENCH.md"
    workbench.write_text(
        "用户自定义开头\n"
        "1. 默认采用标准协作：复杂或专业任务优先咨询 1 个最匹配的子 Agent，再由你整合交付。\n"
        "2. 小事不调度，直接做；但代码、调试、前端/设计、架构、构建/部署、研究、风险、策略、文案、复核任务，除非很简单，否则应调度。\n"
        "用户自定义结尾\n",
        encoding="utf-8",
    )

    changed = migration._migrate_obsolete_harness_prompt_rules(  # pylint: disable=protected-access
        workspace,
    )

    content = workbench.read_text(encoding="utf-8")
    assert changed == ["WORKBENCH.md"]
    assert "用户自定义开头" in content
    assert "用户自定义结尾" in content
    assert "不在每条消息前做隐藏的简单/复杂分类" in content
    assert "否则应调度" not in content


def test_obsolete_workbench_language_does_not_make_delivery_the_default(tmp_path):
    workspace = tmp_path / "default-agent"
    workspace.mkdir()
    workbench = workspace / "WORKBENCH.md"
    workbench.write_text(
        "用户自定义开头\n"
        "最终判断与交付仍由你统一负责。\n"
        "## 交付格式\n"
        "用户自定义结尾\n",
        encoding="utf-8",
    )

    changed = migration._migrate_obsolete_harness_prompt_rules(  # pylint: disable=protected-access
        workspace,
    )

    content = workbench.read_text(encoding="utf-8")
    assert changed == ["WORKBENCH.md"]
    assert "用户自定义开头" in content
    assert "用户自定义结尾" in content
    assert "最终判断与回应" in content
    assert "## 回应方式" in content
    assert "## 交付格式" not in content


def test_obsolete_local_classifier_template_is_replaced(tmp_path):
    workspace = tmp_path / "local-agent"
    workspace.mkdir()
    soul = workspace / "SOUL.md"
    soul.write_text(
        "# SOUL\n- **收到请求后，先对任务进行分类，并回复用户分类结果**\n",
        encoding="utf-8",
    )

    changed = migration._migrate_obsolete_harness_prompt_rules(  # pylint: disable=protected-access
        workspace,
    )

    content = soul.read_text(encoding="utf-8")
    assert changed == ["SOUL.md"]
    assert "不要在每条消息前执行或公开报告" in content
    assert "先对任务进行分类" not in content


def test_obsolete_default_soul_does_not_turn_history_into_a_new_task(tmp_path):
    workspace = tmp_path / "default-agent"
    workspace.mkdir()
    soul = workspace / "SOUL.md"
    soul.write_text(
        "用户自定义开头\n"
        "_你不是聊天机器人。你是澜台里的默认工作伙伴沈行知。澜台是系统与工作台，你负责温度、判断和交付。_\n"
        "每次会话都全新醒来。你面前有来源的资料、记忆、未完成事项和产物版本让你持续存在。读它们，但不把一次性状态或未确认推断写成长期人格。\n"
        "用户自定义结尾\n",
        encoding="utf-8",
    )

    changed = migration._migrate_obsolete_harness_prompt_rules(  # pylint: disable=protected-access
        workspace,
    )

    content = soul.read_text(encoding="utf-8")
    assert changed == ["SOUL.md"]
    assert "用户自定义开头" in content
    assert "用户自定义结尾" in content
    assert "只有用户真实需要行动时" in content
    assert "不要把历史任务" in content
    assert "恢复同一个主体" in content
    assert "全新醒来" not in content
    assert "未完成事项和产物版本" not in content


def test_obsolete_complex_workflow_does_not_force_every_message_into_delivery(
    tmp_path,
):
    workspace = tmp_path / "default-agent"
    workspace.mkdir()
    agents = workspace / "AGENTS.md"
    agents.write_text(
        "用户自定义开头\n"
        "你处理复杂任务时，默认采用下面的顺序：\n\n"
        "1. 判断用户真正要什么：结论、方案、文本、执行、分析、传播、陪伴。\n"
        "2. 确认约束：时间、格式、受众、风险、素材、交付物。\n"
        "3. 拆结构：问题、事实、判断、行动。\n"
        "4. 必要时调动工作台里的专业子 Agent。\n"
        "5. 整合子 Agent 结果，由你统一给最终判断和交付。\n"
        "6. 先交付最有用的部分。\n"
        "7. 必要时补充备选方案。\n"
        "8. 最后给下一步建议，但不强迫用户继续。\n"
        "用户自定义结尾\n",
        encoding="utf-8",
    )

    changed = migration._migrate_obsolete_harness_prompt_rules(  # pylint: disable=protected-access
        workspace,
    )

    content = agents.read_text(encoding="utf-8")
    assert changed == ["AGENTS.md"]
    assert "用户自定义开头" in content
    assert "用户自定义结尾" in content
    assert "普通闲聊、问答和边界清楚的小请求直接回答" in content
    assert "素材、交付物" not in content
    assert "统一给最终判断和交付" not in content
