"""Compact claim guidance must remain lossless under the original strict schema."""

import json

import pytest
from pydantic import ValidationError

from lantai.app.structured_utility import _parse_validated
from lantai.presentation.intelligence import (
    UtilityPresentationSemanticPlanner,
    _SemanticClaimLedger,
)


def test_compact_claim_guidance_preserves_required_fields_and_nondefaults():
    prompt = UtilityPresentationSemanticPlanner.CLAIM_SYSTEM_PROMPT
    assert "紧凑 JSON" in prompt
    assert "kind=fact" in prompt and "calculation_proofs=[]" in prompt
    assert "delivery_contract_ids" in prompt and "必填字段不得省略" in prompt
    assert "非默认值不得省略" in prompt
    assert "不得为了压缩而丢失不同事实" in prompt

    proof = {
        "terms": [
            {"source_ref": "source-1", "source_excerpt": "收入为42万元。", "value": "42",
             "unit": "万元", "coefficient": 1},
            {"source_ref": "source-1", "source_excerpt": "成本为38万元。", "value": "38",
             "unit": "万元", "coefficient": -1},
        ], "result": "4", "unit": "万元", "authorization_source_ref": "source-1",
        "authorization_excerpt": "净额按收入减成本计算。",
    }
    compact = {"claims": [
        {"claim_id": "claim-001", "statement": "收入为42万元。",
         "source_refs": ["source-1"], "delivery_contract_ids": ["delivery-001"]},
        {"claim_id": "claim-002", "kind": "uncertainty", "statement": "未提供成本数据。",
         "source_refs": ["source-1"], "delivery_contract_ids": []},
        {"claim_id": "claim-003", "kind": "calculation", "statement": "净额为4万元。",
         "calculation": "42万元-38万元=4万元", "source_refs": ["source-1"],
         "calculation_proofs": [proof], "delivery_contract_ids": []},
    ]}
    full = _SemanticClaimLedger.model_validate(compact).model_dump(mode="json")
    parsed, repaired = _parse_validated(json.dumps(compact, ensure_ascii=False,
                                                   separators=(",", ":")),
                                         _SemanticClaimLedger)
    assert repaired is False
    assert parsed.model_dump(mode="json") == full
    assert parsed.claims[0].kind == "fact"
    assert parsed.claims[1].kind == "uncertainty"
    assert parsed.claims[2].kind == "calculation"
    assert parsed.claims[2].calculation == "42万元-38万元=4万元"
    assert [item.model_dump(mode="json") for item in parsed.claims[2].calculation_proofs] == [proof]
    assert parsed.claims[0].delivery_contract_ids == ["delivery-001"]
    assert len(json.dumps(compact, ensure_ascii=False, separators=(",", ":"))) < len(
        json.dumps(full, ensure_ascii=False, separators=(",", ":")))

    for required in ("claim_id", "statement", "source_refs", "delivery_contract_ids"):
        invalid = dict(compact["claims"][0])
        invalid.pop(required)
        with pytest.raises(ValidationError):
            _SemanticClaimLedger.model_validate({"claims": [invalid]})
