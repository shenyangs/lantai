#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate and render ten controlled Presentation Skill v3 benchmarks.

The fixture data is deliberately isolated under tests/fixtures. It exercises the
composition and QA pipeline; it is not a fallback content source for users.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image

from lantai.presentation.design_intelligence import (
    compose_deck_ir_with_design_context,
)
from lantai.presentation.layouts import check_capacity
from lantai.presentation.models import (
    AssetRecord,
    ChartObject,
    CompositionEngine,
    DeckIR,
    DeckPlan,
    DeckPlanSlide,
    DeckSlide,
    EditabilityMode,
    FactRecord,
    ImageObject,
    MetricObject,
    ObjectGroup,
    ObjectFrame,
    ObjectRelationship,
    PlanningMode,
    PresentationBrief,
    PresentationJobRequest,
    RelationshipType,
    ShapeObject,
    SlideIntent,
    TableObject,
    TextObject,
    VisualDirection,
)
from lantai.presentation.pptx_compiler import NativePptxCompiler
from lantai.presentation.quality import inspect_pptx, inspect_rendered_slides
from lantai.presentation.render import render_pptx


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/presentation_v3_benchmarks.json"
ASSET_ROOT = ROOT / "tests/fixtures/presentation_v3_assets"
DEFAULT_OUTPUT = ROOT / ".artifacts/presentation-v3-benchmarks-20260914"
ASSET_FILES = {
    "culture-exhibition": "culture-alley.png",
    "future-of-work-keynote": "future-of-work.png",
    "impact-report": "impact-classroom.png",
    "leadership-training": "leadership-coaching.png",
    "product-launch": "product-launch.png",
    "research-brief": "research-team.png",
}
PIPELINE_PATHS = (
    "src/lantai/presentation",
    "src/lantai/app/routers/presentations.py",
    "src/lantai/agents/tools/skill_identity.py",
    "src/lantai/agents/tools/read_skill.py",
    "src/lantai/agents/tools/skill_resources.py",
    "src/lantai/agents/skills/pptx-en",
    "src/lantai/agents/skills/pptx-zh",
    "scripts/run_presentation_v3_benchmarks.py",
    "tests/fixtures/presentation_v3_benchmarks.json",
    "tests/fixtures/presentation_v3_assets",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    for file_path in sorted(
        item for item in path.rglob("*") if item.is_file()
    ):
        relative = file_path.relative_to(path).as_posix()
        if relative.startswith("__pycache__/") or relative.endswith(
            (".pyc", ".DS_Store"),
        ):
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256(file_path).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _pipeline_bindings() -> dict[str, Any]:
    files = sorted(
        {
            path.relative_to(ROOT).as_posix()
            for configured in PIPELINE_PATHS
            for path in (
                [ROOT / configured]
                if (ROOT / configured).is_file()
                else (ROOT / configured).rglob("*")
            )
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix != ".pyc"
        },
    )
    file_hashes = {relative: _sha256(ROOT / relative) for relative in files}
    canonical = json.dumps(
        file_hashes,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    status = _git("status", "--porcelain", "--", *PIPELINE_PATHS)
    return {
        "implementation_commit": _git("rev-parse", "HEAD"),
        "pipeline_paths_clean": not bool(status),
        "pipeline_dirty_entries": status.splitlines(),
        "pipeline_file_sha256": file_hashes,
        "pipeline_bundle_sha256": hashlib.sha256(canonical).hexdigest(),
        "skill_content_sha256": {
            "pptx-en": _tree_sha256(ROOT / "src/lantai/agents/skills/pptx-en"),
            "pptx-zh": _tree_sha256(ROOT / "src/lantai/agents/skills/pptx-zh"),
        },
        "fixture_sha256": _sha256(FIXTURE),
    }


def _frame(x: float, y: float, w: float, h: float) -> ObjectFrame:
    return ObjectFrame(x=x, y=y, w=w, h=h)


def _title(deck_id: str, slide_no: int, text: str) -> TextObject:
    return TextObject(
        id=f"{deck_id}-s{slide_no}-title",
        role="title",
        text=text,
        slot="title",
        emphasis=True,
    )


def _text(
    deck_id: str,
    slide_no: int,
    index: int,
    text: str,
    slot: str,
    *,
    role: str = "body",
    frame: ObjectFrame | None = None,
) -> TextObject:
    return TextObject(
        id=f"{deck_id}-s{slide_no}-{slot}-{index}",
        role=role,
        text=text,
        slot=slot,
        frame=frame,
    )


def _freeform_objects(data: dict[str, Any], slide_no: int) -> list[Any]:
    deck_id = data["id"]
    objects: list[Any] = [
        _text(
            deck_id,
            slide_no,
            0,
            data["statement"],
            "canvas",
            role="title",
            frame=_frame(0.78, 0.55, 10.9, 0.9),
        ),
    ]
    x_positions = (0.9, 4.68, 8.46)
    for index, (x, evidence) in enumerate(
        zip(x_positions, data["evidence"], strict=True),
    ):
        objects.extend(
            [
                _text(
                    deck_id,
                    slide_no,
                    index + 1,
                    evidence,
                    f"evidence_{index + 1}",
                    frame=_frame(x, 2.0, 2.95, 1.1),
                ),
                ShapeObject(
                    id=f"{deck_id}-s{slide_no}-node-{index + 1}",
                    shape_type="ellipse",
                    frame=_frame(x + 1.24, 3.35, 0.46, 0.46),
                    fill_color=data["palette"][2].lstrip("#"),
                    line_color=data["palette"][2].lstrip("#"),
                ),
                ShapeObject(
                    id=f"{deck_id}-s{slide_no}-stem-{index + 1}",
                    shape_type="line",
                    frame=_frame(x + 1.47, 3.8, 0.01, 0.88),
                    line_color=data["palette"][1].lstrip("#"),
                    line_width=1.5,
                ),
            ],
        )
    objects.extend(
        [
            ShapeObject(
                id=f"{deck_id}-s{slide_no}-bus",
                shape_type="line",
                frame=_frame(2.37, 4.68, 7.56, 0.01),
                line_color=data["palette"][1].lstrip("#"),
                line_width=1.5,
            ),
            ShapeObject(
                id=f"{deck_id}-s{slide_no}-outcome-stem",
                shape_type="line",
                frame=_frame(6.15, 4.68, 0.01, 0.56),
                line_color=data["palette"][2].lstrip("#"),
                line_width=2.5,
            ),
            _text(
                deck_id,
                slide_no,
                10,
                data["fact"],
                "outcome",
                role="heading",
                frame=_frame(3.95, 5.42, 4.4, 0.92),
            ),
        ],
    )
    return objects


def _content_slide(
    data: dict[str, Any],
    layout: str,
    slide_no: int,
    image_path: Path | None,
    asset_id: str | None,
) -> DeckSlide:
    deck_id = data["id"]
    default_title = {
        "key_judgment": "核心判断",
        "process": "行动路径",
        "data_conclusion": "数据指向同一个结论",
        "image_focus": "先看见现场，再理解判断",
        "comparison": "旧方式与新选择",
        "two_column": "两个视角，一次转向",
        "three_cards": "三件必须同时成立的事",
        "agenda": "今天只讨论三个问题",
        "table": "把证据放在同一张桌面上",
        "chart": "趋势比单点数字更重要",
        "hierarchy": "从核心判断拆到可执行层",
        "matrix": "四个信号共同构成答案",
        "section": "从材料进入叙事",
    }.get(layout, data["title"])
    slide_titles = data.get("slide_titles", [])
    title_index = slide_no - 2
    title_text = (
        slide_titles[title_index]
        if title_index < len(slide_titles)
        else default_title
    )
    objects: list[Any]
    if layout == "freeform":
        objects = _freeform_objects(data, slide_no)
    elif layout == "quote":
        objects = [
            _text(
                deck_id,
                slide_no,
                1,
                data["quote"],
                "quote",
                role="quote",
            ),
            _text(
                deck_id,
                slide_no,
                2,
                data["attribution"],
                "attribution",
            ),
        ]
        if image_path and asset_id and any(
            token in data["concept"]
            for token in ("人文", "档案", "温暖", "训练", "研究", "公益")
        ):
            objects.append(
                ImageObject(
                    id=f"{deck_id}-s{slide_no}-quote-image",
                    path=str(image_path),
                    asset_id=asset_id,
                    alt=data["image_caption"],
                    slot="quote_visual",
                ),
            )
    elif layout == "section":
        objects = [
            _title(deck_id, slide_no, title_text),
            _text(deck_id, slide_no, 1, data["statement"], "body"),
        ]
    elif layout == "key_judgment":
        objects = [
            _title(deck_id, slide_no, title_text),
            _text(deck_id, slide_no, 1, data["statement"], "body"),
            *[
                _text(deck_id, slide_no, index + 2, value, "body")
                for index, value in enumerate(data["evidence"])
            ],
        ]
    elif layout == "process":
        objects = [_title(deck_id, slide_no, title_text)] + [
            _text(deck_id, slide_no, index, value, f"step_{index}")
            for index, value in enumerate(data["stages"], start=1)
        ]
    elif layout in {"comparison", "two_column"}:
        objects = [
            _title(deck_id, slide_no, title_text),
            _text(deck_id, slide_no, 1, data["left"], "left"),
            _text(deck_id, slide_no, 2, data["right"], "right"),
        ]
    elif layout == "three_cards":
        objects = [_title(deck_id, slide_no, title_text)] + [
            _text(deck_id, slide_no, index, value, f"card_{index}")
            for index, value in enumerate(data["cards"], start=1)
        ]
    elif layout == "agenda":
        agenda = "\n\n".join(
            f"0{index}  {value}"
            for index, value in enumerate(data["evidence"], start=1)
        )
        objects = [
            _title(deck_id, slide_no, title_text),
            _text(deck_id, slide_no, 1, agenda, "items"),
        ]
    elif layout == "hierarchy":
        nodes = [*data["evidence"], data["fact"]]
        objects = [
            _title(deck_id, slide_no, title_text),
            _text(deck_id, slide_no, 1, data["statement"], "root"),
            *[
                _text(deck_id, slide_no, index + 1, value, f"node_{index}")
                for index, value in enumerate(nodes, start=1)
            ],
        ]
    elif layout == "matrix":
        cells = [*data["evidence"], data["fact"]]
        objects = [_title(deck_id, slide_no, title_text)] + [
            _text(deck_id, slide_no, index, value, f"cell_{index}")
            for index, value in enumerate(cells, start=1)
        ]
    elif layout == "table":
        objects = [
            _title(deck_id, slide_no, title_text),
            TableObject(
                id=f"{deck_id}-s{slide_no}-table",
                headers=data["table_headers"],
                rows=data["table_rows"],
                slot="table",
            ),
            _text(deck_id, slide_no, 1, data["statement"], "conclusion"),
        ]
    elif layout in {"chart", "data_conclusion"}:
        required_chart_fields = {
            "chart_type",
            "chart_relationship",
            "chart_unit",
            "chart_series_label",
            "chart_number_format",
            "chart_derivation",
        }
        missing_chart_fields = sorted(required_chart_fields - data.keys())
        if missing_chart_fields:
            raise ValueError(
                f"benchmark {deck_id} has a chart without an explicit semantic contract: "
                + ", ".join(missing_chart_fields),
            )
        chart = ChartObject(
            id=f"{deck_id}-s{slide_no}-chart",
            chart_type=data["chart_type"],
            categories=data["chart_categories"],
            series={data["chart_series_label"]: data["chart_values"]},
            unit=data["chart_unit"],
            number_format=data["chart_number_format"],
            derivation=data["chart_derivation"],
            source_refs=[f"fixture://presentation-v3/{deck_id}"],
            slot="chart",
        )
        objects = [_title(deck_id, slide_no, title_text), chart]
        if layout == "data_conclusion":
            metrics = data.get("chart_metrics")
            if not isinstance(metrics, list) or len(metrics) != 2:
                raise ValueError(
                    f"benchmark {deck_id} data_conclusion requires two explicit chart_metrics",
                )
            objects.extend(
                [
                    MetricObject(
                        id=f"{deck_id}-s{slide_no}-metric-1",
                        label=metrics[0]["label"],
                        value=metrics[0]["value"],
                        detail=metrics[0]["detail"],
                        slot="metric_1",
                    ),
                    MetricObject(
                        id=f"{deck_id}-s{slide_no}-metric-2",
                        label=metrics[1]["label"],
                        value=metrics[1]["value"],
                        detail=metrics[1]["detail"],
                        slot="metric_2",
                    ),
                    _text(
                        deck_id,
                        slide_no,
                        3,
                        data["statement"],
                        "conclusion",
                    ),
                ],
            )
        else:
            objects.append(
                _text(
                    deck_id,
                    slide_no,
                    1,
                    data["statement"],
                    "conclusion",
                ),
            )
    elif layout == "image_focus":
        if not image_path or not asset_id:
            raise ValueError(
                f"benchmark {deck_id} requests image_focus without a real asset",
            )
        objects = [
            _title(deck_id, slide_no, title_text),
            ImageObject(
                id=f"{deck_id}-s{slide_no}-image",
                path=str(image_path),
                asset_id=asset_id,
                alt=data["image_caption"],
                slot="hero",
            ),
            _text(
                deck_id,
                slide_no,
                1,
                data["image_caption"],
                "caption",
            ),
        ]
    else:
        raise ValueError(f"unsupported benchmark layout: {layout}")
    object_ids = [obj.id for obj in objects]
    relationship_type = {
        "comparison": RelationshipType.COMPARISON,
        "two_column": RelationshipType.COMPARISON,
        "process": RelationshipType.PROCESS,
        "hierarchy": RelationshipType.HIERARCHY,
        "matrix": RelationshipType.MATRIX,
        "chart": RelationshipType(
            data.get("chart_relationship", RelationshipType.DATA_TREND.value),
        ),
        "data_conclusion": RelationshipType(
            data.get("chart_relationship", RelationshipType.DATA_TREND.value),
        ),
        "quote": RelationshipType.QUOTE,
        "image_focus": RelationshipType.HERO_VISUAL,
        "section": RelationshipType.HERO_STATEMENT,
    }.get(layout, RelationshipType.COMPOSITION)
    return DeckSlide(
        id=f"{deck_id}-slide-{slide_no}",
        layout_id=layout,
        intent_id=f"{deck_id}-intent-{slide_no}",
        objects=objects,
        speaker_notes=f"Controlled benchmark fixture: {data['fact']}",
        source_fact_ids=[f"{deck_id}-fact-primary"],
        asset_ids=sorted(
            {
                obj.asset_id
                for obj in objects
                if isinstance(obj, ImageObject) and obj.asset_id
            },
        ),
        object_groups=[
            ObjectGroup(
                group_id=f"{deck_id}-s{slide_no}-group-main",
                role="primary-message",
                object_ids=object_ids,
            ),
        ],
        relationships=(
            [
                ObjectRelationship(
                    relationship_id=f"{deck_id}-s{slide_no}-relation-main",
                    relationship_type=relationship_type,
                    source_object_ids=[object_ids[0]],
                    target_object_ids=object_ids[1:],
                    label="visual narrative",
                ),
            ]
            if len(object_ids) > 1
            else []
        ),
    )


def _build_deck(data: dict[str, Any], deck_dir: Path, variant: int) -> DeckIR:
    deck_id = data["id"]
    image_path = (
        ASSET_ROOT / ASSET_FILES[deck_id]
        if deck_id in ASSET_FILES
        else None
    )
    if image_path and not image_path.is_file():
        raise FileNotFoundError(image_path)
    source_ref = f"fixture://presentation-v3/{deck_id}"
    asset_id = f"{deck_id}-asset-primary" if image_path else None
    cover_objects: list[Any] = [
        _title(deck_id, 1, data["title"]),
        _text(deck_id, 1, 1, data["subtitle"], "subtitle"),
    ]
    dedicated_visual_slide = any(
        layout in {"image_focus", "quote"} for layout in data["layouts"]
    )
    if image_path and asset_id and not dedicated_visual_slide and any(
        token in data["concept"]
        for token in ("发布", "人文", "档案", "温暖", "训练", "研究", "公益", "未来", "演讲")
    ):
        cover_objects.append(
            ImageObject(
                id=f"{deck_id}-s1-cover-image",
                path=str(image_path),
                asset_id=asset_id,
                alt=data["image_caption"],
                slot="cover_visual",
            ),
        )
    slides = [
        DeckSlide(
            id=f"{deck_id}-slide-1",
            layout_id="cover",
            intent_id=f"{deck_id}-intent-1",
            objects=cover_objects,
            source_fact_ids=[f"{deck_id}-fact-primary"],
        ),
    ]
    for index, layout in enumerate(data["layouts"], start=2):
        slides.append(
            _content_slide(
                data,
                layout,
                index,
                image_path,
                asset_id,
            ),
        )
    final_no = len(slides) + 1
    slides.append(
        DeckSlide(
            id=f"{deck_id}-slide-{final_no}",
            layout_id="closing",
            intent_id=f"{deck_id}-intent-{final_no}",
            objects=[
                _title(deck_id, final_no, data["closing"]),
                _text(deck_id, final_no, 1, data["fact"], "body"),
            ],
            source_fact_ids=[f"{deck_id}-fact-primary"],
        ),
    )
    return DeckIR(
        id=f"benchmark-{deck_id}",
        title=data["title"],
        theme_id="lantai-warm",
        seed=variant,
        slides=slides,
        source_refs=[source_ref],
        required_facts=[data["fact"]],
        immutable_data=[data["fact"]],
        visual_direction=VisualDirection(
            visual_concept=data["concept"],
            palette=[f"#{value.lstrip('#')}" for value in data["palette"]],
            typography="大标题、高对比、正文不低于 17pt",
            density="low",
            image_language="单一主体、解释性构图",
            chart_language="直接结论、克制坐标轴",
            shape_language="少量原生几何关系",
            grid="12列宽银幕网格",
            recurring_motif="细线与单一强调色",
        ),
        facts=[
            FactRecord(
                fact_id=f"{deck_id}-fact-primary",
                text=data["fact"],
                source_refs=[source_ref],
                immutable=True,
            ),
        ],
        assets=(
            [
                AssetRecord(
                    asset_id=asset_id,
                    local_path=str(image_path),
                    source_ref=f"generated://imagegen-benchmark/{deck_id}",
                    rights_status="generated",
                    sha256=_sha256(image_path),
                    dimensions=(1672, 941),
                    usage_role="explanation",
                    focal_point=(0.5, 0.5),
                    dedupe_key=_sha256(image_path),
                    ingestion_status="ready",
                    upload_policy="never",
                ),
            ]
            if image_path and asset_id
            else []
        ),
    )


def _slide_relationship(slide: DeckSlide) -> RelationshipType:
    if slide.relationships:
        return slide.relationships[0].relationship_type
    if slide.layout_id == "cover":
        return RelationshipType.HERO_STATEMENT
    if slide.layout_id == "closing":
        return RelationshipType.SUMMARY
    return RelationshipType.COMPOSITION


def _slide_text(slide: DeckSlide) -> tuple[str, list[str]]:
    title = ""
    evidence: list[str] = []
    for obj in slide.objects:
        if isinstance(obj, TextObject):
            if obj.role == "title" and not title:
                title = obj.text
            else:
                evidence.append(obj.text)
        elif isinstance(obj, MetricObject):
            evidence.append(
                " ".join(
                    value
                    for value in (obj.label, str(obj.value), obj.detail)
                    if value
                ),
            )
        elif isinstance(obj, ChartObject):
            evidence.append(
                f"{obj.chart_type}: "
                + "; ".join(
                    f"{label} {values}"
                    for label, values in obj.series.items()
                ),
            )
        elif isinstance(obj, TableObject):
            evidence.append(
                " / ".join(obj.headers)
                + ": "
                + "; ".join(" / ".join(row) for row in obj.rows),
            )
        elif isinstance(obj, ImageObject):
            evidence.append(obj.alt)
    return title or (evidence[0] if evidence else slide.id), evidence


def _design_inputs(
    data: dict[str, Any],
    deck: DeckIR,
    variant: int,
) -> tuple[PresentationJobRequest, PresentationBrief, DeckPlan]:
    request = PresentationJobRequest(
        conversation_id=f"benchmark-{data['id']}",
        user_request=(
            f"以{data['concept']}的设计方向，为{data['title']}制作一份完整专业演示。"
            "根据内容关系动态构图，避免卡片堆砌和重复模板。"
        ),
        audience="与主题相关的决策者和专业受众",
        slide_count=len(deck.slides),
        planning_mode=PlanningMode.DETERMINISTIC_FIXTURE,
        composition_engine=CompositionEngine.NATIVE_PPTX,
        seed=variant,
        visual_verification_required=True,
        vision_critique_required=True,
    )
    brief = PresentationBrief(
        objective=data["statement"],
        audience=request.audience,
        narrative=(
            f"从{data['title']}的核心判断出发，以证据、关系和行动形成完整叙事。"
        ),
        required_facts=[data["fact"]],
        immutable_data=[data["fact"]],
        source_refs=list(deck.source_refs),
        source_title=data["title"],
        style_constraints=[data["concept"]],
    )
    plan_slides: list[DeckPlanSlide] = []
    for order, slide in enumerate(deck.slides, start=1):
        title, evidence = _slide_text(slide)
        relationship = _slide_relationship(slide)
        visual_role = (
            "opening"
            if order == 1
            else "closing"
            if order == len(deck.slides)
            else "quote"
            if relationship == RelationshipType.QUOTE
            else "hero_visual"
            if relationship == RelationshipType.HERO_VISUAL
            else "evidence"
            if relationship
            in {
                RelationshipType.DATA_TREND,
                RelationshipType.METRIC_DASHBOARD,
                RelationshipType.EVIDENCE_COLLECTION,
                RelationshipType.MATRIX,
            }
            else "explanation"
        )
        plan_slides.append(
            DeckPlanSlide(
                id=slide.id,
                order=order,
                title=title,
                intent=SlideIntent(
                    id=slide.intent_id or f"{data['id']}-intent-{order}",
                    purpose=(
                        "建立主题"
                        if order == 1
                        else "收束行动"
                        if order == len(deck.slides)
                        else "推进叙事并建立证据关系"
                    ),
                    key_message=title,
                    evidence=evidence,
                    relationship_type=relationship,
                    visual_role=visual_role,
                    source_refs=list(deck.source_refs),
                    required_facts=[data["fact"]],
                    immutable_facts=[data["fact"]],
                    editability_requirement=EditabilityMode.NATIVE_EDITABLE,
                    density=(
                        "high"
                        if sum(len(item) for item in evidence) > 260
                        else "low"
                        if sum(len(item) for item in evidence) < 70
                        else "medium"
                    ),
                ),
                content_blocks=evidence,
            ),
        )
    plan = DeckPlan(
        title=deck.title,
        narrative_arc=brief.narrative,
        visual_direction=deck.visual_direction,
        slides=plan_slides,
    )
    return request, brief, plan


def _montage(images: tuple[Path, ...], output: Path) -> None:
    thumb_w, thumb_h, gap = 640, 360, 24
    columns = 2
    rows = math.ceil(len(images) / columns)
    canvas = Image.new(
        "RGB",
        (
            columns * thumb_w + (columns + 1) * gap,
            rows * thumb_h + (rows + 1) * gap,
        ),
        "#D8D8D8",
    )
    for index, path in enumerate(images):
        with Image.open(path) as source:
            thumb = source.convert("RGB").resize((thumb_w, thumb_h))
        x = gap + (index % columns) * (thumb_w + gap)
        y = gap + (index // columns) * (thumb_h + gap)
        canvas.paste(thumb, (x, y))
    canvas.save(output, "PNG")


def run(output_root: Path, selected: set[str] | None = None) -> dict[str, Any]:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    decks = fixture["decks"]
    if len(decks) != 10:
        raise RuntimeError(
            "presentation v3 benchmark fixture must contain exactly 10 decks",
        )
    output_root.mkdir(parents=True, exist_ok=True)
    compiler = NativePptxCompiler()
    bindings = _pipeline_bindings()
    generator_config = {
        "pipeline_version": "lantai-presentation-v3",
        "planning_mode": PlanningMode.DETERMINISTIC_FIXTURE.value,
        "composition_engine": "native_pptx",
        "semantic_planner": {
            "state": "not_run",
            "model_id": None,
            "model_config_fingerprint": None,
            "reason": (
                "controlled fixture content was converted into SlideIntent and semantic "
                "relationship plans before profile-driven dynamic composition; no model "
                "planner was invoked"
            ),
        },
        "visual_critic": {
            "state": "not_run",
            "model_id": None,
            "model_config_fingerprint": None,
            "reason": "independent visual acceptance is recorded after montage review",
        },
        "fixture_only": True,
    }
    generator_config_sha256 = hashlib.sha256(
        json.dumps(
            generator_config,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    ).hexdigest()
    results: list[dict[str, Any]] = []
    for variant, data in enumerate(decks):
        if selected and data["id"] not in selected:
            continue
        deck_dir = output_root / data["id"]
        render_dir = deck_dir / "render"
        deck_dir.mkdir(parents=True, exist_ok=True)
        deck = _build_deck(data, deck_dir, variant)
        request, brief, plan = _design_inputs(data, deck, variant)
        deck = compose_deck_ir_with_design_context(
            deck,
            request,
            brief,
            plan=plan,
        )
        capacity = [
            {
                "slide_id": slide.id,
                "violations": [item.message for item in check_capacity(slide)],
            }
            for slide in deck.slides
            if check_capacity(slide)
        ]
        deck_path = deck_dir / "deck-ir.json"
        deck_path.write_text(deck.model_dump_json(indent=2), encoding="utf-8")
        pptx_path = deck_dir / "presentation.pptx"
        compilation = compiler.compile(deck, pptx_path)
        rendered = render_pptx(pptx_path, render_dir)
        structural = inspect_pptx(
            deck,
            pptx_path,
            rendered_slide_count=len(rendered.slide_images),
        )
        render_issues = inspect_rendered_slides(rendered.slide_images)
        montage = deck_dir / "montage.png"
        _montage(rendered.slide_images, montage)
        error_issues = [
            issue.model_dump(mode="json")
            for issue in [*structural.issues, *render_issues]
            if issue.severity == "error"
        ]
        passed = (
            not capacity and not compilation.degradations and not error_issues
        )
        results.append(
            {
                "id": data["id"],
                "title": data["title"],
                "design_profile": deck.design_context.profile.style_profile.id,
                "design_profile_source": deck.design_context.profile.source,
                "design_context_identity": deck.design_context.profile.identity,
                "passed_deterministic_checks": passed,
                "manual_visual_status": "pending",
                "slide_count": len(deck.slides),
                "layouts": [slide.layout_id for slide in deck.slides],
                "pptx": str(pptx_path),
                "pptx_sha256": _sha256(pptx_path),
                "deck_ir": str(deck_path),
                "deck_ir_sha256": _sha256(deck_path),
                "montage": str(montage),
                "montage_sha256": _sha256(montage),
                "renderer": rendered.renderer_id,
                "renderer_version": rendered.renderer_version,
                "editable_object_count": compilation.editable_object_count,
                "degradations": compilation.degradations,
                "capacity_violations": capacity,
                "error_issues": error_issues,
            },
        )
    report = {
        "schema": "com.lantai.presentation-benchmark-result.v2",
        "fixture": str(FIXTURE),
        "output_root": str(output_root),
        "bindings": bindings,
        "generator_config": generator_config,
        "generator_config_sha256": generator_config_sha256,
        "expected_deck_count": 10,
        "completed_deck_count": len(results),
        "all_deterministic_checks_passed": bool(results)
        and all(item["passed_deterministic_checks"] for item in results),
        "evidence": {
            "composition": "passed" if results else "not_run",
            "render": (
                "passed"
                if results and all(item["renderer"] for item in results)
                else "failed"
            ),
            "semantic_model_planning": "not_run",
            "semantic_visual_critic": "not_run",
            "independent_visual_acceptance": "not_run",
        },
        "blocking_gaps": [
            "semantic model planning is not exercised by the controlled fixture benchmark",
            "semantic visual critic is not exercised by the controlled fixture benchmark",
            "independent visual acceptance has not been recorded",
        ],
        "visual_acceptance": "not_run",
        "decks": results,
    }
    (output_root / "benchmark-results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--id", action="append", dest="ids")
    args = parser.parse_args()
    report = run(args.output.resolve(), set(args.ids) if args.ids else None)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["all_deterministic_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
