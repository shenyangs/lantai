# -*- coding: utf-8 -*-
"""Rejected calculation evidence gets actionable, source-safe model repairs."""

from copy import deepcopy
import json

import pytest
from pydantic import ValidationError

from lantai.app.structured_utility import (
    StructuredTaskValidationError,
    StructuredUtilityResult,
)
from lantai.presentation.intelligence import UtilityPresentationSemanticPlanner
from lantai.presentation.models import (
    PresentationBrief,
    PresentationJobRequest,
    PresentationSourceSection,
)


class OutlineReached(RuntimeError):
    """Stop only at the next external model boundary after real validation."""


def _calculation_case():
    authorization = "预算合计按甲预算加乙预算计算，不得估算缺失指标。"
    brief = PresentationBrief(
        objective="展示已授权的预算合计",
        audience="报告读者",
        narrative="预算事实与合计",
        required_facts=["完整展示预算事实与已授权合计"],
        immutable_data=["40", "35"],
        source_refs=["source-0"],
        source_sections=[PresentationSourceSection(
            title="预算材料",
            source_ref="source-0",
            body="甲预算40万元。乙预算35万元。" + authorization,
        )],
        prohibited_changes=["不得补造授权或数值"],
    )
    request = PresentationJobRequest(
        conversation_id="calculation-repair-test",
        user_request="根据完整材料制作预算PPT",
        slide_count=6,
    )
    proof = {
        "terms": [
            {"source_ref": "source-0", "source_excerpt": "甲预算40万元。",
             "value": "40", "unit": "万元", "coefficient": 1},
            {"source_ref": "source-0", "source_excerpt": "乙预算35万元。",
             "value": "35", "unit": "万元", "coefficient": 1},
        ],
        "result": "75", "unit": "万元",
        "authorization_source_ref": "source-0",
        "authorization_excerpt": authorization,
    }
    return request, brief, proof


def _calculation_response(kwargs, payload, proof):
    return StructuredUtilityResult(
        value=kwargs["result_model"].model_validate({
            "claims": [{
                "claim_id": "claim-001", "kind": "calculation",
                "statement": "甲乙预算合计75万元。",
                "calculation": "40万元+35万元=75万元",
                "calculation_proofs": [proof],
                "source_refs": ["source-0"],
                "delivery_contract_ids": [
                    contract["contract_id"]
                    for contract in payload["required_delivery_contracts"]
                ],
            }], "chart_contracts": [],
        }),
        model_label="test/model", repaired=False,
    )


def test_planner_repairs_authorization_fields_without_echoing_rejected_excerpt(
    monkeypatch,
):
    request, brief, proof = _calculation_case()
    payloads = []
    rejected_excerpt = "PRIVATE-REJECTED-AUTHORIZATION"

    async def fake_model(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            assert payload["claim_ledger"]["claims"][0]["calculation_proofs"] == [proof]
            raise OutlineReached
        payloads.append(payload)
        proposal = deepcopy(proof)
        if len(payloads) == 1:
            proposal["authorization_excerpt"] = rejected_excerpt
        return _calculation_response(kwargs, payload, proposal)

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)

    assert len(payloads) == 2
    directive = payloads[1]["retry_directive"]
    assert "authorization_source_ref" in directive
    assert "authorization_excerpt" in directive
    assert rejected_excerpt not in directive
    assert payloads[1]["brief"] == brief.model_dump(mode="json")
    assert planner.last_stage_contract_diagnostics[0]["failure_kind"] == "calculation_authorization"
    assert rejected_excerpt not in json.dumps(planner.last_stage_contract_diagnostics)


def test_planner_keeps_authorization_repair_when_input_excerpt_also_fails(monkeypatch):
    request, brief, proof = _calculation_case()
    payloads = []

    async def fake_model(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        payloads.append(payload)
        proposal = deepcopy(proof)
        if len(payloads) == 1:
            proposal["authorization_excerpt"] = "PRIVATE-REJECTED-AUTHORIZATION"
        elif len(payloads) == 2:
            proposal["terms"][0]["source_excerpt"] = "PRIVATE-REJECTED-INPUT"
        return _calculation_response(kwargs, payload, proposal)

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)

    assert len(payloads) == 3
    directive = payloads[2]["retry_directive"]
    assert "authorization_excerpt" in directive
    assert "terms[].source_excerpt" in directive
    assert "PRIVATE-REJECTED" not in directive
    assert payloads[2]["brief"] == brief.model_dump(mode="json")
    assert [item["failure_kind"] for item in planner.last_stage_contract_diagnostics] == [
        "calculation_authorization", "calculation_input_excerpt",
    ]


@pytest.mark.parametrize("field", ["input", "authorization"])
def test_planner_repairs_calculation_source_scope_without_inventing_refs(monkeypatch, field):
    request, brief, proof = _calculation_case()
    payloads = []

    async def fake_model(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        payloads.append(payload)
        proposal = deepcopy(proof)
        if len(payloads) == 1:
            if field == "input":
                proposal["terms"][0]["source_ref"] = "PRIVATE-UNKNOWN-SOURCE"
            else:
                proposal["authorization_source_ref"] = "PRIVATE-UNKNOWN-SOURCE"
        return _calculation_response(kwargs, payload, proposal)

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)

    assert len(payloads) == 2
    directive = payloads[1]["retry_directive"]
    assert "terms[].source_ref" in directive
    assert "authorization_source_ref" in directive
    assert "source_refs" in directive
    assert "unit" in directive
    assert "PRIVATE-UNKNOWN-SOURCE" not in directive
    assert payloads[1]["allowed_source_refs"] == ["source-0"]
    assert planner.last_stage_contract_diagnostics[0]["failure_kind"] == "calculation_scope"


@pytest.mark.parametrize("field,value", [("value", "41"), ("unit", "亿元")])
def test_planner_repairs_source_bound_value_and_unit_without_converting(monkeypatch, field, value):
    request, brief, proof = _calculation_case()
    payloads = []

    async def fake_model(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        payloads.append(payload)
        proposal = deepcopy(proof)
        if len(payloads) == 1:
            proposal["terms"][0][field] = value
        return _calculation_response(kwargs, payload, proposal)

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)

    assert len(payloads) == 2
    directive = payloads[1]["retry_directive"]
    assert "terms[].value" in directive
    assert "terms[].source_excerpt" in directive
    assert "terms[].unit" in directive
    assert "换算" in directive
    assert payloads[1]["brief"] == brief.model_dump(mode="json")
    assert planner.last_stage_contract_diagnostics[0]["failure_kind"] == "calculation_input_value_unit"


@pytest.mark.parametrize("field", ["result", "coefficient"])
def test_planner_repairs_exact_signed_recomputation_without_runtime_patch(monkeypatch, field):
    request, brief, proof = _calculation_case()
    payloads = []

    async def fake_model(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            assert payload["claim_ledger"]["claims"][0]["calculation_proofs"] == [proof]
            raise OutlineReached
        payloads.append(payload)
        proposal = deepcopy(proof)
        if len(payloads) == 1:
            if field == "result":
                proposal["result"] = "74"
            else:
                proposal["terms"][1]["coefficient"] = -1
        return _calculation_response(kwargs, payload, proposal)

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)

    assert len(payloads) == 2
    directive = payloads[1]["retry_directive"]
    assert "terms[].value" in directive
    assert "terms[].coefficient" in directive
    assert "result" in directive
    assert "精确" in directive
    assert payloads[1]["brief"] == brief.model_dump(mode="json")
    assert planner.last_stage_contract_diagnostics[0]["failure_kind"] == "calculation_recomputation"


@pytest.mark.parametrize("alternating", [False, True])
def test_calculation_field_repairs_share_the_existing_three_failure_allowance(
    monkeypatch, alternating,
):
    request, brief, proof = _calculation_case()
    payloads = []
    output_budgets = []

    async def fake_model(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        payloads.append(payload)
        output_budgets.append(kwargs["max_output_tokens"])
        assert kwargs["purpose"] == "presentation-semantic-claims"
        proposal = deepcopy(proof)
        if not alternating or len(payloads) == 1:
            proposal["authorization_excerpt"] = "PRIVATE-REJECTED-AUTHORIZATION"
        elif len(payloads) == 2:
            proposal["terms"][0]["source_excerpt"] = "PRIVATE-REJECTED-INPUT"
        elif len(payloads) == 3:
            proposal["result"] = "74"
        return _calculation_response(kwargs, payload, proposal)

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    initial_cost_reservation = planner.estimated_cost_units(request)
    with pytest.raises(ValueError, match="calculation proof"):
        planner.plan(request, brief)

    assert len(payloads) == 3
    assert planner.last_stage_attempt_counts == {"claims": 3}
    assert len(set(output_budgets)) == 1
    assert planner.estimated_cost_units(request) == initial_cost_reservation
    assert all(payload["brief"] == brief.model_dump(mode="json") for payload in payloads)
    assert "PRIVATE-REJECTED" not in json.dumps(planner.last_stage_contract_diagnostics)


@pytest.mark.parametrize("field", ["coefficient", "result", "unit", "authorization_excerpt"])
def test_planner_explains_calculation_proof_shape_from_real_schema_failure(monkeypatch, field):
    request, brief, proof = _calculation_case()
    payloads = []
    location = (
        "$.claims[0].calculation_proofs[0].terms[0].coefficient"
        if field == "coefficient" else f"$.claims[0].calculation_proofs[0].{field}"
    )

    async def fake_model(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        payloads.append(payload)
        proposal = deepcopy(proof)
        if len(payloads) == 1:
            target = proposal["terms"][0] if field == "coefficient" else proposal
            target.pop(field)
        try:
            return _calculation_response(kwargs, payload, proposal)
        except ValidationError as error:
            raise StructuredTaskValidationError(
                "structured utility task output failed schema validation",
                validation_issues=(f"missing@{location}",),
            ) from error

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)

    assert len(payloads) == 2
    directive = payloads[1]["retry_directive"]
    assert "terms[].coefficient" in directive
    assert "result" in directive
    assert "authorization_source_ref" in directive
    assert "authorization_excerpt" in directive
    assert "十进制字符串" in directive
    assert payloads[1]["brief"] == brief.model_dump(mode="json")
    assert planner.last_stage_contract_diagnostics[0]["schema_issues"] == [
        {"type": "missing", "path": location},
    ]


def test_planner_redacts_unknown_calculation_field_in_legacy_schema_cause(monkeypatch):
    request, brief, proof = _calculation_case()
    payloads = []
    rejected_key = "PRIVATE_REJECTED_CALCULATION_FIELD"
    rejected_value = "PRIVATE_REJECTED_CALCULATION_VALUE"

    async def fake_model(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        payloads.append(payload)
        proposal = deepcopy(proof)
        if len(payloads) == 1:
            proposal[rejected_key] = rejected_value
        try:
            return _calculation_response(kwargs, payload, proposal)
        except ValidationError as error:
            # Legacy adapters expose only the real Pydantic cause, without
            # structured validation_issues. That path must be equally safe.
            raise StructuredTaskValidationError(
                "structured utility task output failed schema validation",
            ) from error

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)

    assert len(payloads) == 2
    directive = payloads[1]["retry_directive"]
    assert "calculation_proofs" in directive
    assert rejected_key not in directive
    assert rejected_value not in directive
    assert payloads[1]["brief"] == brief.model_dump(mode="json")
    diagnostics = json.dumps(planner.last_stage_contract_diagnostics)
    assert rejected_key not in diagnostics
    assert rejected_value not in diagnostics
