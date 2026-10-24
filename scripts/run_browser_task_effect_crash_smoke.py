#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run a real BrowserTaskRuntime process-kill smoke for action effects."""

from __future__ import annotations

import argparse
import asyncio
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

from lantai.app.browser_workbench import BrowserTaskRuntime
from lantai.app.browser_workbench.effects import browser_action_effect_recovery
from lantai.app.browser_workbench.models import (
    BrowserAction,
    BrowserObservation,
    BrowserPolicyDecision,
    BrowserTaskEvent,
    BrowserTaskRequest,
    BrowserTaskRun,
)
from lantai.runtime.effects import (
    RuntimeEffectJournal,
    RuntimeEffectKillPoint,
    assess_runtime_effect_kill_point_replay,
)


RUN_ID = "browser-effect-crash-smoke-run"
ATTEMPT_ID = "browser-effect-crash-smoke-attempt"
EFFECT_ID = "browser-effect-crash-smoke-effect"
ACTION_NAME = "click"

KILL_POINTS: tuple[RuntimeEffectKillPoint, ...] = (
    "before_intent_write",
    "after_intent_fsync_before_execution",
    "after_external_call_before_response",
    "after_external_success_before_commit",
    "after_commit_before_checkpoint",
    "after_checkpoint_before_terminal",
)

DANGEROUS_WINDOWS: set[RuntimeEffectKillPoint] = {
    "after_external_call_before_response",
    "after_external_success_before_commit",
    "after_commit_before_checkpoint",
    "after_checkpoint_before_terminal",
}


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


def _write_marker(workspace: Path, kill_point: str) -> None:
    _write_json_atomic(
        workspace / "markers" / f"{kill_point}.ready",
        {
            "kill_point": kill_point,
            "pid": os.getpid(),
            "ts": datetime.now(timezone.utc).isoformat(),
        },
    )


def _wait_for_kill() -> None:
    while True:
        time.sleep(60)


def _count_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def _record_external_call(workspace: Path, kill_point: str) -> None:
    _append_jsonl(
        workspace / "external-effects.jsonl",
        {
            "effect_id": EFFECT_ID,
            "kill_point": kill_point,
            "payload_hash": "sha256:browser-external-payload",
            "ts": datetime.now(timezone.utc).isoformat(),
        },
    )


def _write_checkpoint(workspace: Path, kill_point: str) -> None:
    _write_json_atomic(
        workspace / "browser-checkpoint.json",
        {
            "effect_id": EFFECT_ID,
            "kill_point": kill_point,
            "checkpoint": "action-observation-persisted",
            "ts": datetime.now(timezone.utc).isoformat(),
        },
    )


class _BrowserCrashSmokeBroker:
    def __init__(self, *, workspace: Path, kill_point: RuntimeEffectKillPoint) -> None:
        self._workspace = workspace
        self._kill_point = kill_point

    def evaluate(self, action: BrowserAction) -> BrowserPolicyDecision:
        return BrowserPolicyDecision(
            decision="allow",
            reason="browser crash smoke policy allow",
            access=action.access or "write",
        )

    async def execute(self, action: BrowserAction) -> BrowserObservation:
        _record_external_call(self._workspace, self._kill_point)
        if self._kill_point == "after_external_call_before_response":
            _write_marker(self._workspace, self._kill_point)
            _wait_for_kill()
        return BrowserObservation(
            ok=True,
            action=action.action,
            runtime="electron_bridge",
            profile_id=action.profile_id,
            page_id=action.page_id,
            url=action.url,
            message="browser action ok",
        )


class _BrowserCrashSmokeRuntime(BrowserTaskRuntime):
    def __init__(
        self,
        *,
        workspace: Path,
        kill_point: RuntimeEffectKillPoint,
        journal: RuntimeEffectJournal,
    ) -> None:
        super().__init__(
            broker=_BrowserCrashSmokeBroker(workspace=workspace, kill_point=kill_point),
            app_store_distribution=lambda: False,
            effect_journal=journal,
        )
        self._smoke_workspace = workspace
        self._smoke_kill_point = kill_point

    def _maybe_stop(self, kill_point: RuntimeEffectKillPoint) -> None:
        if self._smoke_kill_point != kill_point:
            return
        _write_marker(self._smoke_workspace, kill_point)
        _wait_for_kill()

    def _record_effect_intent(self, *args: Any, **kwargs: Any):
        self._maybe_stop("before_intent_write")
        effect = super()._record_effect_intent(*args, **kwargs)
        self._maybe_stop("after_intent_fsync_before_execution")
        return effect

    def _record_effect_settled(self, *args: Any, **kwargs: Any):
        self._maybe_stop("after_external_success_before_commit")
        effect = super()._record_effect_settled(*args, **kwargs)
        self._maybe_stop("after_commit_before_checkpoint")
        return effect

    def _append_event(
        self,
        run: BrowserTaskRun,
        event_type: str,
        message: str,
        *,
        action: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        super()._append_event(
            run,
            event_type,
            message,
            action=action,
            metadata=metadata,
        )
        event = self._events.get(run.run_id, [])[-1]
        _append_jsonl(
            self._smoke_workspace / "browser-task-events.jsonl",
            event.model_dump(mode="json"),
        )

    def _append_observation_event(
        self,
        run: BrowserTaskRun,
        observation: BrowserObservation,
        event_type: str,
        message: str,
        *,
        extra_metadata: dict[str, Any] | None = None,
    ) -> None:
        super()._append_observation_event(
            run,
            observation,
            event_type,
            message,
            extra_metadata=extra_metadata,
        )
        if event_type == "action_completed":
            _write_checkpoint(self._smoke_workspace, self._smoke_kill_point)
            self._maybe_stop("after_checkpoint_before_terminal")


def _action() -> BrowserAction:
    return BrowserAction(
        action=ACTION_NAME,
        selector="#publish",
        access="write",
        confirmed=True,
        url="https://example.com/editor",
        metadata={
            "effect_id": EFFECT_ID,
            "idempotency_key": f"{RUN_ID}:{ATTEMPT_ID}:{EFFECT_ID}",
        },
    )


async def _run_child_async(workspace: Path, kill_point: RuntimeEffectKillPoint) -> None:
    runtime = _BrowserCrashSmokeRuntime(
        workspace=workspace,
        kill_point=kill_point,
        journal=RuntimeEffectJournal(workspace),
    )
    run = await runtime.create(
        BrowserTaskRequest(
            run_id=RUN_ID,
            access="write",
            target_domains=["example.com"],
            instruction="click publish",
            actions=[_action()],
            requires_foreground_confirmation=False,
            max_actions=1,
            max_duration_seconds=30,
            metadata={
                "attempt_id": ATTEMPT_ID,
                "workspace_dir": str(workspace),
                "user_id": "user-browser-smoke",
                "agent_id": "browser-agent",
                "session_id": "browser-session",
                "contract_hash": "sha256:browser-contract",
                "policy_snapshot_hash": "sha256:browser-policy",
            },
        )
    )
    handle = runtime._handles.get(run.run_id)  # pylint: disable=protected-access
    if handle is not None:
        await handle


def _run_child(workspace: Path, kill_point: RuntimeEffectKillPoint) -> None:
    asyncio.run(_run_child_async(workspace, kill_point))


def _wait_for_marker(workspace: Path, kill_point: str, timeout: float) -> Path:
    marker = workspace / "markers" / f"{kill_point}.ready"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            return marker
        time.sleep(0.02)
    raise TimeoutError(f"Timed out waiting for BrowserTask kill marker: {kill_point}")


def _kill_process(process: subprocess.Popen[bytes]) -> int:
    os.kill(process.pid, signal.SIGKILL)
    return process.wait(timeout=5)


def _latest_effect(workspace: Path):
    return RuntimeEffectJournal(workspace).latest_effect(RUN_ID, ATTEMPT_ID, EFFECT_ID)


def _event_types(workspace: Path) -> list[str]:
    path = workspace / "browser-task-events.jsonl"
    if not path.exists():
        return []
    event_types: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = BrowserTaskEvent.model_validate(json.loads(line))
        event_types.append(event.type)
    return event_types


def _resume_decision(workspace: Path, kill_point: RuntimeEffectKillPoint) -> dict[str, Any]:
    latest = _latest_effect(workspace)
    before_calls = _count_jsonl(workspace / "external-effects.jsonl")
    assessment = assess_runtime_effect_kill_point_replay(
        kill_point=kill_point,
        effect=latest,
        recovery=browser_action_effect_recovery(_action()),
    )
    replayed_external_call = False
    if assessment.automatic_external_call_allowed and before_calls == 0:
        _record_external_call(workspace, f"resume:{kill_point}")
        replayed_external_call = True
    after_calls = _count_jsonl(workspace / "external-effects.jsonl")
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
        "checkpoint_exists": (workspace / "browser-checkpoint.json").exists(),
        "event_types": _event_types(workspace),
    }


def _assert_smoke_invariants(result: dict[str, Any]) -> None:
    kill_point = result["kill_point"]
    if not result["child_killed"]:
        raise AssertionError(f"BrowserTask child was not killed at {kill_point}")
    if kill_point == "before_intent_write":
        if result["observed_status"] != "missing":
            raise AssertionError("before_intent should not write a BrowserTask EffectRecord")
    if kill_point == "after_intent_fsync_before_execution":
        if result["observed_status"] != "intent":
            raise AssertionError("after_intent should leave an intent BrowserTask EffectRecord")
    if kill_point in DANGEROUS_WINDOWS:
        if result["external_calls_before_recovery"] != 1:
            raise AssertionError(f"expected one BrowserTask external call before recovery at {kill_point}")
        if result["external_calls_after_recovery"] != 1:
            raise AssertionError(f"BrowserTask external call replayed at {kill_point}")
        if result["replayed_external_call"]:
            raise AssertionError(f"BrowserTask replay executed at {kill_point}")
        if not result["external_call_must_not_be_replayed"]:
            raise AssertionError(f"kill matrix allowed unsafe BrowserTask replay at {kill_point}")
    if kill_point == "after_commit_before_checkpoint":
        if not result["needs_checkpoint_rebuild"] or result["checkpoint_exists"]:
            raise AssertionError("after_commit should require BrowserTask checkpoint rebuild")
        if "action_completed" in result["event_types"]:
            raise AssertionError("after_commit should kill before action_completed")
    if kill_point == "after_checkpoint_before_terminal":
        if not result["needs_terminal_rewrite"] or not result["checkpoint_exists"]:
            raise AssertionError("after_checkpoint should require BrowserTask terminal rewrite")
        if "action_completed" not in result["event_types"]:
            raise AssertionError("after_checkpoint should include action_completed")
        if "task_completed" in result["event_types"]:
            raise AssertionError("after_checkpoint should kill before task_completed")


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
        _wait_for_marker(workspace, kill_point, timeout=10)
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
        _run_child(args.workspace, args.kill_point)
        return 0

    if args.workspace is None:
        with tempfile.TemporaryDirectory(prefix="lantai-browser-effect-crash-smoke-") as tmp:
            print(json.dumps(run_smoke(Path(tmp)), ensure_ascii=False, indent=2))
        return 0

    args.workspace.mkdir(parents=True, exist_ok=True)
    print(json.dumps(run_smoke(args.workspace), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
