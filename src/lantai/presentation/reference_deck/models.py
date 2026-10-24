# -*- coding: utf-8 -*-
"""Portable contracts for clean-room reference-deck learning.

The records describe measurements and design decisions.  They intentionally
do not retain slide XML, embedded media bytes, screenshots, or a reusable set
of source-slide coordinates.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ..models import (
    DesignConstraint,
    PresentationDesignContext,
    QualityBand,
    ReferenceDeckStyleProfile,
    StrictModel,
    TemplateMode,
    TemplateQualityProfile,
)


class ReferenceObjectMeasurement(StrictModel):
    """Object-level measurement extracted from an Office document."""

    object_id: str
    slide_number: int = Field(ge=1)
    z_order: int = Field(ge=0)
    source_layer: Literal["slide", "layout", "master"] = "slide"
    name: str = ""
    kind: str
    role_hint: str = "content"
    x: float = Field(ge=0)
    y: float = Field(ge=0)
    width: float = Field(ge=0)
    height: float = Field(ge=0)
    rotation: float = 0
    text_length: int = Field(default=0, ge=0)
    line_count: int = Field(default=0, ge=0)
    font_families: list[str] = Field(default_factory=list)
    font_sizes: list[float] = Field(default_factory=list)
    text_colors: list[str] = Field(default_factory=list)
    fill_colors: list[str] = Field(default_factory=list)
    line_colors: list[str] = Field(default_factory=list)
    fill_transparency: float | None = Field(default=None, ge=0, le=1)
    line_transparency: float | None = Field(default=None, ge=0, le=1)
    has_shadow: bool = False
    has_gradient: bool = False
    has_bevel: bool = False
    alignments: list[str] = Field(default_factory=list)
    margins: dict[str, float] = Field(default_factory=dict)
    is_placeholder: bool = False
    placeholder_type: str = ""
    image_aspect_ratio: float | None = None
    displayed_aspect_ratio: float | None = None
    image_crop: dict[str, float] = Field(default_factory=dict)
    chart_type: str = ""
    table_shape: tuple[int, int] | None = None
    group_children: int = Field(default=0, ge=0)


class ReferenceSlideMeasurement(StrictModel):
    slide_number: int = Field(ge=1)
    layout_name: str = ""
    master_name: str = ""
    background_colors: list[str] = Field(default_factory=list)
    object_count: int = Field(default=0, ge=0)
    text_object_count: int = Field(default=0, ge=0)
    image_count: int = Field(default=0, ge=0)
    chart_count: int = Field(default=0, ge=0)
    table_count: int = Field(default=0, ge=0)
    shape_count: int = Field(default=0, ge=0)
    text_characters: int = Field(default=0, ge=0)
    title_text: str = ""
    left_edge_clusters: list[float] = Field(default_factory=list)
    top_edge_clusters: list[float] = Field(default_factory=list)
    estimated_whitespace: float = Field(default=0.5, ge=0, le=1)
    density_band: Literal["low", "medium", "high"] = "medium"
    objects: list[ReferenceObjectMeasurement] = Field(default_factory=list)
    # Visible non-placeholder objects on the active inheritance chain are
    # identity evidence, not additional page-authored content or density.
    inherited_objects: list[ReferenceObjectMeasurement] = Field(default_factory=list)


class ReferenceDeckMeasurement(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    source_name: str
    source_format: Literal["pptx", "pdf", "images"]
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    slide_width: float = Field(gt=0)
    slide_height: float = Field(gt=0)
    slide_count: int = Field(ge=1)
    aspect_ratio: float = Field(gt=0)
    master_names: list[str] = Field(default_factory=list)
    layout_names: list[str] = Field(default_factory=list)
    theme_fonts: dict[str, str] = Field(default_factory=dict)
    theme_colors: list[str] = Field(default_factory=list)
    slides: list[ReferenceSlideMeasurement] = Field(default_factory=list)


class VisualSlideMeasurement(StrictModel):
    slide_number: int = Field(ge=1)
    pixel_width: int = Field(gt=0)
    pixel_height: int = Field(gt=0)
    mean_luminance: float = Field(ge=0, le=255)
    luminance_stddev: float = Field(ge=0)
    foreground_occupancy: float = Field(ge=0, le=1)
    edge_density: float = Field(ge=0, le=1)
    mean_saturation: float = Field(ge=0, le=1)
    dominant_colors: list[str] = Field(default_factory=list)
    visual_density: Literal["low", "medium", "high"] = "medium"


class ReferenceVisualMeasurement(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    slides: list[VisualSlideMeasurement] = Field(default_factory=list)
    density_sequence: list[str] = Field(default_factory=list)
    dominant_palette: list[str] = Field(default_factory=list)
    repeated_density_run: int = Field(default=0, ge=0)


class ConstraintClassification(StrictModel):
    template_mode: TemplateMode
    constraints: list[DesignConstraint] = Field(default_factory=list)
    inheritance_recommendations: list[str] = Field(default_factory=list)


class ExtractedDesignGrammar(StrictModel):
    universal_principles: list[str] = Field(default_factory=list)
    style_specific_traits: list[str] = Field(default_factory=list)
    deck_specific_decisions: list[str] = Field(default_factory=list)


class ReferenceLearningResult(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    measurement: ReferenceDeckMeasurement
    visual_measurement: ReferenceVisualMeasurement
    template_quality_profile: TemplateQualityProfile
    constraint_classification: ConstraintClassification
    extracted_grammar: ExtractedDesignGrammar
    style_profile: ReferenceDeckStyleProfile
    design_context: PresentationDesignContext
    render_paths: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def weakest_band(*bands: QualityBand) -> QualityBand:
    """Return the weakest qualitative band without pretending precision."""

    order = {
        QualityBand.STRONG: 4,
        QualityBand.ADEQUATE: 3,
        QualityBand.WEAK: 2,
        QualityBand.UNACCEPTABLE: 1,
        QualityBand.NOT_APPLICABLE: 5,
    }
    applicable = [band for band in bands if band != QualityBand.NOT_APPLICABLE]
    return min(applicable, key=order.__getitem__) if applicable else QualityBand.NOT_APPLICABLE
