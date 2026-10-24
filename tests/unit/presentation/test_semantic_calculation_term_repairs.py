"""Calculation retries locate the rejected input, not model-owned prose."""

from copy import deepcopy
from decimal import Decimal
import json

import pytest

from lantai.app.structured_utility import StructuredUtilityResult
from lantai.presentation.intelligence import UtilityPresentationSemanticPlanner
from lantai.presentation.models import (
    PresentationBrief,
    PresentationJobRequest,
    PresentationSourceSection,
    SourceCalculationProof,
)
from lantai.presentation.page_authoring import verified_calculation_values


class OutlineReached(RuntimeError):
    """The next model boundary proves that strict claim validation passed."""


@pytest.mark.parametrize("failed_field,rejected", [("value", "999"), ("unit", "元")])
@pytest.mark.parametrize("term_index", [0, 1])
@pytest.mark.parametrize("owner_kind", ["claim", "chart"])
def test_public_planner_locates_rejected_term_field_and_only_its_original_source(
    monkeypatch, failed_field, rejected, term_index, owner_kind,
):
    authorization = "预算合计按甲预算加乙预算计算。"
    body = json.dumps({
        "单位": "万元", "甲预算": 40, "乙预算": 35, "授权": authorization,
    }, ensure_ascii=False, indent=2)
    brief = PresentationBrief(
        objective="展示预算事实", audience="报告读者", narrative="预算证据先行",
        required_facts=["完整展示预算事实"], immutable_data=["40", "35"],
        source_refs=["source-0", "source-1"],
        source_sections=[
            PresentationSourceSection(
                title="预算材料", document_title="原始结构化预算",
                source_ref="source-0", body=body,
            ),
            PresentationSourceSection(
                title="不属于本条的材料", source_ref="source-1",
                body="PRIVATE-REJECTED-INPUT。其他项目999万元。",
            ),
        ],
    )
    valid_proof = {
        "terms": [
            {"source_ref": "source-0", "source_excerpt": '"甲预算": 40',
             "value": "40", "unit": "万元", "coefficient": 1},
            {"source_ref": "source-0", "source_excerpt": '"乙预算": 35',
             "value": "35", "unit": "万元", "coefficient": 1},
        ],
        "result": "75", "unit": "万元",
        "authorization_source_ref": "source-0", "authorization_excerpt": authorization,
    }
    request = PresentationJobRequest(
        conversation_id="calculation-term-test", slide_count=6,
        user_request="根据完整材料制作预算PPT",
    )
    original_brief = deepcopy(brief.model_dump(mode="json"))
    payloads = []

    async def fake_model(**kwargs):
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        payload = json.loads(kwargs["user_prompt"])
        payloads.append(payload)
        proof = deepcopy(valid_proof)
        if len(payloads) == 1:
            proof["terms"][term_index][failed_field] = rejected
        else:
            issue = payload["retry_contract_issues"][0]
            assert issue["failure_kind"] == "calculation_input_value_unit"
            owner_path = (
                "$.claims[0]" if owner_kind == "claim" else "$.chart_contracts[0].chart"
            )
            assert issue["path"] == f"{owner_path}.calculation_proofs[0]"
            assert issue["field_path"] == (
                f"{owner_path}.calculation_proofs[0].terms[{term_index}].{failed_field}"
            )
            assert issue["source_text_paths"] == [
                "$.brief.source_sections[0].document_title",
                "$.brief.source_sections[0].title",
                "$.brief.source_sections[0].body",
            ]
            assert issue["source_refs"] == ["source-0"]
            assert "999" not in json.dumps(issue)
            assert "75" not in json.dumps(issue)
            assert "PRIVATE-REJECTED" not in json.dumps(issue)
            assert "field_path" in payload["retry_directive"]
            assert "source_text_paths" in payload["retry_directive"]
            assert payload["brief"] == original_brief
        return StructuredUtilityResult(
            value=kwargs["result_model"].model_validate({
                "claims": [{
                    "claim_id": "claim-001", "kind": "calculation",
                    "statement": "甲预算40万元，乙预算35万元，合计75万元。",
                    "source_refs": ["source-0"],
                    "calculation_proofs": [proof if owner_kind == "claim" else valid_proof],
                    "delivery_contract_ids": [
                        item["contract_id"] for item in payload["required_delivery_contracts"]
                    ],
                }],
                "chart_contracts": [] if owner_kind == "claim" else [{
                    "contract_id": "chart-001",
                    "chart": {
                        "chart_type": "column", "categories": ["甲乙合计", "乙"],
                        "series": {"预算": [75, 35]}, "unit": "万元",
                        "source_refs": ["source-0"], "calculation_proofs": [proof],
                    },
                }],
            }), model_label="test/model", repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)
    assert len(payloads) == planner.last_stage_attempt_counts["claims"] == 2
    assert brief.model_dump(mode="json") == original_brief
    assert "PRIVATE-REJECTED" not in json.dumps(planner.last_stage_contract_diagnostics)


@pytest.mark.parametrize("term_index", [0, 1])
@pytest.mark.parametrize("field,rejected", [("value", "999"), ("unit", "亿元")])
def test_strict_input_rejection_survives_location_metadata(term_index, field, rejected):
    source = "甲预算40万元。乙预算35万元。授权相加。"
    proof = {
        "terms": [
            {"source_ref": "source-0", "source_excerpt": "甲预算40万元。",
             "value": "40", "unit": "万元", "coefficient": 1},
            {"source_ref": "source-0", "source_excerpt": "乙预算35万元。",
             "value": "35", "unit": "万元", "coefficient": 1},
        ],
        "result": "75", "unit": "万元",
        "authorization_source_ref": "source-0", "authorization_excerpt": "授权相加。",
    }
    original = deepcopy(proof)
    proof["terms"][term_index][field] = rejected
    with pytest.raises(ValueError) as caught:
        verified_calculation_values(
            [SourceCalculationProof.model_validate(proof)],
            source_text_for=lambda refs: source, allowed_refs=["source-0"],
        )
    assert type(caught.value) is ValueError
    assert str(caught.value) == "calculation proof input value or unit is not source-grounded"
    assert vars(caught.value) == {
        "presentation_calculation_term_index": term_index,
        "presentation_calculation_term_field": field,
    }
    assert verified_calculation_values(
        [SourceCalculationProof.model_validate(original)],
        source_text_for=lambda refs: source, allowed_refs=["source-0"],
    ) == {Decimal(75)}
