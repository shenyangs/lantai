# -*- coding: utf-8 -*-
"""Template quality arbitration and selective-inheritance policy."""

from __future__ import annotations

from collections import Counter
from hashlib import sha1
from statistics import mean, median
from typing import Iterable

from ..models import (
    ConstraintLevel,
    DesignConstraint,
    DesignQualityFloor,
    QualityBand,
    TemplateMode,
    TemplateQualityProfile,
)
from .models import (
    ConstraintClassification,
    ReferenceDeckMeasurement,
    ReferenceObjectMeasurement,
    ReferenceVisualMeasurement,
    weakest_band,
)


def _constraint(
    category: str,
    value: str,
    level: ConstraintLevel,
    rationale: str,
    *,
    source: str = "reference",
    object_ids: Iterable[str] = (),
) -> DesignConstraint:
    digest = sha1(f"{category}|{value}|{level.value}".encode("utf-8")).hexdigest()[:12]
    return DesignConstraint(
        id=f"reference-constraint-{digest}",
        category=category,
        value=value,
        level=level,
        source=source,
        rationale=rationale,
        object_ids=list(object_ids),
    )


def _all_objects(deck: ReferenceDeckMeasurement) -> list[ReferenceObjectMeasurement]:
    return [item for slide in deck.slides for item in slide.objects]


def _band(value: float, *, strong: float, adequate: float, higher_is_better: bool = True) -> QualityBand:
    if higher_is_better:
        if value >= strong:
            return QualityBand.STRONG
        if value >= adequate:
            return QualityBand.ADEQUATE
        if value >= adequate * 0.6:
            return QualityBand.WEAK
        return QualityBand.UNACCEPTABLE
    if value <= strong:
        return QualityBand.STRONG
    if value <= adequate:
        return QualityBand.ADEQUATE
    if value <= adequate * 1.5:
        return QualityBand.WEAK
    return QualityBand.UNACCEPTABLE


def _font_sizes(objects: Iterable[ReferenceObjectMeasurement]) -> list[float]:
    return [size for item in objects for size in item.font_sizes]


def _font_families(objects: Iterable[ReferenceObjectMeasurement]) -> list[str]:
    return [font for item in objects for font in item.font_families]


def _colors(objects: Iterable[ReferenceObjectMeasurement]) -> list[str]:
    return [
        color
        for item in objects
        for color in [*item.text_colors, *item.fill_colors, *item.line_colors]
    ]


def _repeated_edge_objects(deck: ReferenceDeckMeasurement) -> list[tuple[tuple[object, ...], list[ReferenceObjectMeasurement]]]:
    by_signature: dict[tuple[object, ...], list[ReferenceObjectMeasurement]] = {}
    for slide in deck.slides:
        for item in [*slide.objects, *slide.inherited_objects]:
            near_edge = item.y <= deck.slide_height * 0.14 or item.y + item.height >= deck.slide_height * 0.86
            if not near_edge:
                continue
            signature = (
                item.kind,
                round(item.x, 1),
                round(item.y, 1),
                round(item.width, 1),
                round(item.height, 1),
                item.role_hint,
            )
            by_signature.setdefault(signature, []).append(item)
    threshold = max(2, int(deck.slide_count * 0.55))
    return [
        (signature, items)
        for signature, items in by_signature.items()
        if len({item.slide_number for item in items}) >= threshold
    ]


def _grid_consistency(deck: ReferenceDeckMeasurement) -> float:
    all_lefts = [round(x, 1) for slide in deck.slides for x in slide.left_edge_clusters]
    if not all_lefts:
        return 0
    common = sum(count for _value, count in Counter(all_lefts).most_common(8))
    return min(1.0, common / len(all_lefts))


def _spacing_consistency(deck: ReferenceDeckMeasurement) -> float:
    gaps: list[float] = []
    for slide in deck.slides:
        ordered = sorted(slide.top_edge_clusters)
        gaps.extend(round(b - a, 1) for a, b in zip(ordered, ordered[1:]) if b - a > 0.04)
    if len(gaps) < 2:
        return 0.65
    common = sum(count for _value, count in Counter(gaps).most_common(6))
    return min(1.0, common / len(gaps))


def _composition_signature_sequence(deck: ReferenceDeckMeasurement) -> list[tuple[object, ...]]:
    return [
        (
            slide.text_object_count,
            slide.image_count,
            slide.chart_count,
            slide.table_count,
            min(6, len(slide.left_edge_clusters)),
            slide.density_band,
        )
        for slide in deck.slides
    ]


def _longest_run(values: Iterable[object]) -> int:
    longest = current = 0
    previous: object | None = None
    for value in values:
        if value == previous:
            current += 1
        else:
            current = 1
            previous = value
        longest = max(longest, current)
    return longest


def _merge_constraints(constraints: Iterable[DesignConstraint]) -> list[DesignConstraint]:
    """Merge equivalent evidence into one stable constraint record.

    A repeated master system can be represented by many source objects.  The
    constraint identity describes the policy, while ``object_ids`` retain all
    supporting evidence without emitting duplicate IDs.
    """

    merged: dict[tuple[str, str, ConstraintLevel, str], DesignConstraint] = {}
    for item in constraints:
        key = (item.category, str(item.value), item.level, item.source)
        previous = merged.get(key)
        if previous is None:
            merged[key] = item
            continue
        merged[key] = previous.model_copy(
            update={
                "object_ids": list(dict.fromkeys([*previous.object_ids, *item.object_ids])),
            },
        )
    return list(merged.values())


class TemplateQualityArbitrator:
    """Judge a reference before extracting a reusable style profile."""

    def analyze_quality(
        self,
        deck: ReferenceDeckMeasurement,
        visual: ReferenceVisualMeasurement,
        quality_floor: DesignQualityFloor | None = None,
    ) -> TemplateQualityProfile:
        floor = quality_floor or DesignQualityFloor()
        objects = _all_objects(deck)
        object_evidence_present = bool(objects and deck.slides)
        text_objects = [item for item in objects if item.kind in {"text", "title"} and item.text_length]
        body_sizes = [
            size
            for item in text_objects
            if item.role_hint not in {"title", "footer", "page_number"}
            for size in item.font_sizes
        ]
        title_sizes = [size for item in text_objects if item.role_hint == "title" for size in item.font_sizes]
        median_body = median(body_sizes) if body_sizes else floor.minimum_body_font_size
        median_title = median(title_sizes) if title_sizes else max(body_sizes, default=32)
        hierarchy_ratio = median_title / max(median_body, 1)
        tiny_share = sum(size < floor.minimum_body_font_size for size in body_sizes) / max(len(body_sizes), 1)
        readability = (
            weakest_band(
                _band(median_body, strong=floor.minimum_body_font_size + 1, adequate=floor.minimum_body_font_size - 2),
                _band(tiny_share, strong=0.08, adequate=0.28, higher_is_better=False),
            )
            if text_objects
            else QualityBand.NOT_APPLICABLE
        )
        hierarchy = _band(hierarchy_ratio, strong=1.75, adequate=1.35) if text_objects else QualityBand.NOT_APPLICABLE
        grid_consistency = _band(_grid_consistency(deck), strong=0.56, adequate=0.38) if object_evidence_present else QualityBand.NOT_APPLICABLE
        spacing_quality = _band(_spacing_consistency(deck), strong=0.58, adequate=0.4) if object_evidence_present else QualityBand.NOT_APPLICABLE
        font_count = len(set(_font_families(objects)))
        typography_quality = (
            weakest_band(
                readability,
                _band(font_count, strong=2, adequate=4, higher_is_better=False),
            )
            if text_objects
            else QualityBand.NOT_APPLICABLE
        )
        explicit_palette = set(_colors(objects))
        palette_count = len(explicit_palette) if explicit_palette else len(visual.dominant_palette)
        mean_saturation = (
            mean(item.mean_saturation for item in visual.slides)
            if visual.slides
            else 0.0
        )
        color_discipline = weakest_band(
            _band(palette_count, strong=7, adequate=12, higher_is_better=False),
            _band(mean_saturation, strong=0.18, adequate=0.34, higher_is_better=False),
        )
        whitespace_values = [item.foreground_occupancy for item in visual.slides]
        balance_penalty = mean(abs(value - 0.22) for value in whitespace_values) if whitespace_values else 0.3
        visual_balance = _band(balance_penalty, strong=0.12, adequate=0.22, higher_is_better=False)
        density_variety = len(set(visual.density_sequence))
        density_rhythm = weakest_band(
            _band(density_variety, strong=3, adequate=2),
            _band(visual.repeated_density_run, strong=4, adequate=7, higher_is_better=False),
        )
        charts = [item for item in objects if item.kind == "chart"]
        chart_quality = QualityBand.NOT_APPLICABLE if not charts else QualityBand.ADEQUATE
        images = [item for item in objects if item.kind == "image"]
        distorted = [
            item
            for item in images
            if item.image_aspect_ratio
            and item.displayed_aspect_ratio
            and not any(abs(value) > 0.01 for value in item.image_crop.values())
            and abs(item.image_aspect_ratio - item.displayed_aspect_ratio) / item.image_aspect_ratio > 0.12
        ]
        image_quality = (
            QualityBand.NOT_APPLICABLE
            if not images
            else _band(len(distorted) / len(images), strong=0.02, adequate=0.15, higher_is_better=False)
        )
        repeated_edges = _repeated_edge_objects(deck)
        brand_consistency = (
            _band(len(repeated_edges), strong=3, adequate=1)
            if object_evidence_present
            else QualityBand.NOT_APPLICABLE
        )
        if object_evidence_present:
            signatures = _composition_signature_sequence(deck)
            variety_ratio = len(set(signatures)) / max(deck.slide_count, 1)
            composition_variety = _band(variety_ratio, strong=0.5, adequate=0.28)
            composition_run = _longest_run(signatures)
            composition_pacing = weakest_band(
                composition_variety,
                _band(composition_run, strong=2, adequate=4, higher_is_better=False),
            )
            # Density is intentionally coarse. A deck may stay information-dense
            # for a chapter while changing hierarchy and composition on every
            # page. In that case the object-level pacing evidence is decisive.
            rhythm = (
                QualityBand.STRONG
                if composition_pacing == QualityBand.STRONG and density_variety >= 2
                else weakest_band(composition_pacing, density_rhythm)
            )
        else:
            visual_signatures = {
                (item.visual_density, round(item.foreground_occupancy, 1), round(item.edge_density, 2))
                for item in visual.slides
            }
            variety_ratio = len(visual_signatures) / max(len(visual.slides), 1)
            composition_variety = _band(variety_ratio, strong=0.5, adequate=0.28)
            rhythm = density_rhythm

        severe = [
            band
            for band in (
                readability,
                hierarchy,
                grid_consistency,
                spacing_quality,
                color_discipline,
                visual_balance,
            )
            if band in {QualityBand.WEAK, QualityBand.UNACCEPTABLE}
        ]
        strong = [
            band
            for band in (
                readability,
                hierarchy,
                grid_consistency,
                spacing_quality,
                color_discipline,
                visual_balance,
                rhythm,
                brand_consistency,
                composition_variety,
            )
            if band == QualityBand.STRONG
        ]
        applicable_strong_threshold = 4 if not object_evidence_present else 6
        overall = QualityBand.STRONG if len(strong) >= applicable_strong_threshold and not severe else QualityBand.ADEQUATE
        if len(severe) >= 3:
            overall = QualityBand.WEAK
        if any(band == QualityBand.UNACCEPTABLE for band in severe) and len(severe) >= 4:
            overall = QualityBand.UNACCEPTABLE

        issues: list[str] = []
        if tiny_share > 0.28:
            issues.append("body typography frequently falls below the functional quality floor")
        if palette_count > 12:
            issues.append("the source uses too many uncoordinated explicit colors")
        if any(item.has_shadow for item in objects):
            issues.append("shadow effects require selective review before inheritance")
        if any(item.has_bevel for item in objects):
            issues.append("bevel or emboss-like effects should not be inherited")
        if distorted:
            issues.append("one or more uncropped images appear aspect-distorted")
        if composition_variety in {QualityBand.WEAK, QualityBand.UNACCEPTABLE}:
            issues.append("page composition repeats too mechanically")
        strengths: list[str] = []
        if grid_consistency == QualityBand.STRONG:
            strengths.append("stable alignment rails and grid discipline")
        if hierarchy == QualityBand.STRONG:
            strengths.append("clear typographic hierarchy")
        if color_discipline == QualityBand.STRONG:
            strengths.append("restrained, role-based color system")
        if rhythm == QualityBand.STRONG:
            strengths.append("meaningful density and pacing variation")
        if brand_consistency == QualityBand.STRONG:
            strengths.append("consistent repeated identity elements")
        recommendations = [
            "inherit repeated identity signals according to template mode",
            "recompose new pages from semantic relationships rather than source-page coordinates",
        ]
        if issues:
            recommendations.append("exclude rejected traits and enforce the design quality floor")
        return TemplateQualityProfile(
            readability=readability,
            information_hierarchy=hierarchy,
            grid_consistency=grid_consistency,
            spacing_quality=spacing_quality,
            typography_quality=typography_quality,
            color_discipline=color_discipline,
            visual_balance=visual_balance,
            visual_rhythm=rhythm,
            chart_quality=chart_quality,
            image_quality=image_quality,
            brand_consistency=brand_consistency,
            composition_variety=composition_variety,
            overall_quality=overall,
            strengths=strengths,
            major_issues=issues,
            inheritance_recommendations=recommendations,
        )

    def classify_constraints(
        self,
        deck: ReferenceDeckMeasurement,
        visual: ReferenceVisualMeasurement,
        quality: TemplateQualityProfile,
        template_mode: TemplateMode,
        *,
        explicit_hard_constraints: Iterable[str] = (),
        quality_floor: DesignQualityFloor | None = None,
    ) -> ConstraintClassification:
        floor = quality_floor or DesignQualityFloor()
        objects = _all_objects(deck)
        constraints: list[DesignConstraint] = [
            _constraint(
                "page_size",
                f"{deck.slide_width:g} x {deck.slide_height:g} in",
                ConstraintLevel.HARD,
                "Canvas size is a document-level compatibility constraint.",
            ),
        ]
        for value in explicit_hard_constraints:
            constraints.append(
                _constraint(
                    "user_declared",
                    str(value),
                    ConstraintLevel.HARD,
                    "The user explicitly marked this requirement as immutable.",
                    source="user",
                ),
            )

        identity_objects = [
            item for slide in deck.slides for item in [*slide.objects, *slide.inherited_objects]
        ]
        declared_logos = [item for item in identity_objects if item.role_hint == "logo"]
        if declared_logos:
            constraints.append(
                _constraint(
                    "logo",
                    "visible source objects explicitly identified as logo",
                    ConstraintLevel.HARD,
                    "An identified visible logo remains identity evidence even in a one-slide reference.",
                    object_ids=[item.object_id for item in declared_logos],
                ),
            )

        repeated = _repeated_edge_objects(deck)
        for signature, items in repeated:
            role = str(signature[-1])
            is_logo = role == "logo" or (signature[0] == "image" and signature[2] <= deck.slide_height * 0.14)
            if template_mode == TemplateMode.STYLE_REFERENCE and not is_logo:
                level = ConstraintLevel.SOFT
            elif is_logo:
                level = ConstraintLevel.HARD
            elif template_mode == TemplateMode.STRICT:
                level = ConstraintLevel.HARD
            else:
                level = ConstraintLevel.MEDIUM
            constraints.append(
                _constraint(
                    "logo" if is_logo else "repeated_edge_system",
                    f"{signature[0]} at normalized edge position",
                    level,
                    "Repeated cross-slide edge geometry is an identity or master signal, not a page template.",
                    object_ids=[item.object_id for item in items],
                ),
            )

        fonts = Counter(_font_families(identity_objects))
        if fonts:
            font, _count = fonts.most_common(1)[0]
            constraints.append(
                _constraint(
                    "typography",
                    font,
                    ConstraintLevel.HARD if template_mode == TemplateMode.STRICT else ConstraintLevel.MEDIUM,
                    "The dominant typeface contributes identity; size and composition remain content-dependent.",
                ),
            )
        explicit_colors = Counter(_colors(identity_objects))
        if explicit_colors:
            palette = ",".join(color for color, _count in explicit_colors.most_common(4))
            constraints.append(
                _constraint(
                    "palette",
                    palette,
                    ConstraintLevel.HARD if template_mode == TemplateMode.STRICT else ConstraintLevel.MEDIUM,
                    "Frequently repeated colors are candidate identity tokens, not permission to copy page layouts.",
                ),
            )
        elif visual.dominant_palette:
            constraints.append(
                _constraint(
                    "palette",
                    ",".join(visual.dominant_palette[:4]),
                    ConstraintLevel.HARD if template_mode == TemplateMode.STRICT else ConstraintLevel.SOFT,
                    "Rendered colors are a visual preference when native object evidence is unavailable.",
                ),
            )
        if deck.slides and objects:
            observed_rails = Counter(
                round(value, 1)
                for slide in deck.slides
                for value in slide.left_edge_clusters
            )
            reusable_rail_count = max(
                4,
                min(12, len([value for value, count in observed_rails.items() if count >= 2])),
            )
            constraints.extend([
                _constraint(
                    "grid",
                    f"approximately {reusable_rail_count} reusable alignment rails",
                    ConstraintLevel.MEDIUM if template_mode == TemplateMode.STRICT else ConstraintLevel.SOFT,
                    "Grid discipline is reusable, while exact source coordinates are deck-specific.",
                ),
                _constraint(
                    "composition",
                    "source-page object coordinates",
                    ConstraintLevel.SOFT,
                    "Individual coordinates are observations only and must be recomposed for new semantic content.",
                ),
            ])

        body_sizes = [
            size
            for item in objects
            if item.kind in {"text", "title"} and item.role_hint not in {"title", "footer", "page_number"}
            for size in item.font_sizes
        ]
        if body_sizes and sum(size < floor.minimum_body_font_size for size in body_sizes) / len(body_sizes) > 0.2:
            constraints.append(
                _constraint(
                    "tiny_body_text",
                    f"body sizes below {floor.minimum_body_font_size:g} pt",
                    ConstraintLevel.REJECT,
                    "Below-floor source sizing is diagnosed but is not inherited into new content.",
                ),
            )
        if len(set(_colors(objects))) > 12:
            constraints.append(
                _constraint(
                    "uncontrolled_palette",
                    "more than 12 explicit colors",
                    ConstraintLevel.REJECT,
                    "A broad unstructured palette weakens hierarchy and is not style DNA.",
                ),
            )
        for category, predicate, rationale in (
            ("random_heavy_shadow", lambda item: item.has_shadow, "Decorative shadows are not inherited without semantic purpose."),
            ("decorative_wordart", lambda item: item.has_bevel, "Bevel and emboss effects are excluded from generated style."),
        ):
            affected = [item.object_id for item in objects if predicate(item)]
            if affected:
                constraints.append(_constraint(category, "present in source", ConstraintLevel.REJECT, rationale, object_ids=affected))
        gradients = [item.object_id for item in objects if item.has_gradient]
        if gradients:
            gradient_level = (
                ConstraintLevel.REJECT
                if quality.overall_quality in {QualityBand.WEAK, QualityBand.UNACCEPTABLE}
                else ConstraintLevel.SOFT
            )
            constraints.append(
                _constraint(
                    "decorative_gradient" if gradient_level == ConstraintLevel.REJECT else "gradient_treatment",
                    "present in source",
                    gradient_level,
                    "A gradient must earn an information or brand role; it is never inherited merely because it exists.",
                    object_ids=gradients,
                ),
            )
        stretched = [
            item.object_id
            for item in objects
            if item.kind == "image"
            and item.image_aspect_ratio
            and item.displayed_aspect_ratio
            and not any(abs(value) > 0.01 for value in item.image_crop.values())
            and abs(item.image_aspect_ratio - item.displayed_aspect_ratio) / item.image_aspect_ratio > 0.12
        ]
        if stretched:
            constraints.append(
                _constraint(
                    "stretched_image",
                    "uncropped aspect mismatch",
                    ConstraintLevel.REJECT,
                    "Image distortion is a defect, never a style trait.",
                    object_ids=stretched,
                ),
            )
        return ConstraintClassification(
            template_mode=template_mode,
            constraints=_merge_constraints(constraints),
            inheritance_recommendations=list(quality.inheritance_recommendations),
        )
