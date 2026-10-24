"""Schema-owned review field instructions must neither omit nor repair records."""
from __future__ import annotations
from copy import deepcopy
import json

import pytest
from jsonschema import Draft202012Validator

from lantai.app.structured_utility import StructuredTaskValidationError
from lantai.presentation.intelligence import (
    UtilityPresentationVisualCritic, _ContentReviewVerification, _SlideVisualReview,
)
from lantai.presentation.models import VisualIssue
from tests.unit.presentation.test_visual_review_evidence_linkage import (
    _capture, _check, _finding, _response, _review, _slide,
)

@pytest.mark.parametrize("scope", ["slide", "deck"])
@pytest.mark.parametrize("with_source", [True, False])
def test_public_contract_lists_nested_fields_without_granting_root_context_authority(
    tmp_path, monkeypatch, scope, with_source,
):
    slide = _slide(tmp_path, with_source=with_source)
    response = _response(slide) if with_source else {"issues": [_finding(slide)]}
    shared = deepcopy(_SlideVisualReview.model_json_schema())
    before = slide.model_dump(mode="json")
    calls = _capture(monkeypatch, response)
    result = _review([slide], scope)
    context = json.loads(calls[0]["user_content"][1]["text"])
    contract = context["review_output_contract"]
    root = _SlideVisualReview.model_json_schema()
    verification = _ContentReviewVerification.model_json_schema()
    assert contract["allowed_root_fields"] == list(root["properties"])
    assert contract["required_root_fields_with_source"] == ["verification"]
    assert contract["required_verification_fields"] == verification["required"]
    assert contract["allowed_verification_fields"] == list(verification["properties"])
    assert {"slide_id", "issue_indices", "source_evidence", "candidate_evidence", "explanation"}.issubset(
        contract["required_verification_fields"],
    )
    assert contract["verification_values"] == {
        "check_type": verification["properties"]["check_type"]["enum"],
        "verdict": verification["properties"]["verdict"]["enum"],
    }
    assert contract["field_scope_policy"] == {
        "unknown_fields": "forbidden",
        "slide_id_at_root": "forbidden",
        "input_context_fields_at_root": "forbidden",
        "missing_fields": "regenerate_without_inference_or_local_fill",
    }
    assert set(contract["verification_field_rules"]) == {"slide_id", "issue_indices", "evidence"}
    assert contract["issue_link_policy"]["index_base"] == 0
    assert contract["required_issue_fields"] == VisualIssue.model_json_schema()["required"]
    assert _SlideVisualReview.model_json_schema() == shared
    assert slide.model_dump(mode="json") == before
    assert result.issues == ([] if with_source else [VisualIssue.model_validate(_finding(slide))])

@pytest.mark.parametrize("scope", ["slide", "deck"])
@pytest.mark.parametrize("field", _ContentReviewVerification.model_json_schema()["required"])
def test_missing_required_check_fields_remain_parser_errors_not_filled(
    tmp_path, monkeypatch, scope, field,
):
    slide = _slide(tmp_path)
    response = _response(slide)
    response["verification"][0].pop(field)
    before = deepcopy(response)
    _capture(monkeypatch, response)
    with pytest.raises(StructuredTaskValidationError):
        _review([slide], scope)
    assert response == before
    schema = UtilityPresentationVisualCritic("synthetic-field-agent")._review_result_model([slide]).model_json_schema()
    assert not Draft202012Validator(schema).is_valid(response)

@pytest.mark.parametrize("scope", ["slide", "deck"])
@pytest.mark.parametrize("location", ["root", "verification", "issue"])
def test_extra_context_or_unknown_fields_are_never_silently_dropped(
    tmp_path, monkeypatch, scope, location,
):
    slide = _slide(tmp_path)
    response = _response(slide, findings=[_finding(slide)], checks=[
        _check(slide, verdict="verified_defect", links=[0]),
        _check(slide, check_type="source_coverage", object_ids=[]),
    ])
    if location == "root":
        response["slide_id"] = slide.slide_id
    elif location == "verification":
        response["verification"][0]["synthetic_private_extra_key"] = "Synthetic private value"
    else:
        response["issues"][0]["synthetic_private_extra_key"] = "Synthetic private value"
    before = deepcopy(response)
    _capture(monkeypatch, response)
    with pytest.raises(StructuredTaskValidationError):
        _review([slide], scope)
    assert response == before

@pytest.mark.parametrize("scope", ["slide", "deck"])
@pytest.mark.parametrize("verdict", ["verified_defect", "cannot_verify"])
def test_field_instructions_leave_valid_unresolved_findings_exactly_unchanged(
    tmp_path, monkeypatch, scope, verdict,
):
    slide = _slide(tmp_path)
    response = _response(slide, findings=[_finding(slide)], checks=[
        _check(slide, verdict=verdict, links=[0]),
        _check(slide, check_type="source_coverage", object_ids=[]),
    ])
    _capture(monkeypatch, response)
    result = _review([slide], scope)
    assert result.issues == [VisualIssue.model_validate(response["issues"][0])]

@pytest.mark.parametrize("scope", ["slide", "deck"])
def test_protocol_field_instructions_invalidate_prior_cached_reviews(monkeypatch, scope):
    monkeypatch.setattr("lantai.presentation.intelligence._utility_route_cache_identity", lambda _: "synthetic-route")
    critic = UtilityPresentationVisualCritic("synthetic-field-agent")
    identity = critic.slide_cache_identity() if scope == "slide" else critic.deck_cache_identity()
    assert "/linked-review-schema-v1/protocol-field-contract-v1/" in identity

