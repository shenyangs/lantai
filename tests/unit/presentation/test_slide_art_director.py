from __future__ import annotations

import pytest

from lantai.presentation.design_intelligence import resolve_design_context
from lantai.presentation.models import (
    DeckSlide,
    ObjectFrame,
    PresentationBrief,
    PresentationJobRequest,
    RelationshipType,
    SlideIntent,
    TextObject,
)
from lantai.presentation.slide_art_director import (
    CompositionCandidate,
    SlideArtDirector,
)


def _intent() -> SlideIntent:
    return SlideIntent(
        id="intent-thesis",
        purpose="establish the operating thesis",
        key_message="任务重构决定 AI 的组织价值",
        relationship_type=RelationshipType.THESIS,
    )


def _context():
    return resolve_design_context(
        PresentationJobRequest(
            conversation_id="art-direction-test",
            user_request="为管理层解释企业 AI 的下一阶段",
            audience="management",
            slide_count=1,
        ),
        PresentationBrief(
            objective="解释企业 AI 的下一阶段",
            audience="management",
            narrative="strategy review",
        ),
    )


def _candidate(candidate_id: str, *, x: float) -> CompositionCandidate:
    anchor_id = f"{candidate_id}-anchor"
    return CompositionCandidate(
        candidate_id=candidate_id,
        composition_family="editorial-thesis",
        slide=DeckSlide(
            id=f"{candidate_id}-slide",
            layout_id="explicit-composition",
            intent_id="intent-thesis",
            objects=[
                TextObject(
                    id=anchor_id,
                    role="title",
                    text="任务重构决定 AI 的组织价值",
                    font_size=40,
                    frame=ObjectFrame(x=x, y=1, w=7, h=1.2),
                ),
            ],
        ),
        rationale="Use a decisive typographic anchor.",
        visual_anchor_object_ids=(anchor_id,),
        density="low",
        content_compression="one claim",
    )


def test_art_director_requires_genuinely_different_geometry() -> None:
    context = _context()
    director = SlideArtDirector()

    with pytest.raises(ValueError, match="differ in composition"):
        director.propose(
            intent=_intent(),
            design_context=context,
            candidate_builders=[
                lambda _brief: _candidate("a", x=0.8),
                lambda _brief: _candidate("b", x=0.8),
            ],
        )


def test_art_director_emits_explicit_renderable_scenes() -> None:
    context = _context()
    candidates = SlideArtDirector().propose(
        intent=_intent(),
        design_context=context,
        candidate_builders=[
            lambda _brief: _candidate("a", x=0.8),
            lambda _brief: _candidate("b", x=4.8),
        ],
    )

    assert len(candidates) == 2
    assert all(obj.frame is not None for item in candidates for obj in item.slide.objects)
    assert len(
        {SlideArtDirector.geometry_fingerprint(item) for item in candidates},
    ) == 2
