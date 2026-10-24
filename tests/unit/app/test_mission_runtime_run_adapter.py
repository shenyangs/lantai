# -*- coding: utf-8 -*-
"""Behavior locks for Mission RuntimeRunStore adapter writes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from lantai.agents.mission.handler import handle_mission_command
from lantai.agents.mission.mission_runner import (
    _stream_mission_turn,
    run_mission_phase2,
)
from lantai.agents.mission import state as mission_state
from lantai.agents.mission.state import (
    create_loop_dir,
    init_progress_txt,
    write_loop_config,
    write_prd_json,
    write_task_md,
)
from lantai.runtime.store import RuntimeRunStore


def test_mission_loop_dir_creation_avoids_same_second_collisions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(mission_state, "_ts", lambda: "20260716-010203")

    first = mission_state.create_loop_dir(tmp_path)
    second = mission_state.create_loop_dir(tmp_path)

    assert first != second
    assert first.name == "mission-20260716-010203"
    assert second.name.startswith("mission-20260716-010203-")
    assert first.exists()
    assert second.exists()


class _FakeToolkit:
    def __init__(self) -> None:
        self.groups: dict[str, Any] = {}
        self.tools: dict[str, Any] = {}
        self.updated: list[tuple[list[str], bool]] = []

    def create_tool_group(self, name: str, *, description: str, active: bool) -> None:
        self.groups[name] = {"description": description, "active": active}

    def update_tool_groups(self, groups: list[str], *, active: bool) -> None:
        self.updated.append((groups, active))
        for group in groups:
            self.groups.setdefault(group, {})["active"] = active


class _FakeAgent:
    def __init__(self) -> None:
        self.toolkit = _FakeToolkit()


@pytest.mark.asyncio
async def test_mission_turn_fails_closed_without_native_streamer() -> None:
    with pytest.raises(RuntimeError, match="requires Native stream_turn wiring"):
        _ = [
            item
            async for item in _stream_mission_turn(
                agent=_FakeAgent(),
                msgs=[],
                stream_turn=None,
            )
        ]


async def _empty_stream(_msgs):
    if False:
        yield None, False  # pragma: no cover


@pytest.mark.asyncio
async def test_mission_command_writes_runtime_run_contract(tmp_path: Path) -> None:
    rewritten: list[str] = []

    def rewrite(_msgs, prompt: str) -> None:
        rewritten.append(prompt)

    result = await handle_mission_command(
        "/mission build a runtime migration dashboard",
        [],
        tmp_path,
        "agent-default",
        rewrite,
        session_id="session-mission-1",
    )

    loop_dir = Path(result["loop_dir"])  # type: ignore[index]
    contract = await RuntimeRunStore(tmp_path).load_contract(loop_dir.name)

    assert rewritten
    assert contract is not None
    assert contract.run_id == loop_dir.name
    assert contract.attempt_id == f"{loop_dir.name}:mission-attempt-1"
    assert contract.source == "mission"
    assert contract.status == "running"
    assert contract.revision == 2
    assert contract.owner.agent_id == "agent-default"
    assert contract.owner.session_id == "session-mission-1"
    assert contract.owner.workspace_id == str(tmp_path)
    assert contract.contract_hash.startswith("sha256:")


@pytest.mark.asyncio
async def test_active_mission_resumes_only_through_explicit_continue(
    tmp_path: Path,
) -> None:
    loop_dir = create_loop_dir(tmp_path)
    write_loop_config(
        loop_dir,
        {
            "workspace_dir": str(tmp_path),
            "session_id": "session-mission-continue",
            "current_phase": "execution_confirmed",
            "max_iterations": 7,
        },
    )

    result = await handle_mission_command(
        "/mission continue",
        [],
        tmp_path,
        "agent-default",
        lambda _msgs, _prompt: None,
        session_id="session-mission-continue",
    )

    assert result == {
        "mission_phase": 2,
        "loop_dir": str(loop_dir),
        "max_iterations": 7,
    }


@pytest.mark.asyncio
async def test_mission_phase2_completion_settles_runtime_run_contract(
    tmp_path: Path,
) -> None:
    loop_dir = create_loop_dir(tmp_path)
    write_task_md(loop_dir, "build a runtime migration dashboard")
    init_progress_txt(loop_dir)
    write_loop_config(
        loop_dir,
        {
            "workspace_dir": str(tmp_path),
            "session_id": "session-mission-1",
            "current_phase": "execution_confirmed",
        },
    )
    write_prd_json(
        loop_dir,
        {
            "userStories": [
                {
                    "id": "US-1",
                    "title": "Done",
                    "description": "Already complete",
                    "acceptanceCriteria": ["passes"],
                    "priority": "P0",
                    "passes": True,
                }
            ]
        },
    )

    messages = [
        msg
        async for msg, _last in run_mission_phase2(
            agent=_FakeAgent(),
            msgs=[],
            loop_dir=loop_dir,
            max_iterations=1,
            agent_id="agent-default",
            stream_turn=_empty_stream,
        )
    ]
    contract = await RuntimeRunStore(tmp_path).load_contract(loop_dir.name)

    assert messages
    assert contract is not None
    assert contract.status == "succeeded"
    assert contract.revision == 3
    assert contract.settled_at is not None


@pytest.mark.asyncio
async def test_phase_one_yield_cancel_reaches_native_turn(tmp_path):
    import asyncio

    from lantai.agents.mission.mission_runner import run_mission_phase1
    from lantai.utils.async_iterator import DelegatedAsyncIterator

    trace = []

    async def native():
        try:
            yield "partial", False
            trace.append("unwanted-effect")
        except asyncio.CancelledError:
            trace.append("interrupted")
            yield "interrupted", True
            raise
        finally:
            trace.append("closed")

    inner = native()

    def stream_turn(msgs):
        return inner

    phase = run_mission_phase1(object(), [], tmp_path, stream_turn=stream_turn)

    async def runner_bridge():
        async with DelegatedAsyncIterator(phase) as events:
            async for item in events:
                try:
                    yield item
                except asyncio.CancelledError as exc:
                    events.request_cancel(exc)

    outer = runner_bridge()
    try:
        assert await anext(outer) == ("partial", False)
        try:
            result = await outer.athrow(asyncio.CancelledError())
        except asyncio.CancelledError:
            result = None
        assert trace == [
            "interrupted"
        ], f"Native interruption was lost: {trace}, outer={result}"
        assert result == ("interrupted", True)
        with pytest.raises(asyncio.CancelledError):
            await anext(outer)
        assert trace == ["interrupted", "closed"]
    finally:
        await outer.aclose()
        await phase.aclose()
        await inner.aclose()
