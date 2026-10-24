from __future__ import annotations

import pytest

from lantai.presentation.compiler import PresentationCompiler
from lantai.presentation.design_intelligence import compose_deck_ir_with_design_context
from lantai.presentation.models import (
    DeckIR,
    DeckPlan,
    DeckPlanSlide,
    DeckSlide,
    FactRecord,
    PresentationBrief,
    PresentationJobRequest,
    SlideIntent,
    TextObject,
    VisualDirection,
)
from lantai.presentation.planner import build_deck_ir
from lantai.presentation.quality import inspect_pptx
from lantai.presentation.reconcile import reconcile_pptx


def _request() -> PresentationJobRequest:
    return PresentationJobRequest(
        conversation_id="semantic-provenance",
        user_request="依据原文生成演示文稿",
        slide_count=1,
        seed=17,
    )


def _brief() -> PresentationBrief:
    return PresentationBrief(objective="说明来源边界", audience="读者", narrative="证据先行")


@pytest.mark.parametrize(
    ("layout", "missing_fact"),
    [("cover", False), ("closing", False), ("key_judgment", True), ("freeform", True)],
)
def test_geometry_promotion_preserves_source_provenance_page_type(
    tmp_path,
    layout: str,
    missing_fact: bool,
) -> None:
    deck = DeckIR(
        title="证据边界",
        source_refs=["fixture://source"],
        facts=[
            FactRecord(
                fact_id="fact-source", text="原文证据", source_refs=["fixture://source"]
            )
        ],
        slides=[
            DeckSlide(
                id="source-page",
                layout_id=layout,
                objects=[
                    TextObject(id="title", role="title", text="说明来源边界", slot="title")
                ],
            )
        ],
    )

    composed = compose_deck_ir_with_design_context(deck, _request(), _brief())
    assert composed.slides[0].layout_id == "freeform"
    output = tmp_path / "promoted.pptx"
    PresentationCompiler().compile(composed, output)
    report = inspect_pptx(composed, output)

    missing = [
        issue
        for issue in report.issues
        if issue.code == "provenance.slide_fact_missing"
    ]
    assert bool(missing) is missing_fact
    if not missing_fact:
        assert getattr(composed.slides[0], "semantic_layout", None) == layout


@pytest.mark.parametrize("layout", ["cover", "closing", "key_judgment"])
def test_planned_semantic_type_survives_serialization_and_native_reimport(
    tmp_path, layout
) -> None:
    plan = DeckPlan(
        title="证据边界",
        narrative_arc="证据先行",
        visual_direction=VisualDirection(
            visual_concept="克制可信",
            palette=["203A5F", "FFFFFF"],
            typography="Noto Sans SC",
            image_language="无照片",
            chart_language="原生图表",
            shape_language="简洁几何",
            grid="12 columns",
            recurring_motif="细线",
        ),
        slides=[
            DeckPlanSlide(
                id="page",
                order=1,
                title="说明来源边界",
                content_blocks=["保留原文证据"],
                intent=SlideIntent(
                    id="intent-page",
                    purpose="说明来源",
                    key_message="保留原文证据",
                    semantic_layout=layout,
                ),
            )
        ],
    )
    deck = build_deck_ir(_request(), plan)
    assert getattr(deck.slides[0], "semantic_layout", None) == layout
    restored = DeckIR.model_validate_json(deck.model_dump_json())
    assert restored.slides[0].semantic_layout == layout
    output = tmp_path / "native.pptx"
    PresentationCompiler().compile(restored, output)
    imported = reconcile_pptx(restored, output, assets_dir=tmp_path / "assets")
    assert imported.slides[0].semantic_layout == layout


def test_known_body_page_cannot_disguise_provenance_via_semantic_metadata(tmp_path) -> None:
    deck = DeckIR(
        title="正文不能变封面",
        source_refs=["fixture://source"],
        facts=[
            FactRecord(
                fact_id="fact-source", text="原文证据", source_refs=["fixture://source"]
            )
        ],
        slides=[
            DeckSlide(
                id="body",
                layout_id="key_judgment",
                objects=[
                    TextObject(id="title", role="title", text="正文仍须来源证据", slot="title")
                ],
            ).model_copy(update={"semantic_layout": "cover"})
        ],
    )
    output = tmp_path / "body.pptx"
    PresentationCompiler().compile(deck, output)
    report = inspect_pptx(deck, output)
    assert any(issue.code == "provenance.slide_fact_missing" for issue in report.issues)
