# -*- coding: utf-8 -*-
"""Fault-injection helpers for Runtime migration tests.

These helpers deliberately live under ``tests``. They model crash, race,
stream, JSONL, and side-effect conditions without changing production code.
"""

from __future__ import annotations

import asyncio
import errno
import json
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


WritePhase = Literal["before", "after"]


class StoreWriteFaultInjector:
    """Inject failures around append-only store write checkpoints."""

    def __init__(self) -> None:
        self._counts: dict[tuple[WritePhase, str], int] = defaultdict(int)
        self._actions: dict[tuple[WritePhase, str, int], BaseException] = {}
        self.history: list[tuple[WritePhase, str, int]] = []

    def fail_at(
        self,
        phase: WritePhase,
        label: str,
        *,
        occurrence: int,
        exc: BaseException,
    ) -> None:
        self._validate_occurrence(occurrence)
        self._actions[(phase, label, occurrence)] = exc

    def exit_at(
        self,
        phase: WritePhase,
        label: str,
        *,
        occurrence: int,
        code: int = 1,
    ) -> None:
        self.fail_at(phase, label, occurrence=occurrence, exc=SystemExit(code))

    def before_write(self, label: str) -> None:
        self._checkpoint("before", label)

    def after_write(self, label: str) -> None:
        self._checkpoint("after", label)

    def _checkpoint(self, phase: WritePhase, label: str) -> None:
        key = (phase, label)
        self._counts[key] += 1
        occurrence = self._counts[key]
        self.history.append((phase, label, occurrence))

        action = self._actions.get((phase, label, occurrence))
        if action is not None:
            raise action

    @staticmethod
    def _validate_occurrence(occurrence: int) -> None:
        if occurrence < 1:
            raise ValueError("occurrence must be >= 1")


class AsyncRaceGate:
    """Pause async workers at the same checkpoint until a test releases them."""

    def __init__(self, *, parties: int = 2) -> None:
        if parties < 1:
            raise ValueError("parties must be >= 1")
        self.parties = parties
        self._arrivals: list[str] = []
        self._condition: asyncio.Condition | None = None
        self._release = asyncio.Event()

    @property
    def arrivals(self) -> tuple[str, ...]:
        return tuple(self._arrivals)

    async def pause(self, label: str) -> None:
        condition = self._get_condition()
        async with condition:
            self._arrivals.append(label)
            condition.notify_all()
        await self._release.wait()

    async def wait_for_arrivals(self, count: int) -> None:
        if count < 0:
            raise ValueError("count must be >= 0")
        condition = self._get_condition()
        async with condition:
            await condition.wait_for(lambda: len(self._arrivals) >= count)

    def release(self) -> None:
        self._release.set()

    def _get_condition(self) -> asyncio.Condition:
        if self._condition is None:
            self._condition = asyncio.Condition()
        return self._condition


@dataclass(frozen=True)
class RuntimeSSEFrame:
    event: dict[str, Any]

    @property
    def event_id(self) -> str:
        return str(self.event.get("id", ""))

    def to_sse_text(self) -> str:
        payload = json.dumps({"events": [self.event]}, ensure_ascii=False)
        return f"event: runtime\ndata: {payload}\n\n"


@dataclass(frozen=True)
class SSEDisconnect:
    reason: str = "disconnect"


class RuntimeSSEFaultStream:
    """Build runtime SSE frame streams with common transport faults."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = [dict(event) for event in events]
        self._disconnect_after: int | None = None

    def duplicate_event(self, event_id: str) -> RuntimeSSEFaultStream:
        for event in self._events:
            if event.get("id") == event_id:
                self._events.append(dict(event))
                return self
        raise KeyError(f"event not found: {event_id}")

    def reorder(self, event_ids: list[str]) -> RuntimeSSEFaultStream:
        by_id: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
        for event in self._events:
            by_id[str(event.get("id", ""))].append(event)

        reordered: list[dict[str, Any]] = []
        consumed: set[int] = set()
        for event_id in event_ids:
            if not by_id[event_id]:
                raise KeyError(f"event not found for reorder: {event_id}")
            event = by_id[event_id].popleft()
            consumed.add(id(event))
            reordered.append(event)

        for event in self._events:
            if id(event) not in consumed:
                reordered.append(event)

        self._events = reordered
        return self

    def disconnect_after(self, event_count: int) -> RuntimeSSEFaultStream:
        if event_count < 0:
            raise ValueError("event_count must be >= 0")
        self._disconnect_after = event_count
        return self

    def late_terminal(self, event: dict[str, Any]) -> RuntimeSSEFaultStream:
        self._events.append(dict(event))
        return self

    def frames(self) -> list[RuntimeSSEFrame | SSEDisconnect]:
        frames: list[RuntimeSSEFrame | SSEDisconnect] = []
        for index, event in enumerate(self._events, start=1):
            if self._disconnect_after is not None and index == self._disconnect_after + 1:
                frames.append(SSEDisconnect())
            frames.append(RuntimeSSEFrame(event))

        if self._disconnect_after == len(self._events):
            frames.append(SSEDisconnect())
        return frames


class JsonlFaultFactory:
    """Create JSONL corruption and storage-failure fixtures."""

    def half_line(self, event: dict[str, Any]) -> str:
        text = self._json_line(event).rstrip("\n")
        return text[: max(1, len(text) // 2)]

    def duplicate_seq(
        self,
        events: list[dict[str, Any]],
        *,
        duplicate_seq: int,
    ) -> list[str]:
        return [self._json_line({**event, "seq": duplicate_seq}) for event in events]

    def missing_seq(
        self,
        events: list[dict[str, Any]],
        *,
        missing_seq: int,
    ) -> list[str]:
        return [self._json_line(event) for event in events if event.get("seq") != missing_seq]

    def old_schema_event(
        self,
        *,
        kind: str,
        run_id: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "kind": kind,
            "run_id": run_id,
            "payload": dict(payload or {}),
        }

    def disk_full_error(self, path: str | Path) -> OSError:
        return OSError(errno.ENOSPC, "No space left on device", str(path))

    @staticmethod
    def _json_line(event: dict[str, Any]) -> str:
        return json.dumps(event, ensure_ascii=False) + "\n"


class FakeExternalEffect:
    """Count real side-effect attempts without performing external work."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = defaultdict(int)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(
        self,
        effect_id: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload_copy = dict(payload or {})
        self._counts[effect_id] += 1
        self.calls.append((effect_id, payload_copy))
        return {
            "effect_id": effect_id,
            "execution": self._counts[effect_id],
            "payload": payload_copy,
        }

    def count(self, effect_id: str) -> int:
        return self._counts[effect_id]
