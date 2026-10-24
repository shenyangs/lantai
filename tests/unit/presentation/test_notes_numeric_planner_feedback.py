"""Model-authored note corrections keep source authority and bounded costs."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from lantai.app.structured_utility import StructuredTaskValidationError
from lantai.app.utility_model import UtilityTaskTimeoutError
from lantai.presentation.intelligence import UtilityPresentationObjectEditPlanner
from lantai.presentation.models import (
    DeckIR, DeckSlide, PresentationJobRequest, PresentationObjectEditPlan, TextObject,
)
from lantai.presentation.natural_edit import apply_object_edit_plan
from lantai.presentation.runtime import PresentationJobRuntime
from lantai.utils.operation_control import OperationControl, bind_operation


def _deck():
    return DeckIR(id="notes-feedback-fixture", title="合成备注反馈验证", slides=[
        DeckSlide(id="target", layout_id="freeform", objects=[
            TextObject(id="title", role="title", text="正文和来源不变"),
        ]),
        DeckSlide(id="selected", layout_id="freeform", speaker_notes="保留 42 项原始说明"),
        DeckSlide(id="unselected", layout_id="freeform", speaker_notes="原始未指定页说明"),
    ])


def _response(notes="按当前页的证据说明边界。", *, additional=(), patches=None):
    return {
        "slide_id": "target", "additional_slide_ids": list(additional), "actions": [],
        "speaker_notes_patches": patches if patches is not None else [{"speaker_notes": notes}],
    }


def _transport(monkeypatch, responses):
    calls = []

    async def generate(**kwargs):
        calls.append(kwargs)
        response = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(response, BaseException):
            raise response
        return SimpleNamespace(text=json.dumps(response, ensure_ascii=False), model_label="offline/notes")

    monkeypatch.setattr("lantai.app.structured_utility.run_utility_text_task", generate)
    return calls


def _plan(deck, *, reserve, **kwargs):
    return UtilityPresentationObjectEditPlanner("fixture-agent").plan(
        deck, "target", "仅补写已指定页备注，不改其他字段",
        retry_cost_reserver=reserve, **kwargs,
    )


@pytest.mark.parametrize("bad", [
    "PRIVATE_REJECTED_NOTE 三个类别。", "PRIVATE_REJECTED_NOTE 两条建议。",
    "PRIVATE_REJECTED_NOTE 第四页。", "PRIVATE_REJECTED_NOTE 42 项。",
    "PRIVATE_REJECTED_NOTE 3.5。", "PRIVATE_REJECTED_NOTE 99%。",
])
def test_production_structured_planner_returns_only_model_corrected_notes(monkeypatch, bad):
    deck = _deck()
    before = deck.model_dump_json()
    good = "先说明当前页的证据，再明确材料没有提供的结论。"
    calls = _transport(monkeypatch, [_response(bad), _response(good)])
    reserved = []
    result = _plan(deck, reserve=lambda: reserved.append(8))
    assert len(calls) == 2 and reserved == [8]
    assert result.speaker_notes_patches[0].speaker_notes == good
    changed, ids = apply_object_edit_plan(deck, result)
    assert changed.slides[0].speaker_notes == good and ids == []
    assert changed.slides[1:] == deck.slides[1:]
    first, second = [json.loads(call["user_prompt"]) for call in calls]
    assert "retry_contract_issues" not in first
    assert second["retry_contract_issues"] == [{
        "location": "$.speaker_notes_patches[0].speaker_notes", "violation_code": "notes_numeric_free",
    }]
    assert "中文数量" in second["retry_directive"]
    assert {k: v for k, v in second.items() if not k.startswith("retry_")} == first
    assert bad not in calls[1]["user_prompt"] and "PRIVATE_REJECTED_NOTE" not in calls[1]["user_prompt"]
    assert first["speaker_notes_edit_contract"]["numeric_expression_policy"]["chinese_quantities_forbidden"] is True
    assert deck.model_dump_json() == before


def test_numeric_contract_collects_all_rejected_positions_without_note_values():
    deck = _deck()
    before = deck.model_dump_json()
    plan = PresentationObjectEditPlan.model_validate(_response(additional=["selected"], patches=[
        {"speaker_notes": "PRIVATE_NOTE_ALPHA 三个类别。"},
        {"slide_id": "selected", "speaker_notes": "PRIVATE_NOTE_BETA 42 项。"},
    ]))
    with pytest.raises(ValueError, match="numeric-free") as failure:
        apply_object_edit_plan(deck, plan)
    assert failure.value.presentation_retry_contract_issues == [
        {"location": f"$.speaker_notes_patches[{index}].speaker_notes", "violation_code": "notes_numeric_free"}
        for index in range(2)
    ]
    assert "PRIVATE_NOTE" not in str(failure.value)
    assert deck.model_dump_json() == before


def test_invalid_notes_without_owned_retry_budget_fail_before_return(monkeypatch):
    deck = _deck()
    calls = _transport(monkeypatch, [_response("三个类别。")])
    with pytest.raises(ValueError, match="numeric-free"):
        UtilityPresentationObjectEditPlanner("fixture-agent").plan(deck, "target", "仅补备注")
    assert len(calls) == 1


def test_repeated_numeric_violation_is_bounded_and_never_locally_rewritten(monkeypatch):
    deck = _deck()
    before = deck.model_dump_json()
    bad = "PRIVATE_NEVER_PUBLISHED 三个类别。"
    calls = _transport(monkeypatch, [_response(bad)])
    reserved = []
    with pytest.raises(ValueError, match="numeric-free") as failure:
        _plan(deck, reserve=lambda: reserved.append(8))
    assert len(calls) == 2 and reserved == [8]
    assert "PRIVATE_NEVER_PUBLISHED" not in str(failure.value)
    assert deck.model_dump_json() == before


@pytest.mark.parametrize("notes", ["首先说明证据，随后交代边界。", "统一口径，一起核对。", "第一段讲述材料边界。"])
def test_nonnumeric_ordinary_chinese_does_not_trigger_repair(monkeypatch, notes):
    deck = _deck()
    calls = _transport(monkeypatch, [_response(notes)])
    reserved = []
    result = _plan(deck, reserve=lambda: reserved.append(8))
    assert result.speaker_notes_patches[0].speaker_notes == notes
    assert len(calls) == 1 and reserved == []


def test_unchanged_existing_numeric_notes_are_preserved_without_repair(monkeypatch):
    deck = _deck()
    response = _response(additional=["selected"], patches=[
        {"speaker_notes": "只解释当前页证据。"},
        {"slide_id": "selected", "speaker_notes": deck.slides[1].speaker_notes},
    ])
    calls = _transport(monkeypatch, [response])
    reserved = []
    result = _plan(deck, reserve=lambda: reserved.append(8), additional_slide_ids=["selected"])
    changed, _ = apply_object_edit_plan(deck, result)
    assert changed.slides[1:] == deck.slides[1:] and len(calls) == 1 and reserved == []


def test_scope_failure_is_not_given_numeric_retry_authority(monkeypatch):
    deck = _deck()
    calls = _transport(monkeypatch, [_response(additional=["unselected"], patches=[
        {"slide_id": "unselected", "speaker_notes": "三个越权目标。"},
    ])])
    reserved = []
    with pytest.raises(PermissionError, match="scope"):
        _plan(deck, reserve=lambda: reserved.append(8))
    assert len(calls) == 1 and reserved == []


def test_schema_failure_is_not_retried_or_sanitized(monkeypatch):
    deck = _deck()
    response = _response("三个类别。")
    response["actions"] = [{"kind": "replace_text", "object_id": "title", "text": "PRIVATE_MIXED_ACTION"}]
    calls = _transport(monkeypatch, [response])
    reserved = []
    with pytest.raises(StructuredTaskValidationError) as failure:
        _plan(deck, reserve=lambda: reserved.append(8))
    assert len(calls) == 1 and reserved == []
    assert "PRIVATE_MIXED_ACTION" not in str(failure.value)


def test_transport_timeout_is_not_an_identical_notes_retry(monkeypatch):
    deck = _deck()
    calls = _transport(monkeypatch, [TimeoutError("synthetic transport timeout")])
    reserved = []
    with pytest.raises(TimeoutError):
        _plan(deck, reserve=lambda: reserved.append(8))
    assert len(calls) == 1 and reserved == []


def test_budget_rejection_prevents_second_provider_call(monkeypatch):
    deck = _deck()
    before = deck.model_dump_json()
    calls = _transport(monkeypatch, [_response("三个类别。"), _response()])
    reservations = []

    def exhausted():
        reservations.append(8)
        raise RuntimeError("original aggregate case budget exhausted")

    with pytest.raises(RuntimeError, match="budget exhausted"):
        _plan(deck, reserve=exhausted)
    assert len(calls) == 1 and reservations == [8]
    assert deck.model_dump_json() == before


def test_correction_uses_remaining_local_timeout_not_fresh_timeout(monkeypatch):
    deck = _deck()
    calls = _transport(monkeypatch, [_response("三个类别。"), _response()])
    ticks = iter([100.0, 125.0])
    monkeypatch.setattr("lantai.presentation.intelligence.time.perf_counter", lambda: next(ticks))
    _plan(deck, reserve=lambda: None)
    assert [call["timeout_seconds"] for call in calls] == [90, 65]


def test_expired_local_timeout_does_not_reserve_or_retry(monkeypatch):
    deck = _deck()
    calls = _transport(monkeypatch, [_response("三个类别。")])
    ticks = iter([100.0, 191.0])
    monkeypatch.setattr("lantai.presentation.intelligence.time.perf_counter", lambda: next(ticks))
    reserved = []
    with pytest.raises(TimeoutError):
        _plan(deck, reserve=lambda: reserved.append(8))
    assert len(calls) == 1 and reserved == []


def test_operation_deadline_expires_before_retry_reservation(monkeypatch):
    deck = _deck()
    calls, reserved = [], []
    state = {"remaining": 7}

    async def generate(**kwargs):
        calls.append(kwargs)
        state["remaining"] = 0
        return SimpleNamespace(text=json.dumps(_response("三个类别。"), ensure_ascii=False), model_label="offline/notes")

    monkeypatch.setattr("lantai.app.structured_utility.run_utility_text_task", generate)
    control = OperationControl(owner_key=("presentation", "synthetic"), identity=("generation",),
        ensure_active=lambda: None, remaining_seconds=lambda: state["remaining"])
    with bind_operation(control), pytest.raises(TimeoutError, match="absolute deadline"):
        _plan(deck, reserve=lambda: reserved.append(8))
    assert len(calls) == 1 and calls[0]["timeout_seconds"] == 7 and reserved == []


def test_stop_prevents_correction_and_extra_reservation(monkeypatch):
    deck = _deck()
    calls, reserved = [], []
    state = {"stop": False}

    async def generate(**kwargs):
        calls.append(kwargs)
        state["stop"] = True
        return SimpleNamespace(text=json.dumps(_response("三个类别。"), ensure_ascii=False), model_label="offline/notes")

    def active():
        if state["stop"]:
            raise asyncio.CancelledError()

    monkeypatch.setattr("lantai.app.structured_utility.run_utility_text_task", generate)
    control = OperationControl(owner_key=("presentation", "synthetic"), identity=("generation",),
        ensure_active=active, remaining_seconds=lambda: 90)
    with bind_operation(control), pytest.raises(asyncio.CancelledError):
        _plan(deck, reserve=lambda: reserved.append(8))
    assert len(calls) == 1 and reserved == []


def test_stop_settles_inflight_correction_without_further_retry(monkeypatch):
    deck = _deck()
    calls, reserved = [], []
    state = {"correcting": False, "settled": False}

    async def generate(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return SimpleNamespace(text=json.dumps(_response("三个类别。"), ensure_ascii=False), model_label="offline/notes")
        state["correcting"] = True
        try:
            await asyncio.sleep(10)
        finally:
            state["settled"] = True
        raise AssertionError("stopped correction cannot return")

    def active():
        if state["correcting"]:
            raise asyncio.CancelledError()

    monkeypatch.setattr("lantai.app.structured_utility.run_utility_text_task", generate)
    control = OperationControl(owner_key=("presentation", "synthetic"), identity=("generation",),
        ensure_active=active, remaining_seconds=lambda: 90)
    with bind_operation(control), pytest.raises(asyncio.CancelledError):
        _plan(deck, reserve=lambda: reserved.append(8))
    assert len(calls) == 2 and reserved == [8] and state["settled"] is True


@pytest.mark.parametrize("budget,expected_calls,expected_cost", [(16, 2, 16), (8, 1, 8)])
def test_runtime_owned_planning_charges_each_call_before_dispatch(tmp_path, monkeypatch, budget, expected_calls, expected_cost):
    deck = _deck()
    before = deck.model_dump_json()
    calls = _transport(monkeypatch, [_response("三个类别。"), _response()])
    runtime = PresentationJobRuntime(tmp_path / "jobs", object_edit_planner=UtilityPresentationObjectEditPlanner("fixture-agent"))
    record = runtime.service.create_job(PresentationJobRequest(
        conversation_id="numeric-notes-fixture", user_request="只补备注", slide_count=3, cost_budget_units=budget,
    ))
    try:
        args = (record.job_id, record, deck, deck, None, "target", "仅补备注")
        if budget == 8:
            with pytest.raises(RuntimeError, match="budget"):
                runtime._prepare_edit_candidate(*args, required_slide_ids=["target"])
        else:
            result = runtime._prepare_edit_candidate(*args, required_slide_ids=["target"])
            assert result[8].slides[0].speaker_notes == "按当前页的证据说明边界。"
        assert len(calls) == expected_calls
        assert runtime.service.store.get(record.job_id).cost_used_units == expected_cost
        assert deck.model_dump_json() == before
    finally:
        runtime.shutdown()


def test_historical_injected_planner_receives_no_new_callback(tmp_path):
    deck = _deck()
    calls = []

    class HistoricalPlanner:
        def plan(self, current, page, instruction, *, deck_plan):
            calls.append((current, page, instruction, deck_plan))
            return PresentationObjectEditPlan.model_validate(_response())

    runtime = PresentationJobRuntime(tmp_path / "jobs", object_edit_planner=HistoricalPlanner())
    record = runtime.service.create_job(PresentationJobRequest(
        conversation_id="historical-notes-fixture", user_request="只补备注", slide_count=3,
    ))
    try:
        runtime._prepare_edit_candidate(record.job_id, record, deck, deck, None, "target", "仅补备注")
        assert len(calls) == 1 and runtime.service.store.get(record.job_id).cost_used_units == 8
    finally:
        runtime.shutdown()
