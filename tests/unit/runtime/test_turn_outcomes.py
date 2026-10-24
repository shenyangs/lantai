# -*- coding: utf-8 -*-
"""Behavior locks for explicit turn-outcome normalization."""

from lantai.runtime.turn_outcomes import normalize_turn_outcome_status


def test_turn_outcome_only_accepts_explicit_known_values() -> None:
    assert normalize_turn_outcome_status("delivered") == "delivered"
    assert normalize_turn_outcome_status("INCOMPLETE") == "incomplete"
    assert normalize_turn_outcome_status("future_action_without_delivery") == "unknown"
    assert normalize_turn_outcome_status(None) == "unknown"
