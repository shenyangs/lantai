# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest

from lantai.runtime.collaboration import build_collaboration_contract
from lantai.runtime.hosted_collaboration import HostedCollaborationCoordinator


def test_build_collaboration_contract_is_read_only_and_requires_synthesis() -> None:
    contract = build_collaboration_contract(
        contract_id="collab-1",
        goal="比较两个实现路径",
        why_parallel="两个审查相互独立",
        tasks=[
            {
                "to_agent": "architecture",
                "label": "架构审查",
                "text": "检查边界",
            },
            {"to_agent": "risk", "label": "风险审查", "text": "检查风险"},
        ],
        acceptance_criteria=["指出共识", "保留分歧"],
    )

    assert contract.contract_id == "collab-1"
    assert contract.schema_version == 2
    assert contract.effective_permissions == "read_only"
    assert contract.participation_mode == "autonomous"
    assert contract.protocol == "parallel_consultation"
    assert contract.host.display_name == "沈行知"
    assert contract.round_policy.max_rounds == 2
    assert contract.round_policy.allow_nested_collaboration is False
    assert contract.convergence.mode == "enforce"
    assert contract.convergence.max_no_progress_rounds == 2
    assert contract.convergence.semantic_review_limit == 1
    assert contract.convergence.reserve_synthesis_rounds == 1
    assert contract.convergence.max_research_calls == 10
    assert contract.convergence.max_read_calls == 14
    assert contract.convergence.saturated_outcome == "best_effort_or_checkpoint"
    assert [member.member_id for member in contract.members] == [
        "member-1",
        "member-2",
    ]
    assert contract.acceptance_criteria == ["指出共识", "保留分歧"]
    assert contract.synthesis.required_sections == [
        "agreements",
        "conflicts",
        "unknowns",
        "evidence_gaps",
        "next_steps",
    ]
    public_contract = contract.public_dump()
    assert "assignment" not in public_contract["members"][0]


def test_build_collaboration_contract_can_opt_into_bounded_roundtable() -> None:
    contract = build_collaboration_contract(
        contract_id="collab-roundtable",
        goal="比较两种路线",
        why_parallel="先独立判断，再交叉审阅",
        protocol="roundtable",
        tasks=[
            {"to_agent": "a", "label": "路线 A", "text": "评估 A"},
            {"to_agent": "b", "label": "路线 B", "text": "评估 B"},
        ],
    )

    assert contract.protocol == "roundtable"
    assert contract.round_policy.max_rounds == 2
    assert contract.public_dump()["protocol"] == "roundtable"


def test_hosted_coordinator_fences_stale_drafts_and_requires_host_override() -> None:
    contract = build_collaboration_contract(
        contract_id="collab-fence",
        goal="统一发布计划",
        why_parallel="研究与风险可独立进行",
        tasks=[
            {"to_agent": "research", "label": "研究", "text": "私有研究任务"},
            {"to_agent": "risk", "label": "复核", "text": "私有复核任务"},
        ],
    )
    coordinator = HostedCollaborationCoordinator(contract)
    coordinator.transition("staffing")
    coordinator.transition("working")

    committed = coordinator.propose_message(
        member_id="member-1",
        display_name="研究",
        kind="finding",
        safe_summary="发布窗口建议放在周三",
        base_revision=0,
    )
    stale = coordinator.propose_message(
        member_id="member-2",
        display_name="复核",
        kind="challenge",
        safe_summary="需要补充回滚窗口",
        base_revision=0,
    )

    assert committed.committed_revision == 1
    assert stale.current_revision == 1
    with pytest.raises(PermissionError):
        coordinator.resolve_held_draft(stale.draft_id, action="send_as_is")
    resolved = coordinator.resolve_held_draft(
        stale.draft_id,
        action="revise",
        revised_summary="已基于周三发布补充回滚窗口",
    )
    assert resolved.committed_revision == 2


def test_hosted_coordinator_closes_to_explicit_ready_for_synthesis_outcome() -> None:
    contract = build_collaboration_contract(
        contract_id="collab-close",
        goal="完成方案",
        why_parallel="独立检查",
        tasks=[
            {"to_agent": "a", "label": "A", "text": "private A"},
            {"to_agent": "b", "label": "B", "text": "private B"},
        ],
        acceptance_criteria=["两位成员已完成", "不存在开放冲突"],
    )
    coordinator = HostedCollaborationCoordinator(contract)
    members = [
        {
            "member_id": "member-1",
            "agent_id": "a",
            "display_name": "A",
            "label": "A",
            "status": "completed",
        },
        {
            "member_id": "member-2",
            "agent_id": "b",
            "display_name": "B",
            "label": "B",
            "status": "completed",
        },
    ]
    coordinator.transition("staffing")
    coordinator.transition("working")
    coordinator.transition("reconciling")
    coordinator.transition("verifying")
    closure = coordinator.assess_closure(members)
    coordinator.transition("delivering")
    coordinator.transition("closing")
    coordinator.transition("closed")
    snapshot = coordinator.public_snapshot(
        batch_status="completed",
        members=members,
    )

    assert closure.outcome == "ready_for_synthesis"
    assert closure.deliverable_ready is False
    assert closure.user_action_required is False
    assert snapshot["phase"] == "closed"
    assert snapshot["why_parallel"] == "独立检查"
    assert snapshot["unattended"] is True
    assert snapshot["controls"] == {
        "can_takeover": False,
        "can_stop": False,
        "can_pause": False,
    }


def test_hosted_coordinator_recovers_same_room_without_private_assignment() -> None:
    contract = build_collaboration_contract(
        contract_id="collab-recover",
        goal="恢复同一协作室",
        why_parallel="验证持久化投影",
        tasks=[
            {"to_agent": "a", "label": "研究", "text": "私有研究任务"},
            {"to_agent": "b", "label": "复核", "text": "私有复核任务"},
        ],
    )
    coordinator = HostedCollaborationCoordinator(contract)
    coordinator.transition("staffing")
    coordinator.transition("working")
    coordinator.propose_message(
        member_id="member-1",
        display_name="研究",
        kind="finding",
        safe_summary="形成可复核结论",
        base_revision=0,
    )
    snapshot = coordinator.public_snapshot(
        batch_status="running",
        members=[
            {
                "member_id": "member-1",
                "agent_id": "a",
                "display_name": "研究",
                "label": "研究",
                "status": "completed",
            },
            {
                "member_id": "member-2",
                "agent_id": "b",
                "display_name": "复核",
                "label": "复核",
                "status": "running",
            },
        ],
    )

    restored = HostedCollaborationCoordinator.restore(contract, snapshot)
    replay = restored.public_snapshot(
        batch_status="running",
        members=snapshot["members"],
    )

    assert replay["batch_id"] == snapshot["batch_id"]
    assert replay["revision"] == snapshot["revision"]
    assert replay["conversation_revision"] == 1
    assert replay["messages"] == snapshot["messages"]
    assert "私有研究任务" not in str(replay)


def test_hosted_coordinator_exposes_authoritative_child_execution_identity() -> None:
    """The inspector must bind to a child run without guessing by agent name."""

    contract = build_collaboration_contract(
        contract_id="collab-live-child",
        goal="实时查看子任务",
        why_parallel="独立检查",
        tasks=[
            {"to_agent": "research", "label": "研究", "text": "私有任务"},
            {"to_agent": "review", "label": "复核", "text": "私有复核"},
        ],
    )
    coordinator = HostedCollaborationCoordinator(contract)
    coordinator.transition("staffing")
    coordinator.transition("working")

    snapshot = coordinator.public_snapshot(
        batch_status="running",
        members=[
            {
                "member_id": "member-1",
                "agent_id": "research",
                "display_name": "研究",
                "label": "研究",
                "status": "running",
                "attempt": 1,
                "session_id": "host:to:research:attempt-1",
                "task_id": "task-child-1",
            },
            {
                "member_id": "member-2",
                "agent_id": "review",
                "display_name": "复核",
                "label": "复核",
                "status": "queued",
                "attempt": 0,
            },
        ],
    )

    member = snapshot["members"][0]
    assert member["session_id"] == "host:to:research:attempt-1"
    assert member["task_id"] == "task-child-1"
    assert "私有任务" not in str(snapshot)


def test_hosted_coordinator_exposes_completed_public_member_result() -> None:
    """A completed row and its accepted public result must be atomic."""

    contract = build_collaboration_contract(
        contract_id="collab-public-result",
        goal="完成审核",
        why_parallel="独立复核",
        tasks=[
            {"to_agent": "review", "label": "复核", "text": "私有完整任务"},
            {"to_agent": "research", "label": "研究", "text": "私有研究任务"},
        ],
    )
    coordinator = HostedCollaborationCoordinator(contract)
    coordinator.transition("staffing")
    coordinator.transition("working")

    snapshot = coordinator.public_snapshot(
        batch_status="running",
        members=[
            {
                "member_id": "member-1",
                "agent_id": "review",
                "display_name": "龙井",
                "label": "复核",
                "status": "completed",
                "result": "## 公开验收结果\n\n健康与安全审核通过。",
            },
            {
                "member_id": "member-2",
                "agent_id": "research",
                "display_name": "椰汁",
                "label": "研究",
                "status": "running",
            },
        ],
    )

    completed = snapshot["members"][0]
    assert completed["status"] == "completed"
    assert completed["result"] == "## 公开验收结果\n\n健康与安全审核通过。"
    assert "私有完整任务" not in str(snapshot)
    assert "私有研究任务" not in str(snapshot)


@pytest.mark.parametrize(
    ("members", "kwargs", "expected"),
    [
        (["completed", "failed"], {}, "partial"),
        (["failed", "failed"], {}, "failed"),
        (["cancelled", "cancelled"], {"cancelled": True}, "cancelled"),
        (
            ["completed", "completed"],
            {"approvals_pending": True},
            "waiting_approval",
        ),
    ],
)
def test_hosted_coordinator_always_closes_with_explicit_outcome(
    members: list[str],
    kwargs: dict[str, bool],
    expected: str,
) -> None:
    contract = build_collaboration_contract(
        contract_id=f"collab-{expected}",
        goal="显式结案",
        why_parallel="覆盖兜底结局",
        tasks=[
            {"to_agent": "a", "label": "A", "text": "private A"},
            {"to_agent": "b", "label": "B", "text": "private B"},
        ],
    )
    coordinator = HostedCollaborationCoordinator(contract)
    member_rows = [
        {
            "member_id": f"member-{index + 1}",
            "agent_id": chr(ord("a") + index),
            "display_name": chr(ord("A") + index),
            "label": chr(ord("A") + index),
            "status": status,
        }
        for index, status in enumerate(members)
    ]

    closure = coordinator.assess_closure(member_rows, **kwargs)

    assert closure.outcome == expected
    terminal_members = (
        closure.completed_members
        + closure.failed_members
        + closure.cancelled_members
    )
    assert terminal_members == 2
