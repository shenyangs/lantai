# -*- coding: utf-8 -*-
"""Tests for approval scope memory."""

from __future__ import annotations

from pathlib import Path
import time

import pytest

from lantai.app.approvals.service import ApprovalService
from lantai.security.tool_permission_memory import workspace_approval_matches
from lantai.security.tool_guard.approval import ApprovalDecision
from lantai.security.tool_guard.models import (
    GuardFinding,
    GuardSeverity,
    GuardThreatCategory,
    ToolGuardResult,
)
from lantai.security.tool_policy import ToolExecutionContext, evaluate_shell_command


def _guard_result_with_policy(tool_name: str, params: dict, policy: dict):
    finding = GuardFinding(
        id="finding-1",
        rule_id="runtime_tool_policy",
        category=GuardThreatCategory.RESOURCE_ABUSE,
        severity=GuardSeverity.HIGH,
        title="Runtime Tool Policy",
        description="policy prompt",
        tool_name=tool_name,
        metadata={"policy_decision": policy},
    )
    return ToolGuardResult(
        tool_name=tool_name,
        params=params,
        findings=[finding],
        guardians_used=["runtime_tool_policy"],
    )


@pytest.mark.asyncio
async def test_non_rememberable_approval_omits_remembered_options(tmp_path):
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    outside = tmp_path / "outside.txt"
    tool_input = {"command": f"touch {outside}"}
    decision = evaluate_shell_command(
        tool_input["command"],
        context=ToolExecutionContext(workspace_dir=str(workspace_dir)),
    ).model_dump(mode="json")
    svc = ApprovalService()

    pending = await svc.create_pending(
        session_id="child-session",
        root_session_id="root-session",
        user_id="user",
        channel="console",
        agent_id="default",
        tool_name="execute_shell_command",
        result=_guard_result_with_policy(
            "execute_shell_command",
            tool_input,
            decision,
        ),
        extra={
            "approval_options": [
                "allow_once",
                "allow_session",
                "allow_workspace",
                "deny",
                "view_rules",
            ],
            "tool_call": {
                "id": "tool-1",
                "name": "execute_shell_command",
                "input": tool_input,
            },
            "workspace_dir": str(workspace_dir),
            "policy_decision": decision,
        },
    )

    assert pending.rememberable is False
    assert pending.approval_options == ["allow_once", "deny", "view_rules"]


@pytest.mark.asyncio
async def test_approval_service_remembers_session_scope(tmp_path):
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    tool_input = {"command": "npm install lodash"}
    decision = evaluate_shell_command(
        "npm install lodash",
        context=ToolExecutionContext(workspace_dir=str(workspace_dir)),
    ).model_dump(mode="json")
    svc = ApprovalService()
    pending = await svc.create_pending(
        session_id="child-session",
        root_session_id="root-session",
        user_id="user",
        channel="console",
        agent_id="default",
        tool_name="execute_shell_command",
        result=_guard_result_with_policy(
            "execute_shell_command",
            tool_input,
            decision,
        ),
        extra={
            "tool_call": {
                "id": "tool-1",
                "name": "execute_shell_command",
                "input": tool_input,
            },
            "workspace_dir": str(workspace_dir),
            "policy_decision": decision,
        },
    )

    remembered = await svc.remember_approval_scope(
        pending,
        decision_scope="session",
    )

    assert remembered is not None
    assert remembered["decision_scope"] == "session"
    match = await svc.session_approval_matches(
        root_session_id="root-session",
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
    assert await svc.session_approval_matches(
        root_session_id="root-session",
        user_id="user",
        workspace_dir=str(workspace_dir),
        tool_name="execute_shell_command",
        tool_input={"command": "npm install react"},
        policy_decision=decision,
        source_agent_id="another-agent",
    ) is None
    assert await svc.session_approval_matches(
        root_session_id="root-session",
        user_id="another-user",
        workspace_dir=str(workspace_dir),
        tool_name="execute_shell_command",
        tool_input={"command": "npm install react"},
        policy_decision=decision,
        source_agent_id="default",
    ) is None

    racing = await svc.create_pending(
        session_id="late-child-session",
        root_session_id="root-session",
        user_id="user",
        channel="console",
        agent_id="default",
        tool_name="execute_shell_command",
        result=_guard_result_with_policy(
            "execute_shell_command",
            {"command": "npm install react"},
            decision,
        ),
        extra={
            "tool_call": {
                "id": "tool-late",
                "name": "execute_shell_command",
                "input": {"command": "npm install react"},
            },
            "workspace_dir": str(workspace_dir),
            "policy_decision": decision,
        },
    )
    assert racing.status == ApprovalDecision.APPROVED.value
    assert racing.future.result() == ApprovalDecision.APPROVED
    assert await svc.get_request(racing.request_id) is None


@pytest.mark.asyncio
async def test_live_full_access_override_drains_pending_and_is_user_scoped():
    svc = ApprovalService()
    first = await svc.create_pending(
        session_id="child-1",
        root_session_id="root-session",
        user_id="owner-user",
        channel="console",
        agent_id="default",
        tool_name="execute_shell_command",
        result=_guard_result_with_policy(
            "execute_shell_command",
            {"command": "npm install"},
            {"decision": "prompt", "permission_mode": "default"},
        ),
    )
    second = await svc.create_pending(
        session_id="child-2",
        root_session_id="root-session",
        user_id="owner-user",
        channel="console",
        agent_id="default",
        tool_name="write_file",
        result=_guard_result_with_policy(
            "write_file",
            {"path": "/tmp/result.txt"},
            {"decision": "prompt", "workspace_escape": True},
        ),
    )
    other_user = await svc.create_pending(
        session_id="other-child",
        root_session_id="root-session",
        user_id="other-user",
        channel="console",
        agent_id="default",
        tool_name="execute_shell_command",
        result=_guard_result_with_policy(
            "execute_shell_command",
            {"command": "npm test"},
            {"decision": "prompt"},
        ),
    )

    update = await svc.set_session_permission_override(
        root_session_id="root-session",
        user_id="owner-user",
        permission_mode="full_auto",
        permission_profile="full",
    )

    assert set(update["resolved_request_ids"]) == {
        first.request_id,
        second.request_id,
    }
    assert first.future.result() == ApprovalDecision.APPROVED
    assert second.future.result() == ApprovalDecision.APPROVED
    assert other_user.future.done() is False
    assert await svc.get_session_permission_override(
        root_session_id="root-session",
        user_id="owner-user",
    ) == {
        "permission_mode": "full_auto",
        "permission_profile": "full",
    }
    assert (
        await svc.get_session_permission_override(
            root_session_id="root-session",
            user_id="other-user",
        )
        is None
    )

    racing = await svc.create_pending(
        session_id="late-child",
        root_session_id="root-session",
        user_id="owner-user",
        channel="console",
        agent_id="default",
        tool_name="execute_shell_command",
        result=_guard_result_with_policy(
            "execute_shell_command",
            {"command": "npm run build"},
            {"decision": "prompt"},
        ),
        extra={
            "policy_decision": {"decision": "prompt"},
            "permission_snapshot_at": 0.0,
        },
    )
    assert racing.status == ApprovalDecision.APPROVED.value
    assert racing.future.result() == ApprovalDecision.APPROVED
    assert await svc.get_request(racing.request_id) is None

    later_turn = await svc.create_pending(
        session_id="later-turn",
        root_session_id="root-session",
        user_id="owner-user",
        channel="console",
        agent_id="default",
        tool_name="execute_shell_command",
        result=_guard_result_with_policy(
            "execute_shell_command",
            {"command": "npm run test"},
            {"decision": "prompt"},
        ),
        extra={
            "policy_decision": {"decision": "prompt"},
            "permission_snapshot_at": time.time() + 1,
        },
    )
    assert later_turn.future.done() is False
    assert (
        await svc.get_session_permission_override(
            root_session_id="root-session",
            user_id="owner-user",
            updated_after=time.time() + 1,
        )
        is None
    )


@pytest.mark.asyncio
async def test_full_access_preview_and_apply_share_an_atomic_queue_revision():
    svc = ApprovalService()
    first = await svc.create_pending(
        session_id="child-1",
        root_session_id="root-session",
        user_id="owner-user",
        channel="console",
        agent_id="default",
        tool_name="execute_shell_command",
        result=_guard_result_with_policy(
            "execute_shell_command",
            {"command": "npm test"},
            {"decision": "prompt"},
        ),
    )

    preview = await svc.preview_pending_for_root_session(
        root_session_id="root-session",
        user_id="owner-user",
        session_rememberable_only=False,
    )
    assert preview["requests"] == [
        {
            "request_id": first.request_id,
            "tool_name": "execute_shell_command",
            "severity": "high",
        }
    ]
    assert preview["queue_revision"]

    second = await svc.create_pending(
        session_id="child-2",
        root_session_id="root-session",
        user_id="owner-user",
        channel="console",
        agent_id="default",
        tool_name="write_file",
        result=_guard_result_with_policy(
            "write_file",
            {"path": "/tmp/result.txt"},
            {"decision": "prompt"},
        ),
    )

    with pytest.raises(ValueError, match="queue changed"):
        await svc.set_session_permission_override(
            root_session_id="root-session",
            user_id="owner-user",
            permission_mode="full_auto",
            permission_profile="full",
            expected_queue_revision=preview["queue_revision"],
        )

    assert first.future.done() is False
    assert second.future.done() is False
    assert (
        await svc.get_session_permission_override(
            root_session_id="root-session",
            user_id="owner-user",
        )
        is None
    )

    current = await svc.preview_pending_for_root_session(
        root_session_id="root-session",
        user_id="owner-user",
        session_rememberable_only=False,
    )
    applied = await svc.set_session_permission_override(
        root_session_id="root-session",
        user_id="owner-user",
        permission_mode="full_auto",
        permission_profile="full",
        expected_queue_revision=current["queue_revision"],
    )
    assert applied["resolved_request_ids"] == [
        first.request_id,
        second.request_id,
    ]


@pytest.mark.asyncio
async def test_session_approval_drains_existing_rememberable_pending_only(tmp_path):
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    svc = ApprovalService()
    policy = {"decision": "prompt", "permission_mode": "default"}
    approved = await svc.create_pending(
        session_id="child-1",
        root_session_id="root-session",
        user_id="owner-user",
        channel="console",
        agent_id="default",
        tool_name="execute_shell_command",
        result=_guard_result_with_policy(
            "execute_shell_command",
            {"command": "npm install"},
            policy,
        ),
        extra={
            "workspace_dir": str(workspace_dir),
            "policy_decision": policy,
        },
    )
    workspace_escape = await svc.create_pending(
        session_id="child-2",
        root_session_id="root-session",
        user_id="owner-user",
        channel="console",
        agent_id="default",
        tool_name="write_file",
        result=_guard_result_with_policy(
            "write_file",
            {"path": "/outside/result.txt"},
            {"decision": "prompt", "workspace_escape": True},
        ),
        extra={
            "workspace_dir": str(workspace_dir),
            "policy_decision": {
                "decision": "prompt",
                "workspace_escape": True,
            },
        },
    )

    resolved_ids = await svc.approve_pending_for_root_session(
        root_session_id="root-session",
        user_id="owner-user",
        session_rememberable_only=True,
    )

    assert resolved_ids == [approved.request_id]
    assert approved.future.result() == ApprovalDecision.APPROVED
    assert workspace_escape.future.done() is False


@pytest.mark.asyncio
async def test_cancel_all_pending_by_root_session_denies_and_removes_requests():
    svc = ApprovalService()
    pending = await svc.create_pending(
        session_id="child-session",
        root_session_id="root-session",
        user_id="user",
        channel="console",
        agent_id="default",
        tool_name="execute_shell_command",
        result=_guard_result_with_policy(
            "execute_shell_command",
            {"command": "rm -rf build"},
            {"decision": "prompt"},
        ),
    )
    other = await svc.create_pending(
        session_id="other-session",
        root_session_id="other-root",
        user_id="user",
        channel="console",
        agent_id="default",
        tool_name="execute_shell_command",
        result=_guard_result_with_policy(
            "execute_shell_command",
            {"command": "npm test"},
            {"decision": "prompt"},
        ),
    )

    cancelled = await svc.cancel_all_pending_by_root_session("root-session")

    assert cancelled == 1
    assert pending.status == "cancelled"
    assert pending.future.result() == ApprovalDecision.DENIED
    assert await svc.get_request(pending.request_id) is None
    assert await svc.get_request(other.request_id) is other


@pytest.mark.asyncio
async def test_open_ended_approval_is_not_evicted_before_user_decides():
    svc = ApprovalService()
    pending = await svc.create_pending(
        session_id="child-session",
        root_session_id="root-session",
        user_id="user",
        channel="console",
        agent_id="default",
        tool_name="generate_image",
        result=_guard_result_with_policy(
            "generate_image",
            {"prompt": "画图"},
            {"decision": "prompt"},
        ),
        timeout_seconds=None,
    )
    pending.created_at = time.time() - 24 * 60 * 60

    svc._gc_pending_locked()

    assert await svc.get_request(pending.request_id) is pending
    assert pending.status == "pending"
    assert pending.future.done() is False


@pytest.mark.asyncio
async def test_unknown_tool_can_be_remembered_for_current_session_only(tmp_path):
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    policy = {
        "decision": "prompt",
        "permission_mode": "quiet",
        "permission_profile": "workspace",
        "decision_source": "unknown_tool",
        "risk_level": "medium",
    }
    svc = ApprovalService()
    pending = await svc.create_pending(
        session_id="child-session",
        root_session_id="root-session",
        user_id="user",
        channel="console",
        agent_id="default",
        tool_name="generate_image",
        result=_guard_result_with_policy(
            "generate_image",
            {"prompt": "quiet moonlit room"},
            policy,
        ),
        extra={
            "approval_options": [
                "allow_once",
                "allow_session",
                "allow_workspace",
                "deny",
            ],
            "tool_call": {
                "id": "tool-image",
                "name": "generate_image",
                "input": {"prompt": "quiet moonlit room"},
            },
            "workspace_dir": str(workspace_dir),
            "policy_decision": policy,
        },
    )

    assert pending.session_rememberable is True
    assert pending.rememberable is False
    assert pending.approval_options == [
        "allow_once",
        "allow_session",
        "deny",
    ]

    remembered = await svc.remember_approval_scope(
        pending,
        decision_scope="session",
    )

    assert remembered is not None
    assert remembered["rule_kind"] == "session_access"
    assert remembered["decision_scope"] == "session"
    assert await svc.session_approval_matches(
        root_session_id="root-session",
        workspace_dir=str(workspace_dir),
        tool_name="generate_image",
        tool_input={"prompt": "another image"},
        policy_decision=policy,
        source_agent_id="default",
    ) is not None
    assert await svc.session_approval_matches(
        root_session_id="root-session",
        workspace_dir=str(workspace_dir),
        tool_name="web_search",
        tool_input={"query": "another image"},
        policy_decision=policy,
        source_agent_id="default",
    ) is not None
    assert await svc.session_approval_matches(
        root_session_id="root-session",
        workspace_dir=str(tmp_path / "other-workspace"),
        tool_name="generate_image",
        tool_input={"prompt": "another image"},
        policy_decision=policy,
        source_agent_id="default",
    ) is not None
    assert await svc.session_approval_matches(
        root_session_id="root-session",
        workspace_dir=str(workspace_dir),
        tool_name="execute_shell_command",
        tool_input={"command": "rm -rf /"},
        policy_decision={
            "decision": "forbidden",
            "hard_denied": True,
        },
        source_agent_id="default",
    ) is None
    assert await svc.session_approval_matches(
        root_session_id="root-session",
        workspace_dir=str(workspace_dir),
        tool_name="write_file",
        tool_input={"path": "/outside/workspace"},
        policy_decision={
            "decision": "prompt",
            "workspace_escape": True,
        },
        source_agent_id="default",
    ) is None
    assert await svc.session_approval_matches(
        root_session_id="another-root-session",
        workspace_dir=str(workspace_dir),
        tool_name="generate_image",
        tool_input={"prompt": "another image"},
        policy_decision=policy,
        source_agent_id="default",
    ) is None


@pytest.mark.asyncio
async def test_cancel_stale_pending_for_tool_call_supersedes_matching_request():
    svc = ApprovalService()
    pending = await svc.create_pending(
        session_id="child-session",
        root_session_id="root-session",
        user_id="user",
        channel="console",
        agent_id="default",
        tool_name="execute_shell_command",
        result=_guard_result_with_policy(
            "execute_shell_command",
            {"command": "npm install lodash"},
            {"decision": "prompt"},
        ),
        extra={
            "tool_call": {
                "id": "tool-repeat",
                "name": "execute_shell_command",
                "input": {"command": "npm install lodash"},
            },
        },
    )
    other = await svc.create_pending(
        session_id="child-session",
        root_session_id="root-session",
        user_id="user",
        channel="console",
        agent_id="default",
        tool_name="execute_shell_command",
        result=_guard_result_with_policy(
            "execute_shell_command",
            {"command": "npm test"},
            {"decision": "prompt"},
        ),
        extra={
            "tool_call": {
                "id": "tool-other",
                "name": "execute_shell_command",
                "input": {"command": "npm test"},
            },
        },
    )

    cancelled = await svc.cancel_stale_pending_for_tool_call(
        "child-session",
        "tool-repeat",
    )

    assert cancelled == 1
    assert pending.status == "superseded"
    assert pending.future.result() == ApprovalDecision.TIMEOUT
    assert await svc.get_request(pending.request_id) is None
    assert await svc.get_request(other.request_id) is other


@pytest.mark.asyncio
async def test_approval_memory_lifecycle_deletes_session_and_workspace_rules(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr("lantai.config.utils.WORKING_DIR", tmp_path)
    monkeypatch.setattr("lantai.config.config.WORKING_DIR", tmp_path)
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    tool_input = {"command": "npm install lodash"}
    decision = evaluate_shell_command(
        tool_input["command"],
        context=ToolExecutionContext(workspace_dir=str(workspace_dir)),
    ).model_dump(mode="json")
    svc = ApprovalService()

    async def create_pending_for_memory(session_id: str):
        return await svc.create_pending(
            session_id=session_id,
            root_session_id="root-session",
            user_id="user",
            channel="console",
            agent_id="default",
            tool_name="execute_shell_command",
            result=_guard_result_with_policy(
                "execute_shell_command",
                tool_input,
                decision,
            ),
            extra={
                "tool_call": {
                    "id": f"tool-{session_id}",
                    "name": "execute_shell_command",
                    "input": tool_input,
                },
                "workspace_dir": str(workspace_dir),
                "policy_decision": decision,
            },
        )

    session_pending = await create_pending_for_memory("child-session")
    workspace_pending = await create_pending_for_memory("other-session")

    session_memory = await svc.remember_approval_scope(
        session_pending,
        decision_scope="session",
    )
    workspace_memory = await svc.remember_approval_scope(
        workspace_pending,
        decision_scope="workspace",
    )

    assert session_memory is not None
    assert workspace_memory is not None

    report = await svc.approval_memory_lifecycle_report(
        workspace_dir=str(workspace_dir),
        source_agent_id="default",
        operation="delete",
        dry_run=False,
    )

    archive_path = Path(report["persistent"]["archive_path"])
    assert report["persistent"]["matched_count"] == 1
    assert report["persistent"]["removed_count"] == 1
    assert report["session"]["matched_count"] == 1
    assert report["session"]["removed_count"] == 1
    assert archive_path.exists()
    assert await svc.session_approval_matches(
        root_session_id="root-session",
        workspace_dir=str(workspace_dir),
        tool_name="execute_shell_command",
        tool_input={"command": "npm install react"},
        policy_decision=evaluate_shell_command(
            "npm install react",
            context=ToolExecutionContext(workspace_dir=str(workspace_dir)),
        ),
        source_agent_id="default",
    ) is None
    assert workspace_approval_matches(
        workspace_dir=str(workspace_dir),
        tool_name="execute_shell_command",
        tool_input={"command": "npm install react"},
        policy_decision=evaluate_shell_command(
            "npm install react",
            context=ToolExecutionContext(workspace_dir=str(workspace_dir)),
        ),
        source_agent_id="default",
    ) is None
