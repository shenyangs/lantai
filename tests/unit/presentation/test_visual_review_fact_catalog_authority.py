"""Candidate fact catalogues must not masquerade as independent sources."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from PIL import Image

from lantai.presentation.intelligence import UtilityPresentationVisualCritic
from lantai.presentation.models import (
    DeckIR,
    DeckSlide,
    FactRecord,
    PresentationBrief,
    PresentationSourceSection,
    TextObject,
    VisualIssue,
)
from lantai.presentation.visual_qa import build_visual_critic_inputs
from tests.unit.presentation.visual_review_fixtures import synthetic_review_response


@pytest.mark.parametrize("scope", ["slide", "deck"])
@pytest.mark.parametrize("with_original_source", [True, False])
@pytest.mark.parametrize("issue_type", ["content_accuracy", "source_coverage"])
def test_real_review_boundary_labels_catalog_without_rewriting_or_suppressing(
    tmp_path, monkeypatch, scope, with_original_source, issue_type,
):
    rendered = tmp_path / "candidate.png"
    Image.new("RGB", (320, 180), "white").save(rendered)
    original = "The owner leans toward B. The selection remains undecided."
    catalog_claim = "The owner approved B."
    brief = PresentationBrief(
        objective="Review a synthetic proposal", audience="Reviewers",
        narrative="Separate observations from decisions",
        required_facts=["The selection remains undecided."],
        immutable_data=["B"],
        source_sections=[PresentationSourceSection(
            title="Original observation", source_ref="synthetic-source", body=original,
        )] if with_original_source else [],
    )
    deck = DeckIR(
        id="candidate", title="Synthetic proposal",
        required_facts=[catalog_claim], immutable_data=["B"],
        facts=[FactRecord(
            fact_id="candidate-claim", text=catalog_claim,
            source_refs=["synthetic-source"], immutable=True,
        )],
        slides=[DeckSlide(
            id="page", layout_id="key_judgment", speaker_notes="Decision pending",
            objects=[TextObject(id="visible-claim", role="body", text=original)],
        )],
    )
    before = [value.model_dump(mode="json") for value in (deck, brief)]
    slides = build_visual_critic_inputs(
        deck, [rendered], brief=brief,
        original_user_request="Report the proposal without inventing an approval.",
    )
    finding = VisualIssue(
        slide_id="page", severity="error", issue_type=issue_type,
        object_ids=["visible-claim"], description="A synthetic source finding",
        proposed_action="Resolve the source conflict without removing protected evidence",
    )
    captured = {}

    async def capture_review(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            value=kwargs["result_model"].model_validate(synthetic_review_response(
                kwargs, [finding.model_dump(mode="json")],
            )), model_label="test-vision",
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_content_task", capture_review,
    )
    monkeypatch.setattr(
        "lantai.presentation.intelligence._utility_route_cache_identity", lambda _: "test-route",
    )
    critic = UtilityPresentationVisualCritic("test-agent")
    result = critic.review_slide(slides[0]) if scope == "slide" else critic.review_deck(slides)
    context = json.loads(next(
        block["text"] for block in reversed(captured["user_content"]) if block["type"] == "text"
    ))
    grounding = context if scope == "slide" else context["grounding"][0]
    page = context if scope == "slide" else context["ordered_slides"][0]

    assert grounding["source_fact_catalog_policy"] == {
        "role": "claims_requiring_source_verification",
        "source_refs_role": "source_locators_not_independent_proof",
        "immutable_role": "edit_protection_not_truth_verification",
    }
    assert grounding["source_brief"] == brief.model_dump(mode="json")
    assert grounding["source_facts"] == [fact.model_dump(mode="json") for fact in deck.facts]
    assert page["factual_constraints"] == slides[0].factual_constraints
    assert catalog_claim in page["deck_factual_constraints"]
    assert "The selection remains undecided." in page["deck_factual_constraints"]
    assert [value.model_dump(mode="json") for value in (deck, brief)] == before
    assert result.issues == [finding], "source findings must not be filtered or downgraded"
    assert "source_facts 是待核验的事实目录" in captured["system_prompt"]
    assert "source_refs" in captured["system_prompt"]
    assert "immutable" in captured["system_prompt"]
    assert "HARD/STRICT" in captured["system_prompt"]


def test_catalog_authority_rules_invalidate_both_review_cache_scopes(monkeypatch):
    monkeypatch.setattr(
        "lantai.presentation.intelligence._utility_route_cache_identity", lambda _: "test-route",
    )
    critic = UtilityPresentationVisualCritic("test-agent")
    old_slide = "utility-vision/test-agent/page-and-deck-v6/image-capable-foreground-v1/test-route"
    old_deck = "utility-vision/test-agent/page-and-deck-v7/image-capable-foreground-v1/test-route"
    assert critic.slide_cache_identity() != old_slide
    assert critic.deck_cache_identity() != old_deck
    assert "source-fact-catalog-v1" in critic.slide_cache_identity()
    assert "source-fact-catalog-v1" in critic.deck_cache_identity()
