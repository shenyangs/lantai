# -*- coding: utf-8 -*-
"""Hard boundaries for model-authored native page scenes."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import threading
import warnings
from collections.abc import Sequence
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

import lantai.presentation.intelligence as presentation_intelligence
import lantai.presentation.page_authoring as presentation_page_authoring
from lantai.app.structured_utility import (
    StructuredTaskValidationError,
    StructuredUtilityResult,
)
from lantai.app.utility_model import UtilityTaskTimeoutError
from lantai.presentation.intelligence import (
    UtilityPresentationPageAuthor,
    UtilityPresentationRepairDesigner,
    UtilityPresentationSemanticPlanner,
    UtilityPresentationVisualCritic,
)
from lantai.presentation.jobs import PresentationJobInterruptedError
from lantai.presentation.models import (
    ChartObject,
    CompositionAuthority,
    DeckIR,
    DeckPlanSlide,
    DeckSlide,
    InputMaterial,
    JobStage,
    ObjectGroup,
    ObjectFrame,
    ObjectRelationship,
    PresentationBrief,
    PresentationFailureCategory,
    PresentationFailureRoute,
    PresentationJobRequest,
    PresentationRecoveryAction,
    PresentationSourceSection,
    RelationshipType,
    RichTextRun,
    SlideIntent,
    SourceCalculationProof,
    TextObject,
    VisualCriticSlideInput,
    VisualIssue,
)
from lantai.presentation.page_authoring import (
    PresentationPageAuthorSceneError,
    PresentationSemanticContentError,
    grounded_source_text,
    numeric_tokens,
    page_allowed_numeric_tokens,
    scope_authored_page_object_ids,
    validate_authored_page,
    validate_grounded_numeric_text,
    validate_planned_chart_sources,
    verified_calculation_values,
)
from lantai.presentation.planner import (
    build_brief,
    build_deck_ir,
    build_deterministic_fixture_plan,
)
from lantai.presentation.repair import RepairActionType, SlideRedesignRequest
from lantai.presentation.runtime import PresentationJobRuntime
from tests.unit.presentation.visual_review_fixtures import synthetic_review_response


def _semantic_value_for_call(
    kwargs: dict[str, object],
    plan,
    *,
    mutate_claims=None,
    mutate_outline=None,
    mutate_detail=None,
):
    """Return the matching staged semantic fixture without importing privates."""

    result_model = kwargs["result_model"]
    if result_model.__name__ == "_SemanticClaimLedger":
        value = result_model.from_runtime(plan)
        return mutate_claims(value) if mutate_claims else value
    if result_model.__name__ == "_SemanticDeckOutline":
        value = result_model.from_runtime(plan)
        return mutate_outline(value) if mutate_outline else value
    payload = json.loads(str(kwargs["user_prompt"]))
    target_orders = {
        int(slide["order"]) for slide in payload["target_slides"]
    }
    claim_ids_by_order = {
        int(slide["order"]): [
            str(claim["claim_id"])
            for claim in slide.get("claims", [])
        ]
        for slide in payload["target_slides"]
    }
    value = result_model.from_runtime(
        [slide for slide in plan.slides if slide.order in target_orders],
        claim_ids_by_order=claim_ids_by_order,
    )
    return mutate_detail(value) if mutate_detail else value


def _case():
    chart = ChartObject(
        id="chart-1",
        categories=["甲", "乙"],
        series={"收入": [42, 38]},
        unit="万元",
        source_refs=["source-1"],
        frame=ObjectFrame(x=5, y=2, w=7, h=4),
    )
    planned = DeckPlanSlide(
        id="plan-1",
        order=1,
        title="收入判断",
        intent=SlideIntent(
            id="intent-1",
            purpose="解释收入",
            key_message="甲高于乙",
            required_facts=["甲为42万元"],
            immutable_facts=["乙为38万元"],
        ),
        charts=[chart],
    )
    slide = DeckSlide(
        id="slide-1",
        intent_id="intent-1",
        layout_id="freeform",
        composition_authority=CompositionAuthority.AUTHOR,
        source_fact_ids=["fact-1"],
        objects=[
            TextObject(
                id="title-1", role="title", text="收入判断",
                frame=ObjectFrame(x=1, y=0.5, w=10, h=1),
            ),
            TextObject(
                id="fact-1", role="body", text="甲为42万元；乙为38万元",
                frame=ObjectFrame(x=1, y=2, w=3, h=2),
            ),
            chart,
        ],
    )
    return planned, slide


def test_page_author_scene_hash_binds_saved_native_bytes() -> None:
    planned, slide = _case()
    checked = validate_authored_page(
        slide, planned, slide, source_text="甲为42万元；乙为38万元",
    )
    assert checked.scene_hash
    assert validate_authored_page(
        checked, planned, slide, source_text="甲为42万元；乙为38万元",
    ) == checked
    tampered = checked.model_copy(update={"speaker_notes": "悄悄改过"})
    with pytest.raises(ValueError, match="checkpoint hash"):
        validate_authored_page(
            tampered, planned, slide, source_text="甲为42万元；乙为38万元",
        )


def test_page_author_keeps_pre_calculation_scene_hash_after_json_roundtrip():
    from lantai.presentation.natural_edit import _scene_hash

    planned, scene = _case()
    payload = scene.model_dump(mode="json")
    for obj in payload["objects"]:
        if obj["kind"] == "chart":
            obj.pop("calculation_proofs")
    old_hash = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    payload["scene_hash"] = old_hash
    loaded = DeckSlide.model_validate(payload)
    for restored in (loaded, DeckSlide.model_validate_json(loaded.model_dump_json())):
        assert _scene_hash(restored) == old_hash
        checked = validate_authored_page(restored, planned, scene, source_text="甲为42万元；乙为38万元")
        assert checked.scene_hash == old_hash
        tampered = restored.model_copy(update={"speaker_notes": "更改过的备注"})
        with pytest.raises(ValueError, match="checkpoint hash"):
            validate_authored_page(tampered, planned, scene, source_text="甲为42万元；乙为38万元")


def test_page_author_rejects_missing_visible_fact_and_changed_chart_data() -> None:
    planned, slide = _case()
    missing = slide.model_copy(
        update={
            "objects": [
                obj.model_copy(update={"text": "乙为38万元"})
                if isinstance(obj, TextObject) and obj.role == "body" else obj
                for obj in slide.objects
            ],
        },
    )
    with pytest.raises(ValueError, match="required visible fact") as exc_info:
        validate_authored_page(
            missing, planned, slide, source_text="甲为42万元；乙为38万元",
        )
    assert exc_info.value.presentation_missing_fact_paths == [
        "required_facts[0]",
    ]
    assert "甲为42万元；乙为38万元" not in str(exc_info.value)
    changed = slide.model_copy(
        update={
            "objects": [
                obj.model_copy(update={"series": {"收入": [42, 39]}})
                if isinstance(obj, ChartObject) else obj
                for obj in slide.objects
            ],
        },
    )
    with pytest.raises(ValueError, match="chart data"):
        validate_authored_page(
            changed, planned, slide, source_text="甲为42万元；乙为38万元",
        )


def test_page_author_checks_rich_text_runs_for_invented_numbers() -> None:
    planned, slide = _case()
    invented = slide.model_copy(
        update={
            "objects": [
                obj.model_copy(update={"runs": [RichTextRun(text="增长99%") ]})
                if isinstance(obj, TextObject) and obj.role == "body" else obj
                for obj in slide.objects
            ],
        },
    )
    with pytest.raises(ValueError, match="unsupported number"):
        validate_authored_page(
            invented, planned, slide, source_text="甲为42万元；乙为38万元",
        )


def test_page_author_checks_notes_for_invented_numbers() -> None:
    planned, slide = _case()
    invented = slide.model_copy(update={"speaker_notes": "另有收入99万元"})
    with pytest.raises(ValueError, match="speaker notes"):
        validate_authored_page(
            invented, planned, slide, source_text="甲为42万元；乙为38万元",
        )


def test_page_author_requires_numeric_free_speaker_notes_even_for_page_facts() -> None:
    planned, slide = _case()
    repeated = slide.model_copy(update={"speaker_notes": "口头强调42万元。"})
    with pytest.raises(ValueError, match="speaker notes"):
        validate_authored_page(
            repeated, planned, slide, source_text="甲为42万元；乙为38万元",
        )


def test_page_author_rejects_source_wide_number_absent_from_current_storyboard() -> None:
    planned, slide = _case()
    leaked = slide.model_copy(update={
        "objects": [
            obj.model_copy(update={"text": f"{obj.text}；其他页事实为99万元"})
            if isinstance(obj, TextObject) and obj.role == "body" else obj
            for obj in slide.objects
        ],
    })
    with pytest.raises(ValueError, match="unsupported number"):
        validate_authored_page(
            leaked,
            planned,
            slide,
            source_text="甲为42万元；乙为38万元；其他页事实为99万元",
        )


def test_page_numeric_allowlist_excludes_runtime_and_identifier_digits() -> None:
    planned, _slide = _case()
    planned.content_blocks = ["甲为42万元，乙为38万元。"]
    planned.takeaway = "差额来自已给事实，不新增数字。"

    assert page_allowed_numeric_tokens(planned) == ["42", "38"]


def test_page_numeric_allowlist_normalizes_only_binary_subtraction() -> None:
    planned, _slide = _case()
    planned.intent.required_facts = []
    planned.intent.immutable_facts = []
    planned.content_blocks = ["42万元 -38万元 = 4万元"]
    planned.takeaway = "成本为 -7万元。"

    assert page_allowed_numeric_tokens(planned) == ["42", "38", "4", "-7"]


def test_page_author_rejects_decorative_page_number() -> None:
    planned, slide = _case()
    numbered = slide.model_copy(update={
        "objects": [
            *slide.objects,
            TextObject(
                id="page-number",
                role="meta",
                slot="page-marker",
                text="01",
                frame=ObjectFrame(x=12, y=7, w=0.5, h=0.2),
            ),
        ],
    })
    with pytest.raises(ValueError, match="decorative numbering"):
        validate_authored_page(
            numbered, planned, slide, source_text="甲为42万元；乙为38万元",
        )


def test_page_author_rejects_dangling_native_object_reference_as_scene_error() -> None:
    planned, slide = _case()
    dangling = slide.model_copy(update={
        "object_groups": [ObjectGroup(
            group_id="group-1",
            role="evidence",
            object_ids=["title-1", "missing-object"],
        )],
    })

    with pytest.raises(
        PresentationPageAuthorSceneError,
        match="missing native object",
    ) as caught:
        validate_authored_page(
            dangling,
            planned,
            slide,
            source_text="甲为42万元；乙为38万元",
        )

    assert caught.value.violation_code == "dangling_object_reference"


def test_authored_page_object_ids_are_scoped_without_losing_native_references() -> None:
    planned, slide = _case()
    slide.object_groups = [ObjectGroup(
        group_id="evidence-group",
        role="evidence",
        object_ids=["title-1", "fact-1"],
    )]
    slide.relationships = [ObjectRelationship(
        relationship_id="evidence-flow",
        relationship_type=RelationshipType.EVIDENCE_COLLECTION,
        source_object_ids=["fact-1"],
        target_object_ids=["chart-1"],
    )]

    scoped = scope_authored_page_object_ids(slide)

    assert [obj.id for obj in scoped.objects] == [
        "slide-1::title-1",
        "slide-1::fact-1",
        "slide-1::chart-1",
    ]
    assert scoped.object_groups[0].object_ids == [
        "slide-1::title-1",
        "slide-1::fact-1",
    ]
    assert scoped.relationships[0].source_object_ids == [
        "slide-1::fact-1",
    ]
    assert scoped.relationships[0].target_object_ids == [
        "slide-1::chart-1",
    ]
    assert scoped.scene_hash is None
    assert scope_authored_page_object_ids(scoped) == scoped


def test_runtime_migrates_legacy_page_local_ids_without_weakening_deckir(
    tmp_path,
) -> None:
    first = DeckSlide(
        id="slide-a",
        layout_id="freeform",
        objects=[
            TextObject(
                id="title",
                role="title",
                text="A",
                frame=ObjectFrame(x=1, y=1, w=4, h=1),
            ),
        ],
    )
    second = DeckSlide(
        id="slide-b",
        layout_id="freeform",
        objects=[
            TextObject(
                id="title",
                role="title",
                text="B",
                frame=ObjectFrame(x=1, y=1, w=4, h=1),
            ),
        ],
        object_groups=[ObjectGroup(
            group_id="headline",
            role="headline",
            object_ids=["title"],
        )],
    )
    raw = {
        "schema_version": "2.0",
        "title": "legacy local ids",
        "slides": [
            first.model_dump(mode="json"),
            second.model_dump(mode="json"),
        ],
    }
    checkpoint = tmp_path / "deck-ir.json"
    checkpoint.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="unique across"):
        DeckIR.model_validate(raw)

    deck, migrated = PresentationJobRuntime._load_saved_model_deck_checkpoint(
        checkpoint,
    )

    assert migrated is True
    assert [obj.id for slide in deck.slides for obj in slide.objects] == [
        "slide-a::title",
        "slide-b::title",
    ]
    assert deck.slides[1].object_groups[0].object_ids == [
        "slide-b::title",
    ]


def test_page_author_does_not_trust_invented_storyboard_number() -> None:
    planned, slide = _case()
    planned.title = "收入增长99%"
    slide.objects[0].text = planned.title
    with pytest.raises(ValueError, match="unsupported number"):
        validate_authored_page(
            slide, planned, slide, source_text="甲为42万元；乙为38万元",
        )


def test_page_author_accepts_sourced_subtraction_but_rejects_negative_claim() -> None:
    planned, slide = _case()
    planned.content_blocks = ["42万元 -38万元 = 4万元"]
    source_text = "甲为42万元；乙为38万元；差额为4万元"
    body = next(
        obj for obj in slide.objects
        if isinstance(obj, TextObject) and obj.role == "body"
    )
    body.text = "甲为42万元；乙为38万元；42万元 -38万元 = 4万元"

    validate_authored_page(slide, planned, slide, source_text=source_text)

    body.text = "甲为42万元；乙为38万元；成本为 -38万元；差额为4万元"
    with pytest.raises(
        ValueError,
        match=r"unsupported number\(s\): -38",
    ) as exc_info:
        validate_authored_page(slide, planned, slide, source_text=source_text)
    assert exc_info.value.presentation_numeric_tokens == ["-38"]


def test_grounding_excludes_numeric_source_hashes() -> None:
    request = PresentationJobRequest(
        conversation_id="grounding-hash-test",
        user_request="做判断页",
        slide_count=1,
        materials=[InputMaterial(name="事实.md", content="# 事实\n甲为42万元。")],
    )
    brief = build_brief(request)
    brief.source_coverage[0].source_sha256 = "99" * 32
    source_text = grounded_source_text(request, brief)
    assert "42" in source_text
    assert "99" not in source_text


def test_storyboard_numeric_grounding_allows_display_rounding_and_change_magnitude() -> None:
    source = '"growth": 9.375, "change": -9'

    validate_grounded_numeric_text(
        "Q4收入增长9.4%，经营贡献减少9万元",
        source,
        location="slide-1",
    )
    with pytest.raises(ValueError, match="uncited storyboard number 9"):
        validate_grounded_numeric_text(
            "Q4收入增长9%",
            '"growth": 9.375',
            location="slide-1",
        )


def test_storyboard_numeric_grounding_normalizes_scoped_chinese_integer_facts() -> None:
    source = "项目有四个研究小组；第二轮复核；统一材料口径。"

    validate_grounded_numeric_text(
        "项目由4个研究小组参与，第2轮负责复核",
        source,
        location="claim-ledger-claim-034",
    )
    assert numeric_tokens(source) == ["4", "2"]
    assert numeric_tokens("统一材料口径") == []
    with pytest.raises(ValueError, match="uncited storyboard number 5"):
        validate_grounded_numeric_text(
            "项目由5个研究小组参与",
            source,
            location="claim-ledger-claim-034",
        )
    with pytest.raises(ValueError, match="uncited storyboard number 5") as exc_info:
        validate_grounded_numeric_text(
            "项目由五个研究小组参与",
            source,
            location="claim-ledger-claim-034",
        )
    assert exc_info.value.presentation_numeric_token == "5"


@pytest.mark.parametrize(
    ("entrypoint", "text_length"),
    [("tokens", 2_000), ("tokens", 8_000), ("grounding", 2_000), ("page", 2_000)],
)
def test_long_prose_does_not_scan_quantity_suffix_without_an_operand(
    monkeypatch, entrypoint: str, text_length: int,
) -> None:
    """Lock linear prose scanning without a load-sensitive timing assertion."""

    original = presentation_page_authoring._QUANTITY_SUFFIX
    suffix_calls = []

    class SuffixProbe:
        def match(self, text: str, position: int = 0):
            suffix_calls.append(position)
            return original.match(text, position)

    monkeypatch.setattr(presentation_page_authoring, "_QUANTITY_SUFFIX", SuffixProbe())
    prose = ("材料口径说明" * (text_length // 6 + 1))[:text_length]
    if entrypoint == "tokens":
        assert numeric_tokens(prose, preserve_percent=True) == []
    elif entrypoint == "grounding":
        validate_grounded_numeric_text(prose, prose, location="long-prose")
    else:
        planned, scaffold = _case()
        planned = planned.model_copy(update={
            "charts": [],
            "intent": planned.intent.model_copy(update={
                "key_message": prose,
                "required_facts": [],
                "immutable_facts": [],
            }),
        })
        scaffold = scaffold.model_copy(update={
            "objects": [
                obj.model_copy(update={
                    "text": planned.title if obj.role == "title" else prose,
                })
                for obj in scaffold.objects if isinstance(obj, TextObject)
            ],
        })
        checked = validate_authored_page(
            scaffold, planned, scaffold, source_text=prose,
        )
        assert checked.scene_hash
    assert suffix_calls == [], "ordinary prose must not greedily rescan every suffix"


def test_numeric_retry_source_tokens_preserve_percent_without_changing_value_tokens() -> None:
    source = "原报数为-7；比例17%；另一比例9.375％；编号01；四个组；车型Q4。"

    assert numeric_tokens(source) == ["-7", "17", "9.375", "01", "4"]
    assert numeric_tokens(source, preserve_percent=True) == [
        "-7", "17%", "9.375％", "01", "4",
    ]


def test_storyboard_numeric_grounding_distinguishes_subtraction_from_negative_value() -> None:
    source = '"revenue": 120, "cost": 95, "contribution": 25'

    validate_grounded_numeric_text(
        "收入120万元 -95万元 = 25万元",
        source,
        location="claim-ledger-claim-001",
    )
    with pytest.raises(ValueError, match="uncited storyboard number -95"):
        validate_grounded_numeric_text(
            "成本为 -95万元",
            source,
            location="claim-ledger-claim-001",
        )


@pytest.mark.parametrize("minus", ["-", "−", "－", "﹣"])
@pytest.mark.parametrize("gap", ["", " ", "\t", "\u00a0"])
def test_grounding_preserves_unary_minus_notation(minus: str, gap: str) -> None:
    source = f"原报负数为 {minus}{gap}7万元；比例为 {minus}{gap}17%。"

    assert numeric_tokens(source, preserve_percent=True) == ["-7", "-17%"]
    validate_grounded_numeric_text(
        "原报负数为-7万元；比例为-17%。", source, location="signed-claim",
    )
    validate_grounded_numeric_text(
        source, "原报负数为-7万元；比例为-17%。", location="signed-claim",
    )


@pytest.mark.parametrize("minus", ["-", "−", "－", "﹣"])
@pytest.mark.parametrize("gap", ["", " ", "\t", "\u00a0"])
@pytest.mark.parametrize("equals", ["=", "＝"])
def test_grounding_keeps_subtraction_operands_positive(minus: str, gap: str, equals: str) -> None:
    equation = f"120万元 {minus}{gap}95万元 {equals} 25万元"

    validate_grounded_numeric_text(
        equation, "收入120万元；成本95万元；贡献25万元", location="equation",
    )
    validate_grounded_numeric_text(
        "收入120万元；成本95万元；贡献25万元", equation, location="equation",
    )
    with pytest.raises(ValueError, match="uncited storyboard number -95"):
        validate_grounded_numeric_text(
            "成本为-95万元", equation, location="unary-claim",
        )


@pytest.mark.parametrize("minus", ["-", "−", "－", "﹣"])
@pytest.mark.parametrize("gap", ["", " ", "\t", "\u00a0"])
@pytest.mark.parametrize("equals", ["=", "＝"])
@pytest.mark.parametrize(("last_operator", "result"), [("-", 20), ("+", 30)])
def test_grounding_preserves_explicit_arithmetic_chains(
    minus: str, gap: str, equals: str, last_operator: str, result: int,
) -> None:
    last_operator = minus if last_operator == "-" else last_operator
    equation = f"120万元 {minus}{gap}95万元 {last_operator}{gap}5万元 {equals} {result}万元"
    source = f"收入为120万元；成本为95万元；费用为5万元；贡献为{result}万元。"

    validate_grounded_numeric_text(equation, source, location="arithmetic-chain")
    validate_grounded_numeric_text(source, equation, location="arithmetic-chain")
    for invented in ("成本为-95万元。", "费用为-5万元。"):
        with pytest.raises(ValueError, match="uncited storyboard number"):
            validate_grounded_numeric_text(invented, equation, location="not-a-negative")


@pytest.mark.parametrize("minus", ["-", "−", "－", "﹣"])
@pytest.mark.parametrize("equals", ["=", "＝"])
@pytest.mark.parametrize(("last_operator", "result"), [("-", 20), ("+", 30)])
def test_page_author_preserves_explicit_arithmetic_chains(
    minus: str, equals: str, last_operator: str, result: int,
) -> None:
    last_operator = minus if last_operator == "-" else last_operator
    equation = f"120万元 {minus}95万元 {last_operator}5万元 {equals} {result}万元"
    planned, slide = _case()
    planned.content_blocks.append(equation)
    body = next(
        obj for obj in slide.objects
        if isinstance(obj, TextObject) and obj.role == "body"
    )
    body.text += f"；{equation}"
    source = f"甲为42万元；乙为38万元；收入120万元；成本95万元；费用5万元；贡献{result}万元。"

    assert "95" in page_allowed_numeric_tokens(planned)
    assert "-95" not in page_allowed_numeric_tokens(planned)
    validate_authored_page(slide, planned, slide, source_text=source)
    body.text += "；成本为-95万元"
    with pytest.raises(ValueError, match=r"unsupported number\(s\): -95"):
        validate_authored_page(slide, planned, slide, source_text=source)


@pytest.mark.parametrize("minus", ["-", "−", "－", "﹣"])
@pytest.mark.parametrize("equals", ["=", "＝"])
@pytest.mark.parametrize(("equation", "source", "signed_tokens"), [
    (
        "120万元 {minus}95万元 {minus} {minus}5万元 {equals}30万元",
        "收入120万元；成本95万元；调整-5万元；贡献30万元。",
        ["-5"],
    ),
    (
        "120万元 {minus}95万元 ×(2) {equals}{minus}70万元",
        "收入120万元；成本95万元；系数2；贡献-70万元。",
        ["-70"],
    ),
    (
        "(120万元 {minus}95万元) {minus}5万元 {equals}20万元",
        "收入120万元；成本95万元；费用5万元；贡献20万元。",
        [],
    ),
    (
        "120万元 {minus}95万元 {minus}({minus}5万元) {equals}30万元",
        "收入120万元；成本95万元；调整-5万元；贡献30万元。",
        ["-5"],
    ),
    (
        "25万元 {equals}120万元 {minus}95万元",
        "收入120万元；成本95万元；贡献25万元。",
        [],
    ),
    (
        "（120万元 {minus}95万元） {minus}5万元 {equals}20万元",
        "收入120万元；成本95万元；费用5万元；贡献20万元。",
        [],
    ),
    (
        "120万元 {minus}95万元 + {minus}5万元 {equals}20万元",
        "收入120万元；成本95万元；调整-5万元；贡献20万元。",
        ["-5"],
    ),
])
def test_grounding_preserves_signed_and_parenthesized_equation_operands(
    minus: str, equals: str, equation: str, source: str, signed_tokens: list[str],
) -> None:
    equation = equation.format(minus=minus, equals=equals)

    validate_grounded_numeric_text(equation, source, location="structured-equation")
    validate_grounded_numeric_text(source, equation, location="structured-equation")
    with pytest.raises(ValueError, match="uncited storyboard number -95"):
        validate_grounded_numeric_text(
            "成本为-95万元。", equation, location="invented-unary-negative",
        )
    for token in signed_tokens:
        with pytest.raises(ValueError, match="uncited storyboard number"):
            validate_grounded_numeric_text(
                f"原报数为{token}万元。", source.replace(token, token[1:]),
                location="invented-operand-sign",
            )


@pytest.mark.parametrize("minus", ["-", "−", "－", "﹣"])
@pytest.mark.parametrize("equals", ["=", "＝"])
@pytest.mark.parametrize(("equation", "source", "signed_tokens"), [
    (
        "120万元 {minus}95万元 {minus} {minus}5万元 {equals}30万元",
        "收入120万元；成本95万元；调整-5万元；贡献30万元。",
        ["-5"],
    ),
    (
        "120万元 {minus}95万元 ×(2) {equals}{minus}70万元",
        "收入120万元；成本95万元；系数2；贡献-70万元。",
        ["-70"],
    ),
    (
        "(120万元 {minus}95万元) {minus}5万元 {equals}20万元",
        "收入120万元；成本95万元；费用5万元；贡献20万元。",
        [],
    ),
    (
        "120万元 {minus}95万元 {minus}({minus}5万元) {equals}30万元",
        "收入120万元；成本95万元；调整-5万元；贡献30万元。",
        ["-5"],
    ),
    (
        "25万元 {equals}120万元 {minus}95万元",
        "收入120万元；成本95万元；贡献25万元。",
        [],
    ),
    (
        "（120万元 {minus}95万元） {minus}5万元 {equals}20万元",
        "收入120万元；成本95万元；费用5万元；贡献20万元。",
        [],
    ),
    (
        "120万元 {minus}95万元 + {minus}5万元 {equals}20万元",
        "收入120万元；成本95万元；调整-5万元；贡献20万元。",
        ["-5"],
    ),
])
def test_page_author_preserves_signed_and_parenthesized_equation_operands(
    minus: str, equals: str, equation: str, source: str, signed_tokens: list[str],
) -> None:
    equation = equation.format(minus=minus, equals=equals)
    planned, slide = _case()
    planned.content_blocks.append(equation)
    body = next(
        obj for obj in slide.objects
        if isinstance(obj, TextObject) and obj.role == "body"
    )
    body.text += f"；{equation}"
    source = f"甲为42万元；乙为38万元；{source}"

    tokens = page_allowed_numeric_tokens(planned)
    assert "95" in tokens
    assert "-95" not in tokens
    assert all(token in tokens for token in signed_tokens)
    validate_authored_page(slide, planned, slide, source_text=source)
    body.text += "；成本为-95万元"
    with pytest.raises(ValueError, match=r"unsupported number\(s\): -95"):
        validate_authored_page(slide, planned, slide, source_text=source)


@pytest.mark.parametrize("unit", [
    "元", "万元", "千元", "百元", "亿元", "人", "个", "家", "次",
    "天", "月", "年", "季度", "%", "％",
])
@pytest.mark.parametrize(("equation", "adjustment", "result"), [
    ("(120 -95){unit} =25{unit}", False, 25),
    ("25{unit} =(120 -95){unit}", False, 25),
    ("（(120 -95){unit}） =25{unit}", False, 25),
    ("25{unit} =(120 -95){unit} =25{unit}", False, 25),
    ("（120 -95）{unit} -（−5）{unit} =30{unit}", True, 30),
])
def test_grounding_preserves_postfix_units_on_complete_parenthesized_quantities(
    unit: str, equation: str, adjustment: bool, result: int,
) -> None:
    equation = equation.format(unit=unit)
    source = f"收入120{unit}；成本95{unit}；贡献{result}{unit}。"
    if adjustment:
        source += f"调整-5{unit}。"

    validate_grounded_numeric_text(equation, source, location="postfix-quantity")
    validate_grounded_numeric_text(source, equation, location="postfix-quantity")
    with pytest.raises(ValueError, match="uncited storyboard number -95"):
        validate_grounded_numeric_text(
            f"成本为-95{unit}。", equation, location="invented-postfix-sign",
        )
    if adjustment:
        with pytest.raises(ValueError, match="uncited storyboard number -5"):
            validate_grounded_numeric_text(
                equation, source.replace("-5", "5"), location="invented-unary-sign",
            )


@pytest.mark.parametrize("unit", [
    "元", "万元", "千元", "百元", "亿元", "人", "个", "家", "次",
    "天", "月", "年", "季度", "%", "％",
])
@pytest.mark.parametrize(("equation", "adjustment", "result"), [
    ("(120 -95){unit} =25{unit}", False, 25),
    ("25{unit} =(120 -95){unit}", False, 25),
    ("（(120 -95){unit}） =25{unit}", False, 25),
    ("25{unit} =(120 -95){unit} =25{unit}", False, 25),
    ("（120 -95）{unit} -（−5）{unit} =30{unit}", True, 30),
])
def test_page_author_preserves_postfix_units_on_complete_parenthesized_quantities(
    unit: str, equation: str, adjustment: bool, result: int,
) -> None:
    equation = equation.format(unit=unit)
    planned, slide = _case()
    planned.content_blocks.append(equation)
    body = next(
        obj for obj in slide.objects
        if isinstance(obj, TextObject) and obj.role == "body"
    )
    body.text += f"；{equation}"
    source = f"甲为42万元；乙为38万元；收入120{unit}；成本95{unit}；贡献{result}{unit}。"
    if adjustment:
        source += f"调整-5{unit}。"

    tokens = page_allowed_numeric_tokens(planned)
    assert "95" in tokens
    assert "-95" not in tokens
    if adjustment:
        assert "-5" in tokens
    validate_authored_page(slide, planned, slide, source_text=source)
    body.text += f"；成本为-95{unit}"
    with pytest.raises(ValueError, match=r"unsupported number\(s\): -95"):
        validate_authored_page(slide, planned, slide, source_text=source)


_PARENTHESIZED_QUANTITY_CASES = [
    ("原报数95万元", "原报数为-(95)万元", False),
    ("原报数95万元", "原报数为−（95）万元", False),
    ("原报数95万元", "原报数为−（（95万元））", False),
    ("原报数95%", "原报数为-(95)%", False),
    ("原报数95%", "原报数为-(95%)", False),
    ("原报数95%", "原报数为−（（95）％）", False),
    ("原报数-95万元", "原报数为-(95)万元", True),
    ("原报数-(95)万元", "原报数为-95万元", True),
    ("原报数-95%", "原报数为−（95）％", True),
    ("原报数-(95)%", "原报数为-95%", True),
    ("原报数17%", "原报数为（17）％", True),
    ("原报数（17）％", "原报数为17%", True),
    ("原报数95万元", "原报数为-(-95)万元", True),
    ("原报数95%", "原报数为-(-(95))%", True),
    ("原报数-(-95)万元", "原报数为-95万元", False),
    ("原报数（17）％", "原报数为1700%", False),
    ("原报数（17）％", "原报数为(0.17)%", False),
    ("收入120万元；成本95万元；贡献25万元", "120万元 -(95万元) =25万元", True),
    ("收入120万元；成本95万元；贡献-25万元", "-(120万元 -95万元) =-25万元", True),
    ("收入120万元；成本95万元；贡献-25万元", "-(120 -95)万元 =-25万元", True),
    ("今年120万元 -95万元 =25万元", "收入120万元；成本95万元；贡献25万元", True),
]


@pytest.mark.parametrize(("source", "authored", "accepted"), _PARENTHESIZED_QUANTITY_CASES)
def test_grounding_preserves_atomic_parenthesized_sign_and_percent_notation(
    source: str, authored: str, accepted: bool,
) -> None:
    if accepted:
        validate_grounded_numeric_text(authored, source, location="atomic-quantity")
        validate_grounded_numeric_text(source, authored, location="atomic-quantity")
    else:
        with pytest.raises(ValueError, match="uncited storyboard number"):
            validate_grounded_numeric_text(authored, source, location="atomic-quantity")


@pytest.mark.parametrize(("source", "authored", "accepted"), _PARENTHESIZED_QUANTITY_CASES)
def test_page_author_preserves_atomic_parenthesized_sign_and_percent_notation(
    source: str, authored: str, accepted: bool,
) -> None:
    planned, slide = _case()
    planned.content_blocks.append(authored)
    body = next(
        obj for obj in slide.objects
        if isinstance(obj, TextObject) and obj.role == "body"
    )
    body.text += f"；{authored}"
    source = f"甲为42万元；乙为38万元；{source}"
    if accepted:
        validate_authored_page(slide, planned, slide, source_text=source)
    else:
        with pytest.raises(ValueError, match="unsupported number"):
            validate_authored_page(slide, planned, slide, source_text=source)


@pytest.mark.parametrize("authored", [
    "-(95万元)万元", "−（（95万元））万元", "-(17%)%",
    "−（17％）%", "(17%)%", "(95万元)万元",
])
def test_grounding_rejects_repeated_atomic_quantity_units(authored: str) -> None:
    with pytest.raises(ValueError, match="unsupported numeric notation"):
        validate_grounded_numeric_text(
            authored, "原报数为-95万元；比例为-17%", location="atomic-quantity",
        )


@pytest.mark.parametrize("authored", [
    "-(95万元)万元", "−（（95万元））万元", "-(17%)%",
    "−（17％）%", "(17%)%", "(95万元)万元",
])
def test_page_author_rejects_repeated_atomic_quantity_units(authored: str) -> None:
    planned, slide = _case()
    planned.content_blocks.append(authored)
    body = next(
        obj for obj in slide.objects
        if isinstance(obj, TextObject) and obj.role == "body"
    )
    body.text += f"；{authored}"
    with pytest.raises(ValueError, match="unsupported numeric notation"):
        validate_authored_page(
            slide, planned, slide,
            source_text="甲为42万元；乙为38万元；原报数-95万元；比例-17%",
        )


@pytest.mark.parametrize(("source", "expected"), [
    ("原报数-(95)万元", ["-95"]),
    ("原报数−（（95万元））", ["-95"]),
    ("原报数-(95)%", ["-95%"]),
    ("原报数-(95%)", ["-95%"]),
    ("原报数−（（95）％）", ["-95％"]),
    ("原报数-(-95)万元", ["95"]),
    ("原报数-(-(95))%", ["95%"]),
    ("- (95)万元", ["95"]),
    ("  - （17）％", ["17％"]),
])
def test_numeric_retry_tokens_preserve_atomic_sign_and_quantity(
    source: str, expected: list[str],
) -> None:
    assert numeric_tokens(source, preserve_percent=True) == expected


@pytest.mark.parametrize("line_break", ["\r", "\n", "\r\n", "\v", "\f", "\x85", "\u2028", "\u2029"])
def test_atomic_quantity_sign_never_crosses_a_line(line_break: str) -> None:
    source = f"−{line_break}(95)%"
    assert numeric_tokens(source, preserve_percent=True) == ["95%"]
    validate_grounded_numeric_text("比例95%", source, location="atomic-quantity")
    with pytest.raises(ValueError, match="uncited storyboard number -95"):
        validate_grounded_numeric_text("比例-95%", source, location="atomic-quantity")


def test_atomic_quantity_wrappers_have_a_fail_closed_depth_limit() -> None:
    at_limit = "-(" + "(" * 63 + "95" + ")" * 64 + "%"
    assert numeric_tokens(at_limit, preserve_percent=True) == ["-95%"]
    validate_grounded_numeric_text(at_limit, "比例-95%", location="atomic-quantity")
    over_limit = "-(" + "(" * 64 + "95" + ")" * 65 + "%"
    with pytest.raises(ValueError, match="unsupported numeric notation nesting"):
        validate_grounded_numeric_text(over_limit, "比例95%", location="atomic-quantity")


_UNARY_SIGN_CHAIN_QUANTITY_CASES = [
    ("原报数95万元", "原报数为--(95)万元", True),
    ("原报数95万元", "原报数为--95万元", True),
    ("原报数95万元", "原报数为-(-(+95))万元", True),
    ("原报数95万元", "原报数为−+（−＋95）万元", True),
    ("原报数-95万元", "原报数为-(+95)万元", True),
    ("原报数-95万元", "原报数为-+(95)万元", True),
    ("原报数-95万元", "原报数为-+95万元", True),
    ("原报数-95万元", "原报数为--(-95)万元", True),
    ("原报数-95万元", "原报数为−＋（＋95）万元", True),
    ("原报数-95万元", "原报数为＋（−95）万元", True),
    ("原报数95万元", "原报数为-(+95)万元", False),
    ("原报数95万元", "原报数为-+(95)万元", False),
    ("原报数95万元", "原报数为-+95万元", False),
    ("原报数95%", "原报数为--(95)%", True),
    ("原报数-95%", "原报数为-(+95)%", True),
    ("原报数95%", "原报数为-(+95)%", False),
    ("原报数--(95)%", "原报数为95%", True),
    ("原报数--(95)%", "原报数为-95%", False),
    ("原报数-(+95)%", "原报数为-95%", True),
    ("原报数− +\t（＋ 95）万元", "原报数为-95万元", True),
    ("原报数95万元", "- --(95)万元", True),
    ("原报数-95万元", "- -(+95)万元", True),
    ("收入120万元；调整-95万元；贡献215万元", "120万元 --(95万元)=215万元", True),
    ("收入120万元；成本95万元；贡献25万元", "120万元 -+(95万元)=25万元", True),
    ("收入120万元；成本95万元；贡献-25万元", "-(120万元 -+(95万元))=-25万元", True),
    ("收入120万元；调整-95万元；贡献215万元", "120万元 - -(95万元)=215万元", True),
    ("收入120万元；成本95万元；贡献25万元", "120万元 -(+95万元)=25万元", True),
    ("原报数95万元", "- +(95)万元", True),
]


@pytest.mark.parametrize(("source", "authored", "accepted"), _UNARY_SIGN_CHAIN_QUANTITY_CASES)
def test_grounding_preserves_complete_unary_sign_chain_on_one_quantity(
    source: str, authored: str, accepted: bool,
) -> None:
    if accepted:
        validate_grounded_numeric_text(authored, source, location="unary-chain")
        validate_grounded_numeric_text(source, authored, location="unary-chain")
    else:
        with pytest.raises(ValueError, match="uncited storyboard number"):
            validate_grounded_numeric_text(authored, source, location="unary-chain")


@pytest.mark.parametrize(("source", "authored", "accepted"), _UNARY_SIGN_CHAIN_QUANTITY_CASES)
def test_page_author_preserves_complete_unary_sign_chain_on_one_quantity(
    source: str, authored: str, accepted: bool,
) -> None:
    planned, slide = _case()
    planned.content_blocks.append(authored)
    body = next(
        obj for obj in slide.objects
        if isinstance(obj, TextObject) and obj.role == "body"
    )
    body.text += f"\n{authored}"
    source = f"甲为42万元；乙为38万元\n{source}"
    if accepted:
        validate_authored_page(slide, planned, slide, source_text=source)
    else:
        with pytest.raises(ValueError, match="unsupported number"):
            validate_authored_page(slide, planned, slide, source_text=source)


@pytest.mark.parametrize(("notation", "expected"), [
    ("--95万元", "95"), ("--(95)万元", "95"),
    ("-(+95)万元", "-95"), ("-+(95)万元", "-95"),
    ("-+95万元", "-95"), ("-(-(+95))万元", "95"),
    ("−+（−＋95）万元", "95"), ("−＋（＋95）万元", "-95"),
    ("--(95)%", "95%"), ("-(+95)%", "-95%"),
    ("− +\t（＋ 95）万元", "-95"), ("＋（−95）万元", "-95"),
])
def test_numeric_retry_tokens_preserve_complete_unary_sign_chain(
    notation: str, expected: str,
) -> None:
    assert numeric_tokens(f"原报数{notation}", preserve_percent=True) == [expected]


@pytest.mark.parametrize("line_break", ["\r", "\n", "\r\n", "\v", "\f", "\x85", "\u2028", "\u2029"])
def test_unary_sign_chain_never_crosses_a_line(line_break: str) -> None:
    source = f"−{line_break}+(95)%"
    assert numeric_tokens(source, preserve_percent=True) == ["95%"]
    validate_grounded_numeric_text("比例95%", source, location="unary-chain")
    with pytest.raises(ValueError, match="uncited storyboard number -95"):
        validate_grounded_numeric_text("比例-95%", source, location="unary-chain")


@pytest.mark.parametrize("quantity", ["95%", "(95)%"])
def test_unary_sign_chain_has_a_fail_closed_length_limit(quantity: str) -> None:
    at_limit = "原报数" + "-" * 64 + quantity
    assert numeric_tokens(at_limit, preserve_percent=True) == ["95%"]
    validate_grounded_numeric_text(at_limit, "比例95%", location="unary-chain")
    over_limit = "原报数" + "-" * 65 + quantity
    with pytest.raises(ValueError, match="unsupported numeric notation sign chain"):
        validate_grounded_numeric_text(over_limit, "比例95%", location="unary-chain")


@pytest.mark.parametrize("authored", [
    "120 --95",
    "120万元 --95万元",
    "(120万元) --(95万元)",
    "(120) - -95",
    "120 --95 =",
    "120 --95 =215 +",
    "120 --95 =(215",
    "120万元 − −（95万元）＝215万元＋",
    "120 + - + -95 =",
    "(120) -(+(-95)) =",
    "-(95万元元)",
    "−（95万元元）",
    "-(95万元万元)",
    "-(17%%)",
    "-(17％%)",
    "-(17%％)",
    "-(95万元 元)",
    "-(+(17%%))",
    "(95万元元)",
    "-(95",
    "-（95)",
    "--(95",
    "-+(95",
    "−（95万元",
    "-(17%",
    "-(95万元）",
    "-((95)",
    "120--95",
    "120-95 =",
    "120-95 =215 +",
    "(120-95",
    "120-95)",
    "120-95 +",
    "120-95-",
    "120-95=(215",
    "-(95人次次)",
    "(95人天天)",
    "-(95人月月)",
    "-(95人年年)",
    "-(95人次)人次",
    "(95人天)天",
    "120-95 +(17)",
    "120-95 =(215",
    "120--95(17)",
    "120-95-17",
    "(120-95 +(17))",
    "(120-95))",
    "((120-95)",
    "120-95万元万元",
    "(120-95万元)万元",
    "(120-95)万元万元",
    "(120-95)%％",
    "120-95 =215 + (17",
])
def test_grounding_rejects_unproven_or_malformed_quantity_notation(authored: str) -> None:
    with pytest.raises(ValueError, match="unsupported numeric notation"):
        validate_grounded_numeric_text(
            authored, "收入120万元；成本95万元；贡献215万元；比例17%",
            location="malformed-quantity",
        )


@pytest.mark.parametrize("authored", [
    "120 --95",
    "120万元 --95万元",
    "(120万元) --(95万元)",
    "(120) - -95",
    "120 --95 =",
    "120 --95 =215 +",
    "120 --95 =(215",
    "120万元 − −（95万元）＝215万元＋",
    "120 + - + -95 =",
    "(120) -(+(-95)) =",
    "-(95万元元)",
    "−（95万元元）",
    "-(95万元万元)",
    "-(17%%)",
    "-(17％%)",
    "-(17%％)",
    "-(95万元 元)",
    "-(+(17%%))",
    "(95万元元)",
    "-(95",
    "-（95)",
    "--(95",
    "-+(95",
    "−（95万元",
    "-(17%",
    "-(95万元）",
    "-((95)",
    "120--95",
    "120-95 =",
    "120-95 =215 +",
    "(120-95",
    "120-95)",
    "120-95 +",
    "120-95-",
    "120-95=(215",
    "-(95人次次)",
    "(95人天天)",
    "-(95人月月)",
    "-(95人年年)",
    "-(95人次)人次",
    "(95人天)天",
    "120-95 +(17)",
    "120-95 =(215",
    "120--95(17)",
    "120-95-17",
    "(120-95 +(17))",
    "(120-95))",
    "((120-95)",
    "120-95万元万元",
    "(120-95万元)万元",
    "(120-95)万元万元",
    "(120-95)%％",
    "120-95 =215 + (17",
])
def test_page_author_rejects_unproven_or_malformed_quantity_notation(authored: str) -> None:
    planned, slide = _case()
    planned.content_blocks.append(authored)
    body = next(
        obj for obj in slide.objects
        if isinstance(obj, TextObject) and obj.role == "body"
    )
    body.text += f"\n{authored}"
    with pytest.raises(ValueError, match="unsupported numeric notation"):
        validate_authored_page(
            slide, planned, slide,
            source_text="甲为42万元；乙为38万元；收入120万元；成本95万元；贡献215万元；比例17%",
        )


@pytest.mark.parametrize("authored", [
    "120 --95",
    "120万元 --95万元",
    "(120万元) --(95万元)",
    "(120) - -95",
    "120 --95 =",
    "120 --95 =215 +",
    "120 --95 =(215",
    "120万元 − −（95万元）＝215万元＋",
    "120 + - + -95 =",
    "(120) -(+(-95)) =",
    "-(95万元元)",
    "−（95万元元）",
    "-(95万元万元)",
    "-(17%%)",
    "-(17％%)",
    "-(17%％)",
    "-(95万元 元)",
    "-(+(17%%))",
    "(95万元元)",
    "-(95",
    "-（95)",
    "--(95",
    "-+(95",
    "−（95万元",
    "-(17%",
    "-(95万元）",
    "-((95)",
    "120--95",
    "120-95 =",
    "120-95 =215 +",
    "(120-95",
    "120-95)",
    "120-95 +",
    "120-95-",
    "120-95=(215",
    "-(95人次次)",
    "(95人天天)",
    "-(95人月月)",
    "-(95人年年)",
    "-(95人次)人次",
    "(95人天)天",
    "120-95 +(17)",
    "120-95 =(215",
    "120--95(17)",
    "120-95-17",
    "(120-95 +(17))",
    "(120-95))",
    "((120-95)",
    "120-95万元万元",
    "(120-95万元)万元",
    "(120-95)万元万元",
    "(120-95)%％",
    "120-95 =215 + (17",
])
def test_numeric_retry_tokens_reject_unproven_or_malformed_quantity_notation(
    authored: str,
) -> None:
    with pytest.raises(ValueError, match="unsupported numeric notation"):
        numeric_tokens(authored, preserve_percent=True)



@pytest.mark.parametrize("equation", [
    "120万元 -95万元 ×(2 = -70万元",
    "(120万元 -95万元)) =25万元",
    "120万元 -95万元 - =25万元",
    "120万元 -95万元 ×() =25万元",
    "120万元 -95万元 = (25万元",
    "120万元 -95万元 =25万元 +",
    "120万元 -95万元 +=25万元",
    "120万元 -95万元 ×2合计= -70万元",
    "(120 -95)万元万元 =25万元",
    "(120 -95)%％ =25%",
    "(120 -95)万元2 =25万元",
    "120万元万元 -95万元 =25万元",
    "(120 -95)万元(5) =25万元",
    "(120 -95)万元 + =25万元",
])
def test_grounding_requires_well_formed_whole_equation(equation: str) -> None:
    validate_grounded_numeric_text(
        "成本为-95万元。", equation, location="malformed-equation-source",
    )
    with pytest.raises(ValueError, match="uncited storyboard number -95"):
        validate_grounded_numeric_text(
            equation, "收入120万元；成本95万元；系数2；贡献-70万元；贡献25万元。",
            location="malformed-equation-claim",
        )


_COMPOUND_UNIT_AND_RANGE_CASES = [
    *[
        (source.format(unit=unit), authored.format(unit=unit), expected)
        for unit in ("人次", "人天", "人月", "人年")
        for source, authored, expected in (
            ("累计95{unit}", "累计(95{unit})", ["95"]),
            ("累计(95{unit})", "累计95{unit}", ["95"]),
            ("调整-95{unit}", "调整-(95{unit})", ["-95"]),
            ("调整-(95{unit})", "调整-95{unit}", ["-95"]),
        )
    ],
    *[
        (source.format(sign=sign), authored.format(sign=sign), ["2025", "2026"])
        for sign in ("-", "−", "－", "﹣")
        for source, authored in (
            ("区间2025年到2026年", "区间2025{sign}2026年"),
            ("区间2025{sign}2026年", "区间2025年到2026年"),
            ("区间2025年到2026年", "区间(2025{sign}2026年)"),
            ("区间(2025{sign}2026年)", "区间2025年到2026年"),
        )
    ],
    ("区间5.0到6.5", "区间5.0-6.5", ["5.0", "6.5"]),
    ("区间5.0-6.5", "区间5.0到6.5", ["5.0", "6.5"]),
    ("区间2025年到2026年", "区间2025–2026年", ["2025", "2026"]),
    ("区间2025年到2026年", "区间2025—2026年", ["2025", "2026"]),
]


@pytest.mark.parametrize(("source", "authored", "expected"), _COMPOUND_UNIT_AND_RANGE_CASES)
def test_grounding_preserves_compound_units_and_compact_ranges(
    source: str, authored: str, expected: list[str],
) -> None:
    validate_grounded_numeric_text(authored, source, location="compound-unit-range")
    validate_grounded_numeric_text(source, authored, location="compound-unit-range")


@pytest.mark.parametrize(("source", "authored", "expected"), _COMPOUND_UNIT_AND_RANGE_CASES)
def test_page_author_preserves_compound_units_and_compact_ranges(
    source: str, authored: str, expected: list[str],
) -> None:
    planned, slide = _case()
    planned.content_blocks.append(authored)
    body = next(
        obj for obj in slide.objects
        if isinstance(obj, TextObject) and obj.role == "body"
    )
    body.text += f"\n{authored}"
    assert set(expected) <= set(page_allowed_numeric_tokens(planned))
    validate_authored_page(
        slide, planned, slide, source_text=f"甲为42万元；乙为38万元\n{source}",
    )


@pytest.mark.parametrize(("source", "authored", "expected"), _COMPOUND_UNIT_AND_RANGE_CASES)
def test_numeric_retry_tokens_preserve_compound_units_and_compact_ranges(
    source: str, authored: str, expected: list[str],
) -> None:
    assert numeric_tokens(source, preserve_percent=True) == expected
    assert numeric_tokens(authored, preserve_percent=True) == expected


_COMPACT_RANGE_ADJACENCY_CASES = [
    (left, right, expected)
    for sign in ("-", "−", "－", "﹣")
    for source, authored, expected in (
        ("区间2025年到2026年，人数95人", "区间2025{sign}2026年(95人)", ["2025", "2026", "95"]),
        ("区间2025年到2026年，人数95人", "区间2025{sign}2026年（共95人）", ["2025", "2026", "95"]),
        ("区间2025年到2026年、2027年到2028年", "区间2025{sign}2026年 2027{sign}2028年", ["2025", "2026", "2027", "2028"]),
        ("区间2025年到2026年", "区间2025年{sign}2026年", ["2025", "2026"]),
        ("区间5元到17元", "区间5元{sign}17元", ["5", "17"]),
        ("区间2025年到2026年，人数95人", "区间(2025{sign}2026年)(95人)", ["2025", "2026", "95"]),
        ("区间2025年到2026年，人数95人", "区间2025{sign}2026年 95人", ["2025", "2026", "95"]),
        ("人数95人，区间2025年到2026年", "(95人)2025{sign}2026年", ["95", "2025", "2026"]),
        ("区间2025年到2026年，人数95人", "区间(2025{sign}2026年（共95人）)", ["2025", "2026", "95"]),
        ("区间2025年到2026年，人数95人", "区间(2025{sign}2026)年(95人)", ["2025", "2026", "95"]),
    )
    for left, right in (
        (source, authored.format(sign=sign)),
        (authored.format(sign=sign), source),
    )
]


@pytest.mark.parametrize(("source", "authored", "expected"), _COMPACT_RANGE_ADJACENCY_CASES)
def test_grounding_proves_compact_range_atoms_with_adjacent_content(
    source: str, authored: str, expected: list[str],
) -> None:
    validate_grounded_numeric_text(authored, source, location="range-atom-adjacency")
    validate_grounded_numeric_text(source, authored, location="range-atom-adjacency")


@pytest.mark.parametrize(("source", "authored", "expected"), _COMPACT_RANGE_ADJACENCY_CASES)
def test_page_author_proves_compact_range_atoms_with_adjacent_content(
    source: str, authored: str, expected: list[str],
) -> None:
    planned, slide = _case()
    planned.content_blocks.append(authored)
    body = next(
        obj for obj in slide.objects
        if isinstance(obj, TextObject) and obj.role == "body"
    )
    body.text += f"\n{authored}"
    assert set(expected) <= set(page_allowed_numeric_tokens(planned))
    validate_authored_page(
        slide, planned, slide, source_text=f"甲为42万元；乙为38万元\n{source}",
    )


@pytest.mark.parametrize(("source", "authored", "expected"), _COMPACT_RANGE_ADJACENCY_CASES)
def test_numeric_retry_tokens_prove_compact_range_atoms_with_adjacent_content(
    source: str, authored: str, expected: list[str],
) -> None:
    assert numeric_tokens(source, preserve_percent=True) == expected
    assert numeric_tokens(authored, preserve_percent=True) == expected


_COMPACT_RANGE_WRAPPED_LIST_CASES = [
    (left, right, expected)
    for sign in ("-", "−", "－", "﹣")
    for source, authored, expected in (
        ("区间2025年到2026年", "- 2025{sign}2026年", ["2025", "2026"]),
        ("区间2025年到2026年", "* 2025{sign}2026年", ["2025", "2026"]),
        ("区间2025年到2026年", "  +\t2025{sign}2026年", ["2025", "2026"]),
        ("区间5到17", "区间(5){sign}(17)", ["5", "17"]),
        ("区间5元到17元", "区间(5元){sign}(17元)", ["5", "17"]),
        ("区间5元到17元", "区间5元{sign}(17元)", ["5", "17"]),
        ("区间5元到17元", "区间(5元){sign}17元", ["5", "17"]),
        ("区间5元到17元", "区间（5元）{sign}（17元）", ["5", "17"]),
        ("区间5元到17元", "区间((5)元){sign}((17)元)", ["5", "17"]),
        ("区间5元到17元", "区间((5元)){sign}((17元))", ["5", "17"]),
    )
    for left, right in (
        (source, authored.format(sign=sign)),
        (authored.format(sign=sign), source),
    )
]


@pytest.mark.parametrize(("source", "authored", "expected"), _COMPACT_RANGE_WRAPPED_LIST_CASES)
def test_grounding_proves_wrapped_and_list_range_quantities(
    source: str, authored: str, expected: list[str],
) -> None:
    validate_grounded_numeric_text(authored, source, location="wrapped-list-range")
    validate_grounded_numeric_text(source, authored, location="wrapped-list-range")


@pytest.mark.parametrize(("source", "authored", "expected"), _COMPACT_RANGE_WRAPPED_LIST_CASES)
def test_page_author_proves_wrapped_and_list_range_quantities(
    source: str, authored: str, expected: list[str],
) -> None:
    planned, slide = _case()
    planned.content_blocks.append(authored)
    body = next(obj for obj in slide.objects
                if isinstance(obj, TextObject) and obj.role == "body")
    body.text += f"\n{authored}"
    assert set(expected) <= set(page_allowed_numeric_tokens(planned))
    validate_authored_page(
        slide, planned, slide, source_text=f"甲为42万元；乙为38万元\n{source}",
    )


@pytest.mark.parametrize(("source", "authored", "expected"), _COMPACT_RANGE_WRAPPED_LIST_CASES)
def test_numeric_retry_tokens_prove_wrapped_and_list_range_quantities(
    source: str, authored: str, expected: list[str],
) -> None:
    assert numeric_tokens(source, preserve_percent=True) == expected
    assert numeric_tokens(authored, preserve_percent=True) == expected


@pytest.mark.parametrize("sign", ["-", "−", "－", "﹣"])
@pytest.mark.parametrize("notation", ["(5{sign}17)%", "（5{sign}17）％", "5{sign}17%"])
def test_compact_range_quantity_keeps_shared_percent_dimension(sign: str, notation: str) -> None:
    source = f"比例{notation.format(sign=sign)}"
    authored = "比例5%到17%"
    suffix = "％" if source.endswith("％") else "%"
    assert numeric_tokens(source, preserve_percent=True) == [f"5{suffix}", f"17{suffix}"]
    validate_grounded_numeric_text(authored, source, location="range-percent")
    validate_grounded_numeric_text(source, authored, location="range-percent")
    planned, slide = _case()
    planned.content_blocks.append(authored)
    body = next(obj for obj in slide.objects
                if isinstance(obj, TextObject) and obj.role == "body")
    body.text += f"\n{authored}"
    validate_authored_page(
        slide, planned, slide, source_text=f"甲为42万元；乙为38万元\n{source}",
    )
    for invented in ("比例1700%", "比例0.17%", "比例500%", "比例0.05%"):
        with pytest.raises(ValueError, match="uncited storyboard number"):
            validate_grounded_numeric_text(invented, source, location="range-percent-rescaling")


_ANNOTATED_EQUATION_BOUNDARY_CASES = [
    (left, right)
    for minus in ("-", "−", "－", "﹣")
    for source, authored in (
        ("120万元{minus}95万元=25万元", "120万元{minus}95万元=25万元(结余)"),
        ("120万元{minus}95万元＝25万元", "120万元{minus}95万元＝25万元（结余）"),
        ("120万元{minus}95万元=25万元", "120万元{minus}95万元=25万元 \t(结余)"),
        ("120万元{minus}95万元=25万元", "(起点)120万元{minus}95万元=25万元"),
        ("120万元{minus}95万元=25万元；人数17人", "120万元{minus}95万元=25万元(17人)"),
        ("120万元{minus}95万元=25万元；人数17人", "(17人)120万元{minus}95万元=25万元"),
    )
    for left, right in (
        (source.format(minus=minus), authored.format(minus=minus)),
        (authored.format(minus=minus), source.format(minus=minus)),
    )
]


@pytest.mark.parametrize(("source", "authored"), _ANNOTATED_EQUATION_BOUNDARY_CASES)
def test_grounding_keeps_complete_equation_annotation_boundaries(source, authored):
    validate_grounded_numeric_text(authored, source, location="equation-annotation")
    validate_grounded_numeric_text(source, authored, location="equation-annotation")
    for text in (source, authored):
        with pytest.raises(ValueError, match="uncited storyboard number -95"):
            validate_grounded_numeric_text("成本为-95万元", text, location="invented-sign")


@pytest.mark.parametrize(("source", "authored"), _ANNOTATED_EQUATION_BOUNDARY_CASES)
def test_page_author_keeps_complete_equation_annotation_boundaries(source, authored):
    planned, slide = _case()
    planned.content_blocks.append(authored)
    body = next(obj for obj in slide.objects
                if isinstance(obj, TextObject) and obj.role == "body")
    body.text += f"\n{authored}"
    assert {"120", "95", "25"} <= set(page_allowed_numeric_tokens(planned))
    validate_authored_page(
        slide, planned, slide, source_text=f"甲为42万元；乙为38万元\n{source}",
    )


@pytest.mark.parametrize(("source", "authored"), _ANNOTATED_EQUATION_BOUNDARY_CASES)
def test_numeric_retry_keeps_complete_equation_annotation_boundaries(source, authored):
    # Guidance keeps original binary-minus spelling, not a new negative fact.
    assert set(numeric_tokens(source, preserve_percent=True)) == set(
        numeric_tokens(authored, preserve_percent=True),
    )
    assert "-95" in numeric_tokens(authored, preserve_percent=True)


_STANDARD_WEIGHT_RANGE_CASES = [
    (left, right, [low, high])
    for unit in ("公斤", "千克", "克")
    for low, high in (("5", "17"), ("5.0", "17.5"))
    for source, authored in (
        (f"区间{low}{unit}到{high}{unit}", f"区间({low}{unit})-({high}{unit})"),
        (f"区间{low}{unit}到{high}{unit}", f"区间（{low}{unit}）−（{high}{unit}）"),
        (f"区间{low}{unit}到{high}{unit}", f"区间(({low}){unit})－(({high}){unit})"),
    )
    for left, right in ((source, authored), (authored, source))
]


@pytest.mark.parametrize(("source", "authored", "expected"), _STANDARD_WEIGHT_RANGE_CASES)
def test_grounding_keeps_standard_weight_range_quantities(source, authored, expected):
    validate_grounded_numeric_text(authored, source, location="weight-range")
    validate_grounded_numeric_text(source, authored, location="weight-range")
    with pytest.raises(ValueError, match="uncited storyboard number"):
        validate_grounded_numeric_text(f"重量-{expected[1]}公斤", source, location="invented-sign")


@pytest.mark.parametrize(("source", "authored", "expected"), _STANDARD_WEIGHT_RANGE_CASES)
def test_page_author_keeps_standard_weight_range_quantities(source, authored, expected):
    planned, slide = _case()
    planned.content_blocks.append(authored)
    body = next(obj for obj in slide.objects
                if isinstance(obj, TextObject) and obj.role == "body")
    body.text += f"\n{authored}"
    assert set(expected) <= set(page_allowed_numeric_tokens(planned))
    validate_authored_page(
        slide, planned, slide, source_text=f"甲为42万元；乙为38万元\n{source}",
    )


@pytest.mark.parametrize(("source", "authored", "expected"), _STANDARD_WEIGHT_RANGE_CASES)
def test_numeric_retry_keeps_standard_weight_range_quantities(source, authored, expected):
    assert numeric_tokens(source, preserve_percent=True) == expected
    assert numeric_tokens(authored, preserve_percent=True) == expected


@pytest.mark.parametrize("malformed", [
    "120万元-95万元=25万元(17人)+",
    "120万元-95万元=25万元(17人)=",
    "120万元-95万元=25万元(结余",
    "(起点）120万元-95万元=25万元",
    "120万元-95万元=(25万元万元)",
    "120万元-95万元=25万元(17人)-",
    "120万元-95万元=25万元(17人+)",
    "120万元-95万元=25万元((17人+))",
    "120万元-95万元=25万元(17人*)",
    "120万元-95万元=25万元(17人/)",
    "120万元-95万元=25万元(17人=)",
    "120万元-95万元=25万元()",
])
def test_equation_annotation_boundaries_do_not_authorize_incomplete_math(malformed):
    with pytest.raises(ValueError, match="uncited storyboard number -95"):
        validate_grounded_numeric_text(
            malformed, "收入120万元；成本95万元；贡献25万元；人数17人",
            location="malformed-annotated-equation",
        )


def test_equation_numeric_annotation_keeps_repeated_unit_rejection():
    malformed = "120万元-95万元=25万元(17人次次)"
    with pytest.raises(ValueError, match="uncited storyboard number -95"):
        validate_grounded_numeric_text(
            malformed,
            "收入120万元；成本95万元；贡献25万元；人数17人",
            location="repeated-unit-annotation",
        )
    with pytest.raises(ValueError, match="unsupported numeric notation malformed quantity wrapper"):
        numeric_tokens(malformed, preserve_percent=True)


@pytest.mark.parametrize("unit", ["公斤", "千克", "克"])
def test_weight_range_quantities_keep_repeated_unit_and_unary_sign_guards(unit):
    with pytest.raises(ValueError, match="unsupported numeric notation"):
        numeric_tokens(f"区间(5{unit})-(17{unit}{unit})", preserve_percent=True)
    with pytest.raises(ValueError, match="uncited storyboard number -17"):
        validate_grounded_numeric_text(
            f"调整-(17{unit})", f"重量17{unit}", location="invented-weight-sign",
        )


_GENERIC_QUANTITY_RANGE_CASES = [
    (left, right, [low, high])
    for unit in ("吨", "米", "件", "台", "份", "小时", "未知后缀", "kg")
    for low, high in (("5", "17"), ("5.0", "17.5"))
    for source, authored in (
        (f"区间{low}{unit}到{high}{unit}", f"区间({low}{unit})-({high}{unit})"),
        (f"区间{low}{unit}到{high}{unit}", f"区间（{low}{unit}）−（{high}{unit}）"),
        (f"区间{low}{unit}到{high}{unit}", f"区间(({low}){unit})－(({high}){unit})"),
    )
    for left, right in ((source, authored), (authored, source))
]


@pytest.mark.parametrize(("source", "authored", "expected"), _GENERIC_QUANTITY_RANGE_CASES)
def test_grounding_keeps_generic_single_quantity_range_suffixes(source, authored, expected):
    validate_grounded_numeric_text(authored, source, location="generic-quantity-range")
    validate_grounded_numeric_text(source, authored, location="generic-quantity-range")
    with pytest.raises(ValueError, match="uncited storyboard number"):
        validate_grounded_numeric_text(f"调整-{expected[1]}", source, location="invented-negative")


@pytest.mark.parametrize(("source", "authored", "expected"), _GENERIC_QUANTITY_RANGE_CASES)
def test_page_author_keeps_generic_single_quantity_range_suffixes(source, authored, expected):
    planned, slide = _case()
    planned.content_blocks.append(authored)
    body = next(obj for obj in slide.objects
                if isinstance(obj, TextObject) and obj.role == "body")
    body.text += f"\n{authored}"
    assert set(expected) <= set(page_allowed_numeric_tokens(planned))
    validate_authored_page(slide, planned, slide,
                           source_text=f"甲为42万元；乙为38万元\n{source}")


@pytest.mark.parametrize(("source", "authored", "expected"), _GENERIC_QUANTITY_RANGE_CASES)
def test_numeric_retry_keeps_generic_single_quantity_range_suffixes(source, authored, expected):
    assert numeric_tokens(source, preserve_percent=True) == expected
    assert numeric_tokens(authored, preserve_percent=True) == expected


@pytest.mark.parametrize("unit", ["吨", "米", "件", "台", "份", "小时", "未知后缀", "kg"])
def test_generic_quantity_suffix_keeps_unary_signs_without_proving_equations(unit):
    source = f"调整-17{unit}"
    authored = f"调整-(17{unit})"
    assert numeric_tokens(authored, preserve_percent=True) == ["-17"]
    validate_grounded_numeric_text(authored, source, location="generic-signed-quantity")
    validate_grounded_numeric_text(source, authored, location="generic-signed-quantity")
    with pytest.raises(ValueError, match="uncited storyboard number -17"):
        validate_grounded_numeric_text(authored, f"调整17{unit}", location="generic-invented-sign")
    # An ASCII suffix can leave the minus outside the numeric regex match;
    # both paths must reject the unsupported equation, not infer subtraction.
    with pytest.raises(
        ValueError,
        match="uncited storyboard number -95|unsupported numeric notation unproven operator boundary",
    ):
        validate_grounded_numeric_text(
            f"120{unit}-95{unit}=25{unit}", "收入120；成本95；贡献25",
            location="generic-suffix-not-an-equation-unit",
        )


@pytest.mark.parametrize("suffix", [
    "年的增长率为", "人的变化率为", "未知后缀的变化率为", "kg的变化率为",
])
@pytest.mark.parametrize("minus", ["-", "−", "－", "﹣"])
def test_opaque_suffix_cannot_turn_prose_and_negative_rate_into_a_range(suffix, minus):
    source = "年份2025；变化率-17%"
    authored = f"2025{suffix}{minus}17%"
    assert numeric_tokens(authored, preserve_percent=True) == ["2025", "-17%"]
    validate_grounded_numeric_text(authored, source, location="negative-rate-prose")
    validate_grounded_numeric_text(source, authored, location="negative-rate-prose")
    with pytest.raises(ValueError, match="uncited storyboard number"):
        validate_grounded_numeric_text(
            authored, "年份2025；变化率17%", location="negative-rate-prose-sign",
        )
    planned, slide = _case()
    planned.content_blocks.append(authored)
    body = next(obj for obj in slide.objects
                if isinstance(obj, TextObject) and obj.role == "body")
    body.text += f"\n{authored}"
    assert "-17" in page_allowed_numeric_tokens(planned)
    validate_authored_page(
        slide, planned, slide, source_text=f"甲为42万元；乙为38万元\n{source}",
    )


@pytest.mark.parametrize("malformed", [
    "120万元-95万元理由=25万元",
    "120万元-95万元理由＝25万元",
    "120万元-95万元理由 =25万元",
    "120万元-95万元理由+5万元=30万元",
])
def test_prose_between_quantity_and_equality_cannot_prove_binary_operators(malformed):
    with pytest.raises(ValueError, match="uncited storyboard number -95"):
        validate_grounded_numeric_text(
            malformed, "收入120万元；成本95万元；贡献25万元；费用5万元；贡献30万元",
            location="prose-interrupted-equation",
        )


@pytest.mark.parametrize("suffix", [
    " -5万元", " -5万元 = 合计", "；-5万元 = 20万元", " -负值5万元 = 20万元",
    "\n-5万元 = 20万元", "\r-5万元 = 20万元", "\v-5万元 = 20万元",
    "\f-5万元 = 20万元", "\x85-5万元 = 20万元", "\u2028-5万元 = 20万元",
    "\u2029-5万元 = 20万元",
])
def test_arithmetic_chain_requires_unbroken_same_line_numeric_equation(suffix: str) -> None:
    text = f"120万元 -95万元{suffix}"

    validate_grounded_numeric_text(
        "成本为-95万元", text, location="signed-value-outside-equation",
    )
    with pytest.raises(ValueError, match="uncited storyboard number -95"):
        validate_grounded_numeric_text(
            text, "收入120万元；成本95万元；费用5万元；贡献20万元", location="not-an-equation",
        )


@pytest.mark.parametrize("line_break", ["\n", "\r", "\r\n", "\v", "\f", "\x85", "\u2028", "\u2029"])
def test_grounding_does_not_join_list_markers_or_cross_line_signs(line_break: str) -> None:
    source = line_break.join(["- 7项", "  - 17项", "单独标记为-", "23项"])

    assert numeric_tokens(source) == ["7", "17", "23"]
    validate_grounded_numeric_text("7项；17项；23项", source, location="list")
    for value in (7, 17, 23):
        with pytest.raises(ValueError, match=f"uncited storyboard number -{value}"):
            validate_grounded_numeric_text(
                f"原报数为-{value}", source, location="not-a-negative",
            )


@pytest.mark.parametrize("line_break", ["\n", "\r", "\r\n", "\v", "\f", "\x85", "\u2028", "\u2029"])
@pytest.mark.parametrize("minus", ["-", "− ", "－\t", "﹣\u00a0"])
def test_grounding_keeps_cross_line_negative_facts_signed(line_break: str, minus: str) -> None:
    source = line_break.join(["7万元", f"{minus}17%", "42%", f"{minus}38万元"])
    authored = "计数为7万元；比例为-17%；占比为42%；金额为-38万元。"

    assert numeric_tokens(source, preserve_percent=True) == ["7", "-17%", "42%", "-38"]
    validate_grounded_numeric_text(authored, source, location="cross-line-negative")
    validate_grounded_numeric_text(source, authored, location="cross-line-negative")
    for invented in ("比例为-18%。", "金额为-39万元。"):
        with pytest.raises(ValueError, match="uncited storyboard number"):
            validate_grounded_numeric_text(invented, source, location="invented-negative")


@pytest.mark.parametrize("line_break", ["\n", "\r", "\r\n", "\v", "\f", "\x85", "\u2028", "\u2029"])
def test_page_author_keeps_cross_line_negative_source_and_plan_signed(line_break: str) -> None:
    planned, slide = _case()
    planned.content_blocks.append(line_break.join(["7", "−17%", "比例为-17%"]))
    body = next(
        obj for obj in slide.objects
        if isinstance(obj, TextObject) and obj.role == "body"
    )
    body.text += "；比例为-17%"

    assert "-17" in page_allowed_numeric_tokens(planned)
    validate_authored_page(
        slide, planned, slide,
        source_text=line_break.join(["甲为42万元；乙为38万元。", "7", "−17%"]),
    )


@pytest.mark.parametrize("minus", ["-", "−", "－", "﹣"])
@pytest.mark.parametrize("prefix", ["年份\t增长率\n2025\t", "2025年 ", "2025 ", "2025\u00a0"])
def test_grounding_preserves_adjacent_negative_table_and_year_values(minus: str, prefix: str) -> None:
    source = f"{prefix}{minus}17%"
    authored = "2025年的增长率为-17%。"

    assert numeric_tokens(source, preserve_percent=True) == ["2025", "-17%"]
    validate_grounded_numeric_text(authored, source, location="negative-table")
    validate_grounded_numeric_text(source, authored, location="negative-table")
    for invented in ("2025年的增长率为-18%。", "2025年的增长率为0.17%。"):
        with pytest.raises(ValueError, match="uncited storyboard number"):
            validate_grounded_numeric_text(invented, source, location="invented-table-value")
    with pytest.raises(ValueError, match="uncited storyboard number -17"):
        validate_grounded_numeric_text(source, "2025年的增长率为17%。", location="invented-sign")


@pytest.mark.parametrize("minus", ["-", "−", "－", "﹣"])
@pytest.mark.parametrize("prefix", ["年份\t增长率\n2025\t", "2025年 "])
def test_page_author_preserves_adjacent_negative_table_and_year_values(minus: str, prefix: str) -> None:
    planned, slide = _case()
    planned.content_blocks.append(f"{prefix}{minus}17%")
    body = next(
        obj for obj in slide.objects
        if isinstance(obj, TextObject) and obj.role == "body"
    )
    body.text += "；2025年的增长率为-17%"

    assert "-17" in page_allowed_numeric_tokens(planned)
    validate_authored_page(
        slide, planned, slide,
        source_text=f"甲为42万元；乙为38万元。\n{prefix}{minus}17%",
    )
    with pytest.raises(ValueError, match=r"unsupported number\(s\): -17"):
        validate_authored_page(
            slide, planned, slide, source_text="甲为42万元；乙为38万元；2025年的增长率为17%。",
        )


def test_page_author_uses_the_same_canonical_unary_minus_contract() -> None:
    planned, slide = _case()
    planned.content_blocks.append("净值为-7万元")
    body = next(
        obj for obj in slide.objects
        if isinstance(obj, TextObject) and obj.role == "body"
    )
    body.text += "；净值为- 7万元"

    assert "-7" in page_allowed_numeric_tokens(planned)
    validate_authored_page(
        slide, planned, slide,
        source_text="甲为42万元；乙为38万元；净值为− 7万元",
    )


@pytest.mark.parametrize("source", ["比例为17%。", "比例为17％。", "比例为17 %。"])
@pytest.mark.parametrize("authored", ["比例为0.17%。", "比例为0.17％。", "比例为1700%。"])
def test_grounding_rejects_incorrect_percent_rescaling(source: str, authored: str) -> None:
    with pytest.raises(ValueError, match="uncited storyboard number"):
        validate_grounded_numeric_text(authored, source, location="percent-claim")


@pytest.mark.parametrize(("source", "authored"), [
    ("比例为17%。", "比例为17%。"),
    ("比例为17 %。", "比例为0.17。"),
    ("比例为17%。", "比例为0.17。"),
    ("比例为9.375%。", "比例为9.4%。"),
    ("比例为9.375%。", "比例为0.09375。"),
    ("比例为0.17。", "比例为17%。"),
    ("比例为-17%。", "比例为-0.17。"),
    ("比例为-9.375%。", "比例为-9.4%。"),
])
def test_grounding_preserves_equivalent_percent_notation_and_rounding(
    source: str, authored: str,
) -> None:
    validate_grounded_numeric_text(authored, source, location="percent-claim")


def test_planned_chart_values_must_exist_in_the_cited_source() -> None:
    planned, _slide = _case()
    brief = PresentationBrief(
        objective="解释收入",
        audience="管理层",
        narrative="对比甲乙",
        source_refs=["source-1", "source-2"],
        source_sections=[
            PresentationSourceSection(
                title="甲", body="甲为42万元；占比30%。", source_ref="source-1",
            ),
            PresentationSourceSection(
                title="乙", body="乙为38万元。", source_ref="source-2",
            ),
        ],
    )
    chart = planned.charts[0]
    with pytest.raises(ValueError, match="uncited chart value"):
        validate_planned_chart_sources([planned], brief)

    planned.charts[0] = chart.model_copy(
        update={"source_refs": ["source-1", "source-2"]},
    )
    validate_planned_chart_sources([planned], brief)

    planned.charts[0] = chart.model_copy(
        update={"source_refs": ["source-1", "source-2"], "series": {"收入": [42, 99]}},
    )
    with pytest.raises(ValueError, match="uncited chart value") as exc_info:
        validate_planned_chart_sources([planned], brief)
    assert exc_info.value.presentation_numeric_token == "99"
    assert exc_info.value.presentation_numeric_location == "slide-001"

    planned.charts[0] = chart.model_copy(
        update={"source_refs": ["source-1"], "series": {"占比": [0.3, 30]}},
    )
    validate_planned_chart_sources([planned], brief)

    merged = brief.model_copy(update={"source_sections": [
        PresentationSourceSection(
            title="合并段落", body="甲为42万元；乙为38万元。",
            source_ref="source-1", source_refs=["source-1", "source-2"],
        ),
    ]})
    planned.charts[0] = chart.model_copy(update={"series": {"收入": [42, 38]}})
    with pytest.raises(ValueError, match="uncited chart value"):
        validate_planned_chart_sources([planned], merged)
    planned.charts[0] = chart.model_copy(
        update={"source_refs": ["source-1", "source-2"]},
    )
    validate_planned_chart_sources([planned], merged)


def test_model_storyboard_cannot_pass_a_chart_with_invented_values(monkeypatch) -> None:
    request = PresentationJobRequest(
        conversation_id="chart-grounding-test",
        user_request="用原始材料制作收入对比",
        slide_count=1,
        materials=[InputMaterial(
            name="收入.md", content="# 收入\n甲为42万元；乙为38万元。",
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    plan.slides[0].charts = [ChartObject(
        categories=["甲", "乙"],
        series={"收入": [42, 99]},
        unit="万元",
        derivation="声称直接摘录",
        source_refs=list(brief.source_refs),
    )]

    async def fake_utility(**kwargs):
        return StructuredUtilityResult(
            value=_semantic_value_for_call(kwargs, plan),
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )
    with pytest.raises(ValueError, match="uncited chart value 99"):
        UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)


def _source_profit_proof(source_ref):
    return {
        "terms": [
            {"source_ref": source_ref, "source_excerpt": "Q2：收入145万元，成本108万元。",
             "value": "145", "unit": "万元", "coefficient": 1},
            {"source_ref": source_ref, "source_excerpt": "Q2：收入145万元，成本108万元。",
             "value": "108", "unit": "万元", "coefficient": -1},
        ],
        "result": "37", "unit": "万元",
        "authorization_source_ref": source_ref,
        "authorization_excerpt": "利润按收入减成本计算；不得发明税率或净利润。",
    }


@pytest.mark.parametrize("prompt", [
    UtilityPresentationSemanticPlanner.CLAIM_SYSTEM_PROMPT,
    UtilityPresentationSemanticPlanner.GLOBAL_CONTRACT_SYSTEM_PROMPT,
    UtilityPresentationSemanticPlanner.DETAIL_SYSTEM_PROMPT,
    UtilityPresentationPageAuthor.SYSTEM_PROMPT,
], ids=["claims", "global", "detail", "native-author"])
def test_calculation_proof_prompt_contract_survives_every_stage(prompt):
    # A supported schema cannot remain contradicted by a source-values-only
    # prompt. Later authors must preserve, not invent or strip, the proof.
    assert "calculation_proofs" in prompt


@pytest.mark.parametrize("tamper", [
    "wrong-result", "unknown-ref", "fake-excerpt", "fake-authorization",
    "mixed-unit", "missing-input", "invalid-coefficient", "unbound-unit",
])
def test_calculation_proof_recomputes_untrusted_restored_inputs(tamper):
    raw = _source_profit_proof("source-profit")
    source = "Q2：收入145万元，成本108万元。\n利润按收入减成本计算；不得发明税率或净利润。"
    if tamper == "wrong-result":
        raw["result"] = "38"
    elif tamper == "unknown-ref":
        raw["terms"][0]["source_ref"] = "other-source"
    elif tamper == "fake-excerpt":
        raw["terms"][0]["source_excerpt"] = "收入145万元来自虚构业务。"
    elif tamper == "fake-authorization":
        raw["authorization_excerpt"] = "净利润允许随意推算。"
    elif tamper == "mixed-unit":
        raw["terms"][0]["unit"] = "美元"
    elif tamper == "missing-input":
        raw["terms"][0]["value"] = "146"
    elif tamper == "invalid-coefficient":
        raw["terms"][0]["coefficient"] = 2
    elif tamper == "unbound-unit":
        raw["unit"] = "美元"
        for term in raw["terms"]:
            term["unit"] = "美元"
    # model_construct simulates an untrusted in-memory / checkpoint boundary.
    # Verification must revalidate the complete typed proof rather than trust it.
    proof = SourceCalculationProof.model_construct(**raw)
    with pytest.raises(ValueError):
        verified_calculation_values(
            [proof], source_text_for=lambda refs: source,
            allowed_refs=["source-profit"], unit="万元",
        )


def test_calculation_proof_rejects_blank_authorization_excerpt():
    raw = _source_profit_proof("source-profit")
    raw["authorization_excerpt"] = " \n\t "
    proof = SourceCalculationProof.model_validate(raw)
    with pytest.raises(ValueError, match="calculation proof authorization"):
        verified_calculation_values(
            [proof], source_text_for=lambda refs: "Q2：收入145万元，成本108万元。",
            allowed_refs=["source-profit"],
        )


def test_calculation_proof_schema_errors_do_not_echo_untrusted_source():
    raw = _source_profit_proof("source-profit")
    private_text = "PRIVATE_SOURCE_EXCERPT_DO_NOT_ECHO"
    raw["terms"][0]["source_excerpt"] = private_text
    raw["terms"][0]["value"] = private_text
    proof = SourceCalculationProof.model_construct(**raw)
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        with pytest.raises(ValueError) as caught:
            verified_calculation_values(
                [proof], source_text_for=lambda refs: "",
                allowed_refs=["source-profit"],
            )
    assert private_text not in str(caught.value)
    assert captured == []


@pytest.mark.parametrize("unsupported", ["-37万元", "3700万元", "0.37万元"])
def test_verified_calculation_does_not_grant_sign_or_scale_changes(unsupported):
    with pytest.raises(ValueError, match="uncited storyboard number"):
        validate_grounded_numeric_text(
            unsupported, "收入145万元，成本108万元。", location="profit",
            verified_values={Decimal("37")},
        )


@pytest.mark.parametrize("authored", ["经营贡献减少9万元", "经营贡献减少九万元", "经营贡献变动−9万元"])
def test_verified_negative_calculation_supports_only_its_exact_prose_magnitude(authored):
    values = {Decimal("-9")}
    validate_grounded_numeric_text(
        authored, "收入175万元，成本150万元。", location="difference",
        verified_values=values,
    )
    assert values == {Decimal("-9")}, "display normalization must not change the signed proof"


@pytest.mark.parametrize("unsupported", ["减少10万元", "减少900万元", "减少0.09万元", "减少9.1万元", "成本为-175万元"])
def test_negative_calculation_magnitude_does_not_authorize_other_numeric_claims(unsupported):
    with pytest.raises(ValueError, match="uncited storyboard number"):
        validate_grounded_numeric_text(
            unsupported, "收入175万元，成本150万元。", location="difference",
            verified_values={Decimal("-9")},
        )


def test_page_author_cannot_strip_or_change_chart_calculation_proof():
    planned, scene = _case()
    proof = SourceCalculationProof.model_validate(_source_profit_proof("source-profit"))
    planned.intent.source_refs = ["source-profit"]
    planned.charts[0].source_refs = ["source-profit"]
    planned.charts[0].calculation_proofs = [proof]
    scene.objects = [obj.model_copy(deep=True) for obj in scene.objects]
    scene_chart = next(obj for obj in scene.objects if isinstance(obj, ChartObject))
    scene_chart.calculation_proofs = []
    with pytest.raises(ValueError, match="changed or omitted planned chart data"):
        validate_authored_page(scene, planned, scene, source_text="")
    scene_chart.calculation_proofs = [proof.model_copy(update={"result": "38"})]
    with pytest.raises(ValueError, match="changed or omitted planned chart data"):
        validate_authored_page(scene, planned, scene, source_text="")


def test_calculation_proof_retry_describes_contract_without_echoing_private_text():
    error = ValueError("calculation proof result does not match recomputation: PRIVATE_SOURCE")
    directive = UtilityPresentationSemanticPlanner._safe_retry_directive(error)
    assert "calculation_proofs" in directive
    assert "授权" in directive
    assert "PRIVATE_SOURCE" not in directive


@pytest.mark.parametrize("scope", ["slide", "deck"])
def test_visual_critic_checks_calculation_authorization_not_only_arithmetic(
    scope, monkeypatch, tmp_path,
):
    captured = {}
    monkeypatch.setattr(
        "lantai.presentation.intelligence._utility_route_cache_identity", lambda _: "test-route",
    )

    async def capture_review(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            value=kwargs["result_model"].model_validate(synthetic_review_response(kwargs, [])),
            model_label="vision-model",
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_content_task", capture_review,
    )
    rendered = tmp_path / "slide.png"
    Image.new("RGB", (320, 180), "white").save(rendered)
    slide = VisualCriticSlideInput(
        slide_id="slide-1", slide_index=1, rendered_path=str(rendered),
        rendered_sha256=hashlib.sha256(rendered.read_bytes()).hexdigest(),
        key_message="算术不证明指标授权",
        intent=SlideIntent(
            purpose="审查利润授权", key_message="算术不证明指标授权",
            calculation_proofs=[SourceCalculationProof.model_validate(_source_profit_proof("source-profit"))],
        ),
    )
    critic = UtilityPresentationVisualCritic("agent-1")
    if scope == "slide":
        critic.review_slide(slide)
        context = json.loads(next(
            block["text"] for block in reversed(captured["user_content"]) if block["type"] == "text"
        ))
        assert context["intent"]["calculation_proofs"][0]["result"] == "37"
        assert "page-and-deck-v6" in critic.slide_cache_identity()
    else:
        critic.review_deck([slide])
        context = json.loads(next(
            block["text"] for block in reversed(captured["user_content"]) if block["type"] == "text"
        ))
        assert context["ordered_slides"][0]["intent"]["calculation_proofs"][0]["result"] == "37"
        assert "page-and-deck-v7" in critic.deck_cache_identity()
    prompt = captured["system_prompt"]
    assert "calculation_proofs" in prompt
    assert "算术正确不等于业务授权" in prompt


@pytest.mark.parametrize("scope", ["slide", "deck"])
def test_visual_critic_checks_numeric_roles_and_visible_example_logic(
    scope, monkeypatch, tmp_path,
):
    captured = {}
    monkeypatch.setattr(
        "lantai.presentation.intelligence._utility_route_cache_identity", lambda _: "test-route",
    )

    async def capture_review(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            value=kwargs["result_model"].model_validate(synthetic_review_response(kwargs, [])),
            model_label="vision-model",
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_content_task", capture_review,
    )
    rendered = tmp_path / "example.png"
    Image.new("RGB", (320, 180), "white").save(rendered)
    source = "这份教学材料没有数值指标；1 是示例数量说明。反例只使用色块，不使用文字或图标。"
    slide = VisualCriticSlideInput(
        slide_id="example-1", slide_index=1, rendered_path=str(rendered),
        rendered_sha256=hashlib.sha256(rendered.read_bytes()).hexdigest(),
        key_message="待审示例，不是事实权威",
        original_user_request="依原始材料展示前后对照，不编造数据。",
        source_brief=PresentationBrief(
            objective="教学示例", audience="学习者", narrative="前后对照",
            source_sections=[PresentationSourceSection(
                source_ref="example-source", title="教学材料", body=source,
            )],
        ),
    )
    critic = UtilityPresentationVisualCritic("agent-1")
    if scope == "slide":
        critic.review_slide(slide)
    else:
        critic.review_deck([slide])
    context = json.loads(next(
        block["text"] for block in reversed(captured["user_content"]) if block["type"] == "text"
    ))
    assert source in json.dumps(context, ensure_ascii=False)
    prompt = captured["system_prompt"]
    assert "数字的来源角色" in prompt
    assert "前后对照" in prompt and "实际可见对象" in prompt
    assert "content_accuracy" in prompt and "severity=error" in prompt
    assert "示意值" in prompt
    if scope == "slide":
        assert "page-and-deck-v6" in critic.slide_cache_identity()
    else:
        assert "page-and-deck-v7" in critic.deck_cache_identity()


def test_page_author_keeps_verified_profit_numeric_contract_after_restore() -> None:
    planned, scene = _case()
    planned.charts = []
    planned.intent.source_refs = ["source-profit"]
    planned.intent.required_facts = ["季度利润为37万元。"]
    planned.intent.immutable_facts = []
    planned.intent.calculation_proofs = [SourceCalculationProof.model_validate(_source_profit_proof("source-profit"))]
    scene.objects = scene.objects[:2]
    scene.objects[1].text = "季度利润为37万元。"
    # Exercise the durable JSON boundary, not a trusted in-memory verified flag.
    restored = DeckPlanSlide.model_validate_json(planned.model_dump_json())
    source = "Q2：收入145万元，成本108万元。\n利润按收入减成本计算；不得发明税率或净利润。"
    checked = validate_authored_page(scene, restored, scene, source_text=source)
    assert checked.objects[1].text == "季度利润为37万元。"
    scene.objects[1].text += "净利润为99万元。"
    with pytest.raises(ValueError, match="unsupported number"):
        validate_authored_page(scene, restored, scene, source_text=source)


def test_planned_native_chart_accepts_recomputed_profit_with_source_proof() -> None:
    brief = PresentationBrief(
        objective="展示利润", audience="报告读者", narrative="季度利润",
        source_refs=["source-profit"],
        source_sections=[PresentationSourceSection(
            source_ref="source-profit", title="季度",
            body="Q2：收入145万元，成本108万元。\n利润按收入减成本计算；不得发明税率或净利润。",
        )],
    )
    chart = ChartObject.model_validate({
        "chart_type": "column", "categories": ["Q2"], "series": {"利润": [37]},
        "unit": "万元", "derivation": "145万元−108万元＝37万元",
        "source_refs": ["source-profit"],
        "calculation_proofs": [_source_profit_proof("source-profit")],
    })
    planned = DeckPlanSlide(
        order=1, title="季度利润", charts=[chart],
        intent=SlideIntent(purpose="展示利润", key_message="来源授权利润计算", source_refs=brief.source_refs),
    )
    validate_planned_chart_sources([planned], brief)
    # A formula alone still grants no authority for a derived value.
    planned.charts[0] = chart.model_copy(update={"calculation_proofs": []})
    with pytest.raises(ValueError, match="uncited chart value"):
        validate_planned_chart_sources([planned], brief)


def test_semantic_planner_accepts_recomputed_source_authorized_profit(monkeypatch) -> None:
    request = PresentationJobRequest(
        conversation_id="verified-profit-test",
        user_request="按照来源授权计算季度利润，不推断净利润。",
        slide_count=1,
        materials=[InputMaterial(
            name="季度.md",
            content=(
                "# 季度\nQ2：收入145万元，成本108万元。\n"
                "利润按收入减成本计算；不得发明税率或净利润。"
            ),
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    plan.slides[0].title = "季度利润"
    plan.slides[0].content_blocks = ["季度利润为37万元。"]
    source_ref = brief.source_refs[0]
    proof = _source_profit_proof(source_ref)
    detail_allowed_numbers = []

    async def fake_utility(**kwargs):
        value = _semantic_value_for_call(kwargs, plan)
        if kwargs["result_model"].__name__ == "_SemanticClaimLedger":
            data = value.model_dump(mode="json")
            data["claims"][0].update({
                "kind": "calculation", "calculation": "145万元−108万元＝37万元",
                "calculation_proofs": [proof],
            })
            value = kwargs["result_model"].model_validate(data)
        elif kwargs["result_model"].__name__ == "_SemanticSlideBatch":
            payload = json.loads(str(kwargs["user_prompt"]))
            detail_allowed_numbers.extend(payload["target_slides"][0]["allowed_numeric_tokens"])
        return StructuredUtilityResult(value=value, model_label="test/test", repaired=False)

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_utility,
    )
    result = UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)

    assert "37" in detail_allowed_numbers
    assert "季度利润为37万元。" in result.slides[0].intent.required_facts
    assert result.slides[0].intent.calculation_proofs[0].model_dump(mode="json") == proof
    assert "37" not in numeric_tokens(grounded_source_text(request, brief))


def test_model_storyboard_cannot_pass_an_invented_text_number(monkeypatch) -> None:
    request = PresentationJobRequest(
        conversation_id="text-grounding-test",
        user_request="用原始材料制作收入对比",
        slide_count=1,
        materials=[InputMaterial(
            name="收入.md", content="# 收入\n甲为42万元；乙为38万元。",
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    plan.slides[0].content_blocks = ["甲为99万元"]

    async def fake_utility(**kwargs):
        return StructuredUtilityResult(
            value=_semantic_value_for_call(kwargs, plan),
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )
    with pytest.raises(ValueError, match="uncited storyboard number 99"):
        UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)


def test_model_storyboard_cannot_pass_an_invented_chinese_integer(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="chinese-integer-grounding-test",
        user_request="用原始材料制作研究小组概览",
        slide_count=1,
        materials=[InputMaterial(
            name="小组.md", content="# 概览\n项目有四个研究小组。",
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    plan.slides[0].content_blocks = ["项目由五个研究小组参与"]

    async def fake_utility(**kwargs):
        return StructuredUtilityResult(
            value=_semantic_value_for_call(kwargs, plan),
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )
    with pytest.raises(ValueError, match="uncited storyboard number 5"):
        UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)


def test_semantic_planner_requests_only_storyboard_fields_and_restores_coverage(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="lean-storyboard-schema-test",
        user_request="用原始材料制作收入判断",
        slide_count=10,
        materials=[InputMaterial(
            name="收入.md", content="# 收入\n甲为42万元；乙为38万元。",
        )],
    )
    brief = build_brief(request)
    base_plan = build_deterministic_fixture_plan(request, brief)
    plan = base_plan.model_copy(update={"slides": [
        base_plan.slides[0].model_copy(update={"order": order})
        for order in range(1, 11)
    ]})
    calls = []

    async def fake_utility(**kwargs):
        calls.append(kwargs)
        return StructuredUtilityResult(
            value=_semantic_value_for_call(kwargs, plan),
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )
    result = UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)

    assert [call["purpose"] for call in calls] == [
        "presentation-semantic-claims",
        "presentation-semantic-outline",
        *[
            f"presentation-semantic-detail:{order:03d}-{order:03d}"
            for order in range(1, 11)
        ],
    ]
    claim_schema = calls[0]["result_model"].model_json_schema()
    outline_schema = calls[1]["result_model"].model_json_schema()
    detail_schema = calls[2]["result_model"].model_json_schema()
    assert calls[1]["result_model"] is not type(plan)
    assert set(claim_schema["properties"]) == {
        "schema_version",
        "claims",
        "chart_contracts",
    }
    assert set(outline_schema["properties"]) == {
        "schema_version",
        "title",
        "narrative_arc",
        "visual_direction",
        "slides",
    }
    serialized_outline_schema = json.dumps(outline_schema, ensure_ascii=False)
    assert "content_blocks" not in serialized_outline_schema
    assert '"charts"' not in serialized_outline_schema
    assert set(detail_schema["properties"]) == {"schema_version", "slides"}
    serialized_detail_schema = json.dumps(detail_schema, ensure_ascii=False)
    detail_slide_schema = detail_schema["$defs"]["_SemanticContentUnitSlide"]
    assert set(detail_slide_schema["properties"]) == {
        "order",
        "purpose",
        "key_message",
        "relationship_type",
        "visual_role",
        "density",
        "content_units",
        "takeaway",
        "charts",
    }
    assert "title" not in detail_slide_schema["properties"]
    assert "intent" not in detail_slide_schema["properties"]
    assert "visual_direction" not in serialized_detail_schema
    assert "narrative_arc" not in serialized_detail_schema
    schema = outline_schema
    serialized_schema = json.dumps(schema, ensure_ascii=False)
    assert "composition_plan" not in serialized_schema
    assert "editability_requirement" not in serialized_schema
    assert '"frame"' not in serialized_schema
    assert '"id"' not in serialized_schema
    assert calls[0]["max_output_tokens"] == 8_096
    assert calls[1]["max_output_tokens"] == 8_096
    assert [call["max_output_tokens"] for call in calls[2:]] == [4_096] * 10
    outline_slide_schema = schema["$defs"]["_SemanticOutlineSlide"]
    assert set(outline_slide_schema["properties"]) == {
        "order",
        "title",
        "source_refs",
        "claim_ids",
        "chart_contract_ids",
        "evidence_directive",
    }
    claim_item_schema = claim_schema["$defs"]["_SemanticClaim"]
    assert set(claim_item_schema["properties"]) == {
        "claim_id",
        "kind",
        "statement",
        "calculation",
        "calculation_proofs",
        "source_refs",
        "delivery_contract_ids",
    }
    assert "constraint" not in claim_item_schema["properties"]["kind"]["enum"]
    assert claim_item_schema["properties"]["statement"]["maxLength"] == 64
    assert claim_item_schema["properties"]["calculation_proofs"]["maxItems"] == 24
    proof_schema = claim_schema["$defs"]["SourceCalculationProof"]
    assert set(proof_schema["required"]) == {
        "terms", "result", "unit", "authorization_source_ref", "authorization_excerpt",
    }
    assert proof_schema["additionalProperties"] is False
    assert "delivery_contract_ids" in claim_item_schema["required"]
    chart_contract_schema = claim_schema["$defs"]["_SemanticChartContract"]
    assert set(chart_contract_schema["properties"]) == {
        "contract_id",
        "chart",
    }
    contract_chart_schema = claim_schema["$defs"][
        "_SemanticContractChartObject"
    ]
    assert "source_refs" in contract_chart_schema["required"]
    assert '"evidence":' not in serialized_schema
    assert '"required_facts":' not in serialized_schema
    assert '"immutable_facts":' not in serialized_schema
    assert "事实绑定由确定性层" in calls[2]["system_prompt"]
    assert "此阶段只复用已核验结果，不新增公式、计算凭证或指标" in calls[2]["system_prompt"]
    assert "evidence_directive 只补充验收意图" in calls[2]["system_prompt"]
    assert "不得自行补成量化 KPI 或时间表" in calls[2]["system_prompt"]
    assert "用户明确页数" in calls[1]["system_prompt"]
    assert "order 已承载页码" in calls[1]["system_prompt"]
    assert "真正支撑该 claim" in calls[0]["system_prompt"]
    outline_payload = json.loads(str(calls[1]["user_prompt"]))
    assert outline_payload["claim_ledger"]["claims"]
    assert outline_payload["source_catalog"] == [
        {
            "source_ref": section.source_ref,
            "title": section.title,
        }
        for section in brief.source_sections
    ]
    assert "brief" not in outline_payload
    assert "allowed_numeric_tokens_by_source" not in outline_payload
    assert "只能复用 claim_ledger" in calls[1]["system_prompt"]
    first_target = json.loads(str(calls[2]["user_prompt"]))["target_slides"][0]
    assert first_target["evidence_directive"]
    assert first_target["claims"]
    assert "claim_id" in first_target["claims"][0]
    assert "chart_contracts" in first_target
    assert result.source_coverage == brief.source_coverage
    assert result.slides[0].intent.evidence == result.slides[0].content_blocks
    assert result.slides[0].intent.required_facts == result.slides[0].content_blocks
    assert result.slides[0].intent.immutable_facts == ["42", "38"]


def test_semantic_planner_does_not_promote_editorial_connective_copy_to_fact(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="semantic-editorial-copy-test",
        user_request="用原始材料制作收入判断",
        slide_count=1,
        materials=[InputMaterial(
            name="收入.md",
            content="# 收入\n甲为42万元；乙为38万元。",
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    plan.slides[0].content_blocks = ["甲为42万元；乙为38万元。"]

    async def fake_utility(**kwargs):
        value = _semantic_value_for_call(kwargs, plan)
        if kwargs["result_model"].__name__ == "_SemanticSlideBatch":
            slide = value.slides[0].model_copy(update={
                "content_blocks": [
                    "甲为42万元；乙为38万元。",
                    "相关数据放在同一比较关系中阅读。",
                ],
                "claim_groups": [
                    *value.slides[0].claim_groups,
                    value.slides[0].claim_groups[0].model_copy(
                        update={"claim_ids": []},
                    ),
                ],
            })
            value = value.model_copy(update={"slides": [slide]})
        return StructuredUtilityResult(
            value=value,
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )

    result = UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)

    assert result.slides[0].intent.evidence == [
        "甲为42万元；乙为38万元。",
        "相关数据放在同一比较关系中阅读。",
    ]
    assert result.slides[0].intent.required_facts == [
        "甲为42万元；乙为38万元。",
    ]


def test_semantic_planner_normalizes_localized_density_labels(monkeypatch) -> None:
    request = PresentationJobRequest(
        conversation_id="localized-density-test",
        user_request="用原始材料制作收入判断",
        slide_count=1,
        materials=[InputMaterial(
            name="收入.md", content="# 收入\n甲为42万元；乙为38万元。",
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)

    async def fake_utility(**kwargs):
        def mutate_outline(value):
            payload = value.model_dump(mode="json")
            payload["visual_direction"]["density"] = "适中（信息密度平衡）"
            return kwargs["result_model"].model_validate(payload)

        def mutate_detail(value):
            payload = value.model_dump(mode="json")
            payload["slides"][0]["density"] = "疏朗"
            return kwargs["result_model"].model_validate(payload)

        return StructuredUtilityResult(
            value=_semantic_value_for_call(
                kwargs,
                plan,
                mutate_outline=mutate_outline,
                mutate_detail=mutate_detail,
            ),
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )
    result = UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)

    assert result.visual_direction.density == "medium"
    assert result.slides[0].intent.density == "low"


def test_semantic_claim_ledger_supports_more_than_forty_grounded_claims() -> None:
    ledger = presentation_intelligence._SemanticClaimLedger.model_validate({
        "schema_version": "1.0",
        "claims": [
            {
                "claim_id": f"claim-{index:03d}",
                "kind": "fact",
                "statement": f"判断{chr(0x4E00 + index)}",
                "calculation": "",
                "source_refs": ["source-1"],
                "delivery_contract_ids": [],
            }
            for index in range(1, 42)
        ],
        "chart_contracts": [],
    })

    assert len(ledger.claims) == 41


def test_semantic_planner_retries_claims_that_exceed_storyboard_capacity(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="claim-capacity-test",
        user_request="制作一页判断",
        slide_count=1,
        materials=[InputMaterial(
            name="判断.md",
            content="# 判断\n材料给出一项定性判断。",
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    claim_calls = 0
    retry_directives: list[str] = []

    async def fake_utility(**kwargs):
        nonlocal claim_calls
        if kwargs["result_model"].__name__ == "_SemanticClaimLedger":
            claim_calls += 1
            prompt = json.loads(str(kwargs["user_prompt"]))
            if "retry_directive" in prompt:
                retry_directives.append(str(prompt["retry_directive"]))
            if claim_calls == 1:
                value = kwargs["result_model"].model_validate({
                    "schema_version": "1.0",
                    "claims": [
                        {
                            "claim_id": f"claim-{index:03d}",
                            "kind": "fact",
                            "statement": f"判断{chr(0x4E00 + index)}",
                            "calculation": "",
                            "source_refs": [brief.source_refs[0]],
                            "delivery_contract_ids": [],
                        }
                        for index in range(1, 10)
                    ],
                    "chart_contracts": [],
                })
                return StructuredUtilityResult(
                    value=value,
                    model_label="approved-provider/approved-model",
                    repaired=False,
                )
        return StructuredUtilityResult(
            value=_semantic_value_for_call(kwargs, plan),
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )

    result = UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)

    assert len(result.slides) == 1
    assert claim_calls == 2
    assert len(retry_directives) == 1
    assert "storyboard" in retry_directives[0]


def test_semantic_slide_detail_normalizes_only_known_relationship_aliases() -> None:
    detail = presentation_intelligence._SemanticSlideDetail.model_validate({
        "order": 1,
        "relationship_type": "因果关系",
    })

    assert detail.relationship_type is RelationshipType.CAUSE_EFFECT
    with pytest.raises(ValueError):
        presentation_intelligence._SemanticSlideDetail.model_validate({
            "order": 1,
            "relationship_type": "未经定义的新关系",
        })


def test_semantic_chart_normalizes_numeric_values_with_the_exact_chart_unit() -> None:
    chart = presentation_intelligence._SemanticChartObject.model_validate({
        "chart_type": "bar",
        "categories": ["直接完成", "需要排版", "输入缺失", "服务超时"],
        "series": {"任务数": ["31个", "9 个", "5个", "3 个"]},
        "unit": "个",
        "source_refs": ["source-1"],
    })

    assert chart.series == {"任务数": [31.0, 9.0, 5.0, 3.0]}


@pytest.mark.parametrize("value", ["约31个", "31人", "三十一个", "31个任务"])
def test_semantic_chart_rejects_ambiguous_or_mismatched_numeric_strings(
    value: str,
) -> None:
    with pytest.raises(ValueError):
        presentation_intelligence._SemanticChartObject.model_validate({
            "chart_type": "bar",
            "categories": ["直接完成"],
            "series": {"任务数": [value]},
            "unit": "个",
            "source_refs": ["source-1"],
        })


def test_semantic_outline_source_catalog_covers_merged_section_lineage(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="merged-source-catalog-test",
        user_request="将两份材料合并成一页判断",
        slide_count=1,
        materials=[
            InputMaterial(name="甲.md", content="# 甲\n收入42万元。"),
            InputMaterial(name="乙.md", content="# 乙\n成本38万元。"),
        ],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    calls = []

    async def fake_utility(**kwargs):
        calls.append(kwargs)
        return StructuredUtilityResult(
            value=_semantic_value_for_call(kwargs, plan),
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )
    UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)

    outline_call = next(
        call
        for call in calls
        if call["purpose"] == "presentation-semantic-outline"
    )
    payload = json.loads(str(outline_call["user_prompt"]))
    assert {item["source_ref"] for item in payload["source_catalog"]} == set(
        brief.source_refs,
    )


def test_semantic_planner_uses_mixed_when_model_omits_relationship_type(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="missing-relationship-test",
        user_request="用原始材料制作收入判断",
        slide_count=1,
        materials=[InputMaterial(
            name="收入.md", content="# 收入\n甲为42万元；乙为38万元。",
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)

    async def fake_utility(**kwargs):
        def omit_relationship(value):
            payload = value.model_dump(mode="json")
            del payload["slides"][0]["relationship_type"]
            return type(value).model_validate(payload)

        return StructuredUtilityResult(
            value=_semantic_value_for_call(
                kwargs,
                plan,
                mutate_detail=omit_relationship,
            ),
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )

    result = UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)

    assert result.slides[0].intent.relationship_type.value == "mixed"


def test_semantic_planner_accepts_bare_detail_array_but_still_merges_exactly(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="bare-detail-array-test",
        user_request="用原始材料制作收入判断",
        slide_count=1,
        materials=[InputMaterial(
            name="收入.md", content="# 收入\n甲为42万元；乙为38万元。",
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)

    async def fake_utility(**kwargs):
        value = _semantic_value_for_call(kwargs, plan)
        if str(kwargs["purpose"]).startswith("presentation-semantic-detail"):
            value = kwargs["result_model"].model_validate(
                value.model_dump(mode="json")["slides"],
            )
        return StructuredUtilityResult(
            value=value,
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )

    result = UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)

    assert [slide.order for slide in result.slides] == [1]
    assert result.slides[0].title == plan.slides[0].title


def test_semantic_planner_unwraps_one_redundant_detail_envelope(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="nested-detail-envelope-test",
        user_request="用原始材料制作收入判断",
        slide_count=1,
        materials=[InputMaterial(
            name="收入.md", content="# 收入\n甲为42万元；乙为38万元。",
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)

    async def fake_utility(**kwargs):
        value = _semantic_value_for_call(kwargs, plan)
        if str(kwargs["purpose"]).startswith("presentation-semantic-detail"):
            payload = value.model_dump(mode="json")
            value = kwargs["result_model"].model_validate({
                "schema_version": "1.0",
                "slides": [payload],
            })
        return StructuredUtilityResult(
            value=value,
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )

    result = UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)

    assert [slide.order for slide in result.slides] == [1]
    assert result.slides[0].title == plan.slides[0].title


def test_semantic_planner_discards_redundant_outline_keys_only_when_complete(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="redundant-outline-key-test",
        user_request="用原始材料制作收入判断",
        slide_count=1,
        materials=[InputMaterial(
            name="收入.md", content="# 收入\n甲为42万元；乙为38万元。",
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)

    async def fake_utility(**kwargs):
        value = _semantic_value_for_call(kwargs, plan)
        if kwargs["purpose"] == "presentation-semantic-outline":
            payload = value.model_dump(mode="json")
            payload["slides"][0]["evidence_directe_directive"] = "冗余拼写键"
            value = kwargs["result_model"].model_validate(payload)
        return StructuredUtilityResult(
            value=value,
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )

    result = UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)

    assert [slide.order for slide in result.slides] == [1]
    assert result.slides[0].title == plan.slides[0].title


def test_semantic_planner_inherits_page_sources_when_chart_omits_duplicates(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="chart-source-inheritance-test",
        user_request="用原始材料制作收入对比",
        slide_count=1,
        materials=[InputMaterial(
            name="收入.md", content="# 收入\n甲为42万元；乙为38万元。",
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    plan.slides[0].charts = [ChartObject(
        categories=["甲", "乙"],
        series={"收入": [42, 38]},
        unit="万元",
        derivation="直接摘录",
        source_refs=list(brief.source_refs),
    )]

    async def fake_utility(**kwargs):
        def omit_chart_sources(value):
            payload = value.model_dump(mode="json")
            payload["slides"][0]["charts"][0]["source_refs"] = []
            return type(value).model_validate(payload)

        return StructuredUtilityResult(
            value=_semantic_value_for_call(
                kwargs,
                plan,
                mutate_detail=omit_chart_sources,
            ),
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )

    result = UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)

    assert result.slides[0].charts[0].source_refs == brief.source_refs


def test_semantic_planner_adds_deterministic_chart_provenance_when_omitted(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="chart-provenance-default-test",
        user_request="用原始材料制作收入对比",
        slide_count=1,
        materials=[InputMaterial(
            name="收入.md", content="# 收入\n甲为42万元；乙为38万元。",
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    plan.slides[0].charts = [ChartObject(
        categories=["甲", "乙"],
        series={"收入": [42, 38]},
        unit="万元",
        derivation="直接摘录",
        source_refs=list(brief.source_refs),
    )]

    async def fake_utility(**kwargs):
        def omit_derivation(value):
            payload = value.model_dump(mode="json")
            del payload["slides"][0]["charts"][0]["derivation"]
            return type(value).model_validate(payload)

        return StructuredUtilityResult(
            value=_semantic_value_for_call(
                kwargs,
                plan,
                mutate_detail=omit_derivation,
            ),
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )

    result = UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)

    assert result.slides[0].charts[0].derivation == (
        "来源值；由确定性事实门禁逐项核验"
    )


def test_semantic_planner_caps_detail_concurrency_and_merges_in_order(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="staged-concurrency-test",
        user_request="按原始材料制作十页判断",
        slide_count=10,
        materials=[InputMaterial(
            name="判断.md",
            content="\n\n".join(
                f"# 判断{i}\n事实{i}为{i}万元。" for i in range(1, 11)
            ),
        )],
    )
    brief = build_brief(request)
    base = build_deterministic_fixture_plan(request, brief).slides[0]
    plan = build_deterministic_fixture_plan(request, brief).model_copy(
        update={
            "slides": [
                base.model_copy(
                    update={
                        "order": order,
                        "title": f"第{order}页判断",
                        "content_blocks": [f"事实{order}为{order}万元。"],
                    },
                )
                for order in range(1, 11)
            ],
        },
    )
    active_details = 0
    max_active_details = 0

    async def fake_utility(**kwargs):
        nonlocal active_details, max_active_details
        if str(kwargs["purpose"]).startswith("presentation-semantic-detail"):
            active_details += 1
            max_active_details = max(max_active_details, active_details)
            await asyncio.sleep(0.01)
            active_details -= 1
        return StructuredUtilityResult(
            value=_semantic_value_for_call(kwargs, plan),
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    result = planner.plan(request, brief)

    assert max_active_details == 2
    assert [slide.order for slide in result.slides] == list(range(1, 11))
    assert [slide.title for slide in result.slides] == [
        f"第{order}页判断" for order in range(1, 11)
    ]
    assert planner.last_output_token_budget == 57_152
    assert planner.last_stage_output_token_budgets == {
        "claims": 8_096,
        "outline": 8_096,
        **{
            f"detail:{order:03d}-{order:03d}": 4_096
            for order in range(1, 11)
        },
    }
    assert planner.last_utility_call_count == 12
    assert planner.last_stage_attempt_counts == {
        "claims": 1,
        "outline": 1,
        **{
            f"detail:{order:03d}-{order:03d}": 1
            for order in range(1, 11)
        },
    }
    assert planner.estimated_cost_units(request) == 600


def test_semantic_planner_retries_outline_schema_with_budget_accounting(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="staged-schema-retry-test",
        user_request="制作一页判断",
        slide_count=1,
        materials=[InputMaterial(
            name="判断.md",
            content="# 判断\n事实为1万元。",
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    calls: list[str] = []
    prompts: list[dict[str, object]] = []

    async def fake_utility(**kwargs):
        purpose = str(kwargs["purpose"])
        calls.append(purpose)
        prompts.append(json.loads(str(kwargs["user_prompt"])))
        if purpose == "presentation-semantic-outline" and calls.count(purpose) <= 2:
            raise StructuredTaskValidationError(
                "structured utility task output failed schema validation",
            )
        return StructuredUtilityResult(
            value=_semantic_value_for_call(kwargs, plan),
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")

    result = planner.plan(request, brief)

    assert [slide.order for slide in result.slides] == [1]
    assert calls == [
        "presentation-semantic-claims",
        "presentation-semantic-outline",
        "presentation-semantic-outline",
        "presentation-semantic-outline",
        "presentation-semantic-detail:001-001",
    ]
    assert "retry_directive" not in prompts[0]
    assert "retry_directive" not in prompts[1]
    assert "schema" in str(prompts[2]["retry_directive"])
    assert "schema" in str(prompts[3]["retry_directive"])
    assert planner.last_utility_call_count == 5
    assert planner.last_stage_attempt_counts == {
        "claims": 1,
        "outline": 3,
        "detail:001-001": 1,
    }
    assert planner.last_output_token_budget == (
        planner.last_stage_output_token_budgets["claims"]
        + planner.last_stage_output_token_budgets["outline"] * 3
        + planner.last_stage_output_token_budgets["detail:001-001"]
    )
    assert planner.estimated_cost_units(request) == 150


def test_semantic_planner_retains_distinct_outline_recovery_constraints(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="staged-mixed-outline-retry-test",
        user_request="制作一页判断",
        slide_count=1,
        materials=[InputMaterial(
            name="判断.md",
            content="# 判断\n事实为1万元。",
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    outline_calls = 0
    outline_prompts: list[dict[str, object]] = []

    async def fake_utility(**kwargs):
        nonlocal outline_calls
        value = _semantic_value_for_call(kwargs, plan)
        if kwargs["result_model"].__name__ == "_SemanticDeckOutline":
            outline_calls += 1
            payload = json.loads(str(kwargs["user_prompt"]))
            outline_prompts.append(payload)
            if outline_calls == 1:
                raise StructuredTaskValidationError(
                    "structured utility task output failed schema validation",
                )
            if outline_calls == 2:
                slide = value.slides[0].model_copy(update={
                    "source_refs": ["source-missing"],
                })
                value = value.model_copy(update={"slides": [slide]})
            elif outline_calls == 3:
                slide = value.slides[0].model_copy(update={
                    "title": "建议在三周内完成",
                })
                value = value.model_copy(update={"slides": [slide]})
            else:
                directive = str(payload.get("retry_directive", ""))
                assert "schema" in directive
                assert "source_refs" in directive
                assert "无法支持的数字" in directive
                assert "三周" not in directive
        return StructuredUtilityResult(
            value=value,
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")

    result = planner.plan(request, brief)

    assert [slide.order for slide in result.slides] == [1]
    assert outline_calls == 4
    assert "retry_directive" not in outline_prompts[0]
    assert planner.last_stage_attempt_counts == {
        "claims": 1,
        "outline": 4,
        "detail:001-001": 1,
    }


def test_semantic_planner_replays_mixed_outline_failures_against_assignment_contract(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="staged-outline-assignment-contract-test",
        user_request="制作两页判断",
        slide_count=2,
        materials=[InputMaterial(
            name="判断.md",
            content="# 判断一\n事实一为1万元。\n\n# 判断二\n事实二为2万元。",
        )],
    )
    brief = build_brief(request)
    base_plan = build_deterministic_fixture_plan(request, brief)
    base = base_plan.slides[0]
    plan = base_plan.model_copy(update={"slides": [
        base.model_copy(update={
            "order": 1,
            "title": "判断一",
            "content_blocks": ["事实一为1万元。"],
        }),
        base.model_copy(update={
            "order": 2,
            "title": "判断二",
            "content_blocks": ["事实二为2万元。"],
        }),
    ]})
    outline_calls = 0
    approved_claim_ids: list[str] = []
    missing_claim_id = ""

    async def fake_utility(**kwargs):
        nonlocal outline_calls, approved_claim_ids, missing_claim_id
        value = _semantic_value_for_call(kwargs, plan)
        if kwargs["result_model"].__name__ == "_SemanticClaimLedger":
            approved_claim_ids = [claim.claim_id for claim in value.claims]
        if kwargs["result_model"].__name__ == "_SemanticDeckOutline":
            outline_calls += 1
            payload = json.loads(str(kwargs["user_prompt"]))
            slides = list(value.slides)
            if outline_calls == 1:
                missing_claim_id = slides[0].claim_ids[-1]
                slides[0] = slides[0].model_copy(update={
                    "claim_ids": slides[0].claim_ids[:-1],
                })
                value = value.model_copy(update={"slides": slides})
            elif outline_calls == 2:
                slides[0] = slides[0].model_copy(update={
                    "source_refs": ["source-missing"],
                })
                value = value.model_copy(update={"slides": slides})
            else:
                contract = payload["outline_assignment_contract"]
                assert contract == {
                    "expected_slide_orders": [1, 2],
                    "required_claim_ids": approved_claim_ids,
                    "required_chart_contract_ids": [],
                    "minimum_claims_per_slide": 1,
                    "maximum_claims_per_slide": 8,
                    "claim_minimum_occurrences": 1,
                    "chart_exact_occurrences": 1,
                    "allowed_source_refs": list(brief.source_refs),
                    "assigned_contract_sources_must_be_in_slide_scope": True,
                }
                directive = str(payload.get("retry_directive", ""))
                assert missing_claim_id in directive
                assert "source_refs" in directive
        return StructuredUtilityResult(
            value=value,
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")

    result = planner.plan(request, brief)

    assert [slide.order for slide in result.slides] == [1, 2]
    assert outline_calls == 3
    assert planner.last_stage_contract_diagnostics[:2] == [
        {
            "stage": "outline",
            "attempt": 1,
            "violation_code": "outline_coverage",
            "failure_kind": "semantic_contract",
            "missing_claim_ids": [missing_claim_id],
        },
        {
            "stage": "outline",
            "attempt": 2,
            "violation_code": "outline_source_refs",
            "failure_kind": "semantic_contract",
            "target_orders": [1],
        },
    ]


def test_semantic_planner_scopes_claim_numeric_retry_to_allowed_tokens(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="staged-claim-numeric-contract-test",
        user_request="制作一页收入判断",
        slide_count=1,
        materials=[InputMaterial(
            name="收入.md",
            content="# 收入\n甲为42万元。",
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    plan.slides[0].content_blocks = ["甲为42万元。"]
    claim_calls = 0
    claim_prompts: list[dict[str, object]] = []

    async def fake_utility(**kwargs):
        nonlocal claim_calls
        value = _semantic_value_for_call(kwargs, plan)
        if kwargs["result_model"].__name__ == "_SemanticClaimLedger":
            claim_calls += 1
            payload = json.loads(str(kwargs["user_prompt"]))
            claim_prompts.append(payload)
            if claim_calls <= 2:
                invalid_claim = value.claims[0].model_copy(update={
                    "statement": "甲为99万元。",
                })
                value = value.model_copy(update={"claims": [invalid_claim]})
            else:
                directive = str(payload.get("retry_directive", ""))
                assert "claim-001" in directive
                assert "42" in directive
                assert "99" not in directive
        return StructuredUtilityResult(
            value=value,
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")

    result = planner.plan(request, brief)

    assert [slide.order for slide in result.slides] == [1]
    assert claim_calls == 3
    assert "retry_directive" not in claim_prompts[0]
    assert all(
        "99" not in str(item.get("retry_directive", ""))
        for item in claim_prompts[1:]
    )
    assert planner.last_stage_contract_diagnostics == [
        {
            "stage": "claims",
            "attempt": 1,
            "violation_code": "unsupported_numeric_claim",
            "failure_kind": "numeric_grounding",
        },
        {
            "stage": "claims",
            "attempt": 2,
            "violation_code": "unsupported_numeric_claim",
            "failure_kind": "numeric_grounding",
        },
    ]


def test_semantic_claim_retry_explains_chart_series_number_transport() -> None:
    error = StructuredTaskValidationError(
        "structured utility task output failed schema validation",
        validation_issues=(
            "float_parsing@$.chart_contracts[1].chart.series._[0]",
            "float_parsing@$.chart_contracts[1].chart.series._[1]",
        ),
    )

    directive = UtilityPresentationSemanticPlanner._safe_retry_directive(error)

    assert "series 的每个值必须是 JSON number" in directive
    assert "unit 字段" in directive
    assert "不要把单位拼进 series" in directive
    assert "chart_contracts[1]" not in directive


def test_claim_schema_retry_keeps_source_ids_chart_scope_and_required_empty_fields() -> None:
    error = StructuredTaskValidationError(
        "structured utility task output failed schema validation",
        validation_issues=(
            "string_type@$.claims[12].source_refs[1]",
            "missing@$.claims[12].delivery_contract_ids",
            "extra_forbidden@$.claims[12].chart_contracts",
        ),
    )

    directive = UtilityPresentationSemanticPlanner._safe_retry_directive(error)

    assert "allowed_source_refs" in directive
    assert "JSON 字符串" in directive
    assert "数字或计算凭证" in directive
    assert "chart_contracts 只能位于账本顶层" in directive
    assert "delivery_contract_ids 字段不能省略" in directive
    assert "无对应硬交付时显式返回 []" in directive
    assert "claims[12]" not in directive


def test_claim_id_schema_retry_requires_contiguous_three_digit_ids() -> None:
    error = StructuredTaskValidationError(
        "structured utility task output failed schema validation",
        validation_issues=("string_pattern_mismatch@$.claims[27].claim_id",),
    )

    directive = UtilityPresentationSemanticPlanner._safe_retry_directive(error)

    assert "claim-001" in directive
    assert "连续三位编号" in directive
    assert "claims[27]" not in directive


def test_numeric_claim_retry_preserves_authorized_calculation_proof_path() -> None:
    error = ValueError("synthetic-private-rejected-calculation")
    error.presentation_numeric_token = "876543210987654321"
    error.presentation_numeric_location = "claim-ledger-claim-001"
    error.presentation_allowed_numeric_tokens = ["145", "108"]

    directive = UtilityPresentationSemanticPlanner._safe_retry_directive(error)

    assert "145、108" in directive
    assert "calculation_proofs" in directive
    assert "明确授权的同单位加减" in directive
    assert "不得把未授权数值加入白名单" in directive
    assert "876543210987654321" not in directive
    assert "synthetic-private" not in directive


def test_semantic_claim_prompt_separates_chart_values_from_unit() -> None:
    prompt = UtilityPresentationSemanticPlanner.CLAIM_SYSTEM_PROMPT

    assert "series 的每个值必须是 JSON number" in prompt
    assert "单位只写在 unit 字段" in prompt


def test_semantic_planner_retries_only_timed_out_outline_stage(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="staged-timeout-retry-test",
        user_request="制作一页判断",
        slide_count=1,
        materials=[InputMaterial(
            name="判断.md",
            content="# 判断\n事实为1万元。",
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    calls: list[str] = []
    prompts: list[dict[str, object]] = []

    async def fake_utility(**kwargs):
        purpose = str(kwargs["purpose"])
        calls.append(purpose)
        prompts.append(json.loads(str(kwargs["user_prompt"])))
        if purpose == "presentation-semantic-outline" and calls.count(purpose) == 1:
            raise UtilityTaskTimeoutError(
                phase="total",
                timeout_seconds=120,
            )
        return StructuredUtilityResult(
            value=_semantic_value_for_call(kwargs, plan),
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")

    result = planner.plan(request, brief)

    assert [slide.order for slide in result.slides] == [1]
    assert calls == [
        "presentation-semantic-claims",
        "presentation-semantic-outline",
        "presentation-semantic-outline",
        "presentation-semantic-detail:001-001",
    ]
    assert "retry_directive" not in prompts[0]
    assert "retry_directive" not in prompts[1]
    assert "超时" in str(prompts[2]["retry_directive"])
    assert "120" not in str(prompts[2]["retry_directive"])
    assert planner.last_utility_call_count == 4
    assert planner.last_stage_attempt_counts == {
        "claims": 1,
        "outline": 2,
        "detail:001-001": 1,
    }
    assert planner.last_output_token_budget == (
        planner.last_stage_output_token_budgets["claims"]
        + planner.last_stage_output_token_budgets["outline"] * 2
        + planner.last_stage_output_token_budgets["detail:001-001"]
    )
    assert planner.estimated_cost_units(request) == 150


def test_semantic_planner_allows_active_stream_to_finish_beyond_old_total_limit(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="staged-progress-timeout-test",
        user_request="制作一页判断",
        slide_count=1,
        materials=[InputMaterial(
            name="判断.md",
            content="# 判断\n事实为1万元。",
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    calls: list[dict[str, object]] = []

    async def fake_utility(**kwargs):
        calls.append(kwargs)
        return StructuredUtilityResult(
            value=_semantic_value_for_call(kwargs, plan),
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )

    UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)

    assert calls
    assert all(call["timeout_seconds"] == 360 for call in calls)
    assert all(
        call["first_output_timeout_seconds"] == 120 for call in calls
    )
    assert all(call["idle_timeout_seconds"] == 60 for call in calls)


def test_semantic_planner_retries_claim_ledger_with_unknown_source(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="staged-claim-source-retry-test",
        user_request="制作一页判断",
        slide_count=1,
        materials=[InputMaterial(
            name="判断.md",
            content="# 判断\n事实为1万元。",
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    claim_calls = 0
    retry_directives: list[str] = []

    async def fake_utility(**kwargs):
        nonlocal claim_calls
        value = _semantic_value_for_call(kwargs, plan)
        if kwargs["result_model"].__name__ == "_SemanticClaimLedger":
            claim_calls += 1
            payload = json.loads(str(kwargs["user_prompt"]))
            if "retry_directive" in payload:
                retry_directives.append(str(payload["retry_directive"]))
            if claim_calls == 1:
                invalid_claim = value.claims[0].model_copy(
                    update={"source_refs": ["unknown-source-ref"]},
                )
                value = value.model_copy(update={"claims": [invalid_claim]})
        return StructuredUtilityResult(
            value=value,
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )

    result = UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)

    assert len(result.slides) == 1
    assert claim_calls == 2
    assert len(retry_directives) == 1
    assert "source/claim ledger" in retry_directives[0]
    assert "unknown-source-ref" not in retry_directives[0]


def test_semantic_planner_retries_claim_ledger_that_omits_explicit_delivery_contract(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="staged-delivery-contract-retry-test",
        user_request="制作一页经营判断",
        slide_count=1,
        materials=[InputMaterial(
            id="quarter-data",
            name="quarter-data.json",
            media_type="application/json",
            content=json.dumps(
                {
                    "expected_checks": {
                        "q4_contribution_change_vs_q3": -9,
                    },
                },
                ensure_ascii=False,
            ),
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    plan.slides[0].content_blocks = ["Q4经营贡献较Q3下降9万元。"]
    claim_calls = 0
    retry_directives: list[str] = []
    claim_prompts: list[dict[str, object]] = []

    async def fake_utility(**kwargs):
        nonlocal claim_calls
        value = _semantic_value_for_call(kwargs, plan)
        if kwargs["result_model"].__name__ == "_SemanticClaimLedger":
            claim_calls += 1
            payload = json.loads(str(kwargs["user_prompt"]))
            claim_prompts.append(payload)
            if "retry_directive" in payload:
                retry_directives.append(str(payload["retry_directive"]))
            if claim_calls == 2:
                contract_ids = [
                    contract["contract_id"]
                    for contract in payload["required_delivery_contracts"]
                ]
                claim = value.claims[0].model_copy(update={
                    "delivery_contract_ids": contract_ids,
                })
                value = value.model_copy(update={"claims": [claim]})
        return StructuredUtilityResult(
            value=value,
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )

    result = UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)

    assert len(result.slides) == 1
    assert claim_calls == 2
    assert claim_prompts[0]["required_delivery_contracts"] == [
        {
            "contract_id": "delivery-001",
            "source_key": "expected_checks.q4_contribution_change_vs_q3",
            "required_value": -9,
            "unit": "",
            "source_refs": ["quarter-data"],
        },
    ]
    assert len(retry_directives) == 1
    assert "required_delivery_contracts" in retry_directives[0]
    assert "9" not in retry_directives[0]


def test_semantic_planner_retries_delivery_contract_with_bare_signed_value(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="staged-delivery-semantics-retry-test",
        user_request="制作一页经营判断",
        slide_count=1,
        materials=[InputMaterial(
            id="quarter-data",
            name="quarter-data.json",
            media_type="application/json",
            content=json.dumps(
                {
                    "unit": "万元",
                    "expected_checks": {
                        "q4_contribution_change_vs_q3": -9,
                    },
                },
                ensure_ascii=False,
            ),
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    plan.slides[0].content_blocks = ["Q4经营贡献较Q3下降9万元。"]
    claim_calls = 0
    retry_directives: list[str] = []

    async def fake_utility(**kwargs):
        nonlocal claim_calls
        value = _semantic_value_for_call(kwargs, plan)
        if kwargs["result_model"].__name__ == "_SemanticClaimLedger":
            claim_calls += 1
            payload = json.loads(str(kwargs["user_prompt"]))
            if "retry_directive" in payload:
                retry_directives.append(str(payload["retry_directive"]))
            claim = value.claims[0].model_copy(update={
                "delivery_contract_ids": ["delivery-001"],
            })
            if claim_calls == 1:
                claim = claim.model_copy(update={
                    "statement": "交付检查值为-9万元。",
                })
            value = value.model_copy(update={"claims": [claim]})
        return StructuredUtilityResult(
            value=value,
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )

    result = UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)

    assert len(result.slides) == 1
    assert claim_calls == 2
    assert len(retry_directives) == 1
    assert "完整事实句" in retry_directives[0]
    assert "Q4" not in retry_directives[0]
    assert "9" not in retry_directives[0]


def test_semantic_planner_retries_outline_with_unknown_claim_id(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="staged-outline-claim-retry-test",
        user_request="制作一页判断",
        slide_count=1,
        materials=[InputMaterial(
            name="判断.md",
            content="# 判断\n事实为1万元。",
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    outline_calls = 0
    retry_directives: list[str] = []

    async def fake_utility(**kwargs):
        nonlocal outline_calls
        value = _semantic_value_for_call(kwargs, plan)
        if kwargs["result_model"].__name__ == "_SemanticDeckOutline":
            outline_calls += 1
            payload = json.loads(str(kwargs["user_prompt"]))
            if "retry_directive" in payload:
                retry_directives.append(str(payload["retry_directive"]))
            if outline_calls == 1:
                invalid_slide = value.slides[0].model_copy(
                    update={"claim_ids": ["claim-999"]},
                )
                value = value.model_copy(update={"slides": [invalid_slide]})
        return StructuredUtilityResult(
            value=value,
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )

    result = UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)

    assert len(result.slides) == 1
    assert outline_calls == 2
    assert len(retry_directives) == 1
    assert "claim_ledger" in retry_directives[0]
    assert "999" not in retry_directives[0]


def test_semantic_planner_names_only_missing_contract_ids_on_coverage_retry(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="staged-outline-coverage-retry-test",
        user_request="制作两页判断",
        slide_count=2,
        materials=[InputMaterial(
            name="判断.md",
            content="# 判断一\n事实一为1万元。\n\n# 判断二\n事实二为2万元。",
        )],
    )
    brief = build_brief(request)
    base_plan = build_deterministic_fixture_plan(request, brief)
    base = base_plan.slides[0]
    plan = base_plan.model_copy(update={"slides": [
        base.model_copy(update={
            "order": 1,
            "title": "判断一",
            "content_blocks": ["事实一为1万元。"],
        }),
        base.model_copy(update={
            "order": 2,
            "title": "判断二",
            "content_blocks": ["事实二为2万元。"],
        }),
    ]})
    outline_calls = 0
    missing_id = ""
    retry_directives: list[str] = []

    async def fake_utility(**kwargs):
        nonlocal outline_calls, missing_id
        value = _semantic_value_for_call(kwargs, plan)
        if kwargs["result_model"].__name__ == "_SemanticDeckOutline":
            outline_calls += 1
            payload = json.loads(str(kwargs["user_prompt"]))
            if "retry_directive" in payload:
                retry_directives.append(str(payload["retry_directive"]))
            if outline_calls == 1:
                slides = list(value.slides)
                target_index = next(
                    index
                    for index, slide in enumerate(slides)
                    if slide.claim_ids
                )
                target = slides[target_index]
                missing_id = target.claim_ids[-1]
                slides[target_index] = target.model_copy(update={
                    "claim_ids": target.claim_ids[:-1],
                })
                value = value.model_copy(update={"slides": slides})
        return StructuredUtilityResult(
            value=value,
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )

    result = UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)

    assert len(result.slides) == 2
    assert outline_calls == 2
    assert missing_id in retry_directives[0]
    assert "事实一为1万元" not in retry_directives[0]
    assert "事实二为2万元" not in retry_directives[0]


def test_semantic_planner_retries_outline_with_claimless_slide(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="staged-outline-page-claim-retry-test",
        user_request="制作两页判断",
        slide_count=2,
        materials=[InputMaterial(
            name="判断.md",
            content="# 判断一\n事实一为1万元。\n\n# 判断二\n事实二为2万元。",
        )],
    )
    brief = build_brief(request)
    base_plan = build_deterministic_fixture_plan(request, brief)
    base = base_plan.slides[0]
    plan = base_plan.model_copy(update={"slides": [
        base.model_copy(update={
            "order": 1,
            "title": "判断一",
            "content_blocks": ["事实一为1万元。"],
        }),
        base.model_copy(update={
            "order": 2,
            "title": "判断二",
            "content_blocks": ["事实二为2万元。"],
        }),
    ]})
    outline_calls = 0
    retry_directives: list[str] = []

    async def fake_utility(**kwargs):
        nonlocal outline_calls
        value = _semantic_value_for_call(kwargs, plan)
        if kwargs["result_model"].__name__ == "_SemanticDeckOutline":
            outline_calls += 1
            payload = json.loads(str(kwargs["user_prompt"]))
            if "retry_directive" in payload:
                retry_directives.append(str(payload["retry_directive"]))
            if outline_calls == 1:
                slides = list(value.slides)
                slides[0] = slides[0].model_copy(update={
                    "claim_ids": [
                        *slides[0].claim_ids,
                        *slides[1].claim_ids,
                    ],
                })
                slides[1] = slides[1].model_copy(update={"claim_ids": []})
                value = value.model_copy(update={"slides": slides})
        return StructuredUtilityResult(
            value=value,
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )

    result = UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)

    assert len(result.slides) == 2
    assert outline_calls == 2
    assert len(retry_directives) == 1
    assert "第 2 页" in retry_directives[0]
    assert "至少分配一个" in retry_directives[0]
    assert "事实一为1万元" not in retry_directives[0]
    assert "事实二为2万元" not in retry_directives[0]


def test_semantic_planner_types_exhausted_outline_coverage_as_content(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="staged-outline-coverage-stop-test",
        user_request="制作一页判断",
        slide_count=1,
        materials=[InputMaterial(
            name="判断.md",
            content="# 判断\n事实为1万元。",
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)

    async def fake_utility(**kwargs):
        value = _semantic_value_for_call(kwargs, plan)
        if kwargs["result_model"].__name__ == "_SemanticDeckOutline":
            slide = value.slides[0].model_copy(update={"claim_ids": []})
            value = value.model_copy(update={"slides": [slide]})
        return StructuredUtilityResult(
            value=value,
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )

    with pytest.raises(PresentationSemanticContentError) as captured:
        UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)

    assert captured.value.violation_code == "outline_coverage"


def test_semantic_planner_routes_overfull_outline_to_capacity_retry(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="staged-outline-capacity-retry-test",
        user_request="制作一页判断",
        slide_count=1,
        materials=[InputMaterial(
            name="判断.md",
            content="# 判断\n事实为1万元。",
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    outline_calls = 0
    retry_directives: list[str] = []

    async def fake_utility(**kwargs):
        nonlocal outline_calls
        value = _semantic_value_for_call(kwargs, plan)
        if kwargs["result_model"].__name__ == "_SemanticDeckOutline":
            outline_calls += 1
            payload = json.loads(str(kwargs["user_prompt"]))
            if "retry_directive" in payload:
                retry_directives.append(str(payload["retry_directive"]))
            if outline_calls == 1:
                raw = value.model_dump(mode="json")
                raw["slides"][0]["claim_ids"] = ["claim-001"] * 9
                value = kwargs["result_model"].model_validate(raw)
        return StructuredUtilityResult(
            value=value,
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )

    result = UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)

    assert len(result.slides) == 1
    assert outline_calls == 2
    assert len(retry_directives) == 1
    assert "每页不超过 8 个 claim_id" in retry_directives[0]


def test_semantic_planner_stops_after_three_claim_schema_failures(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="staged-claim-stop-test",
        user_request="制作一页判断",
        slide_count=1,
        materials=[InputMaterial(
            name="判断.md",
            content="# 判断\n事实为1万元。",
        )],
    )
    brief = build_brief(request)
    claim_calls = 0

    async def fake_utility(**_kwargs):
        nonlocal claim_calls
        claim_calls += 1
        raise StructuredTaskValidationError(
            "structured utility task output failed schema validation",
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")

    with pytest.raises(StructuredTaskValidationError):
        planner.plan(request, brief)

    assert claim_calls == 3
    assert planner.last_utility_call_count == 3
    assert planner.last_stage_attempt_counts == {"claims": 3}


def test_semantic_planner_stops_after_three_outline_schema_failures(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="staged-outline-stop-test",
        user_request="制作一页判断",
        slide_count=1,
        materials=[InputMaterial(
            name="判断.md",
            content="# 判断\n事实为1万元。",
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    outline_calls = 0

    async def fake_utility(**kwargs):
        nonlocal outline_calls
        if kwargs["result_model"].__name__ == "_SemanticClaimLedger":
            return StructuredUtilityResult(
                value=_semantic_value_for_call(kwargs, plan),
                model_label="approved-provider/approved-model",
                repaired=False,
            )
        outline_calls += 1
        raise StructuredTaskValidationError(
            "structured utility task output failed schema validation",
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")

    with pytest.raises(StructuredTaskValidationError):
        planner.plan(request, brief)

    assert outline_calls == 3
    assert planner.last_utility_call_count == 4
    assert planner.last_stage_attempt_counts == {
        "claims": 1,
        "outline": 3,
    }


def test_semantic_planner_retries_one_outline_contract_failure(monkeypatch) -> None:
    request = PresentationJobRequest(
        conversation_id="staged-outline-retry-test",
        user_request="制作四页判断",
        slide_count=4,
        materials=[InputMaterial(
            name="判断.md",
            content="\n\n".join(
                f"# 判断{i}\n事实{i}为{i}万元。" for i in range(1, 5)
            ),
        )],
    )
    brief = build_brief(request)
    fixture_plan = build_deterministic_fixture_plan(request, brief)
    base = fixture_plan.slides[0]
    full_plan = fixture_plan.model_copy(update={"slides": [
        base.model_copy(update={
            "order": order,
            "title": f"第{order}页判断",
            "content_blocks": [f"事实{order}为{order}万元。"],
        })
        for order in range(1, 5)
    ]})
    short_plan = full_plan.model_copy(update={"slides": full_plan.slides[:3]})
    outline_calls = 0
    retry_directives: list[str] = []

    async def fake_utility(**kwargs):
        nonlocal outline_calls
        selected_plan = full_plan
        if kwargs["result_model"].__name__ == "_SemanticDeckOutline":
            outline_calls += 1
            payload = json.loads(str(kwargs["user_prompt"]))
            if "retry_directive" in payload:
                retry_directives.append(str(payload["retry_directive"]))
            if outline_calls == 1:
                selected_plan = short_plan
        return StructuredUtilityResult(
            value=_semantic_value_for_call(kwargs, selected_plan),
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")

    result = planner.plan(request, brief)

    assert len(result.slides) == 4
    assert outline_calls == 2
    assert retry_directives == [
        "上次输出没有达到 request.slide_count_ceiling 指定的精确页数；"
        "请完整重新生成恰好该页数，连续编号，不要填充或删减。",
    ]
    assert planner.last_stage_attempt_counts == {
        "claims": 1,
        "outline": 2,
        **{
            f"detail:{order:03d}-{order:03d}": 1
            for order in range(1, 5)
        },
    }


def test_semantic_planner_retries_outline_numeric_scope_before_detail(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="staged-outline-numeric-scope-test",
        user_request="制作一页判断",
        slide_count=1,
        materials=[
            InputMaterial(
                name="要求.md",
                content="# 要求\n整理管理判断。",
            ),
            InputMaterial(
                name="数据.md",
                content="# 数据\nQ4收入175万元。",
            ),
        ],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    outline_calls = 0
    detail_calls = 0
    retry_directives: list[str] = []

    async def fake_utility(**kwargs):
        nonlocal outline_calls, detail_calls
        value = _semantic_value_for_call(kwargs, plan)
        payload = json.loads(str(kwargs["user_prompt"]))
        if kwargs["result_model"].__name__ == "_SemanticDeckOutline":
            outline_calls += 1
            if "retry_directive" in payload:
                retry_directives.append(str(payload["retry_directive"]))
            if outline_calls == 1:
                slide = value.slides[0].model_copy(update={
                    "title": "Q4收入是本页核心判断",
                    "source_refs": [brief.source_refs[0]],
                })
                value = value.model_copy(update={"slides": [slide]})
            else:
                slide = value.slides[0].model_copy(update={
                    "source_refs": list(brief.source_refs),
                })
                value = value.model_copy(update={"slides": [slide]})
        elif kwargs["result_model"].__name__ == "_SemanticSlideBatch":
            detail_calls += 1
        return StructuredUtilityResult(
            value=value,
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")

    result = planner.plan(request, brief)

    assert len(result.slides) == 1
    assert outline_calls == 2
    assert detail_calls == 1
    assert len(retry_directives) == 1
    assert "source_refs" in retry_directives[0]
    assert "4" not in retry_directives[0]
    assert planner.last_stage_attempt_counts == {
        "claims": 1,
        "outline": 2,
        "detail:001-001": 1,
    }


def test_semantic_planner_retries_one_ungrounded_detail_batch(monkeypatch) -> None:
    request = PresentationJobRequest(
        conversation_id="staged-content-retry-test",
        user_request="制作一页判断",
        slide_count=1,
        materials=[InputMaterial(name="判断.md", content="# 判断\n事实为1万元。")],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    detail_calls = 0
    retry_directives: list[str] = []
    detail_payloads: list[dict[str, object]] = []

    async def fake_utility(**kwargs):
        nonlocal detail_calls
        value = _semantic_value_for_call(kwargs, plan)
        if kwargs["result_model"].__name__ == "_SemanticSlideBatch":
            detail_calls += 1
            payload = json.loads(str(kwargs["user_prompt"]))
            detail_payloads.append(payload)
            if "retry_directive" in payload:
                retry_directives.append(str(payload["retry_directive"]))
            if detail_calls == 1:
                slide = value.slides[0].model_copy(
                    update={
                        "content_blocks": [
                            *plan.slides[0].content_blocks,
                            "建议在8周内完成。",
                        ],
                        "claim_groups": [
                            *value.slides[0].claim_groups,
                            value.slides[0].claim_groups[0].model_copy(
                                update={"claim_ids": []},
                            ),
                        ],
                    },
                )
                value = value.model_copy(update={"slides": [slide]})
        return StructuredUtilityResult(
            value=value,
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")

    result = planner.plan(request, brief)

    assert len(result.slides) == 1
    assert detail_calls == 2
    assert len(retry_directives) == 1
    assert "来源" in retry_directives[0]
    assert "8" not in retry_directives[0]
    assert [
        payload["target_slides"][0]["allowed_numeric_tokens"]
        for payload in detail_payloads
    ] == [["1"], ["1"]]
    assert planner.last_stage_attempt_counts == {
        "claims": 1,
        "outline": 1,
        "detail:001-001": 2,
    }


def test_semantic_planner_retries_missing_approved_claim_before_runtime(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="staged-required-claim-test",
        user_request="制作一页判断",
        slide_count=1,
        materials=[InputMaterial(
            name="判断.md",
            content="# 判断\n硬交付：事实为1万元。",
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    plan.slides[0].content_blocks = ["硬交付：事实为1万元。"]
    detail_calls = 0
    retry_directives: list[str] = []
    detail_payloads: list[dict[str, object]] = []
    missing_claim_id = ""

    async def fake_utility(**kwargs):
        nonlocal detail_calls, missing_claim_id
        value = _semantic_value_for_call(kwargs, plan)
        if kwargs["result_model"].__name__ == "_SemanticSlideBatch":
            detail_calls += 1
            payload = json.loads(str(kwargs["user_prompt"]))
            detail_payloads.append(payload)
            if "retry_directive" in payload:
                retry_directives.append(str(payload["retry_directive"]))
            if detail_calls == 1:
                missing_claim_id = value.slides[0].claim_groups[0].claim_ids[0]
                slide = value.slides[0].model_copy(update={
                    "purpose": "仅作判断",
                    "key_message": "仅作判断",
                    "content_blocks": ["只保留定性判断。"],
                    "claim_groups": [],
                    "takeaway": "仅作判断",
                })
                value = value.model_copy(update={"slides": [slide]})
        return StructuredUtilityResult(
            value=value,
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )

    result = UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)

    assert result.slides[0].intent.required_facts == ["硬交付：事实为1万元。"]
    assert result.slides[0].content_blocks == ["硬交付：事实为1万元。"]
    assert result.slides[0].intent.evidence == result.slides[0].content_blocks
    assert detail_calls == 2
    assert len(retry_directives) == 1
    assert "硬 claim" in retry_directives[0]
    assert missing_claim_id in retry_directives[0]
    assert "content_blocks=1" in retry_directives[0]
    assert "claim_groups=0" in retry_directives[0]
    assert "1万元" not in retry_directives[0]
    assert [
        payload["target_slides"][0]["claim_grouping_contract"]
        for payload in detail_payloads
    ] == [{
        "required_claim_ids": [missing_claim_id],
        "each_required_id_exactly_once": True,
        "unknown_claim_ids_forbidden": True,
        "claim_group_count_must_equal_content_block_count": True,
    }] * 2


def test_semantic_planner_retries_before_after_with_more_than_two_units(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="staged-before-after-cardinality-test",
        user_request="制作一页前后对照",
        slide_count=1,
        materials=[InputMaterial(
            name="对照.md",
            content="# 对照\n原状态与新状态来自同一份材料。",
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    claim = plan.slides[0].content_blocks[0]
    detail_calls = 0
    retry_directives: list[str] = []

    async def fake_utility(**kwargs):
        nonlocal detail_calls
        value = _semantic_value_for_call(kwargs, plan)
        if kwargs["result_model"].__name__ == "_SemanticSlideBatch":
            detail_calls += 1
            payload = json.loads(str(kwargs["user_prompt"]))
            if "retry_directive" in payload:
                retry_directives.append(str(payload["retry_directive"]))
            blocks = [claim, claim, claim] if detail_calls == 1 else [claim, claim]
            groups = list(value.slides[0].claim_groups)
            if detail_calls == 2:
                groups.append(groups[0].model_copy(update={"claim_ids": []}))
            slide = value.slides[0].model_copy(update={
                "relationship_type": RelationshipType.BEFORE_AFTER,
                "content_blocks": blocks,
                "claim_groups": groups,
            })
            value = value.model_copy(update={"slides": [slide]})
        return StructuredUtilityResult(
            value=value,
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )

    result = UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)
    deck = build_deck_ir(request, result)

    assert len(deck.slides) == 1
    assert result.slides[0].content_blocks == [claim, claim]
    assert detail_calls == 2
    assert len(retry_directives) == 1
    assert "两项" in retry_directives[0]


def test_semantic_planner_retries_before_after_until_all_claims_are_explicitly_grouped(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="staged-before-after-claim-carry-test",
        user_request="制作一页前后对照",
        slide_count=1,
        materials=[InputMaterial(
            name="对照.md",
            content=(
                "# 对照\n旧状态保持人工流转。新状态改为统一入口。"
                "边界条件仍需人工确认。"
            ),
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    claims = [
        "旧状态保持人工流转。",
        "新状态改为统一入口。",
        "边界条件仍需人工确认。",
    ]
    plan.slides[0].content_blocks = claims
    detail_calls = 0
    retry_directives: list[str] = []

    async def fake_utility(**kwargs):
        nonlocal detail_calls
        value = _semantic_value_for_call(kwargs, plan)
        if kwargs["result_model"].__name__ == "_SemanticSlideBatch":
            detail_calls += 1
            payload = json.loads(str(kwargs["user_prompt"]))
            if "retry_directive" in payload:
                retry_directives.append(str(payload["retry_directive"]))
            groups = list(value.slides[0].claim_groups)
            if detail_calls == 1:
                groups = groups[:2]
            else:
                groups = [
                    groups[0],
                    groups[1].model_copy(update={
                        "claim_ids": [
                            *groups[1].claim_ids,
                            *groups[2].claim_ids,
                        ],
                    }),
                ]
            slide = value.slides[0].model_copy(update={
                "relationship_type": RelationshipType.BEFORE_AFTER,
                "content_blocks": ["原状态", "新状态与边界"],
                "claim_groups": groups,
                "takeaway": "",
            })
            value = value.model_copy(update={"slides": [slide]})
        return StructuredUtilityResult(
            value=value,
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )

    result = UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)
    deck = build_deck_ir(request, result)

    assert len(deck.slides) == 1
    assert len(result.slides[0].content_blocks) == 2
    assert all(
        claim in "".join(result.slides[0].content_blocks)
        for claim in claims
    )
    assert result.slides[0].takeaway == ""
    assert result.slides[0].intent.required_facts == claims
    assert detail_calls == 2
    assert len(retry_directives) == 1
    assert "claim_groups" in retry_directives[0]


def test_semantic_planner_retries_oversegmented_page_before_compose(
    monkeypatch,
) -> None:
    facts = [f"事实{label}必须保留。" for label in "甲乙丙丁戊己庚辛"]
    request = PresentationJobRequest(
        conversation_id="staged-composition-capacity-test",
        user_request="制作一页完整判断",
        slide_count=1,
        materials=[InputMaterial(
            name="判断.md",
            content="# 判断\n" + "".join(facts),
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    plan.slides[0].content_blocks = facts
    plan.slides[0].takeaway = "全部事实需要共同支持最终判断。"
    plan.slides[0].intent.relationship_type = RelationshipType.SUMMARY
    detail_calls = 0
    retry_directives: list[str] = []

    async def fake_utility(**kwargs):
        nonlocal detail_calls
        value = _semantic_value_for_call(kwargs, plan)
        if kwargs["result_model"].__name__ == "_SemanticSlideBatch":
            detail_calls += 1
            payload = json.loads(str(kwargs["user_prompt"]))
            if "retry_directive" in payload:
                retry_directives.append(str(payload["retry_directive"]))
            if detail_calls == 2:
                combined = [
                    "".join(facts[index:index + 2])
                    for index in range(0, len(facts), 2)
                ]
                groups = [
                    value.slides[0].claim_groups[index].model_copy(update={
                        "claim_ids": [
                            *value.slides[0].claim_groups[index].claim_ids,
                            *value.slides[0].claim_groups[index + 1].claim_ids,
                        ],
                    })
                    for index in range(0, len(facts), 2)
                ]
                slide = value.slides[0].model_copy(
                    update={
                        "content_blocks": combined,
                        "claim_groups": groups,
                    },
                )
                value = value.model_copy(update={"slides": [slide]})
        return StructuredUtilityResult(
            value=value,
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )

    result = UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)
    deck = build_deck_ir(request, result)

    assert len(deck.slides) == 1
    assert len(result.slides[0].content_blocks) == 4
    assert all(
        fact in "".join(result.slides[0].content_blocks)
        for fact in facts
    )
    assert detail_calls == 2
    assert len(retry_directives) == 1
    assert "构图容量" in retry_directives[0]
    assert "更少" in retry_directives[0]
    assert not any(fact in retry_directives[0] for fact in facts)


def test_semantic_planner_materializes_exact_claims_from_author_groups(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="staged-explicit-claim-group-test",
        user_request="制作一页完整判断",
        slide_count=1,
        materials=[InputMaterial(
            name="判断.md",
            content="# 判断\n硬交付：事实为1万元。",
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    plan.slides[0].content_blocks = ["硬交付：事实为1万元。"]

    async def fake_utility(**kwargs):
        value = _semantic_value_for_call(kwargs, plan)
        if kwargs["result_model"].__name__ == "_SemanticSlideBatch":
            slide = value.slides[0].model_copy(update={
                "content_blocks": ["事实落点"],
            })
            value = value.model_copy(update={"slides": [slide]})
        return StructuredUtilityResult(
            value=value,
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )

    result = UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)

    assert result.slides[0].content_blocks == [
        "事实落点\n硬交付：事实为1万元。",
    ]
    assert result.slides[0].intent.required_facts == [
        "硬交付：事实为1万元。",
    ]


def test_semantic_planner_retries_detail_that_changes_chart_contract(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="staged-chart-contract-test",
        user_request="制作一页原生图表",
        slide_count=1,
        materials=[InputMaterial(
            name="数据.md",
            content="# 数据\n甲为42万元，乙为38万元。",
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    plan.slides[0].charts = [ChartObject(
        chart_type="column",
        categories=["甲", "乙"],
        series={"收入": [42, 38]},
        unit="万元",
        derivation="直接摘录材料原值。",
        source_refs=[brief.source_refs[0]],
    )]
    detail_calls = 0
    retry_directives: list[str] = []

    async def fake_utility(**kwargs):
        nonlocal detail_calls
        value = _semantic_value_for_call(kwargs, plan)
        if kwargs["result_model"].__name__ == "_SemanticSlideBatch":
            detail_calls += 1
            payload = json.loads(str(kwargs["user_prompt"]))
            if "retry_directive" in payload:
                retry_directives.append(str(payload["retry_directive"]))
            if detail_calls == 1:
                slide = value.slides[0].model_copy(update={"charts": []})
                value = value.model_copy(update={"slides": [slide]})
        return StructuredUtilityResult(
            value=value,
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )

    result = UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)

    assert len(result.slides[0].charts) == 1
    assert detail_calls == 2
    assert len(retry_directives) == 1
    assert "chart_contracts" in retry_directives[0]
    assert "42" not in retry_directives[0]


def test_semantic_planner_does_not_retry_non_schema_failure(monkeypatch) -> None:
    request = PresentationJobRequest(
        conversation_id="staged-no-content-retry-test",
        user_request="制作一页判断",
        slide_count=1,
        materials=[InputMaterial(name="判断.md", content="# 判断\n事实为1万元。")],
    )
    brief = build_brief(request)
    call_count = 0

    async def fake_utility(**_kwargs):
        nonlocal call_count
        call_count += 1
        raise ValueError("content validation must fail closed")

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")

    with pytest.raises(ValueError, match="content validation must fail closed"):
        planner.plan(request, brief)

    assert call_count == 1
    assert planner.last_utility_call_count == 1
    assert planner.last_stage_attempt_counts == {"claims": 1}


def test_semantic_planner_deterministically_orders_complete_detail_batch(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="staged-permuted-merge-test",
        user_request="制作四页判断",
        slide_count=4,
        materials=[InputMaterial(
            name="判断.md",
            content="\n\n".join(
                f"# 判断{i}\n事实{i}为{i}万元。" for i in range(1, 5)
            ),
        )],
    )
    brief = build_brief(request)
    base_plan = build_deterministic_fixture_plan(request, brief)
    base = base_plan.slides[0]
    plan = base_plan.model_copy(update={"slides": [
        base.model_copy(update={"order": order, "title": f"第{order}页"})
        for order in range(1, 5)
    ]})

    async def fake_utility(**kwargs):
        value = _semantic_value_for_call(kwargs, plan)
        if str(kwargs["purpose"]).startswith("presentation-semantic-detail"):
            value = type(value).model_validate({
                "slides": list(reversed(value.model_dump(mode="json")["slides"])),
            })
        return StructuredUtilityResult(
            value=value,
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )

    result = UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)

    assert [slide.order for slide in result.slides] == [1, 2, 3, 4]
    assert [slide.title for slide in result.slides] == [
        "第1页", "第2页", "第3页", "第4页",
    ]


def test_semantic_planner_fails_closed_when_outline_misses_requested_count(
    monkeypatch,
) -> None:
    request = PresentationJobRequest(
        conversation_id="staged-outline-count-test",
        user_request="制作四页判断",
        slide_count=4,
        materials=[InputMaterial(
            name="判断.md",
            content="\n\n".join(
                f"# 判断{i}\n事实{i}为{i}万元。" for i in range(1, 5)
            ),
        )],
    )
    brief = build_brief(request)
    full_plan = build_deterministic_fixture_plan(request, brief)
    short_plan = full_plan.model_copy(update={"slides": full_plan.slides[:3]})

    async def fake_utility(**kwargs):
        return StructuredUtilityResult(
            value=_semantic_value_for_call(kwargs, short_plan),
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )

    with pytest.raises(ValueError, match="exact requested slide count"):
        UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)


@pytest.mark.parametrize(
    "returned_orders",
    [[1, 2, 3], [1, 2, 3, 3], [1, 2, 3, 4, 4]],
)
def test_semantic_planner_fails_closed_on_incomplete_or_duplicate_detail_batch(
    monkeypatch,
    returned_orders: Sequence[int],
) -> None:
    request = PresentationJobRequest(
        conversation_id="staged-merge-guard-test",
        user_request="制作四页判断",
        slide_count=4,
        materials=[InputMaterial(
            name="判断.md",
            content="\n\n".join(
                f"# 判断{i}\n事实{i}为{i}万元。" for i in range(1, 5)
            ),
        )],
    )
    brief = build_brief(request)
    base_plan = build_deterministic_fixture_plan(request, brief)
    base = base_plan.slides[0]
    plan = base_plan.model_copy(update={"slides": [
        base.model_copy(update={"order": order, "title": f"第{order}页"})
        for order in range(1, 5)
    ]})

    async def fake_utility(**kwargs):
        value = _semantic_value_for_call(kwargs, plan)
        if str(kwargs["purpose"]).startswith("presentation-semantic-detail"):
            detail_type = kwargs["result_model"]
            source = {slide.order: slide for slide in plan.slides}
            # Keep the deliberately wrong batch structurally valid so this
            # lock reaches the exact-order guard instead of failing earlier
            # on the independent non-empty claim-group schema contract.
            returned_slides = [
                source[order].model_copy(update={
                    "content_blocks": [source[order].content_blocks[0]],
                })
                for order in returned_orders
            ]
            value = detail_type.from_runtime(
                returned_slides,
                claim_ids_by_order={
                    order: [f"claim-{order:03d}"]
                    for order in set(returned_orders)
                },
            )
        return StructuredUtilityResult(
            value=value,
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )
    with pytest.raises(
        ValueError,
        match="detail batch.*exact target orders",
    ) as captured:
        UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)
    assert captured.value.presentation_detail_batch_stage == "detail:001-001"
    assert captured.value.presentation_detail_target_orders == [1]
    assert captured.value.presentation_detail_returned_orders == returned_orders


def test_utility_page_author_uses_existing_model_route(monkeypatch) -> None:
    request = PresentationJobRequest(
        conversation_id="page-author-test",
        user_request="制作收入判断页",
        slide_count=1,
        materials=[InputMaterial(name="事实.md", content="# 事实\n甲为42万元。")],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    scaffold = build_deck_ir(request, plan).slides[0]
    calls = []

    async def fake_utility(**kwargs):
        calls.append(kwargs)
        result_model = kwargs["result_model"]
        return StructuredUtilityResult(
            value=result_model.model_validate({
                # Providers may echo the former full-slide contract.  These
                # runtime-owned fields must never override the scaffold.
                "id": "model-changed-slide-id",
                "layout_id": "model-changed-layout",
                "semantic_layout": "model-changed-page-type",
                "intent_id": "model-changed-intent",
                "composition_authority": "design_system",
                "revision": 99,
                "editability_mode": "image-slide",
                "source_fact_ids": ["model-invented-fact"],
                "asset_ids": ["model-invented-asset"],
                "scene_hash": "model-invented-hash",
                # Only these authored-scene fields belong to the model.
                "objects": [
                    item.model_dump(mode="json") for item in scaffold.objects
                ],
                "speaker_notes": "保留核心判断，图表用于比较。",
                "object_groups": [],
                "relationships": [],
            }),
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )
    author = UtilityPresentationPageAuthor("agent-1")
    result = author.author(request, brief, plan, plan.slides[0], scaffold)
    assert result.composition_authority == CompositionAuthority.AUTHOR
    assert result.id == scaffold.id
    assert result.layout_id == "freeform"
    assert result.semantic_layout == scaffold.provenance_layout
    assert result.intent_id == scaffold.intent_id
    assert result.revision == scaffold.revision
    assert result.editability_mode == scaffold.editability_mode
    assert result.source_fact_ids == scaffold.source_fact_ids
    assert result.asset_ids == scaffold.asset_ids
    assert result.scene_hash is None
    assert result.speaker_notes == "保留核心判断，图表用于比较。"
    assert author.last_model_label == "approved-provider/approved-model"
    assert calls[0]["purpose"] == "presentation-page-author:slide-001"
    assert calls[0]["result_model"] is not DeckSlide
    assert set(calls[0]["result_model"].model_fields) == {
        "objects",
        "speaker_notes",
        "object_groups",
        "relationships",
        "template_anchors",
    }
    assert result.template_anchors is None
    assert calls[0]["result_model"].model_fields["template_anchors"].default is None
    assert calls[0]["agent_id"] == "agent-1"
    assert calls[0]["usage_session_id"] == request.conversation_id
    assert calls[0]["timeout_seconds"] == 360
    assert calls[0]["first_output_timeout_seconds"] == 120
    assert calls[0]["idle_timeout_seconds"] == 60

    author_schema = calls[0]["result_model"]
    with pytest.raises(ValueError, match="below the 32 pt.*readability floor"):
        author_schema.model_validate({
            "objects": [{
                "kind": "text",
                "id": "small-title",
                "role": "title",
                "text": "收入判断",
                "font_size": 28,
            }],
        })
    with pytest.raises(ValueError, match="rich text run.*17 pt.*readability floor"):
        author_schema.model_validate({
            "objects": [{
                "kind": "text",
                "id": "small-run",
                "role": "body",
                "text": "甲为42万元",
                "font_size": 17,
                "runs": [{"text": "甲为42万元", "font_size": 12}],
            }],
        })

    author.author(
        request,
        brief,
        plan,
        plan.slides[0],
        scaffold,
        retry_directive="仅修复事实门禁，不得新增数字。",
    )
    retry_payload = json.loads(calls[1]["user_prompt"])
    assert retry_payload["retry_directive"] == (
        "仅修复事实门禁，不得新增数字。"
    )
    assert retry_payload["fact_visibility_contract"] == {
        "facts": [
            *(
                {"path": f"required_facts[{index}]", "text": fact}
                for index, fact in enumerate(
                    plan.slides[0].intent.required_facts,
                )
            ),
            *(
                {"path": f"immutable_facts[{index}]", "text": fact}
                for index, fact in enumerate(
                    plan.slides[0].intent.immutable_facts,
                )
            ),
        ],
        "must_appear_verbatim_in_visible_native_objects": True,
        "speaker_notes_do_not_satisfy": True,
    }
    assert retry_payload["speaker_notes_policy"] == {
        "must_be_numeric_free": True,
        "return_empty_string_if_uncertain": True,
    }
    numeric_contract = retry_payload["page_numeric_contract"]
    assert numeric_contract == {
        "allowed_numeric_tokens": page_allowed_numeric_tokens(plan.slides[0]),
        "forbid_unlisted_numbers": True,
        "speaker_notes_must_be_numeric_free": True,
        "forbid_decorative_numbering": True,
        "forbid_page_numbering": True,
    }
    # Keep the complete payload assertion: the wire contract now additionally
    # exposes the strict schema's fields, literal values and atomic margins.
    scene_schema = author_schema.model_json_schema()
    scene_definitions = scene_schema["$defs"]
    object_mapping = scene_schema["properties"]["objects"]["items"]["discriminator"]["mapping"]
    assert retry_payload["native_scene_contract"] == {
        "object_types_by_kind": {
            kind: {
                "required_fields": list(dict.fromkeys([
                    "kind", *scene_definitions[reference.rsplit("/", 1)[-1]].get("required", []),
                ])),
                "allowed_fields": list(scene_definitions[reference.rsplit("/", 1)[-1]]["properties"]),
                "literal_values": {
                    field: (
                        definition["enum"]
                        if "enum" in definition else [definition["const"]]
                    )
                    for field, definition in scene_definitions[
                        reference.rsplit("/", 1)[-1]
                    ]["properties"].items()
                    if "enum" in definition or "const" in definition
                },
                "unknown_fields_allowed": False,
            }
            for kind, reference in object_mapping.items()
        },
        "object_group_contract": {
            "required_fields": ["role", "object_ids"],
            "allowed_fields": list(scene_definitions["ObjectGroup"]["properties"]),
            "unknown_fields_allowed": False,
        },
        "native_shape_style_contract": {
            "allowed_fields": list(scene_definitions["NativeShapeStyle"]["properties"]),
            "unknown_fields_allowed": False,
            "margin_contract": {
                "fields": ["margin_top", "margin_right", "margin_bottom", "margin_left"],
                "unit": "points",
                "minimum": 0,
                "maximum": 144,
                "all_non_null_or_all_absent_or_null": True,
            },
        },
        "canvas_inches": {"width": 13.333, "height": 7.5},
        "frame_bounds": {
            "x": {"min": 0, "max": 13.333},
            "y": {"min": 0, "max": 7.5},
            "w": {"exclusive_min": 0, "max": 13.333},
            "h": {"min": 0, "max": 7.5},
        },
        "frame_extent_bounds": {
            "x_plus_w": {"max": 13.3335},
            "y_plus_h": {"max": 7.5005},
        },
        "font_size_bounds": {
            "text_object": {
                "min": 17,
                "title_min": 32,
                "max": 72,
                "nullable": True,
            },
            "shape_object": {"min": 17, "max": 72},
            "rich_text_run": {
                "min": 17,
                "title_min": 32,
                "max": 72,
                "nullable": True,
            },
        },
        "references_must_target_returned_object_ids": True,
    }


@pytest.mark.parametrize("retry_directive", [None, "保留事实，仅修正空间层级。"])
def test_page_author_preserves_source_numeric_meaning_in_model_input_and_cache(
    monkeypatch, retry_directive,
) -> None:
    request = PresentationJobRequest(
        conversation_id="source-numeric-meaning-test",
        user_request="把流程说明做成可编辑页面，不新增测量数据。",
        slide_count=1,
        materials=[InputMaterial(
            name="合成流程.md",
            content="1. 明确责任人。\n2. 核对交接信息。\n这是一份操作流程，不含指标数据。",
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    scaffold = build_deck_ir(request, plan).slides[0]
    calls = []

    async def fake_transport(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            text=json.dumps({
                "objects": [obj.model_dump(mode="json") for obj in scaffold.objects],
                "speaker_notes": "",
                "object_groups": [],
                "relationships": [],
            }, ensure_ascii=False),
            model_label="synthetic/model",
        )

    # Substitute only the external model transport. The public author, real
    # structured parser and authored-scene schema remain in the exercised path.
    monkeypatch.setattr(
        "lantai.app.structured_utility.run_utility_text_task", fake_transport,
    )
    author = UtilityPresentationPageAuthor("agent-1")
    result = author.author(
        request, brief, plan, plan.slides[0], scaffold,
        **({"retry_directive": retry_directive} if retry_directive else {}),
    )
    assert result.id == scaffold.id
    payload = json.loads(calls[0]["user_prompt"])
    semantics = payload.get("source_semantics_contract")
    assert semantics is not None
    assert semantics["numeric_membership_is_not_fact_authorization"] is True
    assert semantics["numeric_context_must_preserve"] == [
        "semantic_role", "entity", "unit", "period", "meaning",
    ]
    assert semantics["numeric_roles_must_be_interpreted_from_source_by_model"] is True
    assert semantics["ordinal_page_or_count_tokens_are_not_measurement_evidence"] is True
    assert "数字白名单不等于事实授权" in calls[0]["system_prompt"]
    assert "source_semantics_contract" in calls[0]["system_prompt"]
    dependencies = author.cache_dependencies(
        request, brief, plan, plan.slides[0], scaffold,
    )
    assert dependencies["user_payload"]["source_semantics_contract"] == semantics
    assert dependencies["system_prompt"].strip() == (
        calls[0]["system_prompt"].split("\n\n只输出一个符合该 schema")[0]
    )
    assert payload["brief"] == brief.model_dump(mode="json")
    assert payload["storyboard"] == plan.slides[0].model_dump(mode="json")
    assert payload.get("retry_directive") == retry_directive


@pytest.mark.parametrize("illustration_authorized", [False, True])
def test_page_author_receives_source_bound_example_policy_without_local_classification(
    monkeypatch, illustration_authorized,
) -> None:
    instruction = (
        "允许增加明确标为演示的虚构示例，但不得声称来自材料。"
        if illustration_authorized else "仅用材料，不添加虚构示例。"
    )
    request = PresentationJobRequest(
        conversation_id="source-bound-example-test",
        user_request=f"制作可编辑的信息表达页。{instruction}",
        slide_count=1,
        materials=[InputMaterial(
            name="合成表达原则.md",
            content="对照示例应体现所讲的表达差异，不以说明文字代替实际对照。",
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    scaffold = build_deck_ir(request, plan).slides[0]
    calls = []

    async def fake_transport(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            text=json.dumps({
                "objects": [obj.model_dump(mode="json") for obj in scaffold.objects],
                "speaker_notes": "",
                "object_groups": [],
                "relationships": [],
            }, ensure_ascii=False),
            model_label="synthetic/model",
        )

    monkeypatch.setattr(
        "lantai.app.structured_utility.run_utility_text_task", fake_transport,
    )
    author = UtilityPresentationPageAuthor("agent-1")
    author.author(request, brief, plan, plan.slides[0], scaffold)
    payload = json.loads(calls[0]["user_prompt"])
    semantics = payload["source_semantics_contract"]
    assert semantics.get("fictional_examples_require_user_authorization_and_visible_label") is True
    assert semantics.get("example_authorization_must_be_interpreted_by_model") is True
    assert semantics.get("visible_example_must_match_its_stated_claim") is True
    assert payload["request"]["user_request"] == request.user_request
    assert "示例的实际呈现必须符合它声称演示的关系" in calls[0]["system_prompt"]
    assert author.cache_dependencies(
        request, brief, plan, plan.slides[0], scaffold,
    )["user_payload"]["source_semantics_contract"] == semantics


@pytest.mark.parametrize(
    ("empty_scene", "expected_issue"),
    [
        ({"objects": []}, "too_short@$.objects"),
        ({}, "missing@$.objects"),
        ({"id": "legacy-echo", "layout_id": "freeform"}, "missing@$.objects"),
    ],
)
def test_page_author_rejects_empty_scene_at_actual_structured_boundary(
    monkeypatch, empty_scene, expected_issue,
) -> None:
    request = PresentationJobRequest(
        conversation_id="empty-author-scene-test",
        user_request="制作收入判断页",
        slide_count=1,
        materials=[InputMaterial(name="合成事实.md", content="甲为42万元。")],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    scaffold = build_deck_ir(request, plan).slides[0]
    valid_scene = {
        "objects": [obj.model_dump(mode="json") for obj in scaffold.objects],
        "speaker_notes": "",
        "object_groups": [],
        "relationships": [],
    }
    responses = iter([empty_scene, valid_scene])
    calls = []

    async def fake_transport(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            text=json.dumps(next(responses), ensure_ascii=False),
            model_label="approved-provider/approved-model",
        )

    # Keep UtilityPresentationPageAuthor and the real structured parser intact;
    # substitute only the final transport, never the schema result itself.
    monkeypatch.setattr(
        "lantai.app.structured_utility.run_utility_text_task", fake_transport,
    )
    author = UtilityPresentationPageAuthor("agent-1")
    with pytest.raises(StructuredTaskValidationError) as captured:
        author.author(request, brief, plan, plan.slides[0], scaffold)
    assert expected_issue in captured.value.validation_issues
    result = author.author(
        request, brief, plan, plan.slides[0], scaffold,
        retry_directive="objects 必须包含完整的可编辑页面对象，不得返回空列表。",
    )
    assert result.objects
    assert [obj.model_dump(mode="json") for obj in result.objects] == valid_scene["objects"]
    assert result.id == scaffold.id
    assert len(calls) == 2
    for call in calls:
        assert call["timeout_seconds"] == 360
        assert call["first_output_timeout_seconds"] == 120
        assert call["idle_timeout_seconds"] == 60
        assert '"minItems":1' in call["system_prompt"]
    assert author.cache_dependencies(
        request, brief, plan, plan.slides[0], scaffold,
    )["system_prompt"].strip() == calls[0]["system_prompt"].split("\n\n只输出一个符合该 schema")[0]


@pytest.mark.parametrize("retry_directive", [None, "仅修复几何，不改事实。"])
def test_page_author_receives_correlated_frame_bounds_in_real_call_and_cache(
    monkeypatch, retry_directive,
) -> None:
    request = PresentationJobRequest(
        conversation_id="correlated-frame-contract-test",
        user_request="制作收入判断页",
        slide_count=1,
        materials=[InputMaterial(name="合成事实.md", content="甲为42万元。")],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    scaffold = build_deck_ir(request, plan).slides[0]
    calls = []

    async def fake_utility(**kwargs):
        calls.append(kwargs)
        return StructuredUtilityResult(
            value=kwargs["result_model"].model_validate({
                "objects": [obj.model_dump(mode="json") for obj in scaffold.objects],
                "speaker_notes": "",
                "object_groups": [],
                "relationships": [],
            }),
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_utility,
    )
    author = UtilityPresentationPageAuthor("agent-1")
    result = author.author(
        request, brief, plan, plan.slides[0], scaffold,
        **({"retry_directive": retry_directive} if retry_directive else {}),
    )
    assert result.id == scaffold.id
    assert len(calls) == 1
    payload = json.loads(calls[0]["user_prompt"])
    contract = payload["native_scene_contract"]
    assert contract.get("frame_extent_bounds") == {
        "x_plus_w": {"max": 13.3335},
        "y_plus_h": {"max": 7.5005},
    }
    assert "x+w" in calls[0]["system_prompt"]
    assert "y+h" in calls[0]["system_prompt"]
    dependencies = author.cache_dependencies(
        request, brief, plan, plan.slides[0], scaffold,
    )
    assert dependencies["user_payload"]["native_scene_contract"] == contract
    assert dependencies["system_prompt"] == calls[0]["system_prompt"]
    assert calls[0]["timeout_seconds"] == 360
    assert calls[0]["first_output_timeout_seconds"] == 120
    assert calls[0]["idle_timeout_seconds"] == 60

    # The same actual authored-scene schema still rejects correlated overflow
    # and zero width. Advertising constraints must not relax the validator.
    for frame, expected_error in [
        ({"x": 12, "y": 1, "w": 2, "h": 1}, "value_error"),
        ({"x": 1, "y": 7, "w": 1, "h": 1}, "value_error"),
        ({"x": 1, "y": 1, "w": 0, "h": 2}, "greater_than"),
    ]:
        with pytest.raises(ValueError) as captured:
            calls[0]["result_model"].model_validate({
                "objects": [{
                    "kind": "shape", "id": "invalid-shape", "shape_type": "rect",
                    "frame": frame,
                }],
            })
        assert any(error["type"] == expected_error for error in captured.value.errors())


def test_utility_repair_designer_receives_classified_action_bounds(
    monkeypatch,
) -> None:
    planned, slide = _case()
    replacement = slide.model_copy(
        update={
            "objects": [
                obj.model_copy(update={"text": "甲为42万元；乙为38万元"})
                if isinstance(obj, TextObject) and obj.role == "body"
                else obj
                for obj in slide.objects
            ],
        },
    )
    calls = []

    async def fake_utility(**kwargs):
        calls.append(kwargs)
        return StructuredUtilityResult(
            value=kwargs["result_model"](
                slide=replacement,
                action=RepairActionType.REWRITE_TEXT,
                rationale="restore the missing claim",
            ),
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )
    request = SlideRedesignRequest(
        deck_id="deck-1",
        slide=slide,
        intent=planned.intent,
        planned_slide=planned,
        visual_direction=None,
        design_context=None,
        issues=(),
        immutable_facts=("甲为42万元", "乙为38万元"),
        visible_immutable_facts=(
            ("immutable_facts[0]", "甲为42万元"),
        ),
        deck_context=("收入判断",),
        failure_routes=(
            PresentationFailureRoute(
                category=PresentationFailureCategory.CONTENT,
                recovery_action=PresentationRecoveryAction.REVISE_CONTENT,
                issue_codes=["content.required_fact_missing"],
                slide_ids=[slide.id],
                retry_stage=JobStage.REPAIR,
            ),
        ),
        allowed_actions=(
            RepairActionType.REWRITE_TEXT,
            RepairActionType.COMPRESS_TEXT,
        ),
        usage_session_id="conversation-1",
    )

    proposal = UtilityPresentationRepairDesigner("agent-1").redesign(request)

    payload = json.loads(calls[0]["user_prompt"])
    assert payload["failure_routes"][0]["category"] == "content"
    assert payload["failure_routes"][0]["recovery_action"] == (
        "revise_content"
    )
    assert payload["allowed_actions"] == ["rewrite_text", "compress_text"]
    assert payload["fact_visibility_contract"] == {
        "facts": [
            {"path": "immutable_facts[0]", "text": "甲为42万元"},
        ],
        "must_appear_verbatim_in_visible_native_objects": True,
        "speaker_notes_do_not_satisfy": True,
    }
    assert "allowed_actions" in calls[0]["system_prompt"]
    assert "fact_visibility_contract" in calls[0]["system_prompt"]
    assert "speaker_notes 必须完全无数字" in calls[0]["system_prompt"]
    assert "additional_slides" not in calls[0]["result_model"].model_fields
    assert "plan_slides" not in calls[0]["result_model"].model_fields
    assert proposal.action == RepairActionType.REWRITE_TEXT


def test_page_local_repair_prompt_forbids_even_empty_structural_fields(
    monkeypatch,
) -> None:
    planned, slide = _case()
    calls = []

    async def fake_utility(**kwargs):
        calls.append(kwargs)
        return StructuredUtilityResult(
            value=kwargs["result_model"](slide=slide),
            model_label="synthetic/model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )
    UtilityPresentationRepairDesigner("agent-1").redesign(
        SlideRedesignRequest(
            deck_id="deck-1",
            slide=slide,
            intent=planned.intent,
            planned_slide=planned,
            visual_direction=None,
            design_context=None,
            issues=(),
            immutable_facts=(),
            deck_context=(),
            structural_repair_allowed=False,
        ),
    )

    prompt = calls[0]["system_prompt"]
    assert "仅允许 slide、action、rationale" in prompt
    assert "禁止输出 additional_slides 或 plan_slides，即使是空数组" in prompt
    # Prompt correction does not weaken the actual schema boundary.
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        calls[0]["result_model"](
            slide=slide,
            additional_slides=[],
            plan_slides=[],
        )


def test_utility_repair_designer_only_exposes_page_additions_when_authorized(
    monkeypatch,
) -> None:
    planned, slide = _case()
    calls = []

    async def fake_utility(**kwargs):
        calls.append(kwargs)
        return StructuredUtilityResult(
            value=kwargs["result_model"](
                slide=slide,
                action=RepairActionType.REDESIGN_SLIDE,
                rationale="keep the repair page-local",
            ),
            model_label="approved-provider/approved-model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_utility,
    )
    request = SlideRedesignRequest(
        deck_id="deck-1",
        slide=slide,
        intent=planned.intent,
        planned_slide=planned,
        visual_direction=None,
        design_context=None,
        issues=(),
        immutable_facts=(),
        deck_context=("收入判断",),
        allowed_actions=(RepairActionType.REDESIGN_SLIDE,),
        structural_repair_allowed=True,
    )

    proposal = UtilityPresentationRepairDesigner("agent-1").redesign(request)

    assert "additional_slides" in calls[0]["result_model"].model_fields
    assert "plan_slides" in calls[0]["result_model"].model_fields
    assert proposal.additional_slides == ()
    assert proposal.plan_slides == ()


def test_utility_author_and_critic_cache_identity_tracks_model_slots(
    monkeypatch,
) -> None:
    slots = {
        "mode": "dedicated",
        "dedicated": SimpleNamespace(provider_id="vendor", model="model-a"),
        "active": SimpleNamespace(provider_id="backup", model="fallback-a"),
        "available": True,
        "active_available": True,
    }
    monkeypatch.setattr(
        "lantai.presentation.intelligence.ProviderManager.get_instance",
        lambda: SimpleNamespace(
            get_active_model=lambda: None,
            is_model_slot_available=lambda slot: (
                slots["available"]
                if slot is slots["dedicated"] else slots["active_available"]
            ),
        ),
    )
    monkeypatch.setattr(
        "lantai.presentation.intelligence.load_config",
        lambda: SimpleNamespace(
            utility_intelligence=SimpleNamespace(
                mode=slots["mode"], model=slots["dedicated"],
            ),
        ),
    )
    monkeypatch.setattr(
        "lantai.presentation.intelligence.load_agent_config",
        lambda _agent_id: SimpleNamespace(active_model=slots["active"]),
    )
    author = UtilityPresentationPageAuthor("agent-1")
    critic = UtilityPresentationVisualCritic("agent-1")
    original = (author.cache_identity(), critic.cache_identity())
    assert "page-and-deck-v7" in original[1]
    assert "page-and-deck-v6" in critic.slide_cache_identity()
    assert "page-and-deck-v7" in critic.deck_cache_identity()
    slots["dedicated"] = SimpleNamespace(provider_id="vendor", model="model-b")
    updated = (author.cache_identity(), critic.cache_identity())
    assert updated[0] != original[0]
    assert updated[1] != original[1]
    slots["active"] = SimpleNamespace(provider_id="backup", model="fallback-b")
    fallback_updated = (author.cache_identity(), critic.cache_identity())
    assert fallback_updated[0] != updated[0]
    assert fallback_updated[1] != updated[1]
    slots["available"] = False
    unavailable = (author.cache_identity(), critic.cache_identity())
    assert unavailable[0] != fallback_updated[0]
    assert unavailable[1] != fallback_updated[1]
    slots["active_available"] = False
    no_fallback = (author.cache_identity(), critic.cache_identity())
    assert no_fallback[0] != unavailable[0]
    assert no_fallback[1] != unavailable[1]


def test_visual_critic_and_repair_timeout_cover_measured_provider_latency() -> None:
    critic = UtilityPresentationVisualCritic("agent-1")
    repair = UtilityPresentationRepairDesigner("agent-1")

    assert critic.timeout_seconds == 180
    assert repair.timeout_seconds == 180


def test_single_page_critic_uses_foreground_child_lane(monkeypatch, tmp_path):
    captured = {}

    async def capture_review(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            value=kwargs["result_model"].model_validate(synthetic_review_response(kwargs, [])),
            model_label="vision-model",
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_content_task",
        capture_review,
    )
    rendered = tmp_path / "slide.png"
    Image.new("RGB", (320, 180), "white").save(rendered)
    slide = VisualCriticSlideInput(
        slide_id="slide-1",
        slide_index=1,
        rendered_path=str(rendered),
        rendered_sha256=hashlib.sha256(rendered.read_bytes()).hexdigest(),
        key_message="判断",
        usage_session_id="original-task",
    )
    result = UtilityPresentationVisualCritic("agent-1").review_slide(slide)
    assert result.critic_id == "utility-vision/vision-model"
    assert captured["wait_for_idle"] is False
    assert captured["usage_session_id"] == "original-task"
    assert (
        next(block for block in captured["user_content"] if block["type"] == "image")["source"]["data"]
        == base64.b64encode(rendered.read_bytes()).decode()
    )


@pytest.mark.parametrize(
    ("slide_count", "expected_sheet_count"),
    [(19, 2), (60, 5)],
)
def test_deck_visual_critic_reviews_every_slide_within_media_cap(
    monkeypatch,
    tmp_path,
    slide_count: int,
    expected_sheet_count: int,
) -> None:
    captured: dict[str, object] = {}

    async def capture_review(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            value=kwargs["result_model"].model_validate(synthetic_review_response(kwargs, [])),
            model_label="captured-model",
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_content_task",
        capture_review,
    )
    slides: list[VisualCriticSlideInput] = []
    for slide_index in range(1, slide_count + 1):
        rendered_path = tmp_path / f"slide-{slide_index:03d}.png"
        Image.new(
            "RGB",
            (320, 180),
            (slide_index % 255, 80, 120),
        ).save(rendered_path)
        rendered_bytes = rendered_path.read_bytes()
        slides.append(
            VisualCriticSlideInput(
                slide_id=f"slide-{slide_index:03d}",
                slide_index=slide_index,
                rendered_path=str(rendered_path),
                rendered_sha256=hashlib.sha256(rendered_bytes).hexdigest(),
                key_message=f"第 {slide_index} 页判断",
                object_ids=[f"slide-{slide_index:03d}::title"],
            ),
        )

    result = UtilityPresentationVisualCritic("agent-1").review_deck(
        slides,
        retry_directive=(
            "只使用 JSON Schema 允许的 issue_type 枚举重新审阅。"
        ),
    )

    assert result.issues == []
    assert result.critic_id == "utility-vision/captured-model"
    assert captured["wait_for_idle"] is False
    user_content = captured["user_content"]
    assert isinstance(user_content, list)
    image_blocks = [
        block for block in user_content if block.get("type") == "image"
    ]
    assert len(image_blocks) == expected_sheet_count
    assert len(image_blocks) <= 16
    metadata = json.loads(next(
        block["text"] for block in reversed(user_content) if block["type"] == "text"
    ))
    assert metadata["retry_directive"] == (
        "只使用 JSON Schema 允许的 issue_type 枚举重新审阅。"
    )
    ordered_slide_ids = [
        slide["slide_id"] for slide in metadata["ordered_slides"]
    ]
    sheet_slide_ids = [
        slide_id
        for sheet in metadata["render_evidence"]["sheets"]
        for slide_id in sheet["slide_ids"]
    ]
    assert ordered_slide_ids == [slide.slide_id for slide in slides]
    assert sheet_slide_ids == ordered_slide_ids
    for block in image_blocks:
        encoded = block["source"]["data"]
        contact_sheet = Image.open(BytesIO(base64.b64decode(encoded)))
        contact_sheet.verify()


def test_utility_visual_critic_cancels_the_provider_coroutine(
    monkeypatch,
) -> None:
    critic = UtilityPresentationVisualCritic("agent-1")
    provider_cancelled = threading.Event()

    async def wait_for_cancel(_slide):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            provider_cancelled.set()
            raise

    monkeypatch.setattr(critic, "_review_slide", wait_for_cancel)
    checks = 0

    def ensure_active() -> None:
        nonlocal checks
        checks += 1
        if checks >= 2:
            raise PresentationJobInterruptedError("presentation job is cancelled")

    with pytest.raises(PresentationJobInterruptedError, match="cancelled"):
        critic.review_slide_cancellable(object(), ensure_active)

    assert provider_cancelled.is_set()


def test_visual_critic_scopes_page_local_object_ids_without_fuzzy_matching() -> None:
    slide = VisualCriticSlideInput(
        slide_id="slide-009",
        slide_index=9,
        rendered_path="/tmp/slide-9.png",
        rendered_sha256="a" * 64,
        key_message="执行建议",
        object_ids=[
            "slide-009::rec_method",
            "slide-009::rec_primary",
        ],
    )
    result = SimpleNamespace(value=SimpleNamespace(issues=[
        VisualIssue(
            slide_id="slide-009",
            severity="error",
            issue_type="hierarchy",
            object_ids=["rec_method", "slide-009::rec_primary"],
            description="建议层级需要强化",
            proposed_action="调整层级",
        ),
    ]))

    issues = UtilityPresentationVisualCritic._validated_issues(
        result,
        [slide],
    )

    assert issues[0].object_ids == [
        "slide-009::rec_method",
        "slide-009::rec_primary",
    ]


def test_visual_critic_rejects_unknown_or_cross_page_object_ids() -> None:
    slide = VisualCriticSlideInput(
        slide_id="slide-009",
        slide_index=9,
        rendered_path="/tmp/slide-9.png",
        rendered_sha256="a" * 64,
        key_message="执行建议",
        object_ids=["slide-009::rec_method"],
    )
    result = SimpleNamespace(value=SimpleNamespace(issues=[
        VisualIssue(
            slide_id="slide-009",
            severity="error",
            issue_type="hierarchy",
            object_ids=["slide-008::rec_method"],
            description="错误引用了另一页对象",
            proposed_action="不得执行",
        ),
    ]))

    with pytest.raises(ValueError, match="unknown object ids"):
        UtilityPresentationVisualCritic._validated_issues(result, [slide])


@pytest.mark.parametrize("invalid_reference", ["slide", "object"])
def test_visual_critic_reference_errors_are_typed_and_do_not_expose_rejected_ids(
    invalid_reference: str,
) -> None:
    rejected_id = "private-untrusted-reference-do-not-repeat"
    slide = VisualCriticSlideInput(
        slide_id="slide-009",
        slide_index=9,
        rendered_path="/tmp/slide-9.png",
        rendered_sha256="a" * 64,
        key_message="执行建议",
        object_ids=["slide-009::rec_method"],
    )
    issue = VisualIssue(
        slide_id=rejected_id if invalid_reference == "slide" else slide.slide_id,
        severity="error",
        issue_type="hierarchy",
        object_ids=[rejected_id] if invalid_reference == "object" else [],
        description="引用必须来自输入",
        proposed_action="不得执行无效引用",
    )
    result = SimpleNamespace(value=SimpleNamespace(issues=[issue]))

    with pytest.raises(StructuredTaskValidationError) as caught:
        UtilityPresentationVisualCritic._validated_issues(result, [slide])

    field = "slide_id" if invalid_reference == "slide" else "object_ids"
    assert caught.value.validation_issues == (
        f"reference_error@$.issues[0].{field}",
    )
    assert rejected_id not in str(caught.value)
