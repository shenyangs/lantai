"""Keep deck-wide grounding distinct from page-local coverage obligations."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from PIL import Image

from lantai.presentation.intelligence import UtilityPresentationVisualCritic
from lantai.presentation.models import (
    DeckIR,
    DeckPlan,
    DeckPlanSlide,
    DeckSlide,
    FactRecord,
    PresentationBrief,
    PresentationSourceSection,
    SlideIntent,
    TextObject,
    VisualDirection,
    VisualIssue,
)
from lantai.presentation.visual_qa import build_visual_critic_inputs
from tests.unit.presentation.visual_review_fixtures import synthetic_review_response


def _inputs(tmp_path, *, with_plan=True):
    rendered = tmp_path / "page.png"
    Image.new("RGB", (320, 180), "white").save(rendered)
    source = PresentationBrief(
        objective="Review a synthetic trial and proposed next steps",
        audience="Reviewers",
        narrative="Observations before decisions",
        required_facts=["Trial sample: 48 tasks", "No ROI was measured"],
        immutable_data=["48"],
        prohibited_changes=["Each page must identify this as a synthetic trial"],
        source_sections=[PresentationSourceSection(
            title="Trial notes", source_ref="trial-source",
            body="Synthetic trial: 48 tasks. Manual sample: 8 tasks. "
                 "Six decisions remain open. No ROI was measured.",
        )],
    )
    intents = [
        SlideIntent(
            id="intent-1", purpose="Review the manual sample",
            key_message="Manual sample: 8 tasks",
            required_facts=["Manual sample: 8 tasks"],
            immutable_facts=["8"],
        ),
        SlideIntent(
            id="intent-2", purpose="Review open decisions",
            key_message="Six decisions remain open",
            required_facts=["Six decisions remain open"],
            immutable_facts=["6"],
        ),
    ]
    plan = DeckPlan(
        title="Synthetic review", narrative_arc="Observations then decisions",
        visual_direction=VisualDirection(
            visual_concept="editorial", palette=["#FFFFFF", "#111111"],
            typography="sans", image_language="documentary",
            chart_language="direct labels", shape_language="rules",
            grid="twelve columns", recurring_motif="folio",
        ),
        slides=[DeckPlanSlide(
            id=f"plan-{index}", order=index, title=intent.key_message,
            intent=intent, content_blocks=[intent.key_message],
        ) for index, intent in enumerate(intents, 1)],
    )
    deck = DeckIR(
        id="deck", title="Synthetic review",
        required_facts=["Trial sample: 48 tasks"], immutable_data=["48"],
        facts=[FactRecord(
            fact_id="claim-1", text="A generated claim is not source authority",
            source_refs=["trial-source"],
        )],
        slides=[DeckSlide(
            id=f"slide-{index}", layout_id="key_judgment", intent_id=intent.id,
            speaker_notes=intent.key_message,
            objects=[TextObject(id=f"claim-{index}", role="body", text=intent.key_message)],
        ) for index, intent in enumerate(intents, 1)],
    )
    before = [value.model_dump(mode="json") for value in (deck, plan, source)]
    inputs = build_visual_critic_inputs(
        deck, [rendered, rendered], plan=plan if with_plan else None,
        brief=source, original_user_request="Cover every observation across the deck",
    )
    assert [value.model_dump(mode="json") for value in (deck, plan, source)] == before
    return inputs, source


def test_builder_preserves_all_grounding_but_separates_coverage_scopes(tmp_path):
    slides, source = _inputs(tmp_path)
    for slide in slides:
        assert slide.deck_factual_constraints == [
            "Trial sample: 48 tasks", "48", "No ROI was measured",
        ]
        assert slide.source_brief == source
        assert slide.source_facts[0].text == "A generated claim is not source authority"
    assert slides[0].page_factual_constraints == ["Manual sample: 8 tasks", "8"]
    assert slides[1].page_factual_constraints == ["Six decisions remain open", "6"]
    assert slides[0].factual_constraints == [
        *slides[0].deck_factual_constraints, *slides[0].page_factual_constraints,
    ], "legacy validation context must not lose any factual constraint"
    assert "Six decisions remain open" not in slides[0].page_factual_constraints
    assert "Manual sample: 8 tasks" not in slides[1].page_factual_constraints


def test_missing_page_plan_does_not_promote_all_deck_facts_to_page_requirements(tmp_path):
    slides, source = _inputs(tmp_path, with_plan=False)
    assert all(slide.intent is None for slide in slides)
    assert all(slide.page_factual_constraints == [] for slide in slides)
    assert all(slide.factual_constraints == slide.deck_factual_constraints for slide in slides)
    assert all(slide.source_brief == source for slide in slides)


@pytest.mark.parametrize("scope", ["slide", "deck"])
@pytest.mark.parametrize("issue_type", ["content_accuracy", "source_coverage"])
def test_real_review_call_scopes_coverage_without_suppressing_source_findings(
    tmp_path, monkeypatch, scope, issue_type,
):
    slides, source = _inputs(tmp_path)
    captured = {}
    finding = VisualIssue(
        slide_id=slides[0].slide_id, severity="error", issue_type=issue_type,
        object_ids=["claim-1"], description="A real unsupported claim or required omission",
        proposed_action="Correct the visible claim while retaining protected evidence",
    )

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
    assert result.issues == [finding]
    context = json.loads(next(
        block["text"] for block in reversed(captured["user_content"]) if block["type"] == "text"
    ))
    if scope == "slide":
        assert context["review_scope"] == "single_slide"
        page = context
        assert context["source_brief"] == source.model_dump(mode="json")
        assert "不要求每页重复所有整套事实" in captured["system_prompt"]
        assert "逐页标识" in captured["system_prompt"]
        assert "page-and-deck-v6" in critic.slide_cache_identity()
    else:
        assert context["review_scope"] == "whole_deck"
        page = context["ordered_slides"][0]
        assert context["grounding"][0]["source_brief"] == source.model_dump(mode="json")
        assert [p["slide_id"] for p in context["ordered_slides"]] == [s.slide_id for s in slides]
        assert "整套事实完整性" in captured["system_prompt"]
        assert "分布在不同页" in captured["system_prompt"]
        assert "page-and-deck-v7" in critic.deck_cache_identity()
    assert page["factual_constraints_scope"] == "mixed_validation_context"
    assert page["deck_factual_constraints"] == slides[0].deck_factual_constraints
    assert page["page_factual_constraints"] == slides[0].page_factual_constraints
    assert page["factual_constraints"] == slides[0].factual_constraints
    prompt = captured["system_prompt"]
    assert "不是事实权威" in prompt or "只是待审结果" in prompt
    assert "source_coverage" in prompt and "content_accuracy" in prompt
    assert "HARD/STRICT" in prompt
