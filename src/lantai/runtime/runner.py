# -*- coding: utf-8 -*-
"""Small facade over a Lantai RuntimeAdapter."""

from __future__ import annotations

from lantai.utils.async_iterator import DelegatedAsyncIterator

import asyncio

from collections.abc import Callable
from contextlib import aclosing
from typing import Any, AsyncIterator, cast

from .adapter import RuntimeAdapter
from .events import RuntimeEvent
from .input import RuntimeInput
from .request_policy_receipt import (
    EffectivePolicyObservationInvocation,
    EffectivePolicyObservationToken,
    claim_effective_policy_observation,
    mark_effective_policy_observation_exhausted,
)


DiagnosticRunTurn = Callable[
    [RuntimeInput, EffectivePolicyObservationInvocation],
    AsyncIterator[RuntimeEvent],
]


class RuntimeRunner:
    """Application-facing facade for runtime execution."""

    def __init__(
        self,
        adapter: RuntimeAdapter,
        *,
        diagnostic_run_turn: DiagnosticRunTurn | None = None,
        request_policy_observation_token: object | None = None,
    ) -> None:
        self._adapter = adapter
        invocation = (
            claim_effective_policy_observation(
                request_policy_observation_token
            )
            if type(request_policy_observation_token)
            is EffectivePolicyObservationToken
            and diagnostic_run_turn is not None
            else None
        )
        self._diagnostic_run_turn = (
            diagnostic_run_turn if invocation is not None else None
        )
        self.request_policy_observation_invocation = invocation

    async def run_turn(
        self,
        runtime_input: RuntimeInput,
    ) -> AsyncIterator[RuntimeEvent]:
        """Delegate one turn to the configured adapter."""

        invocation = self.request_policy_observation_invocation
        diagnostic_run_turn = self._diagnostic_run_turn
        if invocation is not None and diagnostic_run_turn is not None:
            self.request_policy_observation_invocation = None
            self._diagnostic_run_turn = None
            last_event: RuntimeEvent | None = None
            async with aclosing(
                cast(Any, diagnostic_run_turn(runtime_input, invocation))
            ) as diagnostic_events:
                async with DelegatedAsyncIterator(
                    diagnostic_events
                ) as _owned_events_1:
                    async for event in _owned_events_1:
                        last_event = event
                        try:
                            yield event
                        except asyncio.CancelledError as _stream_cancel:
                            _owned_events_1.request_cancel(_stream_cancel)
            if last_event is not None:
                try:
                    mark_effective_policy_observation_exhausted(
                        invocation,
                        terminal_type=last_event.type,
                        terminal_turn_id=last_event.turn_id or "",
                    )
                except Exception:
                    pass
            return

        async with DelegatedAsyncIterator(
            self._adapter.run_turn(runtime_input)
        ) as _owned_events_2:
            async for event in _owned_events_2:
                try:
                    yield event
                except asyncio.CancelledError as _stream_cancel:
                    _owned_events_2.request_cancel(_stream_cancel)
