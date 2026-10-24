"""Source checks and defects are separate, but neither may silently disappear."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from PIL import Image

from lantai.app.structured_utility import StructuredTaskValidationError
from lantai.presentation.intelligence import UtilityPresentationVisualCritic, _SlideVisualReview
from lantai.presentation.models import (
    FactRecord, PresentationBrief, PresentationSourceSection, TextObject,
    VisualCriticResult, VisualCriticSlideInput, VisualIssue,
)
from lantai.presentation.service import PresentationService


def _slide(tmp_path, *, page="page-a", with_source=True, source_ref="synthetic-original"):
    path = tmp_path / f"{page}.png"
    Image.new("RGB", (320, 180), "white").save(path)
    return VisualCriticSlideInput(
        slide_id=page, slide_index=1, rendered_path=str(path),
        rendered_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        original_user_request="Keep the sample size and the pending decision state.",
        source_brief=PresentationBrief(
            objective="Compare evidence with a candidate", audience="Synthetic reviewers",
            narrative="Observations before decisions", required_facts=["Decision pending"],
            source_sections=[PresentationSourceSection(
                title="Synthetic source", source_ref=source_ref, source_refs=[source_ref + "-merged"],
                body="Twelve testers participated. The decision remains pending.",
            )],
        ) if with_source else None,
        key_message="Twelve testers; decision pending", speaker_notes="Pending is not approved.",
        object_ids=[f"{page}::claim", f"{page}::decision"],
        slide_objects=[
            TextObject(id=f"{page}::claim", role="body", text="Twelve testers participated."),
            TextObject(id=f"{page}::decision", role="body", text="Decision pending."),
        ],
    )


def _finding(slide, *, issue_type="content_accuracy", severity="error", object_ids=None):
    return dict(
        slide_id=slide.slide_id, object_ids=object_ids if object_ids is not None else slide.object_ids[:1],
        issue_type=issue_type, severity=severity,
        description="An authored discrepancy must remain a defect.",
        proposed_action="Correct the candidate after checking the complete original source.",
    )


def _check(slide, *, check_type="content_accuracy", verdict="verified_satisfied", links=None,
           object_ids=None, source_refs=None):
    return dict(
        slide_id=slide.slide_id, object_ids=slide.object_ids[:1] if object_ids is None else object_ids,
        check_type=check_type, verdict=verdict,
        source_refs=source_refs if source_refs is not None else ["synthetic-original"],
        source_evidence="Twelve testers participated; the decision remains pending.",
        candidate_evidence="Twelve testers participated; decision pending.",
        explanation="An authored parser fixture, not an independent quality judgment.",
        issue_indices=[] if links is None else links,
    )


def _response(slide, *, findings=None, checks=None):
    return {"verification": checks if checks is not None else [
        _check(slide), _check(slide, check_type="source_coverage", object_ids=[]),
    ], "issues": [] if findings is None else findings}


def _capture(monkeypatch, response):
    calls = []

    async def transport(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text=json.dumps(response), model_label="synthetic-vision")

    # Only external transport is synthetic. Production packing, schema, parsing,
    # runtime reference checks and the source-quality gate all remain real.
    monkeypatch.setattr("lantai.app.structured_utility.run_utility_content_task", transport)
    monkeypatch.setattr(
        "lantai.presentation.intelligence._utility_route_cache_identity", lambda _: "test-route",
    )
    return calls


def _review(slides, scope):
    critic = UtilityPresentationVisualCritic("test-agent")
    return critic.review_slide(slides[0]) if scope == "slide" else critic.review_deck(slides)


@pytest.mark.parametrize("scope", ["slide", "deck"])
def test_satisfied_checks_stay_out_of_issues_without_losing_original_evidence(tmp_path, monkeypatch, scope):
    slide = _slide(tmp_path)
    before = slide.model_dump(mode="json")
    response = _response(slide)
    calls = _capture(monkeypatch, response)
    assert _review([slide], scope).issues == []
    assert slide.model_dump(mode="json") == before
    schema = json.loads(calls[0]["system_prompt"].split("JSON Schema:\n", 1)[1])
    Draft202012Validator.check_schema(schema)
    assert list(schema["properties"])[:2] == ["verification", "issues"]
    assert "verification" in schema["required"]
    assert Draft202012Validator(schema).is_valid(response)
    assert not Draft202012Validator(schema).is_valid({"issues": []})
    context = json.loads(calls[0]["user_content"][1]["text"])
    contract = context["review_output_contract"]
    assert contract["verification_role"] == "source_checks_before_defect_summary"
    assert contract["issue_link_policy"]["index_base"] == 0
    assert "issue_indices" in calls[0]["system_prompt"]


@pytest.mark.parametrize("scope", ["slide", "deck"])
@pytest.mark.parametrize("severity", ["info", "warning", "error"])
@pytest.mark.parametrize("issue_type", ["content_accuracy", "source_coverage"])
@pytest.mark.parametrize("verdict", ["verified_defect", "cannot_verify"])
def test_linked_source_findings_survive_unchanged_and_always_block_fresh_and_cached_quality(
    tmp_path, monkeypatch, scope, severity, issue_type, verdict,
):
    slide = _slide(tmp_path)
    finding = _finding(slide, issue_type=issue_type, severity=severity)
    other_type = "source_coverage" if issue_type == "content_accuracy" else "content_accuracy"
    response = _response(slide, findings=[finding], checks=[
        _check(slide, check_type=issue_type, verdict=verdict, links=[0]),
        _check(slide, check_type=other_type, object_ids=[]),
    ])
    _capture(monkeypatch, response)
    result = _review([slide], scope)
    assert result.issues == [VisualIssue.model_validate(finding)]
    for value in (result, VisualCriticResult.model_validate_json(result.model_dump_json())):
        before = value.model_dump(mode="json")
        quality = []
        _, issues, verified, outcome = PresentationService._visual_result_issues(value, quality)
        assert len(issues) == len(quality) == 1
        assert issues[0].severity == quality[0].severity == "error"
        assert not verified and outcome == "failed"
        assert value.model_dump(mode="json") == before


@pytest.mark.parametrize("scope", ["slide", "deck"])
@pytest.mark.parametrize("invalid", [
    "missing_ledger", "empty_ledger", "missing_check_type", "missing_defect_issue",
    "unlinked_content_issue", "cannot_verify_without_issue", "satisfied_links_to_issue",
    "wrong_link_type", "wrong_link_page", "wrong_link_object", "out_of_range_link",
    "duplicate_link", "negative_link", "unlocalized_link_to_localized_issue",
])
def test_source_review_fails_closed_on_incomplete_or_inconsistent_checks(
    tmp_path, monkeypatch, scope, invalid,
):
    slide = _slide(tmp_path)
    other = _slide(tmp_path, page="page-b")
    finding = _finding(slide)
    defect = _check(slide, verdict="verified_defect", links=[0])
    coverage = _check(slide, check_type="source_coverage", object_ids=[])
    response = _response(slide, findings=[finding], checks=[defect, coverage])
    if invalid == "missing_ledger":
        response.pop("verification")
    elif invalid == "empty_ledger":
        response["verification"] = []
    elif invalid == "missing_check_type":
        response["verification"] = [defect]
    elif invalid == "missing_defect_issue":
        # Reproduce the real failure pattern without retaining a Provider reply:
        # two findings are present but another checked object has a dropped defect.
        response["verification"].append(_check(
            slide, verdict="verified_defect", object_ids=slide.object_ids[1:], links=[],
        ))
    elif invalid == "unlinked_content_issue":
        response["verification"][0] = _check(slide)
    elif invalid == "cannot_verify_without_issue":
        response["issues"] = []
        defect.update(verdict="cannot_verify", issue_indices=[])
    elif invalid == "satisfied_links_to_issue":
        defect["verdict"] = "verified_satisfied"
    elif invalid == "wrong_link_type":
        defect["check_type"] = "source_coverage"
    elif invalid == "wrong_link_page":
        finding["slide_id"] = other.slide_id
        finding["object_ids"] = other.object_ids[:1]
    elif invalid == "wrong_link_object":
        defect["object_ids"] = slide.object_ids[1:]
    elif invalid == "out_of_range_link":
        defect["issue_indices"] = [1]
    elif invalid == "duplicate_link":
        defect["issue_indices"] = [0, 0]
    elif invalid == "negative_link":
        defect["issue_indices"] = [-1]
    else:
        defect["object_ids"] = []
    _capture(monkeypatch, response)
    with pytest.raises(StructuredTaskValidationError):
        _review([slide, other] if scope == "deck" else [slide], scope)


@pytest.mark.parametrize("scope", ["slide", "deck"])
@pytest.mark.parametrize("invalid", [
    "unknown_slide", "unknown_object", "cross_page_object", "unknown_source",
    "generated_fact_as_source", "cross_page_source", "no_source_refs",
    "blank_source", "blank_candidate", "blank_explanation", "extra_field",
])
def test_check_references_and_evidence_are_strict_scoped_and_private(tmp_path, monkeypatch, scope, invalid):
    first = _slide(tmp_path)
    other = _slide(tmp_path, page="page-b", source_ref="different-source")
    response = _response(first)
    check = response["verification"][0]
    marker = "private-marker-never-echo"
    first.source_facts = [FactRecord(
        fact_id="candidate-claim", text="An unverified generated claim.", source_refs=[marker],
    )]
    if invalid == "unknown_slide":
        check["slide_id"] = marker
    elif invalid == "unknown_object":
        check["object_ids"] = [marker]
    elif invalid == "cross_page_object":
        check["object_ids"] = other.object_ids[:1]
    elif invalid in {"unknown_source", "generated_fact_as_source", "cross_page_source"}:
        check["source_refs"] = ["different-source" if invalid == "cross_page_source" else marker]
    elif invalid == "no_source_refs":
        check["source_refs"] = []
    elif invalid.startswith("blank_"):
        field = {"blank_source": "source_evidence", "blank_candidate": "candidate_evidence",
                 "blank_explanation": "explanation"}[invalid]
        check[field] = " \n\t "
    else:
        check[marker] = marker
    _capture(monkeypatch, response)
    with pytest.raises(StructuredTaskValidationError) as caught:
        _review([first, other] if scope == "deck" else [first], scope)
    assert marker not in str(caught.value)


@pytest.mark.parametrize("scope", ["slide", "deck"])
@pytest.mark.parametrize("source_ref", [
    "synthetic-original", "synthetic-original-merged", "source_brief", "original_user_request",
])
def test_actual_source_locators_and_same_page_aliases_are_advertised_and_accepted(
    tmp_path, monkeypatch, scope, source_ref,
):
    slide = _slide(tmp_path)
    response = _response(slide)
    response["verification"][0].update(object_ids=["claim"], source_refs=[source_ref])
    calls = _capture(monkeypatch, response)
    assert _review([slide], scope).issues == []
    schema = json.loads(calls[0]["system_prompt"].split("JSON Schema:\n", 1)[1])
    validator = Draft202012Validator(schema)
    assert validator.is_valid(response)
    rejected = deepcopy(response)
    rejected["verification"][0]["source_refs"] = ["fabricated-source"]
    assert not validator.is_valid(rejected)
    rejected = deepcopy(response)
    rejected["verification"][0]["object_ids"] = ["fabricated-object"]
    assert not validator.is_valid(rejected)


@pytest.mark.parametrize("scope", ["slide", "deck"])
def test_visual_findings_are_not_turned_into_content_defects(tmp_path, monkeypatch, scope):
    slide = _slide(tmp_path)
    finding = _finding(slide, issue_type="whitespace", severity="warning")
    _capture(monkeypatch, _response(slide, findings=[finding]))
    assert _review([slide], scope).issues == [VisualIssue.model_validate(finding)]


@pytest.mark.parametrize("scope", ["slide", "deck"])
def test_satisfied_check_cannot_cancel_another_claims_defect_on_the_same_object(
    tmp_path, monkeypatch, scope,
):
    slide = _slide(tmp_path)
    finding = _finding(slide)
    response = _response(slide, findings=[finding], checks=[
        _check(slide), _check(slide, verdict="verified_defect", links=[0]),
        _check(slide, check_type="source_coverage", object_ids=[]),
    ])
    _capture(monkeypatch, response)
    assert _review([slide], scope).issues == [VisualIssue.model_validate(finding)]


@pytest.mark.parametrize("scope", ["slide", "deck"])
@pytest.mark.parametrize("complete", [True, False])
def test_every_object_in_a_content_issue_requires_a_linked_check(tmp_path, monkeypatch, scope, complete):
    slide = _slide(tmp_path)
    finding = _finding(slide, object_ids=slide.object_ids)
    checks = [
        _check(slide, verdict="verified_defect", links=[0]),
        _check(slide, check_type="source_coverage", object_ids=[]),
    ]
    if complete:
        checks.append(_check(slide, verdict="verified_defect", links=[0], object_ids=slide.object_ids[1:]))
    _capture(monkeypatch, _response(slide, findings=[finding], checks=checks))
    if complete:
        assert _review([slide], scope).issues == [VisualIssue.model_validate(finding)]
    else:
        with pytest.raises(StructuredTaskValidationError):
            _review([slide], scope)


@pytest.mark.parametrize("scope", ["slide", "deck"])
@pytest.mark.parametrize("invalid", [True, 0.0, "0", None])
def test_issue_indices_are_exact_integers_not_coerced_values(tmp_path, monkeypatch, scope, invalid):
    slide = _slide(tmp_path)
    response = _response(slide, findings=[_finding(slide)], checks=[
        _check(slide, verdict="verified_defect", links=[invalid]),
        _check(slide, check_type="source_coverage", object_ids=[]),
    ])
    _capture(monkeypatch, response)
    with pytest.raises(StructuredTaskValidationError):
        _review([slide], scope)


def test_whole_deck_checks_do_not_require_repeating_both_types_on_every_page(tmp_path, monkeypatch):
    first = _slide(tmp_path)
    other = _slide(tmp_path, page="page-b", source_ref="different-source")
    response = _response(first, checks=[
        _check(first), _check(other, check_type="source_coverage", object_ids=[], source_refs=["different-source"]),
    ])
    _capture(monkeypatch, response)
    assert _review([first, other], "deck").issues == []


@pytest.mark.parametrize("scope", ["slide", "deck"])
def test_no_original_brief_remains_compatible_without_fabricating_source_checks(tmp_path, monkeypatch, scope):
    slide = _slide(tmp_path, with_source=False)
    finding = _finding(slide)
    _capture(monkeypatch, {"issues": [finding]})
    assert _review([slide], scope).issues == [VisualIssue.model_validate(finding)]


@pytest.mark.parametrize("scope", ["slide", "deck"])
def test_check_schema_is_call_local_and_prior_checkpoints_are_invalidated(tmp_path, monkeypatch, scope):
    first = _slide(tmp_path)
    other = _slide(tmp_path, page="page-b", source_ref="different-source")
    before = deepcopy(_SlideVisualReview.model_json_schema())
    critic = UtilityPresentationVisualCritic("test-agent")
    schema_a = critic._review_result_model([first]).model_json_schema()
    schema_b = critic._review_result_model([other]).model_json_schema()
    assert schema_a != schema_b
    assert _SlideVisualReview.model_json_schema() == before
    monkeypatch.setattr(
        "lantai.presentation.intelligence._utility_route_cache_identity", lambda _: "test-route",
    )
    identity = critic.slide_cache_identity() if scope == "slide" else critic.deck_cache_identity()
    assert "/scoped-review-references-v1/evidence-linked-content-review-v1/" in identity
