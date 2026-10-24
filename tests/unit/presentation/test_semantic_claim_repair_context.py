"""Rejected ledgers expose all owned repair locations, never rejected prose."""

from copy import deepcopy
import json

import pytest

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
from lantai.presentation.page_authoring import PresentationSemanticContentError


class OutlineReached(RuntimeError):
    """Stop at the next external boundary after the real claim validator."""


def _budget_case():
    brief = PresentationBrief(
        objective="展示预算事实",
        audience="报告读者",
        narrative="预算证据先行",
        required_facts=["完整展示预算事实"],
        immutable_data=["40", "35"],
        source_refs=["source-0"],
        source_sections=[PresentationSourceSection(
            title="预算材料", source_ref="source-0",
            body="甲预算40万元。乙预算35万元。",
        )],
    )
    request = PresentationJobRequest(
        conversation_id="claim-repair-context-test",
        user_request="根据完整材料制作预算PPT",
        slide_count=6,
    )
    return request, brief


def _response(kwargs, payload, *, rejected):
    return StructuredUtilityResult(
        value=kwargs["result_model"].model_validate({
            "claims": [
                {
                    "claim_id": "claim-001", "kind": "fact",
                    "statement": "甲预算40万元，乙预算35万元。" + (
                        "建议投入999万元。" if rejected else ""
                    ),
                    "source_refs": ["source-0"],
                    "delivery_contract_ids": [
                        item["contract_id"]
                        for item in payload["required_delivery_contracts"]
                    ],
                },
                {
                    "claim_id": "claim-002", "kind": "recommendation",
                    "statement": (
                        "建议备用888万元。" if rejected else "备用安排应待核查。"
                    ),
                    "source_refs": ["source-0"],
                    "delivery_contract_ids": [],
                },
            ],
            "chart_contracts": [],
        }),
        model_label="test/model", repaired=False,
    )


def test_planner_reports_both_independent_numeric_failures_in_one_retry(monkeypatch):
    request, brief = _budget_case()
    payloads = []

    async def fake_model(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        payloads.append(payload)
        if len(payloads) == 2:
            assert payload["retry_contract_issues"] == [
                {
                    "location": f"claim-ledger-claim-{index:03d}",
                    "path": f"$.claims[{index - 1}]",
                    "failure_kind": "numeric_grounding",
                    "source_refs": ["source-0"],
                    "allowed_numeric_tokens": ["40", "35"],
                }
                for index in (1, 2)
            ]
        return _response(kwargs, payload, rejected=len(payloads) == 1)

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)

    assert len(payloads) == 2
    assert payloads[1]["brief"] == brief.model_dump(mode="json")
    assert "999" not in json.dumps(payloads[1]["retry_contract_issues"])
    assert "888" not in json.dumps(payloads[1]["retry_contract_issues"])
    assert planner.last_stage_contract_diagnostics[0]["failure_kind"] == "numeric_grounding"


def test_new_schema_failure_drops_previous_ledger_positions(monkeypatch):
    request, brief = _budget_case()
    payloads = []

    async def fake_model(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        payloads.append(payload)
        if len(payloads) == 2:
            assert len(payload["retry_contract_issues"]) == 2
            raise StructuredTaskValidationError(
                "structured utility task output failed schema validation",
                validation_issues=("missing@$.claims[0].source_refs",),
            )
        if len(payloads) == 3:
            assert "retry_contract_issues" not in payload
        return _response(kwargs, payload, rejected=len(payloads) == 1)

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)

    assert len(payloads) == 3
    assert planner.last_stage_attempt_counts["claims"] == 3


def test_allowed_numbers_come_only_from_each_owners_cited_source(monkeypatch):
    request, brief = _budget_case()
    brief.source_refs.append("source-1")
    brief.source_sections.append(PresentationSourceSection(
        title="其他材料", source_ref="source-1", body="其他项目预算999万元。",
    ))
    payloads = []

    async def fake_model(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        payloads.append(payload)
        if len(payloads) == 2:
            assert all(item["source_refs"] == ["source-0"]
                       for item in payload["retry_contract_issues"])
            assert all(item["allowed_numeric_tokens"] == ["40", "35"]
                       for item in payload["retry_contract_issues"])
        return _response(kwargs, payload, rejected=len(payloads) == 1)

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)

    assert len(payloads) == 2
    assert "999" in json.dumps(payloads[1]["brief"])
    assert "999" not in json.dumps(payloads[1]["retry_contract_issues"])


def test_verified_result_is_not_lent_to_another_claim_without_its_proof(monkeypatch):
    request, brief = _budget_case()
    authorization = "预算合计按甲预算加乙预算计算。"
    brief.source_sections[0].body += authorization
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
    payloads = []

    async def fake_model(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        payloads.append(payload)
        if len(payloads) == 2:
            assert payload["retry_contract_issues"] == [{
                "location": "claim-ledger-claim-002", "path": "$.claims[1]",
                "failure_kind": "numeric_grounding", "source_refs": ["source-0"],
                "allowed_numeric_tokens": ["40", "35"],
            }]
        value = _response(kwargs, payload, rejected=False).value.model_dump(mode="json")
        value["claims"][0].update({
            "kind": "calculation", "statement": "甲乙预算合计75万元。",
            "calculation": "40万元+35万元=75万元",
            "calculation_proofs": [deepcopy(proof)],
        })
        if len(payloads) == 1:
            value["claims"][1]["statement"] = "甲乙预算合计75万元。"
        return StructuredUtilityResult(
            value=kwargs["result_model"].model_validate(value),
            model_label="test/model", repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)

    assert len(payloads) == 2
    assert "75" not in json.dumps(payloads[1]["retry_contract_issues"])


def test_rejected_numeric_context_keeps_the_original_three_attempt_cap(monkeypatch):
    request, brief = _budget_case()
    payloads = []
    output_budgets = []
    rejected_values = []

    async def fake_model(**kwargs):
        assert kwargs["purpose"] == "presentation-semantic-claims"
        payload = json.loads(kwargs["user_prompt"])
        payloads.append(payload)
        output_budgets.append(kwargs["max_output_tokens"])
        result = _response(kwargs, payload, rejected=True)
        rejected_values.append(result.value)
        return result

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    cost_reservation = planner.estimated_cost_units(request)
    with pytest.raises(PresentationSemanticContentError) as failure:
        planner.plan(request, brief)

    assert failure.value.violation_code == "unsupported_number"
    assert len(payloads) == 3
    assert planner.last_stage_attempt_counts == {"claims": 3}
    assert len(set(output_budgets)) == 1
    assert planner.estimated_cost_units(request) == cost_reservation
    assert all(len(payload["retry_contract_issues"]) == 2 for payload in payloads[1:])
    assert all("999" in value.claims[0].statement for value in rejected_values)
    assert all("888" in value.claims[1].statement for value in rejected_values)
    assert "999" not in json.dumps(planner.last_stage_contract_diagnostics)
    assert "888" not in json.dumps(planner.last_stage_contract_diagnostics)


def test_repair_context_is_bounded_without_accepting_unreported_errors(monkeypatch):
    request, brief = _budget_case()
    payloads = []

    async def fake_model(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        payloads.append(payload)
        if len(payloads) == 2:
            issues = payload["retry_contract_issues"]
            assert len(issues) == 32
            assert [item["path"] for item in issues] == [f"$.claims[{i}]" for i in range(32)]
        value = _response(kwargs, payload, rejected=False).value.model_dump(mode="json")
        value["claims"] = [
            {
                **deepcopy(value["claims"][0 if index == 0 else 1]),
                "claim_id": f"claim-{index + 1:03d}",
                "statement": (
                    ("甲预算40万元，乙预算35万元。" if index == 0 else "预算须待核查。")
                    + ("建议备用999万元。" if len(payloads) <= 2 else "")
                ),
            }
            for index in range(40)
        ]
        return StructuredUtilityResult(
            value=kwargs["result_model"].model_validate(value),
            model_label="test/model", repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)

    # The second still-invalid response must be rejected; truncating metadata
    # does not truncate the original validation pass or approve the ledger.
    assert len(payloads) == 3
    assert planner.last_stage_attempt_counts["claims"] == 3
    assert planner.last_stage_contract_diagnostics[0]["failure_kind"] == "numeric_grounding"


def test_planner_locates_independent_invalid_proofs_without_echoing_quotes(monkeypatch):
    request, brief = _budget_case()
    authorization = "预算合计按甲预算加乙预算计算。"
    brief.source_sections[0].body += authorization
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
    payloads = []

    async def fake_model(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        payloads.append(payload)
        proposals = [deepcopy(proof), deepcopy(proof)]
        if len(payloads) == 1:
            proposals[0]["authorization_excerpt"] = "PRIVATE-REJECTED-AUTHORIZATION"
            proposals[1]["terms"][0]["source_excerpt"] = "PRIVATE-REJECTED-INPUT"
        else:
            assert payload["retry_contract_issues"] == [
                {
                    "location": "claim-ledger-claim-001",
                    "path": f"$.claims[0].calculation_proofs[{index}]",
                    "failure_kind": kind,
                    "source_refs": ["source-0"],
                    "allowed_numeric_tokens": ["40", "35"],
                    **({
                        "field_path": (
                            "$.claims[0].calculation_proofs[1].terms[0].source_excerpt"
                        ),
                        "source_text_paths": [
                            "$.brief.source_sections[0].title",
                            "$.brief.source_sections[0].body",
                        ],
                    } if kind == "calculation_input_excerpt" else {}),
                }
                for index, kind in enumerate((
                    "calculation_authorization", "calculation_input_excerpt",
                ))
            ]
        return StructuredUtilityResult(
            value=kwargs["result_model"].model_validate({
                "claims": [{
                    "claim_id": "claim-001", "kind": "calculation",
                    "statement": "甲乙预算合计75万元。",
                    "calculation": "40万元+35万元=75万元",
                    "calculation_proofs": proposals,
                    "source_refs": ["source-0"],
                    "delivery_contract_ids": [
                        item["contract_id"]
                        for item in payload["required_delivery_contracts"]
                    ],
                }], "chart_contracts": [],
            }),
            model_label="test/model", repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)

    assert len(payloads) == 2
    assert "PRIVATE-REJECTED" not in json.dumps(payloads[1])
    assert "75" not in json.dumps(payloads[1]["retry_contract_issues"])
    assert planner.last_stage_contract_diagnostics[0]["failure_kind"] == "calculation_authorization"


def test_planner_reports_both_invalid_chart_contracts_in_one_retry(monkeypatch):
    request, brief = _budget_case()
    payloads = []

    async def fake_model(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        payloads.append(payload)
        if len(payloads) == 2:
            assert payload["retry_contract_issues"] == [
                {
                    "location": f"claim-ledger-chart-{index:03d}",
                    "path": f"$.chart_contracts[{index - 1}].chart",
                    "failure_kind": "numeric_grounding",
                    "source_refs": ["source-0"],
                    "allowed_numeric_tokens": ["40", "35"],
                }
                for index in (1, 2)
            ]
        value = _response(kwargs, payload, rejected=False).value.model_dump(mode="json")
        value["chart_contracts"] = [
            {
                "contract_id": f"chart-{index:03d}",
                "chart": {
                    "chart_type": "column", "categories": ["甲", "乙"],
                    "series": {"预算": [40, bad_value] if len(payloads) == 1 else [40, 35]},
                    "unit": "万元", "source_refs": ["source-0"],
                },
            }
            for index, bad_value in enumerate((999, 888), start=1)
        ]
        return StructuredUtilityResult(
            value=kwargs["result_model"].model_validate(value),
            model_label="test/model", repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)

    assert len(payloads) == 2
    assert "999" not in json.dumps(payloads[1]["retry_contract_issues"])
    assert "888" not in json.dumps(payloads[1]["retry_contract_issues"])
