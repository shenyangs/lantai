import asyncio
from types import SimpleNamespace

import pytest

from lantai.app.runner import task_tracker
from lantai.app.runner.api import _request_stop_for_chat_turn


class UncopyableReplay(list):
    def __iter__(self):
        raise AssertionError("control path copied the replay backlog")


@pytest.mark.asyncio
async def test_stop_does_not_copy_long_replay_backlog():
    registry = task_tracker.SessionRuntimeRegistry()
    state = task_tracker._RunState(
        task=asyncio.get_running_loop().create_future(),
        buffer=UncopyableReplay(["synthetic"] * 100000),
        attempt_id="attempt",
        generation=1,
    )
    registry._runs["chat"] = state
    try:
        stopped, run_id = await _request_stop_for_chat_turn(
            workspace=SimpleNamespace(task_tracker=registry),
            chat_spec=SimpleNamespace(id="chat", meta={}),
            turn_id="attempt",
        )
        assert stopped and run_id == "attempt"
    finally:
        state.task.cancel()
        await registry._finish_run("chat", state, producer_exited=True)


@pytest.mark.asyncio
async def test_metadata_snapshot_preserves_identity_and_terminal_visibility():
    registry = task_tracker.SessionRuntimeRegistry()
    state = task_tracker._RunState(
        task=asyncio.get_running_loop().create_future(),
        buffer=UncopyableReplay(["synthetic"] * 100000),
        attempt_id="attempt",
        generation=7,
        request_id="request",
        root_session_id="root",
    )
    registry._runs["chat"] = state
    try:
        snapshot = await registry.get_control_snapshot("chat")
        assert snapshot["event_count"] == 100000
        assert "events" not in snapshot
        assert snapshot["attempt_id"] == "attempt"
        assert snapshot["generation"] == 7
        assert snapshot["request_id"] == "request"
        assert snapshot["root_session_id"] == "root"
        state.terminal_status = "completed"
        assert await registry.get_control_snapshot("chat") is None
        state.stop_requested = True
        assert (await registry.get_control_snapshot("chat"))[
            "status"
        ] == "stopping"
    finally:
        state.task.cancel()
        await registry._finish_run("chat", state, producer_exited=True)
