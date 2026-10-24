# -*- coding: utf-8 -*-
"""Source identity and exact fact boundaries for native object edits."""

from __future__ import annotations

import asyncio
import json

import pytest

from lantai.app.structured_utility import StructuredUtilityResult
from lantai.presentation.intelligence import UtilityPresentationObjectEditPlanner
from lantai.presentation.models import (
    AddObjectAction,
    ChartObject,
    DeckIR,
    DeckPlan,
    DeckPlanSlide,
    DeckSlide,
    FactRecord,
    ImageObject,
    MetricObject,
    ObjectFrame,
    PresentationBrief,
    PresentationObjectEditPlan,
    PresentationSourceSection,
    RemoveObjectAction,
    ReplaceTextAction,
    ShapeObject,
    SlideIntent,
    SlideIntentPatch,
    SourceCalculationProof,
    TableObject,
    TextObject,
    UpdateChartAction,
    VisualDirection,
)
from lantai.presentation.natural_edit import (
    apply_object_edit_plan,
    apply_slide_intent_patch,
    validate_requested_object_edit_scope,
)
from lantai.presentation.page_authoring import (
    validate_planned_chart_sources,
    validate_planned_text_sources,
)
from lantai.utils.operation_control import OperationControl, bind_operation


def _deck() -> DeckIR:
    return DeckIR(
        id="source-bound-deck",
        title="来源绑定测试",
        source_refs=["material://approved-data", "material://approved-note"],
        required_facts=["甲为42万元"],
        immutable_data=["乙为38万元"],
        facts=[
            FactRecord(
                fact_id="fact-approved",
                text="甲为42万元；乙为38万元",
                source_refs=["material://approved-data"],
                immutable=True,
            ),
        ],
        slides=[
            DeckSlide(
                id="slide-target",
                layout_id="freeform",
                intent_id="intent-target",
                source_fact_ids=["fact-approved"],
                objects=[
                    TextObject(id="title", role="title", text="旧标题"),
                    ChartObject(
                        id="chart",
                        categories=["甲", "乙"],
                        series={"收入": [42, 38]},
                        unit="万元",
                        derivation="来源直接数值",
                        source_refs=["material://approved-data"],
                    ),
                ],
            ),
        ],
    )


def _chart_action(source_refs=None) -> UpdateChartAction:
    return UpdateChartAction(
        object_id="chart",
        categories=["甲", "乙"],
        series={"收入": [42, 38], "成本": [31, 29]},
        source_refs=source_refs,
    )


def _multi_page_deck() -> tuple[DeckIR, DeckPlan]:
    deck = _deck()
    deck.slides.extend([
        DeckSlide(
            id="slide-selected", layout_id="freeform", intent_id="intent-selected",
            objects=[TextObject(id="selected-title", role="title", text="只改指定页")],
        ),
        DeckSlide(
            id="slide-unselected", layout_id="freeform", intent_id="intent-unselected",
            objects=[TextObject(id="unselected-title", role="title", text="不得改动此页")],
        ),
    ])
    plan = DeckPlan(
        title=deck.title, narrative_arc="仅在明确授权范围内修订",
        visual_direction=VisualDirection(
            visual_concept="来源核对", palette=["#111111", "#FFFFFF"],
            typography="清晰", image_language="无装饰图", chart_language="原生图表",
            shape_language="少量分隔线", grid="十二列", recurring_motif="来源标记",
        ),
        slides=[
            DeckPlanSlide(
                order=index, title=slide.id,
                intent=SlideIntent(
                    id=slide.intent_id, purpose="核对事实", key_message="原主判断",
                    required_facts=[f"指定事实{index}"],
                    immutable_facts=[f"限定条件{index}"],
                ),
            )
            for index, slide in enumerate(deck.slides, start=1)
        ],
    )
    return deck, plan


def _comparison_brief() -> PresentationBrief:
    return PresentationBrief(
        objective="核对收入差额", audience="管理层", narrative="按来源计算",
        source_refs=["material://approved-data", "material://approved-note"],
        source_sections=[
            PresentationSourceSection(
                title="收入", body="甲为42万元；乙为38万元",
                source_ref="material://approved-data",
            ),
            PresentationSourceSection(
                title="比较口径", body="可对甲、乙收入做差额比较，单位为万元",
                source_ref="material://approved-note",
            ),
        ],
    )


def _comparison_proof() -> SourceCalculationProof:
    return SourceCalculationProof(
        result="4", unit="万元",
        authorization_source_ref="material://approved-note",
        authorization_excerpt="可对甲、乙收入做差额比较，单位为万元",
        terms=[
            {"source_ref": "material://approved-data", "source_excerpt": "甲为42万元",
             "value": "42", "unit": "万元", "coefficient": 1},
            {"source_ref": "material://approved-data", "source_excerpt": "乙为38万元",
             "value": "38", "unit": "万元", "coefficient": -1},
        ],
    )


def test_intent_edit_adds_source_bound_difference_without_rewriting_facts():
    deck, planned = _multi_page_deck()
    planned.slides[0].intent.source_refs = ["material://approved-note"]
    planned.slides[0].intent.evidence = ["原证据保持不变"]
    before_deck, before_plan = deck.model_dump_json(), planned.model_dump_json()
    proof = _comparison_proof()
    edit = PresentationObjectEditPlan(
        slide_id="slide-target",
        intent_patch=SlideIntentPatch(
            key_message="甲收入较乙高4万元",
            source_ref_additions=["material://approved-data"],
            calculation_proof_additions=[proof],
        ),
    )

    changed = apply_slide_intent_patch(deck, planned, edit, brief=_comparison_brief())

    intent = changed.slides[0].intent
    assert intent.key_message == "甲收入较乙高4万元"
    assert intent.source_refs == ["material://approved-note", "material://approved-data"]
    assert intent.calculation_proofs == [proof]
    assert intent.model_dump(exclude={"key_message", "source_refs", "calculation_proofs"}) == (
        planned.slides[0].intent.model_dump(exclude={"key_message", "source_refs", "calculation_proofs"})
    )
    assert changed.slides[1:] == planned.slides[1:]
    validate_planned_text_sources([changed.slides[0]], _comparison_brief())
    assert deck.model_dump_json() == before_deck
    assert planned.model_dump_json() == before_plan


def _negative_comparison_proof() -> SourceCalculationProof:
    original_proof = _comparison_proof()
    return original_proof.model_copy(update={
        "result": "-4",
        "terms": [term.model_copy(update={"coefficient": -term.coefficient})
                  for term in original_proof.terms],
    })


@pytest.mark.parametrize("message", ["乙收入较甲减少4万元", "乙收入较甲减少四万元"])
def test_negative_calculated_difference_can_be_written_as_a_reduction_magnitude(message):
    deck, planned = _multi_page_deck()
    planned.slides[0].intent.source_refs = ["material://approved-note"]
    proof = _negative_comparison_proof()
    edit = PresentationObjectEditPlan(
        slide_id="slide-target",
        intent_patch=SlideIntentPatch(
            key_message=message,
            source_ref_additions=["material://approved-data"],
            calculation_proof_additions=[proof],
        ),
    )
    before = deck.model_dump_json(), planned.model_dump_json()

    changed = apply_slide_intent_patch(deck, planned, edit, brief=_comparison_brief())

    assert changed.slides[0].intent.key_message == message
    assert changed.slides[0].intent.calculation_proofs == [proof]
    assert changed.slides[1:] == planned.slides[1:]
    validate_planned_text_sources([changed.slides[0]], _comparison_brief())
    assert (deck.model_dump_json(), planned.model_dump_json()) == before


@pytest.mark.parametrize("tamper", ["missing-proof", "wrong-result", "uncited-proof"])
def test_reduction_magnitude_does_not_bypass_original_calculation_evidence(tamper):
    deck, planned = _multi_page_deck()
    planned.slides[0].intent.source_refs = ["material://approved-note"]
    proof = _negative_comparison_proof()
    if tamper == "wrong-result":
        proof = proof.model_copy(update={"result": "-5"})
    elif tamper == "uncited-proof":
        proof = proof.model_copy(update={"authorization_source_ref": "material://invented"})
    edit = PresentationObjectEditPlan(
        slide_id="slide-target",
        intent_patch=SlideIntentPatch(
            key_message="乙收入较甲减少4万元",
            source_ref_additions=["material://approved-data"],
            calculation_proof_additions=[] if tamper == "missing-proof" else [proof],
        ),
    )
    before = deck.model_dump_json(), planned.model_dump_json()

    with pytest.raises(ValueError):
        apply_slide_intent_patch(deck, planned, edit, brief=_comparison_brief())

    assert (deck.model_dump_json(), planned.model_dump_json()) == before


def test_prose_reduction_magnitude_does_not_reverse_a_native_chart_value():
    deck, planned = _multi_page_deck()
    chart = deck.slides[0].objects[1].model_copy(update={
        "categories": ["乙减甲"], "series": {"收入差额": [-4]},
        "source_refs": list(deck.source_refs),
        "calculation_proofs": [_negative_comparison_proof()],
    })
    planned.slides[0].charts = [chart]
    before = chart.model_dump_json()
    validate_planned_chart_sources([planned.slides[0]], _comparison_brief())
    planned.slides[0].charts = [chart.model_copy(update={"series": {"收入差额": [4]}})]

    with pytest.raises(ValueError, match="uncited chart value 4"):
        validate_planned_chart_sources([planned.slides[0]], _comparison_brief())

    assert chart.model_dump_json() == before


def test_object_editor_grounds_new_derivation_in_original_brief(monkeypatch):
    deck, planned = _multi_page_deck()
    planned.slides[0].intent.source_refs = ["material://approved-note"]
    brief = _comparison_brief()
    before = deck.model_dump_json(), planned.model_dump_json(), brief.model_dump_json()
    calls = []

    async def source_bound_utility(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        calls.append(payload)
        assert payload["source_brief"] == brief.model_dump(mode="json")
        return StructuredUtilityResult(
            value=kwargs["result_model"].model_validate({
                "slide_id": "slide-target",
                "actions": [{"kind": "replace_text", "object_id": "title", "text": "甲收入较乙高4万元"}],
                "intent_patch": {
                    "key_message": "甲收入较乙高4万元",
                    "source_ref_additions": ["material://approved-data"],
                    "calculation_proof_additions": [_comparison_proof().model_dump(mode="json")],
                },
            }),
            model_label="test-model", repaired=False,
        )

    monkeypatch.setattr("lantai.presentation.intelligence.run_structured_utility_task", source_bound_utility)
    edit = UtilityPresentationObjectEditPlanner("test-agent").plan(
        deck, "slide-target", "按原材料在指定页给出收入差额", deck_plan=planned, source_brief=brief,
    )
    changed_plan = apply_slide_intent_patch(deck, planned, edit, brief=brief)
    changed_deck, changed_ids = apply_object_edit_plan(deck, edit)
    validate_planned_text_sources([changed_plan.slides[0]], brief)
    assert len(calls) == 1
    assert changed_ids == ["title"]
    assert changed_deck.slides[0].objects[0].text == "甲收入较乙高4万元"
    assert changed_deck.slides[1:] == deck.slides[1:]
    assert changed_plan.slides[0].intent.calculation_proofs == [_comparison_proof()]
    assert (deck.model_dump_json(), planned.model_dump_json(), brief.model_dump_json()) == before


def test_intent_calculation_addition_cannot_borrow_stale_plan_source_authority():
    deck, planned = _multi_page_deck()
    planned.slides[0].intent.source_refs = list(deck.source_refs)
    deck.source_refs = ["material://approved-data"]
    before = deck.model_dump_json(), planned.model_dump_json()
    edit = PresentationObjectEditPlan(
        slide_id="slide-target",
        intent_patch=SlideIntentPatch(
            key_message="甲收入较乙高4万元",
            calculation_proof_additions=[_comparison_proof()],
        ),
    )

    with pytest.raises(ValueError, match="unavailable source|source.*scope"):
        apply_slide_intent_patch(deck, planned, edit, brief=_comparison_brief())

    assert (deck.model_dump_json(), planned.model_dump_json()) == before


@pytest.mark.parametrize("violation", [
    "missing_brief", "new_ref", "missing_section", "wrong_result",
    "wrong_input", "wrong_unit", "wrong_excerpt", "wrong_authorization", "uncited_ref",
])
def test_intent_grounding_additions_reject_invalid_evidence_atomically(violation):
    deck, planned = _multi_page_deck()
    brief, proof = _comparison_brief(), _comparison_proof()
    refs = list(deck.source_refs)
    if violation == "missing_brief":
        brief = None
    elif violation == "new_ref":
        refs.append("material://invented")
    elif violation == "missing_section":
        brief.source_sections = brief.source_sections[:1]
    elif violation == "wrong_result":
        proof = proof.model_copy(update={"result": "5"})
    elif violation == "wrong_input":
        proof = proof.model_copy(update={"terms": [
            proof.terms[0].model_copy(update={"value": "43"}), proof.terms[1],
        ]})
    elif violation == "wrong_unit":
        proof = proof.model_copy(update={"unit": "人", "terms": [
            term.model_copy(update={"unit": "人"}) for term in proof.terms
        ]})
    elif violation == "wrong_excerpt":
        proof = proof.model_copy(update={"terms": [
            proof.terms[0].model_copy(update={"source_excerpt": "甲收入42万元，已经审计"}),
            proof.terms[1],
        ]})
    elif violation == "wrong_authorization":
        proof = proof.model_copy(update={"authorization_excerpt": "允许解释为净利润"})
    elif violation == "uncited_ref":
        refs = ["material://approved-data"]
    before = deck.model_dump_json(), planned.model_dump_json()
    edit = PresentationObjectEditPlan(
        slide_id="slide-target",
        intent_patch=SlideIntentPatch(
            key_message="甲收入较乙高4万元", source_ref_additions=refs,
            calculation_proof_additions=[proof],
        ),
    )
    with pytest.raises(ValueError):
        apply_slide_intent_patch(deck, planned, edit, brief=brief)
    assert (deck.model_dump_json(), planned.model_dump_json()) == before


def test_intent_grounding_only_is_material_but_duplicate_evidence_is_not():
    deck, planned = _multi_page_deck()
    planned.slides[0].intent.key_message = "甲收入较乙高4万元"
    proof = _comparison_proof()
    edit = PresentationObjectEditPlan(
        slide_id="slide-target",
        intent_patch=SlideIntentPatch(
            key_message=planned.slides[0].intent.key_message,
            source_ref_additions=list(deck.source_refs), calculation_proof_additions=[proof],
        ),
    )
    before = planned.model_dump_json()
    changed = apply_slide_intent_patch(deck, planned, edit, brief=_comparison_brief())
    assert changed.slides[0].intent.calculation_proofs == [proof]
    assert changed.slides[0].intent.source_refs == deck.source_refs
    with pytest.raises(ValueError, match="no material change"):
        apply_slide_intent_patch(deck, changed, edit, brief=_comparison_brief())
    assert planned.model_dump_json() == before


def test_later_invalid_calculation_does_not_apply_earlier_grounded_patch():
    deck, planned = _multi_page_deck()
    before = deck.model_dump_json(), planned.model_dump_json()
    edit = PresentationObjectEditPlan(
        slide_id="slide-target", additional_slide_ids=["slide-selected"],
        intent_patches=[
            SlideIntentPatch(
                key_message="甲收入较乙高4万元", source_ref_additions=list(deck.source_refs),
                calculation_proof_additions=[_comparison_proof()],
            ),
            SlideIntentPatch(
                slide_id="slide-selected", key_message="甲收入较乙高5万元",
                source_ref_additions=list(deck.source_refs),
                calculation_proof_additions=[_comparison_proof().model_copy(update={"result": "5"})],
            ),
        ],
    )
    with pytest.raises(ValueError, match="recomputation"):
        apply_slide_intent_patch(deck, planned, edit, brief=_comparison_brief())
    assert (deck.model_dump_json(), planned.model_dump_json()) == before


def test_intent_calculation_append_preserves_existing_proofs_and_enforces_total_limit():
    deck, planned = _multi_page_deck()
    planned.slides[0].intent.source_refs = list(deck.source_refs)
    proof = _comparison_proof()
    old_proofs = [proof.model_copy(update={
        "authorization_excerpt": proof.authorization_excerpt + " " * index,
    }) for index in range(120)]
    planned.slides[0].intent.calculation_proofs = old_proofs
    before = planned.model_dump_json()
    edit = PresentationObjectEditPlan(
        slide_id="slide-target",
        intent_patch=SlideIntentPatch(
            key_message="甲收入较乙高4万元",
            calculation_proof_additions=[proof.model_copy(update={
                "authorization_excerpt": proof.authorization_excerpt + " " * 120,
            })],
        ),
    )
    with pytest.raises(ValueError, match="intent.*limit"):
        apply_slide_intent_patch(deck, planned, edit, brief=_comparison_brief())
    assert planned.model_dump_json() == before
    planned.slides[0].intent.calculation_proofs = old_proofs[:1]
    changed = apply_slide_intent_patch(deck, planned, edit, brief=_comparison_brief())
    assert changed.slides[0].intent.calculation_proofs == [
        old_proofs[0], edit.intent_patch.calculation_proof_additions[0],
    ]


def test_multiple_intent_corrections_keep_identity_facts_and_source_plan():
    deck, planned = _multi_page_deck()
    before = planned.model_dump_json()
    # Plan order/title are not the identity join used by an object editor.
    planned.slides.reverse()
    edit = PresentationObjectEditPlan(
        slide_id="slide-target", additional_slide_ids=["slide-selected"],
        intent_patches=[
            SlideIntentPatch(key_message="主页面的已核对判断"),
            SlideIntentPatch(slide_id="slide-selected", key_message="附加页的已核对判断"),
        ],
    )
    validate_requested_object_edit_scope(deck, "slide-target", ["slide-selected"], plan=edit)
    changed = apply_slide_intent_patch(deck, planned, edit)
    messages = {slide.intent.id: slide.intent.key_message for slide in changed.slides}
    assert messages == {
        "intent-target": "主页面的已核对判断",
        "intent-selected": "附加页的已核对判断",
        "intent-unselected": "原主判断",
    }
    for original, updated in zip(planned.slides, changed.slides, strict=True):
        assert updated.intent.model_dump(exclude={"key_message"}) == original.intent.model_dump(exclude={"key_message"})
        assert updated.model_dump(exclude={"intent"}) == original.model_dump(exclude={"intent"})
    assert all(slide.intent.key_message == "原主判断" for slide in planned.slides)
    planned.slides.reverse()
    assert planned.model_dump_json() == before


@pytest.mark.parametrize("bad_target", ["unknown", "missing_identity", "ambiguous_identity", "shared_deck_identity", "no_change"])
def test_multiple_intent_corrections_fail_atomically_on_later_invalid_patch(bad_target):
    deck, planned = _multi_page_deck()
    target = "slide-selected"
    message = "第二个已核对判断"
    if bad_target == "unknown":
        target = "slide-missing"
    elif bad_target == "missing_identity":
        deck.slides[1].intent_id = None
    elif bad_target == "ambiguous_identity":
        planned.slides[2].intent.id = "intent-selected"
    elif bad_target == "shared_deck_identity":
        deck.slides[2].intent_id = "intent-selected"
    else:
        message = "原主判断"
    before_plan, before_deck = planned.model_dump_json(), deck.model_dump_json()
    edit = PresentationObjectEditPlan(
        slide_id="slide-target", additional_slide_ids=["slide-selected"],
        intent_patches=[SlideIntentPatch(key_message="第一个已核对判断"),
                        SlideIntentPatch(slide_id=target, key_message=message)],
    )
    with pytest.raises(ValueError, match="unknown|identity|ambiguous|material change"):
        apply_slide_intent_patch(deck, planned, edit)
    assert planned.model_dump_json() == before_plan
    assert deck.model_dump_json() == before_deck


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("other_requested", [False, True])
def test_intent_correction_rejects_deck_identity_shared_by_another_page(legacy, other_requested):
    deck, planned = _multi_page_deck()
    deck.slides[2].intent_id = deck.slides[0].intent_id
    # Both schemas accept this historical deck. Do not silently rebind by order.
    deck = DeckIR.model_validate_json(deck.model_dump_json())
    planned = DeckPlan.model_validate_json(planned.model_dump_json())
    planned.slides.reverse()
    before_plan, before_deck = planned.model_dump_json(), deck.model_dump_json()
    patch = SlideIntentPatch(key_message="只授权一页的已核对判断")
    edit = PresentationObjectEditPlan(
        slide_id="slide-target",
        additional_slide_ids=["slide-unselected"] if other_requested else [],
        **({"intent_patch": patch} if legacy else {"intent_patches": [patch]}),
    )
    validate_requested_object_edit_scope(deck, "slide-target", edit.additional_slide_ids, plan=edit)
    with pytest.raises(ValueError, match="ambiguous.*identity|identity.*ambiguous"):
        apply_slide_intent_patch(deck, planned, edit)
    assert planned.model_dump_json() == before_plan
    assert deck.model_dump_json() == before_deck


@pytest.mark.parametrize("patches,legacy,match", [
    ([SlideIntentPatch(key_message="甲"), SlideIntentPatch(slide_id="slide-target", key_message="乙")], None, "unique"),
    ([SlideIntentPatch(slide_id="slide-selected", key_message="甲"), SlideIntentPatch(slide_id="slide-selected", key_message="乙")], None, "unique"),
    ([SlideIntentPatch(key_message="甲")], SlideIntentPatch(key_message="乙"), "cannot combine"),
    ([SlideIntentPatch(slide_id="", key_message="甲")], None, "empty"),
    ([SlideIntentPatch(slide_id=f"slide-{index}", key_message="甲") for index in range(11)], None, "at most 10"),
])
def test_multiple_intent_corrections_reject_ambiguous_or_unbounded_schema(patches, legacy, match):
    with pytest.raises(ValueError, match=match):
        PresentationObjectEditPlan(slide_id="slide-target", intent_patch=legacy, intent_patches=patches)


def test_multi_intent_plan_cannot_invent_page_authority(monkeypatch):
    deck, planned = _multi_page_deck()

    async def fake_utility(**kwargs):
        return StructuredUtilityResult(
            value=PresentationObjectEditPlan(
                slide_id="slide-target", additional_slide_ids=["slide-selected"],
                intent_patches=[SlideIntentPatch(key_message="授权判断"),
                                SlideIntentPatch(slide_id="slide-unselected", key_message="越权判断")],
            ),
            model_label="test-model", repaired=False,
        )

    monkeypatch.setattr("lantai.presentation.intelligence.run_structured_utility_task", fake_utility)
    with pytest.raises(PermissionError, match="requested intent scope"):
        UtilityPresentationObjectEditPlanner("test-agent").plan(
            deck, "slide-target", "只修两页判断", deck_plan=planned,
            additional_slide_ids=["slide-selected"],
        )


@pytest.mark.parametrize("additional", [None, ["slide-selected"]])
def test_object_edit_planner_supplies_exact_native_frame_contract(monkeypatch, additional):
    deck, planned = _multi_page_deck()
    calls = []

    async def fake_utility(**kwargs):
        calls.append(kwargs)
        return StructuredUtilityResult(
            value=PresentationObjectEditPlan(
                slide_id="slide-target",
                actions=[ReplaceTextAction(object_id="title", text="保留来源判断")],
            ), model_label="fixture/fixture", repaired=False,
        )

    monkeypatch.setattr("lantai.presentation.intelligence.run_structured_utility_task", fake_utility)
    UtilityPresentationObjectEditPlanner("fixture-agent").plan(
        deck, "slide-target", "调整有向流程的原生形状", deck_plan=planned,
        additional_slide_ids=additional,
    )
    payload = json.loads(calls[0]["user_prompt"])
    assert payload["native_geometry_contract"] == {
        "canvas_inches": {"width": 13.333, "height": 7.5},
        "frame_bounds": {
            "x": {"min": 0, "max": 13.333},
            "y": {"min": 0, "max": 7.5},
            "w": {"exclusive_min": 0, "max": 13.333},
            "h": {"min": 0, "max": 7.5},
        },
        "frame_edge_bounds": {"x_plus_w_max": 13.3335, "y_plus_h_max": 7.5005},
        "applies_to": ["add_object", "move_resize"],
        "negative_dimensions_forbidden": True,
        "zero_width_forbidden_including_lines": True,
    }
    assert "native_geometry_contract" in calls[0]["system_prompt"]
    assert "unselected-title" not in calls[0]["user_prompt"]
    assert len(calls) == 1
    assert calls[0]["timeout_seconds"] == 90


@pytest.mark.parametrize("additional", [None, ["slide-selected"]])
def test_object_edit_planner_exposes_only_supported_action_targets(monkeypatch, additional):
    deck, planned = _multi_page_deck()
    deck.slides[0].objects.extend([
        ShapeObject(id="shape-label", text="原生形状中的标签", frame=ObjectFrame(x=1, y=1, w=2, h=1)),
        MetricObject(id="metric-value", label="已有指标", value="42"),
        TableObject(id="table-value", headers=["甲"], rows=[["42"]]),
        ImageObject(id="image-value", path="/private/tmp/fixture.png", alt="已有图片"),
    ])
    calls = []

    async def fake_utility(**kwargs):
        calls.append(kwargs)
        return StructuredUtilityResult(
            value=PresentationObjectEditPlan(
                slide_id="slide-target", actions=[ReplaceTextAction(object_id="title", text="保留来源判断")],
            ), model_label="fixture/fixture", repaired=False,
        )

    monkeypatch.setattr("lantai.presentation.intelligence.run_structured_utility_task", fake_utility)
    UtilityPresentationObjectEditPlanner("fixture-agent").plan(
        deck, "slide-target", "只在明确范围内编辑原生对象", deck_plan=planned,
        additional_slide_ids=additional,
    )
    payload = json.loads(calls[0]["user_prompt"])
    assert payload["editable_object_ids_by_action"] == {
        "replace_text": ["title"], "update_chart": ["chart"], "replace_image": ["image-value"],
        "update_table": ["table-value"],
        "set_shape_text": ["shape-label"],
        "move_resize": [obj.id for obj in deck.slides[0].objects],
        "remove_object": [obj.id for obj in deck.slides[0].objects],
    }
    assert "editable_object_ids_by_action" in calls[0]["system_prompt"]
    if additional:
        assert payload["additional_slides"][0]["editable_object_ids_by_action"] == {
            "replace_text": ["selected-title"], "update_chart": [], "replace_image": [],
            "update_table": [],
            "set_shape_text": [],
            "move_resize": ["selected-title"], "remove_object": ["selected-title"],
        }
    else:
        assert payload["additional_slides"] == []
    assert "unselected-title" not in calls[0]["user_prompt"]
    assert len(calls) == 1
    assert calls[0]["timeout_seconds"] == 90


@pytest.mark.parametrize("kind", ["shape", "metric", "table", "chart", "image"])
def test_text_action_on_non_text_object_remains_atomic_and_fails_closed(kind):
    deck = _deck()
    candidates = {
        "shape": ShapeObject(id="unsupported-target", text="不得隐式重写形状", frame=ObjectFrame(x=1, y=1, w=2, h=1)),
        "metric": MetricObject(id="unsupported-target", label="甲", value="42"),
        "table": TableObject(id="unsupported-target", headers=["甲"], rows=[["42"]]),
        "chart": deck.slides[0].objects[1].model_copy(update={"id": "unsupported-target"}),
        "image": ImageObject(id="unsupported-target", path="/private/tmp/fixture.png", alt="原图"),
    }
    deck.slides[0].objects.append(candidates[kind])
    before = deck.model_dump_json()
    edit = PresentationObjectEditPlan(slide_id="slide-target", actions=[
        ReplaceTextAction(object_id="title", text="不能部分落盘"),
        ReplaceTextAction(object_id="unsupported-target", text="不支持的操作"),
    ])
    with pytest.raises(ValueError, match="not editable text"):
        apply_object_edit_plan(deck, edit)
    assert deck.model_dump_json() == before


def test_object_edit_planner_supplies_only_explicit_additional_page_context(monkeypatch):
    deck, planned = _multi_page_deck()
    calls = []

    async def fake_utility(**kwargs):
        calls.append(kwargs)
        return StructuredUtilityResult(
            value=PresentationObjectEditPlan(
                slide_id="slide-target", additional_slide_ids=["slide-selected"],
                actions=[ReplaceTextAction(object_id="selected-title", text="经事实核对的判断")],
                intent_patch=SlideIntentPatch(
                    slide_id="slide-selected", key_message="经事实核对的判断",
                ),
            ), model_label="fixture/fixture", repaired=False,
        )

    monkeypatch.setattr("lantai.presentation.intelligence.run_structured_utility_task", fake_utility)
    result = UtilityPresentationObjectEditPlanner("fixture-agent").plan(
        deck, "slide-target", "同时修订明确指定的两页", deck_plan=planned,
        additional_slide_ids=["slide-selected"],
    )
    payload = json.loads(calls[0]["user_prompt"])
    assert payload["allowed_slide_ids"] == ["slide-target", "slide-selected"]
    assert payload["additional_slide_ids"] == ["slide-selected"]
    assert len(payload["additional_slides"]) == 1
    selected = payload["additional_slides"][0]
    assert selected["slide"] == deck.slides[1].model_dump(mode="json")
    assert selected["planned_intent"] == planned.slides[1].intent.model_dump(mode="json")
    assert selected["fact_visibility_contract"]["facts"] == [
        {"path": "required_facts[0]", "text": "指定事实2"},
        {"path": "immutable_facts[0]", "text": "限定条件2"},
    ]
    assert "unselected-title" not in calls[0]["user_prompt"]
    assert "allowed_slide_ids" in calls[0]["system_prompt"]
    assert result.additional_slide_ids == ["slide-selected"]


def test_object_edit_planner_supplies_explicit_required_pages_without_changing_permissions(monkeypatch):
    deck, planned = _multi_page_deck()
    calls = []

    async def fake_utility(**kwargs):
        calls.append(kwargs)
        return StructuredUtilityResult(
            value=PresentationObjectEditPlan(
                slide_id="slide-target", additional_slide_ids=["slide-selected"],
                actions=[ReplaceTextAction(object_id="title", text="主页面改动"),
                         ReplaceTextAction(object_id="selected-title", text="附加页改动")],
            ), model_label="fixture/fixture", repaired=False,
        )

    monkeypatch.setattr("lantai.presentation.intelligence.run_structured_utility_task", fake_utility)
    UtilityPresentationObjectEditPlanner("fixture-agent").plan(
        deck, "slide-target", "明确要求两页均完成修改", deck_plan=planned,
        additional_slide_ids=["slide-selected"],
        required_slide_ids=["slide-target", "slide-selected"],
    )
    payload = json.loads(calls[0]["user_prompt"])
    assert payload["required_slide_ids"] == ["slide-target", "slide-selected"]
    assert payload["allowed_slide_ids"] == ["slide-target", "slide-selected"]
    assert "required_slide_ids" in calls[0]["system_prompt"]
    assert "unselected-title" not in calls[0]["user_prompt"]


@pytest.mark.parametrize("required_count", [2, 3])
def test_explicit_multi_page_required_edit_gets_bounded_default_timeout(monkeypatch, required_count):
    deck, planned = _multi_page_deck()
    additional = ["slide-selected", "slide-unselected"][:required_count - 1]
    required = ["slide-target", *additional]
    before_deck, before_plan = deck.model_dump_json(), planned.model_dump_json()
    calls = []

    async def fake_utility(**kwargs):
        calls.append(kwargs)
        return StructuredUtilityResult(
            value=PresentationObjectEditPlan(
                slide_id="slide-target", additional_slide_ids=additional,
                actions=[ReplaceTextAction(object_id=object_id, text="明确范围内的真实改动")
                         for object_id in ["title", "selected-title", "unselected-title"][:required_count]],
            ), model_label="fixture/fixture", repaired=False,
        )

    monkeypatch.setattr("lantai.presentation.intelligence.run_structured_utility_task", fake_utility)
    result = UtilityPresentationObjectEditPlanner("fixture-agent").plan(
        deck, "slide-target", "在一次原子提案中完成所有明确必改页", deck_plan=planned,
        additional_slide_ids=additional, required_slide_ids=required,
    )
    assert len(calls) == 1
    assert calls[0]["timeout_seconds"] == 180
    payload = json.loads(calls[0]["user_prompt"])
    assert payload["required_slide_ids"] == required
    assert payload["allowed_slide_ids"] == required
    assert result.additional_slide_ids == additional
    assert deck.model_dump_json() == before_deck
    assert planned.model_dump_json() == before_plan


def _timeout_scope_result(additional):
    object_ids = {"slide-target": "title", "slide-selected": "selected-title",
                  "slide-unselected": "unselected-title"}
    return StructuredUtilityResult(
        value=PresentationObjectEditPlan(
            slide_id="slide-target", additional_slide_ids=list(additional or []),
            actions=[ReplaceTextAction(object_id=object_ids[page], text="范围内的实际改动")
                     for page in ["slide-target", *(additional or [])]],
        ), model_label="fixture/fixture", repaired=False,
    )


@pytest.mark.parametrize("additional,required", [
    (None, None), ([], []), (["slide-selected"], None),
    (["slide-selected"], ["slide-target"]),
    (["slide-selected"], ["slide-selected"]),
    (["slide-selected", "slide-unselected"], []),
])
def test_optional_and_single_required_page_edits_keep_legacy_timeout(monkeypatch, additional, required):
    deck, planned = _multi_page_deck()
    calls = []

    async def fake_utility(**kwargs):
        calls.append(kwargs)
        return _timeout_scope_result(additional)

    monkeypatch.setattr("lantai.presentation.intelligence.run_structured_utility_task", fake_utility)
    planner = UtilityPresentationObjectEditPlanner("fixture-agent")
    assert planner.timeout_seconds == 90
    planner.plan(deck, "slide-target", "仅处理调用者明确范围", deck_plan=planned,
                 additional_slide_ids=additional, required_slide_ids=required)
    assert len(calls) == 1
    assert calls[0]["timeout_seconds"] == 90


@pytest.mark.parametrize("explicit_timeout", [30, 90, 240])
def test_required_multi_page_edit_preserves_explicit_caller_timeout(monkeypatch, explicit_timeout):
    deck, planned = _multi_page_deck()
    calls = []

    async def fake_utility(**kwargs):
        calls.append(kwargs)
        return _timeout_scope_result(["slide-selected"])

    monkeypatch.setattr("lantai.presentation.intelligence.run_structured_utility_task", fake_utility)
    planner = UtilityPresentationObjectEditPlanner("fixture-agent", timeout_seconds=explicit_timeout)
    planner.plan(deck, "slide-target", "显式时限不能被范围策略覆盖", deck_plan=planned,
                 additional_slide_ids=["slide-selected"], required_slide_ids=["slide-target", "slide-selected"])
    assert planner.timeout_seconds == explicit_timeout
    assert len(calls) == 1
    assert calls[0]["timeout_seconds"] == explicit_timeout


@pytest.mark.parametrize("remaining,expected", [(7, 7), (180, 180), (300, 180)])
def test_multi_required_page_timeout_still_obeys_operation_absolute_deadline(monkeypatch, remaining, expected):
    deck, planned = _multi_page_deck()
    calls = []

    async def fake_utility(**kwargs):
        calls.append(kwargs)
        return _timeout_scope_result(["slide-selected"])

    monkeypatch.setattr("lantai.presentation.intelligence.run_structured_utility_task", fake_utility)
    control = OperationControl(owner_key=("presentation", "synthetic-job"), identity=("synthetic-generation",),
                               ensure_active=lambda: None, remaining_seconds=lambda: remaining)
    with bind_operation(control):
        UtilityPresentationObjectEditPlanner("fixture-agent").plan(
            deck, "slide-target", "不得越过作业绝对期限", deck_plan=planned,
            additional_slide_ids=["slide-selected"], required_slide_ids=["slide-target", "slide-selected"],
        )
    assert len(calls) == 1
    assert calls[0]["timeout_seconds"] == expected


@pytest.mark.parametrize("remaining", [0, -1])
def test_expired_required_multi_page_edit_does_not_call_provider(monkeypatch, remaining):
    deck, planned = _multi_page_deck()
    calls = []

    async def unexpected_utility(**kwargs):
        calls.append(kwargs)
        raise AssertionError("expired operation must not reach provider")

    monkeypatch.setattr("lantai.presentation.intelligence.run_structured_utility_task", unexpected_utility)
    control = OperationControl(owner_key=("presentation", "synthetic-job"), identity=("synthetic-generation",),
                               ensure_active=lambda: None, remaining_seconds=lambda: remaining)
    with bind_operation(control), pytest.raises(TimeoutError, match="absolute deadline"):
        UtilityPresentationObjectEditPlanner("fixture-agent").plan(
            deck, "slide-target", "过期作业不得新启动模型", deck_plan=planned,
            additional_slide_ids=["slide-selected"], required_slide_ids=["slide-target", "slide-selected"],
        )
    assert calls == []


def test_stopped_required_multi_page_edit_does_not_call_provider(monkeypatch):
    deck, planned = _multi_page_deck()
    calls = []

    async def unexpected_utility(**kwargs):
        calls.append(kwargs)
        raise AssertionError("stopped operation must not reach provider")

    def stopped():
        raise asyncio.CancelledError()

    monkeypatch.setattr("lantai.presentation.intelligence.run_structured_utility_task", unexpected_utility)
    control = OperationControl(owner_key=("presentation", "synthetic-job"), identity=("synthetic-generation",),
                               ensure_active=stopped, remaining_seconds=lambda: 180)
    with bind_operation(control), pytest.raises(asyncio.CancelledError):
        UtilityPresentationObjectEditPlanner("fixture-agent").plan(
            deck, "slide-target", "停止仍阻止新调用", deck_plan=planned,
            additional_slide_ids=["slide-selected"], required_slide_ids=["slide-target", "slide-selected"],
        )
    assert calls == []


def test_stop_settles_inflight_required_multi_page_planning_without_retry(monkeypatch):
    deck, planned = _multi_page_deck()
    before_deck, before_plan = deck.model_dump_json(), planned.model_dump_json()
    calls, state = [], {"started": False, "settled": False}

    async def offline_utility(**kwargs):
        calls.append(kwargs)
        state["started"] = True
        try:
            await asyncio.sleep(10)
        finally:
            state["settled"] = True
        raise AssertionError("stopped planner must not finish its proposal")

    def stop_after_admission():
        if state["started"]:
            raise asyncio.CancelledError()

    monkeypatch.setattr("lantai.presentation.intelligence.run_structured_utility_task", offline_utility)
    control = OperationControl(owner_key=("presentation", "synthetic-job"), identity=("synthetic-generation",),
                               ensure_active=stop_after_admission, remaining_seconds=lambda: 180)
    with bind_operation(control), pytest.raises(asyncio.CancelledError):
        UtilityPresentationObjectEditPlanner("fixture-agent").plan(
            deck, "slide-target", "停止后先收束调用而非重试", deck_plan=planned,
            additional_slide_ids=["slide-selected"], required_slide_ids=["slide-target", "slide-selected"],
        )
    assert state == {"started": True, "settled": True}
    assert len(calls) == 1
    assert calls[0]["timeout_seconds"] == 180
    assert deck.model_dump_json() == before_deck
    assert planned.model_dump_json() == before_plan


@pytest.mark.parametrize("required", [
    ["slide-selected", "slide-selected"], ["slide-unselected"], ["unknown-slide"],
    [""], "slide-target", [f"slide-{index}" for index in range(11)],
])
def test_object_edit_planner_rejects_invalid_required_pages_before_provider(monkeypatch, required):
    deck, _planned = _multi_page_deck()
    calls = []

    async def unexpected_utility(**kwargs):
        calls.append(kwargs)
        raise AssertionError("invalid mandatory coverage must not call a provider")

    monkeypatch.setattr("lantai.presentation.intelligence.run_structured_utility_task", unexpected_utility)
    with pytest.raises(ValueError, match="required.*scope|scope.*required"):
        UtilityPresentationObjectEditPlanner("fixture-agent").plan(
            deck, "slide-target", "必改页不能扩大权限",
            additional_slide_ids=["slide-selected"], required_slide_ids=required,
        )
    assert calls == []


@pytest.mark.parametrize("metadata_only", [False, True])
def test_required_edit_coverage_rejects_declarations_and_bookkeeping_only(metadata_only):
    from lantai.presentation.natural_edit import validate_required_object_edit_coverage

    deck, _planned = _multi_page_deck()
    changed = deck.model_copy(deep=True)
    changed.slides[0].objects[0].text = "已落实的主页面"
    if metadata_only:
        changed.slides[1].revision += 1
        changed.slides[1].scene_hash = "bookkeeping-only"
    before = deck.model_dump_json()
    with pytest.raises(ValueError, match="edit.required_slide_unchanged"):
        validate_required_object_edit_coverage(
            deck, changed, ["slide-target", "slide-selected"],
        )
    assert deck.model_dump_json() == before


def test_required_edit_coverage_accepts_net_object_changes_and_validated_intent_changes():
    from lantai.presentation.natural_edit import validate_required_object_edit_coverage

    deck, _planned = _multi_page_deck()
    changed = deck.model_copy(deep=True)
    changed.slides[0].objects[0].text = "已落实的主页面"
    validate_required_object_edit_coverage(
        deck, changed, ["slide-target", "slide-selected"],
        changed_intent_slide_ids=["slide-selected"],
    )
    # Empty defaults remain permission-only; they require no extra-page edits.
    validate_required_object_edit_coverage(deck, deck, [])


@pytest.mark.parametrize("additional", [
    ["slide-selected", "slide-selected"], ["slide-target"],
    ["unknown-slide"], [f"slide-{index}" for index in range(10)],
])
def test_object_edit_planner_rejects_invalid_requested_scope_before_provider(monkeypatch, additional):
    deck, _planned = _multi_page_deck()
    calls = []

    async def unexpected_utility(**kwargs):
        calls.append(kwargs)
        raise AssertionError("invalid scope must not call a provider")

    monkeypatch.setattr("lantai.presentation.intelligence.run_structured_utility_task", unexpected_utility)
    with pytest.raises(ValueError, match="scope"):
        UtilityPresentationObjectEditPlanner("fixture-agent").plan(
            deck, "slide-target", "有界修改", additional_slide_ids=additional,
        )
    assert calls == []


@pytest.mark.parametrize("explicit,overscope", [
    (None, "additional"), (["slide-selected"], "additional"),
    (["slide-selected"], "intent"),
])
def test_object_edit_planner_rejects_model_invented_page_authority(monkeypatch, explicit, overscope):
    deck, _planned = _multi_page_deck()

    async def fake_utility(**kwargs):
        return StructuredUtilityResult(
            value=PresentationObjectEditPlan(
                slide_id="slide-target",
                additional_slide_ids=["slide-unselected"] if overscope == "additional" else [],
                actions=[ReplaceTextAction(object_id="title", text="不得接受越权")],
                intent_patch=SlideIntentPatch(slide_id="slide-unselected", key_message="越权判断")
                if overscope == "intent" else None,
            ), model_label="fixture/fixture", repaired=False,
        )

    monkeypatch.setattr("lantai.presentation.intelligence.run_structured_utility_task", fake_utility)
    kwargs = {} if explicit is None else {"additional_slide_ids": explicit}
    with pytest.raises((ValueError, PermissionError), match="scope"):
        UtilityPresentationObjectEditPlanner("fixture-agent").plan(
            deck, "slide-target", "只能修改指定页", **kwargs,
        )


def test_object_edit_planner_supplies_exact_source_ids_and_fact_contract(
    monkeypatch,
) -> None:
    deck = _deck()
    planned = DeckPlan(
        title=deck.title,
        narrative_arc="核对来源，不升级论断",
        visual_direction=VisualDirection(
            visual_concept="克制的来源核对",
            palette=["#111111", "#FFFFFF"],
            typography="清晰",
            image_language="无装饰图",
            chart_language="原生图表",
            shape_language="少量分隔线",
            grid="十二列",
            recurring_motif="来源标记",
        ),
        slides=[
            DeckPlanSlide(
                id="plan-target",
                order=1,
                title="收入判断",
                intent=SlideIntent(
                    id="intent-target",
                    purpose="核对收入",
                    key_message="甲高于乙",
                    required_facts=["甲为42万元"],
                    immutable_facts=["乙为38万元"],
                ),
            ),
        ],
    )
    calls = []

    async def fake_utility(**kwargs):
        calls.append(kwargs)
        return StructuredUtilityResult(
            value=PresentationObjectEditPlan(
                slide_id="slide-target",
                actions=[_chart_action()],
            ),
            model_label="fixture/fixture",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )

    UtilityPresentationObjectEditPlanner("fixture-agent").plan(
        deck,
        "slide-target",
        "加入已提供的成本数据，保留原有来源和端点说明。",
        deck_plan=planned,
    )

    payload = json.loads(calls[0]["user_prompt"])
    assert payload["allowed_source_refs"] == deck.source_refs
    assert payload["source_fact_catalog"] == [
        fact.model_dump(mode="json") for fact in deck.facts
    ]
    assert payload["fact_visibility_contract"] == {
        "facts": [
            {"path": "required_facts[0]", "text": "甲为42万元"},
            {"path": "immutable_facts[0]", "text": "乙为38万元"},
        ],
        "must_appear_verbatim_in_visible_native_objects": True,
        "speaker_notes_do_not_satisfy": True,
    }
    prompt = calls[0]["system_prompt"]
    assert "allowed_source_refs" in prompt
    assert "fact_visibility_contract" in prompt


@pytest.mark.parametrize("source_refs", [["undeclared-source"], []])
def test_chart_edit_rejects_undeclared_or_removed_source_atomically(
    source_refs,
) -> None:
    deck = _deck()
    before = deck.model_dump(mode="json")
    plan = PresentationObjectEditPlan(
        slide_id="slide-target",
        actions=[
            ReplaceTextAction(object_id="title", text="不得部分落盘"),
            _chart_action(source_refs),
        ],
    )
    code = "chart.source_invalid" if source_refs else "chart.source_missing"

    with pytest.raises(ValueError, match=code):
        apply_object_edit_plan(deck, plan)

    assert deck.model_dump(mode="json") == before


@pytest.mark.parametrize("source_refs", [["undeclared-source"], []])
def test_added_native_chart_obeys_the_same_source_boundary(source_refs) -> None:
    deck = _deck()
    before = deck.model_dump(mode="json")
    plan = PresentationObjectEditPlan(
        slide_id="slide-target",
        actions=[
            AddObjectAction(
                object=ChartObject(
                    id="added-chart",
                    categories=["甲", "乙"],
                    series={"收入": [42, 38]},
                    unit="万元",
                    derivation="来源直接数值",
                    source_refs=source_refs,
                ),
            ),
        ],
    )
    code = "chart.source_invalid" if source_refs else "chart.source_missing"

    with pytest.raises(ValueError, match=code):
        apply_object_edit_plan(deck, plan)

    assert deck.model_dump(mode="json") == before


@pytest.mark.parametrize(
    "source_refs", [None, ["material://approved-note"]],
)
def test_chart_edit_preserves_or_uses_an_exact_declared_source(source_refs) -> None:
    deck = _deck()
    plan = PresentationObjectEditPlan(
        slide_id="slide-target",
        actions=[_chart_action(source_refs)],
    )

    updated, changed = apply_object_edit_plan(deck, plan)

    chart = updated.slides[0].objects[1]
    assert changed == ["chart"]
    assert chart.source_refs == (
        source_refs if source_refs is not None
        else ["material://approved-data"]
    )
    assert deck.slides[0].objects[1].series == {"收入": [42, 38]}


def test_removing_an_invalid_chart_does_not_require_keeping_its_source() -> None:
    deck = _deck()
    deck.slides[0].objects[1].source_refs = ["legacy-invalid-source"]
    plan = PresentationObjectEditPlan(
        slide_id="slide-target",
        actions=[RemoveObjectAction(object_id="chart")],
    )

    updated, changed = apply_object_edit_plan(deck, plan)

    assert changed == ["chart"]
    assert all(obj.id != "chart" for obj in updated.slides[0].objects)


def test_unscoped_legacy_chart_source_updates_keep_existing_behavior() -> None:
    deck = _deck().model_copy(update={"source_refs": []})
    plan = PresentationObjectEditPlan(
        slide_id="slide-target",
        actions=[_chart_action(["fixture://approved-new-source"])],
    )

    updated, changed = apply_object_edit_plan(deck, plan)

    assert changed == ["chart"]
    assert updated.slides[0].objects[1].source_refs == [
        "fixture://approved-new-source",
    ]
