# -*- coding: utf-8 -*-
"""Deterministic routing for proactive, user-specific observations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


InterventionRoute = Literal["direct", "ququ", "opportunity", "silent"]


def _bounded(value: float) -> float:
    return max(0.0, min(float(value), 1.0))


@dataclass(frozen=True)
class InterventionCandidate:
    kind: str
    content: str
    relevance: float
    urgency: float
    expected_utility: float
    confidence: float
    relationship_resonance: float
    novelty: float
    intrusion_cost: float
    sensitivity: str = "normal"
    essential: bool = False

    @property
    def value_score(self) -> float:
        score = (
            0.30 * _bounded(self.relevance)
            + 0.20 * _bounded(self.urgency)
            + 0.20 * _bounded(self.expected_utility)
            + 0.15 * _bounded(self.confidence)
            + 0.10 * _bounded(self.relationship_resonance)
            + 0.05 * _bounded(self.novelty)
            - _bounded(self.intrusion_cost)
        )
        if self.sensitivity != "normal":
            score -= 0.15
        return max(-1.0, min(score, 1.0))


@dataclass(frozen=True)
class InterventionDecision:
    route: InterventionRoute
    score: float
    reason: str


def route_intervention(
    candidate: InterventionCandidate,
) -> InterventionDecision:
    """Choose the least intrusive channel that preserves useful initiative."""

    score = candidate.value_score
    if candidate.essential or candidate.urgency >= 0.75 or score >= 0.70:
        return InterventionDecision("direct", score, "important_now")
    if candidate.kind in {"revisit", "recommendation", "future_follow_up"}:
        if score >= 0.30 and candidate.urgency < 0.45:
            return InterventionDecision("opportunity", score, "valuable_later")
    if (
        score >= 0.42
        and candidate.confidence >= 0.65
        and candidate.intrusion_cost <= 0.35
    ):
        return InterventionDecision(
            "ququ",
            score,
            "valuable_without_interrupting",
        )
    return InterventionDecision("silent", score, "insufficient_value")

