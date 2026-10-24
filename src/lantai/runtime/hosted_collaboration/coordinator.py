# -*- coding: utf-8 -*-
"""Host-owned collaboration state machine with revision fencing."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable, Literal, cast
from uuid import uuid4

from ..collaboration import CollaborationContract
from .models import (
    CollaborationClosureAssessment,
    CollaborationConflict,
    CollaborationHeldDraft,
    CollaborationHostDecision,
    CollaborationMessageKind,
    CollaborationPhase,
    CollaborationRoomMessage,
)

_PHASE_ORDER: tuple[CollaborationPhase, ...] = (
    "preparing",
    "staffing",
    "working",
    "reconciling",
    "verifying",
    "delivering",
    "closing",
    "closed",
)


def _safe_text(value: object, *, limit: int = 240) -> str:
    return " ".join(str(value or "").split())[:limit]


def _public_member_projection(member: dict[str, Any]) -> dict[str, Any]:
    """Project one member without separating terminal status from its result.

    Member prompts and private working state remain excluded.  A completed
    member's already-accepted assistant answer is public product output, so it
    must travel atomically with ``status=completed``.  The cap keeps heartbeat
    snapshots bounded; the final room delivery still carries the full result.
    """

    public_member_keys = (
        "member_id",
        "agent_id",
        "display_name",
        "role",
        "label",
        "status",
        "attempt",
        "duration_ms",
        "submission_ms",
        "remote_wait_ms",
        "error_code",
        "session_id",
        "task_id",
        "execution_task_id",
        "run_id",
        "chat_id",
        "depends_on",
        "round_completed",
        "review_status",
        "execution_mode",
        "permission_profile",
        "workspace_scope",
        "allowed_tools",
        "resource_claims",
        "resource_leases",
        "expected_artifacts",
        "artifacts",
    )
    projected = {
        key: member.get(key)
        for key in public_member_keys
        if member.get(key) not in (None, "")
    }
    status = str(member.get("status") or "").lower()
    if status == "completed":
        result = str(member.get("result") or "").strip()
        if result:
            limit = 16_000
            projected["result"] = result[:limit]
            if len(result) > limit:
                projected["result_truncated"] = True
    elif status in {"failed", "cancelled"}:
        error_message = _safe_text(member.get("error_message"), limit=500)
        if error_message:
            projected["error_message"] = error_message
    return projected


def _member_counts(members: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts = {"completed": 0, "failed": 0, "cancelled": 0, "active": 0}
    for member in members:
        status = str(member.get("status") or "").lower()
        if status in counts:
            counts[status] += 1
        elif status in {
            "queued",
            "running",
            "retrying",
            "waiting_resource",
        }:
            counts["active"] += 1
    return counts


class HostedCollaborationCoordinator:
    """Deterministic room host; callers serialize mutations with their lock."""

    def __init__(self, contract: CollaborationContract) -> None:
        self.contract = contract
        self.phase: CollaborationPhase = "preparing"
        self.revision = 0
        self.conversation_revision = 0
        self.current_round = 1
        self.messages: list[CollaborationRoomMessage] = []
        self.held_drafts: dict[str, CollaborationHeldDraft] = {}
        self.conflicts: dict[str, CollaborationConflict] = {}
        self.decisions: list[CollaborationHostDecision] = []
        self.closure: CollaborationClosureAssessment | None = None

    @classmethod
    def restore(
        cls,
        contract: CollaborationContract,
        snapshot: dict[str, Any],
    ) -> "HostedCollaborationCoordinator":
        """Rebuild coordinator state from one persisted public projection.

        RuntimeEvent remains the source of truth.  Recovery intentionally
        restores only public, bounded room state; member-private assignments
        continue to come from the parent tool invocation and are never copied
        into the projection.
        """

        batch_id = str(snapshot.get("batch_id") or "").strip()
        if batch_id and batch_id != contract.contract_id:
            raise ValueError("collaboration snapshot does not match contract")
        coordinator = cls(contract)
        phase = str(snapshot.get("phase") or "preparing")
        if phase not in _PHASE_ORDER:
            raise ValueError(f"unknown collaboration phase: {phase}")
        coordinator.phase = cast(CollaborationPhase, phase)
        coordinator.revision = max(0, int(snapshot.get("revision") or 0))
        coordinator.conversation_revision = max(
            0,
            int(snapshot.get("conversation_revision") or 0),
        )
        coordinator.current_round = min(
            contract.round_policy.max_rounds,
            max(1, int(snapshot.get("current_round") or 1)),
        )
        coordinator.messages = [
            CollaborationRoomMessage.model_validate(item)
            for item in snapshot.get("messages") or []
            if isinstance(item, dict)
        ]
        coordinator.held_drafts = {
            draft.draft_id: draft
            for draft in (
                CollaborationHeldDraft.model_validate(item)
                for item in snapshot.get("held_drafts") or []
                if isinstance(item, dict)
            )
        }
        coordinator.conflicts = {
            conflict.conflict_id: conflict
            for conflict in (
                CollaborationConflict.model_validate(item)
                for item in snapshot.get("conflicts") or []
                if isinstance(item, dict)
            )
        }
        coordinator.decisions = [
            CollaborationHostDecision.model_validate(item)
            for item in snapshot.get("host_decisions") or []
            if isinstance(item, dict)
        ]
        closure = snapshot.get("closure")
        if isinstance(closure, dict):
            coordinator.closure = CollaborationClosureAssessment.model_validate(
                closure,
            )
        return coordinator

    def touch(self) -> int:
        self.revision += 1
        return self.revision

    def advance_round(self) -> int:
        """Advance a bounded room protocol without reopening closed state."""

        if self.phase != "working":
            raise ValueError("collaboration rounds may advance only while working")
        if self.current_round >= self.contract.round_policy.max_rounds:
            raise ValueError("collaboration round limit reached")
        self.current_round += 1
        self.touch()
        return self.current_round

    def transition(self, phase: CollaborationPhase, *, reason: str = "") -> None:
        current_index = _PHASE_ORDER.index(self.phase)
        target_index = _PHASE_ORDER.index(phase)
        if target_index < current_index and self.phase != "closed":
            raise ValueError(
                f"collaboration phase cannot move backward: {self.phase} -> {phase}",
            )
        if self.phase == "closed" and phase != "closed":
            raise ValueError("closed collaboration cannot be reopened in place")
        if phase != self.phase:
            self.phase = phase
            self.touch()
        if reason:
            self.record_host_decision(
                _safe_text(reason),
                reason="phase transition",
                touch=False,
            )

    def record_host_decision(
        self,
        summary: str,
        *,
        reason: str = "",
        touch: bool = True,
    ) -> CollaborationHostDecision:
        if touch:
            self.touch()
        decision = CollaborationHostDecision(
            decision_id=f"decision-{uuid4()}",
            phase=self.phase,
            safe_summary=_safe_text(summary),
            reason=_safe_text(reason),
            revision=self.revision,
        )
        self.decisions.append(decision)
        return decision

    def propose_message(
        self,
        *,
        member_id: str,
        display_name: str,
        kind: CollaborationMessageKind,
        safe_summary: str,
        base_revision: int,
    ) -> CollaborationRoomMessage | CollaborationHeldDraft:
        """Commit a current proposal or hold a stale one without leaking body text."""

        summary = _safe_text(safe_summary)
        if base_revision != self.conversation_revision:
            draft = CollaborationHeldDraft(
                draft_id=f"draft-{uuid4()}",
                member_id=member_id,
                display_name=_safe_text(display_name, limit=80),
                kind=kind,
                safe_summary=summary,
                base_revision=max(0, base_revision),
                current_revision=self.conversation_revision,
            )
            self.held_drafts[draft.draft_id] = draft
            self.touch()
            return draft

        self.conversation_revision += 1
        self.touch()
        message = CollaborationRoomMessage(
            message_id=f"message-{uuid4()}",
            member_id=member_id,
            display_name=_safe_text(display_name, limit=80),
            kind=kind,
            safe_summary=summary,
            base_revision=base_revision,
            committed_revision=self.conversation_revision,
        )
        self.messages.append(message)
        return message

    def resolve_held_draft(
        self,
        draft_id: str,
        *,
        action: Literal["revise", "send_as_is", "drop", "defer"],
        revised_summary: str = "",
        host_override: bool = False,
    ) -> CollaborationRoomMessage | CollaborationHeldDraft:
        draft = self.held_drafts[draft_id]
        if action == "send_as_is" and not host_override:
            raise PermissionError("only the host may send a stale draft as-is")
        if action in {"revise", "send_as_is"}:
            draft.resolution = "revised" if action == "revise" else "sent_as_is"
            summary = revised_summary if action == "revise" else draft.safe_summary
            self.conversation_revision += 1
            self.touch()
            message = CollaborationRoomMessage(
                message_id=f"message-{uuid4()}",
                member_id=draft.member_id,
                display_name=draft.display_name,
                kind=draft.kind,
                safe_summary=_safe_text(summary),
                base_revision=draft.base_revision,
                committed_revision=self.conversation_revision,
            )
            self.messages.append(message)
            return message
        draft.resolution = "dropped" if action == "drop" else "deferred"
        self.touch()
        return draft

    def register_conflict(
        self,
        *,
        safe_summary: str,
        member_ids: list[str] | None = None,
    ) -> CollaborationConflict:
        conflict = CollaborationConflict(
            conflict_id=f"conflict-{uuid4()}",
            safe_summary=_safe_text(safe_summary),
            member_ids=list(dict.fromkeys(member_ids or []))[:4],
        )
        self.conflicts[conflict.conflict_id] = conflict
        self.touch()
        return conflict

    def resolve_conflict(self, conflict_id: str, resolution_summary: str) -> None:
        conflict = self.conflicts[conflict_id]
        conflict.status = "resolved"
        conflict.resolution_summary = _safe_text(resolution_summary)
        self.touch()

    def assess_closure(
        self,
        members: list[dict[str, Any]],
        *,
        approvals_pending: bool = False,
        cancelled: bool = False,
    ) -> CollaborationClosureAssessment:
        counts = _member_counts(members)
        open_conflicts = sum(
            1 for conflict in self.conflicts.values() if conflict.status == "open"
        )
        if cancelled:
            outcome = "cancelled"
            synthesis_ready = False
            needs_user = False
            summary = (
                "用户或父运行已停止协作，成员已回收，保留已有结果。"
            )
        elif approvals_pending:
            outcome = "waiting_approval"
            synthesis_ready = False
            needs_user = True
            summary = (
                "协作结果已收口，仍需用户授权后才能执行外部动作。"
            )
        elif counts["completed"] == len(members) and not open_conflicts:
            outcome = "ready_for_synthesis"
            synthesis_ready = True
            needs_user = False
            summary = (
                "全部协作者的结果已回收，冲突已结算；"
                "待沈行知对照验收条件统一交付。"
            )
        elif counts["completed"] > 0:
            outcome = "partial"
            synthesis_ready = True
            needs_user = False
            summary = (
                "已获得可用的部分结果；沈行知将明确缺口后完成降级交付。"
            )
        else:
            outcome = "failed"
            synthesis_ready = False
            needs_user = True
            summary = "没有形成可验收结果，协作以明确失败结案。"
        # Member process completion is not semantic acceptance.  Only the
        # parent Agent can verify these criteria while synthesizing the final
        # user-visible answer.
        completed_criteria: list[str] = []
        unmet_criteria = [
            criterion
            for criterion in self.contract.acceptance_criteria
            if criterion not in completed_criteria
        ]
        self.closure = CollaborationClosureAssessment(
            outcome=outcome,
            synthesis_ready=synthesis_ready,
            deliverable_ready=False,
            user_action_required=needs_user,
            completed_members=counts["completed"],
            failed_members=counts["failed"],
            cancelled_members=counts["cancelled"],
            open_conflicts=open_conflicts,
            acceptance_met=completed_criteria,
            acceptance_unmet=unmet_criteria,
            summary=summary,
        )
        self.touch()
        return self.closure

    def public_snapshot(
        self,
        *,
        batch_status: str,
        members: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Return a replayable, privacy-bounded room projection."""

        counts = _member_counts(members)
        active = counts["active"]
        terminal = counts["completed"] + counts["failed"] + counts["cancelled"]
        if self.phase == "closed" and self.closure:
            quiet_summary = self.closure.summary
        elif self.phase in {"reconciling", "verifying"}:
            quiet_summary = (
                "沈行知正在统一结果、处理分歧并检查验收条件。"
            )
        elif active:
            quiet_summary = (
                f"{active} 位协作者正在推进，{terminal} 位已进入终态。"
            )
        else:
            quiet_summary = "沈行知正在组织本次协作。"

        return {
            "schema_version": 3,
            "batch_id": self.contract.contract_id,
            "revision": self.revision,
            "conversation_revision": self.conversation_revision,
            "status": batch_status,
            "phase": self.phase,
            "current_round": self.current_round,
            "participation_mode": self.contract.participation_mode,
            "protocol": self.contract.protocol,
            "host": self.contract.host.model_dump(mode="json"),
            "goal": self.contract.goal,
            "why_parallel": self.contract.why_parallel,
            "acceptance_criteria": list(self.contract.acceptance_criteria),
            "members": [_public_member_projection(member) for member in members],
            "quiet_summary": quiet_summary,
            "user_action_required": bool(
                self.closure and self.closure.user_action_required
            ),
            "unattended": self.contract.participation_mode == "autonomous",
            "messages": [
                message.model_dump(mode="json") for message in self.messages[-24:]
            ],
            "held_drafts": [
                draft.model_dump(mode="json")
                for draft in self.held_drafts.values()
                if draft.resolution in {"pending", "deferred"}
            ],
            "conflicts": [
                conflict.model_dump(mode="json")
                for conflict in self.conflicts.values()
            ],
            "host_decisions": [
                decision.model_dump(mode="json") for decision in self.decisions[-16:]
            ],
            "closure": (
                self.closure.model_dump(mode="json") if self.closure else None
            ),
            "controls": {
                "can_takeover": self.phase != "closed",
                "can_stop": self.phase != "closed",
                "can_pause": self.phase not in {"closing", "closed"},
            },
        }

    def clone_public_snapshot(
        self,
        *,
        batch_status: str,
        members: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return deepcopy(
            self.public_snapshot(batch_status=batch_status, members=members),
        )
