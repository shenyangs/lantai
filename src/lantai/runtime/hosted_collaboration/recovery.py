# -*- coding: utf-8 -*-
"""Recovery helpers for closing rooms outside the live coordinator."""

from __future__ import annotations

from typing import Any


def terminal_batch_status(members: list[dict[str, Any]]) -> str:
    statuses = {str(member.get("status") or "") for member in members}
    if statuses == {"completed"}:
        return "completed"
    if "completed" in statuses:
        return "partial"
    if statuses and statuses <= {"cancelled"}:
        return "cancelled"
    return "failed"


def close_public_room_snapshot(
    *,
    source: dict[str, Any],
    batch_id: str,
    members: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return one explicit, replayable terminal room projection.

    This is used when timeout, interruption, or process-boundary cleanup occurs
    outside ``HostedCollaborationCoordinator``.  The function only accepts the
    already-public projection and therefore cannot leak member assignments.
    """

    batch_status = terminal_batch_status(members)
    completed_count = sum(
        1 for member in members if member.get("status") == "completed"
    )
    failed_count = sum(
        1 for member in members if member.get("status") == "failed"
    )
    cancelled_count = sum(
        1 for member in members if member.get("status") == "cancelled"
    )
    if batch_status == "completed":
        outcome = "ready_for_synthesis"
        synthesis_ready = True
        deliverable_ready = False
        user_action_required = False
        summary = "全部协作者已完成，可以由沈行知统一交付。"
    elif batch_status == "partial":
        outcome = "partial"
        synthesis_ready = True
        deliverable_ready = False
        user_action_required = False
        summary = (
            "协作异常结束；沈行知将保留可用结果、说明缺口并降级交付。"
        )
    elif batch_status == "cancelled":
        outcome = "cancelled"
        synthesis_ready = False
        deliverable_ready = False
        user_action_required = False
        summary = "协作已停止，所有成员已回收并保留已有记录。"
    else:
        outcome = "failed"
        synthesis_ready = False
        deliverable_ready = False
        user_action_required = True
        summary = "协作未形成可验收结果，已以明确失败结案。"

    acceptance_criteria = source.get("acceptance_criteria")
    if not isinstance(acceptance_criteria, list):
        acceptance_criteria = []
    conflicts = source.get("conflicts")
    open_conflicts = sum(
        1
        for conflict in (conflicts if isinstance(conflicts, list) else [])
        if isinstance(conflict, dict) and conflict.get("status") == "open"
    )
    try:
        revision = int(source.get("revision") or 0)
    except (TypeError, ValueError):
        revision = 0

    snapshot = dict(source)
    snapshot.update(
        {
            "schema_version": 2,
            "batch_id": batch_id,
            "revision": revision + 1,
            "status": batch_status,
            "phase": "closed",
            "members": members,
            "quiet_summary": summary,
            "user_action_required": user_action_required,
            "closure": {
                "outcome": outcome,
                "synthesis_ready": synthesis_ready,
                "deliverable_ready": deliverable_ready,
                "user_action_required": user_action_required,
                "completed_members": completed_count,
                "failed_members": failed_count,
                "cancelled_members": cancelled_count,
                "open_conflicts": open_conflicts,
                "acceptance_met": [],
                "acceptance_unmet": acceptance_criteria,
                "summary": summary,
            },
            "controls": {
                "can_takeover": False,
                "can_stop": False,
                "can_pause": False,
            },
        },
    )
    snapshot.setdefault(
        "host",
        {
            "agent_id": "default",
            "display_name": "沈行知",
            "role": "host_and_final_owner",
            "responsibilities": [
                "staff",
                "delegate",
                "moderate",
                "reconcile",
                "verify",
                "deliver",
                "close",
            ],
        },
    )
    snapshot.setdefault("participation_mode", "autonomous")
    snapshot.setdefault("unattended", True)
    snapshot.setdefault("conversation_revision", 0)
    snapshot.setdefault("current_round", 1)
    snapshot.setdefault("messages", [])
    snapshot.setdefault("held_drafts", [])
    snapshot.setdefault("conflicts", [])
    snapshot.setdefault("host_decisions", [])
    return snapshot
