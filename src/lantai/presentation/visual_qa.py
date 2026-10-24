"""Semantic visual-review boundary for rendered presentation evidence.

The renderer proves that pixels exist.  A critic is a separate component that
judges whether those pixels communicate the slide intent.  Keeping the two
contracts separate prevents a successful PNG export from being mislabeled as
visual-quality approval.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from .design_intelligence import constraint_levels_by_object
from .models import (
    DeckIR,
    DeckPlan,
    PresentationBrief,
    VisualCriticResult,
    VisualCriticSlideInput,
)
from .quality import file_sha256


@runtime_checkable
class PresentationVisualCritic(Protocol):
    def review(
        self,
        slides: list[VisualCriticSlideInput],
    ) -> VisualCriticResult:
        """Return structured semantic issues for the exact rendered images."""


def build_visual_critic_inputs(
    deck: DeckIR,
    rendered_paths: tuple[Path, ...] | list[Path],
    *,
    plan: DeckPlan | None = None,
    brief: PresentationBrief | None = None,
    original_user_request: str = "",
    usage_session_id: str | None = None,
) -> list[VisualCriticSlideInput]:
    intents_by_id = (
        {item.intent.id: item.intent for item in plan.slides}
        if plan is not None
        else {}
    )
    deck_context = (
        [
            f"Narrative arc: {plan.narrative_arc}",
            *[
                f"{item.order}. {item.intent.key_message}"
                for item in sorted(plan.slides, key=lambda item: item.order)
            ],
        ]
        if plan is not None
        else [slide.speaker_notes or slide.id for slide in deck.slides]
    )
    design_context = deck.design_context or (
        plan.design_context if plan is not None else None
    )
    deck_constraints = list(dict.fromkeys([
        *deck.required_facts, *deck.immutable_data,
        *(brief.required_facts if brief is not None else []),
        *(brief.immutable_data if brief is not None else []),
    ]))
    inputs: list[VisualCriticSlideInput] = []
    for index, (slide, rendered_path) in enumerate(
        zip(deck.slides, rendered_paths, strict=False),
        start=1,
    ):
        intent = intents_by_id.get(slide.intent_id or "")
        page_constraints = list(dict.fromkeys([
            *(intent.required_facts if intent is not None else []),
            *(intent.immutable_facts if intent is not None else []),
        ]))
        constraints = list(dict.fromkeys([*deck_constraints, *page_constraints]))
        object_ids = [obj.id for obj in slide.objects]
        inputs.append(
            VisualCriticSlideInput(
                slide_id=slide.id,
                slide_index=index,
                rendered_path=str(rendered_path),
                rendered_sha256=file_sha256(rendered_path),
                usage_session_id=usage_session_id,
                intent=intent,
                key_message=(
                    intent.key_message
                    if intent is not None
                    else slide.speaker_notes
                ),
                speaker_notes=slide.speaker_notes,
                factual_constraints=constraints,
                deck_factual_constraints=deck_constraints,
                page_factual_constraints=page_constraints,
                object_ids=object_ids,
                full_deck_context=deck_context,
                original_user_request=original_user_request,
                source_brief=brief,
                source_facts=deck.facts,
                slide_objects=slide.objects,
                object_relationships=slide.relationships,
                design_profile=(
                    design_context.profile if design_context is not None else None
                ),
                design_context=design_context,
                template_mode=(
                    design_context.profile.template_mode
                    if design_context is not None
                    else deck.design_profile.template_mode
                    if deck.design_profile is not None
                    else "brand_preserve_optimize"
                ),
                constraint_levels_by_object=(
                    constraint_levels_by_object(design_context, object_ids)
                    if design_context is not None
                    else {}
                ),
            )
        )
    return inputs
