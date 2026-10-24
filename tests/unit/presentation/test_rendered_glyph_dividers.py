"""Rendered glyphs, not empty text-frame area, determine divider collisions."""

from __future__ import annotations

import pytest

from lantai.presentation.models import (
    DeckIR, DeckSlide, EvidenceState, ObjectFrame, PresentationFailureCategory,
    ShapeObject, TextObject,
)
from lantai.presentation.pptx_compiler import NativePptxCompiler
from lantai.presentation.quality import delivery_evidence_issues, inspect_pptx, is_verified_delivery
from lantai.presentation.rendered_geometry import GLYPH_DIVIDER_RULESET
from lantai.presentation.repair import RepairActionType, SlideRedesignProposal, repair_deck_with_designer
from lantai.presentation.service import PresentationService


def _deck(*, axis="vertical", text="MMMMMMMMMMMMMMMMMMMMMMMMMMMM", kind="rect", foreground=True):
    title = TextObject(
        id="title", role="title", slot="title", text="合成字形与分隔线验证",
        font_size=32, frame=ObjectFrame(x=0.6, y=0.4, w=12, h=0.8),
    )
    body = TextObject(
        id="body", role="body", text=text, color="1565C0", font_size=24,
        frame=ObjectFrame(x=0.78, y=1.52, w=4.95, h=1.6),
    )
    divider = ShapeObject(
        id="divider", shape_type=kind, fill_color="D4DDE1", line_color="D4DDE1", line_width=0,
        frame=(ObjectFrame(x=3.3, y=1.55, w=0.02, h=4.95) if axis == "vertical"
               else ObjectFrame(x=0.6, y=1.73, w=11.5, h=0.02)),
    )
    objects = [body, divider] if foreground else [divider, body]
    return DeckIR(
        id="glyph-divider-deck", title=title.text, required_facts=[title.text, text],
        slides=[DeckSlide(id="glyph-slide", layout_id="freeform", objects=[title, *objects])],
    )


def _verify(tmp_path, deck):
    output = tmp_path / "presentation.pptx"
    NativePptxCompiler().compile(deck, output)
    # This is the old false-negative boundary: frame-area overlap alone passes.
    structural = inspect_pptx(deck, output)
    assert structural.passed, [(issue.code, issue.message) for issue in structural.issues]
    report, images = PresentationService(tmp_path / "jobs").verify_artifact(
        deck, output, tmp_path / "rendered", vision_critique_required=False,
    )
    return output, report, images


@pytest.mark.parametrize("axis", ["vertical", "horizontal"])
def test_actual_glyph_crossing_foreground_divider_is_blocking(tmp_path, axis):
    deck = _deck(axis=axis)
    output, report, images = _verify(tmp_path, deck)

    assert not report.passed
    assert report.delivery_status == "failed"
    collision = next(issue for issue in report.issues if issue.code == "layout.glyph_divider_collision")
    assert collision.severity == "error" and collision.repairable
    assert collision.slide_id == "glyph-slide" and collision.object_id == "body"
    assert "divider" in collision.message
    assert report.rendered_glyph_check_version == GLYPH_DIVIDER_RULESET
    assert report.render_evidence == EvidenceState.FAILED
    assert "render evidence" in report.blocking_gaps
    assert not is_verified_delivery(deck, report, output, images)

    # Feed the real issue into the existing designer path; no local content
    # rewrite or stock-layout fallback is introduced by the new gate.
    replacement = deck.slides[0].model_copy(deep=True)
    divider = next(obj for obj in replacement.objects if obj.id == "divider")
    divider.frame = divider.frame.model_copy(update={"x": 8.0} if axis == "vertical" else {"y": 4.0})

    class Designer:
        requests = []

        def redesign(self, request):
            self.requests.append(request)
            return SlideRedesignProposal(
                slide=replacement, action=RepairActionType.MOVE_ELEMENT,
                rationale="move the divider out of the rendered glyphs",
            )

    designer = Designer()
    repaired = repair_deck_with_designer(deck, None, report.issues, designer)
    assert repaired.changed and len(designer.requests) == 1
    request = designer.requests[0]
    assert collision in request.issues
    assert request.failure_routes[0].category == PresentationFailureCategory.GEOMETRY
    assert RepairActionType.MOVE_ELEMENT in request.allowed_actions
    assert [obj.text for obj in repaired.deck.slides[0].objects] == [obj.text for obj in deck.slides[0].objects]
    assert repaired.deck.required_facts == deck.required_facts


def test_empty_text_frame_area_is_not_a_glyph_collision(tmp_path):
    deck = _deck(text="短文本")
    output, report, images = _verify(tmp_path, deck)

    assert report.passed, [(issue.code, issue.message) for issue in report.issues]
    assert not any(issue.code == "layout.glyph_divider_collision" for issue in report.issues)
    assert is_verified_delivery(deck, report, output, images)
    for old_version in (None, "native-foreground-glyph-dividers/v0", "unsupported"):
        stale = report.model_copy(update={"rendered_glyph_check_version": old_version})
        assert any(issue.code == "evidence.glyph_geometry_missing"
                   for issue in delivery_evidence_issues(deck, stale, output, images))
        assert not is_verified_delivery(deck, stale, output, images)


def test_relationship_connector_keeps_its_existing_exemption(tmp_path):
    deck = _deck(kind="line")
    output, report, images = _verify(tmp_path, deck)

    assert report.passed, [(issue.code, issue.message) for issue in report.issues]
    assert is_verified_delivery(deck, report, output, images)
    # Old evidence for an exempt connector does not suddenly require a PDF
    # divider check. Existing safe delivery checkpoints remain compatible.
    old = report.model_copy(update={"rendered_glyph_check_version": None})
    assert is_verified_delivery(deck, old, output, images)


def test_background_divider_is_not_proof_of_foreground_occlusion(tmp_path):
    deck = _deck(foreground=False)
    _output, report, _images = _verify(tmp_path, deck)

    assert not any(issue.code == "layout.glyph_divider_collision" for issue in report.issues)
