"""Twenty-page caller authority is bounded independently of action/intent limits."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from lantai.app.routers import presentations as api
from lantai.presentation.models import (
    DeckIR, DeckSlide, PresentationObjectEditPlan, ReplaceTextAction,
    SlideIntentPatch, TextObject,
)
from lantai.presentation.natural_edit import (
    apply_object_edit_plan, validate_requested_object_edit_scope,
    validate_required_object_edit_coverage,
)


def _deck() -> DeckIR:
    return DeckIR(
        id="bounded-edit", title="Explicit page authority",
        slides=[DeckSlide(
            id=f"page-{index}", layout_id="freeform",
            objects=[TextObject(id=f"title-{index}", role="title", text=f"Original {index}")],
        ) for index in range(21)],
    )


@pytest.mark.parametrize("count", [19, 20])
def test_plan_accepts_explicit_pages_up_to_twenty(count):
    pages = [f"page-{index}" for index in range(count)]
    plan = PresentationObjectEditPlan(
        slide_id=pages[0], additional_slide_ids=pages[1:],
        actions=[ReplaceTextAction(object_id=f"title-{index}", text=f"Revised {index}")
                 for index in range(count)],
    )
    assert plan.additional_slide_ids == pages[1:]
    assert len(plan.actions) == count


@pytest.mark.parametrize("count", [19, 20])
def test_scope_and_net_coverage_accept_explicit_pages_up_to_twenty(count):
    deck = _deck()
    pages = [slide.id for slide in deck.slides[:count]]
    assert validate_requested_object_edit_scope(
        deck, pages[0], pages[1:], required_slide_ids=pages,
    ) == pages
    plan = PresentationObjectEditPlan(
        slide_id=pages[0], additional_slide_ids=pages[1:],
        actions=[ReplaceTextAction(object_id=f"title-{index}", text=f"Revised {index}")
                 for index in range(count)],
    )
    changed, changed_ids = apply_object_edit_plan(deck, plan)
    assert len(changed_ids) == count
    validate_required_object_edit_coverage(deck, changed, pages)
    assert changed.slides[count:] == deck.slides[count:]
    with pytest.raises(ValueError, match="edit.required_slide_unchanged"):
        validate_required_object_edit_coverage(deck, deck, pages)


@pytest.mark.parametrize("alias", [False, True])
@pytest.mark.parametrize("count", [19, 20])
def test_api_preserves_twenty_page_explicit_scope(monkeypatch, alias, count):
    calls = []

    async def runtime(_request):
        return SimpleNamespace(redo_slide=lambda *a, **k: calls.append((a, k)) or {"status": "completed"})

    monkeypatch.setattr(api, "_runtime", runtime)
    pages = [f"page-{index}" for index in range(count)]
    payload = api.SlideRedo(
        conversation_id="owner", slide_id=pages[0], instruction="Edit only these pages",
        base_output_sha256="a" * 64, additional_slide_ids=pages[1:], required_slide_ids=pages,
    )
    route = api.edit_slide if alias else api.redo_slide
    assert asyncio.run(route("job", payload, SimpleNamespace())) == {"status": "completed"}
    assert calls[0][1] == {"additional_slide_ids": pages[1:], "required_slide_ids": pages}


def test_twenty_one_pages_rejected_at_each_boundary():
    pages = [slide.id for slide in _deck().slides]
    with pytest.raises(ValidationError):
        PresentationObjectEditPlan(
            slide_id=pages[0], additional_slide_ids=pages[1:],
            actions=[ReplaceTextAction(object_id="title-0", text="Revised")],
        )
    with pytest.raises(ValidationError):
        api.SlideRedo(
            conversation_id="owner", slide_id=pages[0], instruction="Too wide",
            base_output_sha256="a" * 64, additional_slide_ids=pages[1:], required_slide_ids=pages,
        )
    with pytest.raises(ValueError, match="scope"):
        validate_requested_object_edit_scope(_deck(), pages[0], pages[1:], required_slide_ids=pages)


def test_default_scope_is_still_one_page_and_model_cannot_expand_it():
    deck = _deck()
    assert validate_requested_object_edit_scope(deck, "page-0") == ["page-0"]
    plan = PresentationObjectEditPlan(
        slide_id="page-0", additional_slide_ids=["page-1"],
        actions=[ReplaceTextAction(object_id="title-1", text="Unauthorized")],
    )
    with pytest.raises(PermissionError, match="scope"):
        validate_requested_object_edit_scope(deck, "page-0", plan=plan)
    payload = api.SlideRedo(
        conversation_id="owner", slide_id="page-0", instruction="One page",
        base_output_sha256="a" * 64,
    )
    assert payload.additional_slide_ids == payload.required_slide_ids == []


@pytest.mark.parametrize("additional,required", [
    (["page-1", "page-1"], []), (["page-0"], []), (["missing"], []),
    (["page-1"], ["page-2"]), (["page-1"], ["page-0", "page-0"]),
])
def test_page_authority_still_fails_closed(additional, required):
    with pytest.raises(ValueError, match="scope"):
        validate_requested_object_edit_scope(
            _deck(), "page-0", additional, required_slide_ids=required,
        )


def test_action_and_intent_caps_are_not_widened_with_page_cap():
    pages = [f"page-{index}" for index in range(20)]
    actions = [ReplaceTextAction(object_id=f"object-{index}", text="Revised") for index in range(40)]
    intents = [SlideIntentPatch(slide_id=pages[index], key_message="Source-bound message") for index in range(10)]
    plan = PresentationObjectEditPlan(
        slide_id=pages[0], additional_slide_ids=pages[1:], actions=actions, intent_patches=intents,
    )
    assert len(plan.actions) == 40 and len(plan.intent_patches) == 10
    with pytest.raises(ValidationError, match="at most 40"):
        PresentationObjectEditPlan(slide_id="page-0", actions=[*actions, actions[0]])
    with pytest.raises(ValidationError, match="at most 10"):
        PresentationObjectEditPlan(slide_id="page-0", intent_patches=[
            *intents, SlideIntentPatch(slide_id="page-10", key_message="Eleventh intent"),
        ])
