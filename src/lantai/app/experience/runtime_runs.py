# -*- coding: utf-8 -*-
"""RuntimeRunStore adapter for chat experience events."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from ...runtime.runs import (
    RuntimeBudget,
    RuntimeCompletionEvidence,
    RuntimeDeliveryStatus,
    RuntimeRunContract,
    RuntimeRunOwner,
    RuntimeRunReason,
    RuntimeRunStatus,
    RuntimeTaskContract,
)
from ...runtime.runs import (
    TERMINAL_RUNTIME_RUN_STATUSES,
    RuntimeRunReducerAction,
    RuntimeRunTransitionError,
    normalize_runtime_run_status,
    reduce_runtime_run,
)
from ...runtime.store import (
    RuntimeRunStore,
    RuntimeRunStoreConflictError,
    RuntimeRunStoreError,
)

RuntimeReleaseEditionText = Literal["official", "mas"]


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


def _release_edition_from_environment() -> RuntimeReleaseEditionText:
    app_store = os.environ.get("LANTAI_APP_STORE", "").strip().lower()
    channel = (
        os.environ.get("LANTAI_DISTRIBUTION_CHANNEL")
        or os.environ.get("LANTAI_RELEASE_CHANNEL")
        or ""
    ).strip().lower()
    if app_store in {"1", "true", "yes"} or channel in {
        "mas",
        "appstore",
        "app-store",
        "app_store",
        "testflight",
    }:
        return "mas"
    return "official"


def _owner(
    *,
    agent_id: str,
    session_id: str,
    effective_workspace_dir: str,
) -> RuntimeRunOwner:
    return RuntimeRunOwner(
        user_id="default",
        agent_id=agent_id,
        session_id=session_id,
        workspace_id=str(effective_workspace_dir),
        channel="console",
    )


def _terminal_status_from_final_task_status(value: str) -> RuntimeRunStatus:
    status = normalize_runtime_run_status(value) or "succeeded"
    return status if status in TERMINAL_RUNTIME_RUN_STATUSES else "succeeded"


def _terminal_reason(status: RuntimeRunStatus) -> RuntimeRunReason | None:
    if status == "failed":
        return RuntimeRunReason(
            code="chat_turn_failed",
            message="Chat turn failed.",
            category="runtime",
        )
    if status == "stalled":
        return RuntimeRunReason(
            code="chat_turn_stalled",
            message="Chat turn stalled.",
            category="runtime",
            retryable=True,
        )
    if status == "cancelled":
        return RuntimeRunReason(
            code="chat_turn_cancelled",
            message="Chat turn was interrupted.",
            category="user",
            retryable=True,
        )
    return None


def _cron_terminal_status(final_status: str) -> RuntimeRunStatus:
    text = str(final_status or "").strip().lower()
    if text in {"success", "succeeded", "completed", "finished", "ok"}:
        return "succeeded"
    if text in {"skipped", "deduped", "deduped_skip"}:
        return "cancelled"
    if text in {"cancelled", "canceled", "interrupted"}:
        return "cancelled"
    if text in {"stalled", "timed_out", "timeout"}:
        return "stalled"
    return "failed"


def _cron_terminal_reason(
    *,
    final_status: str,
    error_text: str | None,
) -> RuntimeRunReason | None:
    text = str(final_status or "").strip().lower()
    status = _cron_terminal_status(final_status)
    if status == "succeeded":
        return None
    if text in {"skipped", "deduped", "deduped_skip"}:
        return RuntimeRunReason(
            code="cron_job_skipped",
            message=error_text or "Cron invocation skipped by runtime policy.",
            category="runtime",
            retryable=True,
        )
    if status == "cancelled":
        return RuntimeRunReason(
            code="cron_job_cancelled",
            message=error_text or "Cron job was cancelled.",
            category="user",
            retryable=True,
        )
    if status == "stalled":
        return RuntimeRunReason(
            code="cron_job_stalled",
            message=error_text or "Cron job stalled.",
            category="runtime",
            retryable=True,
        )
    return RuntimeRunReason(
        code="cron_job_failed",
        message=error_text or "Cron job failed.",
        category="runtime",
    )


def _base_chat_contract(
    *,
    agent_id: str,
    chat_id: str,
    session_id: str,
    run_id: str,
    effective_workspace_dir: str,
    now: datetime,
    task_contract: RuntimeTaskContract | None = None,
) -> RuntimeRunContract:
    attempt_id = f"{run_id}:chat-attempt-1"
    if task_contract is not None:
        task_contract = task_contract.model_copy(
            update={"run_id": run_id, "attempt_id": attempt_id},
        )
    identity = {
        "source": "chat",
        "agent_id": agent_id,
        "chat_id": chat_id,
        "session_id": session_id,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "workspace_dir": effective_workspace_dir,
        "task_contract": (
            task_contract.model_dump(mode="json") if task_contract is not None else None
        ),
    }
    return RuntimeRunContract(
        run_id=run_id,
        attempt_id=attempt_id,
        source="chat",
        owner=_owner(
            agent_id=agent_id,
            session_id=session_id,
            effective_workspace_dir=effective_workspace_dir,
        ),
        status="queued",
        budget=task_contract.budget if task_contract is not None else RuntimeBudget(),
        task_contract=task_contract,
        revision=0,
        created_at=now,
        contract_hash=_stable_sha256(identity),
        policy_snapshot_hash=_stable_sha256(
            {
                "source": "chat",
                "agent_id": agent_id,
                "chat_id": chat_id,
                "release_edition": _release_edition_from_environment(),
            },
        ),
        release_edition=_release_edition_from_environment(),
    )


def _start_chat_contract(contract: RuntimeRunContract, *, chat_id: str) -> RuntimeRunContract:
    lease_owner = f"chat:{chat_id}"
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


async def record_chat_runtime_run_started(
    *,
    workspace_dir: Path | str,
    agent_id: str,
    chat_id: str,
    session_id: str,
    run_id: str,
    effective_workspace_dir: str,
    task_contract: RuntimeTaskContract | None = None,
) -> RuntimeRunContract:
    """Create the canonical RuntimeRunContract for one chat turn."""

    store = RuntimeRunStore(workspace_dir)
    existing = await store.load_contract(run_id)
    if existing is not None:
        if existing.status == "queued":
            started = _start_chat_contract(existing, chat_id=chat_id)
            return await store.save_contract(
                started,
                expected_revision=existing.revision,
                expected_attempt_id=existing.attempt_id,
            )
        return existing

    base = _base_chat_contract(
        agent_id=agent_id,
        chat_id=chat_id,
        session_id=session_id,
        run_id=run_id,
        effective_workspace_dir=effective_workspace_dir,
        now=_utc_now(),
        task_contract=task_contract,
    )
    started = _start_chat_contract(base, chat_id=chat_id)
    return await store.save_contract(started, expected_revision=None)


async def record_chat_runtime_run_settled(
    *,
    workspace_dir: Path | str,
    agent_id: str,
    chat_id: str,
    session_id: str,
    run_id: str,
    effective_workspace_dir: str,
    final_task_status: str,
    delivery_status: RuntimeDeliveryStatus = "pending",
    completion_evidence: RuntimeCompletionEvidence | None = None,
) -> RuntimeRunContract:
    """Settle the canonical RuntimeRunContract for one chat turn."""

    store = RuntimeRunStore(workspace_dir)
    existing = await store.load_contract(run_id)
    if existing is None:
        existing = await record_chat_runtime_run_started(
            workspace_dir=workspace_dir,
            agent_id=agent_id,
            chat_id=chat_id,
            session_id=session_id,
            run_id=run_id,
            effective_workspace_dir=effective_workspace_dir,
        )
    if existing.status in TERMINAL_RUNTIME_RUN_STATUSES:
        return existing

    terminal_status = _terminal_status_from_final_task_status(final_task_status)
    settled = reduce_runtime_run(
        existing,
        RuntimeRunReducerAction(
            type="settle",
            status=terminal_status,
            expected_attempt_id=existing.attempt_id,
            expected_revision=existing.revision,
            lease_owner=existing.lease_owner,
            reason=_terminal_reason(terminal_status),
            delivery_status=delivery_status,
            completion_evidence=completion_evidence,
            now=_utc_now(),
        ),
    ).run
    return await store.save_contract(
        settled,
        expected_revision=existing.revision,
        expected_attempt_id=existing.attempt_id,
        lease_owner=existing.lease_owner,
    )


async def touch_chat_runtime_run_heartbeat(
    *,
    workspace_dir: Path | str,
    run_id: str,
    now: datetime | None = None,
) -> RuntimeRunContract | None:
    """Refresh an active chat RuntimeRun heartbeat without writing trace events."""

    store = RuntimeRunStore(workspace_dir)
    try:
        existing = await store.load_contract(run_id)
    except RuntimeRunStoreError:
        return None
    if (
        existing is None
        or existing.source != "chat"
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


def _base_cron_contract(
    *,
    workspace_dir: Path | str,
    agent_id: str,
    job_id: str,
    job_name: str,
    task_type: str,
    channel: str,
    run_id: str,
    now: datetime,
) -> RuntimeRunContract:
    workspace_path = Path(workspace_dir).expanduser()
    attempt_id = f"{run_id}:cron-attempt-1"
    identity = {
        "source": "cron",
        "agent_id": agent_id,
        "job_id": job_id,
        "job_name": job_name,
        "task_type": task_type,
        "channel": channel,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "workspace_dir": str(workspace_path),
    }
    return RuntimeRunContract(
        run_id=run_id,
        attempt_id=attempt_id,
        source="cron",
        owner=RuntimeRunOwner(
            user_id="cron",
            agent_id=agent_id,
            session_id=f"cron:{job_id}",
            workspace_id=str(workspace_path),
            channel=channel or "cron",
        ),
        status="queued",
        revision=0,
        created_at=now,
        contract_hash=_stable_sha256(identity),
        policy_snapshot_hash=_stable_sha256(
            {
                "source": "cron",
                "agent_id": agent_id,
                "job_id": job_id,
                "task_type": task_type,
                "channel": channel,
                "release_edition": _release_edition_from_environment(),
            }
        ),
        release_edition=_release_edition_from_environment(),
    )


def _start_cron_contract(contract: RuntimeRunContract) -> RuntimeRunContract:
    lease_owner = f"cron:{contract.run_id}"
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


async def record_cron_runtime_run_started(
    *,
    workspace_dir: Path | str,
    agent_id: str,
    job_id: str,
    job_name: str,
    task_type: str,
    channel: str,
    run_id: str,
) -> RuntimeRunContract:
    store = RuntimeRunStore(workspace_dir)
    existing = await store.load_contract(run_id)
    if existing is not None:
        return existing
    base = _base_cron_contract(
        workspace_dir=workspace_dir,
        agent_id=agent_id,
        job_id=job_id,
        job_name=job_name,
        task_type=task_type,
        channel=channel,
        run_id=run_id,
        now=_utc_now(),
    )
    started = _start_cron_contract(base)
    return await store.save_contract(started, expected_revision=None)


async def touch_cron_runtime_run_heartbeat(
    *,
    workspace_dir: Path | str,
    run_id: str,
    now: datetime | None = None,
) -> RuntimeRunContract | None:
    """Refresh an active cron RuntimeRun heartbeat without writing trace events."""

    store = RuntimeRunStore(workspace_dir)
    try:
        existing = await store.load_contract(run_id)
    except RuntimeRunStoreError:
        return None
    if (
        existing is None
        or existing.source != "cron"
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


async def record_cron_runtime_run_settled(
    *,
    workspace_dir: Path | str,
    agent_id: str,
    job_id: str,
    job_name: str,
    task_type: str,
    channel: str,
    final_status: str,
    run_id: str,
    error_text: str | None = None,
) -> RuntimeRunContract:
    existing = await record_cron_runtime_run_started(
        workspace_dir=workspace_dir,
        agent_id=agent_id,
        job_id=job_id,
        job_name=job_name,
        task_type=task_type,
        channel=channel,
        run_id=run_id,
    )
    if existing.status in TERMINAL_RUNTIME_RUN_STATUSES:
        return existing
    status = _cron_terminal_status(final_status)
    settled = reduce_runtime_run(
        existing,
        RuntimeRunReducerAction(
            type="settle",
            status=status,
            expected_attempt_id=existing.attempt_id,
            expected_revision=existing.revision,
            lease_owner=existing.lease_owner,
            reason=_cron_terminal_reason(
                final_status=final_status,
                error_text=error_text,
            ),
            now=_utc_now(),
        ),
    ).run
    return await RuntimeRunStore(workspace_dir).save_contract(
        settled,
        expected_revision=existing.revision,
        expected_attempt_id=existing.attempt_id,
        lease_owner=existing.lease_owner,
    )
