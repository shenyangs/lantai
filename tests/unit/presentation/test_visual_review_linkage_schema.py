"""Advertised source-check constraints and bounded private retry diagnostics."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from lantai.app.structured_utility import StructuredTaskValidationError
from lantai.presentation.intelligence import UtilityPresentationVisualCritic, _SlideVisualReview
from lantai.presentation.models import VisualCriticResult
from lantai.presentation.service import PresentationService, _safe_visual_validation_issues
from tests.unit.presentation.test_jobs_and_service import (
    _CheckpointVisualCritic, _render_for_edit_tests, _request, _two_slide_visual_deck,
)
from tests.unit.presentation.test_visual_review_evidence_linkage import (
    _capture, _check, _finding, _response, _review, _slide,
)


@pytest.mark.parametrize("scope", ["slide", "deck"])
@pytest.mark.parametrize("invalid", [
    "cannot_verify_without_link", "defect_without_link", "satisfied_with_link",
    "duplicate_link", "missing_accuracy", "missing_coverage",
    "unresolved_with_empty_issues", "unresolved_without_issues",
])
def test_advertised_schema_rejects_inconsistent_linkage_before_runtime(
    tmp_path, monkeypatch, scope, invalid,
):
    slide = _slide(tmp_path)
    calls = _capture(monkeypatch, _response(slide))
    assert _review([slide], scope).issues == []
    schema = json.loads(calls[0]["system_prompt"].split("JSON Schema:\n", 1)[1])
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    response = _response(slide, findings=[_finding(slide)], checks=[
        _check(slide, verdict="verified_defect", links=[0]),
        _check(slide, check_type="source_coverage", object_ids=[]),
    ])
    assert validator.is_valid(response)
    check = response["verification"][0]
    if invalid == "cannot_verify_without_link":
        check.update(verdict="cannot_verify", issue_indices=[])
    elif invalid == "defect_without_link":
        check["issue_indices"] = []
    elif invalid == "satisfied_with_link":
        check["verdict"] = "verified_satisfied"
    elif invalid == "duplicate_link":
        check["issue_indices"] = [0, 0]
    elif invalid == "missing_accuracy":
        response["verification"] = response["verification"][1:]
        response["issues"] = []
    elif invalid == "missing_coverage":
        response["verification"] = response["verification"][:1]
    elif invalid == "unresolved_with_empty_issues":
        response["issues"] = []
    else:
        response.pop("issues")
    assert not validator.is_valid(response)
    # The existing runtime must continue to fail closed, not be weakened to
    # match an incomplete advertised schema.
    _capture(monkeypatch, response)
    with pytest.raises(StructuredTaskValidationError):
        _review([slide], scope)


@pytest.mark.parametrize("scope", ["slide", "deck"])
@pytest.mark.parametrize("verdict", ["verified_defect", "cannot_verify"])
def test_advertised_linkage_preserves_findings_and_shared_schema(tmp_path, monkeypatch, scope, verdict):
    slide = _slide(tmp_path)
    shared = deepcopy(_SlideVisualReview.model_json_schema())
    response = _response(slide, findings=[_finding(slide)], checks=[
        _check(slide, verdict=verdict, links=[0]),
        _check(slide, check_type="source_coverage", object_ids=[]),
    ])
    calls = _capture(monkeypatch, response)
    result = _review([slide], scope)
    schema = json.loads(calls[0]["system_prompt"].split("JSON Schema:\n", 1)[1])
    assert Draft202012Validator(schema).is_valid(response)
    assert len(result.issues) == 1
    _, issues, verified, outcome = PresentationService._visual_result_issues(result, [])
    assert len(issues) == 1 and issues[0].severity == "error"
    assert not verified and outcome == "failed"
    assert _SlideVisualReview.model_json_schema() == shared


def test_advertised_check_types_are_per_call_not_per_page_and_legacy_remains_valid(tmp_path):
    first = _slide(tmp_path)
    other = _slide(tmp_path, page="page-b", source_ref="different-source")
    response = _response(first, checks=[
        _check(first),
        _check(other, check_type="source_coverage", object_ids=[], source_refs=["different-source"]),
    ])
    critic = UtilityPresentationVisualCritic("synthetic-schema-agent")
    schema = critic._review_result_model([first, other]).model_json_schema()
    assert Draft202012Validator(schema).is_valid(response)
    legacy = _slide(tmp_path, page="legacy", with_source=False)
    legacy_schema = critic._review_result_model([legacy]).model_json_schema()
    assert Draft202012Validator(legacy_schema).is_valid({"issues": [_finding(legacy)]})
    assert not Draft202012Validator(schema).is_valid({"issues": []})
    assert not Draft202012Validator(legacy_schema).is_valid(response)


@pytest.mark.parametrize("diagnostic", [
    "verification_error@$.verification",
    "verification_error@$.verification[0].slide_id",
    "verification_error@$.verification[0].object_ids",
    "verification_error@$.verification[0].check_type",
    "verification_error@$.verification[0].verdict",
    "verification_error@$.verification[0].source_refs",
    "verification_error@$.verification[0].issue_indices",
    "verification_error@$.issues[0]",
    "verification_extra_fields@$.verification[0]",
    "verification_blank_evidence@$.verification[0].source_evidence",
    "verification_blank_evidence@$.verification[0].candidate_evidence",
    "verification_blank_evidence@$.verification[0].explanation",
    "int_type@$.verification[0].issue_indices[0]",
])
def test_private_protocol_retry_keeps_only_owned_diagnostics(diagnostic):
    error = StructuredTaskValidationError("Do not repeat this message", validation_issues=(diagnostic,))
    assert _safe_visual_validation_issues(error) == (diagnostic,)


def test_private_protocol_diagnostics_still_redact_values_unknown_keys_and_large_indices():
    marker = "SYNTHETIC_PRIVATE_RETRY_MARKER_8817"
    issues = (
        f"verification_error@$.verification[0].{marker}",
        f"verification_extra_fields@$.verification[0].{marker}",
        f"{marker}@$.verification[0].issue_indices",
        "verification_error@$.verification[1000000].issue_indices",
        "verification_error@$.verification[0].issue_indices",
    )
    result = _safe_visual_validation_issues(StructuredTaskValidationError(marker, validation_issues=issues))
    assert result == (
        "verification_error@$", "verification_extra_fields@$", "schema_error@$",
        "verification_error@$.verification[0].issue_indices",
    )
    assert marker not in json.dumps(result)
    repeated = _safe_visual_validation_issues(StructuredTaskValidationError(
        marker, validation_issues=("verification_error@$.verification[0].issue_indices",) * 1000,
    ))
    assert len(repeated) == 1
    bounded = _safe_visual_validation_issues(StructuredTaskValidationError(
        marker, validation_issues=tuple(f"verification_error@$.verification[{i}].issue_indices" for i in range(1000)),
    ))
    assert len(bounded) == 24


@pytest.mark.parametrize("failures", [1, 3])
def test_actual_service_retry_keeps_link_location_without_more_attempts_or_private_text(
    tmp_path, monkeypatch, failures,
):
    monkeypatch.setattr("lantai.presentation.service.render_pptx", _render_for_edit_tests)
    marker = "SYNTHETIC_PRIVATE_RETRY_MARKER_8817"

    class LinkageCritic(_CheckpointVisualCritic):
        def __init__(self):
            super().__init__()
            self.directives = []

        def review_deck(self, slides, *, retry_directive=None):
            self.deck_calls += 1
            self.directives.append(retry_directive)
            if self.deck_calls <= failures:
                raise StructuredTaskValidationError(marker, validation_issues=(
                    "verification_error@$.verification[0].issue_indices",
                    f"verification_error@$.verification[0].{marker}",
                ))
            return VisualCriticResult(critic_id="synthetic-linkage-critic", issues=[])

    critic = LinkageCritic()
    service = PresentationService(tmp_path, visual_critic=critic)
    record = service.create_job(_request())
    deck = _two_slide_visual_deck()
    output = service.store.job_dir(record.job_id) / "candidate.pptx"
    service.compiler.compile(deck, output)
    report, _ = service.verify_artifact(
        deck, output, service.store.job_dir(record.job_id) / "renders" / "schema-linkage",
        vision_critique_required=True, job_id=record.job_id,
    )
    assert critic.deck_calls == 2
    assert critic.directives[0] is None
    assert "verification_error@$.verification[0].issue_indices" in critic.directives[1]
    assert marker not in str(critic.directives) and marker not in report.model_dump_json()
    assert report.visual_verified == (failures == 1)
    saved = service.store.get(record.job_id)
    assert saved.cost_used_units == 4
    metrics = [m for m in saved.stage_metrics if m.operation == "critic" and m.slide_id is None]
    assert [m.cost_units for m in metrics] == [1, 1]
    assert [m.outcome for m in metrics] == (["failed", "passed"] if failures == 1 else ["failed", "failed"])
    if failures > 1:
        assert any(issue.code == "vision.critic_schema_invalid" for issue in report.issues)


@pytest.mark.parametrize("scope", ["slide", "deck"])
def test_stricter_advertised_linkage_invalidates_previous_checkpoints(monkeypatch, scope):
    monkeypatch.setattr("lantai.presentation.intelligence._utility_route_cache_identity", lambda _: "test-route")
    critic = UtilityPresentationVisualCritic("synthetic-schema-agent")
    identity = critic.slide_cache_identity() if scope == "slide" else critic.deck_cache_identity()
    assert "/evidence-linked-content-review-v1/linked-review-schema-v1/" in identity
