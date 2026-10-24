# -*- coding: utf-8 -*-
"""Factual, durable progress reports for the existing Runtime control plane."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Literal

from .cost_accounting import BudgetLedger
from .effects import RuntimeEffectJournal
from .runs import (
    TERMINAL_RUNTIME_RUN_STATUSES,
    RuntimeEffectRecord,
    RuntimeProgressCostSummary,
    RuntimeProgressEffectSummary,
    RuntimeProgressReport,
    RuntimeProgressReportKind,
    RuntimeRunContract,
)


_SETTLED_EFFECT_STATUSES = {
    "blocked",
    "committed",
    "failed",
    "probed_committed",
    "safe_to_retry",
    "compensated",
    "human_resolved",
    "cancelled",
}
_ACCEPTED_RESULT_STATUSES = {
    "ok",
    "passed",
    "success",
    "succeeded",
    "verified",
}


def _append_unique(items: list[str], value: object) -> None:
    text = str(value or "").strip()
    if text and text not in items:
        items.append(text)


def _acceptance_result_passes(
    acceptance: str,
    results: tuple[dict[str, object], ...],
) -> bool:
    expected = acceptance.strip().casefold()
    for result in results:
        status = str(result.get("status") or "").strip().casefold()
        if status not in _ACCEPTED_RESULT_STATUSES:
            continue
        identities = (
            result.get("acceptance"),
            result.get("check"),
            result.get("id"),
            result.get("title"),
        )
        if any(str(value or "").strip().casefold() == expected for value in identities):
            return True
    return False


def _latest_effects(
    workspace_dir: Path,
    run: RuntimeRunContract,
) -> dict[str, RuntimeEffectRecord]:
    latest: dict[str, RuntimeEffectRecord] = {}
    for effect in RuntimeEffectJournal(workspace_dir).list_effects(
        run.run_id,
        run.attempt_id,
    ):
        current = latest.get(effect.effect_id)
        if current is None or effect.revision > current.revision:
            latest[effect.effect_id] = effect
    return latest


def _effect_summary(
    workspace_dir: Path,
    run: RuntimeRunContract,
) -> RuntimeProgressEffectSummary:
    latest = _latest_effects(workspace_dir, run)
    counts = Counter(effect.status for effect in latest.values())
    unresolved = tuple(
        sorted(
            effect_id
            for effect_id, effect in latest.items()
            if effect.status not in _SETTLED_EFFECT_STATUSES
        )
    )
    ambiguous = tuple(
        sorted(
            effect_id
            for effect_id, effect in latest.items()
            if effect.status == "ambiguous"
        )
    )
    return RuntimeProgressEffectSummary(
        status_counts=dict(sorted(counts.items())),
        unresolved_effect_ids=unresolved,
        ambiguous_effect_ids=ambiguous,
    )


def _cost_summary(
    workspace_dir: Path,
    run: RuntimeRunContract,
) -> RuntimeProgressCostSummary:
    task = run.task_contract
    grant = task.autonomy_grant if task is not None else None
    limit = grant.max_cost_micro_cny if grant is not None else None
    if limit is None:
        return RuntimeProgressCostSummary()
    ledger = BudgetLedger(
        workspace_dir,
        limit_micro_cny=limit,
        scope_run_id=run.run_id,
    )
    entries = ledger.entries()
    return RuntimeProgressCostSummary(
        limit_micro_cny=limit,
        reserved_micro_cny=ledger.reserved_micro_cny,
        committed_micro_cny=ledger.committed_micro_cny,
        settled_count=sum(entry.status == "settled" for entry in entries),
        pending_count=sum(
            entry.status in {"reserved", "dispatched"} for entry in entries
        ),
        ambiguous_count=sum(entry.status == "ambiguous" for entry in entries),
        overrun_count=sum(entry.status == "disputed_overrun" for entry in entries),
    )


def build_runtime_progress_report(
    run: RuntimeRunContract,
    *,
    workspace_dir: Path | str,
    kind: RuntimeProgressReportKind,
    now: datetime | None = None,
    narrative: str | None = None,
    narrative_error: str | None = None,
) -> RuntimeProgressReport:
    """Build a report from canonical contracts, journals, and cost ledgers.

    Narrative generation is optional and never blocks the factual report.  A
    failed narrative attempt remains explicit while all structured facts stay
    available to the task center.
    """

    generated_at = now or datetime.now(timezone.utc)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    root = Path(workspace_dir).expanduser().resolve(strict=False)
    task = run.task_contract
    grant = task.autonomy_grant if task is not None else None
    capsule = run.checkpoint.continuation_capsule if run.checkpoint else None
    evidence = run.completion_evidence

    acceptance_results = evidence.acceptance_results if evidence is not None else ()
    acceptance = task.acceptance if task is not None else ()
    open_acceptance = tuple(
        item
        for item in acceptance
        if not _acceptance_result_passes(item, acceptance_results)
    )

    artifact_refs: list[str] = []
    if capsule is not None:
        for artifact in capsule.artifacts:
            _append_unique(artifact_refs, artifact.artifact_ref)
            _append_unique(artifact_refs, artifact.relative_path)
    if evidence is not None:
        for artifact_id in evidence.primary_artifact_ids:
            _append_unique(artifact_refs, artifact_id)

    blocked_reasons: list[str] = []
    if capsule is not None:
        for reason in capsule.blocked_reasons:
            _append_unique(blocked_reasons, reason)
    if run.error is not None:
        _append_unique(blocked_reasons, run.error.message or run.error.code)
    if run.stop_reason is not None:
        _append_unique(blocked_reasons, run.stop_reason.message or run.stop_reason.code)
    if run.wait_reason is not None:
        _append_unique(blocked_reasons, f"wait_reason:{run.wait_reason}")

    effects = _effect_summary(root, run)
    costs = _cost_summary(root, run)
    decisions: list[str] = []
    if run.wait_reason == "deadline":
        decisions.append("extend_deadline_and_resume_or_stop")
    if run.status == "waiting_approval":
        decisions.append("resolve_pending_approval")
    if effects.ambiguous_effect_ids:
        decisions.append("resolve_ambiguous_external_effects")
    if grant is not None and grant.revocation_generation > 0:
        decisions.append("issue_new_grant_before_unattended_resume")

    unknown_facts: list[str] = []
    if run.status in {"paused", "waiting_approval"}:
        unknown_facts.append("wait_seconds_not_recorded")
    if costs.ambiguous_count:
        unknown_facts.append("ambiguous_provider_cost")
    if effects.ambiguous_effect_ids:
        unknown_facts.append("ambiguous_external_effect_outcome")
    if narrative_error:
        unknown_facts.append("narrative_generation_failed")

    narrative_text = str(narrative or "").strip() or None
    narrative_failure = str(narrative_error or "").strip() or None
    narrative_status: Literal["not_requested", "generated", "failed"]
    if narrative_failure is not None:
        narrative_status = "failed"
        narrative_text = None
    elif narrative_text is not None:
        narrative_status = "generated"
    else:
        narrative_status = "not_requested"

    end_at = run.settled_at or generated_at
    elapsed_seconds = max(0.0, (end_at - run.created_at).total_seconds())
    payload: dict[str, object] = {
        "schema_version": 1,
        "kind": kind,
        "generated_at": generated_at,
        "run_id": run.run_id,
        "attempt_id": run.attempt_id,
        "task_revision": task.revision if task is not None else None,
        "contract_revision": run.revision,
        "status": run.status,
        "terminal": run.status in TERMINAL_RUNTIME_RUN_STATUSES,
        "objective": task.objective if task is not None else "",
        "completed_milestones": (
            capsule.completed_milestones if capsule is not None else ()
        ),
        "acceptance_results": acceptance_results,
        "open_acceptance_items": open_acceptance,
        "artifact_refs": tuple(artifact_refs),
        "blocked_reasons": tuple(blocked_reasons),
        "user_decisions": tuple(decisions),
        "effect_summary": effects,
        "cost_summary": costs,
        "active_seconds": run.checkpoint.active_seconds if run.checkpoint else None,
        "elapsed_seconds": elapsed_seconds,
        "wait_seconds": None,
        "unknown_facts": tuple(unknown_facts),
        "grant_id": grant.grant_id if grant is not None else None,
        "grant_revision": grant.revision if grant is not None else None,
        "grant_expires_at": grant.expires_at if grant is not None else None,
        "grant_revoked": (
            grant.revocation_generation > 0 if grant is not None else None
        ),
        "wake_key": run.wake_intent.wake_key if run.wake_intent else None,
        "wait_reason": run.wait_reason,
        "narrative_status": narrative_status,
        "narrative": narrative_text,
        "narrative_error": narrative_failure,
    }
    encoded = json.dumps(
        payload,
        default=str,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    payload["report_id"] = f"progress-v1:{hashlib.sha256(encoded).hexdigest()}"
    return RuntimeProgressReport.model_validate(payload)


__all__ = ["build_runtime_progress_report"]
