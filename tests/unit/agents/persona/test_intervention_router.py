# -*- coding: utf-8 -*-
"""Behavior contracts for routing proactive thoughts without random noise."""

from lantai.agents.persona.intervention_router import (
    InterventionCandidate,
    route_intervention,
)


def test_medium_value_personal_observation_routes_to_ququ_instead_of_interrupting():
    decision = route_intervention(
        InterventionCandidate(
            kind="personal_observation",
            content="这个展处理旧物的方式可能会让用户想起正在做的产品。",
            relevance=0.82,
            urgency=0.12,
            expected_utility=0.66,
            confidence=0.76,
            relationship_resonance=0.72,
            novelty=0.80,
            intrusion_cost=0.18,
        ),
    )

    assert decision.route == "ququ"
    assert decision.reason == "valuable_without_interrupting"


def test_low_value_thought_is_silent_even_when_ququ_is_enabled():
    decision = route_intervention(
        InterventionCandidate(
            kind="generic_reaction",
            content="随便说一句关心。",
            relevance=0.22,
            urgency=0.05,
            expected_utility=0.18,
            confidence=0.40,
            relationship_resonance=0.20,
            novelty=0.10,
            intrusion_cost=0.30,
        ),
    )

    assert decision.route == "silent"

