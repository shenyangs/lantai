#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cross-process smoke for RuntimeRun watchdog restart recovery."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lantai.app.runner.stall_watchdog import refresh_stalled_runtime_runs
from lantai.runtime import RuntimeBudget, RuntimeRunContract, RuntimeRunOwner
from lantai.runtime.store import RuntimeRunStore


NOW = datetime(2026, 7, 17, 9, 0, tzinfo=timezone.utc)
RUN_IDS = (
    "run-expired-lease",
    "run-expired-deadline",
    "run-healthy",
)


def _owner(workspace_dir: Path) -> RuntimeRunOwner:
    return RuntimeRunOwner(
        user_id="default",
        agent_id="agent-default",
        session_id="console:restart-smoke",
        workspace_id=str(workspace_dir),
        channel="console",
    )


def _contract(
    workspace_dir: Path,
    *,
    run_id: str,
    lease_expires_at: datetime,
    deadline_at: datetime | None,
) -> RuntimeRunContract:
    return RuntimeRunContract(
        run_id=run_id,
        attempt_id=f"{run_id}:attempt-1",
        source="chat",
        owner=_owner(workspace_dir),
        status="running",
        revision=2,
        lease_owner=f"worker:{run_id}",
        lease_expires_at=lease_expires_at,
        created_at=NOW - timedelta(minutes=5),
        started_at=NOW - timedelta(minutes=4),
        heartbeat_at=NOW - timedelta(seconds=10),
        budget=RuntimeBudget(deadline_at=deadline_at),
        contract_hash="sha256:restart-smoke-contract",
        policy_snapshot_hash="sha256:restart-smoke-policy",
        release_edition="official",
    )


async def _write_phase(workspace_dir: Path) -> dict[str, object]:
    workspace_dir.mkdir(parents=True, exist_ok=True)
    store = RuntimeRunStore(workspace_dir)
    contracts = (
        _contract(
            workspace_dir,
            run_id="run-expired-lease",
            lease_expires_at=NOW - timedelta(seconds=1),
            deadline_at=None,
        ),
        _contract(
            workspace_dir,
            run_id="run-expired-deadline",
            lease_expires_at=NOW + timedelta(minutes=5),
            deadline_at=NOW - timedelta(seconds=1),
        ),
        _contract(
            workspace_dir,
            run_id="run-healthy",
            lease_expires_at=NOW + timedelta(minutes=5),
            deadline_at=NOW + timedelta(minutes=5),
        ),
    )
    for contract in contracts:
        await store.save_contract(contract, expected_revision=None)
    return {
        "phase": "write",
        "pid": _pid(),
        "runs": [contract.run_id for contract in contracts],
    }


async def _refresh_phase(workspace_dir: Path) -> dict[str, object]:
    settled = await refresh_stalled_runtime_runs([workspace_dir], now=NOW)
    return {
        "phase": "refresh",
        "pid": _pid(),
        "settled": [run.run_id for run in settled],
    }


async def _read_phase(workspace_dir: Path) -> dict[str, object]:
    store = RuntimeRunStore(workspace_dir)
    runs: list[dict[str, object]] = []
    for run_id in RUN_IDS:
        contract = await store.load_contract(run_id)
        if contract is None:
            runs.append({"run_id": run_id, "status": "missing"})
            continue
        runs.append(
            {
                "run_id": contract.run_id,
                "status": contract.status,
                "revision": contract.revision,
                "error_code": contract.error.code if contract.error else None,
            }
        )
    return {
        "phase": "read",
        "pid": _pid(),
        "runs": runs,
    }


def _pid() -> int:
    try:
        import os

        return os.getpid()
    except OSError:
        return -1


def _run_child(workspace_dir: Path, phase: str) -> dict[str, object]:
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--workspace",
            str(workspace_dir),
            "--phase",
            phase,
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(completed.stdout)


def _run_parent(workspace_dir: Path) -> dict[str, object]:
    writer = _run_child(workspace_dir, "write")
    watchdog = _run_child(workspace_dir, "refresh")
    reader = _run_child(workspace_dir, "read")
    settled = set(watchdog["settled"])
    runs = list(reader["runs"])
    by_run = {item["run_id"]: item for item in runs}
    return {
        "ok": (
            settled == {"run-expired-lease", "run-expired-deadline"}
            and by_run.get("run-expired-lease", {}).get("status") == "stalled"
            and by_run.get("run-expired-deadline", {}).get("status") == "paused"
            and by_run.get("run-healthy", {}).get("status") == "running"
        ),
        "writer_process": writer["pid"],
        "watchdog_process": watchdog["pid"],
        "reader_process": reader["pid"],
        "runs": runs,
    }


async def _run_phase(workspace_dir: Path, phase: str) -> dict[str, object]:
    if phase == "write":
        return await _write_phase(workspace_dir)
    if phase == "refresh":
        return await _refresh_phase(workspace_dir)
    if phase == "read":
        return await _read_phase(workspace_dir)
    raise ValueError(f"unsupported phase: {phase}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument(
        "--phase",
        choices=("parent", "write", "refresh", "read"),
        default="parent",
    )
    args = parser.parse_args(argv)

    if args.phase == "parent":
        result = _run_parent(args.workspace)
    else:
        result = asyncio.run(_run_phase(args.workspace, args.phase))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if bool(result.get("ok", True)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
