# -*- coding: utf-8 -*-
"""Extract portable style DNA after template quality arbitration."""

from __future__ import annotations

from collections import Counter
from statistics import mean

from ..models import (
    ConstraintLevel,
    DesignQualityFloor,
    ReferenceDeckStyleProfile,
    RelationshipType,
)
from .models import (
    ConstraintClassification,
    ExtractedDesignGrammar,
    ReferenceDeckMeasurement,
    ReferenceObjectMeasurement,
    ReferenceVisualMeasurement,
)


def _objects(deck: ReferenceDeckMeasurement) -> list[ReferenceObjectMeasurement]:
    return [item for slide in deck.slides for item in slide.objects]


def _valid_hex(value: str) -> str | None:
    normalized = value.lstrip("#").upper()
    if len(normalized) == 6 and all(character in "0123456789ABCDEF" for character in normalized):
        return normalized
    return None


def _luminance(color: str) -> float:
    rgb = [int(color[index : index + 2], 16) / 255 for index in (0, 2, 4)]
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def _saturation(color: str) -> float:
    values = [int(color[index : index + 2], 16) / 255 for index in (0, 2, 4)]
    maximum, minimum = max(values), min(values)
    return 0.0 if maximum == 0 else (maximum - minimum) / maximum


def _percentile(values: list[float], ratio: float, fallback: float) -> float:
    if not values:
        return fallback
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * ratio)))
    return float(ordered[index])


def _dominant_font(objects: list[ReferenceObjectMeasurement], deck: ReferenceDeckMeasurement) -> str:
    candidates = Counter(font for item in objects for font in item.font_families)
    for value in deck.theme_fonts.values():
        if value:
            candidates[value] += 1
    return candidates.most_common(1)[0][0] if candidates else "Noto Sans SC"


def _estimated_margins(deck: ReferenceDeckMeasurement) -> dict[str, float]:
    objects = [item for item in _objects(deck) if item.width > 0 and item.height > 0]
    if not objects:
        return {"left": 0.72, "right": 0.72, "top": 0.45, "bottom": 0.45}
    lefts = [item.x for item in objects if item.x > 0.03]
    rights = [deck.slide_width - item.x - item.width for item in objects if deck.slide_width - item.x - item.width > 0.03]
    tops = [item.y for item in objects if item.y > 0.03]
    bottoms = [deck.slide_height - item.y - item.height for item in objects if deck.slide_height - item.y - item.height > 0.03]
    # Reference decks frequently contain many internal columns, so a median or
    # ordinary percentile measures content placement rather than the page rail.
    # The outermost non-bleed object is a better signal, bounded by a functional
    # minimum so an accidental off-canvas ornament does not become a design token.
    def outer(values: list[float], fallback: float, maximum: float) -> float:
        return max(0.2, min(maximum, _percentile(values, 0.0, fallback)))

    return {
        "left": round(outer(lefts, 0.72, 1.1), 3),
        "right": round(outer(rights, 0.72, 1.1), 3),
        "top": round(outer(tops, 0.45, 0.8), 3),
        "bottom": round(outer(bottoms, 0.45, 0.8), 3),
    }


def _spacing_scale(deck: ReferenceDeckMeasurement) -> list[float]:
    gaps: list[float] = []
    for slide in deck.slides:
        ordered = sorted(slide.top_edge_clusters)
        gaps.extend(b - a for a, b in zip(ordered, ordered[1:]) if 0.04 <= b - a <= 1.6)
    if not gaps:
        return [0.12, 0.2, 0.32, 0.52, 0.84]
    return [round(_percentile(gaps, ratio, fallback), 3) for ratio, fallback in (
        (0.1, 0.12), (0.3, 0.2), (0.5, 0.32), (0.7, 0.52), (0.9, 0.84),
    )]


class ReferenceStyleExtractor:
    """Build a data-only profile; compiler branches are intentionally absent."""

    def extract(
        self,
        deck: ReferenceDeckMeasurement,
        visual: ReferenceVisualMeasurement,
        constraints: ConstraintClassification,
        grammar: ExtractedDesignGrammar,
        *,
        profile_id: str,
        quality_floor: DesignQualityFloor | None = None,
        variation_seed: int = 0,
    ) -> ReferenceDeckStyleProfile:
        floor = quality_floor or DesignQualityFloor()
        objects = _objects(deck)
        identity_objects = [
            item for slide in deck.slides for item in [*slide.objects, *slide.inherited_objects]
        ]
        font = _dominant_font(objects, deck)
        sizes = [size for item in objects for size in item.font_sizes]
        observed_body = _percentile(sizes, 0.55, floor.minimum_body_font_size)
        observed_title = _percentile(sizes, 0.95, max(32, observed_body * 1.8))
        body_size = max(float(floor.minimum_body_font_size), min(28.0, observed_body))
        # New content must preserve the observed hierarchy without inheriting a
        # tiny source body scale.  A reference's exact point sizes are evidence,
        # not permission to violate the Lantai readability floor.
        title_size = max(32.0, min(54.0, max(observed_title, body_size * 1.8)))

        explicit = Counter(
            color
            for item in identity_objects
            for raw in [*item.text_colors, *item.fill_colors, *item.line_colors]
            if (color := _valid_hex(raw))
        )
        backgrounds = Counter(
            color
            for slide in deck.slides
            for raw in slide.background_colors
            if (color := _valid_hex(raw))
        )
        visual_colors = [
            color
            for raw in visual.dominant_palette
            if (color := _valid_hex(raw))
        ]
        actual_colors = list(dict.fromkeys(
            [*backgrounds.keys(), *(color for color, _count in explicit.most_common(16)), *visual_colors],
        ))
        if not actual_colors:
            actual_colors = ["F4F4F0", "141414", "757575", "3D3D3D", "E6E6E0"]

        # Theme XML often contains unused Office defaults (blue hyperlinks,
        # purple followed-hyperlinks, etc.).  Only colors observed in objects,
        # inherited backgrounds or rendered pixels may enter the learned DNA.
        canvas = backgrounds.most_common(1)[0][0] if backgrounds else max(actual_colors, key=_luminance)
        non_canvas = [color for color in actual_colors if color != canvas]
        dark = [color for color in non_canvas if _luminance(color) <= 0.55]
        ink = min(dark or non_canvas or ["141414"], key=_luminance)
        midtone = [color for color in non_canvas if 0.25 <= _luminance(color) <= 0.75 and color != ink]
        muted = max(
            midtone or [color for color in non_canvas if color != ink] or ["757575"],
            key=lambda color: explicit[color],
        )
        accent_candidates = [color for color in non_canvas if color not in {ink, muted}]
        saturated = max(accent_candidates, key=_saturation, default=muted)
        if _saturation(saturated) >= 0.12:
            accent = saturated
        else:
            accent = min(accent_candidates or [muted], key=_luminance)
        explicit_subtle = [
            color
            for color in explicit
            if color not in {canvas, ink, accent, muted}
        ]
        repeated_subtle = [
            color
            for color in explicit_subtle
            if explicit[color] >= max(3, deck.slide_count // 2)
        ]
        subtle = min(
            repeated_subtle
            or explicit_subtle
            or [color for color in non_canvas if color not in {ink, accent, muted}],
            key=lambda color: abs(_luminance(color) - _luminance(canvas)),
            default=muted,
        )

        margins = _estimated_margins(deck)
        observed_rails = Counter(
            round(value, 1)
            for slide in deck.slides
            for value in slide.left_edge_clusters
        )
        rail_count = max(4, min(12, len([value for value, count in observed_rails.items() if count >= 2])))
        columns = min(12, max(6, rail_count + (rail_count % 2)))
        mean_occupancy = mean((item.foreground_occupancy for item in visual.slides)) if visual.slides else 0.25
        low_density = sum(item.visual_density == "low" for item in visual.slides)
        high_density = sum(item.visual_density == "high" for item in visual.slides)
        density = "adaptive" if low_density and (high_density or low_density < len(visual.slides)) else (
            "high" if high_density > len(visual.slides) / 2 else "low" if low_density > len(visual.slides) / 2 else "medium"
        )
        rejected = [
            item.category
            for item in constraints.constraints
            if item.level == ConstraintLevel.REJECT
        ]

        line_share = sum(item.kind == "line" for item in objects) / max(len(objects), 1)
        has_repeated_edges = any(item.category == "repeated_edge_system" for item in constraints.constraints)
        text_share = sum(item.kind in {"text", "title"} for item in objects) / max(len(objects), 1)
        mean_saturation = mean((item.mean_saturation for item in visual.slides)) if visual.slides else 0.0
        personalities: list[str] = []
        if text_share >= 0.65 and not any(item.kind == "image" for item in objects):
            personalities.append("typography-first")
        if line_share >= 0.08:
            personalities.append("editorial")
        if has_repeated_edges and rail_count >= 6:
            personalities.append("executive")
        if mean_saturation <= 0.16:
            personalities.append("restrained")
        personalities.append("information-dense" if density in {"medium", "high", "adaptive"} else "spacious")
        if not any(item.has_shadow or item.has_bevel for item in objects):
            personalities.append("minimal-decoration")
        personalities = list(dict.fromkeys(personalities)) or ["reference-derived", "content-led"]
        return ReferenceDeckStyleProfile(
            id=profile_id,
            source_deck=deck.source_name,
            source_format=deck.source_format,
            visual_personality=personalities,
            typography={
                "title_font": font,
                "body_font": font,
                "title_size": round(title_size, 1),
                "body_size": round(body_size, 1),
                "caption_size": max(floor.minimum_caption_font_size, round(body_size * 0.72, 1)),
                "observed_body_size": round(observed_body, 1),
            },
            palette=[canvas, ink, muted, accent, subtle],
            color_roles={
                "canvas": canvas,
                "ink": ink,
                "muted": muted,
                "accent": accent,
                "subtle": subtle,
            },
            grid={
                "columns": columns,
                "observed_alignment_rails": rail_count,
                "margin_x": round(mean([margins["left"], margins["right"]]), 3),
                "margin_y": round(mean([margins["top"], margins["bottom"]]), 3),
                "gutter": round(
                    max(0.16, min(0.28, mean([margins["left"], margins["right"]]) * 0.32)),
                    3,
                ),
            },
            spacing_scale=_spacing_scale(deck),
            margins=margins,
            hierarchy={
                "title_ratio": round(title_size / max(body_size, 1), 2),
                "muted_weight": 0.42,
                "quality_floor_applied": "true",
            },
            density=density,
            shape_language=[
                "open composition fields",
                *( ["fine alignment rules"] if line_share >= 0.08 else [] ),
                "containers only when they express a semantic group",
            ],
            rule_language=[
                "alignment before borders",
                "one dominant anchor per page",
                "supporting evidence remains visually subordinate",
            ],
            number_language=[
                "dominant number with visible unit and context",
                "supporting metrics share a baseline rather than isolated cards",
            ],
            chart_language=[
                "choose chart form from the represented relationship",
                "direct labels and restrained axes",
            ],
            image_language=[
                "use imagery only when it explains or proves the message",
                "preserve image aspect ratio and deliberate crop",
            ],
            header_system={
                "enabled": has_repeated_edges,
                "position": "reference-derived edge rail" if has_repeated_edges else "content-dependent",
                "weight": "quiet",
            },
            footer_system={
                "enabled": has_repeated_edges,
                "position": "reference-derived edge rail" if has_repeated_edges else "content-dependent",
                "page_number": has_repeated_edges,
            },
            recurring_motifs=[
                *( ["quiet edge rail"] if has_repeated_edges else [] ),
                *( ["fine rule"] if line_share >= 0.08 else [] ),
                "consistent page marker",
            ],
            composition_principles=list(grammar.style_specific_traits),
            preferred_relationships=[
                RelationshipType.THESIS,
                RelationshipType.COMPARISON,
                RelationshipType.PROCESS,
                RelationshipType.HIERARCHY,
                RelationshipType.COMPOSITION,
                RelationshipType.DATA_TREND,
                RelationshipType.METRIC_DASHBOARD,
                RelationshipType.SUMMARY,
            ],
            preferred_compositions=[
                "editorial-column",
                "metric-field",
                "comparison-field",
                "progression",
                "dense-report",
                "closing-statement",
            ],
            anti_patterns=list(dict.fromkeys([*rejected, "fixed source-page coordinates", "equal-weight card wall"])),
            composition_policy={
                "whitespace_bias": round(max(0.35, min(0.85, 1.0 - mean_occupancy)), 3),
                "container_density": 0.12,
                "asymmetry": 0.6,
                "source_coordinates_reusable": False,
            },
            variation_seed=variation_seed,
        )
