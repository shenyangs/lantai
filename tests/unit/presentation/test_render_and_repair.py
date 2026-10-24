from __future__ import annotations

from pathlib import Path

import pytest

from lantai.app.structured_utility import StructuredTaskValidationError
from lantai.presentation.compiler import PresentationCompiler
from lantai.presentation.models import (
    ChartObject,
    DeckIR,
    DeckPlan,
    DeckPlanSlide,
    DeckSlide,
    FactRecord,
    ObjectFrame,
    PresentationFailureCategory,
    PresentationRecoveryAction,
    QualityIssue,
    SlideIntent,
    TextObject,
    VisualDirection,
)
from lantai.presentation.quality import inspect_rendered_slides
from lantai.presentation.render import (
    PresentationRenderUnavailable,
    render_pptx,
    slide_image_paths,
)
from lantai.presentation.repair import (
    MAX_REPAIR_ROUNDS,
    RepairActionType,
    SlideRedesignProposal,
    repair_deck_once,
    repair_deck_with_designer,
)


class _Designer:
    def __init__(self, replacement: DeckSlide) -> None:
        self.replacement = replacement
        self.requests = []

    def redesign(self, request):
        self.requests.append(request)
        return SlideRedesignProposal(
            slide=self.replacement,
            action=RepairActionType.REDESIGN_SLIDE,
            rationale="restore hierarchy",
        )


class _SplitDesigner:
    def __init__(
        self,
        first_slide: DeckSlide,
        second_slide: DeckSlide,
        plan_slides: tuple[DeckPlanSlide, DeckPlanSlide],
    ) -> None:
        self.first_slide = first_slide
        self.second_slide = second_slide
        self.plan_slides = plan_slides
        self.requests = []

    def redesign(self, request):
        self.requests.append(request)
        return SlideRedesignProposal(
            slide=self.first_slide,
            additional_slides=(self.second_slide,),
            plan_slides=self.plan_slides,
            action=RepairActionType.SPLIT_SLIDE,
            rationale="把判断和证据拆成两个可读页面",
        )


class _ActionDesigner:
    def __init__(
        self,
        replacement: DeckSlide,
        action: RepairActionType,
    ) -> None:
        self.replacement = replacement
        self.action = action
        self.requests = []

    def redesign(self, request):
        self.requests.append(request)
        return SlideRedesignProposal(
            slide=self.replacement,
            action=self.action,
            rationale="requested action",
        )


class _RetryingGeometryDesigner:
    def __init__(
        self,
        invalid: DeckSlide,
        valid: DeckSlide,
    ) -> None:
        self.invalid = invalid
        self.valid = valid
        self.requests = []

    def redesign(self, request):
        self.requests.append(request)
        return SlideRedesignProposal(
            slide=self.invalid if len(self.requests) == 1 else self.valid,
            action=RepairActionType.MOVE_ELEMENT,
            rationale="retry after deterministic scope validation",
        )


class _RetryingSpeakerNotesDesigner:
    def __init__(self, invalid: DeckSlide, valid: DeckSlide) -> None:
        self.invalid = invalid
        self.valid = valid
        self.requests = []

    def redesign(self, request):
        self.requests.append(request)
        return SlideRedesignProposal(
            slide=self.invalid if len(self.requests) == 1 else self.valid,
            action=RepairActionType.REDESIGN_SLIDE,
            rationale="retry after deterministic speaker-notes validation",
        )


class _RetryingImmutableFactDesigner:
    def __init__(self, invalid: DeckSlide, valid: DeckSlide) -> None:
        self.invalid = invalid
        self.valid = valid
        self.requests = []

    def redesign(self, request):
        self.requests.append(request)
        return SlideRedesignProposal(
            slide=self.invalid if len(self.requests) == 1 else self.valid,
            action=RepairActionType.REDESIGN_SLIDE,
            rationale="retry after deterministic immutable-fact validation",
        )


class _RetryingChartDesigner:
    def __init__(self, invalid: DeckSlide, valid: DeckSlide) -> None:
        self.invalid = invalid
        self.valid = valid
        self.requests = []

    def redesign(self, request):
        self.requests.append(request)
        return SlideRedesignProposal(
            slide=self.invalid if len(self.requests) == 1 else self.valid,
            action=RepairActionType.CHANGE_CHART,
            rationale="restore the audited native chart contract",
        )


def _direction() -> VisualDirection:
    return VisualDirection(
        visual_concept="editorial evidence",
        palette=["#F7F4EE", "#171717"],
        typography="clear editorial hierarchy",
        image_language="documentary",
        chart_language="direct labels",
        shape_language="quiet rules",
        grid="twelve columns",
        recurring_motif="folio",
    )


def test_repair_is_bounded_and_preserves_text() -> None:
    title = "不能被自动改写的数据 95%"
    original = "这是需要保留的长文本。" * 14
    deck = DeckIR(
        title="repair",
        immutable_data=["95%"],
        slides=[
            DeckSlide(
                layout_id="cover",
                objects=[
                    TextObject(role="title", text=title, slot="title"),
                    TextObject(role="body", text=original, slot="subtitle"),
                ],
            )
        ],
    )

    repaired = repair_deck_once(deck)

    assert MAX_REPAIR_ROUNDS == 3
    assert repaired.changed is True
    assert repaired.deck.slides[0].layout_id == "key_judgment"
    assert [obj.text for obj in repaired.deck.slides[0].objects] == [title, original]
    assert repaired.deck.immutable_data == ["95%"]


def test_v3_repair_rebuilds_slide_and_preserves_identity_and_facts() -> None:
    intent = SlideIntent(
        id="intent-a",
        purpose="prove",
        key_message="转化率达到 95%",
        immutable_facts=["95%"],
    )
    plan = DeckPlan(
        title="quality",
        narrative_arc="evidence to decision",
        visual_direction=_direction(),
        slides=[
            DeckPlanSlide(
                id="plan-a",
                order=1,
                title="转化率",
                intent=intent,
                content_blocks=["转化率达到 95%"],
            )
        ],
    )
    original = DeckSlide(
        id="slide-a",
        intent_id="intent-a",
        layout_id="key_judgment",
        objects=[
            TextObject(id="old-title", role="title", text="转化率"),
            TextObject(id="old", role="body", text="转化率达到 95%"),
        ],
    )
    replacement = DeckSlide(
        id="slide-a",
        intent_id="intent-a",
        layout_id="editorial_metric",
        objects=[
            TextObject(id="new-title", role="title", text="转化率"),
            TextObject(id="new-body", role="body", text="转化率已达到目标 95%"),
        ],
    )
    designer = _Designer(replacement)
    deck = DeckIR(
        id="deck-a",
        title="quality",
        immutable_data=["95%"],
        slides=[original],
    )

    repaired = repair_deck_with_designer(
        deck,
        plan,
        [
            QualityIssue(
                code="vision.hierarchy",
                severity="error",
                slide_id="slide-a",
                message="message is visually buried",
                repairable=True,
            )
        ],
        designer,
    )

    assert repaired.changed is True
    assert repaired.deck.slides[0] == replacement
    assert repaired.actions == (
        "slide-a: redesign_slide — restore hierarchy",
    )
    assert designer.requests[0].intent == intent
    assert designer.requests[0].immutable_facts == ("95%",)
    assert designer.requests[0].failure_routes[0].category == (
        PresentationFailureCategory.AESTHETIC
    )
    assert designer.requests[0].failure_routes[0].recovery_action == (
        PresentationRecoveryAction.REDESIGN_SLIDE
    )
    assert RepairActionType.REDESIGN_SLIDE in (
        designer.requests[0].allowed_actions
    )


@pytest.mark.parametrize(
    ("issue_code", "proposed_action", "allowed_action", "category"),
    [
        (
            "content.required_fact_missing",
            RepairActionType.REDESIGN_SLIDE,
            RepairActionType.REWRITE_TEXT,
            PresentationFailureCategory.CONTENT,
        ),
        (
            "vision.alignment",
            RepairActionType.REWRITE_TEXT,
            RepairActionType.MOVE_ELEMENT,
            PresentationFailureCategory.GEOMETRY,
        ),
    ],
)
def test_v3_repair_rejects_actions_outside_the_classified_route(
    issue_code: str,
    proposed_action: RepairActionType,
    allowed_action: RepairActionType,
    category: PresentationFailureCategory,
) -> None:
    original = DeckSlide(
        id="slide-a",
        intent_id="intent-a",
        layout_id="freeform",
        objects=[TextObject(id="old", role="body", text="必须保留的事实")],
    )
    replacement = original.model_copy(
        update={
            "objects": [
                TextObject(id="new", role="body", text="必须保留的事实")
            ],
        },
    )
    designer = _ActionDesigner(replacement, proposed_action)

    with pytest.raises(ValueError, match="not allowed for classified routes"):
        repair_deck_with_designer(
            DeckIR(id="deck-a", title="quality", slides=[original]),
            None,
            [
                QualityIssue(
                    code=issue_code,
                    severity="error",
                    slide_id="slide-a",
                    message="quality failure",
                    repairable=True,
                ),
            ],
            designer,
        )

    assert designer.requests[0].failure_routes[0].category == category
    assert allowed_action in designer.requests[0].allowed_actions
    assert proposed_action not in designer.requests[0].allowed_actions


def test_v3_repair_does_not_send_unknown_failures_to_the_designer() -> None:
    original = DeckSlide(
        id="slide-a",
        intent_id="intent-a",
        layout_id="freeform",
        objects=[TextObject(id="old", role="body", text="原始内容")],
    )
    replacement = original.model_copy(
        update={"objects": [TextObject(id="new", role="body", text="改写")]},
    )
    designer = _ActionDesigner(
        replacement,
        RepairActionType.REDESIGN_SLIDE,
    )

    repaired = repair_deck_with_designer(
        DeckIR(id="deck-a", title="quality", slides=[original]),
        None,
        [
            QualityIssue(
                code="custom.unclassified_failure",
                severity="error",
                slide_id="slide-a",
                message="needs inspection",
                repairable=True,
            ),
        ],
        designer,
    )

    assert repaired.changed is False
    assert repaired.actions == ()
    assert designer.requests == []


def test_v3_geometry_repair_cannot_change_semantic_content() -> None:
    original = DeckSlide(
        id="slide-a",
        intent_id="intent-a",
        layout_id="freeform",
        speaker_notes="说明证据边界",
        objects=[
            TextObject(
                id="body",
                role="body",
                text="必须保留的事实",
                frame=ObjectFrame(x=1, y=1, w=4, h=2),
            ),
        ],
    )
    changed_content = original.model_copy(
        update={
            "objects": [
                TextObject(
                    id="body",
                    role="body",
                    text="被几何修复改写的事实",
                    frame=ObjectFrame(x=2, y=1, w=4, h=2),
                ),
            ],
        },
    )

    with pytest.raises(
        ValueError,
        match="geometry repair changed semantic content",
    ):
        repair_deck_with_designer(
            DeckIR(id="deck-a", title="quality", slides=[original]),
            None,
            [
                QualityIssue(
                    code="vision.alignment",
                    severity="error",
                    slide_id="slide-a",
                    message="misaligned object",
                    repairable=True,
                ),
            ],
            _ActionDesigner(changed_content, RepairActionType.MOVE_ELEMENT),
        )

    moved = original.model_copy(
        update={
            "objects": [
                original.objects[0].model_copy(
                    update={"frame": ObjectFrame(x=2, y=1, w=4, h=2)},
                ),
            ],
        },
    )
    repaired = repair_deck_with_designer(
        DeckIR(id="deck-a", title="quality", slides=[original]),
        None,
        [
            QualityIssue(
                code="vision.alignment",
                severity="error",
                slide_id="slide-a",
                message="misaligned object",
                repairable=True,
            ),
        ],
        _ActionDesigner(moved, RepairActionType.MOVE_ELEMENT),
    )
    assert repaired.changed is True
    assert repaired.deck.slides[0] == moved


def test_v3_repair_retries_a_rejected_proposal_with_safe_validation_feedback(
) -> None:
    original = DeckSlide(
        id="slide-a",
        intent_id="intent-a",
        layout_id="freeform",
        speaker_notes="说明证据边界",
        objects=[
            TextObject(
                id="body",
                role="body",
                text="必须保留的事实",
                frame=ObjectFrame(x=1, y=1, w=4, h=2),
            ),
        ],
    )
    invalid = original.model_copy(
        update={
            "objects": [
                original.objects[0].model_copy(
                    update={
                        "text": "被几何动作改写的内容",
                        "frame": ObjectFrame(x=2, y=1, w=4, h=2),
                    },
                ),
            ],
        },
    )
    valid = original.model_copy(
        update={
            "objects": [
                original.objects[0].model_copy(
                    update={"frame": ObjectFrame(x=2, y=1, w=4, h=2)},
                ),
            ],
        },
    )
    designer = _RetryingGeometryDesigner(invalid, valid)

    repaired = repair_deck_with_designer(
        DeckIR(id="deck-a", title="quality", slides=[original]),
        None,
        [
            QualityIssue(
                code="vision.alignment",
                severity="error",
                slide_id="slide-a",
                message="misaligned object",
                repairable=True,
            ),
        ],
        designer,
        proposal_attempt_limit=2,
    )

    assert repaired.changed is True
    assert repaired.deck.slides[0] == valid
    assert len(designer.requests) == 2
    assert designer.requests[0].retry_directive is None
    assert "geometry repair changed semantic content" in (
        designer.requests[1].retry_directive or ""
    )
    assert "MOVE_ELEMENT" not in (designer.requests[1].retry_directive or "")


@pytest.mark.parametrize("invalid_responses", [1, 3])
def test_v3_repair_retries_structured_output_errors_within_existing_limit(
    invalid_responses: int,
) -> None:
    original = DeckSlide(
        id="slide-schema",
        layout_id="freeform",
        objects=[
            TextObject(
                id="body",
                role="body",
                text="Preserve this fact",
                frame=ObjectFrame(x=1, y=1, w=4, h=2),
            ),
        ],
    )
    replacement = original.model_copy(
        update={
            "objects": [
                original.objects[0].model_copy(
                    update={"frame": ObjectFrame(x=2, y=1, w=4, h=2)},
                ),
            ],
        },
    )
    requests = []
    accepted = []

    class SchemaRetryDesigner:
        def redesign(self, request):
            requests.append(request)
            if len(requests) <= invalid_responses:
                raise StructuredTaskValidationError(
                    "structured utility task output failed schema validation",
                    validation_issues=("extra_forbidden@$.additional_slides",),
                )
            return SlideRedesignProposal(
                slide=replacement,
                action=RepairActionType.MOVE_ELEMENT,
            )

    def run_repair():
        return repair_deck_with_designer(
            DeckIR(id="deck-schema", title="Synthetic", slides=[original]),
            None,
            [
                QualityIssue(
                    code="vision.alignment",
                    severity="error",
                    slide_id=original.id,
                    message="misaligned object",
                    repairable=True,
                ),
            ],
            SchemaRetryDesigner(),
            proposal_attempt_limit=3,
            accepted_slide_callback=lambda *args: accepted.append(args),
        )

    if invalid_responses == 3:
        with pytest.raises(StructuredTaskValidationError):
            run_repair()
        assert accepted == []
    else:
        repaired = run_repair()
        assert repaired.changed
        assert repaired.deck.slides == [replacement]
        assert len(accepted) == 1
    assert len(requests) == min(invalid_responses + 1, 3)
    assert requests[0].retry_directive is None
    for request in requests[1:]:
        assert "extra_forbidden@$.additional_slides" in request.retry_directive
        assert request.structural_repair_allowed is False
        assert request.allowed_actions == requests[0].allowed_actions
        assert request.immutable_facts == requests[0].immutable_facts


def test_v3_repair_rejects_numeric_speaker_notes_with_safe_retry_feedback(
) -> None:
    original = DeckSlide(
        id="slide-a",
        intent_id="intent-a",
        layout_id="freeform",
        speaker_notes="原始讲述边界",
        objects=[TextObject(id="body", role="body", text="必须保留的事实")],
    )
    unsafe_note = "请在第3季度解释修复边界"
    invalid = original.model_copy(update={"speaker_notes": unsafe_note})
    valid = original.model_copy(update={"speaker_notes": "解释修复边界"})
    designer = _RetryingSpeakerNotesDesigner(invalid, valid)

    repaired = repair_deck_with_designer(
        DeckIR(id="deck-a", title="quality", slides=[original]),
        None,
        [
            QualityIssue(
                code="vision.hierarchy",
                severity="error",
                slide_id="slide-a",
                message="message is visually buried",
                repairable=True,
            ),
        ],
        designer,
        proposal_attempt_limit=2,
    )

    assert repaired.changed is True
    assert repaired.deck.slides[0] == valid
    assert len(designer.requests) == 2
    retry_directive = designer.requests[1].retry_directive or ""
    assert "speaker_notes must be numeric-free" in retry_directive
    assert unsafe_note not in retry_directive


def test_v3_repair_retries_native_chart_missing_unit_before_composition() -> None:
    original_chart = ChartObject(
        id="customer_trend_chart",
        chart_type="line",
        categories=["Q1", "Q2"],
        series={"客户数": [10, 12]},
        unit="家",
        derivation="直接摘录季度材料",
        source_refs=["source:quarter-data"],
        frame=ObjectFrame(x=1, y=1.5, w=8, h=4),
    )
    original = DeckSlide(
        id="slide-a",
        intent_id="intent-a",
        layout_id="freeform",
        objects=[original_chart],
    )
    invalid = original.model_copy(
        update={
            "objects": [
                original_chart.model_copy(
                    update={"unit": "", "series": {"客户数": [10, 13]}},
                ),
            ],
        },
    )
    valid = invalid.model_copy(
        update={
            "objects": [
                invalid.objects[0].model_copy(update={"unit": "家"}),
            ],
        },
    )
    designer = _RetryingChartDesigner(invalid, valid)
    accepted = []

    repaired = repair_deck_with_designer(
        DeckIR(
            id="deck-a",
            title="quality",
            source_refs=["source:quarter-data"],
            slides=[original],
        ),
        None,
        [
            QualityIssue(
                code="vision.chart_semantics",
                severity="error",
                slide_id="slide-a",
                message="chart semantics are incomplete",
                repairable=True,
            ),
        ],
        designer,
        accepted_slide_callback=lambda deck, plan, slide_id: accepted.append(
            (deck, plan, slide_id),
        ),
        proposal_attempt_limit=2,
    )

    assert repaired.changed is True
    assert repaired.deck.slides[0] == valid
    assert len(designer.requests) == 2
    assert len(accepted) == 1
    assert accepted[0][0].slides[0] == valid
    assert accepted[0][1] is None
    assert accepted[0][2] == "slide-a"
    assert "native chart customer_trend_chart requires a unit" in (
        designer.requests[1].retry_directive or ""
    )


def test_v3_repair_rejects_a_redesign_that_drops_an_immutable_fact() -> None:
    original = DeckSlide(
        id="slide-a",
        intent_id="intent-a",
        layout_id="key_judgment",
        objects=[TextObject(id="old", role="body", text="转化率达到 95%")],
    )
    replacement = DeckSlide(
        id="slide-a",
        intent_id="intent-a",
        layout_id="editorial_metric",
        objects=[TextObject(id="new", role="body", text="转化率明显提升")],
    )
    deck = DeckIR(
        id="deck-a",
        title="quality",
        immutable_data=["95%"],
        slides=[original],
    )

    with pytest.raises(ValueError, match="removed immutable facts"):
        repair_deck_with_designer(
            deck,
            None,
            [
                QualityIssue(
                    code="vision.composition",
                    severity="error",
                    slide_id="slide-a",
                    message="weak composition",
                    repairable=True,
                )
            ],
            _Designer(replacement),
        )


def test_v3_repair_retries_missing_immutable_fact_with_safe_path_feedback(
) -> None:
    immutable_fact = "受保护事实为 95%"
    original = DeckSlide(
        id="slide-a",
        intent_id="intent-a",
        layout_id="freeform",
        objects=[TextObject(id="body", role="body", text=immutable_fact)],
    )
    invalid = original.model_copy(
        update={
            "objects": [
                original.objects[0].model_copy(
                    update={"text": "改写后遗漏受保护事实"},
                ),
            ],
        },
    )
    valid = original.model_copy(
        update={
            "objects": [
                original.objects[0].model_copy(
                    update={
                        "text": immutable_fact,
                        "frame": ObjectFrame(x=1, y=1, w=5, h=2),
                    },
                ),
            ],
        },
    )
    designer = _RetryingImmutableFactDesigner(invalid, valid)

    repaired = repair_deck_with_designer(
        DeckIR(
            id="deck-a",
            title="quality",
            immutable_data=[immutable_fact],
            slides=[original],
        ),
        None,
        [
            QualityIssue(
                code="design.semantic_layout_mismatch",
                severity="error",
                slide_id="slide-a",
                message="semantic layout mismatch",
                repairable=True,
            ),
        ],
        designer,
        proposal_attempt_limit=2,
    )

    assert repaired.changed is True
    assert repaired.deck.slides[0] == valid
    assert len(designer.requests) == 2
    assert designer.requests[0].visible_immutable_facts == (
        ("immutable_facts[0]", immutable_fact),
    )
    retry_directive = designer.requests[1].retry_directive or ""
    assert "immutable_facts[0]" in retry_directive
    assert immutable_fact not in retry_directive


def test_v3_repair_rejects_a_redesign_that_changes_the_storyboard_title() -> None:
    original = DeckSlide(
        id="slide-a",
        intent_id="intent-a",
        layout_id="key_judgment",
        objects=[
            TextObject(id="title", role="title", text="必须保留的标题"),
            TextObject(id="body", role="body", text="支持标题的事实"),
        ],
    )
    replacement = original.model_copy(
        update={
            "layout_id": "editorial_metric",
            "objects": [
                TextObject(id="title-new", role="title", text="被改写的标题"),
                TextObject(id="body-new", role="body", text="支持标题的事实"),
            ],
        },
    )
    designer = _Designer(replacement)

    with pytest.raises(
        ValueError,
        match="slide redesign must preserve the storyboard title",
    ):
        repair_deck_with_designer(
            DeckIR(id="deck-a", title="quality", slides=[original]),
            None,
            [
                QualityIssue(
                    code="vision.composition",
                    severity="error",
                    slide_id="slide-a",
                    message="weak composition",
                    repairable=True,
                ),
            ],
            designer,
            proposal_attempt_limit=2,
        )

    assert len(designer.requests) == 2
    assert designer.requests[0].retry_directive is None
    assert "slide redesign must preserve the storyboard title" in (
        designer.requests[1].retry_directive or ""
    )


def test_v3_structural_repair_synchronizes_split_contract() -> None:
    original_intent = SlideIntent(
        id="intent-a",
        purpose="prove",
        key_message="95% 的转化率由两项证据共同支撑",
        source_refs=["source:judgment", "source:evidence"],
        required_facts=["95%", "样本 240 份"],
        immutable_facts=["95%", "240"],
    )
    following_intent = SlideIntent(
        id="intent-b",
        purpose="decide",
        key_message="批准下一阶段",
    )
    plan = DeckPlan(
        title="quality",
        narrative_arc="evidence to decision",
        visual_direction=_direction(),
        slides=[
            DeckPlanSlide(
                id="plan-a",
                order=1,
                title="转化率判断",
                intent=original_intent,
                content_blocks=["95%", "样本 240 份"],
            ),
            DeckPlanSlide(
                id="plan-b",
                order=2,
                title="决策",
                intent=following_intent,
                content_blocks=["批准下一阶段"],
            ),
        ],
    )
    original = DeckSlide(
        id="slide-a",
        intent_id="intent-a",
        layout_id="freeform",
        speaker_notes="原页同时解释判断与证据",
        source_fact_ids=["fact-judgment", "fact-evidence"],
        objects=[
            TextObject(
                id="old-a",
                role="body",
                text="转化率达到 95%，样本 240 份",
            ),
        ],
    )
    following = DeckSlide(
        id="slide-b",
        intent_id="intent-b",
        layout_id="freeform",
        speaker_notes="保持不变",
        objects=[TextObject(id="old-b", role="body", text="批准下一阶段")],
    )
    first = DeckSlide(
        id="slide-a",
        intent_id="intent-a",
        layout_id="freeform",
        speaker_notes="先讲清 95% 的核心判断",
        source_fact_ids=["fact-judgment"],
        objects=[TextObject(id="new-a", role="body", text="转化率达到 95%")],
    )
    second = DeckSlide(
        id="slide-a-evidence",
        intent_id="intent-a-evidence",
        layout_id="freeform",
        speaker_notes="再解释样本 240 份的证据边界",
        source_fact_ids=["fact-evidence"],
        objects=[TextObject(id="new-evidence", role="body", text="样本 240 份")],
    )
    split_plan_slides = (
        DeckPlanSlide(
            id="plan-a",
            order=1,
            title="转化率达到 95%",
            intent=original_intent.model_copy(
                update={
                    "source_refs": ["source:judgment"],
                    "required_facts": ["95%"],
                    "immutable_facts": ["95%"],
                },
            ),
            content_blocks=["95%"],
        ),
        DeckPlanSlide(
            id="plan-a-evidence",
            order=2,
            title="240 份样本界定证据边界",
            intent=SlideIntent(
                id="intent-a-evidence",
                purpose="prove",
                key_message="样本 240 份构成证据边界",
                source_refs=["source:evidence"],
                required_facts=["样本 240 份"],
                immutable_facts=["240"],
            ),
            content_blocks=["样本 240 份"],
        ),
    )
    deck = DeckIR(
        id="deck-a",
        title="quality",
        immutable_data=["95%", "240"],
        facts=[
            FactRecord(
                fact_id="fact-judgment",
                text="转化率达到 95%",
                source_refs=["source:judgment"],
                immutable=True,
            ),
            FactRecord(
                fact_id="fact-evidence",
                text="样本 240 份",
                source_refs=["source:evidence"],
                immutable=True,
            ),
        ],
        slides=[original, following],
    )
    designer = _SplitDesigner(first, second, split_plan_slides)

    repaired = repair_deck_with_designer(
        deck,
        plan,
        [
            QualityIssue(
                code="content.density",
                severity="error",
                slide_id="slide-a",
                message="判断和证据挤在同一页",
                repairable=True,
            ),
        ],
        designer,
        allow_structural_repair=True,
        slide_count_ceiling=3,
    )

    assert repaired.changed is True
    assert repaired.plan is not None
    assert [slide.id for slide in repaired.deck.slides] == [
        "slide-a",
        "slide-a-evidence",
        "slide-b",
    ]
    assert [slide.speaker_notes for slide in repaired.deck.slides[:2]] == [
        "先讲清 95% 的核心判断",
        "再解释样本 240 份的证据边界",
    ]
    assert [slide.id for slide in repaired.plan.slides] == [
        "plan-a",
        "plan-a-evidence",
        "plan-b",
    ]
    assert [slide.order for slide in repaired.plan.slides] == [1, 2, 3]
    assert [slide.intent.id for slide in repaired.plan.slides] == [
        "intent-a",
        "intent-a-evidence",
        "intent-b",
    ]
    assert set().union(
        *(set(slide.intent.source_refs) for slide in repaired.plan.slides[:2]),
    ) == set(original_intent.source_refs)
    assert set().union(
        *(set(slide.source_fact_ids) for slide in repaired.deck.slides[:2]),
    ) == set(original.source_fact_ids)
    assert repaired.deck.slides[-1] == following
    assert repaired.actions == ("slide-a: split_slide — 把判断和证据拆成两个可读页面",)
    assert designer.requests[0].planned_slide == plan.slides[0]

    ceiling_designer = _SplitDesigner(first, second, split_plan_slides)
    with pytest.raises(ValueError, match="split is not allowed"):
        repair_deck_with_designer(
            deck,
            plan,
            [
                QualityIssue(
                    code="content.density",
                    severity="error",
                    slide_id="slide-a",
                    message="判断和证据挤在同一页",
                    repairable=True,
                ),
            ],
            ceiling_designer,
            allow_structural_repair=True,
            slide_count_ceiling=2,
        )
    assert ceiling_designer.requests[0].structural_repair_allowed is False
    assert RepairActionType.SPLIT_SLIDE not in (
        ceiling_designer.requests[0].allowed_actions
    )

    duplicated_fact = second.model_copy(
        update={
            "source_fact_ids": ["fact-judgment", "fact-evidence"],
        },
    )
    with pytest.raises(ValueError, match="partition DeckIR fact ids"):
        repair_deck_with_designer(
            deck,
            plan,
            [
                QualityIssue(
                    code="content.density",
                    severity="error",
                    slide_id="slide-a",
                    message="判断和证据挤在同一页",
                    repairable=True,
                ),
            ],
            _SplitDesigner(first, duplicated_fact, split_plan_slides),
            allow_structural_repair=True,
            slide_count_ceiling=3,
        )

    duplicate_notes = second.model_copy(
        update={"speaker_notes": first.speaker_notes},
    )
    with pytest.raises(ValueError, match="speaker-note boundaries"):
        repair_deck_with_designer(
            deck,
            plan,
            [
                QualityIssue(
                    code="content.density",
                    severity="error",
                    slide_id="slide-a",
                    message="判断和证据挤在同一页",
                    repairable=True,
                ),
            ],
            _SplitDesigner(first, duplicate_notes, split_plan_slides),
            allow_structural_repair=True,
            slide_count_ceiling=3,
        )


def test_v3_repair_forbids_split_in_single_slide_scope() -> None:
    intent = SlideIntent(
        id="intent-a",
        purpose="prove",
        key_message="核心判断",
    )
    plan_slide = DeckPlanSlide(
        id="plan-a",
        order=1,
        title="判断",
        intent=intent,
    )
    plan = DeckPlan(
        title="quality",
        narrative_arc="evidence",
        visual_direction=_direction(),
        slides=[plan_slide],
    )
    original = DeckSlide(
        id="slide-a",
        intent_id="intent-a",
        layout_id="freeform",
        objects=[TextObject(id="old", role="body", text="核心判断")],
    )
    first = original.model_copy(
        update={"objects": [TextObject(id="new", role="body", text="判断")]},
    )
    second = DeckSlide(
        id="slide-a-detail",
        intent_id="intent-a-detail",
        layout_id="freeform",
        objects=[TextObject(id="detail", role="body", text="补充")],
    )
    second_plan = DeckPlanSlide(
        id="plan-a-detail",
        order=2,
        title="补充",
        intent=SlideIntent(
            id="intent-a-detail",
            purpose="prove",
            key_message="补充",
        ),
    )

    with pytest.raises(
        ValueError,
        match="structural slide split is not allowed",
    ):
        repair_deck_with_designer(
            DeckIR(id="deck-a", title="quality", slides=[original]),
            plan,
            [
                QualityIssue(
                    code="user.redesign",
                    severity="error",
                    slide_id="slide-a",
                    message="重做这一页",
                    repairable=True,
                ),
            ],
            _SplitDesigner(first, second, (plan_slide, second_plan)),
        )


def test_compiled_slide_can_be_rendered_and_visually_inspected(tmp_path: Path) -> None:
    deck = DeckIR(
        title="render",
        slides=[DeckSlide(layout_id="cover", objects=[TextObject(role="title", text="真实逐页渲染", slot="title")])],
    )
    pptx = tmp_path / "render.pptx"
    PresentationCompiler().compile(deck, pptx)
    try:
        rendered = render_pptx(pptx, tmp_path / "rendered")
    except PresentationRenderUnavailable as exc:
        pytest.skip(str(exc))

    assert len(rendered.slide_images) == 1
    assert inspect_rendered_slides(rendered.slide_images) == []


def test_slide_image_paths_uses_numeric_page_order(tmp_path: Path) -> None:
    for number in (10, 2, 1):
        (tmp_path / f"slide-{number}.png").touch()
    (tmp_path / "slide-preview.png").touch()

    assert [path.name for path in slide_image_paths(tmp_path)] == [
        "slide-1.png",
        "slide-2.png",
        "slide-10.png",
    ]


@pytest.mark.parametrize(
    ('rendered_text', 'fails'),
    [('正文需要保留中文证据标题', False), ('标题', True)],
)
def test_render_text_check_rejects_lost_chinese_but_ignores_reading_order(
    tmp_path: Path, monkeypatch, rendered_text: str, fails: bool,
) -> None:
    from zipfile import ZipFile

    from lantai.presentation import render

    source = tmp_path / 'source.pptx'
    with ZipFile(source, 'w') as archive:
        archive.writestr('ppt/presentation.xml', '''
            <p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
              xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
              <p:sldIdLst><p:sldId id="256" r:id="rId7"/></p:sldIdLst>
            </p:presentation>''')
        archive.writestr('ppt/_rels/presentation.xml.rels', '''
            <Relationships><Relationship Id="rId7" Target="slides/slide9.xml"/></Relationships>''')
        archive.writestr('ppt/slides/slide9.xml', '''
            <slide xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
              <a:t>标题</a:t><a:t>正文需要保留中文证据</a:t>
            </slide>''')

    class TextPage:
        def get_text_range(self):
            return rendered_text

        def close(self):
            pass

    class Page:
        def get_textpage(self):
            return TextPage()

        def close(self):
            pass

    class Document:
        def __len__(self):
            return 1

        def __getitem__(self, index):
            assert index == 0
            return Page()

        def close(self):
            pass

    monkeypatch.setattr(render.pdfium, 'PdfDocument', lambda _: Document())
    if fails:
        with pytest.raises(RuntimeError, match='slide 1 lost 10 of 12 CJK'):
            render.verify_rendered_text(source, tmp_path / 'rendered.pdf')
    else:
        render.verify_rendered_text(source, tmp_path / 'rendered.pdf')
