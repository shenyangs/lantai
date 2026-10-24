# -*- coding: utf-8 -*-
"""Candidate-based slide art direction above the native compiler.

The compiler intentionally knows nothing about style identities or page
meaning.  This module is the narrow bridge where a semantic slide intent,
portable design policy, deck rhythm and curated design priors become several
fully positioned native scenes.  The scenes are rendered and critiqued before
one is selected; a candidate is never selected by block count alone.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Callable, Iterable, Sequence

from .models import (
    DeckSlide,
    DeckVisualPlan,
    PresentationDesignContext,
    SlideIntent,
)


@dataclass(frozen=True)
class ArtDirectionBrief:
    """Inputs available to a composition candidate builder."""

    intent: SlideIntent
    design_context: PresentationDesignContext
    visual_plan: DeckVisualPlan | None
    reference_exemplars: tuple[str, ...]
    preceding_composition_families: tuple[str, ...]


@dataclass(frozen=True)
class CompositionCandidate:
    """One renderable, explicit native scene proposed by the art director."""

    candidate_id: str
    composition_family: str
    slide: DeckSlide
    rationale: str
    visual_anchor_object_ids: tuple[str, ...]
    density: str
    content_compression: str


CandidateBuilder = Callable[[ArtDirectionBrief], CompositionCandidate]


class SlideArtDirector:
    """Generate and validate meaning-led alternatives for one slide.

    Curated builders are design priors, not layout templates: the caller
    supplies them after considering the slide relationship, deck rhythm and
    selected StyleProfile.  Every builder must emit final object geometry.
    """

    def propose(
        self,
        *,
        intent: SlideIntent,
        design_context: PresentationDesignContext,
        candidate_builders: Sequence[CandidateBuilder],
        visual_plan: DeckVisualPlan | None = None,
        reference_exemplars: Iterable[str] = (),
        preceding_composition_families: Iterable[str] = (),
    ) -> tuple[CompositionCandidate, ...]:
        if not candidate_builders:
            raise ValueError("art direction requires at least one candidate builder")
        brief = ArtDirectionBrief(
            intent=intent,
            design_context=design_context,
            visual_plan=visual_plan,
            reference_exemplars=tuple(reference_exemplars),
            preceding_composition_families=tuple(
                preceding_composition_families,
            ),
        )
        candidates = tuple(builder(brief) for builder in candidate_builders)
        self._validate_candidates(candidates)
        return candidates

    @staticmethod
    def geometry_fingerprint(candidate: CompositionCandidate) -> str:
        """Describe actual composition, independent of copy and colors."""

        payload = [
            {
                "kind": obj.kind,
                "role": getattr(obj, "role", ""),
                "slot": getattr(obj, "slot", ""),
                "frame": obj.frame.model_dump(mode="json") if obj.frame else None,
            }
            for obj in candidate.slide.objects
        ]
        return hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        ).hexdigest()

    def _validate_candidates(
        self,
        candidates: Sequence[CompositionCandidate],
    ) -> None:
        identifiers = [candidate.candidate_id for candidate in candidates]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("art direction candidate ids must be unique")
        fingerprints: set[str] = set()
        for candidate in candidates:
            if not candidate.visual_anchor_object_ids:
                raise ValueError(
                    f"candidate {candidate.candidate_id} has no visual anchor",
                )
            object_ids = {obj.id for obj in candidate.slide.objects}
            missing_anchors = set(candidate.visual_anchor_object_ids) - object_ids
            if missing_anchors:
                raise ValueError(
                    f"candidate {candidate.candidate_id} references unknown anchors",
                )
            if any(obj.frame is None for obj in candidate.slide.objects):
                raise ValueError(
                    f"candidate {candidate.candidate_id} is not explicitly composed",
                )
            fingerprint = self.geometry_fingerprint(candidate)
            if fingerprint in fingerprints:
                raise ValueError(
                    "art direction candidates must differ in composition, not only copy or color",
                )
            fingerprints.add(fingerprint)
