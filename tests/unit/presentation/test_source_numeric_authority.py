"""Raw document notation cannot abort planning or authorize invented values."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from lantai.app import utility_model
from lantai.app.utility_model import ResolvedUtilityModel
from lantai.config.config import UtilityIntelligenceConfig
from lantai.presentation.intelligence import UtilityPresentationSemanticPlanner
from lantai.presentation.models import (
    CompositionAuthority, DeckPlanSlide, DeckSlide, InputMaterial, ObjectFrame,
    PresentationJobRequest, SlideIntent, TextObject,
)
from lantai.presentation.page_authoring import (
    PresentationPageAuthorContentError, numeric_tokens, validate_authored_page,
    validate_grounded_numeric_text,
)
from lantai.presentation.planner import build_brief


INCIDENTAL_NOTATION = [
    pytest.param("NASA SP-2016-6105 Rev2", {"2016", "6105", "-2016", "-6105"}, "-2016万元", id="document-id"),
    pytest.param("Figure 2.1-1 The Systems Engineering Engine", {"2.1", "1", "-1"}, "-1万元", id="figure-id"),
    pytest.param("202-358-0775", {"202", "358", "0775", "-358", "-0775"}, "-358万元", id="telephone"),
    pytest.param("scale (e.g., 1-3-9)", {"1", "3", "9", "-3", "-9"}, "-3万元", id="multi-part-scale"),
    pytest.param("94-168) amended by the Omnibus Trade and", {"94", "168", "-168"}, "-168万元", id="extracted-law-id"),
    pytest.param("120kg-95kg=25kg", {"120", "95", "-95"}, "-95万元", id="unproven-unit-equation"),
    pytest.param("2026-02-29", {"2026", "02", "29", "-02", "-29"}, "-29万元", id="invalid-date"),
]
VALID_FACTS = "预算42万元，调整-17%，质量9.375%，数量5公斤-17公斤。"


class BoundaryCaptured(BaseException):
    pass


@pytest.mark.parametrize("notation, forbidden, invented", INCIDENTAL_NOTATION)
def test_incidental_source_notation_keeps_complete_material_at_model_boundary(
    notation, forbidden, invented, monkeypatch,
):
    calls = []

    async def capture(messages, **kwargs):
        calls.append((messages, kwargs))
        raise BoundaryCaptured()

    monkeypatch.setattr(utility_model, "load_config", lambda: SimpleNamespace(
        utility_intelligence=UtilityIntelligenceConfig(mode="dedicated"),
    ))
    monkeypatch.setattr(utility_model, "resolve_utility_model", lambda config, agent_id:
        ResolvedUtilityModel(model=capture, provider_id="private-capture", model_id="capture"),
    )
    text = notation + "\n" + VALID_FACTS
    request = PresentationJobRequest(
        conversation_id="private-source-notation-lock",
        user_request="依据完整材料说明预算与约束。",
        slide_count=1,
        materials=[InputMaterial(id="source", name="source.txt", content=text)],
    )
    brief = build_brief(request)
    with pytest.raises(BoundaryCaptured):
        UtilityPresentationSemanticPlanner("private-agent").plan(request, brief)
    assert len(calls) == 1
    messages, kwargs = calls[0]
    payload = json.loads(messages[1]["content"])
    assert payload["brief"] == brief.model_dump(mode="json")
    assert payload["brief"]["source_sections"][0]["body"] == text
    tokens = set(payload["allowed_numeric_tokens_by_source"]["source"])
    assert {"42", "-17", "9.375", "5", "17"} <= tokens
    assert not tokens & forbidden
    assert kwargs["max_tokens"] > 0


@pytest.mark.parametrize("notation, forbidden, invented", INCIDENTAL_NOTATION)
def test_incidental_source_notation_does_not_block_grounded_claims(notation, forbidden, invented):
    validate_grounded_numeric_text(
        "预算42万元，调整-17%，质量9.4%。", notation + "\n" + VALID_FACTS,
        location="grounded-claim",
    )


@pytest.mark.parametrize("notation, forbidden, invented", INCIDENTAL_NOTATION)
def test_incidental_source_notation_cannot_authorize_invented_claims(notation, forbidden, invented):
    with pytest.raises(PresentationPageAuthorContentError, match="uncited storyboard number"):
        validate_grounded_numeric_text(
            invented, notation + "\n" + VALID_FACTS, location="invented-claim",
        )


def _page(fact):
    planned = DeckPlanSlide(
        id="plan", order=1, title="预算判断",
        intent=SlideIntent(
            id="intent", purpose="解释预算", key_message=fact,
            required_facts=[fact], source_refs=["source"],
        ),
    )
    scene = DeckSlide(
        id="scene", intent_id="intent", layout_id="freeform",
        composition_authority=CompositionAuthority.AUTHOR, source_fact_ids=["fact"],
        objects=[
            TextObject(id="title", role="title", text=planned.title, frame=ObjectFrame(x=1, y=0.5, w=9, h=1)),
            TextObject(id="fact", role="body", text=fact, frame=ObjectFrame(x=1, y=2, w=9, h=2)),
        ],
    )
    return planned, scene


@pytest.mark.parametrize("notation, forbidden, invented", INCIDENTAL_NOTATION)
def test_incidental_source_notation_does_not_block_grounded_native_page(notation, forbidden, invented):
    planned, scene = _page("预算42万元")
    assert validate_authored_page(
        scene, planned, scene, source_text=notation + "\n" + VALID_FACTS,
    ).scene_hash


@pytest.mark.parametrize("notation, forbidden, invented", INCIDENTAL_NOTATION)
def test_incidental_source_notation_cannot_authorize_invented_native_page(notation, forbidden, invented):
    planned, scene = _page(invented)
    with pytest.raises(ValueError, match=r"unsupported number\(s\)"):
        validate_authored_page(
            scene, planned, scene, source_text=notation + "\n" + VALID_FACTS,
        )


@pytest.mark.parametrize("notation", [
    "120-95-17", "120-95)", "2026-02-29", "120kg-95kg=25kg",
])
def test_authored_numeric_notation_remains_strict(notation):
    with pytest.raises(ValueError, match="unsupported numeric notation"):
        numeric_tokens(notation, preserve_percent=True)


def test_grounded_claim_cannot_add_a_new_positive_measurement():
    with pytest.raises(PresentationPageAuthorContentError, match="uncited storyboard number"):
        validate_grounded_numeric_text("预算43万元", VALID_FACTS, location="invented-budget")


def test_native_page_control_has_a_valid_scene_and_grounded_fact():
    planned, scene = _page("预算42万元")
    assert validate_authored_page(scene, planned, scene, source_text=VALID_FACTS).scene_hash
