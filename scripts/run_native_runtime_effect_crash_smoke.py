#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run a real NativeLantaiRuntime process-kill smoke for tool effects."""

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

from lantai.runtime.adapters.native_runtime import NativeLantaiRuntime
from lantai.runtime.effects import (
    RuntimeEffectJournal,
    RuntimeEffectKillPoint,
    RuntimeEffectRecoveryCapability,
    assess_runtime_effect_kill_point_replay,
)
from lantai.runtime.input import RuntimeInput, RuntimeRequestContext
from lantai.runtime.messages import RuntimeMessage
from lantai.runtime.models import (
    ModelContentBlock,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
)
from lantai.runtime.tools import (
    LantaiToolRegistry,
    ToolCall,
    ToolResult,
    ToolSpec,
)


RUN_ID = "native-effect-crash-smoke-run"
ATTEMPT_ID = "native-effect-crash-smoke-attempt"
TOOL_CALL_ID = "native-effect-crash-smoke-call"
EFFECT_ID = f"effect:{TOOL_CALL_ID}"
TOOL_NAME = "native_effect_crash_smoke_tool"

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
            "payload_hash": "sha256:native-external-payload",
            "ts": datetime.now(timezone.utc).isoformat(),
        },
    )


def _write_checkpoint(workspace: Path, kill_point: str) -> None:
    _write_json_atomic(
        workspace / "native-checkpoint.json",
        {
            "effect_id": EFFECT_ID,
            "kill_point": kill_point,
            "checkpoint": "tool-result-consumed",
            "ts": datetime.now(timezone.utc).isoformat(),
        },
    )


class _NativeSmokeModelClient:
    provider_id = "native-smoke"
    model_name = "native-smoke-model"

    def __init__(self) -> None:
        self._request_count = 0

    async def stream(self, request: ModelRequest):
        self._request_count += 1
        if self._request_count == 1:
            yield ModelStreamEvent(kind="message_start", message_id="msg-tool")
            yield ModelStreamEvent(
                kind="tool_call_completed",
                message_id="msg-tool",
                tool_call=ToolCall(
                    id=TOOL_CALL_ID,
                    name=TOOL_NAME,
                    arguments={"value": "payload"},
                    metadata={"attempt_id": ATTEMPT_ID},
                ),
            )
            yield ModelStreamEvent(
                kind="message_completed",
                message_id="msg-tool",
                response=ModelResponse(
                    message=ModelMessage(id="msg-tool", role="assistant"),
                ),
            )
            return

        yield ModelStreamEvent(kind="message_start", message_id="msg-final")
        yield ModelStreamEvent(kind="text_delta", message_id="msg-final", delta="done")
        yield ModelStreamEvent(
            kind="message_completed",
            message_id="msg-final",
            response=ModelResponse(
                message=ModelMessage(
                    id="msg-final",
                    role="assistant",
                    content=[ModelContentBlock(type="text", text="done")],
                ),
            ),
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(message=ModelMessage.text("assistant", "done"))


class _NativeCrashSmokeRuntime(NativeLantaiRuntime):
    def __init__(
        self,
        *,
        workspace: Path,
        kill_point: RuntimeEffectKillPoint,
        registry: LantaiToolRegistry,
        journal: RuntimeEffectJournal,
    ) -> None:
        super().__init__(
            model_client_factory=lambda _agent_id: _NativeSmokeModelClient(),
            tool_registry=registry,
            effect_journal=journal,
        )
        self._smoke_workspace = workspace
        self._smoke_kill_point = kill_point

    def _maybe_stop(self, kill_point: RuntimeEffectKillPoint) -> None:
        if self._smoke_kill_point != kill_point:
            return
        _write_marker(self._smoke_workspace, kill_point)
        _wait_for_kill()

    def _record_tool_effect_intent(self, *args: Any, **kwargs: Any):
        self._maybe_stop("before_intent_write")
        effect = super()._record_tool_effect_intent(*args, **kwargs)
        self._maybe_stop("after_intent_fsync_before_execution")
        return effect

    def _record_tool_effect_settled(self, *args: Any, **kwargs: Any) -> ToolResult:
        self._maybe_stop("after_external_success_before_commit")
        result = super()._record_tool_effect_settled(*args, **kwargs)
        self._maybe_stop("after_commit_before_checkpoint")
        return result


def _recovery() -> RuntimeEffectRecoveryCapability:
    return RuntimeEffectRecoveryCapability(
        external_side_effect=True,
        idempotency="not_idempotent",
        probe="none",
        compensation="none",
        on_ambiguity="manual_disambiguation",
    )


def _build_registry(workspace: Path, kill_point: RuntimeEffectKillPoint) -> LantaiToolRegistry:
    def native_side_effect_tool(value: str) -> ToolResult:
        _record_external_call(workspace, kill_point)
        if kill_point == "after_external_call_before_response":
            _write_marker(workspace, kill_point)
            _wait_for_kill()
        return ToolResult(
            call_id=TOOL_CALL_ID,
            name=TOOL_NAME,
            ok=True,
            content=[{"type": "text", "text": f"external:{value}"}],
        )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name=TOOL_NAME,
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
            effect_recovery=_recovery(),
        ),
        native_side_effect_tool,
    )
    return registry


async def _run_child_async(workspace: Path, kill_point: RuntimeEffectKillPoint) -> None:
    journal = RuntimeEffectJournal(workspace)
    runtime = _NativeCrashSmokeRuntime(
        workspace=workspace,
        kill_point=kill_point,
        registry=_build_registry(workspace, kill_point),
        journal=journal,
    )
    runtime_input = RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id="session-native-smoke",
            user_id="user-native-smoke",
            run_id=RUN_ID,
            workspace_dir=str(workspace),
            metadata={
                "attempt_id": ATTEMPT_ID,
                "contract_hash": "sha256:native-contract",
                "policy_snapshot_hash": "sha256:native-policy",
                "release_edition": "official",
            },
        ),
        messages=[RuntimeMessage.text("user", "run native smoke")],
    )

    async for event in runtime.run_turn(runtime_input):
        _append_jsonl(
            workspace / "runtime-events.jsonl",
            event.model_dump(mode="json"),
        )
        if event.type == "tool.call.completed":
            _write_checkpoint(workspace, kill_point)
            if kill_point == "after_checkpoint_before_terminal":
                _write_marker(workspace, kill_point)
                _wait_for_kill()


def _run_child(workspace: Path, kill_point: RuntimeEffectKillPoint) -> None:
    asyncio.run(_run_child_async(workspace, kill_point))


def _wait_for_marker(workspace: Path, kill_point: str, timeout: float) -> Path:
    marker = workspace / "markers" / f"{kill_point}.ready"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            return marker
        time.sleep(0.02)
    raise TimeoutError(f"Timed out waiting for native runtime kill marker: {kill_point}")


def _kill_process(process: subprocess.Popen[bytes]) -> int:
    os.kill(process.pid, signal.SIGKILL)
    return process.wait(timeout=5)


def _latest_effect(workspace: Path):
    return RuntimeEffectJournal(workspace).latest_effect(RUN_ID, ATTEMPT_ID, EFFECT_ID)


def _event_types(workspace: Path) -> list[str]:
    events_path = workspace / "runtime-events.jsonl"
    if not events_path.exists():
        return []
    event_types: list[str] = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        event_types.append(str(payload.get("type") or ""))
    return event_types


def _resume_decision(workspace: Path, kill_point: RuntimeEffectKillPoint) -> dict[str, Any]:
    latest = _latest_effect(workspace)
    before_calls = _count_jsonl(workspace / "external-effects.jsonl")
    assessment = assess_runtime_effect_kill_point_replay(
        kill_point=kill_point,
        effect=latest,
        recovery=_recovery(),
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
        "checkpoint_exists": (workspace / "native-checkpoint.json").exists(),
        "event_types": _event_types(workspace),
    }


def _assert_smoke_invariants(result: dict[str, Any]) -> None:
    kill_point = result["kill_point"]
    if not result["child_killed"]:
        raise AssertionError(f"native child was not killed at {kill_point}")
    if kill_point == "before_intent_write":
        if result["observed_status"] != "missing":
            raise AssertionError("before_intent should not write an EffectRecord")
    if kill_point == "after_intent_fsync_before_execution":
        if result["observed_status"] != "intent":
            raise AssertionError("after_intent should leave an intent EffectRecord")
    if kill_point in DANGEROUS_WINDOWS:
        if result["external_calls_before_recovery"] != 1:
            raise AssertionError(f"expected one native external call before recovery at {kill_point}")
        if result["external_calls_after_recovery"] != 1:
            raise AssertionError(f"native external call replayed at {kill_point}")
        if result["replayed_external_call"]:
            raise AssertionError(f"native replay executed at {kill_point}")
        if not result["external_call_must_not_be_replayed"]:
            raise AssertionError(f"kill matrix allowed unsafe native replay at {kill_point}")
    if kill_point == "after_commit_before_checkpoint":
        if not result["needs_checkpoint_rebuild"] or result["checkpoint_exists"]:
            raise AssertionError("after_commit should require checkpoint rebuild")
    if kill_point == "after_checkpoint_before_terminal":
        if not result["needs_terminal_rewrite"] or not result["checkpoint_exists"]:
            raise AssertionError("after_checkpoint should require terminal rewrite")
        if "turn.completed" in result["event_types"]:
            raise AssertionError("after_checkpoint should kill before terminal event")


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
        with tempfile.TemporaryDirectory(prefix="lantai-native-effect-crash-smoke-") as tmp:
            print(json.dumps(run_smoke(Path(tmp)), ensure_ascii=False, indent=2))
        return 0

    args.workspace.mkdir(parents=True, exist_ok=True)
    print(json.dumps(run_smoke(args.workspace), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
