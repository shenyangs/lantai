from __future__ import annotations

from lantai.presentation.design_intelligence import resolve_design_context
from lantai.presentation.models import (
    ConstraintLevel,
    PresentationBrief,
    PresentationJobRequest,
    QualityBand,
    TemplateMode,
)
from lantai.presentation.reference_deck.models import (
    ReferenceDeckMeasurement,
    ReferenceObjectMeasurement,
    ReferenceSlideMeasurement,
    ReferenceVisualMeasurement,
    VisualSlideMeasurement,
)
from lantai.presentation.reference_deck.profile_builder import ReferenceProfileBuilder


SHA256 = "a" * 64


def _object(
    *,
    slide: int,
    index: int,
    kind: str = "text",
    role: str = "content",
    x: float = 0.75,
    y: float = 1.5,
    width: float = 4.8,
    height: float = 0.5,
    size: float = 19,
    color: str = "555555",
    name: str = "",
    shadow: bool = False,
    bevel: bool = False,
    image_aspect_ratio: float | None = None,
    displayed_aspect_ratio: float | None = None,
) -> ReferenceObjectMeasurement:
    has_text = kind in {"text", "title"}
    return ReferenceObjectMeasurement(
        object_id=f"slide-{slide}-object-{index}",
        slide_number=slide,
        z_order=index,
        name=name,
        kind=kind,
        role_hint=role,
        x=x,
        y=y,
        width=width,
        height=height,
        text_length=24 if has_text else 0,
        line_count=2 if has_text else 0,
        font_families=["Example Sans"] if has_text else [],
        font_sizes=[size] if has_text else [],
        text_colors=[color] if has_text else [],
        line_colors=[color] if kind == "line" else [],
        fill_colors=[color] if kind in {"shape", "image"} else [],
        has_shadow=shadow,
        has_bevel=bevel,
        image_aspect_ratio=image_aspect_ratio,
        displayed_aspect_ratio=displayed_aspect_ratio,
    )


def _visual(sequence: list[str]) -> ReferenceVisualMeasurement:
    slides = [
        VisualSlideMeasurement(
            slide_number=index,
            pixel_width=1600,
            pixel_height=900,
            mean_luminance=238,
            luminance_stddev=35,
            foreground_occupancy={"low": 0.1, "medium": 0.22, "high": 0.38}[density],
            edge_density={"low": 0.02, "medium": 0.05, "high": 0.09}[density],
            mean_saturation=0.06,
            dominant_colors=["F7F7F4", "111111", "555555", "D8D8D0"],
            visual_density=density,
        )
        for index, density in enumerate(sequence, start=1)
    ]
    longest = max(
        (
            len(run)
            for value in set(sequence)
            for run in "".join("1" if item == value else "0" for item in sequence).split("0")
        ),
        default=0,
    )
    return ReferenceVisualMeasurement(
        slides=slides,
        density_sequence=sequence,
        dominant_palette=["F7F7F4", "111111", "555555", "D8D8D0"],
        repeated_density_run=longest,
    )


def _deck(
    slides: list[list[ReferenceObjectMeasurement]],
    *,
    source_format: str = "pptx",
    theme_colors: list[str] | None = None,
) -> ReferenceDeckMeasurement:
    measured = []
    for number, objects in enumerate(slides, start=1):
        measured.append(
            ReferenceSlideMeasurement(
                slide_number=number,
                layout_name=f"layout-{number % 2}",
                master_name="master-1",
                background_colors=["F7F7F4"],
                object_count=len(objects),
                text_object_count=sum(item.kind in {"text", "title"} for item in objects),
                image_count=sum(item.kind == "image" for item in objects),
                chart_count=sum(item.kind == "chart" for item in objects),
                table_count=sum(item.kind == "table" for item in objects),
                shape_count=sum(item.kind in {"shape", "line"} for item in objects),
                text_characters=sum(item.text_length for item in objects),
                title_text="A conclusion-led title",
                left_edge_clusters=sorted({item.x for item in objects}),
                top_edge_clusters=sorted({item.y for item in objects}),
                density_band="low" if len(objects) <= 2 else "high" if len(objects) >= 6 else "medium",
                objects=objects,
            ),
        )
    return ReferenceDeckMeasurement(
        source_name="controlled-reference.pptx",
        source_format=source_format,
        source_sha256=SHA256,
        slide_width=13.333,
        slide_height=7.5,
        slide_count=len(slides),
        aspect_ratio=16 / 9,
        master_names=["master-1"],
        layout_names=["layout-0", "layout-1"],
        theme_fonts={"major_latin": "Example Sans"},
        theme_colors=theme_colors or [],
        slides=measured,
    )


def _good_reference() -> ReferenceDeckMeasurement:
    slides: list[list[ReferenceObjectMeasurement]] = []
    for slide in range(1, 5):
        objects = [
            _object(slide=slide, index=1, kind="title", role="title", y=0.65, size=32, color="111111"),
            _object(slide=slide, index=2, y=1.65, size=19, color="555555"),
            _object(
                slide=slide,
                index=3,
                kind="line",
                role="content",
                y=0.25,
                width=11.8,
                height=0.01,
                color="D8D8D0",
            ),
        ]
        objects.extend(
            _object(
                slide=slide,
                index=4 + offset,
                x=6.2,
                y=1.65 + offset * 0.65,
                size=18,
                color="555555",
            )
            for offset in range(slide - 1)
        )
        slides.append(objects)
    return _deck(slides, theme_colors=["0000FF", "800080", "F7F7F4", "111111"])


def test_reference_style_uses_observed_colors_not_unused_office_theme_defaults() -> None:
    result = ReferenceProfileBuilder().build(
        _good_reference(),
        _visual(["low", "medium", "high", "medium"]),
        profile_id="reference-editorial",
        template_mode=TemplateMode.STYLE_REFERENCE,
    )

    assert result.style_profile.color_roles["canvas"] == "F7F7F4"
    assert "0000FF" not in result.style_profile.palette
    assert "800080" not in result.style_profile.palette
    assert "source-page object coordinates" not in result.style_profile.model_dump_json()
    constraint_ids = [item.id for item in result.constraint_classification.constraints]
    assert len(constraint_ids) == len(set(constraint_ids))


def test_weak_corporate_reference_preserves_logo_but_rejects_tiny_type() -> None:
    slides = []
    for slide in range(1, 5):
        slides.append(
            [
                _object(slide=slide, index=1, kind="image", role="logo", y=0.18, width=1.2, height=0.3, name="logo"),
                _object(slide=slide, index=2, kind="title", role="title", y=0.8, size=30, color="202020"),
                _object(slide=slide, index=3, y=1.6, size=12, color="555555"),
            ],
        )
    result = ReferenceProfileBuilder().build(
        _deck(slides),
        _visual(["low", "medium", "medium", "high"]),
        profile_id="weak-corporate",
        template_mode=TemplateMode.BRAND_PRESERVE_OPTIMIZE,
    )

    assert any(item.category == "logo" and item.level == ConstraintLevel.HARD for item in result.constraint_classification.constraints)
    assert any(item.category == "tiny_body_text" and item.level == ConstraintLevel.REJECT for item in result.constraint_classification.constraints)
    assert result.style_profile.typography["body_size"] >= 17


def test_intentionally_bad_reference_does_not_turn_defects_into_style_dna() -> None:
    colors = [f"{index:02X}{(index * 13) % 256:02X}{(255 - index * 7) % 256:02X}" for index in range(16)]
    slides: list[list[ReferenceObjectMeasurement]] = []
    for slide in range(1, 4):
        objects = [
            _object(
                slide=slide,
                index=index,
                y=0.35 + index * 0.25,
                size=8,
                color=color,
                shadow=index == 1,
                bevel=index == 2,
            )
            for index, color in enumerate(colors, start=1)
        ]
        objects.append(
            _object(
                slide=slide,
                index=20,
                kind="image",
                role="image",
                image_aspect_ratio=1.8,
                displayed_aspect_ratio=0.7,
            ),
        )
        slides.append(objects)
    result = ReferenceProfileBuilder().build(
        _deck(slides),
        _visual(["high", "high", "high"]),
        profile_id="intentionally-bad",
    )

    rejected = {item.category for item in result.design_context.profile.rejected_traits}
    assert {
        "tiny_body_text",
        "uncontrolled_palette",
        "random_heavy_shadow",
        "decorative_wordart",
        "stretched_image",
    } <= rejected
    assert result.template_quality_profile.overall_quality in {QualityBand.WEAK, QualityBand.UNACCEPTABLE}


def test_visual_only_reference_does_not_invent_native_object_quality_evidence() -> None:
    deck = _deck([[], [], []], source_format="images")
    result = ReferenceProfileBuilder().build(
        deck,
        _visual(["low", "medium", "high"]),
        profile_id="visual-only",
        template_mode=TemplateMode.STYLE_REFERENCE,
    )

    quality = result.template_quality_profile
    assert quality.typography_quality == QualityBand.NOT_APPLICABLE
    assert quality.grid_consistency == QualityBand.NOT_APPLICABLE
    assert quality.brand_consistency == QualityBand.NOT_APPLICABLE
    assert not any(item.category == "grid" for item in result.constraint_classification.constraints)
    assert any(item.category == "palette" and item.level == ConstraintLevel.SOFT for item in result.constraint_classification.constraints)


def test_no_reference_synthesizes_a_design_context_without_legacy_fallback() -> None:
    request = PresentationJobRequest(
        conversation_id="reference-optional",
        user_request="给产品委员会讲清楚为什么现在应当收敛功能范围",
        audience="产品委员会",
        slide_count=8,
        seed=23,
    )
    brief = PresentationBrief(
        objective="形成清晰的产品取舍",
        audience="产品委员会",
        narrative="判断、证据、取舍与行动",
    )

    context = resolve_design_context(request, brief)

    assert context.reference_input_present is False
    assert context.profile.source == "autonomous"
    assert context.profile.style_profile.id
    assert context.profile.quality_floor.minimum_body_font_size >= 17
