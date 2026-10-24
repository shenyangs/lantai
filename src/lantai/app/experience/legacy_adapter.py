# -*- coding: utf-8 -*-
"""Read-only compatibility adapter for legacy chat experience events."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

from lantai.runtime.events import RuntimeEvent

from .events import _event_to_runtime_contract
from .schemas import ChatExperienceEvent


class LegacyExperienceEventAdapter:
    """Convert persisted legacy experience rows to canonical RuntimeEvents.

    This adapter is intentionally read-only. It does not implement RuntimeAdapter
    and cannot execute a turn, tool, browser action, shell command, or delivery.
    """

    side_effect_free: Literal[True] = True

    def to_runtime_event(
        self,
        raw: Mapping[str, object] | ChatExperienceEvent,
    ) -> RuntimeEvent:
        event = (
            raw
            if isinstance(raw, ChatExperienceEvent)
            else ChatExperienceEvent.model_validate(raw)
        )
        return _event_to_runtime_contract(event)

    def to_runtime_events(
        self,
        rows: Sequence[Mapping[str, object] | ChatExperienceEvent],
    ) -> list[RuntimeEvent]:
        return [self.to_runtime_event(row) for row in rows]


def legacy_chat_event_to_runtime_event(
    raw: Mapping[str, object] | ChatExperienceEvent,
) -> RuntimeEvent:
    """Convert one legacy chat event row to a validated RuntimeEvent."""

    return LegacyExperienceEventAdapter().to_runtime_event(raw)
