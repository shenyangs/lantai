# -*- coding: utf-8 -*-
"""Tests for remembered tool approval rules."""

from __future__ import annotations

from lantai.security.tool_permission_memory import (
    remember_workspace_approval,
    workspace_approval_matches,
)
from lantai.security.tool_policy import ToolExecutionContext, evaluate_shell_command


def test_workspace_approval_memory_matches_same_rule_and_command_head(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr("lantai.config.utils.WORKING_DIR", tmp_path)
    monkeypatch.setattr("lantai.config.config.WORKING_DIR", tmp_path)
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    decision = evaluate_shell_command(
        "npm install lodash",
        context=ToolExecutionContext(workspace_dir=str(workspace_dir)),
    )

    remembered = remember_workspace_approval(
        workspace_dir=str(workspace_dir),
        tool_name="execute_shell_command",
        tool_input={"command": "npm install lodash"},
        policy_decision=decision,
        source_agent_id="default",
    )

    assert remembered is not None
    assert remembered.matched_rule_ids == ["shell.install_dependency"]
    assert remembered.command_head == "npm"
    assert remembered.command_pattern == r"^npm\b"

    match = workspace_approval_matches(
        workspace_dir=str(workspace_dir),
        tool_name="execute_shell_command",
        tool_input={"command": "npm install react"},
        policy_decision=evaluate_shell_command(
            "npm install react",
            context=ToolExecutionContext(workspace_dir=str(workspace_dir)),
        ),
        source_agent_id="default",
    )
    assert match is not None

    mismatch = workspace_approval_matches(
        workspace_dir=str(workspace_dir),
        tool_name="execute_shell_command",
        tool_input={"command": "pnpm install react"},
        policy_decision=evaluate_shell_command(
            "pnpm install react",
            context=ToolExecutionContext(workspace_dir=str(workspace_dir)),
        ),
        source_agent_id="default",
    )
    assert mismatch is None


def test_workspace_escape_is_not_persisted(tmp_path, monkeypatch):
    monkeypatch.setattr("lantai.config.utils.WORKING_DIR", tmp_path)
    monkeypatch.setattr("lantai.config.config.WORKING_DIR", tmp_path)
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    outside = tmp_path / "outside.txt"
    decision = evaluate_shell_command(
        f"touch {outside}",
        context=ToolExecutionContext(workspace_dir=str(workspace_dir)),
    )

    remembered = remember_workspace_approval(
        workspace_dir=str(workspace_dir),
        tool_name="execute_shell_command",
        tool_input={"command": f"touch {outside}"},
        policy_decision=decision,
        source_agent_id="default",
    )

    assert remembered is None


def test_workspace_approval_is_bound_to_agent_grant_and_directory_identity(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr("lantai.config.utils.WORKING_DIR", tmp_path)
    monkeypatch.setattr("lantai.config.config.WORKING_DIR", tmp_path)
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    decision = evaluate_shell_command(
        "npm install lodash",
        context=ToolExecutionContext(workspace_dir=str(workspace_dir)),
    )
    remembered = remember_workspace_approval(
        workspace_dir=str(workspace_dir),
        workspace_grant_id="wsg-original",
        tool_name="execute_shell_command",
        tool_input={"command": "npm install lodash"},
        policy_decision=decision,
        source_agent_id="agent-a",
    )
    assert remembered is not None

    def match(*, agent_id: str, grant_id: str):
        return workspace_approval_matches(
            workspace_dir=str(workspace_dir),
            workspace_grant_id=grant_id,
            tool_name="execute_shell_command",
            tool_input={"command": "npm install react"},
            policy_decision=evaluate_shell_command(
                "npm install react",
                context=ToolExecutionContext(workspace_dir=str(workspace_dir)),
            ),
            source_agent_id=agent_id,
        )

    assert match(agent_id="agent-a", grant_id="wsg-original") is not None
    assert match(agent_id="agent-b", grant_id="wsg-original") is None
    assert match(agent_id="agent-a", grant_id="wsg-replaced") is None

    workspace_dir.rename(tmp_path / "workspace-old")
    workspace_dir.mkdir()
    assert match(agent_id="agent-a", grant_id="wsg-original") is None
