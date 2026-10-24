#!/usr/bin/env python3
"""Prove memory ownership invariants through an actual packaged Lantai app.

The controller uses a temporary desktop user-data directory. It never reads or
writes the user's normal Lantai workspace and disables the updater explicitly.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
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
    _assert_ready_state,
    _find_app_executable,
    _extract_sse_payloads,
    _is_new_desktop_instance,
    _json_request,
    _state_base_url,
    _state_headers,
    _stop_app,
    _utc_now_iso,
    _wait_for_state,
    _write_json_file,
    _write_smoke_openai_provider_config,
)
from lantai.agents.memory.canonical_ledger import CanonicalMemoryLedger


EVIDENCE_SCHEMA = "com.lantai.packaged-memory-ownership-smoke.v1"
PENDING_SENTINEL = "用户倾向简洁回复"
CONFIRMED_SENTINEL = "包态记忆门：默认给示例附上最小验证命令"
SENSITIVE_SENTINEL = "api_key=packaged-memory-smoke-must-not-store"
MODEL_CONTEXT_SENTINEL = "默认给代码示例附上最小验证命令"
MODEL_CONTEXT_QUERY = "请给一个代码示例，并附上最小验证命令。"


def _seed_pending_fixture(workspace_dir: Path) -> dict[str, Any]:
    ledger = CanonicalMemoryLedger(workspace_dir)
    event = ledger.record_event(
        PENDING_SENTINEL,
        session_id="packaged-memory-smoke-seed",
        source_role="user",
    )
    if event is None:
        raise AssertionError("Could not create packaged memory smoke source event")
    pending = ledger.capture_fact(
        PENDING_SENTINEL,
        memory_type="style",
        event=event,
    )
    if pending is None or pending.status != "pending":
        raise AssertionError(f"Could not seed pending memory fact: {pending!r}")
    sensitive = ledger.capture_fact(
        SENSITIVE_SENTINEL,
        memory_type="instruction",
        event=event,
    )
    if sensitive is not None:
        raise AssertionError("Sensitive fixture was accepted into canonical memory")
    return {
        "pending_fact_id": pending.id,
        "sensitive_fixture_rejected": True,
    }


def _get_memory_facts(
    state: dict[str, Any],
    *,
    status: str,
    timeout: float,
) -> list[dict[str, Any]]:
    _status, _body, payload = _json_request(
        "GET",
        f"{_state_base_url(state)}/api/workspace/memory-facts?status={status}&limit=50",
        headers=_state_headers(state),
        timeout=timeout,
    )
    facts = payload.get("facts") if isinstance(payload, dict) else None
    if not isinstance(facts, list) or not all(isinstance(item, dict) for item in facts):
        raise AssertionError(f"Invalid packaged memory fact payload: {payload!r}")
    return facts


def _get_user_memory_entries(
    state: dict[str, Any],
    *,
    timeout: float,
) -> list[dict[str, Any]]:
    _status, _body, payload = _json_request(
        "GET",
        f"{_state_base_url(state)}/api/workspace/user-memory?include_archived=true",
        headers=_state_headers(state),
        timeout=timeout,
    )
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list) or not all(
        isinstance(item, dict) for item in entries
    ):
        raise AssertionError(f"Invalid packaged user-memory payload: {payload!r}")
    return entries


def _assert_forgotten_memory_stays_forgotten(
    state: dict[str, Any],
    *,
    entry_id: str,
    fact_id: str,
    timeout: float,
) -> dict[str, Any]:
    confirmed = _get_memory_facts(state, status="confirmed", timeout=timeout)
    forgotten = _get_memory_facts(state, status="forgotten", timeout=timeout)
    entries = _get_user_memory_entries(state, timeout=timeout)
    if any(item.get("id") == fact_id for item in confirmed):
        raise AssertionError("Forgotten fact returned to confirmed memory")
    if not any(item.get("id") == fact_id for item in forgotten):
        raise AssertionError("Forgotten fact lost its canonical tombstone")
    if any(item.get("id") == entry_id for item in entries):
        raise AssertionError("Deleted structured memory entry reappeared")
    return {
        "confirmed_fact_absent": True,
        "canonical_forget_tombstone_present": True,
        "structured_projection_absent": True,
    }


def _exercise_initial_memory_ownership(
    state: dict[str, Any],
    *,
    pending_fact_id: str,
    timeout: float,
) -> dict[str, Any]:
    pending = _get_memory_facts(state, status="pending", timeout=timeout)
    entries_before = _get_user_memory_entries(state, timeout=timeout)
    if not any(
        item.get("id") == pending_fact_id
        and item.get("content") == PENDING_SENTINEL
        for item in pending
    ):
        raise AssertionError("Packaged API did not expose the pending fixture")
    if any(item.get("content") == PENDING_SENTINEL for item in entries_before):
        raise AssertionError("Unconfirmed memory leaked into structured projection")

    _status, _body, created = _json_request(
        "POST",
        f"{_state_base_url(state)}/api/workspace/user-memory",
        payload={
            "content": CONFIRMED_SENTINEL,
            "type": "workflow",
            "confidence": 1,
            "reason": "packaged memory ownership smoke",
        },
        headers=_state_headers(state),
        timeout=timeout,
    )
    entry = created.get("entry") if isinstance(created, dict) else None
    fact_id = str(created.get("ledger_fact_id") or "") if isinstance(created, dict) else ""
    entry_id = str(entry.get("id") or "") if isinstance(entry, dict) else ""
    if not entry_id or not fact_id or entry.get("confirmed") is not True:
        raise AssertionError(f"Packaged API did not create confirmed memory: {created!r}")

    confirmed = _get_memory_facts(state, status="confirmed", timeout=timeout)
    entries_after = _get_user_memory_entries(state, timeout=timeout)
    if not any(item.get("id") == fact_id for item in confirmed):
        raise AssertionError("Confirmed canonical fact was not visible through package API")
    if not any(
        item.get("id") == entry_id
        and item.get("canonical_fact_id") == fact_id
        and item.get("confirmed") is True
        for item in entries_after
    ):
        raise AssertionError("Confirmed fact did not receive a structured projection")

    _json_request(
        "DELETE",
        f"{_state_base_url(state)}/api/workspace/user-memory/{entry_id}",
        headers=_state_headers(state),
        timeout=timeout,
    )
    forgotten = _assert_forgotten_memory_stays_forgotten(
        state,
        entry_id=entry_id,
        fact_id=fact_id,
        timeout=timeout,
    )
    return {
        "pending_fact_visible": True,
        "pending_projection_absent": True,
        "confirmed_fact_created": True,
        "confirmed_projection_created": True,
        "entry_id": entry_id,
        "fact_id": fact_id,
        "after_delete": forgotten,
    }


def _wait_for_provider_quiet(
    server: _LocalOpenAISmokeServer,
    *,
    timeout: float = 8.0,
    quiet_seconds: float = 0.75,
) -> None:
    deadline = time.monotonic() + timeout
    last_count = server.request_count
    quiet_since = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(0.1)
        current = server.request_count
        if current != last_count:
            last_count = current
            quiet_since = time.monotonic()
        elif time.monotonic() - quiet_since >= quiet_seconds:
            return
    raise TimeoutError("Packaged model provider did not become quiet")


def _send_memory_probe_turn(
    state: dict[str, Any],
    *,
    timeout: float,
) -> None:
    suffix = uuid.uuid4().hex[:12]
    client_turn_id = f"packaged-memory-context-{suffix}"
    _status, sse_text, _parsed = _json_request(
        "POST",
        f"{_state_base_url(state)}/api/console/chat",
        payload={
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": MODEL_CONTEXT_QUERY}],
                }
            ],
            "session_id": f"console:packaged-memory-context-{suffix}",
            "user_id": "default",
            "channel": "console",
            "stream": True,
            "client_turn_id": client_turn_id,
            "meta": {"client_turn_id": client_turn_id},
        },
        headers=_state_headers(state),
        timeout=timeout,
    )
    payloads = _extract_sse_payloads(sse_text)
    if not payloads:
        raise AssertionError("Packaged memory probe returned no SSE payloads")
    final = payloads[-1]
    if final.get("status") != "completed":
        raise AssertionError(f"Packaged memory probe did not complete: {final!r}")
    if SMOKE_MODEL_REPLY_PREFIX not in json.dumps(payloads, ensure_ascii=False):
        raise AssertionError("Packaged memory probe did not use the configured provider")


def _exercise_packaged_model_memory_context(
    state: dict[str, Any],
    *,
    server: _LocalOpenAISmokeServer,
    timeout: float,
) -> dict[str, Any]:
    _status, _body, created = _json_request(
        "POST",
        f"{_state_base_url(state)}/api/workspace/user-memory",
        payload={
            "content": MODEL_CONTEXT_SENTINEL,
            "type": "workflow",
            "confidence": 1,
            "reason": "packaged model memory context smoke",
        },
        headers=_state_headers(state),
        timeout=timeout,
    )
    entry = created.get("entry") if isinstance(created, dict) else None
    entry_id = str(entry.get("id") or "") if isinstance(entry, dict) else ""
    fact_id = str(created.get("ledger_fact_id") or "") if isinstance(created, dict) else ""
    if not entry_id or not fact_id:
        raise AssertionError(f"Could not create model-context memory: {created!r}")

    _wait_for_provider_quiet(server)
    before_start = server.request_count
    _send_memory_probe_turn(state, timeout=timeout)
    _wait_for_provider_quiet(server)
    before_payloads = server.request_payloads[before_start:]
    before_text = json.dumps(before_payloads, ensure_ascii=False)
    if MODEL_CONTEXT_SENTINEL not in before_text:
        raise AssertionError("Confirmed memory did not enter packaged model context")
    if PENDING_SENTINEL in before_text:
        raise AssertionError("Pending memory leaked into packaged model context")

    _json_request(
        "DELETE",
        f"{_state_base_url(state)}/api/workspace/user-memory/{entry_id}",
        headers=_state_headers(state),
        timeout=timeout,
    )
    forgotten = _assert_forgotten_memory_stays_forgotten(
        state,
        entry_id=entry_id,
        fact_id=fact_id,
        timeout=timeout,
    )
    _wait_for_provider_quiet(server)
    after_start = server.request_count
    _send_memory_probe_turn(state, timeout=timeout)
    _wait_for_provider_quiet(server)
    after_payloads = server.request_payloads[after_start:]
    after_text = json.dumps(after_payloads, ensure_ascii=False)
    if MODEL_CONTEXT_SENTINEL in after_text:
        raise AssertionError("Forgotten memory remained in packaged model context")
    if PENDING_SENTINEL in after_text:
        raise AssertionError("Pending memory entered model context after forget")
    return {
        "confirmed_present_before_forget": True,
        "pending_absent_before_forget": True,
        "confirmed_absent_after_forget": True,
        "pending_absent_after_forget": True,
        "provider_requests_before_forget": len(before_payloads),
        "provider_requests_after_forget": len(after_payloads),
        "entry_id": entry_id,
        "fact_id": fact_id,
        "after_delete": forgotten,
    }


def _launch(
    executable: Path,
    app_path: Path,
    *,
    env: dict[str, str],
    log_file,
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [str(executable)],
        cwd=str(app_path.parent),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _run(args: argparse.Namespace) -> dict[str, Any]:
    app_path = Path(args.app).expanduser().resolve()
    executable = _find_app_executable(app_path)
    smoke_root = Path(tempfile.mkdtemp(prefix="lantai-packaged-memory-smoke-"))
    state_path = smoke_root / "desktop-lifecycle-state.json"
    user_data_dir = smoke_root / "user-data"
    workspace_dir = user_data_dir / "working" / "workspaces" / "default"
    log_path = smoke_root / "app.log"
    started_at = _utc_now_iso()
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
        }
    )
    proc: subprocess.Popen[str] | None = None
    provider_context: _LocalOpenAISmokeServer | None = None
    provider: _LocalOpenAISmokeServer | None = None
    phase = "initial launch"
    try:
        fixture = _seed_pending_fixture(workspace_dir)
        provider_context = _LocalOpenAISmokeServer()
        provider = provider_context.__enter__()
        provider_config = _write_smoke_openai_provider_config(
            user_data_dir,
            base_url=provider.base_url,
        )
        with log_path.open("w", encoding="utf-8") as log_file:
            proc = _launch(executable, app_path, env=env, log_file=log_file)
            state = _wait_for_state(
                state_path,
                timeout=args.timeout,
                predicate=lambda payload: int(payload.get("sequence") or 0) >= 1,
                process=proc,
                expected_distribution=args.distribution,
            )
            _assert_ready_state(state, expected_distribution=args.distribution)
            _assert_desktop_readiness(state, timeout=args.timeout)
            initial = _exercise_initial_memory_ownership(
                state,
                pending_fact_id=str(fixture["pending_fact_id"]),
                timeout=args.timeout,
            )
            model_context = _exercise_packaged_model_memory_context(
                state,
                server=provider,
                timeout=args.timeout,
            )

            phase = "managed backend restart"
            old_sequence = int(state.get("sequence") or 0)
            old_pid = int(state.get("backend_pid") or 0)
            os.kill(old_pid, 9)
            restarted = _wait_for_state(
                state_path,
                timeout=args.timeout,
                predicate=lambda payload: (
                    int(payload.get("sequence") or 0) > old_sequence
                    and int(payload.get("backend_pid") or 0) > 0
                    and int(payload.get("backend_pid") or 0) != old_pid
                ),
                process=proc,
                expected_distribution=args.distribution,
            )
            _assert_ready_state(restarted, expected_distribution=args.distribution)
            _assert_desktop_readiness(restarted, timeout=args.timeout)
            after_backend_restart = _assert_forgotten_memory_stays_forgotten(
                restarted,
                entry_id=str(initial["entry_id"]),
                fact_id=str(initial["fact_id"]),
                timeout=args.timeout,
            )

            phase = "desktop app relaunch"
            previous_nonce = str(restarted.get("desktop_instance_nonce") or "")
            _stop_app(proc)
            proc = None
            state_path.unlink(missing_ok=True)
            proc = _launch(executable, app_path, env=env, log_file=log_file)
            relaunched = _wait_for_state(
                state_path,
                timeout=args.timeout,
                predicate=lambda payload: _is_new_desktop_instance(
                    payload,
                    previous_nonce=previous_nonce,
                ),
                process=proc,
                expected_distribution=args.distribution,
            )
            _assert_ready_state(relaunched, expected_distribution=args.distribution)
            _assert_desktop_readiness(relaunched, timeout=args.timeout)
            after_app_relaunch = _assert_forgotten_memory_stays_forgotten(
                relaunched,
                entry_id=str(initial["entry_id"]),
                fact_id=str(initial["fact_id"]),
                timeout=args.timeout,
            )
            if any(
                item.get("content") == PENDING_SENTINEL
                for item in _get_user_memory_entries(relaunched, timeout=args.timeout)
            ):
                raise AssertionError("Pending memory projected after app relaunch")

        evidence = {
            "schema": EVIDENCE_SCHEMA,
            "passed": True,
            "distribution": args.distribution,
            "started_at": started_at,
            "completed_at": _utc_now_iso(),
            "app": _app_metadata_for_evidence(app_path),
            "isolation": {
                "temporary_user_data": True,
                "updater_disabled": True,
                "user_workspace_read": False,
            },
            "provider": {
                "provider_id": provider_config["provider_id"],
                "model": provider_config["model"],
                "loopback_only": True,
                "request_count": provider.request_count,
            },
            "checks": {
                "sensitive_fixture_rejected": fixture["sensitive_fixture_rejected"],
                "initial": initial,
                "model_context": model_context,
                "after_backend_restart": after_backend_restart,
                "after_app_relaunch": after_app_relaunch,
                "pending_projection_absent_after_relaunch": True,
            },
        }
        if args.evidence_output:
            _write_json_file(Path(args.evidence_output), evidence)
        return evidence
    except Exception as exc:
        raise RuntimeError(f"Packaged memory smoke failed during {phase}: {exc}") from exc
    finally:
        _stop_app(proc)
        if provider_context is not None:
            with contextlib.suppress(Exception):
                provider_context.__exit__(None, None, None)
        if not args.keep_workdir:
            shutil.rmtree(smoke_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify memory ownership through an actual packaged Lantai app."
    )
    parser.add_argument("--app", required=True)
    parser.add_argument("--distribution", default="official")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--evidence-output")
    parser.add_argument("--keep-workdir", action="store_true")
    args = parser.parse_args(argv)
    try:
        evidence = _run(args)
    except Exception as exc:
        print(f"Packaged memory ownership smoke failed: {exc}", file=os.sys.stderr)
        return 1
    print(
        "Packaged memory ownership smoke passed: "
        f"version={evidence['app']['short_version']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
