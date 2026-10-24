"""Schema diagnostics retain owned fields, not rejected model keys or values."""

from __future__ import annotations

import json
from types import SimpleNamespace

import lantai.presentation.intelligence as intelligence
from lantai.app.structured_utility import StructuredTaskValidationError
from lantai.presentation.models import PresentationJobRequest
from lantai.presentation.runtime import PresentationJobRuntime


def test_schema_diagnostics_redact_unknown_fields_and_keep_owned_locations():
    marker = "synthetic_private_schema_key"
    result = intelligence.safe_semantic_schema_issues([
        "missing@$.claims[0].delivery_contract_ids",
        "string_too_long@$.claims[1].statement",
        f"extra_forbidden@$.claims[1].{marker}",
        f"{marker}@$.claims",
        f"missing@$.claims[{10**9}].{marker}",
    ])
    assert result == [
        {"type": "missing", "path": "$.claims[0].delivery_contract_ids"},
        {"type": "string_too_long", "path": "$.claims[1].statement"},
        {"type": "extra_forbidden", "path": "$.claims[1].<redacted>"},
    ]
    assert marker not in json.dumps(result)


def test_planner_retains_schema_fields_without_exception_message_or_rejected_values():
    planner = intelligence.UtilityPresentationSemanticPlanner("schema-diagnostic-agent")
    marker = "synthetic_private_schema_value"
    error = StructuredTaskValidationError(
        marker, validation_issues=("missing@$.claims[0].delivery_contract_ids",),
    )
    planner._record_safe_contract_failure(
        stage_key="claims", attempt=1, error=error,
        result_model=intelligence._SemanticClaimLedger,
    )
    assert planner.last_stage_contract_diagnostics[0]["schema_issues"] == [
        {"type": "missing", "path": "$.claims[0].delivery_contract_ids"},
    ]
    assert marker not in json.dumps(planner.last_stage_contract_diagnostics)


def test_runtime_revalidates_private_checkpoint_schema_issues(tmp_path):
    marker = "synthetic_private_schema_marker"
    planner = SimpleNamespace(
        last_stage_attempt_counts={"claims": 1},
        last_stage_contract_failures=[{
            "stage": "claims", "attempt": 1, "violation_code": "validation_contract",
            "failure_kind": "schema_validation", "schema_issues": [
                {"type": "missing", "path": "$.claims[0].source_refs", "input": marker},
                {"type": "extra_forbidden", "path": f"$.{marker}"},
                {"type": marker, "path": "$.claims"},
            ],
        }],
    )
    runtime = PresentationJobRuntime(tmp_path, semantic_planner=planner)
    try:
        record = runtime.service.create_job(PresentationJobRequest(
            conversation_id="schema-diagnostic-contract", user_request="合成结构校验", slide_count=1,
        ))
        path = runtime._write_semantic_plan_diagnostics(record.job_id)
        assert path is not None
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
        assert checkpoint["stage_contract_failures"][0]["schema_issues"] == [
            {"type": "missing", "path": "$.claims[0].source_refs"},
            {"type": "extra_forbidden", "path": "$.<redacted>"},
        ]
        assert marker not in json.dumps(checkpoint)
    finally:
        runtime.shutdown()


def test_schema_diagnostic_entries_are_deduplicated_and_bounded():
    assert intelligence.safe_semantic_schema_issues(
        ["missing@$.claims[0].statement"] * 1000,
    ) == [{"type": "missing", "path": "$.claims[0].statement"}]
    assert len(intelligence.safe_semantic_schema_issues([
        f"missing@$.claims[{index}].statement" for index in range(1000)
    ])) == 24
