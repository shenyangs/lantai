# -*- coding: utf-8 -*-
"""RuntimeRunStore adapter for due reminder invocations."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from ...runtime.runs import (
    TERMINAL_RUNTIME_RUN_STATUSES,
    RuntimeRunContract,
    RuntimeRunOwner,
    RuntimeRunReason,
    RuntimeRunReducerAction,
    RuntimeRunStatus,
    RuntimeStepRef,
    normalize_runtime_run_status,
    reduce_runtime_run,
)
from ...runtime.store import RuntimeRunStore
from .models import ReminderItem

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


def _reminder_terminal_status(final_status: str) -> RuntimeRunStatus:
    status = normalize_runtime_run_status(final_status)
    if status not in TERMINAL_RUNTIME_RUN_STATUSES:
        raise ValueError("Reminder settlement requires an explicit terminal status")
    return status


def _reminder_terminal_reason(
    *,
    final_status: str,
    error_text: str | None,
) -> RuntimeRunReason | None:
    status = _reminder_terminal_status(final_status)
    if status == "succeeded":
        return None
    if status == "cancelled":
        return RuntimeRunReason(
            code="reminder_invocation_cancelled",
            message=error_text or "Reminder invocation was cancelled.",
            category="runtime",
            retryable=True,
        )
    if status == "stalled":
        return RuntimeRunReason(
            code="reminder_invocation_stalled",
            message=error_text or "Reminder invocation stalled.",
            category="runtime",
            retryable=True,
        )
    return RuntimeRunReason(
        code="reminder_invocation_failed",
        message=error_text or "Reminder invocation failed.",
        category="runtime",
    )


def _base_reminder_contract(
    *,
    workspace_dir: Path | str,
    agent_id: str,
    reminder: ReminderItem,
    run_id: str,
    now: datetime,
) -> RuntimeRunContract:
    workspace_path = Path(workspace_dir).expanduser()
    attempt_id = f"{run_id}:reminder-attempt-1"
    identity = {
        "source": "reminder",
        "agent_id": agent_id,
        "reminder_id": reminder.id,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "workspace_dir": str(workspace_path),
    }
    return RuntimeRunContract(
        run_id=run_id,
        attempt_id=attempt_id,
        source="reminder",
        owner=RuntimeRunOwner(
            user_id="reminder",
            agent_id=agent_id,
            session_id=f"reminder:{reminder.id}",
            workspace_id=str(workspace_path),
            channel="reminder",
        ),
        status="queued",
        revision=0,
        created_at=now,
        current_step=RuntimeStepRef(
            step_id=f"{run_id}:delivery",
            index=0,
            kind="delivery",
            title=reminder.title,
        ),
        contract_hash=_stable_sha256(identity),
        policy_snapshot_hash=_stable_sha256(
            {
                "source": "reminder",
                "agent_id": agent_id,
                "reminder_id": reminder.id,
                "priority": reminder.priority,
                "release_edition": _release_edition_from_environment(),
            },
        ),
        release_edition=_release_edition_from_environment(),
    )


def _start_reminder_contract(contract: RuntimeRunContract) -> RuntimeRunContract:
    lease_owner = f"reminder:{contract.run_id}"
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


async def record_reminder_runtime_run_started(
    *,
    workspace_dir: Path | str,
    agent_id: str,
    reminder: ReminderItem,
    run_id: str,
) -> RuntimeRunContract:
    store = RuntimeRunStore(workspace_dir)
    existing = await store.load_contract(run_id)
    if existing is not None:
        if (existing.source != "reminder" or existing.owner.agent_id != agent_id
                or existing.owner.session_id != f"reminder:{reminder.id}"
                or Path(existing.owner.workspace_id or "").expanduser().resolve() != Path(workspace_dir).expanduser().resolve()
                or existing.attempt_id != f"{run_id}:reminder-attempt-1"):
            raise ValueError("Reminder RuntimeRun identity conflict")
        return existing
    base = _base_reminder_contract(
        workspace_dir=workspace_dir,
        agent_id=agent_id,
        reminder=reminder,
        run_id=run_id,
        now=_utc_now(),
    )
    started = _start_reminder_contract(base)
    return await store.save_contract(started, expected_revision=None)


async def record_reminder_runtime_run_settled(
    *,
    workspace_dir: Path | str,
    agent_id: str,
    reminder: ReminderItem,
    run_id: str,
    final_status: str,
    error_text: str | None = None,
) -> RuntimeRunContract:
    status = _reminder_terminal_status(final_status)
    existing = await record_reminder_runtime_run_started(
        workspace_dir=workspace_dir,
        agent_id=agent_id,
        reminder=reminder,
        run_id=run_id,
    )
    if existing.status in TERMINAL_RUNTIME_RUN_STATUSES:
        if existing.status != status:
            raise ValueError("Reminder RuntimeRun already settled with a different status")
        return existing
    settled = reduce_runtime_run(
        existing,
        RuntimeRunReducerAction(
            type="settle",
            status=status,
            expected_attempt_id=existing.attempt_id,
            expected_revision=existing.revision,
            lease_owner=existing.lease_owner,
            reason=_reminder_terminal_reason(
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
