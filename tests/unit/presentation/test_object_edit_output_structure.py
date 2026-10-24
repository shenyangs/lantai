"""Plan metadata stays at the root; action schema failures remain fail-closed."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from lantai.app.structured_utility import StructuredTaskValidationError
from lantai.presentation.intelligence import UtilityPresentationObjectEditPlanner
from lantai.presentation.models import (
    DeckIR,
    DeckSlide,
    ObjectFrame,
    PresentationBrief,
    PresentationSourceSection,
    ShapeObject,
    TextObject,
)


def _inputs():
    deck = DeckIR(
        id="structure-fixture", title="结构契约",
        required_facts=["这是待验证的目标，不是已取得成果"],
        immutable_data=["不得改变原始口径"],
        source_refs=["material://structure-fixture"],
        slides=[
            DeckSlide(id=page, layout_id="freeform", objects=[
                TextObject(id=f"{page}-text", role="body", text="原正文"),
                ShapeObject(id=f"{page}-shape", shape_type="rect", text="原形状文字",
                            frame=ObjectFrame(x=1, y=2, w=3, h=1)),
            ])
            for page in ("primary", "selected")
        ],
    )
    brief = PresentationBrief(
        objective="只修订明确目标", audience="测试", narrative="来源绑定",
        source_refs=["material://structure-fixture"],
        source_sections=[PresentationSourceSection(
            title="原始材料", body="这是待验证的目标，不是已取得成果",
            source_ref="material://structure-fixture",
        )],
    )
    return deck, brief


def _transport(monkeypatch, response):
    calls = []

    async def offline_transport(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text=json.dumps(response, ensure_ascii=False),
                               model_label="offline/fixture")

    monkeypatch.setattr("lantai.app.structured_utility.run_utility_text_task", offline_transport)
    return calls


@pytest.mark.parametrize("multiple", [False, True])
def test_real_structured_boundary_supplies_explicit_root_and_flat_action_roles(monkeypatch, multiple):
    deck, brief = _inputs()
    before = (deck.model_dump_json(), brief.model_dump_json())
    metadata = {"slide_id": "primary", "key_message": "仍是待验证的目标"}
    response = {
        "slide_id": "primary", "additional_slide_ids": ["selected"] if multiple else [],
        "actions": [{"kind": "replace_text", "object_id": "primary-text", "text": "修订正文"}],
        "rationale": "对象变化与作者判断分别表达",
        **({"intent_patches": [metadata]} if multiple else {"intent_patch": metadata}),
    }
    if multiple:
        response["actions"].append({"kind": "set_shape_text", "object_id": "selected-shape", "text": "修订形状文字"})
    calls = _transport(monkeypatch, response)
    result = UtilityPresentationObjectEditPlanner("fixture-agent").plan(
        deck, "primary", "同时修正明确页面的正文和主判断", source_brief=brief,
        additional_slide_ids=["selected"] if multiple else None,
        required_slide_ids=["primary", "selected"] if multiple else ["primary"],
    )
    assert len(calls) == 1
    prompt = calls[0]["system_prompt"]
    for root_path in ("$.intent_patch", "$.intent_patches", "$.rationale"):
        assert root_path in prompt
    assert "$.actions[*]" in prompt
    assert "扁平" in prompt
    assert "JSON Schema:" in prompt
    assert calls[0]["structured_output_schema"]["additionalProperties"] is False
    payload = json.loads(calls[0]["user_prompt"])
    assert payload["source_brief"] == brief.model_dump(mode="json")
    assert payload["immutable_data"] == deck.immutable_data
    assert payload["required_facts"] == deck.required_facts
    assert payload["allowed_slide_ids"] == (["primary", "selected"] if multiple else ["primary"])
    assert result.rationale == response["rationale"]
    assert result.resolved_intent_patches()[0].key_message == metadata["key_message"]
    assert (deck.model_dump_json(), brief.model_dump_json()) == before


@pytest.mark.parametrize("kind", ["replace_text", "set_shape_text"])
@pytest.mark.parametrize("field", ["intent_patch", "intent_patches", "rationale"])
def test_nested_plan_metadata_is_not_discarded_rehomed_or_retried(monkeypatch, kind, field):
    deck, brief = _inputs()
    before = (deck.model_dump_json(), brief.model_dump_json())
    sentinel = "REJECTED_VALUE_MUST_NOT_APPEAR_IN_SAFE_ERROR"
    action = {"kind": kind, "object_id": f"primary-{'text' if kind == 'replace_text' else 'shape'}", "text": "实际改动"}
    metadata = {"slide_id": "primary", "key_message": sentinel}
    action[field] = ([metadata] if field == "intent_patches" else metadata if field == "intent_patch" else sentinel)
    calls = _transport(monkeypatch, {"slide_id": "primary", "actions": [action]})
    with pytest.raises(StructuredTaskValidationError) as failure:
        UtilityPresentationObjectEditPlanner("fixture-agent").plan(
            deck, "primary", "只改已授权原生对象", source_brief=brief,
        )
    assert len(calls) == 1
    assert failure.value.validation_issues == (f"extra_forbidden@$.actions[0].{kind}.{field}",)
    assert sentinel not in str(failure.value)
    assert (deck.model_dump_json(), brief.model_dump_json()) == before


def test_root_single_and_multi_intent_forms_stay_mutually_exclusive(monkeypatch):
    deck, brief = _inputs()
    metadata = {"slide_id": "primary", "key_message": "保持来源限定"}
    calls = _transport(monkeypatch, {
        "slide_id": "primary",
        "actions": [{"kind": "replace_text", "object_id": "primary-text", "text": "实际改动"}],
        "intent_patch": metadata, "intent_patches": [metadata],
    })
    before = deck.model_dump_json()
    with pytest.raises(StructuredTaskValidationError):
        UtilityPresentationObjectEditPlanner("fixture-agent").plan(
            deck, "primary", "不能同时使用两个主判断修订入口", source_brief=brief,
        )
    assert len(calls) == 1
    assert deck.model_dump_json() == before
