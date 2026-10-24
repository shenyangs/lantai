#!/usr/bin/env python3
"""Exercise auditable packaged-app recovery across the six release fault classes."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from packaged_app_lifecycle_smoke import (
    SMOKE_MODEL_REPLY_PREFIX,
    _LocalOpenAISmokeServer,
    _app_metadata_for_evidence,
    _assert_desktop_readiness,
    _assert_failure_events,
    _assert_packaged_browser_content_trial_handoff,
    _assert_packaged_customization_integrity,
    _assert_persisted_success_events,
    _assert_ready_state,
    _console_electron_binary,
    _exercise_console_model_success_path,
    _extract_json_object,
    _extract_sse_payloads,
    _find_app_executable,
    _install_termination_signal_handlers,
    _is_new_desktop_instance,
    _json_request,
    _run_chat_runtime_stream_renderer_smoke,
    _seed_chat_provider_stream_ui,
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


SCHEMA = "com.lantai.packaged-agent-fault-matrix.v1"
SCENARIOS = (
    "network_disconnect",
    "chat_switch",
    "renderer_reload",
    "app_restart",
    "tool_process_death",
    "approval_interruption",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record(
    root: Path,
    *,
    scenario: str,
    cycle: int,
    observed_failure: str,
    recovery_state: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    path = root / scenario / f"cycle-{cycle:02d}.json"
    payload = {
        "scenario": scenario,
        "cycle": cycle,
        "observed_failure": observed_failure,
        "recovery_state": recovery_state,
        "details": details,
    }
    _write_json_file(path, payload)
    return {
        "run_id": f"fault-{cycle:02d}-{scenario}",
        "scenario": scenario,
        "passed": True,
        "observed_failure": observed_failure,
        "recovery_state": recovery_state,
        "artifact": {"path": str(path.resolve()), "sha256": _sha256(path)},
    }


def _chat_body(suffix: str) -> tuple[str, dict[str, Any]]:
    session_id = f"console:packaged-network-fault-{suffix}"
    client_turn_id = f"packaged-network-fault-{suffix}"
    return session_id, {
        "input": [
            {
                "role": "user",
                "content": [{"type": "text", "text": f"network fault {suffix}"}],
            }
        ],
        "session_id": session_id,
        "user_id": "default",
        "channel": "console",
        "stream": True,
        "client_turn_id": client_turn_id,
        "meta": {"client_turn_id": client_turn_id},
    }


def _network_fault(
    state: dict[str, Any], provider: _LocalOpenAISmokeServer, *, timeout: float
) -> dict[str, Any]:
    suffix = uuid.uuid4().hex[:8]
    session_id, body = _chat_body(suffix)
    # The model stack deliberately retries transient provider failures many
    # times. Keep the provider unavailable for the entire turn; clear the
    # counter only after the runtime has emitted its terminal response.
    provider.fail_next_chat_requests(10_000)
    try:
        _status, sse_text, _parsed = _json_request(
            "POST",
            f"{_state_base_url(state)}/api/console/chat",
            payload=body,
            headers=_state_headers(state),
            timeout=timeout,
        )
    finally:
        provider.clear_chat_failures()
    payloads = _extract_sse_payloads(sse_text)
    final = payloads[-1] if payloads else {}
    if final.get("status") != "failed":
        raise AssertionError(f"Network fault did not settle as failed: {payloads!r}")
    _status, _body, chats = _json_request(
        "GET",
        f"{_state_base_url(state)}/api/chats?mode=all",
        headers=_state_headers(state),
        timeout=timeout,
    )
    chat = next(
        (item for item in chats or () if item.get("session_id") == session_id),
        None,
    )
    if not chat:
        raise AssertionError("Network failure chat was not persisted")
    _status, _body, events = _json_request(
        "GET",
        f"{_state_base_url(state)}/api/chats/{chat['id']}/events?after_seq=0",
        headers=_state_headers(state),
        timeout=timeout,
    )
    _assert_failure_events(events)
    recovered_session, recovered_chat = _exercise_console_model_success_path(
        state, timeout=timeout
    )
    return {
        "failed_chat_id": chat["id"],
        "failure_status": final.get("status"),
        "failure_error": final.get("error"),
        "recovered_session_id": recovered_session,
        "recovered_chat_id": recovered_chat,
    }


def _chat_switch_fault(
    state: dict[str, Any], *, base_chat_id: str, workspace_dir: Path, timeout: float
) -> dict[str, Any]:
    seeded = _seed_chat_provider_stream_ui(
        state, workspace_dir=workspace_dir, timeout=timeout
    )
    result = _run_chat_runtime_stream_renderer_smoke(
        state,
        chat_id=base_chat_id,
        timeout=timeout,
        provider_stream_ui=seeded,
        expected_terminal_type="turn.completed",
        expected_terminal_statuses={"completed", "finished"},
    )
    route = result.get("provider_stream_route_switch_ui") or result.get("route_switch")
    if not isinstance(route, dict) or route.get("ok") is not True:
        raise AssertionError(f"Chat route switch did not prove isolation: {result!r}")
    return route


def _renderer_reload_fault(
    state: dict[str, Any], *, chat_id: str, timeout: float
) -> dict[str, Any]:
    script = Path(__file__).resolve().parents[1] / "console/electron/rendererReloadFaultSmoke.cjs"
    command = [
        str(_console_electron_binary()),
        str(script),
        "--base-url",
        _state_base_url(state),
        "--desktop-token",
        _state_headers(state)["X-Lantai-Desktop-Token"],
        "--chat-id",
        chat_id,
        "--sentinel",
        SMOKE_MODEL_REPLY_PREFIX,
        "--timeout-ms",
        str(max(1000, int(timeout * 1000))),
    ]
    result = subprocess.run(
        command,
        cwd=str(script.parents[1]),
        text=True,
        capture_output=True,
        timeout=timeout + 20,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"Renderer reload smoke failed: {result.stdout}\n{result.stderr}"
        )
    payload = _extract_json_object(result.stdout)
    if payload.get("ok") is not True or int(payload.get("load_count") or 0) < 2:
        raise AssertionError(f"Renderer reload proof is incomplete: {payload!r}")
    return payload


def _wait_coding_task(
    state: dict[str, Any], task_id: str, *, timeout: float, statuses: set[str]
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        _status, _body, latest = _json_request(
            "GET",
            f"{_state_base_url(state)}/api/coding/tasks/{task_id}",
            headers=_state_headers(state),
            timeout=timeout,
        )
        if str(latest.get("status") or "") in statuses:
            return latest
        time.sleep(0.1)
    raise AssertionError(f"Coding task {task_id} did not reach {statuses}: {latest!r}")


def _tool_process_death_fault(state: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    command = "python3 -c 'import os,time; print(os.getpid(), flush=True); time.sleep(60)'"
    _status, _body, task = _json_request(
        "POST",
        f"{_state_base_url(state)}/api/coding/tasks",
        payload={"command": command, "confirmed": True, "timeout_seconds": 90},
        headers=_state_headers(state),
        timeout=timeout,
    )
    task_id = str(task.get("task_id") or "")
    running = _wait_coding_task(state, task_id, timeout=timeout, statuses={"running"})
    deadline = time.monotonic() + timeout
    tool_pid = 0
    while time.monotonic() < deadline:
        current = _wait_coding_task(
            state, task_id, timeout=timeout, statuses={"running", "failed"}
        )
        tail = str(current.get("stdout_tail") or "").strip().splitlines()
        if tail and tail[-1].strip().isdigit():
            tool_pid = int(tail[-1].strip())
            break
        time.sleep(0.1)
    if tool_pid <= 0:
        raise AssertionError(f"Coding child did not expose a process id: {running!r}")
    os.kill(tool_pid, signal.SIGKILL)
    failed = _wait_coding_task(state, task_id, timeout=timeout, statuses={"failed"})
    if int(failed.get("returncode") or 0) == 0:
        raise AssertionError(f"Killed tool process did not retain return code: {failed!r}")
    _status, _body, recovery = _json_request(
        "POST",
        f"{_state_base_url(state)}/api/coding/tasks",
        payload={
            "command": "python3 -c 'print(\"tool-recovered\")'",
            "confirmed": True,
            "timeout_seconds": 30,
        },
        headers=_state_headers(state),
        timeout=timeout,
    )
    recovered = _wait_coding_task(
        state,
        str(recovery.get("task_id") or ""),
        timeout=timeout,
        statuses={"completed"},
    )
    return {
        "killed_task_id": task_id,
        "killed_pid": tool_pid,
        "killed_returncode": failed.get("returncode"),
        "recovery_task_id": recovered.get("task_id"),
        "recovery_status": recovered.get("status"),
    }


def _approval_interruption_fault(
    state: dict[str, Any], *, workspace_dir: Path, timeout: float
) -> dict[str, Any]:
    from packaged_app_lifecycle_smoke import (
        _assert_loop_succeeded_projection,
        _assert_loop_waiting_approval_projection,
        _run_packaged_browser_content_trial_once,
    )

    run_id = f"packaged-approval-fault-{uuid.uuid4().hex[:8]}"
    pending = _run_packaged_browser_content_trial_once(
        workspace_dir,
        run_id=run_id,
        human_gate_decision="pending",
        release_edition="official",
    )
    waiting = _assert_loop_waiting_approval_projection(
        state, run_id=run_id, timeout=timeout
    )
    if pending.get("status") != "waiting_approval":
        raise AssertionError(f"Approval fault did not wait: {pending!r}")
    approved = _run_packaged_browser_content_trial_once(
        workspace_dir,
        run_id=run_id,
        human_gate_decision="approved",
        release_edition="official",
    )
    succeeded = _assert_loop_succeeded_projection(
        state, run_id=run_id, timeout=timeout
    )
    if approved.get("attempt_id") != pending.get("attempt_id"):
        raise AssertionError(
            f"Approval recovery changed attempt: {pending!r} {approved!r}"
        )
    return {
        "run_id": run_id,
        "pending_status": waiting.get("authority_status"),
        "final_status": succeeded.get("authority_status"),
        "attempt_id": approved.get("attempt_id"),
    }


def _launch(executable: Path, app_path: Path, env: dict[str, str], log: Any):
    return subprocess.Popen(
        [str(executable)],
        cwd=str(app_path.parent),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _run(args: argparse.Namespace) -> None:
    app_path = Path(args.app).expanduser().resolve()
    executable = _find_app_executable(app_path)
    artifact_root = Path(args.artifact_dir).expanduser().resolve()
    evidence_path = Path(args.evidence_output).expanduser().resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)
    smoke_root = Path(tempfile.mkdtemp(prefix="lantai-packaged-fault-matrix-"))
    user_data = smoke_root / "user-data"
    workspace_dir = user_data / "working/workspaces/default"
    state_path = smoke_root / "state.json"
    log_path = artifact_root / "app.log"
    records: list[dict[str, Any]] = []
    failure: str | None = None
    cleanup: dict[str, Any] = {"tracked_alive": {}, "matching_alive": {}}
    restore_handlers = _install_termination_signal_handlers()
    proc: subprocess.Popen[str] | None = None
    provider_context: _LocalOpenAISmokeServer | None = None
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
    try:
        provider_context = _LocalOpenAISmokeServer()
        provider = provider_context.__enter__()
        _write_smoke_openai_provider_config(user_data, base_url=provider.base_url)
        with log_path.open("a", encoding="utf-8") as log:
            proc = _launch(executable, app_path, env, log)
            state = _wait_for_state(
                state_path,
                timeout=args.timeout,
                predicate=lambda item: int(item.get("sequence") or 0) >= 1,
                process=proc,
                expected_distribution="official",
            )
            _assert_ready_state(state, expected_distribution="official")
            _assert_desktop_readiness(state, timeout=args.timeout)
            _assert_packaged_customization_integrity(state, timeout=args.timeout)
            for cycle in range(1, args.cycles + 1):
                network = _network_fault(state, provider, timeout=args.timeout)
                records.append(
                    _record(
                        artifact_root,
                        scenario="network_disconnect",
                        cycle=cycle,
                        observed_failure="model_provider_http_503_network_disconnect",
                        recovery_state="next_model_turn_verified_completed",
                        details=network,
                    )
                )

                switched = _chat_switch_fault(
                    state,
                    base_chat_id=str(network["recovered_chat_id"]),
                    workspace_dir=workspace_dir,
                    timeout=args.timeout,
                )
                records.append(
                    _record(
                        artifact_root,
                        scenario="chat_switch",
                        cycle=cycle,
                        observed_failure="active_stream_route_changed_to_isolated_chat",
                        recovery_state="original_stream_restored_without_cross_chat_leak",
                        details=switched,
                    )
                )

                session_id, chat_id = _exercise_console_model_success_path(
                    state, timeout=args.timeout
                )
                renderer = _renderer_reload_fault(
                    state, chat_id=chat_id, timeout=args.timeout
                )
                records.append(
                    _record(
                        artifact_root,
                        scenario="renderer_reload",
                        cycle=cycle,
                        observed_failure=str(renderer["observed_failure"]),
                        recovery_state=str(renderer["recovery_state"]),
                        details=renderer,
                    )
                )

                previous_nonce = str(state.get("desktop_instance_nonce") or "")
                old_pids = {proc.pid, int(state["backend_pid"])}
                old_pids.update(_descendant_pids(_process_snapshot(), proc.pid))
                _stop_app(proc)
                proc = None
                old_cleanup = _wait_for_process_cleanup(
                    tracked_pids=old_pids, markers=(), timeout=args.cleanup_timeout
                )
                if old_cleanup["tracked_alive"]:
                    raise AssertionError(f"App restart left old processes: {old_cleanup}")
                state_path.unlink(missing_ok=True)
                proc = _launch(executable, app_path, env, log)
                state = _wait_for_state(
                    state_path,
                    timeout=args.timeout,
                    predicate=lambda item: _is_new_desktop_instance(
                        item, previous_nonce=previous_nonce
                    ),
                    process=proc,
                    expected_distribution="official",
                )
                _assert_desktop_readiness(state, timeout=args.timeout)
                _assert_persisted_success_events(
                    state, session_id=session_id, timeout=args.timeout
                )
                _assert_packaged_customization_integrity(state, timeout=args.timeout)
                records.append(
                    _record(
                        artifact_root,
                        scenario="app_restart",
                        cycle=cycle,
                        observed_failure=f"desktop_nonce_{previous_nonce}_stopped",
                        recovery_state=(
                            f"desktop_nonce_{state.get('desktop_instance_nonce')}_ready_with_chat"
                        ),
                        details={"old_cleanup": old_cleanup, "chat_id": chat_id},
                    )
                )

                tool = _tool_process_death_fault(state, timeout=args.timeout)
                records.append(
                    _record(
                        artifact_root,
                        scenario="tool_process_death",
                        cycle=cycle,
                        observed_failure=f"coding_child_pid_{tool['killed_pid']}_sigkill",
                        recovery_state="next_coding_process_completed",
                        details=tool,
                    )
                )

                approval = _approval_interruption_fault(
                    state,
                    workspace_dir=workspace_dir,
                    timeout=args.timeout,
                )
                records.append(
                    _record(
                        artifact_root,
                        scenario="approval_interruption",
                        cycle=cycle,
                        observed_failure="run_waiting_approval_without_side_effect",
                        recovery_state="same_attempt_approved_and_succeeded",
                        details=approval,
                    )
                )
                print(f"fault matrix cycle {cycle}/{args.cycles} passed", flush=True)

            current_pids = {proc.pid, int(state["backend_pid"])}
            current_pids.update(_descendant_pids(_process_snapshot(), proc.pid))
            _stop_app(proc)
            proc = None
            cleanup = _wait_for_process_cleanup(
                tracked_pids=current_pids,
                # Other isolated release soaks may exercise the same exact app
                # concurrently. The per-run userData marker distinguishes this
                # process tree without treating those legal instances as leaks.
                markers=(str(user_data),),
                timeout=args.cleanup_timeout,
            )
            if cleanup["tracked_alive"] or cleanup["matching_alive"]:
                raise AssertionError(f"Fault matrix left processes alive: {cleanup}")
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
                "app": _app_metadata_for_evidence(app_path),
                "cycles": args.cycles,
                "records": records,
                "cleanup": cleanup,
            },
        )
        if not args.keep_workdir:
            shutil.rmtree(smoke_root, ignore_errors=True)
        restore_handlers()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True)
    parser.add_argument("--cycles", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--cleanup-timeout", type=float, default=20)
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--evidence-output", required=True)
    parser.add_argument("--keep-workdir", action="store_true")
    args = parser.parse_args(argv)
    if args.cycles < 1:
        parser.error("cycles must be positive")
    try:
        _run(args)
    except Exception as exc:  # noqa: BLE001 - command diagnostics
        print(f"Packaged Agent fault matrix failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
