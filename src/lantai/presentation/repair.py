"""Repair contracts for legacy layout recovery and Presentation v3 redesign."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable, Protocol, Sequence

from ..app.structured_utility import StructuredTaskValidationError
from .failure import failure_routes_for_quality_issues
from .layouts import check_capacity, get_layout
from .models import (
    ChartObject,
    DeckIR,
    DeckObject,
    DeckPlan,
    DeckPlanSlide,
    DeckSlide,
    ImageObject,
    MetricObject,
    PresentationDesignContext,
    PresentationFailureCategory,
    PresentationFailureRoute,
    QualityIssue,
    ShapeObject,
    SlideIntent,
    TableObject,
    TextObject,
    VisualDirection,
)
from .page_authoring import numeric_tokens

MAX_REPAIR_ROUNDS = 3


@dataclass(frozen=True)
class RepairResult:
    deck: DeckIR
    changed: bool
    actions: tuple[str, ...]
    plan: DeckPlan | None = None


class RepairActionType(str, Enum):
    """Semantic operations a v3 repair designer may choose."""

    REWRITE_TEXT = "rewrite_text"
    COMPRESS_TEXT = "compress_text"
    SPLIT_SLIDE = "split_slide"
    MERGE_SLIDE = "merge_slide"
    MOVE_ELEMENT = "move_element"
    RESIZE_ELEMENT = "resize_element"
    CHANGE_VISUAL = "change_visual"
    REGENERATE_IMAGE = "regenerate_image"
    REDRAW_DIAGRAM = "redraw_diagram"
    CHANGE_CHART = "change_chart"
    RECROP_IMAGE = "recrop_image"
    REPLACE_LAYOUT = "replace_layout"
    REDESIGN_SLIDE = "redesign_slide"


_ACTIONS_BY_FAILURE_CATEGORY: dict[
    PresentationFailureCategory,
    tuple[RepairActionType, ...],
] = {
    PresentationFailureCategory.CONTENT: (
        RepairActionType.REWRITE_TEXT,
        RepairActionType.COMPRESS_TEXT,
        RepairActionType.SPLIT_SLIDE,
        RepairActionType.REDRAW_DIAGRAM,
        RepairActionType.CHANGE_CHART,
    ),
    PresentationFailureCategory.GEOMETRY: (
        RepairActionType.MOVE_ELEMENT,
        RepairActionType.RESIZE_ELEMENT,
        RepairActionType.RECROP_IMAGE,
        RepairActionType.REPLACE_LAYOUT,
        RepairActionType.SPLIT_SLIDE,
    ),
    PresentationFailureCategory.AESTHETIC: (
        RepairActionType.MOVE_ELEMENT,
        RepairActionType.RESIZE_ELEMENT,
        RepairActionType.CHANGE_VISUAL,
        RepairActionType.REGENERATE_IMAGE,
        RepairActionType.REDRAW_DIAGRAM,
        RepairActionType.CHANGE_CHART,
        RepairActionType.RECROP_IMAGE,
        RepairActionType.REPLACE_LAYOUT,
        RepairActionType.REDESIGN_SLIDE,
    ),
}

_GEOMETRY_REPAIR_ACTIONS = {
    RepairActionType.MOVE_ELEMENT,
    RepairActionType.RESIZE_ELEMENT,
    RepairActionType.RECROP_IMAGE,
    RepairActionType.REPLACE_LAYOUT,
}


@dataclass(frozen=True)
class SlideRedesignRequest:
    deck_id: str
    slide: DeckSlide
    intent: SlideIntent | None
    planned_slide: DeckPlanSlide | None
    visual_direction: VisualDirection | None
    design_context: PresentationDesignContext | None
    issues: tuple[QualityIssue, ...]
    immutable_facts: tuple[str, ...]
    deck_context: tuple[str, ...]
    visible_immutable_facts: tuple[tuple[str, str], ...] = ()
    failure_routes: tuple[PresentationFailureRoute, ...] = ()
    allowed_actions: tuple[RepairActionType, ...] = ()
    usage_session_id: str | None = None
    structural_repair_allowed: bool = False
    slide_count_ceiling: int | None = None
    retry_directive: str | None = None


@dataclass(frozen=True)
class SlideRedesignProposal:
    slide: DeckSlide
    action: RepairActionType = RepairActionType.REDESIGN_SLIDE
    rationale: str = ""
    additional_slides: tuple[DeckSlide, ...] = ()
    plan_slides: tuple[DeckPlanSlide, ...] = ()


class PresentationRepairDesigner(Protocol):
    """External reasoning boundary; execution remains deterministic."""

    def redesign(self, request: SlideRedesignRequest) -> SlideRedesignProposal:
        ...


def _repair_constraints_for_issues(
    issues: Sequence[QualityIssue],
    *,
    allow_structural_repair: bool,
) -> tuple[
    tuple[PresentationFailureRoute, ...],
    tuple[RepairActionType, ...],
]:
    routes = tuple(failure_routes_for_quality_issues(issues))
    if not routes or any(
        route.category not in _ACTIONS_BY_FAILURE_CATEGORY
        for route in routes
    ):
        return routes, ()

    allowed = {
        action
        for route in routes
        for action in _ACTIONS_BY_FAILURE_CATEGORY[route.category]
    }
    if not allow_structural_repair:
        allowed.discard(RepairActionType.SPLIT_SLIDE)
    return routes, tuple(
        action for action in RepairActionType if action in allowed
    )


def _searchable_slide(slide: DeckSlide) -> str:
    return re.sub(
        r"\s+",
        "",
        json.dumps(
            slide.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
        ).casefold(),
    )


def _searchable_visible_slide(slide: DeckSlide) -> str:
    return re.sub(
        r"\s+",
        "",
        json.dumps(
            [obj.model_dump(mode="json") for obj in slide.objects],
            ensure_ascii=False,
            sort_keys=True,
        ).casefold(),
    )


def _visible_immutable_fact_contract(
    slide: DeckSlide,
    immutable_facts: Sequence[str],
) -> tuple[tuple[str, str], ...]:
    visible = _searchable_visible_slide(slide)
    return tuple(
        (f"immutable_facts[{index}]", fact)
        for index, fact in enumerate(immutable_facts)
        if (token := re.sub(r"\s+", "", fact).casefold())
        and token in visible
    )


def _semantic_object_payload(obj: DeckObject) -> dict[str, object]:
    common: dict[str, object] = {"id": obj.id, "kind": obj.kind}
    if isinstance(obj, TextObject):
        return {
            **common,
            "text": obj.text,
            "runs": [
                [run.text, run.paragraph_break] for run in obj.runs
            ],
        }
    if isinstance(obj, ImageObject):
        return {
            **common,
            "path": obj.path,
            "asset_id": obj.asset_id,
            "alt": obj.alt,
        }
    if isinstance(obj, MetricObject):
        return {
            **common,
            "label": obj.label,
            "value": obj.value,
            "detail": obj.detail,
        }
    if isinstance(obj, TableObject):
        return {
            **common,
            "headers": obj.headers,
            "rows": obj.rows,
        }
    if isinstance(obj, ChartObject):
        return {
            **common,
            "categories": obj.categories,
            "series": obj.series,
            "unit": obj.unit,
            "number_format": obj.number_format,
            "derivation": obj.derivation,
            "source_refs": obj.source_refs,
        }
    if isinstance(obj, ShapeObject):
        return {**common, "text": obj.text}
    raise TypeError(f"unsupported DeckIR object: {type(obj).__name__}")


def _semantic_slide_signature(slide: DeckSlide) -> str:
    payloads = [
        json.dumps(
            _semantic_object_payload(obj),
            ensure_ascii=False,
            sort_keys=True,
        )
        for obj in slide.objects
    ]
    return json.dumps(
        {
            "objects": sorted(payloads),
            "speaker_notes": slide.speaker_notes,
            "source_fact_ids": sorted(slide.source_fact_ids),
            "asset_ids": sorted(slide.asset_ids),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _validate_action_scope(
    original: DeckSlide,
    proposal: SlideRedesignProposal,
) -> None:
    if (
        proposal.action in _GEOMETRY_REPAIR_ACTIONS
        and _semantic_slide_signature(proposal.slide)
        != _semantic_slide_signature(original)
    ):
        raise ValueError(
            "geometry repair changed semantic content; route the content "
            "change through a content repair action",
        )


def _validate_partition(
    label: str,
    original: Sequence[str],
    replacements: Sequence[Sequence[str]],
) -> None:
    before = Counter(original)
    after = Counter(item for values in replacements for item in values)
    if after != before:
        lost = sorted((before - after).elements())
        invented = sorted((after - before).elements())
        details = []
        if lost:
            details.append("lost=" + ", ".join(lost))
        if invented:
            details.append("invented=" + ", ".join(invented))
        raise ValueError(
            f"split slide must partition {label} without loss or invention: "
            + "; ".join(details),
        )


def _hard_object_ids(
    design_context: PresentationDesignContext | None,
) -> set[str]:
    if design_context is None:
        return set()
    return {
        object_id
        for constraint in [
            *design_context.profile.brand_constraints,
            *design_context.profile.master_constraints,
            *design_context.profile.hard_constraints,
        ]
        for object_id in constraint.object_ids
    }


def _validate_repaired_slide_chart_contracts(slide: DeckSlide) -> None:
    """Reject incomplete native charts while the proposal can still retry.

    Composition deliberately refuses to guess measurement units, derivations,
    or evidence bindings. Repair proposals must satisfy the same contract
    before acceptance, while the bounded designer retry remains available.
    """

    for obj in slide.objects:
        if not isinstance(obj, ChartObject):
            continue
        if not obj.unit.strip():
            raise ValueError(f"native chart {obj.id} requires a unit")
        if not obj.derivation.strip():
            raise ValueError(
                f"native chart {obj.id} requires a derivation statement",
            )
        if not obj.source_refs:
            raise ValueError(
                f"native chart {obj.id} requires source references",
            )


def _validate_replacement(
    deck: DeckIR,
    original: DeckSlide,
    proposal: SlideRedesignProposal,
    visible_immutable_facts: Sequence[tuple[str, str]],
    design_context: PresentationDesignContext | None,
) -> DeckIR:
    replacement = proposal.slide
    if replacement.id != original.id:
        raise ValueError("slide redesign must preserve the stable slide id")
    if replacement.intent_id != original.intent_id:
        raise ValueError("slide redesign must preserve the storyboard intent id")
    original_titles = tuple(
        obj.text
        for obj in original.objects
        if isinstance(obj, TextObject) and obj.role == "title"
    )
    replacement_titles = tuple(
        obj.text
        for obj in replacement.objects
        if isinstance(obj, TextObject) and obj.role == "title"
    )
    if replacement_titles != original_titles:
        raise ValueError("slide redesign must preserve the storyboard title")
    if replacement == original:
        raise ValueError("redo_slide must produce a materially new slide design")
    if (
        replacement.speaker_notes != original.speaker_notes
        and numeric_tokens(replacement.speaker_notes)
    ):
        raise ValueError(
            "slide redesign speaker_notes must be numeric-free or preserve "
            "the original notes",
        )
    _validate_repaired_slide_chart_contracts(replacement)

    after = _searchable_visible_slide(replacement)
    lost_paths: list[str] = []
    for path, fact in visible_immutable_facts:
        token = re.sub(r"\s+", "", fact).casefold()
        if token and token not in after:
            lost_paths.append(path)
    if lost_paths:
        raise ValueError(
            "slide redesign removed immutable facts at paths: "
            + ", ".join(lost_paths)
        )

    hard_ids = _hard_object_ids(design_context)
    if hard_ids:
        before_objects = {obj.id: obj for obj in original.objects}
        after_objects = {obj.id: obj for obj in replacement.objects}
        changed_hard = [
            object_id
            for object_id in hard_ids
            if object_id in before_objects
            and after_objects.get(object_id) != before_objects[object_id]
        ]
        removed_hard = [
            object_id
            for object_id in hard_ids
            if object_id in before_objects and object_id not in after_objects
        ]
        if changed_hard or removed_hard:
            raise ValueError(
                "slide redesign modified HARD constrained objects: "
                + ", ".join(sorted(set([*changed_hard, *removed_hard])))
            )

    slides = [replacement if item.id == original.id else item for item in deck.slides]
    # Full model validation also rejects object ids colliding with another slide.
    payload = deck.model_dump(mode="json")
    payload["slides"] = [slide.model_dump(mode="json") for slide in slides]
    return DeckIR.model_validate(payload)


def _validate_split(
    deck: DeckIR,
    plan: DeckPlan | None,
    original: DeckSlide,
    proposal: SlideRedesignProposal,
    immutable_facts: Sequence[str],
    design_context: PresentationDesignContext | None,
    *,
    slide_count_ceiling: int | None,
) -> tuple[DeckIR, DeckPlan]:
    if plan is None:
        raise ValueError(
            "structural slide split requires an authoritative DeckPlan",
        )

    planned_matches = [
        item for item in plan.slides if item.intent.id == original.intent_id
    ]
    if len(planned_matches) != 1:
        raise ValueError(
            "structural slide split requires exactly one matching "
            "storyboard intent",
        )
    original_plan_slide = planned_matches[0]
    deck_replacements = (proposal.slide, *proposal.additional_slides)
    plan_replacements = proposal.plan_slides
    if len(deck_replacements) != 2 or len(plan_replacements) != 2:
        raise ValueError(
            "structural slide split must produce exactly two DeckIR "
            "and DeckPlan pages",
        )
    if proposal.slide.id != original.id:
        raise ValueError(
            "the first split page must preserve the stable slide id",
        )
    if proposal.slide.intent_id != original.intent_id:
        raise ValueError(
            "the first split page must preserve the storyboard intent id",
        )
    if plan_replacements[0].id != original_plan_slide.id:
        raise ValueError(
            "the first split plan page must preserve the stable plan id",
        )
    if plan_replacements[0].intent.id != original_plan_slide.intent.id:
        raise ValueError(
            "the first split plan page must preserve the storyboard intent id",
        )
    for replacement_slide in deck_replacements:
        _validate_repaired_slide_chart_contracts(replacement_slide)
    if any(
        deck_slide.intent_id != plan_slide.intent.id
        for deck_slide, plan_slide in zip(
            deck_replacements,
            plan_replacements,
            strict=True,
        )
    ):
        raise ValueError(
            "split DeckIR pages must map one-to-one to plan intents",
        )

    speaker_notes = [
        (item.speaker_notes or "").strip() for item in deck_replacements
    ]
    if any(not note for note in speaker_notes):
        raise ValueError("split pages must each provide speaker notes")
    if len(set(speaker_notes)) != len(speaker_notes):
        raise ValueError(
            "split pages must define distinct speaker-note boundaries",
        )

    replacement_slide_ids = [item.id for item in deck_replacements]
    replacement_plan_ids = [item.id for item in plan_replacements]
    replacement_intent_ids = [item.intent.id for item in plan_replacements]
    if len(set(replacement_slide_ids)) != len(replacement_slide_ids):
        raise ValueError("split slide ids must be unique")
    if len(set(replacement_plan_ids)) != len(replacement_plan_ids):
        raise ValueError("split plan ids must be unique")
    if len(set(replacement_intent_ids)) != len(replacement_intent_ids):
        raise ValueError("split intent ids must be unique")

    other_slide_ids = {
        item.id for item in deck.slides if item.id != original.id
    }
    if other_slide_ids.intersection(replacement_slide_ids):
        raise ValueError("split slide id collides with another deck page")
    other_plan_ids = {
        item.id for item in plan.slides if item.id != original_plan_slide.id
    }
    if other_plan_ids.intersection(replacement_plan_ids):
        raise ValueError("split plan id collides with another storyboard page")
    other_intent_ids = {
        item.intent.id
        for item in plan.slides
        if item.id != original_plan_slide.id
    }
    if other_intent_ids.intersection(replacement_intent_ids):
        raise ValueError(
            "split intent id collides with another storyboard intent",
        )

    resulting_count = len(deck.slides) + 1
    if (
        slide_count_ceiling is not None
        and resulting_count > slide_count_ceiling
    ):
        raise ValueError(
            "structural slide split exceeds the requested slide-count ceiling",
        )

    _validate_partition(
        "storyboard source references",
        original_plan_slide.intent.source_refs,
        [item.intent.source_refs for item in plan_replacements],
    )
    _validate_partition(
        "storyboard required facts",
        original_plan_slide.intent.required_facts,
        [item.intent.required_facts for item in plan_replacements],
    )
    _validate_partition(
        "storyboard immutable facts",
        original_plan_slide.intent.immutable_facts,
        [item.intent.immutable_facts for item in plan_replacements],
    )
    _validate_partition(
        "DeckIR fact ids",
        original.source_fact_ids,
        [item.source_fact_ids for item in deck_replacements],
    )
    _validate_partition(
        "DeckIR asset ids",
        original.asset_ids,
        [item.asset_ids for item in deck_replacements],
    )

    before = _searchable_slide(original)
    after = "".join(_searchable_slide(item) for item in deck_replacements)
    lost_immutable = []
    for fact in immutable_facts:
        token = re.sub(r"\s+", "", fact).casefold()
        if token and token in before and token not in after:
            lost_immutable.append(fact)
    if lost_immutable:
        raise ValueError(
            "slide split removed immutable facts: " + "、".join(lost_immutable),
        )

    hard_ids = _hard_object_ids(design_context)
    before_objects = {
        obj.id: obj for obj in original.objects if obj.id in hard_ids
    }
    after_objects: dict[str, list[DeckObject]] = {}
    for slide in deck_replacements:
        for obj in slide.objects:
            if obj.id in hard_ids:
                after_objects.setdefault(obj.id, []).append(obj)
    changed_hard = [
        object_id
        for object_id, before_object in before_objects.items()
        if after_objects.get(object_id) != [before_object]
    ]
    if changed_hard:
        raise ValueError(
            "slide split modified, removed, or duplicated HARD "
            "constrained objects: " + ", ".join(sorted(changed_hard)),
        )

    deck_slides: list[DeckSlide] = []
    for item in deck.slides:
        if item.id == original.id:
            deck_slides.extend(deck_replacements)
        else:
            deck_slides.append(item)
    deck_payload = deck.model_dump(mode="json")
    deck_payload["slides"] = [
        slide.model_dump(mode="json") for slide in deck_slides
    ]
    repaired_deck = DeckIR.model_validate(deck_payload)

    raw_plan_slides: list[DeckPlanSlide] = []
    for item in plan.slides:
        if item.id == original_plan_slide.id:
            raw_plan_slides.extend(plan_replacements)
        else:
            raw_plan_slides.append(item)
    normalized_plan_slides = [
        item.model_copy(update={"order": index})
        for index, item in enumerate(raw_plan_slides, start=1)
    ]
    plan_payload = plan.model_dump(mode="json")
    plan_payload["slides"] = [
        slide.model_dump(mode="json") for slide in normalized_plan_slides
    ]
    repaired_plan = DeckPlan.model_validate(plan_payload)
    if len(repaired_deck.slides) != len(repaired_plan.slides):
        raise ValueError(
            "split DeckIR and DeckPlan page counts must stay synchronized",
        )
    return repaired_deck, repaired_plan


def repair_deck_with_designer(
    deck: DeckIR,
    plan: DeckPlan | None,
    issues: Sequence[QualityIssue],
    designer: PresentationRepairDesigner,
    *,
    usage_session_id: str | None = None,
    allow_structural_repair: bool = False,
    slide_count_ceiling: int | None = None,
    redesign_executor: Callable[
        [PresentationRepairDesigner, SlideRedesignRequest],
        SlideRedesignProposal,
    ]
    | None = None,
    accepted_slide_callback: Callable[
        [DeckIR, DeckPlan | None, str],
        None,
    ]
    | None = None,
    proposal_attempt_limit: int = 1,
) -> RepairResult:
    """Redesign every failing slide with bounded proposal validation.

    This is the v3 repair path.  It never maps a slide to a stock fallback
    layout and never labels recompilation of unchanged DeckIR as a redesign.
    """

    if proposal_attempt_limit < 1:
        raise ValueError("proposal_attempt_limit must be at least 1")

    errors_by_slide: dict[str, list[QualityIssue]] = {}
    for issue in issues:
        if issue.severity == "error" and issue.repairable and issue.slide_id:
            errors_by_slide.setdefault(issue.slide_id, []).append(issue)
    if not errors_by_slide:
        return RepairResult(deck=deck, changed=False, actions=(), plan=plan)

    working = deck
    working_plan = plan
    design_context = deck.design_context or (
        plan.design_context if plan is not None else None
    )
    actions: list[str] = []
    for slide_id in [
        slide.id for slide in deck.slides if slide.id in errors_by_slide
    ]:
        structural_repair_allowed = allow_structural_repair and (
            slide_count_ceiling is None
            or len(working.slides) < slide_count_ceiling
        )
        failure_routes, allowed_actions = _repair_constraints_for_issues(
            errors_by_slide[slide_id],
            allow_structural_repair=structural_repair_allowed,
        )
        if not allowed_actions:
            continue
        original = next(
            slide for slide in working.slides if slide.id == slide_id
        )
        planned_matches = [
            item
            for item in (
                working_plan.slides if working_plan is not None else []
            )
            if item.intent.id == original.intent_id
        ]
        planned_slide = (
            planned_matches[0] if len(planned_matches) == 1 else None
        )
        intent = planned_slide.intent if planned_slide is not None else None
        immutable_facts = tuple(
            dict.fromkeys(
                [
                    *(intent.immutable_facts if intent is not None else []),
                    *working.immutable_data,
                ],
            ),
        )
        visible_immutable_facts = _visible_immutable_fact_contract(
            original,
            immutable_facts,
        )
        redesign_request = SlideRedesignRequest(
            deck_id=working.id,
            slide=original,
            intent=intent,
            planned_slide=planned_slide,
            visual_direction=(
                working_plan.visual_direction
                if working_plan is not None
                else None
            ),
            design_context=design_context,
            issues=tuple(errors_by_slide[slide_id]),
            immutable_facts=immutable_facts,
            deck_context=tuple(
                item.speaker_notes or item.id for item in working.slides
            ),
            visible_immutable_facts=visible_immutable_facts,
            failure_routes=failure_routes,
            allowed_actions=allowed_actions,
            usage_session_id=usage_session_id,
            structural_repair_allowed=structural_repair_allowed,
            slide_count_ceiling=slide_count_ceiling,
        )
        for proposal_attempt in range(1, proposal_attempt_limit + 1):
            try:
                proposal = (
                    redesign_executor(designer, redesign_request)
                    if redesign_executor is not None
                    else designer.redesign(redesign_request)
                )
            except StructuredTaskValidationError as exc:
                # Correct rejected structured output within the existing
                # proposal budget. Other execution failures still fail fast.
                if proposal_attempt >= proposal_attempt_limit:
                    raise
                redesign_request = replace(
                    redesign_request,
                    retry_directive=(
                        "上一份页级修复提案未通过结构校验："
                        f"{type(exc).__name__}: {exc}。"
                        "只纠正该校验错误，不得放宽事实、权限、HARD "
                        "对象或动作范围；重新输出完整提案。"
                    ),
                )
                continue
            try:
                if (
                    proposal.action == RepairActionType.SPLIT_SLIDE
                    and not structural_repair_allowed
                ):
                    raise ValueError(
                        "structural slide split is not allowed in this edit "
                        "scope",
                    )
                if proposal.action not in allowed_actions:
                    classified_routes = ", ".join(
                        f"{route.category.value}/"
                        f"{route.recovery_action.value}"
                        for route in failure_routes
                    )
                    raise ValueError(
                        f"repair action {proposal.action.value} is not "
                        "allowed for classified routes: "
                        f"{classified_routes}",
                    )
                _validate_action_scope(original, proposal)
                if proposal.action == RepairActionType.SPLIT_SLIDE:
                    next_deck, next_plan = _validate_split(
                        working,
                        working_plan,
                        original,
                        proposal,
                        immutable_facts,
                        design_context,
                        slide_count_ceiling=slide_count_ceiling,
                    )
                else:
                    if proposal.action == RepairActionType.MERGE_SLIDE:
                        raise ValueError(
                            "structural slide merge is not supported",
                        )
                    if proposal.additional_slides or proposal.plan_slides:
                        raise ValueError(
                            "non-structural redesign cannot add DeckIR or "
                            "DeckPlan pages",
                        )
                    next_deck = _validate_replacement(
                        working,
                        original,
                        proposal,
                        visible_immutable_facts,
                        design_context,
                    )
                    next_plan = working_plan
                if accepted_slide_callback is not None:
                    accepted_slide_callback(
                        next_deck,
                        next_plan,
                        slide_id,
                    )
            except (TypeError, ValueError) as exc:
                if proposal_attempt >= proposal_attempt_limit:
                    raise
                redesign_request = replace(
                    redesign_request,
                    retry_directive=(
                        "上一份页级修复提案未通过确定性校验："
                        f"{type(exc).__name__}: {exc}。"
                        "只纠正该校验错误，不得放宽事实、权限、HARD "
                        "对象或动作范围；重新输出完整提案。"
                    ),
                )
                continue
            working, working_plan = next_deck, next_plan
            actions.append(
                f"{slide_id}: {proposal.action.value}"
                + (
                    f" — {proposal.rationale}"
                    if proposal.rationale
                    else ""
                ),
            )
            break
    return RepairResult(
        deck=working,
        changed=working != deck or working_plan != plan,
        actions=tuple(actions),
        plan=working_plan,
    )


def _compatible_slots(slide: DeckSlide, layout_id: str, obj: DeckObject) -> list[str]:
    layout = get_layout(layout_id)
    return [slot.id for slot in layout.slots if obj.kind in slot.accepts]


def _remap_to_fallback(slide: DeckSlide) -> DeckSlide | None:
    layout = get_layout(slide.layout_id)
    if not layout.fallback_layout_id:
        return None
    fallback = get_layout(layout.fallback_layout_id)
    counts: dict[str, int] = {}
    remapped: list[DeckObject] = []
    for obj in slide.objects:
        candidates = _compatible_slots(slide, fallback.id, obj)
        if not candidates:
            return None
        chosen = next(
            (
                slot_id
                for slot_id in candidates
                if counts.get(slot_id, 0) < next(slot.max_items for slot in fallback.slots if slot.id == slot_id)
                and (
                    not isinstance(obj, TextObject)
                    or next(slot.max_chars for slot in fallback.slots if slot.id == slot_id) is None
                    or len(obj.text) <= next(slot.max_chars for slot in fallback.slots if slot.id == slot_id)
                )
            ),
            None,
        )
        if chosen is None:
            return None
        counts[chosen] = counts.get(chosen, 0) + 1
        remapped.append(obj.model_copy(update={"slot": chosen}))
    candidate = slide.model_copy(update={"layout_id": fallback.id, "objects": remapped})
    return candidate if not check_capacity(candidate) else None


def repair_deck_once(deck: DeckIR) -> RepairResult:
    """Legacy compiler compatibility only; v3 must use semantic redesign."""
    changed = False
    actions: list[str] = []
    slides: list[DeckSlide] = []
    for slide in deck.slides:
        if not check_capacity(slide):
            slides.append(slide)
            continue
        fallback = _remap_to_fallback(slide)
        if fallback is None:
            slides.append(slide)
            continue
        slides.append(fallback)
        changed = True
        actions.append(f"{slide.id}: {slide.layout_id} -> {fallback.layout_id}")
    return RepairResult(deck=deck.model_copy(update={"slides": slides}), changed=changed, actions=tuple(actions))
