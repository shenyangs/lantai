"""Recover malformed notes items only within the existing owned correction."""

import asyncio
import json

import pytest

from lantai.app.structured_utility import StructuredTaskValidationError
from lantai.presentation.intelligence import UtilityPresentationObjectEditPlanner
from lantai.utils.operation_control import OperationControl, bind_operation

from tests.unit.presentation.test_notes_only_protocol import _deck, _plan, _proposal, _transport


def _malformed():
    return _proposal(patches=["PRIVATE_REJECTED_ITEM"])


def test_item_type_failure_uses_owned_correction_without_echoing_values(monkeypatch):
    deck = _deck()
    before = deck.model_dump_json()
    calls = _transport(monkeypatch, [_malformed(), _proposal()])
    reserved = []
    result = _plan(deck, retry_cost_reserver=lambda: reserved.append(8))
    assert len(calls) == 2 and reserved == [8]
    assert result.speaker_notes_patches[0].speaker_notes == _proposal()["speaker_notes_patches"][0]["speaker_notes"]
    first, second = [json.loads(call["user_prompt"]) for call in calls]
    assert {key: value for key, value in second.items() if not key.startswith("retry_")} == first
    assert second["retry_contract_issues"] == [{
        "location": "$.speaker_notes_patches[0]", "violation_code": "notes_patch_object_required",
    }]
    assert "PRIVATE_REJECTED_ITEM" not in calls[1]["user_prompt"]
    assert calls[0]["structured_output_schema"] == calls[1]["structured_output_schema"]
    assert deck.model_dump_json() == before


@pytest.mark.parametrize("bad", [
    {**_malformed(), "actions": []},
    _proposal(patches=[{"slide_id": "primary"}]),
    _proposal(patches=[{"speaker_notes": 42}]),
    _proposal(patches=["bad"] * 21),
    # More errors than the public 24-issue summary: extra fields stay rejected.
    {**_proposal(patches=[{"slide_id": 42}] * 20), "actions": []},
])
def test_other_schema_errors_are_not_given_retry_authority(monkeypatch, bad):
    calls = _transport(monkeypatch, [bad, _proposal()])
    reserved = []
    with pytest.raises(StructuredTaskValidationError):
        _plan(_deck(), retry_cost_reserver=lambda: reserved.append(8))
    assert len(calls) == 1 and reserved == []


@pytest.mark.parametrize("responses,exception", [
    ([_malformed(), _malformed()], StructuredTaskValidationError),
    ([_proposal("三个类别。"), _malformed()], StructuredTaskValidationError),
    ([_malformed(), _proposal("三个类别。")], ValueError),
    ([_malformed(), _proposal(additional=["optional"], patches=[
        {"slide_id": "optional", "speaker_notes": "说明证据。"},
    ])], PermissionError),
])
def test_second_response_is_fully_checked_without_third_call(monkeypatch, responses, exception):
    deck = _deck()
    before = deck.model_dump_json()
    calls = _transport(monkeypatch, [*responses, _proposal()])
    reserved = []
    with pytest.raises(exception):
        _plan(deck, retry_cost_reserver=lambda: reserved.append(8))
    assert len(calls) == 2 and reserved == [8]
    assert deck.model_dump_json() == before


@pytest.mark.parametrize("notes_only,reserve", [(False, True), (True, False)])
def test_default_wire_and_missing_retry_authority_remain_fail_closed(monkeypatch, notes_only, reserve):
    calls = _transport(monkeypatch, [_malformed(), _proposal()])
    reserved = []
    with pytest.raises(StructuredTaskValidationError):
        UtilityPresentationObjectEditPlanner("fixture-agent", notes_only=notes_only).plan(
            _deck(), "primary", "仅补备注",
            retry_cost_reserver=(lambda: reserved.append(8)) if reserve else None,
        )
    assert len(calls) == 1 and reserved == []


def test_schema_retry_budget_rejection_prevents_dispatch(monkeypatch):
    calls = _transport(monkeypatch, [_malformed(), _proposal()])

    def exhausted():
        raise RuntimeError("budget exhausted")

    with pytest.raises(RuntimeError, match="budget exhausted"):
        _plan(_deck(), retry_cost_reserver=exhausted)
    assert len(calls) == 1


def test_public_summary_without_complete_typed_cause_cannot_authorize_retry(monkeypatch):
    error = StructuredTaskValidationError(
        "synthetic safe summary", validation_issues=("model_type@$.speaker_notes_patches[0]",),
    )
    calls = _transport(monkeypatch, [error, _proposal()])
    reserved = []
    with pytest.raises(StructuredTaskValidationError):
        _plan(_deck(), retry_cost_reserver=lambda: reserved.append(8))
    assert len(calls) == 1 and reserved == []


@pytest.mark.parametrize("elapsed", [25, 91])
def test_schema_retry_consumes_remaining_local_timeout(monkeypatch, elapsed):
    calls = _transport(monkeypatch, [_malformed(), _proposal()])
    ticks = iter([100, 100 + elapsed])
    monkeypatch.setattr("lantai.presentation.intelligence.time.perf_counter", lambda: next(ticks))
    reserved = []
    if elapsed > 90:
        with pytest.raises(TimeoutError):
            _plan(_deck(), retry_cost_reserver=lambda: reserved.append(8))
        assert len(calls) == 1 and reserved == []
    else:
        _plan(_deck(), retry_cost_reserver=lambda: reserved.append(8))
        assert [call["timeout_seconds"] for call in calls] == [90, 65]
        assert reserved == [8]


@pytest.mark.parametrize("stop", [True, False])
def test_schema_retry_obeys_stop_and_absolute_deadline(monkeypatch, stop):
    calls = _transport(monkeypatch, [_malformed(), _proposal()])
    reserved = []

    def active():
        if calls and stop:
            raise asyncio.CancelledError()

    control = OperationControl(
        owner_key=("presentation", "synthetic"), identity=("schema",),
        ensure_active=active, remaining_seconds=lambda: 0 if calls and not stop else 90,
    )
    with bind_operation(control), pytest.raises(asyncio.CancelledError if stop else TimeoutError):
        _plan(_deck(), retry_cost_reserver=lambda: reserved.append(8))
    assert len(calls) == 1 and reserved == []
