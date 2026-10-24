# -*- coding: utf-8 -*-
"""Single-source capability catalog contracts."""

from pathlib import Path

from lantai.runtime.capability_catalog import (
    PRODUCT_CAPABILITIES,
    PROGRESSIVE_TOOL_GROUPS,
    progressive_tool_group_for,
    render_product_capability_reference,
)


def test_product_catalog_covers_current_rich_surfaces() -> None:
    ids = {item.id for item in PRODUCT_CAPABILITIES}

    assert {
        "chat",
        "works",
        "browser-workbench",
        "mac-use",
        "office-co-creation",
        "image-generation",
        "web-search",
        "hosted-collaboration",
        "customization-studio",
        "mcp-connectors",
        "recovery",
    } <= ids
    assert "deepseek-quick-search" not in ids
    assert "deepseek_quick_search" not in PROGRESSIVE_TOOL_GROUPS["research"][1]
    assert "task-center-v2" not in ids
    assert {"files", "browser", "research", "collaboration"} <= set(
        PROGRESSIVE_TOOL_GROUPS
    )
    assert "computer" in PROGRESSIVE_TOOL_GROUPS


def test_product_reference_is_generated_from_catalog() -> None:
    rendered = render_product_capability_reference("zh")

    assert "<!-- lantai:capability-catalog:start -->" in rendered
    assert "Browser Workbench" in rendered
    assert "作品" in rendered
    assert "协作室" in rendered
    assert "任务中心" not in rendered
    assert "/task-center" not in rendered
    assert "<!-- lantai:capability-catalog:end -->" in rendered


def test_packaged_product_guide_embeds_exact_generated_catalog() -> None:
    guide = (
        Path(__file__).parents[3] / "src/lantai/agents/md_files/zh/PRODUCT_GUIDE.md"
    ).read_text(encoding="utf-8")

    assert 'template_version: "2.0.0"' in guide
    assert render_product_capability_reference("zh") in guide


def test_files_group_exposes_complete_curated_skill_workflow() -> None:
    _description, tools = PROGRESSIVE_TOOL_GROUPS["files"]

    assert {
        "declare_deliverable_contract",
        "read_skill",
        "list_skill_resources",
        "copy_skill_resource",
        "run_skill_script",
        "create_presentation",
        "get_presentation_generation_status",
        "retry_presentation_generation",
        "finalize_presentation_generation",
    } <= tools
    assert "execute_shell_command" not in tools


def test_existing_files_group_exposes_bound_private_editor_workflow() -> None:
    tools = {
        "record_private_editor_context",
        "stage_private_editor_candidate",
        "record_private_editor_angle_result",
        "adopt_private_editor_candidate",
    }
    assert tools <= PROGRESSIVE_TOOL_GROUPS["files"][1]
    for name in tools:
        assert progressive_tool_group_for(origin="builtin", name=name) == "files"
        assert progressive_tool_group_for(origin="mcp", name=name) == "connected_services"


def test_computer_group_keeps_mac_read_write_and_stop_tools_together() -> None:
    _description, tools = PROGRESSIVE_TOOL_GROUPS["computer"]

    assert tools == {
        "mac_inspect",
        "mac_observe",
        "mac_act",
        "mac_verify",
        "mac_session",
    }


def test_only_versioned_host_chrome_metadata_enters_browser_group() -> None:
    official = {
        "official_integration": "chrome-devtools",
        "integration_policy_version": "lantai-chrome-devtools/1",
        "progressive_group": "browser",
    }

    assert (
        progressive_tool_group_for(
            origin="mcp",
            name="performance_start_trace",
            metadata=official,
        )
        == "browser"
    )
    assert (
        progressive_tool_group_for(
            origin="mcp",
            name="performance_start_trace",
            metadata={"progressive_group": "browser"},
        )
        == "connected_services"
    )
    assert (
        progressive_tool_group_for(
            origin="mcp",
            name="performance_start_trace",
            metadata={**official, "integration_policy_version": "forged"},
        )
        == "connected_services"
    )
