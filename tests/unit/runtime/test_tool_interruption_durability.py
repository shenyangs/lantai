"""Interrupted tool writes must settle before the attempt can be reused."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from lantai.runtime.adapters.native_runtime import NativeLantaiRuntime
from lantai.runtime.artifact_effects import RuntimeArtifactEffectJournal
from lantai.runtime.effects import RuntimeEffectJournal, RuntimeEffectRecoveryCapability
from lantai.runtime.models import ModelMessage, ModelResponse, ModelStreamEvent
from lantai.runtime.native_turn_support import _TurnDeadlineExceeded
from lantai.runtime.tools import LantaiToolRegistry, ToolCall, ToolOutputSemantics, ToolSpec
from tests.unit.runtime.test_native_runtime import ScriptedModelClient, _completed, _runtime_input


def _call(path: Path, call_id: str = "write-1"):
    return [
        ModelStreamEvent(
            kind="tool_call_completed",
            tool_call=ToolCall(id=call_id, name="write_once", arguments={"path": str(path)}),
        ),
        ModelStreamEvent(
            kind="message_completed",
            response=ModelResponse(message=ModelMessage(role="assistant")),
        ),
    ]


def _registry(executor, *, external: bool = True):
    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="write_once",
            read_only=not external,
            input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            effect_recovery=RuntimeEffectRecoveryCapability(
                external_side_effect=external,
                idempotency="not_idempotent" if external else "read_only",
                probe="manual" if external else "not_required",
                on_ambiguity="manual_disambiguation" if external else "safe_to_retry",
            ),
            output_semantics=ToolOutputSemantics(
                artifact_role="candidate", argument_path_fields=("path",),
            ) if external else ToolOutputSemantics(),
        ),
        executor,
    )
    return registry


def _request(root: Path, *, run_id: str = "turn-1"):
    request = _runtime_input()
    return request.model_copy(update={
        "request": request.request.model_copy(update={"workspace_dir": str(root), "run_id": run_id}),
        "metadata": {"state_workspace_dir": str(root)},
    })


def _runtime(root: Path, registry, scripts, **kwargs):
    client = ScriptedModelClient(scripts)
    return NativeLantaiRuntime(
        model_client_factory=lambda _: client,
        tool_registry=registry,
        effect_journal=RuntimeEffectJournal(root),
        artifact_effect_journal=RuntimeArtifactEffectJournal(root),
        **kwargs,
    )


def _latest(root: Path):
    effect = RuntimeEffectJournal(root).latest_effect("turn-1", "turn-1:native-attempt", "effect:write-1")
    assert effect is not None
    return effect


def _assert_ambiguous_write(root: Path):
    effect = _latest(root)
    assert effect.status == "ambiguous"
    assert effect.revision == 2
    assert effect.settled_at is not None
    assert effect.result_hash
    assert effect.ambiguity_reason
    assert effect.idempotency_key == "turn-1:turn-1:native-attempt:effect:write-1"
    artifacts = RuntimeArtifactEffectJournal(root).list_records(effect.run_id, effect.attempt_id)
    assert len(artifacts) == 2
    assert artifacts[-1].status == "ambiguous"
    assert artifacts[-1].settled_at is not None
    assert artifacts[-1].source_effect_id == effect.effect_id
    assert artifacts[-1].sha256 is None
    return effect


async def _assert_restart_blocks_replay(root: Path, registry, path: Path):
    resumed = _runtime(root, registry, [_call(path, "new-call"), [_completed("check the existing write", message_id="final")]])
    events = await asyncio.wait_for(
        _collect(resumed, _request(root, run_id="turn-2")), timeout=2,
    )
    completed = next(event for event in events if event.type == "tool.call.completed")
    assert completed.payload["metadata"]["automatic_retry_allowed"] is False
    assert completed.payload["metadata"]["requires_human"] is True
    assert completed.payload["canonical_tool"]["effect_id"] == "effect:write-1"
    assert path.read_bytes() == b"applied\n"
    _assert_ambiguous_write(root)


async def _collect(runtime, request, events=None):
    collected = events if events is not None else []
    async for event in runtime.run_turn(request):
        collected.append(event)
    return collected


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat_stop", [False, True])
async def test_cancelled_write_settles_after_cleanup_and_blocks_restart(tmp_path, repeat_stop):
    path = tmp_path / "artifact.txt"
    started, cleanup_started, cleanup_release, cleanup_done = (asyncio.Event() for _ in range(4))
    calls = []

    async def write_once(path):
        calls.append(path)
        with Path(path).open("ab") as handle:
            handle.write(b"applied\n")
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleanup_started.set()
            await cleanup_release.wait()
            cleanup_done.set()

    registry = _registry(write_once)
    runtime = _runtime(tmp_path, registry, [_call(path)])
    events = []
    task = asyncio.create_task(_collect(runtime, _request(tmp_path), events))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        await asyncio.wait_for(cleanup_started.wait(), timeout=2)
        if repeat_stop:
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert _latest(tmp_path).status == "running"
    finally:
        cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert cleanup_done.is_set()
    assert not any(event.type == "tool.call.completed" for event in events)
    _assert_ambiguous_write(tmp_path)
    await _assert_restart_blocks_replay(tmp_path, registry, path)
    assert calls == [str(path)]


@pytest.mark.asyncio
async def test_cancelled_sync_write_waits_for_worker_before_settlement(tmp_path):
    path = tmp_path / "artifact.txt"
    started, release, completed = (threading.Event() for _ in range(3))

    def write_once(path):
        with Path(path).open("ab") as handle:
            handle.write(b"applied\n")
        started.set()
        assert release.wait(timeout=3)
        completed.set()
        return "applied"

    registry = _registry(write_once)
    runtime = _runtime(tmp_path, registry, [_call(path)])
    task = asyncio.create_task(_collect(runtime, _request(tmp_path)))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert _latest(tmp_path).status == "running"
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert completed.is_set()
    _assert_ambiguous_write(tmp_path)
    await _assert_restart_blocks_replay(tmp_path, registry, path)


@pytest.mark.asyncio
async def test_closing_native_stream_before_dispatch_cancels_unused_intent(tmp_path):
    path = tmp_path / "artifact.txt"
    calls = []
    registry = _registry(lambda path: calls.append(path) or "applied")
    runtime = _runtime(tmp_path, registry, [_call(path)])
    stream = runtime.run_turn(_request(tmp_path))
    async for event in stream:
        if event.type == "tool.started":
            break
    await stream.aclose()
    assert calls == []
    assert not path.exists()
    effect = _latest(tmp_path)
    assert effect.status == "cancelled"
    assert effect.started_at is None
    assert effect.settled_at is not None
    assert effect.result_hash
    artifacts = RuntimeArtifactEffectJournal(tmp_path).list_records(effect.run_id, effect.attempt_id)
    assert artifacts[-1].status == "failed"
    assert artifacts[-1].settled_at is not None


@pytest.mark.asyncio
async def test_explicit_turn_deadline_settles_applied_write_before_failure(tmp_path):
    path = tmp_path / "artifact.txt"
    cleanup_done = asyncio.Event()

    async def write_once(path):
        with Path(path).open("ab") as handle:
            handle.write(b"applied\n")
        try:
            await asyncio.Future()
        finally:
            cleanup_done.set()

    registry = _registry(write_once)
    # Only this private harness opts into a short turn deadline.
    runtime = _runtime(tmp_path, registry, [_call(path)], turn_timeout_seconds=0.1)
    events = []
    with pytest.raises(_TurnDeadlineExceeded):
        await asyncio.wait_for(_collect(runtime, _request(tmp_path), events), timeout=2)
    assert cleanup_done.is_set()
    failed = next(event for event in events if event.type == "turn.failed")
    assert failed.payload["error_type"] == "_TurnDeadlineExceeded"
    assert not any(event.type == "tool.call.completed" for event in events)
    _assert_ambiguous_write(tmp_path)
    await _assert_restart_blocks_replay(tmp_path, registry, path)


@pytest.mark.asyncio
async def test_cancelled_read_only_call_has_no_ambiguous_external_effect(tmp_path):
    started = asyncio.Event()

    async def read_only(path):
        started.set()
        await asyncio.Future()

    runtime = _runtime(tmp_path, _registry(read_only, external=False), [_call(tmp_path / "unused")])
    task = asyncio.create_task(_collect(runtime, _request(tmp_path)))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    effect = _latest(tmp_path)
    assert effect.status == "failed"
    assert effect.settled_at is not None
    assert effect.result_hash
    assert effect.ambiguity_reason is None
    assert RuntimeArtifactEffectJournal(tmp_path).list_records(effect.run_id, effect.attempt_id) == []
