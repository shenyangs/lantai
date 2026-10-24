# -*- coding: utf-8 -*-
"""Tests for the built-in tool permission policy."""

from __future__ import annotations

from lantai.security.tool_policy import (
    ToolExecutionContext,
    evaluate_shell_command,
    evaluate_tool_policy,
    list_permission_profiles,
)


def test_permission_profiles_are_user_facing():
    profiles = list_permission_profiles(distribution="developer_id")

    assert [profile.id for profile in profiles] == [
        "observe",
        "workspace",
        "full",
    ]
    assert all(profile.name for profile in profiles)
    assert all(profile.description for profile in profiles)


def test_read_only_command_is_allowed():
    decision = evaluate_shell_command("rg -n RuntimeEvent src")

    assert decision.decision == "allow"
    assert decision.profile == "workspace"


def test_custom_path_rule_matches_effect_paths_without_reclassifying_reads(
    tmp_path,
):
    protected = tmp_path / "protected"
    protected.mkdir()
    context = ToolExecutionContext(
        workspace_dir=str(tmp_path),
        command_rules=[
            {
                "id": "protect-target",
                "enabled": True,
                "effect": "deny",
                "tool_name": "execute_shell_command",
                "path_pattern": str(protected / "*"),
            },
        ],
    )

    denied = evaluate_shell_command(
        f"cp source.txt {protected / 'target.txt'}",
        context=context,
    )
    unrelated = evaluate_shell_command("ls /Applications", context=context)

    assert denied.decision == "forbidden"
    assert denied.effective_rule_id == "protect-target"
    assert unrelated.decision == "allow"


def test_baseline_web_reads_are_allowed_but_plan_mode_still_blocks_network():
    default_decision = evaluate_tool_policy(
        "web_search",
        {"query": "latest policy"},
        context=ToolExecutionContext(permission_mode="default"),
    )
    plan_decision = evaluate_tool_policy(
        "web_fetch",
        {"url": "https://example.com"},
        context=ToolExecutionContext(permission_mode="plan"),
    )

    assert default_decision.decision == "allow"
    assert default_decision.decision_source == "builtin_allow"
    assert plan_decision.decision == "forbidden"
    assert plan_decision.matched_rule_id == "mode.plan_tool_blocked"


def test_chat_native_analysis_uses_existing_read_write_and_delivery_permissions(tmp_path):
    workspace = ToolExecutionContext(workspace_dir=str(tmp_path), permission_mode="quiet", permission_profile="workspace")
    observe = ToolExecutionContext(workspace_dir=str(tmp_path), permission_mode="default", permission_profile="observe")
    plan = ToolExecutionContext(workspace_dir=str(tmp_path), permission_mode="plan", permission_profile="workspace")
    for name in ("import_data_table", "analyze_data_table", "refresh_data_table", "refresh_analysis_result"):
        assert evaluate_tool_policy(name, {}, context=workspace).decision == "allow"
        assert evaluate_tool_policy(name, {}, context=observe).decision == "prompt"
        assert evaluate_tool_policy(name, {}, context=plan).decision == "forbidden"
    for name in ("get_analysis_result", "get_analysis_delivery_status"):
        assert evaluate_tool_policy(name, {}, context=workspace).decision == "allow"
        assert evaluate_tool_policy(name, {}, context=plan).decision == "forbidden"
    assert evaluate_tool_policy("export_analysis_evidence", {}, context=workspace).decision == "allow"
    assert evaluate_tool_policy("export_analysis_evidence", {}, context=observe).decision == "prompt"
    for name in ("create_analysis_report", "create_analysis_chart", "create_analysis_presentation"):
        assert evaluate_tool_policy(name, {}, context=workspace).decision == "prompt"
    clear = evaluate_tool_policy("clear_analysis_data", {}, context=workspace)
    assert clear.decision == "prompt"
    assert clear.matched_rule_id == "clear_analysis_data.confirmation_required"
    full = ToolExecutionContext(workspace_dir=str(tmp_path), permission_mode="full_auto", permission_profile="full")
    assert evaluate_tool_policy("clear_analysis_data", {}, context=full).decision == "prompt"
    assert evaluate_tool_policy("clear_analysis_data", {}, context=plan).decision == "forbidden"


def test_plan_mode_allows_internal_plan_state_controls_only():
    create_decision = evaluate_tool_policy(
        "create_plan",
        {"name": "release"},
        context=ToolExecutionContext(permission_mode="plan"),
    )
    mutate_decision = evaluate_tool_policy(
        "write_file",
        {"path": "release.md"},
        context=ToolExecutionContext(permission_mode="plan"),
    )

    assert create_decision.decision == "allow"
    assert create_decision.decision_source == "mode"
    assert mutate_decision.decision == "forbidden"
    assert mutate_decision.matched_rule_id == "mode.plan_tool_blocked"


def test_skill_resource_discovery_is_read_only_but_script_execution_is_gated():
    discovery = evaluate_tool_policy(
        "list_skill_resources",
        {"skill_name": "pptx"},
        context=ToolExecutionContext(permission_mode="default"),
    )
    default_run = evaluate_tool_policy(
        "run_skill_script",
        {
            "skill_name": "pptx",
            "script_path": "scripts/render_pptxgenjs.py",
            "arguments": [],
        },
        context=ToolExecutionContext(permission_mode="default"),
    )
    full_run = evaluate_tool_policy(
        "run_skill_script",
        {
            "skill_name": "pptx",
            "script_path": "scripts/render_pptxgenjs.py",
            "arguments": [],
        },
        context=ToolExecutionContext(
            permission_profile="full",
            permission_mode="full_auto",
            distribution="developer_id",
        ),
    )

    assert discovery.decision == "allow"
    assert default_run.decision == "prompt"
    assert default_run.matched_rule_id == "skill_script.confirmation_required"
    assert full_run.decision == "allow"


def test_copy_skill_resource_uses_the_same_workspace_write_policy():
    context = ToolExecutionContext(permission_mode="default")
    copy_decision = evaluate_tool_policy(
        "copy_skill_resource",
        {
            "skill_name": "pptx",
            "resource_path": "assets/template.pptx",
            "destination_path": "template.pptx",
        },
        context=context,
    )
    write_decision = evaluate_tool_policy(
        "write_file",
        {"file_path": "template.pptx", "content": ""},
        context=context,
    )

    assert copy_decision.decision == write_decision.decision


def test_dependency_install_requires_prompt():
    decision = evaluate_shell_command("npm install lodash")

    assert decision.decision == "prompt"
    assert decision.matched_rule_id == "shell.install_dependency"
    assert decision.requires_approval is True
    assert decision.risk == "high"
    assert decision.rememberable is True


def test_destructive_command_is_forbidden():
    decision = evaluate_shell_command("rm -rf /")

    assert decision.decision == "forbidden"
    assert decision.matched_rule_id == "shell.rm_root"
    assert decision.risk == "critical"


def test_workspace_escape_requires_prompt(tmp_path):
    decision = evaluate_shell_command(
        f"touch {tmp_path.parent / 'outside.txt'}",
        context=ToolExecutionContext(workspace_dir=str(tmp_path)),
    )

    assert decision.decision == "prompt"
    assert decision.workspace_escape is True
    assert decision.matched_rule_id == "shell.workspace_escape"
    assert decision.rememberable is False


def test_relative_workspace_escape_requires_prompt(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    decision = evaluate_shell_command(
        "touch ../outside.txt",
        context=ToolExecutionContext(workspace_dir=str(workspace)),
    )
    tool_decision = evaluate_tool_policy(
        "write_file",
        {"path": "../outside.txt"},
        context=ToolExecutionContext(workspace_dir=str(workspace)),
    )

    assert decision.decision == "prompt"
    assert decision.workspace_escape is True
    assert tool_decision.decision == "prompt"
    assert tool_decision.workspace_escape is True


def test_sensitive_system_path_write_is_forbidden():
    decision = evaluate_shell_command("touch /Applications/Lantai.app")

    assert decision.decision == "forbidden"
    assert decision.matched_rule_id == "shell.system_path_write"


def test_user_library_workspace_is_not_mistaken_for_system_library():
    workspace = (
        "/Users/example/Library/Application Support/lantai-console/working/"
        "workspaces/default/topic-workspaces/current-topic"
    )
    decision = evaluate_shell_command(
        (
            'mkdir -p extracted && cp "'
            f"{workspace}/media/source.docx"
            '" extracted/source.docx'
        ),
        context=ToolExecutionContext(
            workspace_dir=workspace,
            permission_profile="full",
            permission_mode="full_auto",
        ),
    )

    assert decision.decision == "allow"
    assert decision.matched_rule_id is None


def test_real_system_library_write_remains_forbidden():
    decision = evaluate_shell_command(
        'cp source.txt "/Library/Application Support/Lantai/source.txt"',
        context=ToolExecutionContext(
            permission_profile="full",
            permission_mode="full_auto",
        ),
    )

    assert decision.decision == "forbidden"
    assert decision.matched_rule_id == "shell.system_path_write"


def test_system_path_reads_with_stderr_to_dev_null_are_allowed():
    context = ToolExecutionContext(
        permission_profile="full",
        permission_mode="full_auto",
        distribution="developer_id",
    )

    decisions = [
        evaluate_shell_command(
            "ls /Applications 2>/dev/null", context=context
        ),
        evaluate_shell_command(
            "find /System/Library/Fonts -maxdepth 1 2>/dev/null",
            context=context,
        ),
        evaluate_shell_command(
            "echo /Applications/Lantai.app 2>/dev/null",
            context=context,
        ),
    ]

    assert {decision.decision for decision in decisions} == {"allow"}
    assert all(decision.hard_denied is False for decision in decisions)


def test_only_copy_destination_is_treated_as_a_write_target(tmp_path):
    context = ToolExecutionContext(
        workspace_dir=str(tmp_path),
        permission_profile="full",
        permission_mode="full_auto",
        distribution="developer_id",
    )

    decision = evaluate_shell_command(
        "cp /Applications/Lantai.app/Contents/Info.plist copied.plist",
        context=context,
    )

    assert decision.decision == "allow"
    assert decision.hard_denied is False


def test_output_redirection_and_tee_to_system_paths_remain_forbidden():
    context = ToolExecutionContext(
        permission_profile="full",
        permission_mode="full_auto",
        distribution="developer_id",
    )

    decisions = [
        evaluate_shell_command(
            'echo hi > "/Applications/Lantai App/state.txt"',
            context=context,
        ),
        evaluate_shell_command(
            "cat source.txt | tee /System/Library/lantai-state.txt",
            context=context,
        ),
    ]

    assert {decision.decision for decision in decisions} == {"forbidden"}
    assert all(
        decision.matched_rule_id == "shell.system_path_write"
        for decision in decisions
    )


def test_attached_redirection_outside_workspace_requires_prompt(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    decision = evaluate_shell_command(
        "echo hi >>../outside.txt",
        context=ToolExecutionContext(workspace_dir=str(workspace)),
    )

    assert decision.decision == "prompt"
    assert decision.workspace_escape is True
    assert decision.matched_rule_id == "shell.workspace_escape"


def test_app_store_distribution_hides_full_and_downgrades_profile():
    profiles = list_permission_profiles(distribution="testflight")
    assert [profile.id for profile in profiles] == ["observe", "workspace"]

    decision = evaluate_shell_command(
        "rg RuntimeEvent src",
        context=ToolExecutionContext(
            permission_profile="full",
            distribution="app_store",
        ),
    )
    assert decision.profile == "workspace"
    assert decision.distribution == "app_store"


def test_app_store_distribution_can_come_from_environment(monkeypatch):
    monkeypatch.setenv("LANTAI_RELEASE_CHANNEL", "mas")
    assert [profile.id for profile in list_permission_profiles()] == [
        "observe",
        "workspace",
    ]

    monkeypatch.delenv("LANTAI_RELEASE_CHANNEL")
    monkeypatch.setenv("LANTAI_APP_STORE", "1")
    decision = evaluate_shell_command("rg RuntimeEvent src", profile="full")

    assert decision.profile == "workspace"
    assert decision.distribution == "app_store"


def test_observe_profile_prompts_for_write_tool():
    decision = evaluate_tool_policy(
        "write_file",
        {"path": "notes.md"},
        profile="observe",
    )
    append_decision = evaluate_tool_policy(
        "append_file",
        {"path": "notes.md", "content": "hello"},
        profile="observe",
    )

    assert decision.decision == "prompt"
    assert decision.profile == "observe"
    assert append_decision.decision == "prompt"
    assert append_decision.profile == "observe"


def test_permission_modes_cover_shell_behavior(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()

    default_decision = evaluate_shell_command(
        "npm test",
        context=ToolExecutionContext(
            workspace_dir=str(workspace),
            permission_mode="default",
        ),
    )
    quiet_decision = evaluate_shell_command(
        "npm test",
        context=ToolExecutionContext(
            workspace_dir=str(workspace),
            permission_mode="quiet",
        ),
    )
    plan_decision = evaluate_shell_command(
        "rg RuntimeEvent src",
        context=ToolExecutionContext(
            workspace_dir=str(workspace),
            permission_mode="plan",
        ),
    )
    full_auto_decision = evaluate_shell_command(
        "npm install lodash",
        context=ToolExecutionContext(
            workspace_dir=str(workspace),
            permission_mode="full_auto",
            distribution="developer_id",
        ),
    )

    assert default_decision.decision == "prompt"
    assert default_decision.rememberable is True
    assert quiet_decision.decision == "allow"
    assert plan_decision.decision == "forbidden"
    assert full_auto_decision.decision == "allow"


def test_full_auto_still_prompts_for_workspace_escape(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()

    shell = evaluate_shell_command(
        "touch ../outside.txt",
        context=ToolExecutionContext(
            workspace_dir=str(workspace),
            permission_mode="full_auto",
            distribution="developer_id",
        ),
    )
    write_tool = evaluate_tool_policy(
        "write_file",
        {"path": "../outside.txt"},
        context=ToolExecutionContext(
            workspace_dir=str(workspace),
            permission_mode="full_auto",
            distribution="developer_id",
        ),
    )

    assert shell.decision == "prompt"
    assert shell.workspace_escape is True
    assert write_tool.decision == "prompt"
    assert write_tool.workspace_escape is True


def test_chat_complete_access_eliminates_prompts_but_keeps_hard_denies(
    tmp_path,
):
    workspace = tmp_path / "work"
    workspace.mkdir()
    context = ToolExecutionContext(
        workspace_dir=str(workspace),
        permission_profile="full",
        permission_mode="full_auto",
        distribution="developer_id",
        command_rules=[
            {
                "id": "legacy-ask",
                "enabled": True,
                "effect": "ask",
                "tool_name": "execute_shell_command",
                "command_pattern": r"^touch\b",
            },
        ],
    )

    decisions = [
        evaluate_shell_command("touch ../outside.txt", context=context),
        evaluate_tool_policy(
            "write_file",
            {"path": "../outside.txt"},
            context=context,
        ),
        evaluate_tool_policy(
            "execute_python_code",
            {"code": "print('ok')"},
            context=context,
        ),
        evaluate_tool_policy(
            "browser_workbench",
            {"action": "click", "selector": "#submit"},
            context=context,
        ),
        evaluate_tool_policy(
            "send_file_to_user",
            {"path": "../outside.txt"},
            context=context,
        ),
        evaluate_tool_policy(
            "submit_to_agent",
            {"agent_id": "researcher", "task": "verify the result"},
            context=context,
        ),
        evaluate_tool_policy(
            "mystery_publish_tool",
            {"payload": "hello"},
            context=context,
        ),
        evaluate_tool_policy(
            "generate_image",
            {"prompt": "quiet moonlit room"},
            context=context,
        ),
    ]

    assert {decision.decision for decision in decisions} == {"allow"}
    assert all(decision.requires_approval is False for decision in decisions)
    assert all(decision.decision_source == "chat_permission" for decision in decisions)

    hard_denied = evaluate_shell_command("rm -rf /", context=context)
    sensitive_alias = evaluate_tool_policy(
        "python_magic_executor",
        {"code": "print('unsafe')"},
        context=context,
    )
    assert hard_denied.decision == "forbidden"
    assert hard_denied.hard_denied is True
    assert sensitive_alias.decision == "forbidden"
    assert sensitive_alias.hard_denied is True


def test_complete_access_still_honors_explicit_deny_rule(tmp_path):
    context = ToolExecutionContext(
        workspace_dir=str(tmp_path),
        permission_profile="full",
        permission_mode="full_auto",
        distribution="developer_id",
        command_rules=[
            {
                "id": "never-release",
                "enabled": True,
                "effect": "deny",
                "tool_name": "execute_shell_command",
                "command_pattern": r"^release\b",
            },
        ],
    )

    decision = evaluate_shell_command("release production", context=context)

    assert decision.decision == "forbidden"
    assert decision.effective_rule_id == "never-release"


def test_accept_edits_allows_workspace_file_edits_but_prompts_shell(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    decision = evaluate_tool_policy(
        "write_file",
        {"path": "notes.md"},
        context=ToolExecutionContext(
            workspace_dir=str(workspace),
            permission_mode="accept_edits",
        ),
    )
    shell = evaluate_shell_command(
        "python scripts/build.py",
        context=ToolExecutionContext(
            workspace_dir=str(workspace),
            permission_mode="accept_edits",
        ),
    )

    assert decision.decision == "allow"
    assert decision.command_category == "file_edit"
    assert shell.decision == "prompt"


def test_command_rules_apply_before_mode_fallback(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    ctx = ToolExecutionContext(
        workspace_dir=str(workspace),
        permission_mode="quiet",
        command_rules=[
            {
                "id": "deny-npm-test",
                "enabled": True,
                "effect": "deny",
                "tool_name": "execute_shell_command",
                "command_pattern": r"^npm\s+test\b",
            },
        ],
    )

    decision = evaluate_shell_command("npm test", context=ctx)

    assert decision.decision == "forbidden"
    assert decision.effective_rule_id == "deny-npm-test"
    assert decision.decision_source == "user_rule"


def test_app_store_full_auto_mode_downgrades_to_quiet():
    decision = evaluate_shell_command(
        "npm install lodash",
        context=ToolExecutionContext(
            permission_mode="full_auto",
            distribution="app_store",
        ),
    )

    assert decision.permission_mode == "quiet"
    assert decision.distribution == "app_store"
    assert decision.decision == "prompt"


def test_browser_read_actions_are_allowed_by_tool_policy():
    decision = evaluate_tool_policy(
        "browser_workbench",
        {"action": "open", "url": "https://github.com"},
        context=ToolExecutionContext(distribution="developer_id"),
    )

    assert decision.decision == "allow"
    assert decision.command_category == "browser_read"


def test_browser_find_is_allowed_as_a_browser_read_tool():
    direct_decision = evaluate_tool_policy(
        "browser_find",
        {"text": "Release"},
        context=ToolExecutionContext(distribution="developer_id"),
    )
    workbench_decision = evaluate_tool_policy(
        "browser_workbench",
        {"action": "find", "text": "Release"},
        context=ToolExecutionContext(distribution="developer_id"),
    )

    assert direct_decision.decision == "allow"
    assert direct_decision.command_category == "browser_read"
    assert workbench_decision.decision == "allow"
    assert workbench_decision.command_category == "browser_read"


def test_browser_read_task_and_scroll_key_are_silently_allowed():
    task_decision = evaluate_tool_policy(
        "browser_task",
        {
            "operation": "create",
            "access": "read",
            "instruction": "翻看20页笔记并总结",
        },
        context=ToolExecutionContext(distribution="developer_id"),
    )
    scroll_decision = evaluate_tool_policy(
        "browser_use",
        {"action": "press_key", "key": "PageDown"},
        context=ToolExecutionContext(distribution="developer_id"),
    )

    assert task_decision.decision == "allow"
    assert task_decision.command_category == "browser_read"
    assert scroll_decision.decision == "allow"
    assert scroll_decision.command_category == "browser_read"


def test_browser_write_task_still_requires_approval():
    decision = evaluate_tool_policy(
        "browser_task",
        {
            "operation": "create",
            "access": "write",
            "instruction": "发布一条帖子",
        },
        context=ToolExecutionContext(distribution="developer_id"),
    )

    assert decision.decision == "prompt"
    assert decision.matched_rule_id == "browser.write_confirmation_required"


def test_browser_write_actions_prompt_by_tool_policy():
    decision = evaluate_tool_policy(
        "browser_workbench",
        {"action": "click", "selector": "#submit"},
        context=ToolExecutionContext(distribution="developer_id"),
    )

    assert decision.decision == "prompt"
    assert decision.matched_rule_id == "browser.write_confirmation_required"
    assert decision.risk == "high"
    assert decision.rememberable is False


def test_browser_unknown_action_prompts_by_tool_policy():
    decision = evaluate_tool_policy(
        "browser_workbench",
        {"action": "invented_write"},
        context=ToolExecutionContext(distribution="developer_id"),
    )

    assert decision.decision == "prompt"
    assert decision.matched_rule_id == "browser.unknown_action"
    assert decision.risk == "high"


def test_app_store_distribution_blocks_agent_browser_automation():
    decision = evaluate_tool_policy(
        "browser_workbench",
        {"action": "snapshot"},
        context=ToolExecutionContext(distribution="app_store"),
    )

    assert decision.decision == "forbidden"
    assert decision.matched_rule_id == "app_store.browser_automation_blocked"


def test_mac_use_reads_are_allowed_but_writes_require_task_authority():
    read = evaluate_tool_policy(
        "mac_observe",
        {"app_bundle_id": "com.apple.TextEdit", "window_id": "42"},
        context=ToolExecutionContext(permission_mode="default"),
    )
    write = evaluate_tool_policy(
        "mac_act",
        {
            "operation": "set_value",
            "app_bundle_id": "com.apple.TextEdit",
            "window_id": "42",
            "observation_id": "obs-1",
        },
        context=ToolExecutionContext(permission_mode="default"),
    )
    full = evaluate_tool_policy(
        "mac_act",
        {
            "operation": "set_value",
            "app_bundle_id": "com.apple.TextEdit",
            "window_id": "42",
            "observation_id": "obs-1",
        },
        context=ToolExecutionContext(
            permission_mode="full_auto",
            permission_profile="full",
        ),
    )

    assert read.decision == "allow"
    assert read.command_category == "mac_use_read"
    assert write.decision == "prompt"
    assert write.rememberable is True
    assert full.decision == "allow"
    assert full.command_category == "mac_use_write"


def test_app_store_distribution_blocks_mac_use_even_for_reads():
    decision = evaluate_tool_policy(
        "mac_inspect",
        {},
        context=ToolExecutionContext(
            permission_mode="default",
            distribution="app_store",
        ),
    )

    assert decision.decision == "forbidden"
    assert decision.matched_rule_id == "app_store.mac_use_blocked"
    assert decision.hard_denied is True


def test_invalid_tool_params_are_forbidden():
    decision = evaluate_tool_policy(
        "write_file",
        ["not", "a", "dict"],  # type: ignore[arg-type]
    )

    assert decision.decision == "forbidden"
    assert decision.matched_rule_id == "tool.invalid_params"


def test_unknown_tool_prompts_instead_of_silent_allow():
    decision = evaluate_tool_policy(
        "mystery_publish_tool",
        {"payload": "hello"},
    )

    assert decision.decision == "prompt"
    assert decision.matched_rule_id == "tool.unknown"


def test_hosted_collaboration_is_known_low_risk_internal_tool():
    decision = evaluate_tool_policy(
        "collaborate_with_agents",
        {
            "goal": "compare two independent analyses",
            "tasks": [
                {"to_agent": "logic", "label": "logic", "text": "analyze"},
                {"to_agent": "science", "label": "science", "text": "analyze"},
            ],
        },
    )

    assert decision.decision == "allow"
    assert decision.requires_approval is False
    assert decision.command_category == "internal_coordination"


def test_code_execution_tool_requires_policy_boundary():
    decision = evaluate_tool_policy(
        "execute_python_code",
        {"code": "print('hello')"},
        context=ToolExecutionContext(distribution="developer_id"),
    )

    assert decision.decision == "prompt"
    assert decision.matched_rule_id == "code_execution.confirmation_required"
