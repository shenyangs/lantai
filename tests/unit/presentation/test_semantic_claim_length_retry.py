"""Length failures must explain the existing contract, never truncate claims."""

from __future__ import annotations

import asyncio
import json

import pytest

import lantai.presentation.intelligence as intelligence
from lantai.app.structured_utility import (
    StructuredTaskValidationError,
    StructuredUtilityResult,
    _parse_validated,
)
from lantai.presentation.models import PresentationJobRequest


def _ledger(statement: str) -> dict:
    return {"claims": [{
        "claim_id": "claim-001", "kind": "fact", "statement": statement,
        "calculation": "", "source_refs": ["synthetic-source"],
        "delivery_contract_ids": [],
    }]}


def test_claim_length_retry_uses_owned_path_and_existing_schema_limit() -> None:
    marker = "synthetic-private-rejected-value"
    error = StructuredTaskValidationError(
        marker,
        validation_issues=(
            "string_too_long@$.claims[34].statement",
            f"string_too_long@$.{marker}",
        ),
    )
    directive = intelligence.UtilityPresentationSemanticPlanner._safe_retry_directive(error)
    schema = intelligence._SemanticClaimLedger.model_json_schema()
    assert schema["$defs"]["_SemanticClaim"]["properties"]["statement"]["maxLength"] == 64
    assert "claims[].statement" in directive
    assert "64" in directive
    assert "字符" in directive
    assert "不得截断" in directive
    assert "数值、单位" in directive
    assert marker not in directive


@pytest.mark.parametrize("issue", [
    "string_too_long@$.claims[0].private_statement",
    "string_too_long@$.private_claims[0].statement",
    "string_too_long@$.claims[10001].statement",
    "string_too_short@$.claims[0].statement",
    "private_error@$.claims[0].statement",
])
def test_unknown_or_different_issue_does_not_trigger_claim_length_feedback(issue: str) -> None:
    directive = intelligence.UtilityPresentationSemanticPlanner._safe_retry_directive(
        StructuredTaskValidationError("synthetic schema failure", validation_issues=(issue,)),
    )
    assert "claims[].statement" not in directive


def test_real_parse_rejection_gets_actionable_retry_without_content_repair(monkeypatch) -> None:
    planner = intelligence.UtilityPresentationSemanticPlanner("synthetic-claim-length-agent")
    request = PresentationJobRequest(
        conversation_id="synthetic-claim-length", user_request="合成长度契约", slide_count=1,
    )
    rejected = "合成事实" * 17
    accepted = "合成事实仍须完整保留。"
    calls = []

    async def fake_utility(**kwargs):
        calls.append(kwargs)
        payload = json.loads(kwargs["user_prompt"])
        if len(calls) == 1:
            _parse_validated(json.dumps(_ledger(rejected)), kwargs["result_model"])
            pytest.fail("overlong claim unexpectedly passed")
        assert "claims[].statement" in payload["retry_directive"]
        assert "64" in payload["retry_directive"]
        assert rejected not in payload["retry_directive"]
        value, repaired = _parse_validated(json.dumps(_ledger(accepted)), kwargs["result_model"])
        assert repaired is False
        return StructuredUtilityResult(value=value, model_label="synthetic/test", repaired=False)

    monkeypatch.setattr(intelligence, "run_structured_utility_task", fake_utility)
    result = asyncio.run(planner._run_stage(
        stage_key="claims", request=request, system_prompt="合成主张契约",
        payload={"fixture": True}, purpose="synthetic-claim-length",
        result_model=intelligence._SemanticClaimLedger,
        output_token_budget=512, attempt_limit=3,
    ))
    assert result.claims[0].statement == accepted
    assert len(calls) == planner.last_utility_call_count == 2
    assert all(call["max_output_tokens"] == 512 for call in calls)
    assert planner.last_stage_attempt_counts == {"claims": 2}
    assert planner.last_stage_contract_diagnostics == [{
        "stage": "claims", "attempt": 1, "violation_code": "validation_contract",
        "failure_kind": "schema_validation", "schema_issues": [
            {"type": "string_too_long", "path": "$.claims[0].statement"},
        ],
    }]


def test_repeated_overlong_claims_still_fail_at_original_attempt_limit(monkeypatch) -> None:
    planner = intelligence.UtilityPresentationSemanticPlanner("synthetic-claim-length-agent")
    request = PresentationJobRequest(
        conversation_id="synthetic-claim-length-exhaustion",
        user_request="合成长度契约", slide_count=1,
    )
    calls = []

    async def fake_utility(**kwargs):
        calls.append(kwargs)
        _parse_validated(json.dumps(_ledger("合成事实" * 17)), kwargs["result_model"])
        pytest.fail("overlong claim unexpectedly passed")

    monkeypatch.setattr(intelligence, "run_structured_utility_task", fake_utility)
    with pytest.raises(StructuredTaskValidationError):
        asyncio.run(planner._run_stage(
            stage_key="claims", request=request, system_prompt="合成主张契约",
            payload={"fixture": True}, purpose="synthetic-claim-length",
            result_model=intelligence._SemanticClaimLedger,
            output_token_budget=512, attempt_limit=3,
        ))
    assert len(calls) == planner.last_utility_call_count == 3
    assert planner.last_stage_attempt_counts == {"claims": 3}
    assert all(call["max_output_tokens"] == 512 for call in calls)
    assert len(planner.last_stage_contract_failures) == 3
