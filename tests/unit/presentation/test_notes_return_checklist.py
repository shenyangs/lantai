"""Lock model-owned return guidance; real model compliance needs a paid repro."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from lantai.presentation.intelligence import UtilityPresentationObjectEditPlanner
from lantai.presentation.models import DeckIR, DeckSlide, TextObject
from lantai.presentation.natural_edit import apply_object_edit_plan


def _deck():
    return DeckIR(id="notes-return-fixture", title="合成返回契约验证", slides=[
        DeckSlide(id="primary", layout_id="freeform", objects=[
            TextObject(id="title", role="title", text="原生正文与事实保持不变"),
        ]),
        DeckSlide(id="selected", layout_id="freeform", speaker_notes="既有 42 项说明"),
        DeckSlide(id="optional", layout_id="freeform", speaker_notes="可选页原备注"),
    ])


def _response(notes="围绕当前页说明证据和结论边界。", *, additional=(), patches=None):
    return {"slide_id": "primary", "additional_slide_ids": list(additional), "actions": [],
        "speaker_notes_patches": patches if patches is not None else [{"speaker_notes": notes}]}


def _transport(monkeypatch, responses):
    calls = []

    async def generate(**kwargs):
        calls.append(kwargs)
        value = responses[min(len(calls) - 1, len(responses) - 1)]
        return SimpleNamespace(text=json.dumps(value, ensure_ascii=False), model_label="offline/checklist")

    monkeypatch.setattr("lantai.app.structured_utility.run_utility_text_task", generate)
    return calls


def _plan(deck, **kwargs):
    return UtilityPresentationObjectEditPlanner("fixture-agent").plan(
        deck, "primary", "只补写明确指定页讲稿，不修改正文、来源或其他页备注", **kwargs)


@pytest.mark.parametrize("additional", [[], ["selected"], ["selected", "optional"]])
def test_return_checklist_declares_only_caller_allowed_root_scope(monkeypatch, additional):
    calls = _transport(monkeypatch, [_response()])
    _plan(_deck(), additional_slide_ids=additional)
    payload = json.loads(calls[0]["user_prompt"])
    checklist = payload["speaker_notes_return_checklist"]
    assert list(payload)[-1] == "speaker_notes_return_checklist"
    assert checklist["applies_only_when_speaker_notes_patches_used"] is True
    assert checklist["root_slide_id_must_match_input"] is True
    assert checklist["every_nonprimary_patch_target_must_be_in_root_additional_slide_ids"] is True
    assert checklist["root_additional_slide_ids_must_be_subset_of_input"] is True
    assert checklist["allowed_additional_slide_ids"] == additional
    assert "speaker_notes_return_checklist" in calls[0]["system_prompt"]


@pytest.mark.parametrize("phrase", ["一个", "这一点", "这一次", "第一页", "两条", "三个"])
def test_common_chinese_quantity_examples_reach_real_structured_call(monkeypatch, phrase):
    calls = _transport(monkeypatch, [_response()])
    _plan(_deck())
    checklist = json.loads(calls[0]["user_prompt"])["speaker_notes_return_checklist"]
    assert phrase in checklist["common_quantity_phrases_forbidden"]
    assert phrase in calls[0]["system_prompt"]
    assert checklist["new_or_changed_notes_numeric_free"] is True
    assert checklist["existing_unrequested_notes_preserved_verbatim"] is True
    assert checklist["notes_only_actions_empty_and_no_intent_patch"] is True


@pytest.mark.parametrize("ordinary", ["统一", "一起", "首先"])
def test_checklist_does_not_expand_numeric_gate_to_ordinary_words(monkeypatch, ordinary):
    original = ordinary + "说明材料边界。"
    calls = _transport(monkeypatch, [_response(original)])
    reserved = []
    result = _plan(_deck(), retry_cost_reserver=lambda: reserved.append(8))
    checklist = json.loads(calls[0]["user_prompt"])["speaker_notes_return_checklist"]
    assert ordinary in checklist["ordinary_nonnumeric_words_allowed"]
    assert result.speaker_notes_patches[0].speaker_notes == original
    assert len(calls) == 1 and reserved == []


def test_owned_retry_repeats_static_checklist_without_rejected_notes_or_values(monkeypatch):
    deck = _deck()
    before = deck.model_dump_json()
    bad = "PRIVATE_SYNTHETIC_REJECTED_4937 这一点值得说明。"
    corrected = "围绕主旨说明当前证据，随后交代材料未提供的结论。"
    calls = _transport(monkeypatch, [_response(bad), _response(corrected)])
    reserved = []
    result = _plan(deck, retry_cost_reserver=lambda: reserved.append(8))
    first, second = [json.loads(call["user_prompt"]) for call in calls]
    assert first["speaker_notes_return_checklist"] == second["speaker_notes_return_checklist"]
    assert "speaker_notes_return_checklist" in second["retry_directive"]
    for phrase in ["一个", "这一点", "这一次"]:
        assert phrase in second["retry_directive"]
    assert second["retry_contract_issues"] == [{
        "location": "$.speaker_notes_patches[0].speaker_notes", "violation_code": "notes_numeric_free"}]
    assert bad not in calls[1]["user_prompt"] and "PRIVATE_SYNTHETIC_REJECTED_4937" not in calls[1]["user_prompt"]
    assert result.speaker_notes_patches[0].speaker_notes == corrected
    assert len(calls) == 2 and reserved == [8] and deck.model_dump_json() == before


def test_root_scope_omission_still_fails_closed_without_permission_retry(monkeypatch):
    deck = _deck()
    before = deck.model_dump_json()
    calls = _transport(monkeypatch, [_response(patches=[
        {"speaker_notes": "说明当前页的证据。"},
        {"slide_id": "selected", "speaker_notes": "一个说明。"},
    ])])
    reserved = []
    with pytest.raises(PermissionError, match="declared notes scope"):
        _plan(deck, additional_slide_ids=["selected"], required_slide_ids=["primary", "selected"],
            retry_cost_reserver=lambda: reserved.append(8))
    assert len(calls) == 1 and reserved == [] and deck.model_dump_json() == before


def test_optional_scope_is_not_rewritten_to_satisfy_return_checklist(monkeypatch):
    deck = _deck()
    calls = _transport(monkeypatch, [_response()])
    result = _plan(deck, additional_slide_ids=["selected", "optional"])
    changed, _ = apply_object_edit_plan(deck, result)
    assert result.additional_slide_ids == []
    assert changed.slides[1:] == deck.slides[1:] and len(calls) == 1


def test_checklist_never_adds_notes_to_a_body_only_model_plan(monkeypatch):
    deck = _deck()
    response = {"slide_id": "primary", "actions": [
        {"kind": "replace_text", "object_id": "title", "text": "原生正文修订"}]}
    calls = _transport(monkeypatch, [response])
    result = UtilityPresentationObjectEditPlanner("fixture-agent").plan(deck, "primary", "只修改正文标题")
    changed, _ = apply_object_edit_plan(deck, result)
    assert result.speaker_notes_patches == []
    assert [slide.speaker_notes for slide in changed.slides] == [slide.speaker_notes for slide in deck.slides]
    assert len(calls) == 1


def test_failed_numeric_retry_never_becomes_a_locally_written_note(monkeypatch):
    deck = _deck()
    before = deck.model_dump_json()
    calls = _transport(monkeypatch, [_response("这一次仍含数量表达。")])
    reserved = []
    with pytest.raises(ValueError, match="numeric-free"):
        _plan(deck, retry_cost_reserver=lambda: reserved.append(8))
    assert len(calls) == 2 and reserved == [8] and deck.model_dump_json() == before


def test_checklist_does_not_grant_an_unpaid_correction(monkeypatch):
    deck = _deck()
    before = deck.model_dump_json()
    calls = _transport(monkeypatch, [_response("一个未通过的说明。")])
    with pytest.raises(ValueError, match="numeric-free"):
        _plan(deck)
    assert len(calls) == 1 and deck.model_dump_json() == before
