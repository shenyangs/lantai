#!/usr/bin/env python3
"""Prove repeated packaged-app start/stop ownership without residual processes."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from packaged_app_lifecycle_smoke import (
    _app_metadata_for_evidence,
    _assert_desktop_readiness,
    _assert_ready_state,
    _find_app_executable,
    _install_termination_signal_handlers,
    _stop_app,
    _wait_for_state,
    _write_json_file,
)


EVIDENCE_SCHEMA = "com.lantai.packaged-app-rapid-lifecycle-smoke.v1"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _process_snapshot() -> dict[int, dict[str, Any]]:
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,ppid=,command="],
        check=True,
        capture_output=True,
        text=True,
    )
    snapshot: dict[int, dict[str, Any]] = {}
    for raw_line in result.stdout.splitlines():
        fields = raw_line.strip().split(None, 2)
        if len(fields) < 2:
            continue
        try:
            pid = int(fields[0])
            ppid = int(fields[1])
        except ValueError:
            continue
        snapshot[pid] = {
            "ppid": ppid,
            "command": fields[2] if len(fields) == 3 else "",
        }
    return snapshot


def _descendant_pids(
    snapshot: dict[int, dict[str, Any]], root_pid: int
) -> set[int]:
    descendants: set[int] = set()
    frontier = {root_pid}
    while frontier:
        parents = frontier
        frontier = {
            pid
            for pid, details in snapshot.items()
            if pid not in descendants
            and pid != root_pid
            and int(details.get("ppid") or 0) in parents
        }
        descendants.update(frontier)
    return descendants


def _matching_processes(*markers: str) -> dict[int, str]:
    normalized = tuple(marker for marker in markers if marker)
    if not normalized:
        return {}
    return {
        pid: str(details.get("command") or "")
        for pid, details in _process_snapshot().items()
        if any(marker in str(details.get("command") or "") for marker in normalized)
    }


def _assert_clean_process_baseline(*markers: str) -> None:
    matching = {
        pid: command
        for pid, command in _matching_processes(*markers).items()
        if pid != os.getpid()
    }
    if matching:
        raise AssertionError(
            "packaged lifecycle baseline already contains candidate processes: "
            f"{matching}"
        )


def _backend_process_pids(
    snapshot: dict[int, dict[str, Any]], backend_executable: Path
) -> set[int]:
    marker = str(backend_executable.resolve())
    return {
        pid
        for pid, details in snapshot.items()
        if (
            (command := str(details.get("command") or "").strip()) == marker
            or command.startswith(f"{marker} app ")
        )
    }


def _wait_for_process_cleanup(
    *,
    tracked_pids: set[int],
    markers: tuple[str, ...],
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_tracked: dict[int, str] = {}
    last_matching: dict[int, str] = {}
    while time.monotonic() < deadline:
        snapshot = _process_snapshot()
        last_tracked = {
            pid: str(snapshot[pid].get("command") or "")
            for pid in tracked_pids
            if pid in snapshot
        }
        last_matching = {
            pid: command
            for pid, command in _matching_processes(*markers).items()
            if pid != os.getpid()
        }
        if not last_tracked and not last_matching:
            return {"tracked_alive": {}, "matching_alive": {}}
        time.sleep(0.1)
    return {
        "tracked_alive": {str(pid): command for pid, command in last_tracked.items()},
        "matching_alive": {str(pid): command for pid, command in last_matching.items()},
    }


def _run(args: argparse.Namespace) -> None:
    app_path = Path(args.app).expanduser().resolve()
    executable = _find_app_executable(app_path)
    backend_executable = (
        app_path
        / "Contents"
        / "Resources"
        / "lantai-server"
        / "Lantai Backend.app"
        / "Contents"
        / "MacOS"
        / "lantai-server"
    )
    if not backend_executable.is_file():
        raise AssertionError(f"packaged backend executable is missing: {backend_executable}")
    # Never normalize a leak from an earlier lifecycle probe into the stress
    # run's baseline. Candidate-local desktop, backend, or detached MCP
    # processes must all be gone before cycle one starts.
    _assert_clean_process_baseline(str(executable), str(backend_executable))
    smoke_root = Path(tempfile.mkdtemp(prefix="lantai-rapid-lifecycle-"))
    user_data_dir = smoke_root / "user-data"
    state_path = smoke_root / "desktop-lifecycle-state.json"
    log_path = smoke_root / "app.log"
    evidence_path = Path(args.evidence_output).expanduser().resolve()
    started_at = _utc_now_iso()
    records: list[dict[str, Any]] = []
    observed_backend_pids: set[int] = set()
    backend_pid_reuse_count = 0
    duplicate_backend_process_count = 0
    maximum_simultaneous_backend_processes = 0
    observed_nonces: set[str] = set()
    restore_handlers = _install_termination_signal_handlers()
    proc: subprocess.Popen[str] | None = None
    failure: str | None = None

    env = os.environ.copy()
    env.update(
        {
            "LANTAI_DESKTOP_LIFECYCLE_SMOKE_FILE": str(state_path),
            "LANTAI_DESKTOP_USER_DATA_DIR": str(user_data_dir),
            "LANTAI_DESKTOP_HEADLESS_SMOKE": "1",
            "LANTAI_AUTH_ENABLED": "0",
            "LANTAI_DISABLE_GITHUB_UPDATER": "1",
            "LANTAI_DISABLE_KEYRING": "1",
            "LANTAI_SKIP_KEYRING": "1",
            "LANTAI_REMINDER_DELIVERY_WORKER": "0",
        }
    )

    try:
        with log_path.open("a", encoding="utf-8") as log_file:
            for index in range(1, args.cycles + 1):
                state_path.unlink(missing_ok=True)
                cycle_started = time.monotonic()
                proc = subprocess.Popen(
                    [str(executable)],
                    cwd=str(app_path.parent),
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                state = _wait_for_state(
                    state_path,
                    timeout=args.timeout,
                    predicate=lambda payload: int(payload.get("sequence") or 0) >= 1,
                    process=proc,
                    expected_distribution=args.distribution,
                )
                _assert_ready_state(state, expected_distribution=args.distribution)
                backend_pid = _assert_desktop_readiness(state, timeout=args.timeout)
                nonce = str(state.get("desktop_instance_nonce") or "")
                if backend_pid in observed_backend_pids:
                    # Sequential PID reuse is legal after the previous process is gone.
                    backend_pid_reuse_count += 1
                if nonce in observed_nonces:
                    raise AssertionError(
                        f"desktop instance nonce was reused during stress run: {nonce}"
                    )
                observed_backend_pids.add(backend_pid)
                observed_nonces.add(nonce)

                snapshot = _process_snapshot()
                backend_pids = _backend_process_pids(snapshot, backend_executable)
                maximum_simultaneous_backend_processes = max(
                    maximum_simultaneous_backend_processes, len(backend_pids)
                )
                if backend_pids != {backend_pid}:
                    duplicate_backend_process_count += max(0, len(backend_pids) - 1)
                    raise AssertionError(
                        "packaged app did not own exactly one expected backend process: "
                        f"state_pid={backend_pid}, observed_pids={sorted(backend_pids)}"
                    )
                tracked = {proc.pid, backend_pid, *_descendant_pids(snapshot, proc.pid)}
                _stop_app(proc)
                proc = None
                cleanup = _wait_for_process_cleanup(
                    tracked_pids=tracked,
                    markers=(
                        str(user_data_dir),
                        str(executable),
                        str(backend_executable),
                    ),
                    timeout=args.cleanup_timeout,
                )
                if cleanup["tracked_alive"] or cleanup["matching_alive"]:
                    raise AssertionError(
                        f"cycle {index} left packaged app processes alive: {cleanup}"
                    )
                records.append(
                    {
                        "cycle": index,
                        "backend_pid": backend_pid,
                        "simultaneous_backend_process_count": len(backend_pids),
                        "desktop_instance_nonce": nonce,
                        "tracked_process_count": len(tracked),
                        "cleanup": cleanup,
                        "duration_seconds": round(time.monotonic() - cycle_started, 3),
                    }
                )
                if index == 1 or index % 10 == 0 or index == args.cycles:
                    print(f"Rapid lifecycle cycle {index}/{args.cycles} passed", flush=True)
    except Exception as exc:  # noqa: BLE001 - evidence must preserve the failure
        failure = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        _stop_app(proc)
        payload = {
            "schema": EVIDENCE_SCHEMA,
            "passed": failure is None and len(records) == args.cycles,
            "started_at": started_at,
            "completed_at": _utc_now_iso(),
            "distribution": args.distribution,
            "requested_cycles": args.cycles,
            "completed_cycles": len(records),
            "unique_backend_pid_count": len(observed_backend_pids),
            "backend_pid_reuse_count": backend_pid_reuse_count,
            "duplicate_backend_process_count": duplicate_backend_process_count,
            "maximum_simultaneous_backend_processes": (
                maximum_simultaneous_backend_processes
            ),
            "unique_desktop_instance_nonce_count": len(observed_nonces),
            "orphan_process_count": sum(
                len(record["cleanup"]["tracked_alive"])
                + len(record["cleanup"]["matching_alive"])
                for record in records
            ),
            "failure": failure,
            "app": _app_metadata_for_evidence(app_path),
            "cycles": records,
        }
        _write_json_file(evidence_path, payload)
        restore_handlers()
        if args.keep_workdir:
            print(f"Rapid lifecycle workdir: {smoke_root}")
        else:
            shutil.rmtree(smoke_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run repeated packaged-app start/stop lifecycle checks."
    )
    parser.add_argument("--app", required=True, help="Packaged .app bundle path")
    parser.add_argument("--cycles", type=int, default=100)
    parser.add_argument("--distribution", choices=("official",), default="official")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--cleanup-timeout", type=float, default=15.0)
    parser.add_argument("--evidence-output", required=True)
    parser.add_argument("--keep-workdir", action="store_true")
    args = parser.parse_args(argv)
    if args.cycles < 1:
        parser.error("--cycles must be at least 1")
    try:
        _run(args)
    except Exception as exc:  # noqa: BLE001 - CLI diagnostics
        print(f"Rapid packaged lifecycle smoke failed: {exc}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
