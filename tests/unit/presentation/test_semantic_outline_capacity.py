"""Deck-wide reasoning and strict outline JSON share one output window."""

from __future__ import annotations

import pytest

from lantai.app.structured_utility import StructuredUtilityResult
from lantai.app.utility_model import UtilityTaskIncompleteError
from lantai.presentation.intelligence import UtilityPresentationSemanticPlanner
from tests.unit.presentation.test_page_authoring import _semantic_value_for_call
from tests.unit.presentation.test_semantic_detail_capacity import _fixture


def test_public_planner_leaves_outline_json_room_after_observed_reasoning(
    monkeypatch,
):
    """Reproduce the transport boundary, not the provider's private content."""
    request, brief, plan = _fixture(10)
    calls = []

    async def model_boundary(**kwargs):
        calls.append(kwargs)
        if kwargs["purpose"] == "presentation-semantic-outline":
            # D03 consumed 4605 of a 5024-token completion on reasoning.
            # Its strict ten-page outline still needs a separate JSON window.
            if kwargs["max_output_tokens"] < 4_605 + 2_048:
                raise UtilityTaskIncompleteError(finish_reason="length")
        return StructuredUtilityResult(
            value=_semantic_value_for_call(kwargs, plan),
            model_label="approved-provider/approved-model", repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        model_boundary,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    result = planner.plan(request, brief)

    assert [slide.order for slide in result.slides] == list(range(1, 11))
    outline_calls = [
        call for call in calls
        if call["purpose"] == "presentation-semantic-outline"
    ]
    assert len(outline_calls) == 1
    assert 4_605 + 2_048 <= outline_calls[0]["max_output_tokens"] <= 8_192
    assert calls[0]["max_output_tokens"] == 8_096
    assert len(calls) == planner.last_utility_call_count == 12
    assert planner.last_stage_attempt_counts["outline"] == 1
    assert planner.last_output_token_budget == sum(
        call["max_output_tokens"] for call in calls
    )
    assert planner.estimated_cost_units(request) == 600


def test_larger_outline_window_still_rejects_truncation_without_blind_retry(
    monkeypatch,
):
    request, brief, plan = _fixture(10)
    purposes = []

    async def model_boundary(**kwargs):
        purposes.append(kwargs["purpose"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise UtilityTaskIncompleteError(finish_reason="length")
        return StructuredUtilityResult(
            value=_semantic_value_for_call(kwargs, plan),
            model_label="approved-provider/approved-model", repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        model_boundary,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(UtilityTaskIncompleteError, match="incomplete: length"):
        planner.plan(request, brief)
    assert purposes == [
        "presentation-semantic-claims", "presentation-semantic-outline",
    ]
    assert planner.last_stage_attempt_counts == {"claims": 1, "outline": 1}
