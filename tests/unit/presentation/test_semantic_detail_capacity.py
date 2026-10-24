"""Reasoning and strict detail JSON share one bounded provider output window."""

from __future__ import annotations

import pytest

from lantai.app.structured_utility import StructuredUtilityResult
from lantai.app.utility_model import UtilityTaskIncompleteError
from lantai.presentation.intelligence import UtilityPresentationSemanticPlanner
from lantai.presentation.models import InputMaterial, PresentationJobRequest
from lantai.presentation.planner import build_brief, build_deterministic_fixture_plan
from tests.unit.presentation.test_page_authoring import _semantic_value_for_call


def _fixture(slide_count):
    request = PresentationJobRequest(
        conversation_id="detail-reasoning-capacity",
        user_request="只用材料制作判断",
        slide_count=slide_count,
        materials=[InputMaterial(name="材料.md", content="# 判断\n甲为40万元。")],
    )
    brief = build_brief(request)
    base = build_deterministic_fixture_plan(request, brief)
    plan = base.model_copy(update={"slides": [
        base.slides[0].model_copy(update={"order": order})
        for order in range(1, slide_count + 1)
    ]})
    return request, brief, plan


@pytest.mark.parametrize("batch_size", [1, 2, 3, 4])
def test_public_planner_leaves_detail_json_room_after_observed_reasoning(
    monkeypatch, batch_size,
):
    """A capacity boundary, not a claim that provider reasoning is predictable."""
    request, brief, plan = _fixture(batch_size)
    calls = []

    async def model_boundary(**kwargs):
        calls.append(kwargs)
        if str(kwargs["purpose"]).startswith("presentation-semantic-detail:"):
            # D05's failed stream spent 1521 completion tokens on reasoning.
            # Reserve a separate 2048-token first-page structured JSON window.
            required = 1_521 + 2_048 + (batch_size - 1) * 500
            if kwargs["max_output_tokens"] < required:
                raise UtilityTaskIncompleteError(finish_reason="length")
        return StructuredUtilityResult(
            value=_semantic_value_for_call(kwargs, plan),
            model_label="approved-provider/approved-model", repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        model_boundary,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1", detail_batch_size=batch_size)
    result = planner.plan(request, brief)

    assert [slide.order for slide in result.slides] == list(range(1, batch_size + 1))
    assert len(calls) == planner.last_utility_call_count == 3
    assert planner.last_stage_attempt_counts == {
        "claims": 1, "outline": 1,
        f"detail:001-{batch_size:03d}": 1,
    }
    assert calls[-1]["max_output_tokens"] <= 8_192
    assert calls[:2][0]["max_output_tokens"] == 4_096 + batch_size * 400
    assert calls[:2][1]["max_output_tokens"] == 4_096 + batch_size * 400
    assert planner.last_output_token_budget == sum(c["max_output_tokens"] for c in calls)
    assert planner.estimated_cost_units(request) == 150


def test_larger_detail_window_still_rejects_provider_truncation_without_retry(
    monkeypatch,
):
    request, brief, plan = _fixture(1)
    purposes = []

    async def model_boundary(**kwargs):
        purposes.append(kwargs["purpose"])
        if str(kwargs["purpose"]).startswith("presentation-semantic-detail:"):
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
        "presentation-semantic-detail:001-001",
    ]
    assert planner.last_stage_attempt_counts["detail:001-001"] == 1
