"""Source claims and reasoning share the same bounded provider completion."""

from __future__ import annotations

import pytest

from lantai.app.structured_utility import StructuredUtilityResult
from lantai.app.utility_model import UtilityTaskIncompleteError
from lantai.presentation.intelligence import (
    UtilityPresentationSemanticPlanner, _semantic_claim_output_token_budget,
)
from tests.unit.presentation.test_page_authoring import _semantic_value_for_call
from tests.unit.presentation.test_semantic_detail_capacity import _fixture


def test_public_planner_reserves_claim_json_room_after_observed_reasoning(
    monkeypatch,
):
    request, brief, plan = _fixture(10)
    calls = []

    async def model_boundary(**kwargs):
        calls.append(kwargs)
        if kwargs['purpose'] == 'presentation-semantic-claims':
            # D03 consumed 3297 reasoning tokens in a 5024 completion window
            # and truncated its source/proof JSON. A separate bounded JSON
            # reserve is necessary; this does not predict future reasoning.
            if kwargs['max_output_tokens'] < 3_297 + 3_072:
                raise UtilityTaskIncompleteError(finish_reason='length')
        return StructuredUtilityResult(
            value=_semantic_value_for_call(kwargs, plan),
            model_label='approved-provider/approved-model', repaired=False,
        )

    monkeypatch.setattr(
        'lantai.presentation.intelligence.run_structured_utility_task',
        model_boundary,
    )
    planner = UtilityPresentationSemanticPlanner('agent-1')
    result = planner.plan(request, brief)

    assert [slide.order for slide in result.slides] == list(range(1, 11))
    assert calls[0]['purpose'] == 'presentation-semantic-claims'
    assert 3_297 + 3_072 <= calls[0]['max_output_tokens'] <= 8_192
    assert calls[1]['max_output_tokens'] == 8_096
    assert len(calls) == planner.last_utility_call_count == 12
    assert planner.last_stage_attempt_counts['claims'] == 1
    assert planner.last_output_token_budget == sum(c['max_output_tokens'] for c in calls)
    assert planner.estimated_cost_units(request) == 600


def test_larger_claim_window_still_blocks_truncation_without_blind_retry(
    monkeypatch,
):
    request, brief, _plan = _fixture(10)
    purposes = []

    async def model_boundary(**kwargs):
        purposes.append(kwargs['purpose'])
        raise UtilityTaskIncompleteError(finish_reason='length')

    monkeypatch.setattr(
        'lantai.presentation.intelligence.run_structured_utility_task',
        model_boundary,
    )
    planner = UtilityPresentationSemanticPlanner('agent-1')
    with pytest.raises(UtilityTaskIncompleteError, match='incomplete: length'):
        planner.plan(request, brief)
    assert purposes == ['presentation-semantic-claims']
    assert planner.last_stage_attempt_counts == {'claims': 1}


@pytest.mark.parametrize('pages', [1, 10, 19, 64])
def test_claim_completion_window_keeps_original_hard_ceiling(pages):
    assert 0 < _semantic_claim_output_token_budget(pages) <= 8_192
