#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run a real process-kill smoke for the Runtime Effect crash matrix."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lantai.runtime.effects import (
    RuntimeEffectKillPoint,
    RuntimeEffectRecoveryCapability,
    RuntimeEffectReducerAction,
    RuntimeEffectJournal,
    assess_runtime_effect_kill_point_replay,
    reduce_runtime_effect,
)
from lantai.runtime.runs import RuntimeEffectRecord, RuntimeRunOwner


RUN_ID = "effect-crash-smoke-run"
ATTEMPT_ID = "effect-crash-smoke-attempt"
STEP_ID = "effect-crash-smoke-step"
EFFECT_ID = "effect-crash-smoke-effect"
NOW = datetime(2026, 7, 16, 14, 0, tzinfo=timezone.utc)

KILL_POINTS: tuple[RuntimeEffectKillPoint, ...] = (
    "before_intent_write",
    "after_intent_fsync_before_execution",
    "after_external_call_before_response",
    "after_external_success_before_commit",
    "after_commit_before_checkpoint",
    "after_checkpoint_before_terminal",
)


def _owner() -> RuntimeRunOwner:
    return RuntimeRunOwner(
        user_id="user-1",
        agent_id="agent-default",
        session_id="session-1",
        workspace_id="workspace-1",
        channel="console",
    )


def _initial_effect() -> RuntimeEffectRecord:
    return RuntimeEffectRecord(
        run_id=RUN_ID,
        attempt_id=ATTEMPT_ID,
        step_id=STEP_ID,
        effect_id=EFFECT_ID,
        source="browser",
        owner=_owner(),
        status="intent",
        revision=0,
        tool_name="browser.publish",
        args_hash="sha256:args",
        policy_snapshot_hash="sha256:policy",
        idempotency_key=f"{RUN_ID}:{ATTEMPT_ID}:{STEP_ID}:{EFFECT_ID}",
        created_at=NOW,
        release_edition="official",
    )


def _non_idempotent_recovery() -> RuntimeEffectRecoveryCapability:
    return RuntimeEffectRecoveryCapability(
        external_side_effect=True,
        idempotency="not_idempotent",
        probe="none",
        compensation="none",
        on_ambiguity="manual_disambiguation",
    )


def _json_line(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            fd = -1
            handle.write(_json_line(payload))
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if fd >= 0:
            os.close(fd)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def _count_external_calls(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def _record_external_call(workspace: Path, kill_point: str) -> None:
    _append_jsonl(
        workspace / "external-effects.jsonl",
        {
            "effect_id": EFFECT_ID,
            "kill_point": kill_point,
            "payload_hash": "sha256:external-payload",
            "ts": datetime.now(timezone.utc).isoformat(),
        },
    )


def _write_checkpoint(workspace: Path, kill_point: str) -> None:
    _write_json_atomic(
        workspace / "checkpoint.json",
        {
            "effect_id": EFFECT_ID,
            "kill_point": kill_point,
            "checkpoint": "after-effect",
            "ts": datetime.now(timezone.utc).isoformat(),
        },
    )


def _write_marker(workspace: Path, kill_point: str) -> None:
    marker = workspace / "markers" / f"{kill_point}.ready"
    _write_json_atomic(
        marker,
        {
            "kill_point": kill_point,
            "pid": os.getpid(),
            "ts": datetime.now(timezone.utc).isoformat(),
        },
    )


def _wait_for_kill() -> None:
    while True:
        time.sleep(60)


def _write_child_state(workspace: Path, kill_point: RuntimeEffectKillPoint) -> None:
    journal = RuntimeEffectJournal(workspace)
    intent = _initial_effect()

    if kill_point == "before_intent_write":
        _write_marker(workspace, kill_point)
        _wait_for_kill()

    journal.append_effect(intent)
    if kill_point == "after_intent_fsync_before_execution":
        _write_marker(workspace, kill_point)
        _wait_for_kill()

    running = reduce_runtime_effect(
        intent,
        RuntimeEffectReducerAction(type="start", now=NOW),
    ).effect
    journal.append_effect(running)
    _record_external_call(workspace, kill_point)
    if kill_point in {
        "after_external_call_before_response",
        "after_external_success_before_commit",
    }:
        _write_marker(workspace, kill_point)
        _wait_for_kill()

    committed = reduce_runtime_effect(
        running,
        RuntimeEffectReducerAction(
            type="commit",
            result_hash="sha256:external-result",
            now=NOW,
        ),
    ).effect
    journal.append_effect(committed)
    if kill_point == "after_commit_before_checkpoint":
        _write_marker(workspace, kill_point)
        _wait_for_kill()

    _write_checkpoint(workspace, kill_point)
    if kill_point == "after_checkpoint_before_terminal":
        _write_marker(workspace, kill_point)
        _wait_for_kill()

    raise SystemExit(f"unsupported kill point: {kill_point}")


def _wait_for_marker(workspace: Path, kill_point: str, timeout: float) -> Path:
    marker = workspace / "markers" / f"{kill_point}.ready"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            return marker
        time.sleep(0.02)
    raise TimeoutError(f"Timed out waiting for kill marker: {kill_point}")


def _kill_process(process: subprocess.Popen[bytes]) -> int:
    os.kill(process.pid, signal.SIGKILL)
    return process.wait(timeout=5)


def _read_latest_effect(workspace: Path) -> RuntimeEffectRecord | None:
    return RuntimeEffectJournal(workspace).latest_effect(
        RUN_ID,
        ATTEMPT_ID,
        EFFECT_ID,
    )


def _resume_decision(workspace: Path, kill_point: RuntimeEffectKillPoint) -> dict[str, Any]:
    latest = _read_latest_effect(workspace)
    before_calls = _count_external_calls(workspace / "external-effects.jsonl")
    assessment = assess_runtime_effect_kill_point_replay(
        kill_point=kill_point,
        effect=latest,
        recovery=_non_idempotent_recovery(),
    )
    replayed_external_call = False
    if assessment.automatic_external_call_allowed and before_calls == 0:
        _record_external_call(workspace, f"resume:{kill_point}")
        replayed_external_call = True
    after_calls = _count_external_calls(workspace / "external-effects.jsonl")
    checkpoint_exists = (workspace / "checkpoint.json").exists()
    return {
        "kill_point": kill_point,
        "observed_status": assessment.observed_status,
        "crash_window": assessment.base_assessment.crash_window,
        "child_status": None if latest is None else latest.status,
        "child_revision": None if latest is None else latest.revision,
        "external_calls_before_recovery": before_calls,
        "external_calls_after_recovery": after_calls,
        "replayed_external_call": replayed_external_call,
        "automatic_external_call_allowed": assessment.automatic_external_call_allowed,
        "external_call_may_have_been_issued": assessment.external_call_may_have_been_issued,
        "external_call_must_not_be_replayed": assessment.external_call_must_not_be_replayed,
        "requires_probe": assessment.requires_probe,
        "requires_human": assessment.requires_human,
        "needs_checkpoint_rebuild": assessment.needs_checkpoint_rebuild,
        "needs_terminal_rewrite": assessment.needs_terminal_rewrite,
        "recommended_reducer_actions": list(assessment.recommended_reducer_actions),
        "checkpoint_exists": checkpoint_exists,
    }


def _run_one(root: Path, kill_point: RuntimeEffectKillPoint) -> dict[str, Any]:
    workspace = root / kill_point
    workspace.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--child",
            "--workspace",
            str(workspace),
            "--kill-point",
            kill_point,
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_marker(workspace, kill_point, timeout=5)
        exit_code = _kill_process(process)
        stderr = process.stderr.read().decode("utf-8", errors="replace")
        result = _resume_decision(workspace, kill_point)
        result.update(
            {
                "workspace": str(workspace),
                "child_pid": process.pid,
                "child_exit_code": exit_code,
                "child_killed": exit_code < 0,
                "stderr": stderr,
            },
        )
        _assert_smoke_invariants(result)
        return result
    finally:
        if process.poll() is None:
            _kill_process(process)


def _assert_smoke_invariants(result: dict[str, Any]) -> None:
    kill_point = result["kill_point"]
    if not result["child_killed"]:
        raise AssertionError(f"child was not killed at {kill_point}")
    if kill_point in {
        "after_external_call_before_response",
        "after_external_success_before_commit",
        "after_commit_before_checkpoint",
        "after_checkpoint_before_terminal",
    }:
        if result["external_calls_before_recovery"] != 1:
            raise AssertionError(f"expected one external call before recovery at {kill_point}")
        if result["external_calls_after_recovery"] != 1:
            raise AssertionError(f"external call replayed at {kill_point}")
        if not result["external_call_must_not_be_replayed"]:
            raise AssertionError(f"kill matrix allowed unsafe replay at {kill_point}")
    if kill_point == "before_intent_write":
        if result["observed_status"] != "missing":
            raise AssertionError("before_intent should have no journaled effect")
    if kill_point == "after_intent_fsync_before_execution":
        if result["observed_status"] != "intent":
            raise AssertionError("after_intent should leave an intent record")
    if kill_point == "after_commit_before_checkpoint":
        if not result["needs_checkpoint_rebuild"] or result["checkpoint_exists"]:
            raise AssertionError("after_commit should require checkpoint rebuild")
    if kill_point == "after_checkpoint_before_terminal":
        if not result["needs_terminal_rewrite"] or not result["checkpoint_exists"]:
            raise AssertionError("after_checkpoint should require terminal rewrite")


def run_smoke(root: Path) -> dict[str, Any]:
    results = [_run_one(root, kill_point) for kill_point in KILL_POINTS]
    return {
        "ok": True,
        "schema_version": 1,
        "root": str(root),
        "kill_points": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--kill-point", choices=KILL_POINTS)
    parser.add_argument("--child", action="store_true")
    args = parser.parse_args(argv)

    if args.child:
        if args.workspace is None or args.kill_point is None:
            parser.error("--child requires --workspace and --kill-point")
        _write_child_state(args.workspace, args.kill_point)
        return 0

    if args.workspace is None:
        with tempfile.TemporaryDirectory(prefix="lantai-effect-crash-smoke-") as tmp:
            print(json.dumps(run_smoke(Path(tmp)), ensure_ascii=False, indent=2))
        return 0

    args.workspace.mkdir(parents=True, exist_ok=True)
    print(json.dumps(run_smoke(args.workspace), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
