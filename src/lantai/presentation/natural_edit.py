# -*- coding: utf-8 -*-
"""Translate bounded natural-language slide edits into explicit DeckIR changes.

The translator intentionally fails closed.  A free-form instruction is never
treated as completed work unless it resolves to concrete native text or labels
inside the requested slide and changes their values.
"""

from __future__ import annotations

import hashlib
import json
import re

from pydantic import ValidationError

from .models import (
    AddObjectAction,
    ChartObject,
    DeckIR,
    DeckPlan,
    ImageObject,
    MAX_OBJECT_EDIT_SLIDES,
    MoveResizeAction,
    NativeGroupObject,
    PresentationBrief,
    PresentationObjectEditPlan,
    RemoveObjectAction,
    ReplaceImageAction,
    ReplaceTextAction,
    SetShapeTextAction,
    ShapeObject,
    SourceFactCorrection,
    SourceFactCorrectionRecord,
    SpeakerNotesOutputContract,
    TableObject,
    TextObject,
    TextReplacement,
    UpdateChartAction,
    UpdateTableAction,
)
from .page_authoring import numeric_tokens, page_scene_hash, validate_planned_text_sources
from .text_editing import replace_native_text_object
from .table_editing import replace_native_table_text


_QUOTED_REPLACEMENT = re.compile(
    r"(?:将|把)\s*[“\"「『](.+?)[”\"」』]\s*"
    r"(?:改为|改成|替换为|换成)\s*[“\"「『](.+?)[”\"」』]",
)
_TARGETED_REPLACEMENT = re.compile(
    r"^(?:请)?(?:把|将)?(?:本页|这页|这一页|当前页)?(?:的)?"
    r"(副标题|标题|正文|结论|文案)\s*"
    r"(?:改为|改成|替换为|换成|更改为)\s*[：:]?\s*(.+?)\s*$",
)


def _source_correction_digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _source_correction_error() -> ValueError:
    # Source excerpts and private paths must not leak through validation errors.
    return ValueError("source_fact_correction: invalid source, protected fact, provenance or caller scope")


def _validate_correction_source(
    deck: DeckIR, brief: PresentationBrief | None, correction: SourceFactCorrection,
) -> None:
    if brief is None or correction.original_fact == correction.corrected_fact:
        raise _source_correction_error()
    ref = correction.source_ref
    if ref not in deck.source_refs or ref not in brief.source_refs:
        raise _source_correction_error()
    for coverage in (deck.source_coverage, brief.source_coverage):
        matches = [item for item in coverage if item.material_id == ref]
        if (len(matches) != 1 or matches[0].status != "supported_content_extracted"
                or matches[0].source_sha256 != correction.source_sha256):
            raise _source_correction_error()
    sections = [
        section for section in brief.source_sections
        if ref in (section.source_refs or [section.source_ref])
        and hashlib.sha256(section.body.encode("utf-8")).hexdigest() == correction.source_section_sha256
    ]
    if len(sections) != 1 or correction.corrected_fact not in sections[0].body:
        raise _source_correction_error()
    protected = [*brief.required_facts, *brief.immutable_data, *deck.immutable_data]
    if any(value and (value in correction.original_fact or correction.original_fact in value)
           for value in protected):
        raise _source_correction_error()


def _correction_slide_ids(deck: DeckIR, fact_ids: set[str]) -> list[str]:
    return [slide.id for slide in deck.slides if fact_ids.intersection(slide.source_fact_ids)]


def validate_source_fact_correction_history(
    deck: DeckIR, brief: PresentationBrief | None,
) -> None:
    """Reverse every revision; persisted flags are not verification evidence."""
    if not deck.source_fact_corrections:
        return
    working = deck.model_copy(deep=True)
    for record in reversed(deck.source_fact_corrections):
        correction = record.correction
        _validate_correction_source(working, brief, correction)
        catalog = {fact.fact_id: fact for fact in working.facts}
        if (len(catalog) != len(working.facts)
                or _source_correction_digest([fact.model_dump(mode="json") for fact in working.facts])
                != record.corrected_facts_sha256
                or _source_correction_digest(working.required_facts) != record.corrected_required_facts_sha256
                or record.fact_index >= len(working.required_facts)
                or working.required_facts[record.fact_index] != correction.corrected_fact
                or record.slide_ids != _correction_slide_ids(working, set(record.original_fact_texts))):
            raise _source_correction_error()
        for fact_id, original in record.original_fact_texts.items():
            fact = catalog.get(fact_id)
            if (fact is None or fact.immutable or correction.source_ref not in fact.source_refs
                    or correction.original_fact not in original
                    or fact.text != original.replace(correction.original_fact, correction.corrected_fact)):
                raise _source_correction_error()
            fact.text = original
        working.required_facts[record.fact_index] = correction.original_fact
        if (working.required_facts.count(correction.original_fact) != 1
                or set(record.original_fact_texts) != {
                    fact.fact_id for fact in working.facts if correction.original_fact in fact.text
                }
                or _source_correction_digest([fact.model_dump(mode="json") for fact in working.facts])
                != record.base_facts_sha256
                or _source_correction_digest(working.required_facts) != record.base_required_facts_sha256):
            raise _source_correction_error()


def apply_source_fact_corrections(
    deck: DeckIR,
    plan: DeckPlan | None,
    brief: PresentationBrief | None,
    corrections: list[SourceFactCorrection | dict],
    *,
    allowed_slide_ids: list[str],
) -> tuple[DeckIR, DeckPlan | None]:
    """Apply caller-pinned, verbatim source corrections without editing pixels.

    Facts stay one-for-one with stable IDs. Full original-source semantic
    review still decides whether the subsequently edited native deck passes.
    """
    validate_source_fact_correction_history(deck, brief)
    if not corrections:
        return deck, plan
    if (not isinstance(corrections, list) or len(corrections) > 10
            or len(deck.source_fact_corrections) + len(corrections) > 40
            or not isinstance(allowed_slide_ids, list)
            or not 1 <= len(allowed_slide_ids) <= MAX_OBJECT_EDIT_SLIDES
            or len(set(allowed_slide_ids)) != len(allowed_slide_ids)
            or not set(allowed_slide_ids).issubset({slide.id for slide in deck.slides})):
        raise _source_correction_error()
    try:
        requests = [SourceFactCorrection.model_validate(item) for item in corrections]
    except (ValidationError, TypeError):
        raise _source_correction_error() from None
    if len({item.original_fact for item in requests}) != len(requests):
        raise _source_correction_error()
    changed, changed_plan = deck.model_copy(deep=True), plan.model_copy(deep=True) if plan else None
    for correction in requests:
        _validate_correction_source(changed, brief, correction)
        old, new = correction.original_fact, correction.corrected_fact
        if changed.required_facts.count(old) != 1 or new in changed.required_facts:
            raise _source_correction_error()
        original_texts = {fact.fact_id: fact.text for fact in changed.facts if old in fact.text}
        if (not original_texts or len(original_texts) > 120
                or len({fact.fact_id for fact in changed.facts}) != len(changed.facts)
                or any(fact.immutable or correction.source_ref not in fact.source_refs
                       for fact in changed.facts if fact.fact_id in original_texts)):
            raise _source_correction_error()
        affected = _correction_slide_ids(changed, set(original_texts))
        if not affected or not set(affected).issubset(allowed_slide_ids):
            raise _source_correction_error()
        if changed_plan is not None:
            for planned in changed_plan.slides:
                if any(old in value or value in old for value in planned.intent.immutable_facts if value):
                    raise _source_correction_error()
                fields = [*planned.content_blocks, *planned.intent.evidence, *planned.intent.required_facts]
                if not any(old in value for value in fields):
                    continue
                joined = [slide for slide in changed.slides if slide.intent_id == planned.intent.id]
                if (len(joined) != 1 or joined[0].id not in affected
                        or sum(item.intent.id == planned.intent.id for item in changed_plan.slides) != 1):
                    raise _source_correction_error()
                planned.content_blocks = [value.replace(old, new) for value in planned.content_blocks]
                planned.intent.evidence = [value.replace(old, new) for value in planned.intent.evidence]
                planned.intent.required_facts = [value.replace(old, new) for value in planned.intent.required_facts]
        fact_index = changed.required_facts.index(old)
        base_facts = _source_correction_digest([fact.model_dump(mode="json") for fact in changed.facts])
        base_required = _source_correction_digest(changed.required_facts)
        changed.required_facts[fact_index] = new
        for fact in changed.facts:
            if fact.fact_id in original_texts:
                fact.text = fact.text.replace(old, new)
        changed.source_fact_corrections.append(SourceFactCorrectionRecord(
            correction=correction, fact_index=fact_index, original_fact_texts=original_texts,
            slide_ids=affected, base_facts_sha256=base_facts, base_required_facts_sha256=base_required,
            corrected_facts_sha256=_source_correction_digest([fact.model_dump(mode="json") for fact in changed.facts]),
            corrected_required_facts_sha256=_source_correction_digest(changed.required_facts),
        ))
    validate_source_fact_correction_history(changed, brief)
    return changed, changed_plan


def _unquote(value: str) -> str:
    value = value.strip()
    pairs = {"“": "”", "「": "」", "『": "』", '"': '"', "'": "'"}
    if len(value) >= 2 and value[0] in pairs and value[-1] == pairs[value[0]]:
        return value[1:-1].strip()
    return value


def validate_requested_object_edit_scope(
    deck: DeckIR,
    slide_id: str,
    additional_slide_ids: list[str] | None = None,
    *,
    plan: PresentationObjectEditPlan | None = None,
    required_slide_ids: list[str] | None = None,
) -> list[str]:
    """Keep page authority with the caller, never with the model response."""
    additional = [] if additional_slide_ids is None else additional_slide_ids
    if (
        not isinstance(additional, list)
        or len(additional) > MAX_OBJECT_EDIT_SLIDES - 1
        or any(not isinstance(item, str) or not item for item in additional)
    ):
        raise ValueError("requested object edit scope is invalid or unbounded")
    requested = [slide_id, *additional]
    if len(set(requested)) != len(requested):
        raise ValueError("requested object edit scope contains duplicate slides")
    if not set(requested).issubset({slide.id for slide in deck.slides}):
        raise ValueError("requested object edit scope contains an unknown slide")
    required = [] if required_slide_ids is None else required_slide_ids
    if (
        not isinstance(required, list)
        or len(required) > MAX_OBJECT_EDIT_SLIDES
        or any(not isinstance(item, str) or not item for item in required)
        or len(set(required)) != len(required)
        or not set(required).issubset(requested)
    ):
        raise ValueError("required edit page scope must be a bounded unique subset of requested scope")
    if plan is not None:
        if (
            plan.slide_id != slide_id
            or not set(plan.additional_slide_ids).issubset(additional)
        ):
            raise PermissionError("object edit plan exceeded the requested slide scope")
        if any(patch.slide_id not in requested for patch in plan.resolved_intent_patches()):
            raise PermissionError("object edit plan exceeded the requested intent scope")
        if any(patch.slide_id not in requested for patch in plan.resolved_speaker_notes_patches()):
            raise PermissionError("object edit plan exceeded the requested notes scope")
    return requested


def validate_required_object_edit_coverage(
    original: DeckIR,
    changed: DeckIR,
    required_slide_ids: list[str],
    *,
    changed_intent_slide_ids: list[str] | tuple[str, ...] = (),
) -> None:
    """Require net edits, not declared scope or revision bookkeeping.

    The caller first validates requested authority and applies intent patches
    through ``apply_slide_intent_patch``. This check does not infer intent from
    instruction wording or replace whole-deck quality verification.
    Empty requirements preserve the legacy permission-only edit contract.
    """

    original_by_id = {slide.id: slide for slide in original.slides}
    changed_by_id = {slide.id: slide for slide in changed.slides}
    intent_changes = set(changed_intent_slide_ids)
    bookkeeping = {"revision", "scene_hash"}
    for slide_id in required_slide_ids:
        before = original_by_id.get(slide_id)
        after = changed_by_id.get(slide_id)
        if before is None or after is None:
            raise ValueError("required edit page scope contains an unknown slide")
        if slide_id in intent_changes:
            continue
        if before.model_dump(exclude=bookkeeping) == after.model_dump(exclude=bookkeeping):
            raise ValueError(f"edit.required_slide_unchanged: {slide_id}")


def _target_objects(
    text_objects: list[TextObject],
    target: str,
) -> list[TextObject]:
    if target == "标题":
        return [
            obj
            for obj in text_objects
            if obj.role == "title" or obj.slot == "title"
        ]
    if target == "副标题":
        return [obj for obj in text_objects if obj.slot == "subtitle"]
    if target == "结论":
        return [obj for obj in text_objects if obj.slot == "conclusion"]
    candidates = [
        obj
        for obj in text_objects
        if obj.role != "title" and obj.slot not in {"title", "subtitle"}
    ]
    if len(candidates) != 1:
        raise ValueError(
            f"“{target}”指向 {len(candidates)} 个文本对象；请引用要替换的原文以消除歧义",
        )
    return candidates


def plan_text_edit(
    deck: DeckIR,
    slide_id: str,
    instruction: str,
) -> tuple[DeckIR, list[TextReplacement]]:
    """Resolve a natural-language instruction to a changed, slide-scoped DeckIR."""

    normalized = instruction.strip()
    if not normalized:
        raise ValueError("修改要求不能为空")
    slide = next((item for item in deck.slides if item.id == slide_id), None)
    if slide is None:
        raise ValueError("unknown slide id")
    text_objects = [
        obj for obj in slide.objects if isinstance(obj, TextObject)
    ]
    if not text_objects:
        raise ValueError("目标页没有可编辑的原生文本对象")

    replacements: list[TextReplacement] = []
    quoted = _QUOTED_REPLACEMENT.search(normalized)
    if quoted:
        old_text, new_text = (part.strip() for part in quoted.groups())
        if not old_text or not new_text:
            raise ValueError("替换指令必须同时包含原文和新文")
        matches = [obj for obj in text_objects if old_text in obj.text]
        if not matches:
            raise ValueError("目标页中未找到指令引用的原文")
        for obj in matches:
            changed = obj.text.replace(old_text, new_text)
            if changed != obj.text:
                replacements.append(
                    TextReplacement(object_id=obj.id, text=changed),
                )
    else:
        targeted = _TARGETED_REPLACEMENT.match(normalized)
        if not targeted:
            raise ValueError(
                "暂未能把要求安全映射到具体对象；请使用“标题改为……”或“把‘原文’改为‘新文’”",
            )
        target, raw_text = targeted.groups()
        new_text = _unquote(raw_text)
        if not new_text:
            raise ValueError("新文案不能为空")
        for obj in _target_objects(text_objects, target):
            if obj.text != new_text:
                replacements.append(
                    TextReplacement(object_id=obj.id, text=new_text),
                )

    if not replacements:
        raise ValueError("修改要求没有产生实际变化")

    replacement_by_id = {item.object_id: item.text for item in replacements}
    updated_slides = []
    for current_slide in deck.slides:
        if current_slide.id != slide_id:
            updated_slides.append(current_slide)
            continue
        updated_objects = [
            replace_native_text_object(obj, replacement_by_id[obj.id])
            if isinstance(obj, TextObject) and obj.id in replacement_by_id
            else obj
            for obj in current_slide.objects
        ]
        updated_slide = current_slide.model_copy(
            update={
                "objects": updated_objects,
                "revision": current_slide.revision + 1,
                "scene_hash": None,
            },
        )
        updated_slides.append(
            updated_slide.model_copy(
                update={"scene_hash": _scene_hash(updated_slide)},
            ),
        )
    return deck.model_copy(update={"slides": updated_slides}), replacements


def _scene_hash(slide) -> str:
    return page_scene_hash(slide)


class SpeakerNotesNumericContractError(ValueError):
    """Owned note locations only; rejected model text is never diagnostic data."""

    def __init__(self, issues: list[dict[str, str]]) -> None:
        super().__init__("new speaker notes must be numeric-free")
        self.presentation_retry_contract_issues = issues


def validate_speaker_notes_numeric_contract(
    deck: DeckIR, plan: PresentationObjectEditPlan,
) -> None:
    """Check numeric syntax, not meaning; the critic owns source grounding."""

    scoped_slide_ids = [plan.slide_id, *plan.additional_slide_ids]
    slides_by_id = {slide.id: slide for slide in deck.slides}
    if any(slide_id not in slides_by_id for slide_id in scoped_slide_ids):
        raise ValueError("object edit plan targets an unknown slide")
    patches = plan.resolved_speaker_notes_patches()
    if any(patch.slide_id not in scoped_slide_ids for patch in patches):
        raise PermissionError("object edit plan exceeded the declared notes scope")
    issues = [
        {"location": f"$.speaker_notes_patches[{index}].speaker_notes",
         "violation_code": "notes_numeric_free"}
        for index, patch in enumerate(patches)
        if patch.speaker_notes != slides_by_id[patch.slide_id].speaker_notes
        and numeric_tokens(patch.speaker_notes)
    ]
    if issues:
        raise SpeakerNotesNumericContractError(issues)


class SpeakerNotesOutputContractError(ValueError):
    """Safe feedback: counts and caller-owned characters, never rejected text."""

    def __init__(
        self, issues: list[dict[str, str]], *, note_measurements: list[dict[str, object]] | None = None,
    ) -> None:
        super().__init__("new speaker notes violate the explicit output contract")
        self.presentation_retry_contract_issues = issues
        self.presentation_retry_note_measurements = note_measurements or []


def validate_speaker_notes_output_contract(
    deck: DeckIR, plan: PresentationObjectEditPlan, contract: SpeakerNotesOutputContract,
) -> None:
    """Collect lexical failures; only the model may author corrected notes."""
    if not isinstance(contract, SpeakerNotesOutputContract):
        raise TypeError("notes_output_contract must be an explicit typed contract")
    issues: list[dict[str, str]] = []
    try:
        # Keep the existing permission checks and numeric syntax unchanged.
        validate_speaker_notes_numeric_contract(deck, plan)
    except SpeakerNotesNumericContractError as error:
        issues.extend(error.presentation_retry_contract_issues)
    numeric_locations = {issue["location"] for issue in issues}
    slides_by_id = {slide.id: slide for slide in deck.slides}
    forbidden = frozenset(contract.forbidden_characters)
    measurements: list[dict[str, object]] = []
    for index, patch in enumerate(plan.resolved_speaker_notes_patches()):
        if patch.speaker_notes == slides_by_id[patch.slide_id].speaker_notes:
            continue
        location = f"$.speaker_notes_patches[{index}].speaker_notes"
        count = sum("\u4e00" <= char <= "\u9fff" for char in patch.speaker_notes)
        # The model must regenerate the whole proposal, so include in-bounds
        # changed pages too. Only caller-supplied characters may be echoed;
        # rejected text, numeric tokens and untrusted excerpts remain private.
        measurements.append({
            "location": location,
            "slide_id": patch.slide_id,
            "han_characters": count,
            "ascii_digit_characters": sum("0" <= char <= "9" for char in patch.speaker_notes),
            "numeric_free": location not in numeric_locations,
            "forbidden_character_counts": [
                {"character": character, "count": patch.speaker_notes.count(character)}
                for character in contract.forbidden_characters if character in patch.speaker_notes
            ],
        })
        if contract.min_han_characters is not None and count < contract.min_han_characters:
            issues.append({"location": location, "violation_code": "notes_han_length_below_minimum"})
        if contract.max_han_characters is not None and count > contract.max_han_characters:
            issues.append({"location": location, "violation_code": "notes_han_length_above_maximum"})
        if forbidden.intersection(patch.speaker_notes):
            issues.append({"location": location, "violation_code": "notes_forbidden_character"})
    if issues:
        raise SpeakerNotesOutputContractError(issues, note_measurements=measurements)


def apply_object_edit_plan(
    deck: DeckIR,
    plan: PresentationObjectEditPlan,
) -> tuple[DeckIR, list[str]]:
    """Apply native-object actions only to the explicitly scoped slides."""

    scoped_slide_ids = [plan.slide_id, *plan.additional_slide_ids]
    slides_by_id = {slide.id: slide for slide in deck.slides}
    unknown_slides = [
        slide_id for slide_id in scoped_slide_ids if slide_id not in slides_by_id
    ]
    if unknown_slides:
        raise ValueError("object edit plan targets an unknown slide")

    if plan.speaker_notes_patches:
        validate_speaker_notes_numeric_contract(deck, plan)
        updated = dict(slides_by_id)
        changed_notes = False
        for patch in plan.resolved_speaker_notes_patches():
            if patch.slide_id not in scoped_slide_ids:
                raise PermissionError("object edit plan exceeded the declared notes scope")
            before = slides_by_id[patch.slide_id]
            if patch.speaker_notes == before.speaker_notes:
                continue
            after = before.model_copy(update={
                "speaker_notes": patch.speaker_notes,
                "revision": before.revision + 1, "scene_hash": None,
            })
            updated[patch.slide_id] = after.model_copy(update={"scene_hash": _scene_hash(after)})
            changed_notes = True
        if not changed_notes:
            raise ValueError("object edit plan produced no material change")
        return deck.model_copy(update={"slides": [updated[slide.id] for slide in deck.slides]}), []

    object_to_slide: dict[str, str] = {}
    for slide_id in scoped_slide_ids:
        for obj in slides_by_id[slide_id].objects:
            if obj.id in object_to_slide:
                raise ValueError(f"duplicate object id in edit scope: {obj.id}")
            object_to_slide[obj.id] = slide_id

    actions_by_slide: dict[str, list] = {
        slide_id: [] for slide_id in scoped_slide_ids
    }
    all_object_ids = {
        obj.id for slide in deck.slides for obj in slide.objects
    }
    for action in plan.actions:
        if isinstance(action, AddObjectAction):
            if isinstance(action.object, NativeGroupObject):
                raise ValueError("native group is read-only; authored groups are not supported")
            if action.object.id in all_object_ids:
                raise ValueError(f"object already exists: {action.object.id}")
            actions_by_slide[plan.slide_id].append(action)
            all_object_ids.add(action.object.id)
            continue
        target_slide_id = object_to_slide.get(action.object_id)
        if target_slide_id is None:
            raise ValueError(f"unknown object id: {action.object_id}")
        if isinstance(next(obj for obj in slides_by_id[target_slide_id].objects
                           if obj.id == action.object_id), NativeGroupObject):
            raise ValueError("native group is read-only; internal group editing is not supported")
        actions_by_slide[target_slide_id].append(action)

    updated_by_id = dict(slides_by_id)
    materially_changed: set[str] = set()
    for slide_id in scoped_slide_ids:
        actions = actions_by_slide[slide_id]
        if not actions:
            continue
        updated_slide, slide_changed = _apply_object_actions_to_slide(
            deck,
            slides_by_id[slide_id],
            actions,
        )
        updated_by_id[slide_id] = updated_slide
        materially_changed.update(slide_changed)

    # Fail before native compilation/rendering, without changing the original
    # deck or silently rebinding invented source labels to valid source IDs.
    # Decks without a declared source catalog retain their legacy edit behavior;
    # the full content inspector still owns their delivery requirements.
    allowed_sources = set(deck.source_refs)
    if allowed_sources:
        for slide_id in scoped_slide_ids:
            for obj in updated_by_id[slide_id].objects:
                if not isinstance(obj, ChartObject) or obj.id not in materially_changed:
                    continue
                if not obj.source_refs:
                    raise ValueError(
                        "chart.source_missing: edited chart has no source binding",
                    )
                if set(obj.source_refs) - allowed_sources:
                    raise ValueError(
                        "chart.source_invalid: edited chart references an undeclared source",
                    )

    changed = []
    for action in plan.actions:
        object_id = (
            action.object.id
            if isinstance(action, AddObjectAction)
            else action.object_id
        )
        if object_id in materially_changed and object_id not in changed:
            changed.append(object_id)
    if not changed:
        raise ValueError("object edit plan produced no material change")
    return deck.model_copy(
        update={
            "slides": [updated_by_id[slide.id] for slide in deck.slides],
        },
    ), changed


def _apply_object_actions_to_slide(deck, slide, actions):
    objects = {obj.id: obj for obj in slide.objects}
    original = dict(objects)
    order = [obj.id for obj in slide.objects]
    changed: list[str] = []
    catalog = {asset.asset_id: asset for asset in deck.assets}

    for action in actions:
        if isinstance(action, AddObjectAction):
            if action.object.id in objects:
                raise ValueError(f"object already exists: {action.object.id}")
            objects[action.object.id] = action.object
            order.append(action.object.id)
            changed.append(action.object.id)
            continue
        object_id = action.object_id
        if object_id not in objects:
            raise ValueError(f"unknown object id: {object_id}")
        obj = objects[object_id]
        if isinstance(action, RemoveObjectAction):
            del objects[object_id]
            order.remove(object_id)
            changed.append(object_id)
        elif isinstance(action, ReplaceTextAction):
            if not isinstance(obj, TextObject):
                raise ValueError(f"object is not editable text: {object_id}")
            objects[object_id] = replace_native_text_object(obj, action.text)
            changed.append(object_id)
        elif isinstance(action, SetShapeTextAction):
            if not isinstance(obj, ShapeObject) or obj.shape_type == "line":
                raise ValueError(f"object is not an editable shape label: {object_id}")
            objects[object_id] = obj.model_copy(update={"text": action.text})
            changed.append(object_id)
        elif isinstance(action, UpdateTableAction):
            if not isinstance(obj, TableObject):
                raise ValueError(f"object is not an editable table: {object_id}")
            objects[object_id] = replace_native_table_text(obj, action.headers, action.rows)
            changed.append(object_id)
        elif isinstance(action, UpdateChartAction):
            if not isinstance(obj, ChartObject):
                raise ValueError(f"object is not a chart: {object_id}")
            objects[object_id] = obj.model_copy(
                update={
                    "chart_type": action.chart_type or obj.chart_type,
                    "categories": action.categories,
                    "series": action.series,
                    "show_legend": (
                        action.show_legend
                        if action.show_legend is not None
                        else obj.show_legend
                    ),
                    "unit": action.unit if action.unit is not None else obj.unit,
                    "number_format": (
                        action.number_format
                        if action.number_format is not None
                        else obj.number_format
                    ),
                    "derivation": (
                        action.derivation
                        if action.derivation is not None
                        else obj.derivation
                    ),
                    "source_refs": (
                        action.source_refs
                        if action.source_refs is not None
                        else obj.source_refs
                    ),
                },
            )
            changed.append(object_id)
        elif isinstance(action, ReplaceImageAction):
            if not isinstance(obj, ImageObject):
                raise ValueError(f"object is not an image: {object_id}")
            asset = catalog.get(action.asset_id)
            if asset is None:
                raise ValueError(f"unknown image asset: {action.asset_id}")
            if asset.ingestion_status not in {"ready", "recovered"}:
                raise ValueError(
                    f"image asset is not ready: {action.asset_id}",
                )
            if asset.rights_status == "review":
                raise ValueError(
                    f"image rights need review: {action.asset_id}",
                )
            objects[object_id] = obj.model_copy(
                update={
                    "asset_id": action.asset_id,
                    "path": asset.local_path,
                    "alt": action.alt or obj.alt,
                    "crop": action.crop or obj.crop,
                    "focal_point": action.focal_point or obj.focal_point,
                },
            )
            changed.append(object_id)
        elif isinstance(action, MoveResizeAction):
            objects[object_id] = obj.model_copy(update={"frame": action.frame})
            changed.append(object_id)

    changed = list(dict.fromkeys(changed))
    if not changed or all(
        objects.get(key) == original.get(key) for key in changed
    ):
        raise ValueError("object edit plan produced no material change")
    object_ids = set(objects)
    groups = [
        group.model_copy(
            update={
                "object_ids": [
                    item for item in group.object_ids if item in object_ids
                ],
            },
        )
        for group in slide.object_groups
        if any(item in object_ids for item in group.object_ids)
    ]
    relationships = [
        relation
        for relation in slide.relationships
        if set(
            relation.source_object_ids + relation.target_object_ids,
        ).issubset(
            object_ids,
        )
    ]
    updated_objects = [objects[object_id] for object_id in order]
    used_assets = sorted(
        {
            obj.asset_id
            for obj in updated_objects
            if isinstance(obj, ImageObject) and obj.asset_id
        },
    )
    updated_slide = slide.model_copy(
        update={
            "objects": updated_objects,
            "revision": slide.revision + 1,
            "asset_ids": used_assets,
            "object_groups": groups,
            "relationships": relationships,
            "scene_hash": None,
        },
    )
    updated_slide = updated_slide.model_copy(
        update={"scene_hash": _scene_hash(updated_slide)},
    )
    return updated_slide, changed


def apply_slide_intent_patch(
    deck: DeckIR,
    plan: DeckPlan,
    edit_plan: PresentationObjectEditPlan,
    *,
    brief: PresentationBrief | None = None,
) -> DeckPlan:
    """Apply explicit main-judgment corrections as one immutable proposal.

    Deck slide IDs and plan slide IDs are intentionally different.  The
    stable join is ``DeckSlide.intent_id`` -> ``DeckPlanSlide.intent.id``;
    using order or title here would make an edit vulnerable to later reorder.
    """

    patches = edit_plan.resolved_intent_patches()
    if not patches:
        return plan
    changed_slides = {}
    for patch in patches:
        slide = next((item for item in deck.slides if item.id == patch.slide_id), None)
        if slide is None:
            raise ValueError("intent patch targets an unknown slide")
        if not slide.intent_id:
            raise ValueError("target slide has no authored intent identity")
        if sum(item.intent_id == slide.intent_id for item in deck.slides) != 1:
            raise ValueError("target slide has an ambiguous shared authored identity in DeckIR")
        matches = [item for item in plan.slides if item.intent.id == slide.intent_id]
        if len(matches) != 1:
            raise ValueError("target slide intent is missing or ambiguous in DeckPlan")
        planned_slide = matches[0]
        if planned_slide.id in changed_slides:
            raise ValueError("intent patches target an ambiguous shared authored identity")
        source_refs = list(planned_slide.intent.source_refs)
        proofs = list(planned_slide.intent.calculation_proofs)
        has_grounding_additions = bool(
            patch.source_ref_additions or patch.calculation_proof_additions
        )
        if has_grounding_additions:
            if brief is None:
                raise ValueError("intent grounding additions require the original source brief")
            section_refs = {
                ref for section in brief.source_sections
                for ref in (section.source_refs or [section.source_ref])
            }
            allowed_refs = set(deck.source_refs) & set(brief.source_refs) & section_refs
            if any(ref not in allowed_refs for ref in patch.source_ref_additions):
                raise ValueError("intent grounding addition cites an unavailable source")
            for ref in patch.source_ref_additions:
                if ref not in source_refs:
                    source_refs.append(ref)
            for proof in patch.calculation_proof_additions:
                proof_refs = {term.source_ref for term in proof.terms} | {
                    proof.authorization_source_ref,
                }
                if not proof_refs.issubset(allowed_refs):
                    raise ValueError("intent calculation addition cites an unavailable source")
                if proof not in proofs:
                    proofs.append(proof)
        try:
            changed_intent = type(planned_slide.intent).model_validate({
                **planned_slide.intent.model_dump(),
                "key_message": patch.key_message,
                "source_refs": source_refs,
                "calculation_proofs": [proof.model_dump() for proof in proofs],
            })
        except ValidationError:
            # Do not expose original excerpts through Pydantic input_value.
            raise ValueError("intent proposal violates its typed schema or limit") from None
        if changed_intent == planned_slide.intent:
            raise ValueError("intent patch produced no material change")
        changed_slide = planned_slide.model_copy(update={
            "intent": changed_intent,
        })
        if has_grounding_additions:
            validate_planned_text_sources([changed_slide], brief)
        changed_slides[planned_slide.id] = changed_slide
    return plan.model_copy(
        update={
            "slides": [
                changed_slides.get(item.id, item)
                for item in plan.slides
            ],
        },
    )
