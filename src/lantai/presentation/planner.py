# -*- coding: utf-8 -*-
"""Deterministic, material-grounded planner for the presentation pipeline.

The stable path deliberately owns no model credentials.  It turns a structured
text outline into validated DeckIR while preserving source facts verbatim.  A
request with declared but unreadable materials fails closed instead of quietly
shipping the generic no-material baseline.
"""

from __future__ import annotations

import hashlib
import json
import re

from .design_intelligence import (
    CompositionPlanner,
    DeckRhythmPlanner,
    enrich_plan_with_design_context,
    resolve_native_composition_tokens,
)
from .material_extraction import PresentationMaterialError, read_material
from .models import (
    DeckIR,
    DeckPlan,
    DeckPlanSlide,
    DeckSlide,
    FactRecord,
    ImageObject,
    MetricObject,
    ObjectGroup,
    ObjectRelationship,
    PlanningMode,
    PresentationBrief,
    PresentationJobRequest,
    PresentationSourceCoverage,
    PresentationSourceSection,
    RelationshipType,
    SlideIntent,
    TextObject,
    VisualDirection,
)


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_SLIDE_PREFIX_RE = re.compile(
    r"^第\s*\d+\s*页\s*[：:、.\-]?\s*(.*)$",
    re.IGNORECASE,
)
_NUMBER_TOKEN_RE = re.compile(
    r"(?<![0-9A-Za-z.])\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)?%?",
)
_LIST_ITEM_RE = re.compile(
    r"^\s*(?:(?:[-*+•·●○▪▸▹①-⑳])|(?:\d{1,2}[.、。)]))\s*(.+?)\s*$",
)
_PROCESS_TITLE_RE = re.compile(r"路线|流程|阶段|步骤|路径|时间线|进程")
_COMPARISON_TITLE_RE = re.compile(
    r"对比|比较|差异|区别|差距|中美|ToB\s*/\s*ToC|业务定位|\bvs\.?\b",
    re.IGNORECASE,
)
_HIERARCHY_ROOT_RE = re.compile(r"→|阶梯|主线|路径|路线|框架|逻辑")
_METRIC_TITLE_RE = re.compile(r"关键数字|核心数据|关键指标|数据概览")
_QUOTE_TITLE_RE = re.compile(r"金句|引语|摘录")
def _clean_section_title(value: str) -> str:
    title = re.sub(r"\s+#+\s*$", "", value.strip())
    match = _SLIDE_PREFIX_RE.match(title)
    if match:
        title = match.group(1).strip() or title
    return title or "材料要点"


def _fit_source_sections(
    sections: list[PresentationSourceSection],
    target_count: int,
) -> list[PresentationSourceSection]:
    """Deterministically merge or split an outline to the requested page count."""

    fitted = list(sections)
    if not fitted:
        return fitted
    if len(fitted) > target_count:
        # Exact contiguous buckets preserve order and every source character.
        # Keep the first source_ref for old readers, but bind every contributing
        # material to the merged section and its eventual page.
        merged: list[PresentationSourceSection] = []
        for index in range(target_count):
            start = index * len(fitted) // target_count
            end = (index + 1) * len(fitted) // target_count
            consumed = fitted[start:end]
            merged.append(
                PresentationSourceSection(
                    title=" / ".join(item.title for item in consumed),
                    body="\n".join(item.body for item in consumed),
                    source_ref=consumed[0].source_ref,
                    document_title="\n".join(dict.fromkeys(
                        item.document_title for item in consumed
                        if item.document_title
                    )),
                    source_refs=list(
                        dict.fromkeys(
                            ref
                            for item in consumed
                            for ref in (item.source_refs or [item.source_ref])
                        )
                    ),
                ),
            )
        fitted = merged
    while len(fitted) < target_count:
        split_index = max(
            range(len(fitted)),
            key=lambda index: len(_split_text(fitted[index].body)),
        )
        source = fitted[split_index]
        blocks = _split_text(source.body)
        if len(blocks) < 2:
            # slide_count is a ceiling/target, never permission to duplicate
            # source content or invent a recap page.
            break
        middle = (len(blocks) + 1) // 2
        fitted[split_index : split_index + 1] = [
            PresentationSourceSection(
                title=f"{source.title}（1）",
                body="\n".join(blocks[:middle]),
                source_ref=source.source_ref,
                document_title=source.document_title,
                source_refs=list(source.source_refs),
            ),
            PresentationSourceSection(
                title=f"{source.title}（2）",
                body="\n".join(blocks[middle:]),
                source_ref=source.source_ref,
                document_title=source.document_title,
                source_refs=list(source.source_refs),
            ),
        ]
    return fitted[:target_count]


def _parse_sections(
    text: str,
    source_ref: str,
    *,
    parse_markdown_headings: bool = True,
) -> tuple[str, list[PresentationSourceSection]]:
    document_title = ""
    sections: list[PresentationSourceSection] = []
    current_title = ""
    current_lines: list[str] = []
    saw_section_heading = False
    section_heading_level: int | None = None

    def flush() -> None:
        nonlocal current_lines
        body = "\n".join(current_lines).strip()
        if body or current_title:
            sections.append(
                PresentationSourceSection(
                    title=_clean_section_title(
                        current_title or document_title or "材料开篇"
                    ),
                    body=body or current_title,
                    source_ref=source_ref,
                    document_title=document_title,
                ),
            )
        current_lines = []

    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.rstrip()
        heading = _HEADING_RE.match(line) if parse_markdown_headings else None
        if not heading:
            if line.strip() or current_lines:
                current_lines.append(line)
            continue
        level = len(heading.group(1))
        heading_text = heading.group(2).strip()
        if level == 1 and not saw_section_heading and not current_title:
            document_title = "\n".join(
                part for part in (document_title, heading_text) if part
            )
            continue
        if (
            section_heading_level is not None
            and level != section_heading_level
        ):
            if current_title:
                current_lines.append(heading_text)
            continue
        flush()
        current_title = heading_text
        saw_section_heading = True
        section_heading_level = section_heading_level or level
    flush()

    if not sections:
        body = text.strip()
        if body:
            sections.append(
                PresentationSourceSection(
                    title=document_title or "材料要点",
                    body=body,
                    source_ref=source_ref,
                    document_title=document_title,
                ),
            )
    return document_title, sections


def _fact_anchor(body: str) -> str:
    lines = [
        re.sub(
            r"^\s*(?:(?:[>*+\-]+|\d+[.、]|①-⑳)\s*)?",
            "",
            line,
        ).strip()
        for line in body.splitlines()
        if line.strip()
    ]
    if not lines:
        return ""
    first = lines[0]
    sentence = re.split(r"(?<=[。！？.!?])\s*", first, maxsplit=1)[0]
    return (sentence or first)[:120]


def _immutable_tokens(text: str) -> list[str]:
    return list(dict.fromkeys(_NUMBER_TOKEN_RE.findall(text)))


def _split_text(text: str, limit: int = 480) -> list[str]:
    normalized = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    if not normalized:
        return []

    # A slide outline is usually already authored as bullets or short
    # paragraphs.  Preserve those semantic units instead of collapsing the
    # whole section into one text box merely because it is under the character
    # limit.  Continuation lines remain attached to their preceding paragraph.
    logical_units: list[str] = []
    current_lines: list[str] = []

    def flush_unit() -> None:
        if current_lines:
            logical_units.append("\n".join(current_lines).strip())
            current_lines.clear()

    for raw_line in normalized.splitlines():
        line = raw_line.strip()
        if not line:
            flush_unit()
            continue
        item = _LIST_ITEM_RE.match(line)
        if item:
            flush_unit()
            logical_units.append(item.group(1).strip())
            continue
        current_lines.append(line)
    flush_unit()

    if not logical_units:
        logical_units = [normalized]

    pieces: list[str] = []
    for unit in logical_units:
        if len(unit) <= limit:
            pieces.append(unit)
            continue
        pieces.extend(
            piece.strip()
            for piece in re.split(r"(?<=[。！？；.!?;])", unit)
            if piece.strip()
        )

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        while len(piece) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(piece[:limit])
            piece = piece[limit:]
        if not piece:
            continue
        if len(piece) <= limit and not current:
            chunks.append(piece)
            continue
        candidate = f"{current}{piece}" if current else piece
        if len(candidate) <= limit:
            current = candidate
        else:
            chunks.append(current)
            current = piece
    if current:
        chunks.append(current)
    if len(chunks) > 12:
        raise PresentationMaterialError(
            "one outline section exceeds the stable twelve-block slide capacity",
        )
    return chunks


def _relationship_type(title: str, blocks: list[str]) -> RelationshipType:
    """Infer the authored relationship; block count is capacity only."""
    if _QUOTE_TITLE_RE.search(title):
        return RelationshipType.QUOTE
    if _COMPARISON_TITLE_RE.search(title):
        return RelationshipType.COMPARISON
    if _PROCESS_TITLE_RE.search(title):
        return RelationshipType.PROCESS
    if _HIERARCHY_ROOT_RE.search("\n".join(blocks)):
        return RelationshipType.HIERARCHY
    if _METRIC_TITLE_RE.search(title):
        return RelationshipType.METRIC_DASHBOARD
    return RelationshipType.COMPOSITION


def _grounded_layout(
    relationship: RelationshipType,
    blocks: list[str],
) -> str:
    """Map semantics to a legacy-compatible layout after intent is known."""
    count = len(blocks)
    if relationship == RelationshipType.PROCESS:
        if 2 <= count <= 5:
            return "process"
        if 6 <= count <= 9:
            return "hierarchy"
    if relationship == RelationshipType.COMPARISON and count >= 2:
        return "comparison"
    if relationship == RelationshipType.HIERARCHY:
        return "hierarchy"
    if relationship == RelationshipType.QUOTE:
        return "quote" if count == 1 else "two_column"
    if relationship in {
        RelationshipType.DATA_TREND,
        RelationshipType.METRIC_DASHBOARD,
    }:
        return "three_cards"
    if count >= 4:
        return "matrix"
    return "key_judgment"


def _visual_direction(theme_id: str) -> VisualDirection:
    return VisualDirection(
        visual_concept="editorial evidence map",
        palette=["#F6F2EA", "#14213D", "#D69E2E", "#FFFFFF"],
        typography="large editorial headlines with restrained body copy",
        density="medium",
        image_language="documentary images only when they explain evidence",
        chart_language="direct labels, quiet axes, one highlighted series",
        shape_language="open compositions, rules and fields; avoid card grids",
        grid="12-column asymmetric editorial grid",
        recurring_motif=f"source thread and page marker ({theme_id})",
    )


def _balanced_groups(
    blocks: list[str],
    *,
    max_groups: int,
    max_chars: int,
) -> list[str]:
    """Keep authored order while fitting dense facts into bounded surfaces."""
    group_count = min(max_groups, len(blocks))
    if not group_count:
        return []
    base, remainder = divmod(len(blocks), group_count)
    sizes = [
        base + (1 if index < remainder else 0) for index in range(group_count)
    ]
    groups: list[str] = []
    cursor = 0
    for size in sizes:
        group = "\n".join(blocks[cursor : cursor + size])
        cursor += size
        if len(group) > max_chars:
            raise PresentationMaterialError(
                "dense outline facts exceed the semantic surface capacity",
            )
        groups.append(group)
    return groups


def _hierarchy_content(blocks: list[str]) -> tuple[str, list[str]]:
    root_index = next(
        (
            index
            for index, block in enumerate(blocks)
            if _HIERARCHY_ROOT_RE.search(block)
        ),
        0,
    )
    root = blocks[root_index]
    if len(root) > 100:
        raise PresentationMaterialError(
            "hierarchy root exceeds the semantic surface capacity",
        )
    remaining = blocks[:root_index] + blocks[root_index + 1 :]
    return root, _balanced_groups(remaining, max_groups=4, max_chars=130)


def _metric_parts(block: str) -> tuple[str, str, str]:
    """Keep an authored metric intact while separating its visual hierarchy."""
    match = re.match(r"^([^:：]{1,24})([:：])(.*)$", block)
    if not match:
        return "关键数字：", block, ""
    label = f"{match.group(1).strip()}{match.group(2)}"
    payload = match.group(3).strip()
    detail_match = re.match(r"^([^,，]+)([,，].*)$", payload)
    if not detail_match:
        return label, payload, ""
    return label, detail_match.group(1).strip(), detail_match.group(2).strip()


def build_brief(request: PresentationJobRequest) -> PresentationBrief:
    source_refs: list[str] = []
    source_title = ""
    source_sections: list[PresentationSourceSection] = []
    source_coverage: list[PresentationSourceCoverage] = []
    for material in request.materials:
        text, coverage = read_material(material)
        source_coverage.append(coverage)
        # PDF extractors return literal text: a table's "#" column is not
        # Markdown authoring and must stay in its original source position.
        title, sections = _parse_sections(
            text, material.id,
            parse_markdown_headings=not coverage.method.startswith("pdf_"),
        )
        if sections:
            source_refs.append(material.id)
            source_title = source_title or title
            source_sections.extend(sections)
    required_facts = [
        anchor
        for section in source_sections
        if (anchor := _fact_anchor(section.body))
    ]
    immutable_data = _immutable_tokens(
        "\n".join(section.grounded_text for section in source_sections),
    )
    return PresentationBrief(
        objective=request.user_request,
        audience=request.audience,
        narrative="从背景与判断出发，逐步展开证据、方案和结论。",
        required_facts=required_facts,
        immutable_data=immutable_data,
        source_refs=source_refs,
        source_title=source_title,
        source_sections=source_sections,
        source_coverage=source_coverage,
        style_constraints=["16:9", "原生可编辑", "优先清晰与稳定", request.theme_id],
        prohibited_changes=[
            "不得虚构输入材料中的数据",
            "不得用整页截图代替可编辑对象",
        ],
    )


def build_deterministic_fixture_plan(
    request: PresentationJobRequest,
    brief: PresentationBrief,
) -> DeckPlan:
    """Build a source-grounded fixture plan without model intelligence.

    Production runtime calls this only when ``planning_mode`` is explicitly
    ``deterministic_fixture``.  Block-count heuristics are retained solely for
    repeatable tests and offline recovery fixtures.
    """
    if brief.source_sections:
        selected = _fit_source_sections(
            brief.source_sections,
            request.slide_count,
        )
        grounded_slides = []
        for index, section in enumerate(selected):
            blocks = _split_text(section.body)
            relationship = _relationship_type(section.title, blocks)
            layout = (
                "cover"
                if index == 0 and "封面" in section.title
                else "closing"
                if index == len(selected) - 1
                and re.search(r"结语|收束|总结|结束|保密", section.title)
                else _grounded_layout(relationship, blocks)
            )
            anchor = _fact_anchor(section.body)
            intent = SlideIntent(
                id=f"intent-{index + 1:03d}",
                purpose=(
                    "建立主题"
                    if index == 0
                    else "收束结论"
                    if index == len(selected) - 1
                    else "展开证据"
                ),
                key_message=anchor or section.title,
                evidence=blocks,
                relationship_type=relationship,
                visual_role=(
                    "opening"
                    if layout == "cover"
                    else "closing"
                    if layout == "closing"
                    else "evidence"
                ),
                semantic_layout=layout,
                source_refs=section.source_refs or [section.source_ref],
                required_facts=blocks,
                immutable_facts=_immutable_tokens(section.body),
                density="low" if layout in {"cover", "closing"} else "medium",
            )
            grounded_slides.append(
                DeckPlanSlide(
                    id=f"plan-slide-{index + 1:03d}",
                    order=index + 1,
                    title=section.title,
                    intent=intent,
                    content_blocks=blocks,
                ),
            )
        plan = DeckPlan(
            title=(brief.source_title or selected[0].title)[:42],
            narrative_arc=" → ".join(section.title for section in selected),
            visual_direction=_visual_direction(request.theme_id),
            source_coverage=list(brief.source_coverage),
            slides=grounded_slides,
        )
        return enrich_plan_with_design_context(request, brief, plan)

    raise PresentationMaterialError(
        "stable presentation generation requires source material or "
        "model-authored slides; refusing to invent filler content or data",
    )


# Compatibility for callers that intentionally exercise the deterministic
# fixture planner.  Production runtime does not call this alias.
build_plan = build_deterministic_fixture_plan


def _text(
    slide_no: int,
    role: str,
    slot: str,
    text: str,
    *,
    emphasis: bool = False,
) -> TextObject:
    return TextObject(
        id=f"s{slide_no:03d}-{slot}",
        role=role,
        slot=slot,
        text=text,
        emphasis=emphasis,
    )


def _legacy_fixture_objects(
    item: DeckPlanSlide,
    request: PresentationJobRequest,
):
    """Preserve the historical slot contract for explicit offline fixtures.

    This branch is intentionally unreachable from normal model planning.  It
    exists so old fixture checkpoints remain readable while production content
    is represented by semantic roles instead of legacy layout names.
    """

    slide_no = item.order
    blocks = item.content_blocks
    if item.intent.semantic_layout == "cover":
        cover_title = blocks[0] if blocks else item.title
        subtitle = "\n".join(blocks[1:]).strip() or request.audience
        return [
            _text(slide_no, "title", "title", cover_title, emphasis=True),
            _text(slide_no, "subtitle", "subtitle", subtitle[:120]),
        ]
    if item.intent.semantic_layout == "closing":
        closing_title = blocks[0] if blocks else item.title
        body = "\n".join(blocks[1:]) or request.audience
        return [
            _text(slide_no, "title", "title", closing_title, emphasis=True),
            _text(slide_no, "body", "body", body[:180]),
        ]
    layout = item.intent.semantic_layout
    if layout == "quote":
        return [
            _text(slide_no, "quote", "quote", blocks[0], emphasis=True),
            _text(slide_no, "attribution", "attribution", item.title),
        ]
    title = _text(slide_no, "title", "title", item.title, emphasis=True)
    if _METRIC_TITLE_RE.search(item.title) and layout == "three_cards":
        return [
            title,
            *[
                MetricObject(
                    id=f"s{slide_no:03d}-metric-{index:02d}",
                    label=label,
                    value=value,
                    detail=detail,
                    slot=(
                        f"card_{min(3, (index - 1) * 3 // len(blocks) + 1)}"
                    ),
                )
                for index, block in enumerate(blocks, start=1)
                for label, value, detail in [_metric_parts(block)]
            ],
        ]
    if _QUOTE_TITLE_RE.search(item.title) and layout in {
        "two_column",
        "three_cards",
        "matrix",
    }:
        max_groups = {"two_column": 2, "three_cards": 3, "matrix": 4}[layout]
        max_chars = {"two_column": 300, "three_cards": 150, "matrix": 160}[
            layout
        ]
        quote_blocks = _balanced_groups(
            blocks,
            max_groups=max_groups,
            max_chars=max_chars,
        )
        slot_prefix = {
            "two_column": ("left", "right"),
            "three_cards": ("card_1", "card_2", "card_3"),
            "matrix": ("cell_1", "cell_2", "cell_3", "cell_4"),
        }[layout]
        return [
            title,
            *[
                _text(slide_no, "quote", slot_prefix[index - 1], block)
                for index, block in enumerate(quote_blocks, start=1)
            ],
        ]
    if layout == "key_judgment":
        return [title, _text(slide_no, "body", "body", "\n".join(blocks))]
    if layout in {"two_column", "comparison"}:
        split_at = (len(blocks) + 1) // 2
        return [
            title,
            _text(slide_no, "body", "left", "\n".join(blocks[:split_at])),
            _text(slide_no, "body", "right", "\n".join(blocks[split_at:])),
        ]
    if layout == "hierarchy":
        root, nodes = _hierarchy_content(blocks)
        return [
            title,
            _text(slide_no, "body", "root", root, emphasis=True),
            *[
                _text(slide_no, "body", f"node_{index}", node)
                for index, node in enumerate(nodes, start=1)
            ],
        ]
    if layout == "three_cards":
        blocks = _balanced_groups(blocks, max_groups=3, max_chars=150)
    elif layout == "matrix":
        blocks = _balanced_groups(blocks, max_groups=4, max_chars=160)
    slot_prefix = {
        "three_cards": "card",
        "matrix": "cell",
        "process": "step",
    }.get(layout)
    if slot_prefix:
        return [
            title,
            *[
                _text(
                    slide_no,
                    "body",
                    f"{slot_prefix}_{index}",
                    block,
                )
                for index, block in enumerate(blocks, start=1)
            ],
        ]
    return [title, _text(slide_no, "body", "body", "\n".join(blocks))]


def _production_grounded_objects(
    item: DeckPlanSlide,
    request: PresentationJobRequest,
):
    """Create native semantic objects without choosing a visual template.

    Relationship recognition has already happened in the storyboard.  This
    function preserves those semantic units and exposes meaningful roles to the
    CompositionPlanner.  It never derives a relationship or a layout from the
    number of paragraphs.
    """

    slide_no = item.order
    blocks = [block.strip() for block in item.content_blocks if block.strip()]
    relationship = item.intent.relationship_type
    visual_role = item.intent.visual_role.casefold()

    if item.charts:
        return [
            _text(slide_no, "title", "title", item.title, emphasis=True),
            *[
                chart.model_copy(update={"frame": None})
                for chart in item.charts
            ],
            *[
                _text(slide_no, "evidence", f"evidence_{index}", block)
                for index, block in enumerate(blocks, start=1)
            ],
        ]

    if "opening" in visual_role:
        lead = blocks[0] if blocks else item.intent.key_message or item.title
        support = "\n".join(blocks[1:]).strip()
        objects = [
            _text(slide_no, "title", "hero_statement", lead, emphasis=True),
        ]
        if support:
            objects.append(_text(slide_no, "subtitle", "context", support))
        return objects

    if "closing" in visual_role:
        lead = item.intent.key_message or blocks[0] or item.title
        support_blocks = blocks if lead not in blocks else blocks[1:]
        objects = [
            _text(slide_no, "title", "closing_statement", lead, emphasis=True),
        ]
        if support_blocks:
            objects.append(
                _text(
                    slide_no,
                    "body",
                    "closing_support",
                    "\n".join(support_blocks),
                ),
            )
        return objects

    if relationship == RelationshipType.QUOTE:
        quotes = blocks or [item.intent.key_message]
        return [
            _text(slide_no, "title", "title", item.title, emphasis=True),
            *[
                _text(
                    slide_no,
                    "quote",
                    "quote" if len(quotes) == 1 else f"quote_{index}",
                    quote,
                    emphasis=index == 1,
                )
                for index, quote in enumerate(quotes, start=1)
            ],
        ]

    title = _text(slide_no, "title", "title", item.title, emphasis=True)

    if relationship == RelationshipType.METRIC_DASHBOARD:
        metrics: list[MetricObject] = []
        for index, block in enumerate(blocks, start=1):
            label, value, detail = _metric_parts(block)
            metrics.append(
                MetricObject(
                    id=f"s{slide_no:03d}-metric-{index:02d}",
                    label=label.rstrip("：:"),
                    value=value,
                    detail=detail,
                    slot=("lead_metric" if not metrics else f"support_metric_{index}"),
                ),
            )
        return [
            title,
            *metrics,
        ]

    if relationship in {
        RelationshipType.PROCESS,
        RelationshipType.CHRONOLOGY,
        RelationshipType.JOURNEY,
        RelationshipType.ROADMAP,
        RelationshipType.CAUSE_EFFECT,
    }:
        return [
            title,
            *[
                _text(slide_no, "step", f"step_{index}", block)
                for index, block in enumerate(blocks, start=1)
            ],
        ]

    if relationship in {
        RelationshipType.COMPARISON,
        RelationshipType.BEFORE_AFTER,
    }:
        if relationship == RelationshipType.BEFORE_AFTER and len(blocks) != 2:
            raise ValueError("before_after requires two authored semantic units")
        return [
            title,
            *[
                _text(slide_no, "comparison", f"comparison_{index}", block)
                for index, block in enumerate(blocks, start=1)
            ],
        ]

    if relationship in {
        RelationshipType.HIERARCHY,
        RelationshipType.LAYERS,
        RelationshipType.FUNNEL,
    }:
        root, nodes = _hierarchy_content(blocks)
        return [
            title,
            _text(slide_no, "thesis", "root", root, emphasis=True),
            *[
                _text(slide_no, "evidence", f"level_{index}", node)
                for index, node in enumerate(nodes, start=1)
            ],
        ]

    if relationship == RelationshipType.MATRIX:
        return [
            title,
            *[
                _text(slide_no, "evidence", f"matrix_item_{index}", block)
                for index, block in enumerate(blocks, start=1)
            ],
        ]

    if relationship in {
        RelationshipType.HERO_STATEMENT,
        RelationshipType.THESIS,
        RelationshipType.SUMMARY,
    }:
        lead = blocks[0] if blocks else item.intent.key_message
        return [
            title,
            _text(slide_no, "thesis", "lead", lead, emphasis=True),
            *[
                _text(slide_no, "evidence", f"support_{index}", block)
                for index, block in enumerate(blocks[1:], start=1)
            ],
        ]

    if relationship in {
        RelationshipType.DATA_TREND,
        RelationshipType.EVIDENCE_COLLECTION,
    }:
        return [
            title,
            *[
                _text(
                    slide_no,
                    "evidence",
                    ("lead_evidence" if index == 1 else f"evidence_{index}"),
                    block,
                    emphasis=index == 1,
                )
                for index, block in enumerate(blocks, start=1)
            ],
        ]

    # COMPOSITION/MIXED deliberately preserves multiple authored units.  The
    # planner will establish a focal hierarchy; it is not a square-root grid.
    return [
        title,
        *[
            _text(
                slide_no,
                "evidence" if index > 1 else "thesis",
                "lead" if index == 1 else f"support_{index - 1}",
                block,
                emphasis=index == 1,
            )
            for index, block in enumerate(blocks, start=1)
        ],
    ]


def _grounded_objects(item: DeckPlanSlide, request: PresentationJobRequest):
    if request.planning_mode == PlanningMode.DETERMINISTIC_FIXTURE:
        return _legacy_fixture_objects(item, request)
    return _production_grounded_objects(item, request)


def validate_slide_composition_capacity(
    request: PresentationJobRequest,
    slide: DeckPlanSlide,
    design_context,
) -> None:
    """Exercise the exact production object mapper and geometry capacity gate.

    Semantic page batches call this before their checkpoint is accepted so an
    over-segmented page is returned to the model-owned authoring stage.  The
    deterministic runtime may measure and reject, but it must never merge,
    discard, or silently append author content after that checkpoint.
    """

    composition_planner = CompositionPlanner()
    beat = DeckRhythmPlanner().plan([slide], design_context).beats[0]
    composition = composition_planner.plan_slide(
        slide,
        design_context,
        beat,
    )
    composition_planner.compose_objects(
        slide,
        _grounded_objects(slide, request),
        design_context,
        composition,
    )


def build_deck_ir(request: PresentationJobRequest, plan: DeckPlan) -> DeckIR:
    if plan.design_context is None or plan.visual_plan is None or any(
        item.composition_plan is None for item in plan.slides
    ):
        inferred_brief = PresentationBrief(
            objective=request.user_request,
            audience=request.audience,
            narrative=plan.narrative_arc,
            required_facts=list(
                dict.fromkeys(
                    fact
                    for item in plan.slides
                    for fact in item.intent.required_facts
                ),
            ),
            immutable_data=list(
                dict.fromkeys(
                    fact
                    for item in plan.slides
                    for fact in item.intent.immutable_facts
                ),
            ),
            source_refs=list(
                dict.fromkeys(
                    ref
                    for item in plan.slides
                    for ref in item.intent.source_refs
                ),
            ),
            source_title=plan.title,
        )
        plan = enrich_plan_with_design_context(request, inferred_brief, plan)
    if plan.design_context is None:
        raise RuntimeError("presentation design context was not resolved")

    missing_content = [
        item.id for item in plan.slides if not item.content_blocks and not item.charts
    ]
    if missing_content:
        raise PresentationMaterialError(
            "every planned slide requires grounded content blocks; refusing "
            f"placeholder generation for {', '.join(missing_content)}",
        )
    fact_by_key: dict[tuple[str, tuple[str, ...], bool], FactRecord] = {}
    for item in plan.slides:
        immutable = set(item.intent.immutable_facts)
        for fact in [*item.intent.required_facts, *item.intent.evidence]:
            cleaned = fact.strip()
            if not cleaned:
                continue
            key = (
                cleaned,
                tuple(dict.fromkeys(item.intent.source_refs)),
                cleaned in immutable,
            )
            if key not in fact_by_key:
                fact_by_key[key] = FactRecord(
                    fact_id=f"fact-{len(fact_by_key) + 1:04d}",
                    text=cleaned,
                    source_refs=list(key[1]),
                    immutable=key[2],
                )

    slides: list[DeckSlide] = []
    composition_planner = CompositionPlanner()
    for item in plan.slides:
        if item.composition_plan is None:
            raise RuntimeError(f"missing composition plan for {item.id}")
        objects = composition_planner.compose_objects(
            item,
            _grounded_objects(item, request),
            plan.design_context,
            item.composition_plan,
        )
        object_ids = [obj.id for obj in objects]
        title_ids = [
            obj.id
            for obj in objects
            if isinstance(obj, TextObject) and obj.role == "title"
        ]
        content_ids = [
            obj_id for obj_id in object_ids if obj_id not in title_ids
        ]
        fact_ids = [
            fact.fact_id
            for fact in fact_by_key.values()
            if fact.text
            in {
                *item.intent.required_facts,
                *item.intent.evidence,
            }
            and set(fact.source_refs).issubset(set(item.intent.source_refs))
        ]
        groups = (
            [
                ObjectGroup(
                    group_id=f"group-{item.order:03d}-content",
                    role=item.intent.visual_role,
                    object_ids=content_ids,
                ),
            ]
            if content_ids
            else []
        )
        relationships = (
            [
                ObjectRelationship(
                    relationship_id=f"relationship-{item.order:03d}-primary",
                    relationship_type=item.intent.relationship_type,
                    source_object_ids=title_ids or [object_ids[0]],
                    target_object_ids=content_ids,
                    label=item.intent.key_message,
                ),
            ]
            if content_ids and object_ids
            else []
        )
        slide = DeckSlide(
            id=f"slide-{item.order:03d}",
            layout_id="freeform",
            semantic_layout=item.intent.semantic_layout,
            intent_id=item.intent.id,
            objects=objects,
            speaker_notes=item.intent.key_message,
            editability_mode=item.intent.editability_requirement,
            source_fact_ids=fact_ids,
            asset_ids=sorted(
                {
                    obj.asset_id
                    for obj in objects
                    if isinstance(obj, ImageObject) and obj.asset_id
                },
            ),
            object_groups=groups,
            relationships=relationships,
        )
        payload = slide.model_copy(update={"scene_hash": None}).model_dump(
            mode="json",
        )
        scene_hash = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        ).hexdigest()
        slides.append(slide.model_copy(update={"scene_hash": scene_hash}))
    return DeckIR(
        id=f"deck-{plan.title[:12]}-{request.seed}",
        title=plan.title,
        theme_id=request.theme_id,
        language=request.language,
        seed=request.seed,
        slides=slides,
        source_refs=list(
            dict.fromkeys(
                source_ref
                for item in plan.slides
                for source_ref in item.intent.source_refs
            ),
        ),
        source_coverage=list(plan.source_coverage),
        required_facts=list(
            dict.fromkeys(
                fact
                for item in plan.slides
                for fact in item.intent.required_facts
            ),
        ),
        immutable_data=list(
            dict.fromkeys(
                fact
                for item in plan.slides
                for fact in item.intent.immutable_facts
            ),
        ),
        visual_direction=plan.visual_direction,
        design_profile=plan.design_profile,
        design_context=plan.design_context,
        composition_tokens=resolve_native_composition_tokens(
            plan.design_context,
        ),
        visual_plan=plan.visual_plan,
        facts=list(fact_by_key.values()),
    )
