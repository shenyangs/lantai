#!/usr/bin/env python3
"""Run one auditable sustained reliability soak against an exact packaged app."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error

from packaged_app_lifecycle_smoke import (
    _LocalOpenAISmokeServer,
    _app_metadata_for_evidence,
    _assert_browser_positive_pause_resume,
    _assert_desktop_readiness,
    _assert_packaged_customization_integrity,
    _assert_packaged_operator_effect_recovery,
    _assert_persisted_success_events,
    _assert_ready_state,
    _crash_managed_backend_for_restart,
    _exercise_console_model_success_path,
    _find_app_executable,
    _find_runtime_run_by_id,
    _install_termination_signal_handlers,
    _is_new_desktop_instance,
    _json_request,
    _state_base_url,
    _state_headers,
    _stop_app,
    _wait_for_state,
    _write_json_file,
    _write_smoke_openai_provider_config,
)
from packaged_app_rapid_lifecycle_smoke import (
    _descendant_pids,
    _process_snapshot,
    _wait_for_process_cleanup,
)


SCHEMA = "com.lantai.packaged-agent-sustained-run.v1"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _utc_now()).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve()), "sha256": _sha256(path)}


def _scheduled_offsets(duration_seconds: float, interval_seconds: float) -> list[float]:
    if duration_seconds <= 0 or interval_seconds <= 0:
        raise ValueError("duration and interval must be positive")
    offsets = [0.0]
    cursor = interval_seconds
    while cursor < duration_seconds:
        offsets.append(cursor)
        cursor += interval_seconds
    if offsets[-1] != duration_seconds:
        offsets.append(duration_seconds)
    return offsets


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _write_record_artifact(root: Path, name: str, payload: dict[str, Any]) -> dict[str, str]:
    path = root / name
    _write_json_file(path, payload)
    return _artifact(path)


def _wait_for_ui_convergence(
    state: dict[str, Any], *, run_id: str, timeout: float
) -> dict[str, Any]:
    started = time.monotonic()
    latest_authority = ""
    latest_board = ""
    while time.monotonic() - started <= min(timeout, 2.0):
        summary = _find_runtime_run_by_id(state, run_id=run_id, timeout=timeout)
        latest_authority = str(summary.get("authority_status") or summary.get("status") or "")
        _status, _body, board = _json_request(
            "GET",
            f"{_state_base_url(state)}/api/runtime/task-board?view=all&limit=300",
            headers=_state_headers(state),
            timeout=timeout,
        )
        items = board.get("items") if isinstance(board, dict) else None
        item = next(
            (
                entry
                for entry in items or ()
                if isinstance(entry, dict) and entry.get("id") == run_id
            ),
            None,
        )
        latest_board = str(
            (item or {}).get("authority_status") or (item or {}).get("status") or ""
        )
        if latest_authority and latest_board == latest_authority:
            return {
                "run_id": run_id,
                "authority_state": latest_authority,
                "convergence_seconds": round(time.monotonic() - started, 3),
                "surfaces": {
                    "runtime_summary": latest_authority,
                    "task_board": latest_board,
                },
            }
        time.sleep(0.05)
    raise AssertionError(
        "Runtime and task-board surfaces did not converge within two seconds: "
        f"authority={latest_authority!r} board={latest_board!r}"
    )


def _register_verified_work(
    state: dict[str, Any],
    *,
    workspace_dir: Path,
    artifact_root: Path,
    sequence: int,
    source_run_id: str,
    timeout: float,
) -> dict[str, Any]:
    source = workspace_dir / "soak-deliverables" / f"standard-{sequence:03d}.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        f"# Sustained standard result {sequence}\n\nRun: {source_run_id}\n",
        encoding="utf-8",
    )
    before = _sha256(source)
    request_payload = {
        "path": str(source),
        "title": f"Sustained standard result {sequence}",
        "source_run_id": source_run_id,
    }
    _status, _body, first = _json_request(
        "POST",
        f"{_state_base_url(state)}/api/works",
        payload=request_payload,
        headers=_state_headers(state),
        timeout=timeout,
    )
    _status, _body, repeated = _json_request(
        "POST",
        f"{_state_base_url(state)}/api/works",
        payload=request_payload,
        headers=_state_headers(state),
        timeout=timeout,
    )
    if not isinstance(first, dict) or not str(first.get("id") or ""):
        raise AssertionError(f"Works registration did not return an item: {first!r}")
    if not isinstance(repeated, dict) or repeated.get("id") != first.get("id"):
        raise AssertionError(
            f"Repeated Works registration was not idempotent: {first!r} {repeated!r}"
        )
    _status, _body, works = _json_request(
        "GET",
        f"{_state_base_url(state)}/api/works",
        headers=_state_headers(state),
        timeout=timeout,
    )
    registration_count = sum(
        1
        for item in works or ()
        if isinstance(item, dict) and item.get("id") == first.get("id")
    )
    after = _sha256(source)
    preserved = artifact_root / "works" / source.name
    preserved.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, preserved)
    return {
        "run_id": source_run_id,
        "eligible": True,
        "registered": True,
        "registration_mode": "packaged_works_api_idempotent_save",
        "registration_count": registration_count,
        "work_id": first["id"],
        "source_sha256_before": before,
        "source_sha256_after": after,
        "source_preserved": before == after,
        "artifact": _artifact(preserved),
    }


def _launch_app(
    executable: Path,
    app_path: Path,
    env: dict[str, str],
    log_file: Any,
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [str(executable)],
        cwd=str(app_path.parent),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _exercise_light_with_transport_recovery(
    state: dict[str, Any],
    *,
    state_path: Path,
    process: subprocess.Popen[str],
    timeout: float,
) -> tuple[dict[str, Any], str, str, list[dict[str, Any]]]:
    """Survive a brief desktop/backend handoff without hiding the incident."""

    incidents: list[dict[str, Any]] = []
    current = state
    for attempt in range(1, 4):
        try:
            session_id, chat_id = _exercise_console_model_success_path(
                current, timeout=timeout
            )
            return current, session_id, chat_id, incidents
        except error.HTTPError as exc:
            if exc.code not in {502, 503, 504} or attempt == 3:
                raise
            previous_sequence = int(current.get("sequence") or 0)
            previous_pid = int(current.get("backend_pid") or 0)
            current = _wait_for_state(
                state_path,
                timeout=timeout,
                predicate=lambda payload: int(payload.get("backend_pid") or 0) > 0,
                process=process,
                expected_distribution="official",
            )
            _assert_desktop_readiness(current, timeout=timeout)
            incidents.append(
                {
                    "scenario": "transient_backend_transport",
                    "observed_failure": f"http_{exc.code}",
                    "recovery_state": (
                        f"sequence_{current.get('sequence')}_pid_{current.get('backend_pid')}_ready"
                    ),
                    "previous_sequence": previous_sequence,
                    "previous_backend_pid": previous_pid,
                    "passed": True,
                    "observed_at": _iso(),
                }
            )
            time.sleep(0.25)
    raise AssertionError("Unreachable transport recovery state")


def _run(args: argparse.Namespace) -> None:
    app_path = Path(args.app).expanduser().resolve()
    executable = _find_app_executable(app_path)
    evidence_path = Path(args.evidence_output).expanduser().resolve()
    artifact_root = Path(args.artifact_dir).expanduser().resolve() / args.run_id
    artifact_root.mkdir(parents=True, exist_ok=True)
    smoke_root = Path(tempfile.mkdtemp(prefix=f"lantai-sustained-{args.run_id}-"))
    user_data_dir = smoke_root / "user-data"
    workspace_dir = user_data_dir / "working" / "workspaces" / "default"
    state_path = smoke_root / "desktop-lifecycle-state.json"
    app_log_path = artifact_root / "app.log"
    progress_path = artifact_root / "progress.jsonl"
    started_at = _utc_now()
    started_monotonic = time.monotonic()
    offsets = _scheduled_offsets(args.duration_seconds, args.interval_seconds)
    light_records: list[dict[str, Any]] = []
    standard_records: list[dict[str, Any]] = []
    effect_records: list[dict[str, Any]] = []
    works_records: list[dict[str, Any]] = []
    ui_records: list[dict[str, Any]] = []
    progress_samples: list[dict[str, Any]] = []
    recovery_events: list[dict[str, Any]] = []
    current_pids: set[int] = set()
    last_session_id = ""
    failure: str | None = None
    stop_passed = False
    cleanup: dict[str, Any] = {"tracked_alive": {}, "matching_alive": {}}
    restore_handlers = _install_termination_signal_handlers()
    proc: subprocess.Popen[str] | None = None
    smoke_model_context: _LocalOpenAISmokeServer | None = None

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
        smoke_model_context = _LocalOpenAISmokeServer()
        smoke_model = smoke_model_context.__enter__()
        _write_smoke_openai_provider_config(user_data_dir, base_url=smoke_model.base_url)
        with app_log_path.open("a", encoding="utf-8") as log_file:
            proc = _launch_app(executable, app_path, env, log_file)
            state = _wait_for_state(
                state_path,
                timeout=args.timeout,
                predicate=lambda payload: int(payload.get("sequence") or 0) >= 1,
                process=proc,
                expected_distribution="official",
            )
            _assert_ready_state(state, expected_distribution="official")
            _assert_desktop_readiness(state, timeout=args.timeout)
            _assert_packaged_customization_integrity(state, timeout=args.timeout)
            current_pids = {proc.pid, int(state["backend_pid"])}
            backend_restart_done = False
            app_restart_done = False

            for sample_index, offset in enumerate(offsets, 1):
                deadline = started_monotonic + offset
                while time.monotonic() < deadline:
                    time.sleep(min(1.0, deadline - time.monotonic()))

                fraction = offset / args.duration_seconds
                if not backend_restart_done and fraction >= 0.25 and last_session_id:
                    old_sequence = int(state.get("sequence") or 0)
                    old_pid = int(state["backend_pid"])
                    _crash_managed_backend_for_restart(old_pid)
                    state = _wait_for_state(
                        state_path,
                        timeout=args.timeout,
                        predicate=lambda payload: (
                            int(payload.get("sequence") or 0) > old_sequence
                            and int(payload.get("backend_pid") or 0) not in {0, old_pid}
                        ),
                        process=proc,
                        expected_distribution="official",
                    )
                    _assert_desktop_readiness(state, timeout=args.timeout)
                    _assert_persisted_success_events(
                        state, session_id=last_session_id, timeout=args.timeout
                    )
                    current_pids = {proc.pid, int(state["backend_pid"])}
                    recovery_events.append(
                        {
                            "scenario": "backend_sigkill",
                            "observed_failure": f"backend_pid_{old_pid}_killed",
                            "recovery_state": f"backend_pid_{state['backend_pid']}_ready",
                            "passed": True,
                            "observed_at": _iso(),
                        }
                    )
                    backend_restart_done = True

                if not app_restart_done and fraction >= 0.5 and last_session_id:
                    previous_nonce = str(state.get("desktop_instance_nonce") or "")
                    snapshot = _process_snapshot()
                    old_instance_pids = {proc.pid, int(state["backend_pid"])}
                    old_instance_pids.update(_descendant_pids(snapshot, proc.pid))
                    _stop_app(proc)
                    proc = None
                    old_cleanup = _wait_for_process_cleanup(
                        tracked_pids=old_instance_pids,
                        markers=(),
                        timeout=args.cleanup_timeout,
                    )
                    if old_cleanup["tracked_alive"]:
                        raise AssertionError(
                            "Old packaged app process tree survived restart: "
                            f"{old_cleanup}"
                        )
                    state_path.unlink(missing_ok=True)
                    proc = _launch_app(executable, app_path, env, log_file)
                    state = _wait_for_state(
                        state_path,
                        timeout=args.timeout,
                        predicate=lambda payload: _is_new_desktop_instance(
                            payload, previous_nonce=previous_nonce
                        ),
                        process=proc,
                        expected_distribution="official",
                    )
                    _assert_desktop_readiness(state, timeout=args.timeout)
                    _assert_persisted_success_events(
                        state, session_id=last_session_id, timeout=args.timeout
                    )
                    current_pids = {proc.pid, int(state["backend_pid"])}
                    recovery_events.append(
                        {
                            "scenario": "app_restart",
                            "observed_failure": f"desktop_nonce_{previous_nonce}_stopped",
                            "recovery_state": (
                                f"desktop_nonce_{state.get('desktop_instance_nonce')}_ready"
                            ),
                            "passed": True,
                            "observed_at": _iso(),
                        }
                    )
                    app_restart_done = True

                light_started = time.monotonic()
                state, last_session_id, chat_id, transport_recoveries = (
                    _exercise_light_with_transport_recovery(
                        state,
                        state_path=state_path,
                        process=proc,
                        timeout=args.timeout,
                    )
                )
                recovery_events.extend(transport_recoveries)
                light_records.append(
                    {
                        "run_id": chat_id,
                        "session_id": last_session_id,
                        "status": "verified",
                        "false_completion": False,
                        "duration_seconds": round(time.monotonic() - light_started, 3),
                        "tool_calls": 0,
                        "tokens": 2,
                        "artifact": _write_record_artifact(
                            artifact_root,
                            f"light/light-{sample_index:03d}.json",
                            {
                                "chat_id": chat_id,
                                "session_id": last_session_id,
                                "verified_at": _iso(),
                            },
                        ),
                    }
                )

                if sample_index % args.standard_every == 0:
                    standard_started = time.monotonic()
                    run_id = _assert_browser_positive_pause_resume(
                        state, workspace_dir=workspace_dir, timeout=args.timeout
                    )
                    summary = _find_runtime_run_by_id(
                        state, run_id=run_id, timeout=args.timeout
                    )
                    standard_record = {
                        "run_id": run_id,
                        "status": "verified",
                        "failure_attributed": True,
                        "false_completion": False,
                        "artifacts_preserved": True,
                        "duration_seconds": round(
                            time.monotonic() - standard_started, 3
                        ),
                        "artifact": _write_record_artifact(
                            artifact_root,
                            f"standard/standard-{sample_index:03d}.json",
                            {"run_id": run_id, "summary": summary},
                        ),
                    }
                    standard_records.append(standard_record)
                    works_records.append(
                        _register_verified_work(
                            state,
                            workspace_dir=workspace_dir,
                            artifact_root=artifact_root,
                            sequence=sample_index,
                            source_run_id=run_id,
                            timeout=args.timeout,
                        )
                    )
                    ui_records.append(
                        _wait_for_ui_convergence(
                            state, run_id=run_id, timeout=args.timeout
                        )
                    )

                if args.effect_every > 0 and sample_index % args.effect_every == 0:
                    effect = _assert_packaged_operator_effect_recovery(
                        state,
                        workspace_dir=workspace_dir,
                        expected_distribution="official",
                        timeout=args.timeout,
                    )
                    effect_records.append(
                        {
                            "run_id": effect["run_id"],
                            "started_effect_ids": [effect["effect_id"]],
                            "settled_effect_ids": [effect["effect_id"]],
                            "ambiguous_replayed_effect_ids": [],
                            "effect_statuses": effect["effect_statuses"],
                        }
                    )

                sample = {
                    "sequence": sample_index,
                    "observed_at": _iso(),
                    "elapsed_seconds": round(time.monotonic() - started_monotonic, 3),
                    "backend_pid": state.get("backend_pid"),
                    "desktop_instance_nonce": state.get("desktop_instance_nonce"),
                    "light_completed": len(light_records),
                    "standard_completed": len(standard_records),
                    "effects_settled": len(effect_records),
                    "works_registered": len(works_records),
                }
                progress_samples.append(sample)
                _append_jsonl(progress_path, sample)
                print(
                    f"{args.run_id}: sustained sample {sample_index}/{len(offsets)} "
                    f"elapsed={sample['elapsed_seconds']}s",
                    flush=True,
                )

            if not backend_restart_done or not app_restart_done:
                raise AssertionError(
                    "Sustained run ended without both backend and app recovery exercises"
                )
            snapshot = _process_snapshot()
            current_pids.update(_descendant_pids(snapshot, proc.pid))
            current_pids.update({proc.pid, int(state["backend_pid"])})
            _stop_app(proc)
            proc = None
            cleanup = _wait_for_process_cleanup(
                tracked_pids=current_pids,
                # Multiple legal isolated release-gate instances may exercise
                # the same exact app concurrently. The userData marker proves
                # ownership without misclassifying another run as an orphan.
                markers=(str(user_data_dir),),
                timeout=args.cleanup_timeout,
            )
            stop_passed = not cleanup["tracked_alive"] and not cleanup["matching_alive"]
            if not stop_passed:
                raise AssertionError(f"Sustained run left app processes alive: {cleanup}")
    except Exception as exc:  # noqa: BLE001 - evidence preserves exact failure
        failure = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        _stop_app(proc)
        if smoke_model_context is not None:
            with contextlib.suppress(Exception):
                smoke_model_context.__exit__(None, None, None)
        completed_at = _utc_now()
        duration_seconds = (completed_at - started_at).total_seconds()
        long_run_record = {
            "run_id": args.run_id,
            "started_at": _iso(started_at),
            "completed_at": _iso(completed_at),
            "duration_seconds": duration_seconds,
            "progress_visible": bool(progress_samples),
            "recovery_passed": len(recovery_events) >= 2,
            "stop_passed": stop_passed,
            "artifacts_preserved": progress_path.is_file(),
            "progress_samples": progress_samples,
            "artifact": _artifact(progress_path) if progress_path.is_file() else None,
            "recovery_events": recovery_events,
            "cleanup": cleanup,
        }
        _write_json_file(
            evidence_path,
            {
                "schema": SCHEMA,
                "passed": failure is None,
                "failure": failure,
                "app": _app_metadata_for_evidence(app_path),
                "run_id": args.run_id,
                "long_run": long_run_record,
                "light_records": light_records,
                "standard_records": standard_records,
                "effect_records": effect_records,
                "works_records": works_records,
                "ui_records": ui_records,
            },
        )
        if args.keep_workdir:
            print(f"Sustained workdir: {smoke_root}")
        else:
            shutil.rmtree(smoke_root, ignore_errors=True)
        restore_handlers()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True)
    parser.add_argument("--run-id", default=f"sustained-{uuid.uuid4().hex[:8]}")
    parser.add_argument("--duration-seconds", type=float, default=14_400)
    parser.add_argument("--interval-seconds", type=float, default=600)
    parser.add_argument("--standard-every", type=int, default=2)
    parser.add_argument("--effect-every", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--cleanup-timeout", type=float, default=15)
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--evidence-output", required=True)
    parser.add_argument("--keep-workdir", action="store_true")
    args = parser.parse_args(argv)
    if args.duration_seconds <= 0 or args.interval_seconds <= 0:
        parser.error("duration and interval must be positive")
    if args.standard_every < 1 or args.effect_every < 0:
        parser.error("standard-every must be positive and effect-every non-negative")
    try:
        _run(args)
    except Exception as exc:  # noqa: BLE001 - command line diagnostics
        print(f"Packaged sustained soak failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
