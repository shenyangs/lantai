# -*- coding: utf-8 -*-
"""Behavior locks for factual Runtime deadline and completion reports."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from lantai.runtime.continuation import build_continuation_capsule
from lantai.runtime.cost_accounting import BudgetLedger, BudgetMode
from lantai.runtime.effects import RuntimeEffectJournal
from lantai.runtime.reporting import build_runtime_progress_report
from lantai.runtime.runs import (
    RuntimeAutonomyGrant,
    RuntimeBudget,
    RuntimeCheckpoint,
    RuntimeCompletionEvidence,
    RuntimeContinuationArtifact,
    RuntimeContinuationMilestone,
    RuntimeEffectRecord,
    RuntimeRunContract,
    RuntimeRunOwner,
    RuntimeTaskContract,
)


NOW = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)


def _owner(workspace_id: str) -> RuntimeRunOwner:
    return RuntimeRunOwner(
        user_id="user-1",
        agent_id="agent-1",
        session_id="console:chat-1",
        workspace_id=workspace_id,
        channel="console",
    )


def _grant(workspace_id: str) -> RuntimeAutonomyGrant:
    return RuntimeAutonomyGrant(
        grant_id="grant-1",
        approved=True,
        issued_by="user",
        run_id="run-1",
        chat_id="chat-1",
        workspace_id=workspace_id,
        target_task_revision=0,
        issued_at=NOW - timedelta(hours=1),
        starts_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=1),
        allowed_operations=("model_call", "local_read", "local_write_new"),
        allowed_paths=(workspace_id,),
        allowed_provider_ids=("provider-1",),
        allowed_model_ids=("model-1",),
        budget_currency="CNY",
        max_cost_micro_cny=100,
        model_price_bounds=(
            {
                "provider_id": "provider-1",
                "model_id": "model-1",
                "pricing_version": "provider-1-2026-09-20",
                "max_dispatch_micro_cny": 50,
                "upper_bound_proven": True,
            },
        ),
    )


def _run(tmp_path) -> RuntimeRunContract:
    workspace_id = str(tmp_path.resolve())
    task = RuntimeTaskContract(
        run_id="run-1",
        attempt_id="attempt-1",
        objective="Produce a verified overnight artifact.",
        acceptance=("Artifact exists.", "Review passed."),
        budget=RuntimeBudget(deadline_at=NOW),
        autonomy_grant=_grant(workspace_id),
    )
    milestone = RuntimeContinuationMilestone(
        milestone_id="milestone-1",
        statement="Artifact exists.",
        evidence_refs=("tool-result:verify-1",),
    )
    artifact = RuntimeContinuationArtifact(
        artifact_ref="artifact:report-v1",
        relative_path="deliverables/report.md",
        content_sha256="a" * 64,
        size_bytes=128,
        modified_ns=1,
    )
    capsule = build_continuation_capsule(
        task_contract=task,
        task_id="task-1",
        chat_id="chat-1",
        run_id="run-1",
        attempt_id="attempt-1",
        context_generation=2,
        covered_event_seq=20,
        workspace_dir=tmp_path,
        completed_milestones=(milestone,),
        artifacts=(artifact,),
        blocked_reasons=("Awaiting final review.",),
        provider_id="provider-1",
        model_id="model-1",
    )
    return RuntimeRunContract(
        run_id="run-1",
        attempt_id="attempt-1",
        source="chat",
        owner=_owner(workspace_id),
        status="paused",
        revision=8,
        created_at=NOW - timedelta(hours=2),
        started_at=NOW - timedelta(hours=2),
        heartbeat_at=NOW - timedelta(minutes=1),
        budget=task.budget,
        task_contract=task,
        wait_reason="deadline",
        completion_evidence=RuntimeCompletionEvidence(
            acceptance_results=(
                {"acceptance": "Artifact exists.", "status": "verified"},
            ),
            primary_artifact_ids=("artifact:report-v1",),
        ),
        checkpoint=RuntimeCheckpoint(
            checkpoint_id="checkpoint-1",
            seq=20,
            continuation_capsule=capsule,
            active_seconds=1800,
        ),
        release_edition="official",
    )


def _effect(
    tmp_path,
    *,
    effect_id: str,
    status: str,
) -> RuntimeEffectRecord:
    return RuntimeEffectRecord(
        run_id="run-1",
        attempt_id="attempt-1",
        step_id=f"step-{effect_id}",
        effect_id=effect_id,
        source="chat",
        owner=_owner(str(tmp_path.resolve())),
        status=status,  # type: ignore[arg-type]
        revision=1,
        tool_name="test.effect",
        args_hash=f"sha256:args:{effect_id}",
        policy_snapshot_hash="sha256:policy",
        idempotency_key=f"run-1:attempt-1:{effect_id}",
        created_at=NOW,
        release_edition="official",
    )


def test_deadline_report_projects_durable_facts_without_claiming_completion(
    tmp_path,
) -> None:
    run = _run(tmp_path)
    journal = RuntimeEffectJournal(tmp_path)
    journal.append_effect(_effect(tmp_path, effect_id="committed", status="committed"))
    journal.append_effect(_effect(tmp_path, effect_id="uncertain", status="ambiguous"))
    ledger = BudgetLedger(
        tmp_path,
        limit_micro_cny=100,
        scope_run_id="run-1",
    )
    ledger.reserve(
        reservation_id="dispatch-1",
        run_id="run-1",
        amount_micro_cny=40,
        mode=BudgetMode.VERIFIED_UPPER_BOUND,
        upper_bound_proven=True,
    )
    ledger.claim_dispatch("dispatch-1")
    ledger.settle("dispatch-1", actual_micro_cny=25)
    ledger.reserve(
        reservation_id="dispatch-2",
        run_id="run-1",
        amount_micro_cny=50,
        mode=BudgetMode.VERIFIED_UPPER_BOUND,
        upper_bound_proven=True,
    )
    ledger.claim_dispatch("dispatch-2")
    ledger.mark_ambiguous("dispatch-2")

    report = build_runtime_progress_report(
        run,
        workspace_dir=tmp_path,
        kind="deadline_snapshot",
        now=NOW,
    )

    assert report.status == "paused"
    assert report.terminal is False
    assert report.completed_milestones[0].statement == "Artifact exists."
    assert report.open_acceptance_items == ("Review passed.",)
    assert report.artifact_refs == (
        "artifact:report-v1",
        "deliverables/report.md",
    )
    assert report.blocked_reasons == (
        "Awaiting final review.",
        "wait_reason:deadline",
    )
    assert report.effect_summary.status_counts == {
        "ambiguous": 1,
        "committed": 1,
    }
    assert report.effect_summary.ambiguous_effect_ids == ("uncertain",)
    assert report.cost_summary.committed_micro_cny == 25
    assert report.cost_summary.reserved_micro_cny == 50
    assert report.cost_summary.settled_count == 1
    assert report.cost_summary.ambiguous_count == 1
    assert "extend_deadline_and_resume_or_stop" in report.user_decisions
    assert "resolve_ambiguous_external_effects" in report.user_decisions
    assert "ambiguous_provider_cost" in report.unknown_facts
    assert report.narrative_status == "not_requested"


def test_narrative_failure_keeps_the_structured_report(tmp_path) -> None:
    report = build_runtime_progress_report(
        _run(tmp_path),
        workspace_dir=tmp_path,
        kind="deadline_snapshot",
        now=NOW,
        narrative="must not survive a failed generation",
        narrative_error="provider unavailable",
    )

    assert report.narrative_status == "failed"
    assert report.narrative is None
    assert report.narrative_error == "provider unavailable"
    assert report.completed_milestones[0].statement == "Artifact exists."
    assert report.open_acceptance_items == ("Review passed.",)
    assert "narrative_generation_failed" in report.unknown_facts
