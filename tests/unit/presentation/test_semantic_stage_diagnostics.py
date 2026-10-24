"""Stage diagnostics distinguish owned failure kinds without rejected content."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import lantai.presentation.intelligence as intelligence
from lantai.app.structured_utility import (
    StructuredTaskValidationError,
    StructuredUtilityResult,
    _parse_validated,
)
from lantai.presentation.models import PresentationJobRequest
from lantai.presentation.page_authoring import PresentationSemanticContentError
from lantai.presentation.runtime import PresentationJobRuntime


def test_enriched_diagnostics_preserve_legacy_contract_failure_shape(tmp_path):
    planner = intelligence.UtilityPresentationSemanticPlanner("diagnostic-test-agent")
    error = ValueError("synthetic-private-numeric-marker")
    error.violation_code = "unsupported_numeric_claim"
    error.presentation_numeric_token = "synthetic-private-numeric-marker"
    planner._record_safe_contract_failure(
        stage_key="claims:001", attempt=1, error=error,
    )
    planner.last_stage_attempt_counts = {"claims:001": 1}
    legacy = [{"stage": "claims:001", "attempt": 1,
               "violation_code": "unsupported_numeric_claim"}]
    assert planner.last_stage_contract_failures == legacy
    assert planner.last_stage_contract_diagnostics == [
        {**legacy[0], "failure_kind": "numeric_grounding"},
    ]
    runtime = PresentationJobRuntime(tmp_path, semantic_planner=planner)
    try:
        record = runtime.service.create_job(PresentationJobRequest(
            conversation_id="diagnostic-compatibility", user_request="合成诊断用例", slide_count=1,
        ))
        path = runtime._write_semantic_plan_diagnostics(record.job_id)
        assert path is not None
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
        assert checkpoint["stage_contract_failures"] == planner.last_stage_contract_diagnostics
        assert "synthetic-private-numeric-marker" not in json.dumps(checkpoint)
    finally:
        runtime.shutdown()


@pytest.mark.parametrize(("message", "kind"), [
    ("semantic claim ledger exceeds storyboard capacity", "ledger_capacity"),
    ("semantic claim ledger requires exact contiguous ids", "ledger_contiguous_ids"),
    ("semantic claim ledger requires known source references", "ledger_known_source_refs"),
    ("semantic claim ledger references unknown delivery contract ids", "ledger_delivery_ids"),
    ("semantic claim ledger must cover every required delivery contract", "ledger_delivery_coverage"),
    ("semantic claim delivery contract exceeds source scope", "ledger_delivery_source_scope"),
    ("semantic claim omitted required delivery value", "ledger_delivery_value"),
    ("semantic claim omitted required delivery semantics", "ledger_delivery_semantics"),
    ("calculation proof does not match its typed schema", "calculation_schema"),
    ("calculation proof exceeds source or unit scope", "calculation_scope"),
    ("calculation proof authorization is not in its cited source", "calculation_authorization"),
    ("calculation proof input excerpt is not in its cited source", "calculation_input_excerpt"),
    ("calculation proof input value or unit is not source-grounded", "calculation_input_value_unit"),
    ("calculation proof result does not match recomputation", "calculation_recomputation"),
    ("calculation proof requires individually cited source bodies", "calculation_individual_sources"),
])
def test_owned_ledger_failures_have_distinct_safe_kinds(message, kind):
    planner = intelligence.UtilityPresentationSemanticPlanner("diagnostic-test-agent")
    planner._record_safe_contract_failure(
        stage_key="claims", attempt=1, error=ValueError(message),
    )
    assert planner.last_stage_contract_diagnostics == [{
        "stage": "claims", "attempt": 1, "violation_code": "validation_contract",
        "failure_kind": kind,
    }]


@pytest.mark.parametrize(("message", "kind"), [
    ("structured utility task returned empty output", "empty_output"),
    ("structured utility task returned invalid JSON", "invalid_json"),
    ("structured utility task output failed schema validation", "schema_validation"),
])
def test_owned_parser_failures_have_distinct_safe_kinds(message, kind):
    planner = intelligence.UtilityPresentationSemanticPlanner("diagnostic-test-agent")
    planner._record_safe_contract_failure(
        stage_key="claims", attempt=1, error=StructuredTaskValidationError(message),
    )
    assert planner.last_stage_contract_diagnostics[0]["failure_kind"] == kind


def test_unknown_messages_paths_and_numeric_values_are_not_diagnostic_content():
    planner = intelligence.UtilityPresentationSemanticPlanner("diagnostic-test-agent")
    marker = "synthetic-private-diagnostic-marker"
    planner._record_safe_contract_failure(
        stage_key="claims", attempt=1, error=ValueError(marker),
    )
    planner._record_safe_contract_failure(
        stage_key="claims", attempt=2,
        error=StructuredTaskValidationError(
            marker, validation_issues=(f"extra_forbidden@$.{marker}",),
        ),
    )
    numeric_error = ValueError(marker)
    numeric_error.presentation_numeric_token = marker
    numeric_error.presentation_numeric_location = marker
    planner._record_safe_contract_failure(
        stage_key="claims", attempt=3, error=numeric_error,
    )
    failures = planner.last_stage_contract_diagnostics
    assert [item["failure_kind"] for item in failures] == [
        "semantic_contract", "schema_validation", "numeric_grounding",
    ]
    assert marker not in json.dumps(failures)
    assert all(set(item) == {"stage", "attempt", "violation_code", "failure_kind"}
               for item in failures)


def test_real_parse_failures_record_each_attempt_without_changing_exhaustion(monkeypatch, tmp_path):
    planner = intelligence.UtilityPresentationSemanticPlanner("diagnostic-test-agent")
    request = PresentationJobRequest(
        conversation_id="diagnostic-contract", user_request="合成诊断用例", slide_count=1,
    )
    marker = "synthetic-private-schema-marker"
    caught_errors = []
    calls = []

    async def fake_utility(**kwargs):
        calls.append(kwargs)
        try:
            _parse_validated(json.dumps({"claims": [], marker: marker}), kwargs["result_model"])
        except StructuredTaskValidationError as error:
            caught_errors.append(error)
            raise
        pytest.fail("invalid payload unexpectedly parsed")

    monkeypatch.setattr(intelligence, "run_structured_utility_task", fake_utility)
    with pytest.raises(StructuredTaskValidationError) as caught:
        asyncio.run(planner._run_stage(
            stage_key="claims", request=request,
            system_prompt="合成结构校验", payload={"fixture": True},
            purpose="synthetic-stage-diagnostic",
            result_model=intelligence._SemanticClaimLedger,
            output_token_budget=512, attempt_limit=3,
        ))
    assert caught.value is caught_errors[-1]
    assert len(calls) == len(caught_errors) == 3
    assert all(call["max_output_tokens"] == 512 for call in calls)
    assert planner.last_stage_attempt_counts == {"claims": 3}
    assert planner.last_utility_call_count == 3
    assert planner.last_stage_contract_diagnostics == [
        {"stage": "claims", "attempt": index, "violation_code": "validation_contract",
         "failure_kind": "schema_validation", "schema_issues": [
             {"type": "too_short", "path": "$.claims"},
             {"type": "extra_forbidden", "path": "$.<redacted>"},
         ]}
        for index in range(1, 4)
    ]
    assert marker not in json.dumps(planner.last_stage_contract_diagnostics)
    runtime = PresentationJobRuntime(tmp_path, semantic_planner=planner)
    try:
        record = runtime.service.create_job(request)
        path = runtime._write_semantic_plan_diagnostics(record.job_id)
        assert path is not None
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
        assert checkpoint["stage_contract_failures"] == planner.last_stage_contract_diagnostics
        assert marker not in json.dumps(checkpoint)
    finally:
        runtime.shutdown()


def test_semantic_failure_keeps_original_typed_rejection_and_retry_limit(monkeypatch):
    planner = intelligence.UtilityPresentationSemanticPlanner("diagnostic-test-agent")
    request = PresentationJobRequest(
        conversation_id="diagnostic-semantic-contract", user_request="合成诊断用例", slide_count=1,
    )
    calls = []
    errors = []

    async def fake_utility(**kwargs):
        calls.append(kwargs)
        return StructuredUtilityResult(value=object(), model_label="synthetic/test", repaired=False)

    def validator(value):
        error = ValueError("semantic claim ledger requires known source references")
        errors.append(error)
        raise error

    monkeypatch.setattr(intelligence, "run_structured_utility_task", fake_utility)
    with pytest.raises(PresentationSemanticContentError) as caught:
        asyncio.run(planner._run_stage(
            stage_key="claims", request=request,
            system_prompt="合成主张校验", payload={"fixture": True},
            purpose="synthetic-stage-diagnostic",
            result_model=intelligence._SemanticClaimLedger,
            output_token_budget=512, attempt_limit=3, result_validator=validator,
        ))
    assert caught.value.violation_code == "claim_contract"
    assert caught.value.__cause__ is errors[-1]
    assert len(calls) == len(errors) == 3
    assert all(call["max_output_tokens"] == 512 for call in calls)
    assert [item["failure_kind"] for item in planner.last_stage_contract_diagnostics] == [
        "ledger_known_source_refs",
    ] * 3


@pytest.mark.parametrize("kind", [
    "empty_output", "invalid_json", "schema_validation", "numeric_grounding",
    "semantic_contract", "ledger_capacity", "ledger_contiguous_ids",
    "ledger_known_source_refs", "ledger_delivery_ids", "ledger_delivery_coverage",
    "ledger_delivery_source_scope", "ledger_delivery_value", "ledger_delivery_semantics",
    "calculation_schema", "calculation_scope", "calculation_authorization",
    "calculation_input_excerpt", "calculation_input_value_unit",
    "calculation_recomputation", "calculation_individual_sources",
])
def test_runtime_checkpoint_retains_only_owned_failure_kinds(tmp_path, kind):
    marker = "synthetic_private_diagnostic_marker"
    failures = [
        {"stage": "claims", "attempt": 1, "violation_code": "validation_contract",
         "failure_kind": kind, "unsafe_content": marker},
        {"stage": "claims", "attempt": 2, "violation_code": "validation_contract",
         "failure_kind": marker, "unsafe_content": marker},
    ]
    planner = SimpleNamespace(
        last_stage_contract_failures=failures,
        last_stage_attempt_counts={"claims": 2},
    )
    runtime = PresentationJobRuntime(tmp_path, semantic_planner=planner)
    try:
        record = runtime.service.create_job(PresentationJobRequest(
            conversation_id="diagnostic-checkpoint", user_request="合成诊断用例", slide_count=1,
        ))
        path = runtime._write_semantic_plan_diagnostics(record.job_id)
        assert path is not None
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
        assert checkpoint["stage_contract_failures"] == [
            {"stage": "claims", "attempt": 1, "violation_code": "validation_contract",
             "failure_kind": kind},
            {"stage": "claims", "attempt": 2, "violation_code": "validation_contract"},
        ]
        assert marker not in json.dumps(checkpoint)
    finally:
        runtime.shutdown()
