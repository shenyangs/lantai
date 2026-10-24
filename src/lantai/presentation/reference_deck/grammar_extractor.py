# -*- coding: utf-8 -*-
"""Separate universal design evidence from style and deck-specific choices."""

from __future__ import annotations

from collections import Counter
from statistics import mean

from ..models import QualityBand, TemplateQualityProfile
from .models import (
    ExtractedDesignGrammar,
    ReferenceDeckMeasurement,
    ReferenceVisualMeasurement,
)


class DesignGrammarExtractor:
    """Describe why a reference works without turning its pages into templates."""

    def extract(
        self,
        deck: ReferenceDeckMeasurement,
        visual: ReferenceVisualMeasurement,
        quality: TemplateQualityProfile,
    ) -> ExtractedDesignGrammar:
        universal: list[str] = [
            "understand the information relationship before composing the page",
            "allocate visual weight intentionally instead of making every element equal",
            "preserve content truth and use editable native objects for essential information",
        ]
        if quality.information_hierarchy in {QualityBand.STRONG, QualityBand.ADEQUATE}:
            universal.append("establish a clear hierarchy and one dominant visual anchor")
        if quality.grid_consistency in {QualityBand.STRONG, QualityBand.ADEQUATE}:
            universal.append("use stable alignment rails to organize density before adding containers")
        if quality.visual_balance in {QualityBand.STRONG, QualityBand.ADEQUATE}:
            universal.append("use whitespace as active information structure")
        if quality.visual_rhythm in {QualityBand.STRONG, QualityBand.ADEQUATE}:
            universal.append("vary page composition and density while retaining one deck-level language")

        objects = [item for slide in deck.slides for item in slide.objects]
        images = sum(item.kind == "image" for item in objects)
        charts = sum(item.kind == "chart" for item in objects)
        text = sum(item.kind in {"text", "title"} for item in objects)
        line_share = sum(item.kind == "line" for item in objects) / max(len(objects), 1)
        mean_saturation = mean(
            (item.mean_saturation for item in visual.slides),
        ) if visual.slides else 0.0

        style_traits: list[str] = []
        if text and not images:
            style_traits.append("typography-first rather than image-dependent")
        if mean_saturation <= 0.18:
            style_traits.append("restrained low-saturation palette")
        if line_share >= 0.08:
            style_traits.append("fine rules act as editorial rails rather than decorative frames")
        if quality.grid_consistency == QualityBand.STRONG:
            style_traits.append("strict repeated alignment rails")
        if quality.brand_consistency in {QualityBand.STRONG, QualityBand.ADEQUATE}:
            style_traits.append("quiet repeated header or footer identity system")
        if charts == 0:
            style_traits.append("numbers and evidence are frequently expressed through type and spatial grouping")
        if max(Counter(visual.density_sequence).values(), default=0) < len(visual.density_sequence):
            style_traits.append("density changes are used as part of the narrative rhythm")

        deck_choices: list[str] = []
        if visual.slides:
            low = [str(item.slide_number) for item in visual.slides if item.visual_density == "low"]
            high = [str(item.slide_number) for item in visual.slides if item.visual_density == "high"]
            if low:
                deck_choices.append(f"low-density breathing pages occur at slide(s) {', '.join(low)}")
            if high:
                deck_choices.append(f"high-density evidence pages occur at slide(s) {', '.join(high)}")
        signatures = Counter(
            (
                slide.text_object_count,
                slide.image_count,
                slide.chart_count,
                slide.density_band,
            )
            for slide in deck.slides
        )
        deck_choices.append(
            f"this deck uses {len(signatures)} observed object-density signatures across {deck.slide_count} slides",
        )
        deck_choices.append(
            "individual object coordinates and element counts remain deck-specific observations and are not reusable composition rules",
        )
        return ExtractedDesignGrammar(
            universal_principles=list(dict.fromkeys(universal)),
            style_specific_traits=list(dict.fromkeys(style_traits)),
            deck_specific_decisions=deck_choices,
        )
