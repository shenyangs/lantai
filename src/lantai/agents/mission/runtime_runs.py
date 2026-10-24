# -*- coding: utf-8 -*-
"""RuntimeRunStore adapter for Mission Mode loops."""

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
    RuntimeRunStatus,
)
from ...runtime.runs import (
    TERMINAL_RUNTIME_RUN_STATUSES,
    RuntimeRunReducerAction,
    reduce_runtime_run,
)
from ...runtime.store import RuntimeRunStore
from .state import read_loop_config

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


def _workspace_dir_for_loop(loop_dir: Path) -> Path:
    cfg = read_loop_config(loop_dir)
    workspace_dir = str(cfg.get("workspace_dir") or "").strip()
    if workspace_dir:
        return Path(workspace_dir)
    return loop_dir.parent.parent


def _session_id_for_loop(loop_dir: Path) -> str | None:
    session_id = str(read_loop_config(loop_dir).get("session_id") or "").strip()
    return session_id or None


def _base_mission_contract(
    *,
    loop_dir: Path,
    agent_id: str,
    now: datetime,
) -> RuntimeRunContract:
    workspace_dir = _workspace_dir_for_loop(loop_dir)
    session_id = _session_id_for_loop(loop_dir)
    run_id = loop_dir.name
    attempt_id = f"{run_id}:mission-attempt-1"
    identity = {
        "source": "mission",
        "agent_id": agent_id,
        "session_id": session_id,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "loop_dir": str(loop_dir),
        "workspace_dir": str(workspace_dir),
    }
    release_edition = _release_edition_from_environment()
    return RuntimeRunContract(
        run_id=run_id,
        attempt_id=attempt_id,
        source="mission",
        owner=RuntimeRunOwner(
            user_id="default",
            agent_id=agent_id,
            session_id=session_id,
            workspace_id=str(workspace_dir),
            channel="console",
        ),
        status="queued",
        revision=0,
        created_at=now,
        contract_hash=_stable_sha256(identity),
        policy_snapshot_hash=_stable_sha256(
            {
                "source": "mission",
                "agent_id": agent_id,
                "release_edition": release_edition,
            },
        ),
        release_edition=release_edition,
    )


def _start_mission_contract(contract: RuntimeRunContract) -> RuntimeRunContract:
    lease_owner = f"mission:{contract.run_id}"
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


def _terminal_reason(status: RuntimeRunStatus) -> RuntimeRunReason | None:
    if status == "failed":
        return RuntimeRunReason(
            code="mission_failed",
            message="Mission failed.",
            category="runtime",
        )
    if status == "stalled":
        return RuntimeRunReason(
            code="mission_max_iterations_reached",
            message="Mission reached its maximum iteration budget.",
            category="runtime",
            retryable=True,
        )
    if status == "cancelled":
        return RuntimeRunReason(
            code="mission_cancelled",
            message="Mission was cancelled.",
            category="user",
            retryable=True,
        )
    return None


async def record_mission_runtime_run_started(
    *,
    loop_dir: Path | str,
    agent_id: str,
) -> RuntimeRunContract:
    """Create the canonical RuntimeRunContract for a Mission loop."""

    loop_path = Path(loop_dir)
    workspace_dir = _workspace_dir_for_loop(loop_path)
    store = RuntimeRunStore(workspace_dir)
    existing = await store.load_contract(loop_path.name)
    if existing is not None:
        return existing

    base = _base_mission_contract(
        loop_dir=loop_path,
        agent_id=agent_id or "default",
        now=_utc_now(),
    )
    started = _start_mission_contract(base)
    return await store.save_contract(started, expected_revision=None)


async def record_mission_runtime_run_settled(
    *,
    loop_dir: Path | str,
    agent_id: str,
    status: RuntimeRunStatus,
) -> RuntimeRunContract:
    """Settle the canonical RuntimeRunContract for a Mission loop."""

    if status not in TERMINAL_RUNTIME_RUN_STATUSES:
        raise ValueError(f"Mission RuntimeRun settle requires terminal status: {status}")

    existing = await record_mission_runtime_run_started(
        loop_dir=loop_dir,
        agent_id=agent_id,
    )
    if existing.status in TERMINAL_RUNTIME_RUN_STATUSES:
        return existing

    workspace_dir = _workspace_dir_for_loop(Path(loop_dir))
    settled = reduce_runtime_run(
        existing,
        RuntimeRunReducerAction(
            type="settle",
            status=status,
            expected_attempt_id=existing.attempt_id,
            expected_revision=existing.revision,
            lease_owner=existing.lease_owner,
            reason=_terminal_reason(status),
            now=_utc_now(),
        ),
    ).run
    return await RuntimeRunStore(workspace_dir).save_contract(
        settled,
        expected_revision=existing.revision,
        expected_attempt_id=existing.attempt_id,
        lease_owner=existing.lease_owner,
    )
