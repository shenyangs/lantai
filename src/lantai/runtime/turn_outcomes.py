# -*- coding: utf-8 -*-
"""Explicit semantic delivery outcome values for one chat turn."""

from __future__ import annotations

from typing import Literal


TurnOutcomeStatus = Literal[
    "delivered",
    "incomplete",
    "needs_input",
    "blocked",
    "unknown",
]

TURN_OUTCOME_STATUSES = frozenset(
    {"delivered", "incomplete", "needs_input", "blocked", "unknown"},
)


def normalize_turn_outcome_status(value: object) -> TurnOutcomeStatus:
    """Return a stable, forward-compatible delivery outcome."""

    normalized = str(value or "").strip().lower()
    if normalized in TURN_OUTCOME_STATUSES:
        return normalized  # type: ignore[return-value]
    return "unknown"
