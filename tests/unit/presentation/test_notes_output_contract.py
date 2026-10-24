"""Explicit lexical/length contracts feed one paid model correction, not a rewrite."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from lantai.app.utility_model import UtilityTaskIncompleteError
from lantai.presentation.intelligence import UtilityPresentationObjectEditPlanner
from lantai.presentation.models import DeckIR, DeckSlide, PresentationObjectEditPlan, TextObject
from lantai.presentation.natural_edit import apply_object_edit_plan
from lantai.utils.operation_control import OperationControl, bind_operation


def _contract(**kwargs):
    from lantai.presentation.models import SpeakerNotesOutputContract

    return SpeakerNotesOutputContract(**kwargs)


def _deck():
    return DeckIR(id="explicit-notes-fixture", title="合成显式讲稿限制", slides=[
        DeckSlide(id="primary", layout_id="freeform", objects=[
            TextObject(id="title", role="title", text="原生正文与事实保持不变"),
        ]),
        DeckSlide(id="selected", layout_id="freeform", speaker_notes="既有 42 项说明"),
        DeckSlide(id="optional", layout_id="freeform", speaker_notes="可选页原备注"),
    ])


def _proposal(notes="当前页证据边界", *, additional=(), patches=None):
    return {"slide_id": "primary", "additional_slide_ids": list(additional),
            "speaker_notes_patches": patches if patches is not None else [{"speaker_notes": notes}],
            "rationale": "只补充明确指定的讲稿"}


def _transport(monkeypatch, responses):
    calls = []

    async def generate(**kwargs):
        calls.append(kwargs)
        value = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(value, BaseException):
            raise value
        return SimpleNamespace(text=json.dumps(value, ensure_ascii=False), model_label="offline/contract")

    monkeypatch.setattr("lantai.app.structured_utility.run_utility_text_task", generate)
    return calls


def _plan(deck, contract, **kwargs):
    return UtilityPresentationObjectEditPlanner(
        "fixture-agent", notes_only=True, notes_output_contract=contract,
    ).plan(deck, "primary", "只补写指定页讲稿，原始材料及其他字段保持不变", **kwargs)


def test_contract_is_caller_owned_and_input_context_and_output_schema_are_preserved(monkeypatch):
    calls = _transport(monkeypatch, [_proposal()])
    deck = _deck()
    before = deck.model_dump_json()
    UtilityPresentationObjectEditPlanner("fixture-agent", notes_only=True).plan(
        deck, "primary", "只补写指定页讲稿，原始材料及其他字段保持不变",
        additional_slide_ids=["selected", "optional"],
    )
    contract = _contract(min_han_characters=5, max_han_characters=8, forbidden_characters=["一", "两"])
    result = _plan(deck, contract, additional_slide_ids=["selected", "optional"])
    old, new = [json.loads(call["user_prompt"]) for call in calls]
    assert new.pop("speaker_notes_output_contract") == contract.model_dump(mode="json")
    # Only caller-conflicting positive guidance changes; every context field,
    # negative example, scope boundary and existing note remains exact.
    old["speaker_notes_return_checklist"]["ordinary_nonnumeric_words_allowed"] = ["首先"]
    policy = old["speaker_notes_edit_contract"]["numeric_expression_policy"]
    policy.pop("ordinary_nonnumeric_words_not_banned")
    policy["ordinary_nonnumeric_words_subject_to_explicit_character_bans"] = True
    assert new == old
    assert calls[0]["system_prompt"].split("JSON Schema:\n", 1)[1] == calls[1]["system_prompt"].split("JSON Schema:\n", 1)[1]
    assert "speaker_notes_output_contract" in calls[1]["system_prompt"]
    assert result.speaker_notes_patches[0].speaker_notes == "当前页证据边界"
    assert deck.model_dump_json() == before


@pytest.mark.parametrize("forbidden,qualitative,ordinary", [
    (["一"], ["本页", "当前内容", "主旨", "随后"], ["首先"]),
    (["当"], ["本页", "主旨", "随后"], ["统一", "一起", "首先"]),
    (["首"], ["本页", "当前内容", "主旨", "随后"], ["统一", "一起"]),
    (["主"], ["本页", "当前内容", "随后"], ["统一", "一起", "首先"]),
    (["随"], ["本页", "当前内容", "主旨"], ["统一", "一起", "首先"]),
    (["统"], ["本页", "当前内容", "主旨", "随后"], ["一起", "首先"]),
    (["A"], ["本页", "当前内容", "主旨", "随后"], ["统一", "一起", "首先"]),
    (["本", "当", "主", "随", "统", "一", "首"], [], []),
])
def test_explicit_character_bans_compile_only_conflicting_positive_guidance(
    monkeypatch, forbidden, qualitative, ordinary,
):
    calls = _transport(monkeypatch, [_proposal("说明证据")])
    contract = _contract(forbidden_characters=forbidden)
    before = contract.model_dump(mode="json")
    result = _plan(_deck(), contract)
    payload = json.loads(calls[0]["user_prompt"])
    checklist = payload["speaker_notes_return_checklist"]
    assert checklist["qualitative_wording_options"] == qualitative
    assert checklist["ordinary_nonnumeric_words_allowed"] == ordinary
    assert payload["speaker_notes_edit_contract"]["numeric_expression_policy"] == {
        "arabic_numbers_and_percentages_forbidden": True,
        "chinese_quantities_forbidden": True,
        "qualitative_explanation_required": True,
        "unchanged_existing_notes_exempt": True,
        "ordinary_nonnumeric_words_subject_to_explicit_character_bans": True,
    }
    assert "统一、一起、首先等非数量词按既有约束允许；" not in calls[0]["system_prompt"]
    # Negative examples and complete input data are not output suggestions.
    assert checklist["common_quantity_phrases_forbidden"] == ["一个", "这一点", "这一次", "第一页", "两条", "三个"]
    assert payload["speaker_notes_output_contract"] == before
    assert contract.model_dump(mode="json") == before
    assert result.speaker_notes_patches[0].speaker_notes == "说明证据"
    assert len(calls) == 1


def test_length_only_contract_keeps_default_positive_guidance_and_wire_context(monkeypatch):
    calls = _transport(monkeypatch, [_proposal("说明证据")])
    deck = _deck()
    UtilityPresentationObjectEditPlanner("fixture-agent", notes_only=True).plan(
        deck, "primary", "只补写指定页讲稿，原始材料及其他字段保持不变",
    )
    contract = _contract(min_han_characters=4, max_han_characters=4)
    _plan(deck, contract)
    old, new = [json.loads(call["user_prompt"]) for call in calls]
    assert new.pop("speaker_notes_output_contract") == contract.model_dump(mode="json")
    assert new == old
    default_guidance = "统一、一起、首先等非数量词按既有约束允许；"
    assert all(default_guidance in call["system_prompt"] for call in calls)
    assert calls[0]["system_prompt"].split("JSON Schema:\n", 1)[1] == calls[1]["system_prompt"].split("JSON Schema:\n", 1)[1]


def test_owned_correction_reuses_compiled_guidance_without_restoring_conflicts(monkeypatch):
    calls = _transport(monkeypatch, [_proposal("统一说明"), _proposal("说明证据")])
    reserved = []
    result = _plan(_deck(), _contract(forbidden_characters=["一"]),
                   retry_cost_reserver=lambda: reserved.append(8))
    first, second = [json.loads(call["user_prompt"]) for call in calls]
    assert first["speaker_notes_return_checklist"]["ordinary_nonnumeric_words_allowed"] == ["首先"]
    assert {key: value for key, value in second.items() if not key.startswith("retry_")} == first
    assert calls[0]["system_prompt"] == calls[1]["system_prompt"]
    assert second["retry_contract_issues"] == [{
        "location": "$.speaker_notes_patches[0].speaker_notes",
        "violation_code": "notes_forbidden_character",
    }]
    assert result.speaker_notes_patches[0].speaker_notes == "说明证据"
    assert len(calls) == 2 and reserved == [8]


@pytest.mark.parametrize("count,valid", [(4, False), (5, True), (8, True), (9, False)])
def test_han_bounds_are_inclusive_and_only_count_note_values(monkeypatch, count, valid):
    note = "ASCII，" + "讲" * count + "。"
    calls = _transport(monkeypatch, [_proposal(note)])
    contract = _contract(min_han_characters=5, max_han_characters=8)
    if valid:
        assert _plan(_deck(), contract).speaker_notes_patches[0].speaker_notes == note
    else:
        with pytest.raises(ValueError, match="output contract") as failure:
            _plan(_deck(), contract)
        assert failure.value.presentation_retry_contract_issues == [{
            "location": "$.speaker_notes_patches[0].speaker_notes",
            "violation_code": "notes_han_length_below_minimum" if count < 5 else "notes_han_length_above_maximum",
        }]
    assert len(calls) == 1


def test_ordinary_words_are_allowed_by_default_but_explicit_character_bans_take_priority(monkeypatch):
    bad, good = "统一说明材料并核对证据。", "说明当前材料并核对证据。"
    calls = _transport(monkeypatch, [_proposal(bad), _proposal(bad), _proposal(good)])
    default = UtilityPresentationObjectEditPlanner("fixture-agent", notes_only=True).plan(_deck(), "primary", "仅补讲稿")
    assert default.speaker_notes_patches[0].speaker_notes == bad
    reserved = []
    result = _plan(_deck(), _contract(forbidden_characters=["一"]), retry_cost_reserver=lambda: reserved.append(8))
    assert result.speaker_notes_patches[0].speaker_notes == good
    assert len(calls) == 3 and reserved == [8]
    feedback = json.loads(calls[-1]["user_prompt"])
    assert feedback["retry_contract_issues"][0]["violation_code"] == "notes_forbidden_character"
    assert feedback["speaker_notes_output_contract"]["forbidden_characters"] == ["一"]


def test_combined_feedback_reports_all_scoped_numeric_length_and_character_failures_without_text(monkeypatch):
    deck = _deck()
    before = deck.model_dump_json()
    private = "PRIVATE_REJECTED_SYNTHETIC"
    bad = _proposal(additional=["selected"], patches=[
        {"speaker_notes": private + "统一说明42项" + "讲" * 14},
        {"slide_id": "selected", "speaker_notes": private + "一条"},
    ])
    good = _proposal(additional=["selected"], patches=[
        {"speaker_notes": "当前页证据边界"},
        {"slide_id": "selected", "speaker_notes": "说明材料支持范围"},
    ])
    calls = _transport(monkeypatch, [bad, good])
    contract = _contract(min_han_characters=5, max_han_characters=12, forbidden_characters=["一"])
    reserved = []
    result = _plan(deck, contract, additional_slide_ids=["selected"],
                   required_slide_ids=["primary", "selected"], retry_cost_reserver=lambda: reserved.append(8))
    assert len(calls) == 2 and reserved == [8]
    first, second = [json.loads(call["user_prompt"]) for call in calls]
    assert {k: v for k, v in second.items() if not k.startswith("retry_")} == first
    observed = {(item["location"], item["violation_code"]) for item in second["retry_contract_issues"]}
    expected = {(f"$.speaker_notes_patches[{i}].speaker_notes", code) for i, bound in [
        (0, "notes_han_length_above_maximum"), (1, "notes_han_length_below_minimum"),
    ] for code in ["notes_numeric_free", bound, "notes_forbidden_character"]}
    assert observed == expected
    assert all(set(item) == {"location", "violation_code"} for item in second["retry_contract_issues"])
    assert private not in calls[-1]["user_prompt"]
    assert calls[1]["timeout_seconds"] <= calls[0]["timeout_seconds"] == 180
    assert result.speaker_notes_patches == PresentationObjectEditPlan.model_validate(good).speaker_notes_patches
    changed, ids = apply_object_edit_plan(deck, result)
    assert ids == [] and changed.slides[2] == deck.slides[2]
    assert changed.slides[0].objects == deck.slides[0].objects
    assert deck.model_dump_json() == before


def test_unchanged_existing_notes_are_exempt_from_new_output_limits(monkeypatch):
    deck = _deck()
    calls = _transport(monkeypatch, [_proposal(additional=["selected"], patches=[
        {"speaker_notes": "当前页证据边界"},
        {"slide_id": "selected", "speaker_notes": deck.slides[1].speaker_notes},
    ])])
    reserved = []
    result = _plan(deck, _contract(min_han_characters=7, max_han_characters=7, forbidden_characters=["既"]),
                   additional_slide_ids=["selected"], retry_cost_reserver=lambda: reserved.append(8))
    changed, _ = apply_object_edit_plan(deck, result)
    assert changed.slides[1:] == deck.slides[1:] and len(calls) == 1 and reserved == []


def test_explicit_correction_measures_all_changed_pages_without_rejected_text(monkeypatch):
    deck = _deck()
    before = deck.model_dump_json()
    private = "PRIVATE_REJECTED_SYNTHETIC"
    bad = _proposal(additional=["selected", "optional"], patches=[
        {"speaker_notes": private + "两统一说明42项" + "讲" * 14},
        {"slide_id": "selected", "speaker_notes": private + "一条"},
        {"slide_id": "optional", "speaker_notes": "说明当前材料支持范围"},
    ])
    good = _proposal(additional=["selected", "optional"], patches=[
        {"speaker_notes": "当前页证据边界"},
        {"slide_id": "selected", "speaker_notes": "说明材料支持范围"},
        {"slide_id": "optional", "speaker_notes": "说明当前材料支持范围"},
    ])
    calls = _transport(monkeypatch, [bad, good])
    contract = _contract(min_han_characters=5, max_han_characters=12, forbidden_characters=["两", "一"])
    contract_before = contract.model_dump(mode="json")
    reserved = []
    result = _plan(deck, contract, additional_slide_ids=["selected", "optional"],
                   required_slide_ids=["primary", "selected", "optional"],
                   retry_cost_reserver=lambda: reserved.append(8))
    first, second = [json.loads(call["user_prompt"]) for call in calls]
    assert "retry_note_measurements" not in first
    assert second["retry_note_measurements"] == [
        {"location": "$.speaker_notes_patches[0].speaker_notes", "slide_id": "primary",
         "han_characters": 20, "ascii_digit_characters": 2, "numeric_free": False,
         "forbidden_character_counts": [{"character": "两", "count": 1}, {"character": "一", "count": 1}]},
        {"location": "$.speaker_notes_patches[1].speaker_notes", "slide_id": "selected",
         "han_characters": 2, "ascii_digit_characters": 0, "numeric_free": False,
         "forbidden_character_counts": [{"character": "一", "count": 1}]},
        {"location": "$.speaker_notes_patches[2].speaker_notes", "slide_id": "optional",
         "han_characters": 10, "ascii_digit_characters": 0, "numeric_free": True,
         "forbidden_character_counts": []},
    ]
    assert {key: value for key, value in second.items() if not key.startswith("retry_")} == first
    assert all(set(issue) == {"location", "violation_code"} for issue in second["retry_contract_issues"])
    assert private not in calls[1]["user_prompt"]
    assert calls[0]["system_prompt"] == calls[1]["system_prompt"]
    assert calls[0]["system_prompt"].split("JSON Schema:\n", 1)[1] == calls[1]["system_prompt"].split("JSON Schema:\n", 1)[1]
    assert contract.model_dump(mode="json") == contract_before
    assert result.speaker_notes_patches == PresentationObjectEditPlan.model_validate(good).speaker_notes_patches
    assert deck.model_dump_json() == before and len(calls) == 2 and reserved == [8]


@pytest.mark.parametrize("count", [4, 9])
def test_explicit_terminal_error_has_safe_counts_but_exempts_unchanged_notes(monkeypatch, count):
    deck = _deck()
    private = "PRIVATE_TERMINAL_SYNTHETIC"
    calls = _transport(monkeypatch, [_proposal(additional=["selected"], patches=[
        {"speaker_notes": private + "讲" * count + "，。"},
        {"slide_id": "selected", "speaker_notes": deck.slides[1].speaker_notes},
    ])])
    with pytest.raises(ValueError, match="output contract") as failure:
        _plan(deck, _contract(min_han_characters=5, max_han_characters=8, forbidden_characters=["既"]),
              additional_slide_ids=["selected"])
    assert failure.value.presentation_retry_note_measurements == [
        {"location": "$.speaker_notes_patches[0].speaker_notes", "slide_id": "primary",
         "han_characters": count, "ascii_digit_characters": 0, "numeric_free": True,
         "forbidden_character_counts": []},
    ]
    assert private not in str(failure.value)
    assert private not in json.dumps(failure.value.presentation_retry_note_measurements)
    assert len(calls) == 1


def test_legacy_numeric_correction_does_not_receive_explicit_contract_measurements(monkeypatch):
    calls = _transport(monkeypatch, [_proposal("说明42项材料"), _proposal()])
    reserved = []
    result = UtilityPresentationObjectEditPlanner("fixture-agent", notes_only=True).plan(
        _deck(), "primary", "仅补讲稿", retry_cost_reserver=lambda: reserved.append(8),
    )
    first, second = [json.loads(call["user_prompt"]) for call in calls]
    assert "retry_note_measurements" not in first and "retry_note_measurements" not in second
    assert {key: value for key, value in second.items() if not key.startswith("retry_")} == first
    assert second["retry_contract_issues"] == [{
        "location": "$.speaker_notes_patches[0].speaker_notes", "violation_code": "notes_numeric_free",
    }]
    assert result.speaker_notes_patches[0].speaker_notes == "当前页证据边界"
    assert len(calls) == 2 and reserved == [8]


def test_observed_five_page_failure_keeps_full_scope_one_retry_and_no_local_repair(monkeypatch):
    deck = _deck()
    deck.slides.extend([
        DeckSlide(id="extra", layout_id="freeform"),
        DeckSlide(id="last", layout_id="freeform"),
    ])
    before = deck.model_dump_json()
    targets = [slide.id for slide in deck.slides]
    observed_first = [(106, 3), (91, 0), (96, 1), (103, 2), (103, 2)]
    observed_second = [(219, 5), (163, 4), (154, 4), (155, 3), (132, 2)]

    def synthetic_proposal(counts):
        return _proposal(additional=targets[1:], patches=[
            {"slide_id": target, "speaker_notes": "一" * banned + "讲" * (han - banned)}
            for target, (han, banned) in zip(targets, counts, strict=True)
        ])

    calls = _transport(monkeypatch, [synthetic_proposal(observed_first), synthetic_proposal(observed_second)])
    reserved = []
    with pytest.raises(ValueError, match="output contract") as failure:
        _plan(deck, _contract(min_han_characters=80, max_han_characters=160,
                            forbidden_characters=list("零〇一二两三四五六七八九十百千万亿")),
              additional_slide_ids=targets[1:], required_slide_ids=targets,
              retry_cost_reserver=lambda: reserved.append(8))
    first, second = [json.loads(call["user_prompt"]) for call in calls]
    assert {key: value for key, value in second.items() if not key.startswith("retry_")} == first
    assert [item["slide_id"] for item in second["retry_note_measurements"]] == targets
    assert [item["han_characters"] for item in second["retry_note_measurements"]] == [han for han, _ in observed_first]
    assert [sum(hit["count"] for hit in item["forbidden_character_counts"])
            for item in second["retry_note_measurements"]] == [banned for _, banned in observed_first]
    assert not any(item["ascii_digit_characters"] for item in second["retry_note_measurements"])
    assert [item["han_characters"] for item in failure.value.presentation_retry_note_measurements] == [
        han for han, _ in observed_second
    ]
    assert len(calls) == 2 and reserved == [8] and deck.model_dump_json() == before


@pytest.mark.parametrize("reserve", [False, True])
def test_no_unowned_or_third_attempt_and_no_fallback_notes(monkeypatch, reserve):
    deck = _deck()
    before = deck.model_dump_json()
    bad = "PRIVATE_NOT_PUBLISHED 统一讲述。"
    calls = _transport(monkeypatch, [_proposal(bad)])
    reserved = []
    with pytest.raises(ValueError, match="output contract") as failure:
        _plan(deck, _contract(forbidden_characters=["一"]),
              retry_cost_reserver=(lambda: reserved.append(8)) if reserve else None)
    assert len(calls) == (2 if reserve else 1) and reserved == ([8] if reserve else [])
    assert "PRIVATE_NOT_PUBLISHED" not in str(failure.value)
    assert deck.model_dump_json() == before


def test_scope_rejection_does_not_acquire_output_feedback_authority(monkeypatch):
    calls = _transport(monkeypatch, [_proposal(additional=["optional"], patches=[
        {"slide_id": "optional", "speaker_notes": "一条"},
    ])])
    reserved = []
    with pytest.raises(PermissionError):
        _plan(_deck(), _contract(min_han_characters=5, forbidden_characters=["一"]),
              additional_slide_ids=["selected"], retry_cost_reserver=lambda: reserved.append(8))
    assert len(calls) == 1 and reserved == []


def test_truncation_is_not_an_output_contract_retry(monkeypatch):
    calls = _transport(monkeypatch, [UtilityTaskIncompleteError(finish_reason="length")])
    reserved = []
    with pytest.raises(UtilityTaskIncompleteError):
        _plan(_deck(), _contract(min_han_characters=5), retry_cost_reserver=lambda: reserved.append(8))
    assert len(calls) == 1 and reserved == []


def test_budget_rejection_stops_before_second_call(monkeypatch):
    calls = _transport(monkeypatch, [_proposal("统一说明")])
    reserved = []

    def exhausted():
        reserved.append(8)
        raise RuntimeError("original aggregate budget exhausted")

    with pytest.raises(RuntimeError, match="budget exhausted"):
        _plan(_deck(), _contract(forbidden_characters=["一"]), retry_cost_reserver=exhausted)
    assert len(calls) == 1 and reserved == [8]


@pytest.mark.parametrize("elapsed,expected", [(25, 65), (91, None)])
def test_output_correction_keeps_remaining_local_timeout(monkeypatch, elapsed, expected):
    calls = _transport(monkeypatch, [_proposal("统一说明"), _proposal()])
    ticks = iter([100.0, 100.0 + elapsed])
    monkeypatch.setattr("lantai.presentation.intelligence.time.perf_counter", lambda: next(ticks))
    reserved = []
    contract = _contract(forbidden_characters=["一"])
    if expected is None:
        with pytest.raises(TimeoutError):
            _plan(_deck(), contract, retry_cost_reserver=lambda: reserved.append(8))
        assert len(calls) == 1 and reserved == []
    else:
        _plan(_deck(), contract, retry_cost_reserver=lambda: reserved.append(8))
        assert [call["timeout_seconds"] for call in calls] == [90, expected] and reserved == [8]


@pytest.mark.parametrize("end", ["stop", "deadline"])
def test_operation_stop_and_deadline_prevent_extra_reservation(monkeypatch, end):
    calls, reserved = [], []
    state = {"active": True}

    async def generate(**kwargs):
        calls.append(kwargs)
        state["active"] = False
        return SimpleNamespace(text=json.dumps(_proposal("统一说明"), ensure_ascii=False), model_label="offline/contract")

    def active():
        if end == "stop" and not state["active"]:
            raise asyncio.CancelledError()

    monkeypatch.setattr("lantai.app.structured_utility.run_utility_text_task", generate)
    control = OperationControl(owner_key=("presentation", "synthetic"), identity=("generation",),
        ensure_active=active, remaining_seconds=lambda: 7 if state["active"] or end == "stop" else 0)
    failure = asyncio.CancelledError if end == "stop" else TimeoutError
    with bind_operation(control), pytest.raises(failure):
        _plan(_deck(), _contract(forbidden_characters=["一"]), retry_cost_reserver=lambda: reserved.append(8))
    assert len(calls) == 1 and reserved == []


def test_planner_snapshots_and_revalidates_explicit_caller_contract(monkeypatch):
    contract = _contract(forbidden_characters=["一"])
    planner = UtilityPresentationObjectEditPlanner("fixture-agent", notes_only=True, notes_output_contract=contract)
    contract.forbidden_characters.clear()
    calls = _transport(monkeypatch, [_proposal("统一说明")])
    with pytest.raises(ValueError, match="output contract"):
        planner.plan(_deck(), "primary", "仅补讲稿")
    assert json.loads(calls[0]["user_prompt"])["speaker_notes_output_contract"]["forbidden_characters"] == ["一"]
    from lantai.presentation.models import SpeakerNotesOutputContract

    invalid = SpeakerNotesOutputContract.model_construct(min_han_characters=-1)
    with pytest.raises(ValueError, match="notes_output_contract"):
        UtilityPresentationObjectEditPlanner("fixture-agent", notes_only=True, notes_output_contract=invalid)


@pytest.mark.parametrize("payload", [
    {"min_han_characters": -1}, {"min_han_characters": True}, {"max_han_characters": "8"},
    {"max_han_characters": 6001}, {"min_han_characters": 9, "max_han_characters": 8},
    {"forbidden_characters": "一"}, {"forbidden_characters": ["一条"]},
    {"forbidden_characters": [""]}, {"forbidden_characters": ["\n"]},
    {"forbidden_characters": ["一", "一"]}, {"forbidden_characters": [1]},
    {"forbidden_characters": [chr(0x4e00 + i) for i in range(129)]}, {"unrecognized": "private"},
])
def test_explicit_output_contract_rejects_coercion_unknown_fields_and_unbounded_limits(payload):
    with pytest.raises(ValidationError):
        _contract(**payload)


@pytest.mark.parametrize("contract", [{}, {"min_han_characters": 5}, "private", 1])
def test_constructor_requires_typed_explicit_contract(contract):
    with pytest.raises(TypeError, match="notes_output_contract"):
        UtilityPresentationObjectEditPlanner("fixture-agent", notes_only=True, notes_output_contract=contract)


def test_output_contract_requires_explicit_notes_only_authority():
    with pytest.raises(ValueError, match="notes_only"):
        UtilityPresentationObjectEditPlanner("fixture-agent", notes_output_contract=_contract(min_han_characters=5))


@pytest.mark.parametrize("limits,bad", [
    ({"min_han_characters": 5, "max_han_characters": 8, "forbidden_characters": ["两", "一"]}, "一条"),
    ({"min_han_characters": 5}, "讲"),
    ({"max_han_characters": 8, "forbidden_characters": ["\\", '"']}, "讲" * 9),
])
def test_field_checks_repeat_only_owned_value_requirements_on_explicit_correction(monkeypatch, limits, bad):
    deck = _deck()
    before = deck.model_dump_json()
    private = "PRIVATE_REJECTED_SYNTHETIC"
    contract = _contract(**limits)
    contract_before = contract.model_dump(mode="json")
    calls = _transport(monkeypatch, [_proposal(private + bad), _proposal("说明证据当前")])
    reserved = []
    result = _plan(deck, contract, retry_cost_reserver=lambda: reserved.append(8))
    first, second = [json.loads(call["user_prompt"]) for call in calls]
    assert "retry_speaker_notes_value_checks" not in first
    assert second["retry_speaker_notes_value_checks"] == {
        "field": "speaker_notes_patches[*].speaker_notes",
        "applies_to": "new_or_changed_note_values_only",
        "han_codepoint_range_inclusive": ["U+4E00", "U+9FFF"],
        "min_han_characters_inclusive": contract.min_han_characters,
        "max_han_characters_inclusive": contract.max_han_characters,
        "ascii_digit_character_count_required": 0,
        "all_caller_forbidden_character_counts_required": [
            {"character": character, "count": 0} for character in contract.forbidden_characters
        ],
        "unchanged_existing_notes_exempt": True,
        "json_slide_ids_not_note_values": True,
    }
    assert list(second)[-1] == "retry_speaker_notes_value_checks"
    assert "retry_speaker_notes_value_checks" in second["retry_directive"]
    assert private not in calls[-1]["user_prompt"]
    assert {key: value for key, value in second.items() if not key.startswith("retry_")} == first
    assert calls[0]["system_prompt"] == calls[1]["system_prompt"]
    assert result.speaker_notes_patches[0].speaker_notes == "说明证据当前"
    assert deck.model_dump_json() == before and contract.model_dump(mode="json") == contract_before
    assert len(calls) == 2 and reserved == [8]


def test_field_checks_do_not_leak_into_default_numeric_correction(monkeypatch):
    calls = _transport(monkeypatch, [_proposal("说明42项材料"), _proposal()])
    reserved = []
    UtilityPresentationObjectEditPlanner("fixture-agent", notes_only=True).plan(
        _deck(), "primary", "仅补讲稿", retry_cost_reserver=lambda: reserved.append(8),
    )
    assert all("retry_speaker_notes_value_checks" not in json.loads(call["user_prompt"]) for call in calls)
    assert len(calls) == 2 and reserved == [8]


def test_field_checks_keep_observed_length_success_with_forbidden_words_fail_closed(monkeypatch):
    deck = _deck()
    deck.slides.extend([DeckSlide(id="extra", layout_id="freeform"), DeckSlide(id="last", layout_id="freeform")])
    before = deck.model_dump_json()
    targets = [slide.id for slide in deck.slides]
    observed_first = [(159, 5), (171, 4), (152, 5), (170, 5), (186, 3)]
    observed_second = [(122, 5), (123, 0), (124, 1), (103, 1), (122, 0)]
    forbidden = list("零〇一二两三四五六七八九十百千万亿")

    def proposal(counts):
        return _proposal(additional=targets[1:], patches=[
            {"slide_id": target, "speaker_notes": "一" * banned + "讲" * (han - banned)}
            for target, (han, banned) in zip(targets, counts, strict=True)
        ])

    calls = _transport(monkeypatch, [proposal(observed_first), proposal(observed_second)])
    reserved = []
    with pytest.raises(ValueError, match="output contract") as failure:
        _plan(deck, _contract(min_han_characters=80, max_han_characters=160, forbidden_characters=forbidden),
              additional_slide_ids=targets[1:], required_slide_ids=targets,
              retry_cost_reserver=lambda: reserved.append(8))
    first, second = [json.loads(call["user_prompt"]) for call in calls]
    checks = second["retry_speaker_notes_value_checks"]
    assert checks["all_caller_forbidden_character_counts_required"] == [
        {"character": character, "count": 0} for character in forbidden
    ]
    assert checks["min_han_characters_inclusive"] == 80 and checks["max_han_characters_inclusive"] == 160
    assert second["required_slide_ids"] == first["required_slide_ids"] == targets
    assert {key: value for key, value in second.items() if not key.startswith("retry_")} == first
    assert [item["han_characters"] for item in failure.value.presentation_retry_note_measurements] == [
        han for han, _ in observed_second
    ]
    assert all(item["violation_code"] != "notes_han_length_above_maximum"
               for item in failure.value.presentation_retry_contract_issues)
    assert {item["location"] for item in failure.value.presentation_retry_contract_issues} == {
        "$.speaker_notes_patches[0].speaker_notes", "$.speaker_notes_patches[2].speaker_notes",
        "$.speaker_notes_patches[3].speaker_notes",
    }
    assert len(calls) == 2 and reserved == [8] and deck.model_dump_json() == before


def test_field_checks_are_not_added_to_successful_first_explicit_proposal(monkeypatch):
    calls = _transport(monkeypatch, [_proposal("说明证据当前")])
    reserved = []
    _plan(_deck(), _contract(min_han_characters=5, max_han_characters=8, forbidden_characters=["一"]),
          retry_cost_reserver=lambda: reserved.append(8))
    assert "retry_speaker_notes_value_checks" not in json.loads(calls[0]["user_prompt"])
    assert len(calls) == 1 and reserved == []
