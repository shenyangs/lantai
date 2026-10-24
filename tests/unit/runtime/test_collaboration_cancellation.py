"""R01: exercise cancellation through the production native turn/transaction."""

from __future__ import annotations

import asyncio
import gc
import threading
from pathlib import Path

import pytest

from lantai.runtime.adapters.native_runtime import NativeLantaiRuntime
from lantai.runtime.input import RuntimeInput, RuntimeRequestContext
from lantai.runtime.messages import RuntimeMessage
from lantai.runtime.models import ModelMessage, ModelResponse, ModelStreamEvent
from lantai.runtime.tool_response import ToolResponse
from lantai.runtime.tools import LantaiToolRegistry, ToolCall, ToolSpec
from lantai.runtime.turn_control import RuntimeTurnControlStore
from lantai.agents.tools import agent_management


class _Client:
    provider_id = "test"
    model_name = "test"

    def __init__(self, call: ToolCall | None) -> None:
        self.call = call
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        call, self.call = self.call, None
        if call is not None:
            yield ModelStreamEvent(kind="tool_call_completed", tool_call=call)
        yield ModelStreamEvent(
            kind="message_completed",
            response=ModelResponse(
                message=ModelMessage.text("assistant", "" if call else "new answer"),
            ),
        )


def _start(tmp_path, executor, *, progressive=False, attempt="old", call=True,
           timeout=None, on_event=None):
    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="collaborate_with_agents" if progressive else "chat_with_agent",
            metadata={"progressive_tool": "collaboration"} if progressive else {},
        ),
        executor,
    )
    client = _Client(
        ToolCall(
            id=f"call-{attempt}",
            name="collaborate_with_agents" if progressive else "chat_with_agent",
            arguments={},
        ) if call else None,
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        **({"tool_timeout_seconds": timeout} if timeout is not None else {}),
    )
    runtime_input = RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default", session_id="session", user_id="user",
            run_id=f"run-{attempt}", workspace_dir=str(tmp_path),
        ),
        messages=[RuntimeMessage.text("user", "test request")],
        metadata={"state_workspace_dir": str(tmp_path), "attempt_id": attempt},
    )
    events = []

    async def collect():
        async for event in runtime.run_turn(runtime_input):
            events.append(event)
            if on_event is not None:
                on_event(event)

    return asyncio.create_task(collect()), events, client


def _owned(attempt="old"):
    return {
        task for task in asyncio.all_tasks()
        if task.get_name() in {
            f"waitable-collaboration-call-{attempt}",
            f"collaboration-operation-call-{attempt}",
            f"collaboration-steer-watch-call-{attempt}",
            f"native-tool-heartbeat-call-{attempt}",
        }
    }


async def _dispose(task, owned):
    # Also reap leaks on the unfixed baseline; a failed assertion must not
    # contaminate later tests or leave a blocked event-loop shutdown.
    for child in {*owned, task}:
        if not child.done():
            child.cancel()
    await asyncio.gather(task, *owned, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("progressive", [False, True])
async def test_parent_stop_joins_execution_before_interrupt(tmp_path: Path, progressive):
    started, stopped = asyncio.Event(), asyncio.Event()
    late_release = asyncio.Event()
    existing = tmp_path / "existing.txt"
    existing.write_text("keep")

    async def execute():
        started.set()
        try:
            await late_release.wait()
            (tmp_path / "late-old-result.txt").write_text("old attempt")
            return "old result"
        finally:
            stopped.set()

    async def stream():
        result = await execute()
        yield ToolResponse(content=[], metadata={"result": result})

    task, events, _ = _start(tmp_path, stream if progressive else execute,
                             progressive=progressive)
    owned = set()
    try:
        await asyncio.wait_for(started.wait(), 2)
        owned = _owned()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert stopped.is_set(), "parent exited before owned execution stopped"
        assert all(child.done() for child in owned)
        assert events[-1].type == "turn.interrupted"
        assert not any(e.type == "tool.call.completed" for e in events)

        # A new attempt runs after stop; releasing the old I/O cannot write
        # a late result or alter the new attempt. This is local ownership,
        # not a claim about remote side effects or UI fencing.
        newer, new_events, _ = _start(tmp_path, execute, attempt="new", call=False)
        await asyncio.wait_for(newer, 2)
        late_release.set()
        await asyncio.sleep(0)
        assert new_events[-1].type == "turn.completed"
        assert all(e.turn_id == "run-new" for e in new_events)
        assert not (tmp_path / "late-old-result.txt").exists()
        assert existing.read_text() == "keep"
    finally:
        late_release.set()
        await _dispose(task, owned)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_kind", ["parent", "steer"])
async def test_repeated_stop_cannot_interrupt_child_cleanup(tmp_path: Path, stop_kind):
    started, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))

    async def execute():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            cleaned.set()

    task, events, client = _start(tmp_path, execute)
    owned = set()
    try:
        await asyncio.wait_for(started.wait(), 2)
        owned = _owned()
        if stop_kind == "parent":
            task.cancel()
        else:
            RuntimeTurnControlStore(tmp_path).append_steer(
                client_turn_id="steer", run_id="run-old", attempt_id="old",
                chat_id="chat", text="new direction", wakeup="immediate",
            )
        await asyncio.wait_for(cleaning.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "parent abandoned cleanup after repeated stop"
        assert not any(e.type == "turn.interrupted" for e in events)
        assert len(client.requests) == 1
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert cleaned.is_set(), "repeated parent cancellation killed cleanup"
        assert all(child.done() for child in owned)
        assert events[-1].type == "turn.interrupted"
    finally:
        release.set()
        await _dispose(task, owned)


@pytest.mark.asyncio
async def test_child_failure_is_visible_and_watcher_reaped(tmp_path: Path):
    async def execute():
        raise RuntimeError("injected child failure")

    task, events, _ = _start(tmp_path, execute)
    await asyncio.wait_for(task, 2)
    failures = [e for e in events if e.type == "tool.call.completed"]
    assert len(failures) == 1
    assert failures[0].payload["ok"] is False
    assert "injected child failure" in str(failures[0].payload)
    assert not _owned()


@pytest.mark.asyncio
async def test_steer_cleanup_failure_is_not_reported_as_success(tmp_path: Path):
    started = asyncio.Event()

    async def execute():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            raise RuntimeError("injected cleanup failure")

    task, events, _ = _start(tmp_path, execute)
    owned = set()
    try:
        await asyncio.wait_for(started.wait(), 2)
        owned = _owned()
        RuntimeTurnControlStore(tmp_path).append_steer(
            client_turn_id="steer", run_id="run-old", attempt_id="old",
            chat_id="chat", text="new direction", wakeup="immediate",
        )
        await asyncio.wait_for(task, 2)
        failure = next(e for e in events if e.type == "tool.call.completed")
        assert failure.payload["ok"] is False
        assert "injected cleanup failure" in str(failure.payload)
        assert all(child.done() for child in owned)
    finally:
        await _dispose(task, owned)


@pytest.mark.asyncio
async def test_timeout_reaps_cooperative_operation_before_result(tmp_path: Path):
    stopped = asyncio.Event()

    async def execute():
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            stopped.set()

    task, events, _ = _start(tmp_path, execute, timeout=0.02)
    await asyncio.wait_for(task, 2)
    assert stopped.is_set()
    failure = next(e for e in events if e.type == "tool.call.completed")
    assert failure.payload["error_code"] == "tool_timeout"
    assert not _owned()


@pytest.mark.asyncio
async def test_watcher_failure_remains_visible_and_stops_operation(tmp_path, monkeypatch):
    original = RuntimeTurnControlStore.read_admissible_inbox
    stopped = asyncio.Event()

    def read(store, *args, **kwargs):
        if asyncio.current_task().get_name().startswith("collaboration-steer-watch-"):
            raise OSError("injected inbox failure")
        return original(store, *args, **kwargs)

    monkeypatch.setattr(RuntimeTurnControlStore, "read_admissible_inbox", read)

    async def execute():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    task, events, _ = _start(tmp_path, execute)
    await asyncio.wait_for(task, 2)
    assert stopped.is_set()
    failure = next(e for e in events if e.type == "tool.call.completed")
    assert "injected inbox failure" in str(failure.payload)
    assert failure.payload["error_code"] == "tool_failed"
    assert not _owned()


@pytest.mark.asyncio
async def test_native_stop_drains_real_collaboration_members(tmp_path, monkeypatch):
    """Real batch tool/member cleanup; only the remote API boundary is fake."""
    monkeypatch.setattr(agent_management, "resolve_agent_api_base_url",
                        lambda *_: "http://isolated.invalid")
    monkeypatch.setattr(agent_management, "list_agents_data", lambda *_: {
        "agents": [{"id": name, "name": name, "enabled": True}
                   for name in ("agent_a", "agent_b")],
    })
    submitted, cancelled = [], []
    cleaning, release = threading.Event(), threading.Event()

    def submit(_base, _payload, to_agent, _timeout):
        submitted.append(to_agent)
        return {"task_id": f"task-{to_agent}"}

    def cancel(_base, task_id, **_kwargs):
        cleaning.set()
        assert release.wait(5), "test did not release remote cancellation"
        cancelled.append(task_id)
        return {"status": "cancelled"}

    monkeypatch.setattr(agent_management, "submit_agent_chat_task", submit)
    monkeypatch.setattr(agent_management, "get_agent_chat_task_status",
                        lambda *_args, **_kwargs: {"status": "running"})
    monkeypatch.setattr(agent_management, "cancel_agent_chat_task", cancel)

    def execute():
        return agent_management.collaborate_with_agents([
            {"to_agent": "agent_a", "text": "research", "label": "research"},
            {"to_agent": "agent_b", "text": "review", "label": "review"},
        ])

    task, events, _ = _start(tmp_path, execute, progressive=True)
    owned = set()
    try:
        async def wait_submitted():
            while len(submitted) < 2:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_submitted(), 2)

        async def wait_active_member_read():
            # Submission runs in worker threads. It can finish while the
            # progressive iterator is yielding an update and its per-read
            # operation/wait/watcher tasks are being replaced. This test
            # cancels an active read; the progress-yield gap is covered by
            # test_closing_turn_stream_at_progress_yield_closes_collaboration_iterator.
            # Synchronize on the real ownership boundary, not thread timing.
            while True:
                wrappers = _owned()
                members = {t for t in asyncio.all_tasks()
                           if t.get_name().startswith("collaboration-collab-")}
                if len(wrappers) == 4 and len(members) == 2:
                    return wrappers | members
                await asyncio.sleep(0)

        owned = await asyncio.wait_for(wait_active_member_read(), 2)
        assert len(owned) >= 5
        task.cancel()
        assert await asyncio.to_thread(cleaning.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert not any(e.type == "turn.interrupted" for e in events)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert sorted(cancelled) == ["task-agent_a", "task-agent_b"]
        assert all(t.done() for t in owned)
        assert events[-1].type == "turn.interrupted"
    finally:
        release.set()
        await _dispose(task, owned)


@pytest.mark.parametrize("cancel_close_during_cleanup", [False, True])
@pytest.mark.asyncio
async def test_closing_turn_stream_at_progress_yield_closes_collaboration_iterator(
    tmp_path: Path,
    cancel_close_during_cleanup: bool,
):
    """Closing the public stream must cross every suspended generator layer."""
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    closed = asyncio.Event()

    async def collaborate_with_agents():
        try:
            yield ToolResponse(
                content=[],
                metadata={
                    "collaboration": {
                        "batch_id": "batch-yield-gap",
                        "revision": 1,
                        "status": "running",
                        "members": [],
                    },
                },
                stream=True,
                is_last=False,
            )
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            if cancel_close_during_cleanup:
                await cleanup_release.wait()
            closed.set()

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="collaborate_with_agents",
            metadata={"progressive_tool": "collaboration"},
        ),
        collaborate_with_agents,
    )
    client = _Client(
        ToolCall(
            id="call-yield-gap",
            name="collaborate_with_agents",
            arguments={},
        )
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )
    runtime_input = RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id="session",
            user_id="user",
            run_id="run-yield-gap",
            workspace_dir=str(tmp_path),
        ),
        messages=[RuntimeMessage.text("user", "test request")],
        metadata={
            "state_workspace_dir": str(tmp_path),
            "attempt_id": "yield-gap",
        },
    )
    stream = runtime.run_turn(runtime_input)

    try:
        while True:
            event = await asyncio.wait_for(anext(stream), 2)
            if event.type == "tool.call.delta":
                break
        close_task = asyncio.create_task(
            stream.aclose(),
            name="yield-gap-public-stream-close",
        )
        if cancel_close_during_cleanup:
            await asyncio.wait_for(cleanup_started.wait(), 1)
            close_task.cancel()
            close_task.cancel()
            await asyncio.sleep(0)
            assert not close_task.done()
            cleanup_release.set()
        await asyncio.wait_for(close_task, 2)
        await asyncio.wait_for(closed.wait(), 1)
        assert not _owned("yield-gap")
    finally:
        cleanup_release.set()
        await stream.aclose()


@pytest.mark.asyncio
async def test_parent_stop_keeps_cleanup_failure_observable(tmp_path, caplog):
    started = asyncio.Event()

    async def execute():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            raise RuntimeError("injected parent-stop cleanup failure")

    task, events, _ = _start(tmp_path, execute)
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert "injected parent-stop cleanup failure" in caplog.text
    assert events[-1].type == "turn.interrupted"
    assert not any(e.type == "tool.call.completed" for e in events)
    assert not _owned()


@pytest.mark.asyncio
async def test_parent_stop_ignores_child_value_returned_after_cancel(tmp_path):
    started, returned = asyncio.Event(), asyncio.Event()

    async def execute():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            returned.set()
            return "late old value despite cancellation"

    task, events, _ = _start(tmp_path, execute)
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert returned.is_set()
    assert events[-1].type == "turn.interrupted"
    assert not any(e.type == "tool.call.completed" for e in events)
    assert not _owned()


@pytest.mark.asyncio
@pytest.mark.parametrize("progressive", [False, True])
@pytest.mark.parametrize("limit", [
    "expired_turn", "zero_tool_timeout", "expires_before_first_step",
])
async def test_exhausted_budget_never_starts_collaboration(
    tmp_path, monkeypatch, progressive, limit,
):
    from lantai.runtime.adapters.native_runtime import (
        _DeadlineBudget, _TurnDeadlineExceeded,
    )

    budget = _DeadlineBudget(None)
    monkeypatch.setattr(
        _DeadlineBudget, "from_runtime_input",
        classmethod(lambda _cls, *_args, **_kwargs: budget),
    )
    calls = []

    async def execute():
        calls.append("external operation started")
        return "result"

    async def stream():
        await execute()
        yield ToolResponse(content=[])

    def on_event(event):
        # Expire after admission/projection of tool.started, immediately
        # before the transaction attempts to schedule the execution body.
        if limit == "expired_turn" and event.type == "tool.started":
            budget.deadline_monotonic = 0
        elif limit == "expires_before_first_step" and event.type == "tool.started":
            asyncio.get_running_loop().call_soon(
                setattr, budget, "deadline_monotonic", 0,
            )

    shielded = []
    original_shield = asyncio.shield

    def shield(awaitable):
        future = original_shield(awaitable)
        shielded.append(future)
        return future

    monkeypatch.setattr(asyncio, "shield", shield)
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    unobserved = []
    loop.set_exception_handler(lambda _loop, error: unobserved.append(error))
    try:
        task, events, _ = _start(
            tmp_path, stream if progressive else execute, progressive=progressive,
            timeout=0 if limit == "zero_tool_timeout" else None, on_event=on_event,
        )
        if limit != "zero_tool_timeout":
            with pytest.raises(_TurnDeadlineExceeded):
                await asyncio.wait_for(task, 2)
        else:
            await asyncio.wait_for(task, 2)
        assert calls == [], "exhausted budget still allowed an external operation"
        assert not _owned()
        if limit == "zero_tool_timeout":
            result = next(e for e in events if e.type == "tool.call.completed")
            assert result.payload["error_code"] == "tool_timeout"

        del task
        await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)
        assert not unobserved, [error.get("message") for error in unobserved]
        # CPython defers the loop warning until finalization. Pytest's log
        # capture can retain exception frames past this test, so also inspect
        # the flag which triggers that warning without retrieving the error.
        assert not [f for f in shielded if getattr(f, "_log_traceback", False)]
    finally:
        for future in shielded:
            if future.done() and not future.cancelled():
                future.exception()
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_simultaneous_success_and_watcher_failure_is_logged(
    tmp_path, monkeypatch, caplog,
):
    original_read = RuntimeTurnControlStore.read_admissible_inbox
    original_wait = asyncio.wait

    def read(store, *args, **kwargs):
        if asyncio.current_task().get_name().startswith("collaboration-steer-watch-"):
            raise OSError("simultaneous watcher failure")
        return original_read(store, *args, **kwargs)

    async def wait(tasks, **kwargs):
        if all(t.get_name().startswith(("waitable-collaboration-",
                                       "collaboration-steer-watch-")) for t in tasks):
            # Deterministic scheduler interleaving: both finish before the
            # transaction resumes from wait(FIRST_COMPLETED).
            await asyncio.gather(*tasks, return_exceptions=True)
        return await original_wait(tasks, **kwargs)

    monkeypatch.setattr(RuntimeTurnControlStore, "read_admissible_inbox", read)
    monkeypatch.setattr(asyncio, "wait", wait)

    async def execute():
        return "completed before watcher failure was observed"

    task, events, _ = _start(tmp_path, execute)
    await asyncio.wait_for(task, 2)
    result = next(e for e in events if e.type == "tool.call.completed")
    assert result.payload["ok"] is True
    assert "simultaneous watcher failure" in caplog.text
    assert not _owned()
