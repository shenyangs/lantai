#!/usr/bin/env python3
"""Verify previous -> current -> previous -> current data-safe app transitions."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from packaged_app_lifecycle_smoke import (
    _LocalOpenAISmokeServer,
    _app_metadata_for_evidence,
    _assert_desktop_readiness,
    _assert_persisted_success_events,
    _assert_ready_state,
    _exercise_console_model_success_path,
    _find_app_executable,
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


SCHEMA = "com.lantai.packaged-app-upgrade-rollback-smoke.v1"


def _bundle_version(app: Path) -> str:
    with (app / "Contents/Info.plist").open("rb") as handle:
        return str(plistlib.load(handle).get("CFBundleShortVersionString") or "")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _get(state: dict[str, Any], endpoint: str, *, timeout: float) -> Any:
    _status, _body, payload = _json_request(
        "GET",
        f"{_state_base_url(state)}{endpoint}",
        headers=_state_headers(state),
        timeout=timeout,
    )
    return payload


def _post(
    state: dict[str, Any], endpoint: str, payload: dict[str, Any], *, timeout: float
) -> Any:
    _status, _body, result = _json_request(
        "POST",
        f"{_state_base_url(state)}{endpoint}",
        payload=payload,
        headers=_state_headers(state),
        timeout=timeout,
    )
    return result


def _put(
    state: dict[str, Any], endpoint: str, payload: dict[str, Any], *, timeout: float
) -> Any:
    _status, _body, result = _json_request(
        "PUT",
        f"{_state_base_url(state)}{endpoint}",
        payload=payload,
        headers=_state_headers(state),
        timeout=timeout,
    )
    return result


def _wait_task(
    state: dict[str, Any], task_id: str, *, timeout: float
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        payload = _get(state, f"/api/coding/tasks/{task_id}", timeout=timeout)
        latest = payload if isinstance(payload, dict) else {}
        if latest.get("status") in {"completed", "failed", "cancelled", "stalled"}:
            return latest
        time.sleep(0.1)
    raise AssertionError(f"Transition task did not settle: {latest!r}")


def _seed(state: dict[str, Any], *, customization_dir: Path, timeout: float) -> dict[str, Any]:
    profile_id = "profile_v1_upgrade_fixture"
    created = json.loads(
        (customization_dir / "profiles" / f"{profile_id}.json").read_text(
            encoding="utf-8"
        )
    )
    if not isinstance(created, dict) or created.get("revision") != 3:
        raise AssertionError(f"Previous app did not read the V1 Profile: {created!r}")
    session_id, chat_id = _exercise_console_model_success_path(
        state, timeout=timeout
    )
    task = _post(
        state,
        "/api/coding/tasks",
        {
            "command": "python3 -c 'print(\"upgrade-rollback-task-sentinel\")'",
            "confirmed": True,
            "timeout_seconds": 30,
        },
        timeout=timeout,
    )
    settled = _wait_task(state, str(task.get("task_id") or ""), timeout=timeout)
    if settled.get("status") != "completed":
        raise AssertionError(f"Transition seed task failed: {settled!r}")
    return {
        "profile_id": profile_id,
        "profile_revision": created.get("revision"),
        "session_id": session_id,
        "chat_id": chat_id,
        "task_id": settled["task_id"],
        "task_output_seq": settled.get("output_seq"),
    }


def _snapshot(
    state: dict[str, Any],
    *,
    seed: dict[str, Any],
    customization_dir: Path,
    verify_customization_api: bool,
    timeout: float,
) -> dict[str, Any]:
    profile = json.loads(
        (
            customization_dir
            / "profiles"
            / f"{seed['profile_id']}.json"
        ).read_text(encoding="utf-8")
    )
    customization_state = json.loads(
        (customization_dir / "state.json").read_text(encoding="utf-8")
    )
    slots = json.loads(
        (customization_dir / "slots.json").read_text(encoding="utf-8")
    )
    restored_chat_id = _assert_persisted_success_events(
        state, session_id=str(seed["session_id"]), timeout=timeout
    )
    events = _get(
        state,
        f"/api/chats/{seed['chat_id']}/events?after_seq=0",
        timeout=timeout,
    )
    task = _get(
        state, f"/api/coding/tasks/{seed['task_id']}", timeout=timeout
    )
    values = profile.get("overrides") if isinstance(profile, dict) else {}
    slot = next(
        (item for item in slots or () if item.get("index") == 1),
        None,
    )
    snapshot = {
        "profile_id": profile.get("id"),
        "profile_revision": profile.get("revision"),
        "profile_overrides": profile.get("overrides"),
        "active_profile_id": customization_state.get("active_profile_id"),
        "customization_mode": customization_state.get("mode"),
        "slot_1_profile_id": (slot or {}).get("profile_id"),
        "effective_values": {
            "appearance.palette": values.get("appearance.palette"),
            "persona.presence": values.get("persona.presence"),
        },
        "chat_id": restored_chat_id,
        "chat_event_count": len(events.get("events") or ()),
        "task_id": task.get("task_id"),
        "task_status": task.get("status"),
        "task_output_seq": task.get("output_seq"),
        "task_stdout": task.get("stdout_tail"),
    }
    if snapshot["profile_id"] != seed["profile_id"]:
        raise AssertionError(f"Profile identity changed: {snapshot!r}")
    if snapshot["active_profile_id"] != seed["profile_id"]:
        raise AssertionError(f"Active profile changed: {snapshot!r}")
    if snapshot["slot_1_profile_id"] != seed["profile_id"]:
        raise AssertionError(f"Profile slot changed: {snapshot!r}")
    if snapshot["effective_values"] != {
        "appearance.palette": "warm",
        "persona.presence": "quiet",
    }:
        raise AssertionError(f"Effective personalization changed: {snapshot!r}")
    if verify_customization_api:
        effective = _get(
            state, "/api/customization/effective-state", timeout=timeout
        )
        api_values = effective.get("values") if isinstance(effective, dict) else {}
        observed = {
            key: api_values.get(key) for key in snapshot["effective_values"]
        }
        if observed != snapshot["effective_values"]:
            raise AssertionError(
                "Current app API did not resolve the preserved personalization: "
                f"disk={snapshot['effective_values']!r} api={observed!r}"
            )
    if snapshot["task_status"] != "completed" or "upgrade-rollback-task-sentinel" not in str(
        snapshot["task_stdout"] or ""
    ):
        raise AssertionError(f"Completed task changed: {snapshot!r}")
    return snapshot


def _launch(
    app_path: Path,
    env: dict[str, str],
    log: Any,
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [str(_find_app_executable(app_path))],
        cwd=str(app_path.parent),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _run(args: argparse.Namespace) -> None:
    previous_app = Path(args.previous_app).expanduser().resolve()
    current_app = Path(args.current_app).expanduser().resolve()
    evidence_path = Path(args.evidence_output).expanduser().resolve()
    smoke_root = Path(tempfile.mkdtemp(prefix="lantai-upgrade-rollback-"))
    user_data = smoke_root / "user-data"
    fixture = Path(args.customization_fixture).expanduser().resolve()
    customization_dir = user_data / "working/customization"
    state_path = smoke_root / "state.json"
    log_path = smoke_root / "app.log"
    restore_handlers = _install_termination_signal_handlers()
    provider_context: _LocalOpenAISmokeServer | None = None
    proc: subprocess.Popen[str] | None = None
    failure: str | None = None
    phases: list[dict[str, Any]] = []
    seed: dict[str, Any] = {}
    env = os.environ.copy()
    env.update(
        {
            "LANTAI_DESKTOP_LIFECYCLE_SMOKE_FILE": str(state_path),
            "LANTAI_DESKTOP_USER_DATA_DIR": str(user_data),
            "LANTAI_DESKTOP_HEADLESS_SMOKE": "1",
            "LANTAI_AUTH_ENABLED": "0",
            "LANTAI_DISABLE_GITHUB_UPDATER": "1",
            "LANTAI_DISABLE_KEYRING": "1",
            "LANTAI_SKIP_KEYRING": "1",
            "LANTAI_REMINDER_DELIVERY_WORKER": "0",
        }
    )
    shutil.copytree(fixture, customization_dir)
    sequence = [
        ("previous_seed", previous_app),
        ("upgrade_current", current_app),
        ("rollback_previous", previous_app),
        ("reupgrade_current", current_app),
    ]
    baseline: dict[str, Any] | None = None
    try:
        provider_context = _LocalOpenAISmokeServer()
        provider = provider_context.__enter__()
        _write_smoke_openai_provider_config(user_data, base_url=provider.base_url)
        previous_nonce = ""
        with log_path.open("a", encoding="utf-8") as log:
            for phase_name, app_path in sequence:
                state_path.unlink(missing_ok=True)
                proc = _launch(app_path, env, log)
                state = _wait_for_state(
                    state_path,
                    timeout=args.timeout,
                    predicate=(
                        (lambda item: _is_new_desktop_instance(item, previous_nonce=previous_nonce))
                        if previous_nonce
                        else (lambda item: int(item.get("sequence") or 0) >= 1)
                    ),
                    process=proc,
                    expected_distribution="official",
                )
                _assert_ready_state(state, expected_distribution="official")
                _assert_desktop_readiness(state, timeout=args.timeout)
                if phase_name == "previous_seed":
                    seed = _seed(
                        state,
                        customization_dir=customization_dir,
                        timeout=args.timeout,
                    )
                snapshot = _snapshot(
                    state,
                    seed=seed,
                    customization_dir=customization_dir,
                    verify_customization_api=app_path == current_app,
                    timeout=args.timeout,
                )
                if baseline is None:
                    baseline = snapshot
                elif snapshot != baseline:
                    raise AssertionError(
                        f"Transition {phase_name} changed durable state: "
                        f"baseline={baseline!r} snapshot={snapshot!r}"
                    )
                phases.append(
                    {
                        "phase": phase_name,
                        "version": _bundle_version(app_path),
                        "app_path": str(app_path),
                        "executable_sha256": _sha256(_find_app_executable(app_path)),
                        "desktop_instance_nonce": state.get("desktop_instance_nonce"),
                        "snapshot": snapshot,
                    }
                )
                previous_nonce = str(state.get("desktop_instance_nonce") or "")
                tracked = {proc.pid, int(state["backend_pid"])}
                tracked.update(_descendant_pids(_process_snapshot(), proc.pid))
                _stop_app(proc)
                proc = None
                cleanup = _wait_for_process_cleanup(
                    tracked_pids=tracked,
                    markers=(str(user_data),),
                    timeout=args.cleanup_timeout,
                )
                if cleanup["tracked_alive"] or cleanup["matching_alive"]:
                    raise AssertionError(
                        f"Transition {phase_name} left processes alive: {cleanup}"
                    )
    except Exception as exc:  # noqa: BLE001 - preserve exact evidence failure
        failure = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        _stop_app(proc)
        if provider_context is not None:
            with contextlib.suppress(Exception):
                provider_context.__exit__(None, None, None)
        _write_json_file(
            evidence_path,
            {
                "schema": SCHEMA,
                "passed": failure is None,
                "failure": failure,
                "previous_app": _app_metadata_for_evidence(previous_app),
                "current_app": _app_metadata_for_evidence(current_app),
                "seed": seed,
                "phases": phases,
            },
        )
        if args.keep_workdir:
            print(f"Upgrade rollback workdir: {smoke_root}")
        else:
            shutil.rmtree(smoke_root, ignore_errors=True)
        restore_handlers()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-app", required=True)
    parser.add_argument("--current-app", required=True)
    parser.add_argument(
        "--customization-fixture",
        default=str(
            Path(__file__).resolve().parents[1]
            / "tests/fixtures/customization_v1"
        ),
    )
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--cleanup-timeout", type=float, default=20)
    parser.add_argument("--evidence-output", required=True)
    parser.add_argument("--keep-workdir", action="store_true")
    args = parser.parse_args(argv)
    try:
        _run(args)
    except Exception as exc:  # noqa: BLE001 - command diagnostics
        print(f"Packaged upgrade/rollback smoke failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
