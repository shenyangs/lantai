# -*- coding: utf-8 -*-
"""Runtime stall watchdog for orphaned pending chat turns."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .task_tracker import read_control_snapshot
from .models import ChatSpec
from .runtime_state import (
    pending_turn_dict,
    pending_turn_run_id,
    pending_turn_text,
)
from ..experience import (
    RuntimeUsageSummary,
    append_chat_event,
    build_turn_event_preview,
    record_chat_turn_settled,
    record_chat_turn_stop_requested,
    resolve_effective_workspace_dir_from_meta,
    runtime_usage_summary_for_run,
)
from ..experience.schemas import TurnEventPreview
from ...runtime import (
    ACTIVE_RUNTIME_RUN_STATUSES,
    LoopBudget,
    LoopBudgetUsage,
    LoopContract,
    LoopDeliverable,
    LoopDeterministicGate,
    LoopRubric,
    LoopRubricDimension,
    RuntimeRunContract,
    RuntimeRunStore,
    RuntimeRunStoreConflictError,
    assess_loop_runtime_run_stop_conditions,
    save_runtime_run_loop_stop_assessment,
)


DEFAULT_STALL_SECONDS = 90
_TERMINAL_PENDING_STATUSES = {
    "cancelled",
    "canceled",
    "completed",
    "failed",
    "finished",
    "interrupted",
    "stalled",
}


def runtime_stall_seconds() -> int:
    """Return the pending-turn stall threshold in seconds."""

    raw = os.getenv("LANTAI_RUNTIME_STALL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_STALL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_STALL_SECONDS
    return max(1, value)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def load_turn_event_preview(
    chat_spec: ChatSpec,
    *,
    root_workspace_dir: Path | str | None = None,
) -> TurnEventPreview | None:
    """Load the compact runtime preview for a chat's effective workspace."""

    effective_workspace_dir = resolve_effective_workspace_dir_from_meta(
        chat_spec.meta or {},
        root_workspace_dir=root_workspace_dir,
    )
    if effective_workspace_dir is None:
        return None
    return await asyncio.to_thread(
        build_turn_event_preview,
        effective_workspace_dir,
        chat_spec.id,
    )


def last_pending_turn_activity_at(
    chat_spec: ChatSpec,
    turn_event_preview: TurnEventPreview | None,
) -> datetime | None:
    """Return the latest known activity timestamp for a pending turn."""

    candidates: list[datetime] = []
    for event in getattr(turn_event_preview, "events", []) or []:
        parsed = _parse_datetime(event.ts)
        if parsed is not None:
            candidates.append(parsed)

    pending_turn = pending_turn_dict(chat_spec)
    for key in ("updated_at", "started_at", "created_at"):
        parsed = _parse_datetime(pending_turn.get(key))
        if parsed is not None:
            candidates.append(parsed)

    return max(candidates) if candidates else None


def _has_active_run(
    tracker_status: str, active_run: dict[str, Any] | None
) -> bool:
    active_status = str((active_run or {}).get("status") or "").lower()
    occupied_statuses = {"running", "stopping", "cleanup_failed", "cleaning_up"}
    return tracker_status in occupied_statuses or active_status in occupied_statuses


async def _resource_snapshot(tracker, run_key: str) -> tuple[str, dict[str, Any] | None]:
    """Keep terminal cleanup out of orphan recovery without changing public idle."""
    status, snapshot = await asyncio.gather(
        tracker.get_status(run_key), read_control_snapshot(tracker, run_key),
    )
    settlement_snapshot = getattr(tracker, "get_settlement_snapshot", None)
    if settlement_snapshot is not None:
        settlement = await settlement_snapshot(run_key)
        if settlement is not None:
            return str(settlement.get("status") or status), settlement
    return status, snapshot


def _is_pending_candidate(pending_turn: dict[str, Any]) -> bool:
    status = str(pending_turn.get("status") or "").lower()
    if status in _TERMINAL_PENDING_STATUSES:
        return False
    return bool(
        pending_turn_text(pending_turn) or pending_turn_run_id(pending_turn)
    )


def should_mark_stalled(
    *,
    chat_spec: ChatSpec,
    tracker_status: str,
    active_run: dict[str, Any] | None,
    turn_event_preview: TurnEventPreview | None,
    now: datetime | None = None,
    threshold_seconds: int | None = None,
) -> bool:
    """Return whether a pending chat turn should be marked as stalled."""

    pending_turn = pending_turn_dict(chat_spec)
    if not _is_pending_candidate(pending_turn):
        return False
    if _has_active_run(tracker_status, active_run):
        return False
    last_activity_at = last_pending_turn_activity_at(
        chat_spec, turn_event_preview
    )
    if last_activity_at is None:
        return False
    current = now or _now()
    threshold = threshold_seconds or runtime_stall_seconds()
    return (current - last_activity_at).total_seconds() >= threshold


async def mark_chat_stalled_if_needed(
    *,
    mgr,
    workspace,
    chat_spec: ChatSpec,
    tracker_status: str,
    active_run: dict[str, Any] | None,
    turn_event_preview: TurnEventPreview | None,
    now: datetime | None = None,
    threshold_seconds: int | None = None,
    force: bool = False,
) -> tuple[ChatSpec, bool]:
    """Persist a stalled pending turn and append one RuntimeEvent if needed."""

    latest = await mgr.get_chat(chat_spec.id)
    chat_spec = latest or chat_spec
    pending_turn = pending_turn_dict(chat_spec)
    if force:
        if not _is_pending_candidate(pending_turn) or _has_active_run(
            tracker_status,
            active_run,
        ):
            return chat_spec, False
    elif not should_mark_stalled(
        chat_spec=chat_spec,
        tracker_status=tracker_status,
        active_run=active_run,
        turn_event_preview=turn_event_preview,
        now=now,
        threshold_seconds=threshold_seconds,
    ):
        return chat_spec, False

    current = now or _now()
    threshold = threshold_seconds or runtime_stall_seconds()
    last_activity_at = last_pending_turn_activity_at(
        chat_spec,
        turn_event_preview,
    )
    if last_activity_at is None:
        return chat_spec, False

    effective_workspace_dir = resolve_effective_workspace_dir_from_meta(
        chat_spec.meta or {},
        root_workspace_dir=workspace.workspace_dir,
    )
    if effective_workspace_dir is None:
        return chat_spec, False

    run_id = pending_turn_run_id(pending_turn) or chat_spec.id
    elapsed_seconds = max(0, int((current - last_activity_at).total_seconds()))
    stalled_at = _iso(current)
    updated_pending_turn = {
        **pending_turn,
        "status": "stalled",
        "stalled_at": stalled_at,
        "last_activity_at": _iso(last_activity_at),
        "stalled_reason": "pending_turn_without_active_task",
    }
    updated = await mgr.merge_chat_meta(
        chat_spec.id,
        {
            "last_turn_run_id": run_id,
            "pending_turn": updated_pending_turn,
        },
    )
    chat_spec = updated or chat_spec
    await append_chat_event(
        workspace_dir=effective_workspace_dir,
        agent_id=str(getattr(workspace, "agent_id", "") or "default"),
        chat_id=chat_spec.id,
        session_id=chat_spec.session_id,
        run_id=run_id,
        kind="chat.system.event",
        type="turn.stalled",
        status="stalled",
        summary="本轮可能卡住",
        payload={
            "phase": "failure",
            "trace_title": "本轮可能卡住",
            "trace_detail": (f"{threshold} 秒内没有新的运行事件，且后端没有活动任务"),
            "reason": "pending_turn_without_active_task",
            "last_activity_at": _iso(last_activity_at),
            "elapsed_seconds": elapsed_seconds,
            "threshold_seconds": threshold,
            "workspace_dir": str(effective_workspace_dir),
        },
        source="system",
    )
    return chat_spec, True


async def reconcile_pending_chat_stall(
    *,
    mgr,
    workspace,
    chat_spec: ChatSpec,
    force: bool = False,
) -> tuple[ChatSpec, bool]:
    """Reconcile a persisted pending turn against the live task tracker."""

    tracker_status, active_run = await _resource_snapshot(workspace.task_tracker, chat_spec.id)
    if _has_active_run(tracker_status, active_run):
        return chat_spec, False
    preview = await load_turn_event_preview(
        chat_spec,
        root_workspace_dir=workspace.workspace_dir,
    )
    return await mark_chat_stalled_if_needed(
        mgr=mgr,
        workspace=workspace,
        chat_spec=chat_spec,
        tracker_status=tracker_status,
        active_run=active_run,
        turn_event_preview=preview,
        force=force,
    )


async def settle_orphaned_pending_turn_on_stop(
    *,
    mgr,
    workspace,
    chat_spec: ChatSpec,
    requested_chat_id: str,
) -> tuple[ChatSpec, bool]:
    """Confirm stop when no task exists but a pending turn is still persisted.

    This is not an optimistic stop: the tracker is checked again first.  The
    terminal event and metadata only reconcile an already-inactive orphan.
    """

    latest = await mgr.get_chat(chat_spec.id)
    chat_spec = latest or chat_spec
    pending_turn = pending_turn_dict(chat_spec)
    if not _is_pending_candidate(pending_turn):
        return chat_spec, False
    tracker_status, active_run = await _resource_snapshot(workspace.task_tracker, chat_spec.id)
    if _has_active_run(tracker_status, active_run):
        return chat_spec, False
    effective_workspace_dir = resolve_effective_workspace_dir_from_meta(
        chat_spec.meta or {},
        root_workspace_dir=workspace.workspace_dir,
    )
    if effective_workspace_dir is None:
        return chat_spec, False

    run_id = pending_turn_run_id(pending_turn) or chat_spec.id
    await record_chat_turn_stop_requested(
        workspace_dir=effective_workspace_dir,
        agent_id=str(getattr(workspace, "agent_id", "") or "default"),
        chat_id=chat_spec.id,
        session_id=chat_spec.session_id,
        requested_chat_id=requested_chat_id,
        resolved_chat_id=chat_spec.id,
    )
    await record_chat_turn_settled(
        workspace_dir=effective_workspace_dir,
        agent_id=str(getattr(workspace, "agent_id", "") or "default"),
        chat_id=chat_spec.id,
        session_id=chat_spec.session_id,
        final_task_status="interrupted",
        outcome_status="incomplete",
        pending_turn_cleared=True,
        active_run_event_count=0,
        run_id=run_id,
        delivery_status=str(
            (chat_spec.meta or {}).get("last_turn_delivery_status")
            or "not_delivered",
        ),
    )
    settled_at = _iso(_now())
    updated = await mgr.merge_chat_meta(
        chat_spec.id,
        {
            "last_turn_run_id": run_id,
            "last_turn_status": "interrupted",
            "last_turn_final_task_status": "interrupted",
            "last_turn_settled_at": settled_at,
            "last_result_summary": "运行已停止，已生成内容仍已保留",
            "last_turn_outcome_status": "incomplete",
            "last_turn_outcome_run_id": run_id,
            "last_turn_delivery_status": str(
                (chat_spec.meta or {}).get("last_turn_delivery_status")
                or "not_delivered",
            ),
        },
        remove_keys=["pending_turn"],
    )
    return updated or chat_spec, updated is not None


async def refresh_stalled_chats(
    workspace,
    chats: list[ChatSpec] | None = None,
) -> list[ChatSpec]:
    """Apply the stall watchdog to a batch of chats for list-style APIs."""

    mgr = workspace.chat_manager
    tracker = workspace.task_tracker
    source_chats = chats if chats is not None else await mgr.list_chats()
    refreshed: list[ChatSpec] = []
    for chat_spec in source_chats:
        # Almost every chat in a mature profile is idle.  Replaying each
        # chat's complete event ledger just to prove that it has no pending
        # turn makes list-style endpoints scale with all historical data.
        # Pending-turn metadata is the cheap authoritative gate for whether
        # the watchdog has any work to do.
        if not _is_pending_candidate(pending_turn_dict(chat_spec)):
            refreshed.append(chat_spec)
            continue
        tracker_status, active_run = await _resource_snapshot(tracker, chat_spec.id)
        if _has_active_run(tracker_status, active_run):
            refreshed.append(chat_spec)
            continue
        turn_event_preview = await load_turn_event_preview(
            chat_spec,
            root_workspace_dir=workspace.workspace_dir,
        )
        updated, _ = await mark_chat_stalled_if_needed(
            mgr=mgr,
            workspace=workspace,
            chat_spec=chat_spec,
            tracker_status=tracker_status,
            active_run=active_run,
            turn_event_preview=turn_event_preview,
        )
        refreshed.append(updated)
    return refreshed


def _runtime_watchdog_contract(run: RuntimeRunContract) -> LoopContract:
    """Build a transient LoopContract projection from a RuntimeRun budget."""

    return LoopContract(
        contract_id=f"runtime-watchdog:{run.run_id}",
        objective="Assess RuntimeRun watchdog, lease, and wall-clock budget state",
        scope=["RuntimeRun liveness assessment"],
        deliverables=[
            LoopDeliverable(
                deliverable_id="runtime-run-state",
                title="RuntimeRun terminal state",
                acceptance=[
                    "Only structured RuntimeRun budget or lease limits settle a run",
                ],
            )
        ],
        acceptance=[
            "RuntimeRun reducer and store fences remain the only terminal write path",
        ],
        deterministic_gates=[
            LoopDeterministicGate(
                gate_id="runtime-run-store",
                name="RuntimeRunStore fence",
                kind="manual_check",
                expected="Persisted via RuntimeRunStore.save_contract",
            )
        ],
        rubric=LoopRubric(
            rubric_id="runtime-watchdog",
            dimensions=[
                LoopRubricDimension(
                    dimension_id="contract-liveness",
                    name="Contract liveness",
                    weight=1,
                )
            ],
        ),
        budgets=LoopBudget(
            max_attempts=run.budget.max_turns,
            max_steps=run.budget.max_steps,
            max_tool_calls=run.budget.max_tool_calls,
            max_tokens=run.budget.max_tokens,
            max_cost_points=run.budget.max_cost_points,
            max_wall_seconds=run.budget.max_seconds,
            max_artifact_bytes=run.budget.max_artifact_bytes,
        ),
    )


def _runtime_watchdog_usage(
    run: RuntimeRunContract,
    *,
    now: datetime,
    event_usage: RuntimeUsageSummary | None = None,
) -> LoopBudgetUsage:
    started_at = run.started_at or run.created_at
    wall_seconds = max(
        0, (now - started_at.astimezone(timezone.utc)).total_seconds()
    )
    tokens = 0
    cost_points = 0.0
    if event_usage is not None:
        tokens = int(
            (event_usage.input_tokens or 0) + (event_usage.output_tokens or 0)
        )
        cost_points = float(event_usage.cost_points or 0)
        tool_calls = int(event_usage.tool_calls or 0)
        artifact_bytes = int(event_usage.artifact_bytes or 0)
    else:
        tool_calls = 0
        artifact_bytes = 0
    steps = 0
    if run.current_step is not None:
        steps = run.current_step.index + 1
    return LoopBudgetUsage(
        attempts=1,
        steps=steps,
        tool_calls=max(0, tool_calls),
        tokens=max(0, tokens),
        cost_points=max(0.0, cost_points),
        wall_seconds=wall_seconds,
        artifact_bytes=max(0, artifact_bytes),
    )


async def refresh_stalled_runtime_runs(
    workspace_dirs: list[Path | str],
    *,
    now: datetime | None = None,
) -> list[RuntimeRunContract]:
    """Settle active RuntimeRunStore contracts whose structured limits expired."""

    current = now or _now()
    settled: list[RuntimeRunContract] = []
    seen_paths: set[str] = set()
    for workspace_dir in workspace_dirs:
        path = Path(workspace_dir).expanduser()
        key = str(path)
        if key in seen_paths:
            continue
        seen_paths.add(key)
        store = RuntimeRunStore(path)
        for run in await store.list_contracts_async():
            if run.status not in ACTIVE_RUNTIME_RUN_STATUSES:
                continue
            assessment = assess_loop_runtime_run_stop_conditions(
                _runtime_watchdog_contract(run),
                run,
                usage=_runtime_watchdog_usage(
                    run,
                    now=current,
                    event_usage=runtime_usage_summary_for_run(
                        path,
                        run.run_id,
                        attempt_id=run.attempt_id,
                    ),
                ),
                now=current,
            )
            if not assessment.should_stop:
                continue
            try:
                saved = await save_runtime_run_loop_stop_assessment(
                    store,
                    run,
                    assessment,
                    expected_attempt_id=run.attempt_id,
                    expected_revision=run.revision,
                    lease_owner=run.lease_owner,
                    now=current,
                )
            except RuntimeRunStoreConflictError:
                continue
            if saved is not None:
                settled.append(saved)
    return settled
