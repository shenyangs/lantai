# -*- coding: utf-8 -*-
"""Deterministic design measurements for the clean-room v3 composer.

This module does not decide slide meaning or choose a layout.  It turns an
already-designed semantic scene into explicit, reviewable typography metrics
and validates the data contract behind native charts.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from dataclasses import dataclass

from .assets import resolve_deck_assets
from .models import ChartObject, DeckIR, DesignQualityFloor, TextObject


@dataclass(frozen=True)
class TextMeasurement:
    weighted_characters: float
    estimated_lines: int
    estimated_height: float
    overflow_risk: bool


def _character_weight(character: str) -> float:
    if character in {"\n", "\r"}:
        return 0.0
    if unicodedata.east_asian_width(character) in {"W", "F"}:
        return 1.0
    if character.isspace():
        return 0.32
    return 0.55


def measure_text(
    text: str,
    *,
    font_size: float,
    frame_width: float,
    frame_height: float,
) -> TextMeasurement:
    """Estimate CJK/Latin line use in PowerPoint inch coordinates."""

    weighted = sum(_character_weight(character) for character in text)
    # At 18pt, one CJK glyph is roughly 0.25in wide.  The estimate is
    # deliberately conservative because actual fonts and punctuation vary.
    characters_per_line = max(1.0, frame_width / (font_size / 72.0))
    explicit_lines = max(1, text.count("\n") + 1)
    estimated_lines = max(
        explicit_lines,
        int(math.ceil(weighted / characters_per_line)),
    )
    line_height = font_size / 72.0 * 1.24
    estimated_height = estimated_lines * line_height
    return TextMeasurement(
        weighted_characters=weighted,
        estimated_lines=estimated_lines,
        estimated_height=estimated_height,
        overflow_risk=estimated_height > frame_height,
    )


def _font_candidates(
    obj: TextObject,
    density: str,
    quality_floor: DesignQualityFloor,
    *,
    title_floor: float,
) -> tuple[float, ...]:
    if obj.role == "title" or obj.slot == "title":
        base = {"low": 38.0, "medium": 34.0, "high": 30.0}[density]
        resolved = max(base, title_floor)
        return (resolved, resolved, resolved)
    if obj.role in {"eyebrow", "caption", "footer"}:
        floor = quality_floor.minimum_caption_font_size
        return (max(18.0, floor), max(17.0, floor), floor)
    base = {"low": 24.0, "medium": 21.0, "high": 18.0}[density]
    floor = quality_floor.minimum_body_font_size
    return (max(base, floor), max(base - 2, floor), floor)


def _fit_font_size(
    obj: TextObject,
    density: str,
    quality_floor: DesignQualityFloor,
    *,
    title_floor: float,
) -> float:
    if obj.font_size is not None:
        minimum = (
            quality_floor.minimum_caption_font_size
            if obj.role in {"eyebrow", "caption", "footer"}
            else quality_floor.minimum_body_font_size
        )
        return (
            max(obj.font_size, title_floor)
            if obj.role == "title" or obj.slot == "title"
            else max(obj.font_size, minimum)
        )
    candidates = _font_candidates(
        obj,
        density,
        quality_floor,
        title_floor=title_floor,
    )
    if obj.frame is None:
        return candidates[0]
    for candidate in candidates:
        measurement = measure_text(
            obj.text,
            font_size=candidate,
            frame_width=obj.frame.w,
            frame_height=obj.frame.h,
        )
        if not measurement.overflow_risk:
            return candidate
    return candidates[-1]


def apply_design_measurements(deck: DeckIR) -> DeckIR:
    """Bind explicit typography and chart semantics before composition."""

    context = deck.design_context
    quality_floor = (
        context.profile.quality_floor
        if context is not None
        else (
            deck.design_profile.quality_floor
            if deck.design_profile is not None
            else DesignQualityFloor()
        )
    )
    profile_density = (
        context.profile.style_profile.density
        if context is not None
        else (
            deck.design_profile.style_profile.density
            if deck.design_profile is not None
            else "adaptive"
        )
    )
    density = (
        profile_density
        if profile_density != "adaptive"
        else (
            deck.visual_direction.density
            if deck.visual_direction is not None
            else "medium"
        )
    )
    slides = []
    for slide_index, slide in enumerate(deck.slides):
        # The readability floor is semantic, not stylistic.  A deck's opening
        # statement must remain legible even when the selected profile is
        # unusually dense; subsequent slide titles use the standard floor.
        title_floor = 42.0 if slide_index == 0 else 32.0
        objects = []
        for obj in slide.objects:
            if isinstance(obj, TextObject):
                objects.append(
                    obj.model_copy(
                        update={
                            "font_size": _fit_font_size(
                                obj,
                                density,
                                quality_floor,
                                title_floor=title_floor,
                            ),
                        },
                    ),
                )
                continue
            if isinstance(obj, ChartObject):
                if not obj.derivation.strip():
                    raise ValueError(
                        f"native chart {obj.id} requires a derivation statement",
                    )
                if not obj.source_refs:
                    raise ValueError(
                        f"native chart {obj.id} requires source references",
                    )
                if not obj.unit.strip():
                    raise ValueError(f"native chart {obj.id} requires a unit")
            objects.append(obj)
        measured_slide = slide.model_copy(
            update={"objects": objects, "scene_hash": None},
        )
        scene_hash = hashlib.sha256(
            json.dumps(
                measured_slide.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        ).hexdigest()
        slides.append(
            measured_slide.model_copy(update={"scene_hash": scene_hash}),
        )
    return deck.model_copy(update={"slides": slides})


def prepare_deck_for_composition(deck: DeckIR) -> DeckIR:
    """Resolve audited assets and bind deterministic design measurements."""

    return apply_design_measurements(resolve_deck_assets(deck))
