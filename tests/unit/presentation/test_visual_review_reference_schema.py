"""The advertised critic schema must mirror strict same-page references."""

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
from lantai.presentation.models import TextObject, VisualCriticSlideInput, VisualIssue
from lantai.presentation.service import PresentationService


def _slide(tmp_path, *, page="page-a", object_ids=None):
    path = tmp_path / f"{page}.png"
    Image.new("RGB", (320, 180), "white").save(path)
    ids = [f"{page}::claim", "legacy-object"] if object_ids is None else object_ids
    return VisualCriticSlideInput(
        slide_id=page, slide_index=1, rendered_path=str(path),
        rendered_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        key_message="A synthetic claim remains subject to source verification.",
        original_user_request="Do not infer approval or waive a source defect.",
        speaker_notes="Candidate note, not independent evidence.",
        object_ids=ids,
        slide_objects=[TextObject(id=value, role="body", text="Pending.") for value in ids],
    )


def _finding(slide, *, object_ids=None, severity="error", issue_type="content_accuracy"):
    return dict(
        slide_id=slide.slide_id,
        object_ids=slide.object_ids if object_ids is None else object_ids,
        severity=severity, issue_type=issue_type,
        description="A synthetic source discrepancy must remain a defect.",
        proposed_action="Correct the claim only after checking the original source.",
    )


def _capture(monkeypatch, findings):
    calls = []

    async def transport(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text=json.dumps({"issues": findings}), model_label="synthetic-vision")

    # Keep production context, formatter-facing schema, structured parser and
    # reference checks. Only this external transport is a synthetic fixture.
    monkeypatch.setattr("lantai.app.structured_utility.run_utility_content_task", transport)
    monkeypatch.setattr(
        "lantai.presentation.intelligence._utility_route_cache_identity", lambda _: "test-route",
    )
    return calls


def _review(slides, scope):
    critic = UtilityPresentationVisualCritic("test-agent")
    return critic.review_slide(slides[0]) if scope == "slide" else critic.review_deck(slides)


def _schema(call):
    schema = json.loads(call["system_prompt"].split("JSON Schema:\n", 1)[1])
    Draft202012Validator.check_schema(schema)
    return schema


@pytest.mark.parametrize("scope", ["slide", "deck"])
@pytest.mark.parametrize("invalid", ["unknown_slide", "unknown_object", "other_page_object"])
def test_public_critic_advertised_schema_rejects_unknown_and_cross_page_references(
    tmp_path, monkeypatch, scope, invalid,
):
    first = _slide(tmp_path)
    other = _slide(tmp_path, page="page-b")
    other.slide_index = 2
    finding = _finding(first)
    calls = _capture(monkeypatch, [finding])
    _review([first, other], scope)
    validator = Draft202012Validator(_schema(calls[0]))
    assert validator.is_valid({"issues": [finding]})
    rejected = deepcopy(finding)
    if invalid == "unknown_slide":
        rejected["slide_id"] = "not-an-input-page"
    elif invalid == "unknown_object":
        rejected["object_ids"] = ["not-an-input-object"]
    else:
        rejected["object_ids"] = [other.object_ids[0]]
    assert not validator.is_valid({"issues": [rejected]})


@pytest.mark.parametrize("scope", ["slide", "deck"])
@pytest.mark.parametrize("representation", ["canonical", "same_page_alias", "unlocalized"])
def test_public_critic_preserves_exact_references_existing_aliases_and_unlocalized_findings(
    tmp_path, monkeypatch, scope, representation,
):
    slide = _slide(tmp_path)
    before = slide.model_dump(mode="json")
    selected = {"canonical": slide.object_ids,
                "same_page_alias": ["claim", "legacy-object"],
                "unlocalized": []}[representation]
    finding = _finding(slide, object_ids=selected)
    calls = _capture(monkeypatch, [finding])
    result = _review([slide], scope)
    assert Draft202012Validator(_schema(calls[0])).is_valid({"issues": [finding]})
    expected = _finding(slide, object_ids=[] if representation == "unlocalized" else slide.object_ids)
    assert result.issues == [VisualIssue.model_validate(expected)]
    assert slide.model_dump(mode="json") == before
    context = json.loads(calls[0]["user_content"][1]["text"])
    page = context if scope == "slide" else context["ordered_slides"][0]
    assert page["object_ids"] == before["object_ids"]
    assert page["speaker_notes"] == before["speaker_notes"]
    assert page["slide_objects"] == UtilityPresentationVisualCritic._slide_context(slide)["slide_objects"]


@pytest.mark.parametrize("scope", ["slide", "deck"])
def test_public_critic_advertised_schema_allows_empty_objects_only_when_input_has_no_objects(
    tmp_path, monkeypatch, scope,
):
    slide = _slide(tmp_path, object_ids=[])
    finding = _finding(slide)
    calls = _capture(monkeypatch, [finding])
    assert _review([slide], scope).issues == [VisualIssue.model_validate(finding)]
    validator = Draft202012Validator(_schema(calls[0]))
    assert validator.is_valid({"issues": [finding]})
    assert validator.is_valid({"issues": []})
    rejected = deepcopy(finding)
    rejected["object_ids"] = ["invented"]
    assert not validator.is_valid({"issues": [rejected]})


@pytest.mark.parametrize("scope", ["slide", "deck"])
@pytest.mark.parametrize("severity", ["info", "warning", "error"])
@pytest.mark.parametrize("issue_type", ["content_accuracy", "source_coverage"])
def test_scoped_reference_schema_does_not_filter_findings_or_weaken_fact_gate(
    tmp_path, monkeypatch, scope, severity, issue_type,
):
    slide = _slide(tmp_path)
    finding = _finding(slide, severity=severity, issue_type=issue_type)
    calls = _capture(monkeypatch, [finding])
    result = _review([slide], scope)
    assert Draft202012Validator(_schema(calls[0])).is_valid({"issues": [finding]})
    assert result.issues == [VisualIssue.model_validate(finding)]
    quality = []
    _, issues, verified, outcome = PresentationService._visual_result_issues(result, quality)
    assert len(issues) == len(quality) == 1
    assert issues[0].severity == quality[0].severity == "error"
    assert not verified and outcome == "failed"


@pytest.mark.parametrize("scope", ["slide", "deck"])
@pytest.mark.parametrize("invalid", ["unknown_slide", "unknown_object", "other_page_object"])
def test_scoped_schema_retains_existing_runtime_unknown_reference_failure(
    tmp_path, monkeypatch, scope, invalid,
):
    first = _slide(tmp_path)
    other = _slide(tmp_path, page="page-b")
    finding = _finding(first)
    if invalid == "unknown_slide":
        finding["slide_id"] = "not-an-input-page"
    elif invalid == "unknown_object":
        finding["object_ids"] = ["private-marker-do-not-echo"]
    else:
        finding["object_ids"] = [other.object_ids[0]]
    _capture(monkeypatch, [finding])
    with pytest.raises(StructuredTaskValidationError, match="unknown") as caught:
        _review([first, other], scope)
    assert "private-marker-do-not-echo" not in str(caught.value)
    assert caught.value.validation_issues[0].startswith("reference_error@$.issues[0].")


@pytest.mark.parametrize("scope", ["slide", "deck"])
def test_critic_reference_schema_is_call_local_and_does_not_mutate_shared_models(
    tmp_path, monkeypatch, scope,
):
    first = _slide(tmp_path)
    other = _slide(tmp_path, page="page-b")
    shared_schema = deepcopy(_SlideVisualReview.model_json_schema())
    first_calls = _capture(monkeypatch, [_finding(first)])
    _review([first], scope)
    first_schema = _schema(first_calls[0])
    other_calls = _capture(monkeypatch, [_finding(other)])
    _review([other], scope)
    other_schema = _schema(other_calls[0])
    assert first_schema != other_schema
    assert Draft202012Validator(first_schema).is_valid({"issues": [_finding(first)]})
    assert not Draft202012Validator(first_schema).is_valid({"issues": [_finding(other)]})
    assert Draft202012Validator(other_schema).is_valid({"issues": [_finding(other)]})
    assert not Draft202012Validator(other_schema).is_valid({"issues": [_finding(first)]})
    assert _SlideVisualReview.model_json_schema() == shared_schema


@pytest.mark.parametrize("scope", ["slide", "deck"])
def test_reference_schema_rules_invalidate_previous_unscoped_critic_checkpoints(monkeypatch, scope):
    monkeypatch.setattr(
        "lantai.presentation.intelligence._utility_route_cache_identity", lambda _: "test-route",
    )
    critic = UtilityPresentationVisualCritic("test-agent")
    identity = critic.slide_cache_identity() if scope == "slide" else critic.deck_cache_identity()
    assert "/native-notes-context-v1/scoped-review-references-v1/" in identity


@pytest.mark.parametrize("scope", ["slide", "deck"])
@pytest.mark.parametrize("selected,accepted", [
    ("page-a::Claim", True),
    ("Claim", True),
    ("page-a::group::claim", True),
    ('quote"claim', True),
    ("原生/图表[1]", True),
    ("page-b::supplied-in-page-a", True),
    ("claim", False),
    ("group::claim", False),
    ('page-b::quote"claim', False),
    ("原生/图表[2]", False),
])
def test_scoped_schema_and_runtime_agree_on_literal_ids_without_guessing(
    tmp_path, monkeypatch, scope, selected, accepted,
):
    slide = _slide(tmp_path, object_ids=[
        "page-a::Claim", "page-a::group::claim", 'page-a::quote"claim',
        "原生/图表[1]", "page-b::supplied-in-page-a",
    ])
    calls = _capture(monkeypatch, [_finding(slide, object_ids=[])])
    _review([slide], scope)
    finding = _finding(slide, object_ids=[selected])
    assert Draft202012Validator(_schema(calls[0])).is_valid({"issues": [finding]}) is accepted
    _capture(monkeypatch, [finding])
    if accepted:
        result = _review([slide], scope)
        expected = selected if selected in slide.object_ids else f"page-a::{selected}"
        assert result.issues[0].object_ids == [expected]
    else:
        with pytest.raises(StructuredTaskValidationError, match="unknown"):
            _review([slide], scope)


@pytest.mark.parametrize("scope", ["slide", "deck"])
@pytest.mark.parametrize("missing", ["slide_id", "severity", "issue_type", "description", "proposed_action"])
def test_scoped_schema_retains_all_base_required_issue_fields(tmp_path, monkeypatch, scope, missing):
    slide = _slide(tmp_path)
    finding = _finding(slide)
    calls = _capture(monkeypatch, [finding])
    _review([slide], scope)
    del finding[missing]
    assert not Draft202012Validator(_schema(calls[0])).is_valid({"issues": [finding]})
