from pathlib import Path

from lantai.agents.role_templates import (
    ROLE_TEMPLATE_FILE_NAME,
    ROLE_TEMPLATE_MARKER_END,
    ROLE_TEMPLATE_MARKER_START,
    apply_role_template_to_workspace,
    get_role_template,
    list_role_templates,
    parse_role_template_markdown,
)


def test_list_builtin_role_templates_is_chinese_first():
    templates = list_role_templates()

    assert len(templates) >= 6
    assert any(template.id == "engineering-code-reviewer" for template in templates)
    assert all(template.name for template in templates)
    assert all(template.description for template in templates)


def test_octop_inspired_scenario_templates_reuse_existing_skills():
    templates = {template.id: template for template in list_role_templates()}

    assert {
        "file-delivery-specialist",
        "connector-integration-specialist",
        "evidence-competitive-researcher",
    } <= templates.keys()
    assert templates["file-delivery-specialist"].recommended_skills == [
        "file_reader",
        "pdf",
        "docx",
        "lantai-office-documents",
    ]
    assert "OAuth" in get_role_template("connector-integration-specialist").body


def test_builtin_role_templates_follow_the_current_single_release_contract():
    bodies = "\n".join(
        get_role_template(template.id).body for template in list_role_templates()
    )

    assert "双发行版" not in bodies
    assert "App Store 沙盒" not in bodies
    assert "TestFlight" not in bodies


def test_parse_markdown_role_template_frontmatter():
    template = parse_role_template_markdown(
        """---
name: 前端体验审查员
description: 检查界面信息层级和中文文案。
category: 设计
tags:
  - UI
  - 中文
recommended_skills:
  - browser_visible
  - guidance
color: "#5aa681"
---
# 前端体验审查员

## 使命
检查界面层级、空状态、错误状态和中文文案。

## 输出
给出问题清单和可验收建议。

## 边界
不做无关重设计。
""",
        source_name="ux-reviewer.md",
    )

    assert template.name == "前端体验审查员"
    assert template.category == "设计"
    assert template.tags == ["UI", "中文"]
    assert template.recommended_skills == ["browser_visible", "guidance"]
    assert template.suggested_agent_id.startswith("agent-")
    assert {check.status for check in template.checks} <= {
        "pass",
        "warning",
        "error",
    }
    assert all(check.status != "error" for check in template.checks)


def test_apply_role_template_to_workspace_replaces_prompt_section(tmp_path: Path):
    template = get_role_template("engineering-code-reviewer")
    soul_path = tmp_path / "SOUL.md"
    soul_path.write_text("# SOUL\n\n原有人格内容。\n", encoding="utf-8")

    apply_role_template_to_workspace(tmp_path, template)
    first_soul = soul_path.read_text(encoding="utf-8")
    first_template_file = (tmp_path / ROLE_TEMPLATE_FILE_NAME).read_text(
        encoding="utf-8",
    )

    assert ROLE_TEMPLATE_MARKER_START in first_soul
    assert ROLE_TEMPLATE_MARKER_END in first_soul
    assert "专业角色模板：代码审查员" in first_soul
    assert "原有人格内容" in first_soul
    assert "代码审查员" in first_template_file

    second_template = get_role_template("qa-test-planner")
    apply_role_template_to_workspace(tmp_path, second_template)
    second_soul = soul_path.read_text(encoding="utf-8")

    assert second_soul.count(ROLE_TEMPLATE_MARKER_START) == 1
    assert "专业角色模板：测试与验收规划师" in second_soul
    assert "专业角色模板：代码审查员" not in second_soul
