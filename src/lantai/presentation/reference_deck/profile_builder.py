# -*- coding: utf-8 -*-
"""Build the one PresentationDesignContext consumed by every design stage."""

from __future__ import annotations

from ..design_intelligence import ColorStrategyEngine
from ..models import (
    ConstraintLevel,
    DesignQualityFloor,
    PresentationDesignContext,
    PresentationDesignProfile,
    TemplateMode,
    TemplateQualityProfile,
)
from .grammar_extractor import DesignGrammarExtractor
from .models import (
    ConstraintClassification,
    ReferenceDeckMeasurement,
    ReferenceLearningResult,
    ReferenceVisualMeasurement,
)
from .quality_arbitration import TemplateQualityArbitrator
from .style_extractor import ReferenceStyleExtractor


class ReferenceProfileBuilder:
    """Arbitrate, selectively inherit, then construct a portable profile."""

    def build(
        self,
        deck: ReferenceDeckMeasurement,
        visual: ReferenceVisualMeasurement,
        *,
        profile_id: str,
        template_mode: TemplateMode = TemplateMode.BRAND_PRESERVE_OPTIMIZE,
        explicit_hard_constraints: tuple[str, ...] = (),
        quality_floor: DesignQualityFloor | None = None,
        generated_for_request: str = "",
        variation_seed: int = 0,
        render_paths: tuple[str, ...] = (),
    ) -> ReferenceLearningResult:
        floor = quality_floor or DesignQualityFloor()
        arbitrator = TemplateQualityArbitrator()
        quality = arbitrator.analyze_quality(deck, visual, floor)
        classified = arbitrator.classify_constraints(
            deck,
            visual,
            quality,
            template_mode=template_mode,
            explicit_hard_constraints=explicit_hard_constraints,
            quality_floor=floor,
        )
        grammar = DesignGrammarExtractor().extract(deck, visual, quality)
        style = ReferenceStyleExtractor().extract(
            deck,
            visual,
            classified,
            grammar,
            profile_id=profile_id,
            quality_floor=floor,
            variation_seed=variation_seed,
        )
        constraints = classified.constraints
        color_profile = ColorStrategyEngine().resolve(
            style=style,
            user_request=generated_for_request,
            constraints=constraints,
            quality=quality,
            template_mode=template_mode,
            reference_palette=visual.dominant_palette,
            seed=variation_seed,
        )
        by_level = {
            level: [item for item in constraints if item.level == level]
            for level in ConstraintLevel
        }
        brand = [
            item
            for item in constraints
            if item.category in {"logo", "palette", "typography", "user_declared"}
            and item.level != ConstraintLevel.REJECT
        ]
        masters = [
            item
            for item in constraints
            if item.category in {"page_size", "repeated_edge_system"}
            and item.level != ConstraintLevel.REJECT
        ]
        if template_mode == TemplateMode.STRICT:
            flexibility = (
                "Preserve all supplied template geometry and identity constraints; "
                "repair only technical defects such as overflow, missing assets, corruption or out-of-bounds content."
            )
        elif template_mode == TemplateMode.STYLE_REFERENCE:
            flexibility = (
                "Transfer selected design language and quality strengths; dynamically recompose every page from semantic relationships."
            )
        else:
            flexibility = (
                "Preserve mandatory identity and master signals while modernizing hierarchy, grid, spacing, composition and rhythm."
            )
        profile = PresentationDesignProfile(
            identity=f"reference:{profile_id}:{deck.source_sha256[:12]}",
            source="reference",
            template_mode=template_mode,
            brand_constraints=brand,
            master_constraints=masters,
            hard_constraints=by_level[ConstraintLevel.HARD],
            medium_constraints=by_level[ConstraintLevel.MEDIUM],
            soft_preferences=by_level[ConstraintLevel.SOFT],
            rejected_traits=by_level[ConstraintLevel.REJECT],
            style_profile=style,
            color_profile=color_profile,
            design_grammar=grammar.universal_principles,
            preferred_compositions=style.preferred_compositions,
            forbidden_patterns=list(dict.fromkeys([*style.anti_patterns, *floor.forbidden_patterns])),
            reference_examples=[deck.source_name],
            template_quality_profile=quality,
            quality_floor=floor,
            flexibility_policy=flexibility,
        )
        context = PresentationDesignContext(
            profile=profile,
            reference_input_present=True,
            reference_artifact_id=f"sha256:{deck.source_sha256}",
            generated_for_request=generated_for_request,
        )
        notes = [
            "The source deck is not bundled; the result contains measurements and abstract design policy only.",
            "Source-page coordinates remain analysis evidence and are not composition templates.",
        ]
        if template_mode == TemplateMode.STRICT:
            notes.append("REJECT findings are diagnostics only in STRICT mode and do not authorize aesthetic redesign.")
        return ReferenceLearningResult(
            measurement=deck,
            visual_measurement=visual,
            template_quality_profile=quality,
            constraint_classification=classified,
            extracted_grammar=grammar,
            style_profile=style,
            design_context=context,
            render_paths=list(render_paths),
            notes=notes,
        )
