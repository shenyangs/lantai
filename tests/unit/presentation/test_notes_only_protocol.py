"""Explicit notes-only output protocol keeps source, scope and control intact."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from lantai.app.structured_utility import StructuredTaskValidationError
from lantai.app.utility_model import UtilityTaskIncompleteError
from lantai.presentation.intelligence import UtilityPresentationObjectEditPlanner
from lantai.presentation.models import DeckIR, DeckSlide, TextObject
from lantai.presentation.natural_edit import apply_object_edit_plan


def _deck():
    return DeckIR(id="compact-notes-fixture", title="合成讲稿协议验证", slides=[
        DeckSlide(id="primary", layout_id="freeform", objects=[
            TextObject(id="title", role="title", text="原生正文与事实保持不变"),
        ]),
        DeckSlide(id="selected", layout_id="freeform", speaker_notes="既有 42 项说明"),
        DeckSlide(id="optional", layout_id="freeform", speaker_notes="可选页原备注"),
    ])


def _proposal(notes="围绕当前页说明证据和结论边界。", *, additional=(), patches=None):
    return {"slide_id": "primary", "additional_slide_ids": list(additional),
        "speaker_notes_patches": patches if patches is not None else [{"speaker_notes": notes}],
        "rationale": "仅补充明确指定的讲稿"}


def _transport(monkeypatch, responses):
    calls = []

    async def generate(**kwargs):
        calls.append(kwargs)
        value = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(text=json.dumps(value, ensure_ascii=False), model_label="offline/compact")

    monkeypatch.setattr("lantai.app.structured_utility.run_utility_text_task", generate)
    return calls


def _plan(deck, **kwargs):
    return UtilityPresentationObjectEditPlanner("fixture-agent", notes_only=True).plan(
        deck, "primary", "只补写明确指定页讲稿，不修改正文、来源或其他页备注", **kwargs)


def _schema(call):
    return json.loads(call["system_prompt"].split("JSON Schema:\n", 1)[1])


def test_explicit_notes_protocol_removes_unrelated_action_schema_only(monkeypatch):
    calls = _transport(monkeypatch, [_proposal()])
    result = _plan(_deck())
    schema = _schema(calls[0])
    assert set(schema["properties"]) == {
        "slide_id", "additional_slide_ids", "speaker_notes_patches", "rationale",
    }
    assert set(schema.get("$defs", {})) == {"SlideSpeakerNotesPatch"}
    assert schema["additionalProperties"] is False
    assert schema["properties"]["speaker_notes_patches"]["minItems"] == 1
    assert schema["properties"]["speaker_notes_patches"]["maxItems"] == 20
    assert result.actions == [] and result.intent_patch is None and result.intent_patches == []
    assert result.speaker_notes_patches[0].speaker_notes == _proposal()["speaker_notes_patches"][0]["speaker_notes"]
    assert len(calls) == 1


def test_compact_mode_preserves_complete_input_context_and_instruction(monkeypatch):
    deck = _deck()
    before = deck.model_dump_json()
    calls = _transport(monkeypatch, [_proposal()])
    instruction = "只补写明确指定页讲稿，不修改正文、来源或其他页备注"
    UtilityPresentationObjectEditPlanner("fixture-agent").plan(
        deck, "primary", instruction, additional_slide_ids=["selected", "optional"])
    _plan(deck, additional_slide_ids=["selected", "optional"])
    old, new = [json.loads(call["user_prompt"]) for call in calls]
    assert new == old
    assert new["instruction"] == instruction
    assert new["additional_slides"][0]["slide"]["speaker_notes"] == "既有 42 项说明"
    assert len(new["additional_slides"]) == 2
    assert deck.model_dump_json() == before


@pytest.mark.parametrize("extra", [
    {"actions": []},
    {"actions": [{"kind": "replace_text", "object_id": "title", "text": "不得修改"}]},
    {"intent_patch": {"key_message": "不得新增"}},
    {"intent_patches": []},
    {"schema_version": "1.0"},
])
def test_compact_protocol_rejects_unrelated_fields_not_silently_dropping_them(monkeypatch, extra):
    calls = _transport(monkeypatch, [{**_proposal(), **extra}])
    before = _deck().model_dump_json()
    with pytest.raises(StructuredTaskValidationError):
        _plan(_deck())
    assert len(calls) == 1 and _deck().model_dump_json() == before


@pytest.mark.parametrize("proposal", [
    _proposal(patches=[]),
    _proposal(patches=[{"speaker_notes": "说明当前内容。"}, {"slide_id": "primary", "speaker_notes": "交代边界。"}]),
    _proposal(patches=[{"slide_id": "", "speaker_notes": "明确目标。"}]),
])
def test_compact_protocol_retains_material_and_unique_target_validation(monkeypatch, proposal):
    calls = _transport(monkeypatch, [proposal])
    with pytest.raises(StructuredTaskValidationError):
        _plan(_deck())
    assert len(calls) == 1


@pytest.mark.parametrize("proposal", [
    _proposal(patches=[{"slide_id": "selected", "speaker_notes": "说明材料边界。"}]),
    _proposal(additional=["optional"], patches=[{"slide_id": "optional", "speaker_notes": "说明材料边界。"}]),
])
def test_compact_mode_keeps_root_and_caller_permissions_fail_closed(monkeypatch, proposal):
    calls = _transport(monkeypatch, [proposal])
    reserved = []
    with pytest.raises(PermissionError):
        _plan(_deck(), additional_slide_ids=["selected"], retry_cost_reserver=lambda: reserved.append(8))
    assert len(calls) == 1 and reserved == []


def test_compact_feedback_is_model_owned_bounded_and_never_rewrites_values(monkeypatch):
    deck = _deck()
    before = deck.model_dump_json()
    bad = "PRIVATE_SYNTHETIC_4937 这一点值得说明。"
    good = "围绕主旨说明当前证据，随后交代材料未提供的结论。"
    calls = _transport(monkeypatch, [_proposal(bad), _proposal(good)])
    reserved = []
    result = _plan(deck, retry_cost_reserver=lambda: reserved.append(8))
    assert len(calls) == 2 and reserved == [8]
    first, second = [json.loads(call["user_prompt"]) for call in calls]
    assert _schema(calls[0]) == _schema(calls[1])
    assert second["retry_contract_issues"] == [{
        "location": "$.speaker_notes_patches[0].speaker_notes", "violation_code": "notes_numeric_free",
    }]
    assert "PRIVATE_SYNTHETIC_4937" not in calls[1]["user_prompt"] and bad not in calls[1]["user_prompt"]
    assert calls[1]["timeout_seconds"] <= calls[0]["timeout_seconds"]
    assert result.speaker_notes_patches[0].speaker_notes == good
    assert deck.model_dump_json() == before


def test_compact_failed_feedback_does_not_create_fallback_notes(monkeypatch):
    calls = _transport(monkeypatch, [_proposal("这一次仍有中文数量。")])
    reserved = []
    with pytest.raises(ValueError, match="numeric-free"):
        _plan(_deck(), retry_cost_reserver=lambda: reserved.append(8))
    assert len(calls) == 2 and reserved == [8]


def test_known_truncation_is_not_json_feedback_or_an_extra_provider_call(monkeypatch):
    calls = _transport(monkeypatch, [UtilityTaskIncompleteError(finish_reason="length")])
    reserved = []
    with pytest.raises(UtilityTaskIncompleteError):
        _plan(_deck(), retry_cost_reserver=lambda: reserved.append(8))
    assert len(calls) == 1 and reserved == []


def test_mode_is_explicit_and_generic_body_plan_is_unchanged(monkeypatch):
    calls = _transport(monkeypatch, [{"slide_id": "primary", "actions": [{
        "kind": "replace_text", "object_id": "title", "text": "讲稿说明仍在正文",
    }]}])
    deck = _deck()
    result = UtilityPresentationObjectEditPlanner("fixture-agent").plan(deck, "primary", "讲稿示例：仅修订标题正文")
    schema = _schema(calls[0])
    assert "actions" in schema["properties"] and len(schema["$defs"]) > 8
    changed, _ = apply_object_edit_plan(deck, result)
    assert changed.slides[0].objects[0].text == "讲稿说明仍在正文"
    assert [s.speaker_notes for s in changed.slides] == [s.speaker_notes for s in deck.slides]
    assert len(calls) == 1


def test_compact_optional_scope_is_not_mandatory_or_locally_populated(monkeypatch):
    calls = _transport(monkeypatch, [_proposal()])
    deck = _deck()
    result = _plan(deck, additional_slide_ids=["selected", "optional"])
    changed, _ = apply_object_edit_plan(deck, result)
    assert result.additional_slide_ids == []
    assert changed.slides[1:] == deck.slides[1:] and len(calls) == 1


@pytest.mark.parametrize("flag", ["notes", 1, None])
def test_mode_does_not_coerce_ambiguous_authority(flag):
    with pytest.raises(TypeError, match="notes_only"):
        UtilityPresentationObjectEditPlanner("fixture-agent", notes_only=flag)
