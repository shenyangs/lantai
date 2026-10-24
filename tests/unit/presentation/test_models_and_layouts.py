from __future__ import annotations

import pytest
from pydantic import ValidationError

from lantai.presentation.layouts import CORE_LAYOUTS, check_capacity
from lantai.presentation.models import (
    ChartObject,
    CompositionEngine,
    DeckIR,
    DeckPlan,
    DeckPlanSlide,
    DeckSlide,
    GenerationMode,
    ImageObject,
    ObjectFrame,
    PresentationJobRequest,
    SlideIntent,
    ShapeObject,
    TableObject,
    TextObject,
)


def test_new_presentation_defaults_to_v3_with_semantic_visual_review() -> None:
    request = PresentationJobRequest(
        conversation_id="conversation-defaults",
        user_request="基于材料生成一份演示文稿",
    )

    assert request.composition_engine == CompositionEngine.NATIVE_PPTX
    assert request.vision_critique_required is True


def test_custom_template_requires_a_template_path() -> None:
    with pytest.raises(ValidationError, match="template_path"):
        PresentationJobRequest(
            conversation_id="conversation-a",
            user_request="生成一份产品汇报",
            mode=GenerationMode.CUSTOM_TEMPLATE,
        )


def test_deck_plan_requires_contiguous_order() -> None:
    intent = SlideIntent(
        id="intent-1",
        purpose="开场",
        key_message="本季度增长稳健",
        semantic_layout="cover",
    )
    with pytest.raises(ValidationError, match="contiguous"):
        DeckPlan(
            title="季度汇报",
            narrative_arc="先结论后证据",
            slides=[DeckPlanSlide(id="plan-1", order=2, title="封面", intent=intent)],
        )


def test_table_and_chart_widths_are_schema_validated() -> None:
    with pytest.raises(ValidationError, match="header width"):
        TableObject(headers=["指标", "数值"], rows=[["用户数"]])
    with pytest.raises(ValidationError, match="categories"):
        ChartObject(categories=["Q1", "Q2"], series={"收入": [10.0]})


def test_deck_object_ids_are_unique_across_slides() -> None:
    first = DeckSlide(
        id="slide-1",
        layout_id="cover",
        objects=[TextObject(id="shared", role="title", text="A", slot="title")],
    )
    second = DeckSlide(
        id="slide-2",
        layout_id="section",
        objects=[TextObject(id="shared", role="title", text="B", slot="title")],
    )
    with pytest.raises(ValidationError, match="unique across"):
        DeckIR(title="duplicate ids", slides=[first, second])


def test_capacity_check_reports_unknown_slots_types_and_overflow() -> None:
    slide = DeckSlide(
        id="slide-capacity",
        layout_id="cover",
        objects=[
            TextObject(role="title", text="X" * 80, slot="title"),
            TextObject(role="body", text="unsupported", slot="unknown"),
        ],
    )
    messages = [violation.message for violation in check_capacity(slide)]
    assert any("exceeds 54" in message for message in messages)
    assert any("not defined" in message for message in messages)


def test_cover_capacity_accepts_an_optional_explanatory_visual() -> None:
    slide = DeckSlide(
        id="slide-cover-visual",
        layout_id="cover",
        objects=[
            TextObject(id="title", role="title", text="以证据驱动设计", slot="title"),
            ImageObject(
                id="cover-image",
                path="/tmp/cover.png",
                alt="解释主题的封面视觉",
                slot="cover_visual",
            ),
        ],
    )

    assert check_capacity(slide) == []


def test_quote_capacity_accepts_an_optional_explanatory_visual() -> None:
    slide = DeckSlide(
        id="slide-quote-visual",
        layout_id="quote",
        objects=[
            TextObject(
                id="quote-copy",
                role="quote",
                text="The work changes before the title does.",
                slot="quote",
            ),
            TextObject(
                id="quote-source",
                role="body",
                text="Research participant",
                slot="attribution",
            ),
            ImageObject(
                id="quote-photo",
                path="/tmp/portrait.png",
                alt="Portrait supporting the attributed quotation",
                slot="quote_visual",
            ),
        ],
    )

    assert check_capacity(slide) == []


def test_registry_contains_v3_semantic_layouts_with_valid_fallbacks() -> None:
    assert set(CORE_LAYOUTS) == {
        "freeform", "cover", "section", "key_judgment", "two_column", "three_cards", "comparison", "process", "data_conclusion",
        "agenda", "matrix", "hierarchy", "table", "chart", "image_focus", "quote", "closing",
    }
    for layout in CORE_LAYOUTS.values():
        assert len({slot.id for slot in layout.slots}) == len(layout.slots)
        if layout.fallback_layout_id:
            assert layout.fallback_layout_id in CORE_LAYOUTS


def test_freeform_requires_bounded_frames_and_accepts_native_shapes() -> None:
    shape = ShapeObject(
        id="shape-one",
        frame=ObjectFrame(x=1, y=1, w=3, h=1),
        text="原生可编辑",
    )
    assert check_capacity(
        DeckSlide(layout_id="freeform", objects=[shape])
    ) == []
    unframed = TextObject(role="body", text="missing frame", slot="canvas")
    messages = [
        violation.message
        for violation in check_capacity(
            DeckSlide(layout_id="freeform", objects=[unframed])
        )
    ]
    assert messages == [
        f"freeform object {unframed.id} requires an explicit frame"
    ]


@pytest.mark.parametrize('series', [{}, {'数量': [float('nan')]}, {'数量': [float('inf')]}, {'数量': [-float('inf')]}])
def test_chart_rejects_missing_or_nonfinite_data(series) -> None:
    from lantai.presentation.models import ChartObject

    with pytest.raises(ValueError):
        ChartObject(categories=['六月'], series=series)
