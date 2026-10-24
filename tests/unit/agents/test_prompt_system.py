# -*- coding: utf-8 -*-
"""Contracts for the typed Lantai prompt compiler and manifest."""

from pathlib import Path

import pytest

from lantai.agents.prompt import (
    _build_agent_identity_header,
    build_system_prompt_bundle_from_working_dir,
)
from lantai.agents.prompt_system import (
    PromptRegistry,
    PromptSection,
    build_workspace_template_drift_report,
)


def test_prompt_registry_compiles_in_order_with_deterministic_manifest() -> None:
    registry = PromptRegistry(language="zh", provider="kimi", model="m1")
    registry.add(
        PromptSection(
            id="kernel.identity",
            content="稳定身份",
            owner="persona",
            version="1",
            required=True,
            cache_tier="kernel",
            source_kind="platform_canon",
        )
    )
    registry.add(
        PromptSection(
            id="turn.intent",
            content="本轮语义",
            owner="turn_intelligence",
            version="1",
            dynamic=True,
            cache_tier="turn",
            source_kind="runtime_projection",
        )
    )

    first = registry.compile(active_tools=("read_file",))
    second = registry.compile(active_tools=("read_file",))

    assert first.prompt == "稳定身份\n\n本轮语义"
    assert first.manifest["prompt_build_id"] == second.manifest["prompt_build_id"]
    assert [row["id"] for row in first.manifest["sections"]] == [
        "kernel.identity",
        "turn.intent",
    ]
    assert first.manifest["active_tools"] == ["read_file"]
    assert first.manifest["total_chars"] == len(first.prompt)
    assert first.manifest["estimated_tokens"] > 0
    assert "content" not in first.manifest["sections"][0]


def test_prompt_registry_rejects_duplicate_or_empty_required_sections() -> None:
    registry = PromptRegistry(language="zh")
    registry.add(
        PromptSection(
            id="kernel.identity",
            content="identity",
            owner="persona",
            required=True,
        )
    )
    with pytest.raises(ValueError, match="duplicate prompt section"):
        registry.add(
            PromptSection(
                id="kernel.identity",
                content="duplicate",
                owner="persona",
            )
        )

    empty = PromptRegistry(language="zh")
    empty.add(
        PromptSection(
            id="kernel.required",
            content="",
            owner="persona",
            required=True,
        )
    )
    with pytest.raises(ValueError, match="required prompt section"):
        empty.compile()


def test_prompt_registry_reports_token_budget_conflict_without_dropping_text() -> None:
    registry = PromptRegistry(language="zh")
    registry.add(PromptSection(id="kernel", content="内容" * 20, owner="persona"))

    build = registry.compile(token_budget=1)

    assert build.prompt == "内容" * 20
    assert build.manifest["within_token_budget"] is False
    assert build.manifest["conflicts"][0]["type"] == "token_budget_exceeded"


def test_base_prompt_bundle_separates_platform_canon_and_user_overlay(
    tmp_path: Path,
) -> None:
    (tmp_path / "AGENTS.md").write_text("用户增量合同", encoding="utf-8")

    bundle = build_system_prompt_bundle_from_working_dir(
        working_dir=tmp_path,
        enabled_files=["AGENTS.md"],
        agent_id="default",
        language="zh",
    )

    assert "沈行知人格宪法" in bundle.prompt
    assert "用户增量合同" in bundle.prompt
    rows = {row["id"]: row for row in bundle.manifest["sections"]}
    assert rows["kernel.temperament_constitution"]["source_kind"] == (
        "platform_canon"
    )
    assert rows["workspace.prompt_files"]["source_kind"] == "user_overlay"
    assert rows["kernel.temperament_enforcement"]["cache_tier"] == "kernel"
    assert rows["workspace.prompt_files"]["metadata"]["files"][0]["name"] == (
        "AGENTS.md"
    )


def test_workspace_drift_report_is_read_only_and_shows_user_diff(tmp_path: Path) -> None:
    platform = tmp_path / "platform"
    workspace = tmp_path / "workspace"
    platform.mkdir()
    workspace.mkdir()
    (platform / "SOUL.md").write_text("官方\n", encoding="utf-8")
    (workspace / "SOUL.md").write_text("官方\n用户增量\n", encoding="utf-8")

    report = build_workspace_template_drift_report(platform, workspace)

    assert report["files"][0]["status"] == "user_modified"
    assert "+用户增量" in report["files"][0]["diff"]
    assert (workspace / "SOUL.md").read_text(encoding="utf-8") == "官方\n用户增量\n"


def test_agent_identity_policy_keeps_semantics_under_compact_budget() -> None:
    identity = _build_agent_identity_header("default")

    assert len(identity) < 1_700
    assert "specialist" in identity
    assert "input-validation" in identity
    assert "Never describe a sub-agent as a Skill" in identity
    assert "Always own the final judgment" in identity


def test_compaction_restart_keeps_kernel_while_provider_switch_changes_build_id() -> None:
    section = PromptSection(
        id="kernel.persona",
        content="protected kernel",
        owner="persona",
        required=True,
        cache_tier="kernel",
        source_kind="platform_canon",
    )
    before = PromptRegistry(language="zh", provider="kimi", model="m1")
    before.add(section)
    after_restart = PromptRegistry(language="zh", provider="kimi", model="m1")
    after_restart.add(section)
    after_switch = PromptRegistry(language="zh", provider="deepseek", model="m2")
    after_switch.add(section)

    first = before.compile(context={"history_state": "full"})
    restarted = after_restart.compile(context={"history_state": "full"})
    switched = after_switch.compile(context={"history_state": "full"})

    assert restarted.prompt == first.prompt
    assert restarted.manifest["prompt_build_id"] == first.manifest["prompt_build_id"]
    assert switched.prompt == first.prompt
    assert switched.manifest["prompt_build_id"] != first.manifest["prompt_build_id"]
    assert switched.manifest["sections"][0]["sha256"] == (
        first.manifest["sections"][0]["sha256"]
    )
