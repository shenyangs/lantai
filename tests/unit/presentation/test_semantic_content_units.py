"""Single-owner content/claim transport without weakening semantic gates."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

import lantai.presentation.intelligence as intelligence
from lantai.app.structured_utility import (
    StructuredTaskValidationError,
    StructuredUtilityResult,
    _parse_validated,
)
from lantai.presentation.models import InputMaterial, PresentationJobRequest
from lantai.presentation.page_authoring import PresentationSemanticContentError
from lantai.presentation.planner import build_brief, build_deterministic_fixture_plan


def _unit(label: str, *claim_ids: str) -> dict[str, object]:
    return {"label": label, "claim_ids": list(claim_ids)}


def test_content_unit_transport_schema_has_one_owner_for_label_and_claim_ids():
    schema = intelligence._SemanticSlideBatch.model_json_schema()
    assert schema["x-lantai-content-unit-transport"] == 2
    slide_ref = schema["properties"]["slides"]["items"]["$ref"]
    slide = schema["$defs"][slide_ref.rsplit("/", 1)[-1]]
    assert "content_units" in slide["properties"]
    assert "content_units" in slide["required"]
    assert "content_blocks" not in slide["properties"]
    assert "claim_groups" not in slide["properties"]
    units = slide["properties"]["content_units"]
    assert units["minItems"] == 1
    assert units["maxItems"] == 8
    unit_ref = units["items"]["$ref"]
    unit = schema["$defs"][unit_ref.rsplit("/", 1)[-1]]
    assert set(unit["properties"]) == {"label", "claim_ids"}
    assert set(unit["required"]) == {"label", "claim_ids"}
    assert unit["additionalProperties"] is False
    assert unit["properties"]["claim_ids"]["minItems"] == 1
    assert unit["properties"]["claim_ids"]["maxItems"] == 8


def test_content_units_preserve_author_order_and_grouping_in_legacy_runtime():
    detail = intelligence._SemanticSlideDetail.model_validate({
        "order": 1,
        "content_units": [
            _unit("合并判断", "claim-002", "claim-001"),
            _unit("边界", "claim-003"),
        ],
    })
    assert detail.content_blocks == ["合并判断", "边界"]
    assert [group.claim_ids for group in detail.claim_groups] == [
        ["claim-002", "claim-001"], ["claim-003"],
    ]
    assert intelligence._SemanticSlideDetail.model_validate(detail.model_dump()) == detail


@pytest.mark.parametrize("legacy_field", ["content_blocks", "claim_groups"])
def test_content_units_reject_mixed_or_ambiguous_representations(legacy_field):
    with pytest.raises(ValidationError):
        intelligence._SemanticSlideDetail.model_validate({
            "order": 1,
            "content_units": [_unit("判断", "claim-001")],
            legacy_field: [],
        })


@pytest.mark.parametrize("units", [
    [],
    [_unit("判断", "claim-001")] * 9,
    [_unit("判断")],
    [_unit("判断", *[f"claim-{index:03d}" for index in range(1, 10)])],
    [{"label": "判断", "claim_ids": ["claim-001"], "unexpected": True}],
    [{"claim_ids": ["claim-001"]}],
    [{"label": "判断"}],
    [{"label": ["判断"], "claim_ids": ["claim-001"]}],
])
def test_content_units_keep_strict_schema_and_capacity(units):
    with pytest.raises(ValidationError):
        intelligence._SemanticSlideDetail.model_validate({
            "order": 1, "content_units": units,
        })


def test_legacy_parallel_arrays_remain_readable_but_are_not_requested():
    payload = {
        "order": 1,
        "content_blocks": ["判断"],
        "claim_groups": [{"claim_ids": ["claim-001"]}],
    }
    assert intelligence._SemanticSlideDetail.model_validate(payload).model_dump()[
        "content_blocks"
    ] == ["判断"]


@pytest.mark.parametrize("wrapper", ["batch", "bare", "nested"])
def test_actual_structured_parser_reads_canonical_units_without_json_repair(wrapper):
    slides = [{"order": 1, "content_units": [_unit("判断", "claim-001")]}]
    payload = (
        {"slides": slides} if wrapper == "batch"
        else slides if wrapper == "bare"
        else {"slides": [{"slides": slides}]}
    )
    value, repaired = _parse_validated(
        json.dumps(payload), intelligence._SemanticSlideBatch,
    )
    assert repaired is False
    assert value.slides[0].content_blocks == ["判断"]
    assert value.slides[0].claim_groups[0].claim_ids == ["claim-001"]


@pytest.mark.parametrize("outer_fields", [
    {"synthetic_extra": True},
    {"schema_version": "2.0"},
    {"schema_version": None},
    {"content_blocks": []},
])
def test_nested_compatibility_cannot_discard_invalid_outer_envelope(outer_fields):
    nested = {"slides": [{"order": 1, "content_units": [_unit("判断", "claim-001")]}]}
    payload = {"slides": [nested], **outer_fields}
    with pytest.raises(StructuredTaskValidationError):
        _parse_validated(json.dumps(payload), intelligence._SemanticSlideBatch)


def _planner_fixture(
    monkeypatch, *, bad_grouping: str | None = None,
    invalid_envelope: dict[str, object] | None = None,
):
    request = PresentationJobRequest(
        conversation_id="canonical-content-units",
        user_request="用给定账目制作一页判断",
        slide_count=1,
        materials=[InputMaterial(
            name="账目.md", content="# 账目\n收入为2万元。成本为1万元。",
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    plan.slides[0].content_blocks = ["收入为2万元。", "成本为1万元。"]
    calls = []

    async def fake_utility(**kwargs):
        calls.append(kwargs)
        model = kwargs["result_model"]
        if model.__name__ in {"_SemanticClaimLedger", "_SemanticDeckOutline"}:
            value = model.from_runtime(plan)
        else:
            payload = json.loads(kwargs["user_prompt"])
            target = payload["target_slides"][0]
            claim_ids = [claim["claim_id"] for claim in target["claims"]]
            if bad_grouping == "missing":
                claim_ids = claim_ids[:1]
            elif bad_grouping == "duplicate":
                claim_ids.append(claim_ids[0])
            elif bad_grouping == "unknown":
                claim_ids[-1] = "claim-999"
            authored = {
                "slides": [{
                    "order": target["order"],
                    "content_units": [_unit("经营账目", *claim_ids)],
                }],
            }
            if invalid_envelope is not None:
                authored = {"slides": [authored], **invalid_envelope}
            value, _ = _parse_validated(json.dumps(authored), model)
        return StructuredUtilityResult(
            value=value, model_label="test-provider/test-model", repaired=False,
        )

    monkeypatch.setattr(intelligence, "run_structured_utility_task", fake_utility)
    planner = intelligence.UtilityPresentationSemanticPlanner("test-agent")
    return request, brief, planner, calls


def test_canonical_model_response_materializes_all_approved_claims(monkeypatch):
    request, brief, planner, calls = _planner_fixture(monkeypatch)
    plan = planner.plan(request, brief)
    assert plan.slides[0].content_blocks == [
        "经营账目\n收入为2万元。\n成本为1万元。",
    ]
    assert plan.slides[0].intent.required_facts == ["收入为2万元。", "成本为1万元。"]
    assert planner.last_stage_attempt_counts == {
        "claims": 1, "outline": 1, "detail:001-001": 1,
    }
    detail_call = calls[-1]
    transport = json.loads(detail_call["user_prompt"])["target_slides"][0][
        "content_unit_transport_contract"
    ]
    assert transport == {
        "version": 2,
        "field": "content_units",
        "unit_fields": ["label", "claim_ids"],
        "parallel_arrays_forbidden": True,
    }
    assert "content_units" in detail_call["system_prompt"]


@pytest.mark.parametrize("bad_grouping", ["missing", "duplicate", "unknown"])
def test_canonical_units_still_fail_closed_on_claim_coverage(monkeypatch, bad_grouping):
    request, brief, planner, _ = _planner_fixture(
        monkeypatch, bad_grouping=bad_grouping,
    )
    with pytest.raises(PresentationSemanticContentError) as caught:
        planner.plan(request, brief)
    assert caught.value.violation_code == "claim_grouping"
    assert planner.last_stage_attempt_counts["detail:001-001"] == 3
    assert len(planner.last_stage_contract_failures) == 3
    assert all(
        failure["violation_code"] == "claim_grouping"
        for failure in planner.last_stage_contract_failures
    )


@pytest.mark.parametrize("outer_fields", [
    {"synthetic_extra": True},
    {"schema_version": "2.0"},
])
def test_actual_planner_rejects_invalid_envelope_with_same_retry_limit(monkeypatch, outer_fields):
    request, brief, planner, calls = _planner_fixture(
        monkeypatch, invalid_envelope=outer_fields,
    )
    with pytest.raises(StructuredTaskValidationError):
        planner.plan(request, brief)
    assert planner.last_stage_attempt_counts == {
        "claims": 1, "outline": 1, "detail:001-001": 3,
    }
    assert len(calls) == planner.last_utility_call_count == 5
    assert len({call["max_output_tokens"] for call in calls[2:]}) == 1


def test_grouping_retry_directive_requests_only_canonical_units():
    error = PresentationSemanticContentError(
        "semantic claim grouping invariant failed", violation_code="claim_grouping",
    )
    error.presentation_content_block_count = 3
    error.presentation_claim_group_count = 2
    directive = intelligence.UtilityPresentationSemanticPlanner._safe_retry_directive(error)
    assert "只输出 content_units" in directive
    assert "不要输出两个平行数组" in directive
    assert "请让两个数组等长" not in directive
    assert "content_blocks=3" in directive
    assert "claim_groups=2" in directive
