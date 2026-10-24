"""Structured-source quote repair points at original fields, never rewrites them."""

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
    """Stop at the next external boundary, after actual claim validation."""


def _case():
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
                body="PRIVATE-REJECTED-INPUT。乙预算35万元。其他项目999万元。",
            ),
        ],
    )
    proof = {
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
        conversation_id="structured-quote-test", slide_count=6,
        user_request="根据完整材料制作预算PPT",
    )
    return request, brief, proof


@pytest.mark.parametrize("owner_kind", ["claim", "chart"])
@pytest.mark.parametrize("term_index", [0, 1])
def test_public_planner_locates_exact_failed_term_and_own_original_fields(
    monkeypatch, owner_kind, term_index,
):
    request, brief, valid_proof = _case()
    original_brief = deepcopy(brief.model_dump(mode="json"))
    proposals = []
    payloads = []

    async def fake_model(**kwargs):
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        payload = json.loads(kwargs["user_prompt"])
        payloads.append(payload)
        proof = deepcopy(valid_proof)
        if len(payloads) == 1:
            # A quote in another original source is not in this owner's source.
            proof["terms"][term_index]["source_excerpt"] = "PRIVATE-REJECTED-INPUT"
        else:
            owner_path = ("$.claims[0]" if owner_kind == "claim"
                          else "$.chart_contracts[0].chart")
            issue = payload["retry_contract_issues"][0]
            assert issue["path"] == owner_path + ".calculation_proofs[0]"
            assert issue["field_path"] == (
                owner_path + f".calculation_proofs[0].terms[{term_index}].source_excerpt"
            )
            assert issue["source_text_paths"] == [
                "$.brief.source_sections[0].document_title",
                "$.brief.source_sections[0].title",
                "$.brief.source_sections[0].body",
            ]
            assert "JSON" in payload["retry_directive"]
            assert "source_text_paths" in payload["retry_directive"]
            assert issue["failure_kind"] == "calculation_input_excerpt"
            assert issue["source_refs"] == ["source-0"]
            assert "PRIVATE-REJECTED" not in json.dumps(issue)
            assert "999" not in json.dumps(issue)
            assert "75" not in json.dumps(issue)
            # The source is unchanged, including JSON punctuation/field names.
            assert payload["brief"] == original_brief
            assert valid_proof["terms"][term_index]["source_excerpt"] in (
                payload["brief"]["source_sections"][0]["body"]
            )
        proposals.append(deepcopy(proof))
        claims = [{
            "claim_id": "claim-001", "kind": "calculation",
            "statement": "甲预算40万元，乙预算35万元，合计75万元。",
            "source_refs": ["source-0"],
            "calculation_proofs": [proof if owner_kind == "claim" else valid_proof],
            "delivery_contract_ids": [
                item["contract_id"] for item in payload["required_delivery_contracts"]
            ],
        }]
        charts = [] if owner_kind == "claim" else [{
            "contract_id": "chart-001",
            "chart": {
                "chart_type": "column", "categories": ["甲乙合计", "乙"],
                "series": {"预算": [75, 35]}, "unit": "万元",
                "source_refs": ["source-0"], "calculation_proofs": [proof],
            },
        }]
        return StructuredUtilityResult(
            value=kwargs["result_model"].model_validate({
                "claims": claims, "chart_contracts": charts,
            }), model_label="test/model", repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)

    assert len(payloads) == 2
    assert planner.last_stage_attempt_counts["claims"] == 2
    assert proposals[0]["terms"][term_index]["source_excerpt"] == "PRIVATE-REJECTED-INPUT"
    assert proposals[1] == valid_proof
    assert brief.model_dump(mode="json") == original_brief
    assert "PRIVATE-REJECTED" not in json.dumps(planner.last_stage_contract_diagnostics)


@pytest.mark.parametrize("term_index", [0, 1])
def test_original_strict_rejection_and_message_survive_field_metadata(term_index):
    _, brief, proof = _case()
    original = deepcopy(proof)
    proof["terms"][term_index]["source_excerpt"] = "乙预算35万元。"
    source = brief.source_sections[0].grounded_text
    with pytest.raises(ValueError) as caught:
        verified_calculation_values(
            [SourceCalculationProof.model_validate(proof)],
            source_text_for=lambda refs: source, allowed_refs=["source-0"],
        )
    assert type(caught.value) is ValueError
    assert str(caught.value) == "calculation proof input excerpt is not in its cited source"
    assert caught.value.presentation_calculation_term_index == term_index
    assert "source_excerpt" not in vars(caught.value)
    assert "乙预算35万元。" not in json.dumps(vars(caught.value))
    assert verified_calculation_values(
        [SourceCalculationProof.model_validate(original)],
        source_text_for=lambda refs: source, allowed_refs=["source-0"],
    ) == {Decimal(75)}
