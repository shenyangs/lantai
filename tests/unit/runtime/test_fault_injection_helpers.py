# -*- coding: utf-8 -*-
"""Behavior locks for Runtime migration fault-injection helpers."""

from __future__ import annotations

import asyncio
import errno
import json

import pytest

from tests.support.runtime_faults import (
    AsyncRaceGate,
    FakeExternalEffect,
    JsonlFaultFactory,
    RuntimeSSEFaultStream,
    SSEDisconnect,
    StoreWriteFaultInjector,
)


def test_store_write_fault_injector_triggers_before_and_after_write() -> None:
    injector = StoreWriteFaultInjector()
    injector.fail_at("before", "runs.jsonl", occurrence=2, exc=RuntimeError("pre-crash"))
    injector.exit_at("after", "runs.jsonl", occurrence=1, code=99)

    injector.before_write("runs.jsonl")
    with pytest.raises(RuntimeError, match="pre-crash"):
        injector.before_write("runs.jsonl")

    with pytest.raises(SystemExit) as exit_info:
        injector.after_write("runs.jsonl")

    assert exit_info.value.code == 99
    assert injector.history == [
        ("before", "runs.jsonl", 1),
        ("before", "runs.jsonl", 2),
        ("after", "runs.jsonl", 1),
    ]


@pytest.mark.asyncio
async def test_async_race_gate_pauses_two_coroutines_until_released() -> None:
    gate = AsyncRaceGate(parties=2)
    arrivals: list[str] = []

    async def worker(label: str) -> str:
        arrivals.append(f"{label}:before")
        await gate.pause(label)
        arrivals.append(f"{label}:after")
        return label

    first = asyncio.create_task(worker("claim"))
    await gate.wait_for_arrivals(1)
    assert arrivals == ["claim:before"]
    assert not first.done()

    second = asyncio.create_task(worker("stop"))
    await gate.wait_for_arrivals(2)
    assert gate.arrivals == ("claim", "stop")
    assert not first.done()
    assert not second.done()

    gate.release()
    assert await asyncio.gather(first, second) == ["claim", "stop"]
    assert arrivals == ["claim:before", "stop:before", "claim:after", "stop:after"]


def test_runtime_sse_fault_stream_builds_disconnect_duplicate_reorder_and_late_terminal() -> None:
    stream = (
        RuntimeSSEFaultStream(
            [
                {"id": "evt-1", "seq": 1, "type": "turn.started"},
                {"id": "evt-2", "seq": 2, "type": "message.delta"},
            ]
        )
        .duplicate_event("evt-1")
        .reorder(["evt-2", "evt-1", "evt-1"])
        .disconnect_after(2)
        .late_terminal({"id": "evt-3", "seq": 3, "type": "turn.completed"})
    )

    frames = stream.frames()

    assert [getattr(frame, "event_id", "disconnect") for frame in frames] == [
        "evt-2",
        "evt-1",
        "disconnect",
        "evt-1",
        "evt-3",
    ]
    assert isinstance(frames[2], SSEDisconnect)
    assert frames[0].to_sse_text().startswith("event: runtime\n")
    assert json.loads(frames[0].to_sse_text().split("data: ", 1)[1])["events"][0]["id"] == "evt-2"


def test_jsonl_fault_factory_generates_corrupt_sequences_and_disk_full_error() -> None:
    factory = JsonlFaultFactory()
    events = [
        {"id": "evt-1", "seq": 1, "type": "turn.started"},
        {"id": "evt-2", "seq": 2, "type": "turn.completed"},
    ]

    assert factory.half_line(events[0]).startswith('{"id":')
    assert not factory.half_line(events[0]).endswith("\n")
    assert [json.loads(line)["seq"] for line in factory.duplicate_seq(events, duplicate_seq=1)] == [1, 1]
    assert [json.loads(line)["seq"] for line in factory.missing_seq(events, missing_seq=2)] == [1]
    assert factory.old_schema_event(kind="chat.turn.started", run_id="run-1") == {
        "kind": "chat.turn.started",
        "run_id": "run-1",
        "payload": {},
    }

    error = factory.disk_full_error("events.jsonl")
    assert isinstance(error, OSError)
    assert error.errno == errno.ENOSPC
    assert "events.jsonl" in str(error)


@pytest.mark.asyncio
async def test_fake_external_effect_counts_actual_executions() -> None:
    effect = FakeExternalEffect()

    assert await effect.execute("send-message", payload={"text": "a"}) == {
        "effect_id": "send-message",
        "execution": 1,
        "payload": {"text": "a"},
    }
    assert await effect.execute("send-message", payload={"text": "a"}) == {
        "effect_id": "send-message",
        "execution": 2,
        "payload": {"text": "a"},
    }
    assert await effect.execute("write-file") == {
        "effect_id": "write-file",
        "execution": 1,
        "payload": {},
    }

    assert effect.count("send-message") == 2
    assert effect.count("write-file") == 1
    assert effect.calls == [
        ("send-message", {"text": "a"}),
        ("send-message", {"text": "a"}),
        ("write-file", {}),
    ]
