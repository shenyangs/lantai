# -*- coding: utf-8 -*-
"""RuntimeRunStore adapter for BrowserTaskRuntime."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from ...runtime.runs import (
    RuntimeRunContract,
    RuntimeRunOwner,
    RuntimeRunReason,
    RuntimeRunStatus,
    RuntimeStepContract,
    RuntimeStepRef,
    start_runtime_run_attempt,
)
from ...runtime.runs import (
    TERMINAL_RUNTIME_RUN_STATUSES,
    RuntimeRunReducerAction,
    RuntimeRunTransitionError,
    reduce_runtime_run,
)
from ...runtime.store import (
    RuntimeRunStore,
    RuntimeRunStoreConflictError,
    RuntimeRunStoreError,
)
from .models import BrowserTaskRun, BrowserTaskStatus


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stable_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _workspace_dir_for_run(run: BrowserTaskRun) -> Path | None:
    raw = str(run.metadata.get("workspace_dir") or "").strip()
    return Path(raw).expanduser() if raw else None


def _release_edition(
    run: BrowserTaskRun,
    *,
    app_store_distribution: bool,
) -> str:
    raw = (
        str(
            run.metadata.get("release_edition")
            or run.metadata.get("distribution")
            or "",
        )
        .strip()
        .lower()
    )
    if raw in {"mas", "app_store", "app-store", "appstore", "testflight"}:
        return "mas"
    return "mas" if app_store_distribution else "official"


def _owner(run: BrowserTaskRun, *, workspace_dir: Path) -> RuntimeRunOwner:
    return RuntimeRunOwner(
        user_id=str(run.metadata.get("user_id") or "default"),
        agent_id=str(run.metadata.get("agent_id") or "browser"),
        session_id=str(run.metadata.get("session_id") or run.run_id),
        workspace_id=str(workspace_dir),
        channel="browser",
    )


def _base_contract(
    run: BrowserTaskRun,
    *,
    workspace_dir: Path,
    app_store_distribution: bool,
    now: datetime,
) -> RuntimeRunContract:
    attempt_id = str(
        run.metadata.get("attempt_id") or f"{run.run_id}:browser-attempt",
    )
    identity = {
        "source": "browser",
        "run_id": run.run_id,
        "attempt_id": attempt_id,
        "profile_id": run.profile_id,
        "target_domains": run.target_domains,
        "access": run.access,
        "workspace_dir": str(workspace_dir),
    }
    release_edition = _release_edition(
        run,
        app_store_distribution=app_store_distribution,
    )
    return RuntimeRunContract(
        run_id=run.run_id,
        attempt_id=attempt_id,
        source="browser",
        owner=_owner(run, workspace_dir=workspace_dir),
        status="queued",
        revision=0,
        created_at=now,
        contract_hash=_stable_sha256(identity),
        policy_snapshot_hash=str(
            run.metadata.get("policy_snapshot_hash")
            or _stable_sha256(
                {
                    "source": "browser",
                    "access": run.access,
                    "target_domains": run.target_domains,
                    "release_edition": release_edition,
                },
            ),
        ),
        release_edition=release_edition,  # type: ignore[arg-type]
    )


def _start_contract(contract: RuntimeRunContract) -> RuntimeRunContract:
    lease_owner = f"browser:{contract.run_id}"
    claimed = reduce_runtime_run(
        contract,
        RuntimeRunReducerAction(
            type="claim",
            expected_revision=0,
            lease_owner=lease_owner,
            now=contract.created_at,
        ),
    ).run
    return reduce_runtime_run(
        claimed,
        RuntimeRunReducerAction(
            type="start",
            expected_attempt_id=claimed.attempt_id,
            expected_revision=claimed.revision,
            lease_owner=lease_owner,
            now=contract.created_at,
        ),
    ).run


def _terminal_status(status: BrowserTaskStatus) -> RuntimeRunStatus | None:
    if status == "completed":
        return "succeeded"
    if status == "failed":
        return "failed"
    if status == "cancelled":
        return "cancelled"
    if status == "manual_takeover_required":
        return "stalled"
    return None


def _terminal_reason(
    run: BrowserTaskRun,
    status: RuntimeRunStatus,
) -> RuntimeRunReason | None:
    if status == "failed":
        return RuntimeRunReason(
            code=str(run.error_code or "browser_task_failed"),
            message=str(run.result.get("message") or "Browser task failed."),
            category="tool",
        )
    if status == "stalled":
        return RuntimeRunReason(
            code=str(
                run.error_code or "browser_task_manual_takeover_required",
            ),
            message=str(
                run.result.get("message")
                or "Browser task requires manual takeover.",
            ),
            category="tool",
            retryable=True,
        )
    if status == "cancelled":
        return RuntimeRunReason(
            code=str(run.error_code or "browser_task_cancelled"),
            message=str(
                run.result.get("message") or "Browser task cancelled.",
            ),
            category="user",
            retryable=True,
        )
    return None


async def record_browser_task_runtime_run_started(
    run: BrowserTaskRun,
    *,
    app_store_distribution: bool,
) -> RuntimeRunContract | None:
    """Create a canonical RuntimeRunContract for a BrowserTaskRun."""

    workspace_dir = _workspace_dir_for_run(run)
    if workspace_dir is None:
        return None
    store = RuntimeRunStore(workspace_dir)
    existing = await store.load_contract(run.run_id)
    if existing is not None:
        return existing

    base = _base_contract(
        run,
        workspace_dir=workspace_dir,
        app_store_distribution=app_store_distribution,
        now=_utc_now(),
    )
    started = _start_contract(base)
    return await store.save_contract(started, expected_revision=None)


async def _save_browser_transition(
    store: RuntimeRunStore,
    existing: RuntimeRunContract,
    *,
    action: str,
    target: str,
    reason: RuntimeRunReason | None = None,
    can_settle: Callable[[], bool] | None = None,
) -> RuntimeRunContract:
    """Retry revision-only races without crossing an Attempt or lease."""
    original_attempt = existing.attempt_id
    original_lease = existing.lease_owner
    for retry in range(4):
        if (
            existing.status == target
            or existing.status in TERMINAL_RUNTIME_RUN_STATUSES
        ):
            return existing
        if can_settle is not None and not can_settle():
            return existing
        fields = dict(
            type=action,
            expected_attempt_id=existing.attempt_id,
            expected_revision=existing.revision,
            lease_owner=existing.lease_owner,
            now=_utc_now(),
        )
        if action == "settle":
            fields.update(status=target, reason=reason)
        reduced = reduce_runtime_run(
            existing, RuntimeRunReducerAction(**fields)
        ).run
        try:
            return await store.save_contract(
                reduced,
                expected_revision=existing.revision,
                expected_attempt_id=original_attempt,
                lease_owner=original_lease,
                commit_guard=can_settle,
            )
        except RuntimeRunStoreConflictError:
            latest = await store.load_contract(existing.run_id)
            if (
                retry == 3
                or latest is None
                or latest.attempt_id != original_attempt
                or latest.lease_owner != original_lease
            ):
                raise
            existing = latest
    raise AssertionError("unreachable")


async def record_browser_task_runtime_run_settled(
    run: BrowserTaskRun,
    *,
    app_store_distribution: bool,
    can_settle: Callable[[], bool] | None = None,
) -> RuntimeRunContract | None:
    """Settle the canonical RuntimeRunContract for a BrowserTaskRun."""

    status = _terminal_status(run.status)
    if status is None:
        return None
    existing = await record_browser_task_runtime_run_started(
        run,
        app_store_distribution=app_store_distribution,
    )
    if existing is None or existing.status in TERMINAL_RUNTIME_RUN_STATUSES:
        return existing
    if can_settle is not None and not can_settle():
        return existing

    workspace_dir = _workspace_dir_for_run(run)
    if workspace_dir is None:
        return None
    return await _save_browser_transition(
        RuntimeRunStore(workspace_dir),
        existing,
        action="settle",
        target=status,
        reason=_terminal_reason(run, status),
        can_settle=can_settle,
    )


async def touch_browser_task_runtime_run_heartbeat(
    run: BrowserTaskRun,
    *,
    now: datetime | None = None,
    current_step: RuntimeStepRef | None = None,
) -> RuntimeRunContract | None:
    """Refresh an active BrowserTask RuntimeRun heartbeat without trace
    events.
    """

    workspace_dir = _workspace_dir_for_run(run)
    if workspace_dir is None:
        return None
    store = RuntimeRunStore(workspace_dir)
    try:
        existing = await store.load_contract(run.run_id)
    except RuntimeRunStoreError:
        return None
    if (
        existing is None
        or existing.source != "browser"
        or existing.status == "queued"
        or existing.status in TERMINAL_RUNTIME_RUN_STATUSES
    ):
        return None

    try:
        touched = reduce_runtime_run(
            existing,
            RuntimeRunReducerAction(
                type="heartbeat",
                expected_attempt_id=existing.attempt_id,
                expected_revision=existing.revision,
                lease_owner=existing.lease_owner,
                current_step=current_step,
                now=now or _utc_now(),
            ),
        ).run
        return await store.save_contract(
            touched,
            expected_revision=existing.revision,
            expected_attempt_id=existing.attempt_id,
            lease_owner=existing.lease_owner,
        )
    except (
        RuntimeRunStoreConflictError,
        RuntimeRunStoreError,
        RuntimeRunTransitionError,
    ):
        return None


async def record_browser_task_runtime_run_control(
    run: BrowserTaskRun,
    *,
    action: str,
) -> RuntimeRunContract | None:
    """Project Browser controls into the canonical Runtime Run reducer."""

    if action not in {"wait_approval", "pause", "resume"}:
        raise ValueError(f"Unsupported browser Runtime control: {action}")
    workspace_dir = _workspace_dir_for_run(run)
    if workspace_dir is None:
        return None
    store = RuntimeRunStore(workspace_dir)
    try:
        existing = await store.load_contract(run.run_id)
        if (
            existing is None
            or existing.status in TERMINAL_RUNTIME_RUN_STATUSES
        ):
            return existing
        target = {
            "wait_approval": "waiting_approval",
            "pause": "paused",
            "resume": "running",
        }[action]
        if existing.status == target:
            return existing
        return await _save_browser_transition(
            store,
            existing,
            action=action,
            target=target,
        )
    except (
        RuntimeRunStoreConflictError,
        RuntimeRunStoreError,
        RuntimeRunTransitionError,
    ):
        return None


async def record_browser_task_runtime_takeover_resumed(
    run: BrowserTaskRun,
) -> RuntimeRunContract | None:
    """Create and start a new fenced Attempt after manual takeover."""

    workspace_dir = _workspace_dir_for_run(run)
    if workspace_dir is None:
        return None
    store = RuntimeRunStore(workspace_dir)
    try:
        existing = await store.load_contract(run.run_id)
        if existing is None or existing.status != "stalled":
            return None
        takeover_count = max(1, int(run.metadata.get("takeover_count") or 1))
        attempt = start_runtime_run_attempt(
            existing,
            attempt_id=(
                f"{run.run_id}"
                ":browser-attempt:takeover-"
                f"{takeover_count}"
            ),
            attempt_reason="takeover",
            lease_owner=f"browser:{run.run_id}",
            now=_utc_now(),
        )
        started = _start_contract(attempt)
        return await store.save_contract(
            started,
            expected_revision=existing.revision,
            expected_attempt_id=existing.attempt_id,
        )
    except (
        RuntimeRunStoreConflictError,
        RuntimeRunStoreError,
        RuntimeRunTransitionError,
        ValueError,
    ):
        return None


_PHASE_STEP_KIND = {
    "observing": "browser_action",
    "planning": "plan",
    "checking_policy": "approval",
    "executing": "browser_action",
    "verifying": "evaluation",
    "recovering": "plan",
}


async def record_browser_task_runtime_step(
    run: BrowserTaskRun,
    *,
    phase: str,
    index: int,
    turn_index: int,
    title: str,
) -> RuntimeStepContract | None:
    """Write one Browser Agent Turn/Step and point the Runtime Run at it."""

    workspace_dir = _workspace_dir_for_run(run)
    if workspace_dir is None:
        return None
    store = RuntimeRunStore(workspace_dir)
    try:
        contract = await store.load_contract(run.run_id)
        if (
            contract is None
            or contract.status in TERMINAL_RUNTIME_RUN_STATUSES
        ):
            return None
        now = _utc_now()
        attempt_token = hashlib.sha256(
            contract.attempt_id.encode("utf-8"),
        ).hexdigest()[:12]
        if phase == "planning":
            turn_step = RuntimeStepContract(
                run_id=run.run_id,
                attempt_id=contract.attempt_id,
                step_id=f"browser-{attempt_token}-turn-{turn_index}",
                source="browser",
                owner=contract.owner,
                index=max(0, index * 2),
                kind="turn",
                title=f"Browser Agent turn {turn_index}",
                status="succeeded",
                revision=1,
                created_at=now,
                started_at=now,
                heartbeat_at=now,
                settled_at=now,
                release_edition=contract.release_edition,
            )
            await store.save_step(turn_step)
        kind = _PHASE_STEP_KIND.get(phase, "browser_action")
        step = RuntimeStepContract(
            run_id=run.run_id,
            attempt_id=contract.attempt_id,
            step_id=f"browser-{attempt_token}-step-{index}-{phase}",
            source="browser",
            owner=contract.owner,
            index=max(0, index * 2 + 1),
            kind=kind,  # type: ignore[arg-type]
            title=title or phase,
            status="succeeded",
            revision=1,
            created_at=now,
            started_at=now,
            heartbeat_at=now,
            settled_at=now,
            release_edition=contract.release_edition,
        )
        await store.save_step(step)
        await touch_browser_task_runtime_run_heartbeat(
            run,
            current_step=RuntimeStepRef(
                step_id=step.step_id,
                index=step.index,
                kind=step.kind,
                title=step.title,
            ),
        )
        return step
    except (
        RuntimeRunStoreConflictError,
        RuntimeRunStoreError,
        RuntimeRunTransitionError,
        ValueError,
    ):
        return None
