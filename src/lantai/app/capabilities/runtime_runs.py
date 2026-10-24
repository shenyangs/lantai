# -*- coding: utf-8 -*-
"""RuntimeRunStore facade for Capability and Pipeline TaskRun rows."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from ...runtime.runs import (
    RuntimeRunContract,
    RuntimeRunOwner,
    RuntimeRunReason,
    RuntimeRunSource,
    RuntimeRunStatus,
)
from ...runtime.runs import (
    TERMINAL_RUNTIME_RUN_STATUSES,
    RuntimeRunReducerAction,
    normalize_runtime_run_status,
    reduce_runtime_run,
)
from ...runtime.store import RuntimeRunStore
from .models import TaskRun

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


def _runtime_source(task_run: TaskRun) -> RuntimeRunSource:
    if task_run.source in {"chat", "cron", "capability", "pipeline"}:
        return task_run.source
    return "capability"


def _target_status(task_run: TaskRun) -> RuntimeRunStatus:
    if task_run.status == "blocked":
        return "waiting_approval"
    status = normalize_runtime_run_status(task_run.status)
    return status or "running"


def _owner(task_run: TaskRun, *, workspace_dir: Path) -> RuntimeRunOwner:
    return RuntimeRunOwner(
        user_id=str(task_run.metadata.get("user_id") or "default"),
        agent_id=task_run.agent_id,
        session_id=task_run.session_id,
        workspace_id=str(workspace_dir),
        channel=str(task_run.metadata.get("channel") or "capability"),
    )


def _base_contract(
    task_run: TaskRun,
    *,
    workspace_dir: Path,
    now: datetime,
) -> RuntimeRunContract:
    source = _runtime_source(task_run)
    attempt_id = str(
        task_run.metadata.get("attempt_id")
        or f"{task_run.id}:{source}-attempt-1",
    )
    release_edition = _release_edition_from_environment()
    identity = {
        "source": source,
        "task_run_id": task_run.id,
        "attempt_id": attempt_id,
        "agent_id": task_run.agent_id,
        "session_id": task_run.session_id,
        "workspace_dir": str(workspace_dir),
    }
    return RuntimeRunContract(
        run_id=task_run.id,
        attempt_id=attempt_id,
        source=source,
        owner=_owner(task_run, workspace_dir=workspace_dir),
        status="queued",
        revision=0,
        created_at=task_run.started_at or now,
        heartbeat_at=task_run.last_heartbeat_at,
        contract_hash=_stable_sha256(identity),
        policy_snapshot_hash=str(
            task_run.metadata.get("policy_snapshot_hash")
            or _stable_sha256(
                {
                    "source": source,
                    "agent_id": task_run.agent_id,
                    "release_edition": release_edition,
                }
            ),
        ),
        release_edition=release_edition,
    )


def _reason(task_run: TaskRun, status: RuntimeRunStatus) -> RuntimeRunReason | None:
    if status == "failed":
        return RuntimeRunReason(
            code=str(task_run.metadata.get("error_code") or "task_run_failed"),
            message=task_run.failure_reason or "Task run failed.",
            category="runtime",
        )
    if status == "stalled":
        return RuntimeRunReason(
            code=str(task_run.metadata.get("error_code") or "task_run_stalled"),
            message=task_run.failure_reason or "Task run stalled.",
            category="runtime",
            retryable=True,
        )
    if status == "cancelled":
        return RuntimeRunReason(
            code=str(task_run.metadata.get("error_code") or "task_run_cancelled"),
            message=task_run.failure_reason or "Task run cancelled.",
            category="user",
            retryable=True,
        )
    return None


def _heartbeat_contract(
    contract: RuntimeRunContract,
    *,
    task_run: TaskRun,
) -> RuntimeRunContract:
    heartbeat_at = task_run.last_heartbeat_at
    if (
        heartbeat_at is None
        or contract.status in TERMINAL_RUNTIME_RUN_STATUSES
        or (contract.heartbeat_at is not None and heartbeat_at <= contract.heartbeat_at)
    ):
        return contract
    return reduce_runtime_run(
        contract,
        RuntimeRunReducerAction(
            type="heartbeat",
            expected_attempt_id=contract.attempt_id,
            expected_revision=contract.revision,
            lease_owner=contract.lease_owner,
            now=heartbeat_at,
        ),
    ).run


def _advance_contract(
    contract: RuntimeRunContract,
    target: RuntimeRunStatus,
    *,
    task_run: TaskRun,
) -> RuntimeRunContract:
    current = contract
    now = _utc_now()
    lease_owner = current.lease_owner or f"{current.source}:{current.run_id}"
    if current.status == target:
        return _heartbeat_contract(current, task_run=task_run)
    if current.status in TERMINAL_RUNTIME_RUN_STATUSES:
        return current
    if current.status == "queued" and target != "queued":
        current = reduce_runtime_run(
            current,
            RuntimeRunReducerAction(
                type="claim",
                expected_revision=current.revision,
                lease_owner=lease_owner,
                now=now,
            ),
        ).run
    if current.status == "claimed" and target not in {"claimed"}:
        current = reduce_runtime_run(
            current,
            RuntimeRunReducerAction(
                type="start",
                expected_attempt_id=current.attempt_id,
                expected_revision=current.revision,
                lease_owner=lease_owner,
                now=now,
            ),
        ).run
    if target == current.status:
        return _heartbeat_contract(current, task_run=task_run)
    if target == "waiting_approval":
        return reduce_runtime_run(
            current,
            RuntimeRunReducerAction(
                type="wait_approval",
                expected_attempt_id=current.attempt_id,
                expected_revision=current.revision,
                lease_owner=current.lease_owner,
                now=now,
            ),
        ).run
    if target in TERMINAL_RUNTIME_RUN_STATUSES:
        return reduce_runtime_run(
            current,
            RuntimeRunReducerAction(
                type="settle",
                status=target,
                expected_attempt_id=current.attempt_id,
                expected_revision=current.revision,
                lease_owner=current.lease_owner,
                reason=_reason(task_run, target),
                now=now,
            ),
        ).run
    return current


async def record_task_run_runtime_contract(
    workspace_dir: Path | str,
    task_run: TaskRun,
) -> RuntimeRunContract:
    """Project a TaskRun row into the canonical RuntimeRunStore."""

    root = Path(workspace_dir).expanduser()
    store = RuntimeRunStore(root)
    existing = await store.load_contract(task_run.id)
    base = existing or _base_contract(task_run, workspace_dir=root, now=_utc_now())
    projected = _advance_contract(
        base,
        _target_status(task_run),
        task_run=task_run,
    )
    if existing is not None and projected == existing:
        return existing
    return await store.save_contract(
        projected,
        expected_revision=existing.revision if existing is not None else None,
        expected_attempt_id=existing.attempt_id if existing is not None else None,
        lease_owner=existing.lease_owner if existing is not None else None,
    )
