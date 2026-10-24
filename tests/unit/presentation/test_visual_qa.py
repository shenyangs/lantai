from pathlib import Path

from PIL import Image

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
)
from lantai.presentation.intelligence import UtilityPresentationVisualCritic
from lantai.presentation.visual_qa import build_visual_critic_inputs


def test_visual_critic_input_binds_intent_objects_and_render(tmp_path: Path):
    image_path = tmp_path / "slide-1.png"
    Image.new("RGB", (1280, 720), "white").save(image_path)
    intent = SlideIntent(
        id="intent-1",
        purpose="explain",
        key_message="Revenue reached 42%",
        required_facts=["42%"],
        immutable_facts=["42%"],
    )
    plan = DeckPlan(
        title="Evidence",
        narrative_arc="First establish the result, then qualify it.",
        visual_direction=VisualDirection(
            visual_concept="editorial",
            palette=["#FFFFFF", "#111111"],
            typography="sans",
            image_language="documentary",
            chart_language="direct labels",
            shape_language="rules",
            grid="twelve columns",
            recurring_motif="folio",
        ),
        slides=[
            DeckPlanSlide(
                id="plan-1",
                order=1,
                title="Evidence",
                intent=intent,
                content_blocks=["42%"],
            )
        ],
    )
    deck = DeckIR(
        id="deck-1",
        title="Evidence",
        slides=[
            DeckSlide(
                id="slide-1",
                layout_id="key_judgment",
                intent_id="intent-1",
                speaker_notes="Revenue reached 42%",
                objects=[
                    TextObject(
                        id="object-1",
                        role="body",
                        text="42%",
                    )
                ],
            )
        ],
        required_facts=["Source: audited report"],
        immutable_data=["42%"],
    )

    inputs = build_visual_critic_inputs(
        deck, [image_path], plan=plan, usage_session_id="original-task",
    )

    assert len(inputs) == 1
    assert inputs[0].intent == intent
    assert inputs[0].object_ids == ["object-1"]
    assert inputs[0].factual_constraints == ["Source: audited report", "42%"]
    assert inputs[0].full_deck_context == [
        "Narrative arc: First establish the result, then qualify it.",
        "1. Revenue reached 42%",
    ]
    assert inputs[0].rendered_sha256
    assert inputs[0].usage_session_id == "original-task"


def test_critic_receives_original_sources_not_only_generated_claims(tmp_path: Path):
    image = tmp_path / "slide.png"
    Image.new("RGB", (640, 360), "white").save(image)
    source = PresentationBrief(
        objective="介绍三个场景",
        audience="试用者",
        narrative="复盘、客户方案、培训",
        source_sections=[PresentationSourceSection(
            title="观察边界", source_ref="source-1",
            body="有时会停止；支持来源关联，不能承诺所有事实可追溯。",
        )],
        prohibited_changes=["不得把有时改为多数"],
        style_constraints=["暖白与紫色"],
    )
    fact = FactRecord(fact_id="fact-1", text="10/14是比例，不是10/14人", source_refs=["source-1"])
    deck = DeckIR(id="deck", title="质量复核", facts=[fact], slides=[DeckSlide(
        id="slide-1", layout_id="key_judgment", speaker_notes="多数会停止",
        objects=[TextObject(id="claim", role="body", text="多数会停止")],
        source_fact_ids=["fact-1"],
    )])
    before = (deck.model_dump(mode="json"), source.model_dump(mode="json"))

    inputs = build_visual_critic_inputs(
        deck, [image], brief=source, original_user_request="三个场景都必须给出行动示例",
    )
    slide = inputs[0]
    assert slide.source_brief == source
    assert slide.source_facts == [fact]
    assert slide.original_user_request == "三个场景都必须给出行动示例"
    assert slide.slide_objects == deck.slides[0].objects
    context = UtilityPresentationVisualCritic._slide_context(slide)
    assert context["source_brief"] == source.model_dump(mode="json")
    assert context["source_facts"] == [fact.model_dump(mode="json")]
    assert context["slide_objects"][0]["text"] == "多数会停止"
    assert context["original_user_request"] == slide.original_user_request
    assert (deck.model_dump(mode="json"), source.model_dump(mode="json")) == before


def test_deck_critic_deduplicates_sources_without_losing_distinct_contexts(tmp_path: Path):
    image = tmp_path / "slide.png"
    Image.new("RGB", (640, 360), "white").save(image)
    source = PresentationBrief(objective="来源一", audience="内部", narrative="观察")
    deck = DeckIR(id="deck", title="来源", slides=[
        DeckSlide(id="slide-1", layout_id="key_judgment", speaker_notes="观察"),
        DeckSlide(id="slide-2", layout_id="key_judgment", speaker_notes="局限"),
    ])
    slides = build_visual_critic_inputs(deck, [image, image], brief=source)
    contexts = UtilityPresentationVisualCritic._deck_source_contexts(slides)
    assert len(contexts) == 1
    assert contexts[0]["slide_ids"] == ["slide-1", "slide-2"]
    assert contexts[0]["source_brief"] == source.model_dump(mode="json")
    other = PresentationBrief(objective="来源二", audience="内部", narrative="对照")
    slides[1].source_brief = other
    contexts = UtilityPresentationVisualCritic._deck_source_contexts(slides)
    assert len(contexts) == 2
    assert contexts[1]["slide_ids"] == ["slide-2"]
    assert contexts[1]["source_brief"] == other.model_dump(mode="json")
