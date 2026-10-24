# -*- coding: utf-8 -*-
"""Shared state transitions for progressively exposed runtime tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class ProgressiveToolTransition:
    """Result of one progressive-tool loader call."""

    active_groups: frozenset[str]
    newly_activated_groups: frozenset[str]
    changed: bool
    cleared: bool


def transition_progressive_tool_groups(
    current_groups: set[str] | frozenset[str],
    selections: Mapping[str, bool],
    *,
    available_groups: set[str] | frozenset[str],
) -> ProgressiveToolTransition:
    """Apply one loader call using cumulative, turn-scoped semantics.

    A call containing one or more true fields adds those groups to the
    current set. False fields are ignored in that case because some model
    providers materialize every optional boolean with its default value.
    A call with no true fields is the explicit clear operation.
    """

    invalid = {
        name
        for name, value in selections.items()
        if name not in available_groups or not isinstance(value, bool)
    }
    if invalid:
        raise ValueError(
            "Invalid progressive tool groups: " + ", ".join(sorted(invalid)),
        )

    previous = frozenset(current_groups)
    requested = frozenset(name for name, value in selections.items() if value)
    active = previous | requested if requested else frozenset()
    return ProgressiveToolTransition(
        active_groups=active,
        newly_activated_groups=active - previous,
        changed=active != previous,
        cleared=not requested,
    )
