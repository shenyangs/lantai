#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate the first artwork-led Editorial Executive acceptance deck.

This is a controlled benchmark, not a production fallback and not a fixed
template library.  It exercises the new Slide Art Director with multiple
meaning-led composition candidates for decisive pages, renders every
candidate, records selection evidence, and then composes a 12-page native,
editable deck.  All scenario metrics are explicitly marked as benchmark data.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import shutil
import sys
from typing import Any, Callable

from PIL import Image, ImageDraw, ImageFont, ImageOps


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from lantai.presentation.models import (  # noqa: E402
    DeckIR,
    DeckRhythmRole,
    DeckSlide,
    DeckVisualBeat,
    DeckVisualPlan,
    NativeCompositionTokens,
    ObjectFrame,
    PresentationDesignContext,
    RelationshipType,
    RichTextRun,
    ShapeObject,
    SlideIntent,
    TextObject,
)
from lantai.presentation.pptx_compiler import NativePptxCompiler  # noqa: E402
from lantai.presentation.quality import (  # noqa: E402
    inspect_pptx,
    inspect_rendered_slides,
)
from lantai.presentation.render import render_pptx  # noqa: E402
from lantai.presentation.slide_art_director import (  # noqa: E402
    ArtDirectionBrief,
    CompositionCandidate,
    SlideArtDirector,
)


DEFAULT_OUTPUT = (
    REPOSITORY_ROOT / ".artifacts/editorial-executive-art-director-v1"
)
DEFAULT_CONTEXT = (
    REPOSITORY_ROOT
    / ".artifacts/reference-kimi-analysis-v4/presentation-design-profile.json"
)
DEFAULT_REFERENCE_RENDER = (
    REPOSITORY_ROOT / ".artifacts/reference-kimi-analysis-v4/render"
)

CANVAS = "F7F5F0"
INK = "11110F"
MUTED = "716E67"
SUBTLE = "DDD9D0"
FAINT = "ECE9E2"
ACCENT = "C94732"
ACCENT_PALE = "EBD3CA"
WHITE = "FFFFFF"
BLACK = "0A0A09"
OLIVE = "77755D"

TOKENS = NativeCompositionTokens(
    canvas=CANVAS,
    ink=INK,
    muted=MUTED,
    accent=ACCENT,
    subtle=SUBTLE,
    title_font="Songti SC",
    body_font="PingFang SC",
    title_size=38,
    body_size=18,
    caption_size=12,
)

SELECTION = {2: "b", 4: "c", 6: "b", 7: "c", 9: "b"}


def f(x: float, y: float, w: float, h: float) -> ObjectFrame:
    return ObjectFrame(x=x, y=y, w=w, h=h)


def t(
    slide: int,
    name: str,
    text: str,
    frame: ObjectFrame,
    *,
    size: float = 18,
    color: str = INK,
    role: str = "body",
    emphasis: bool = False,
    align: str = "left",
    valign: str = "top",
    prefix: str = "final",
) -> TextObject:
    return TextObject(
        id=f"art-{prefix}-s{slide}-{name}",
        role=role,
        text=text,
        slot="title" if role == "title" else "body",
        font_size=size,
        color=color,
        emphasis=emphasis,
        align=align,
        valign=valign,
        frame=frame,
    )


def rt(
    slide: int,
    name: str,
    runs: list[RichTextRun],
    frame: ObjectFrame,
    *,
    role: str = "body",
    align: str = "left",
    valign: str = "top",
    prefix: str = "final",
) -> TextObject:
    return TextObject(
        id=f"art-{prefix}-s{slide}-{name}",
        role=role,
        text="".join(run.text for run in runs),
        font_size=17,
        color=INK,
        align=align,
        valign=valign,
        frame=frame,
        runs=runs,
    )


def sh(
    slide: int,
    name: str,
    frame: ObjectFrame,
    *,
    shape: str = "rect",
    fill: str | None = None,
    line: str | None = None,
    width: float = 0,
    prefix: str = "final",
) -> ShapeObject:
    return ShapeObject(
        id=f"art-{prefix}-s{slide}-{name}",
        shape_type=shape,
        frame=frame,
        fill_color=fill,
        line_color=line or fill,
        line_width=width,
    )


def rule(
    slide: int,
    name: str,
    frame: ObjectFrame,
    *,
    color: str = SUBTLE,
    width: float = 1,
    prefix: str = "final",
) -> ShapeObject:
    return sh(
        slide,
        name,
        frame,
        shape="line",
        line=color,
        width=width,
        prefix=prefix,
    )


def bg(slide: int, color: str, *, prefix: str = "final") -> ShapeObject:
    return sh(slide, "background", f(0, 0, 13.333, 7.5), fill=color, prefix=prefix)


def meta(
    slide: int,
    section: str,
    *,
    dark: bool = False,
    rail: str = "footer",
    prefix: str = "final",
) -> list[Any]:
    primary = WHITE if dark else INK
    secondary = "B9B6AE" if dark else MUTED
    objects: list[Any] = []
    if rail == "top":
        objects.extend(
            [
                rule(slide, "meta-top-rule", f(0.72, 0.52, 11.86, 0), color=primary, width=0.8, prefix=prefix),
                t(slide, "meta-section", section.upper(), f(0.72, 0.24, 4.8, 0.2), size=17, color=secondary, role="meta", emphasis=True, prefix=prefix),
                t(slide, "meta-folio", f"{slide:02d}", f(11.95, 0.22, 0.62, 0.22), size=17, color=secondary, role="meta", align="right", prefix=prefix),
            ],
        )
    elif rail == "footer":
        objects.extend(
            [
                rule(slide, "meta-bottom-rule", f(0.72, 7.06, 11.86, 0), color=primary, width=0.7, prefix=prefix),
                t(slide, "meta-section", section.upper(), f(0.72, 7.15, 4.8, 0.18), size=17, color=secondary, role="meta", emphasis=True, prefix=prefix),
                t(slide, "meta-folio", f"{slide:02d} / 12", f(11.5, 7.15, 1.08, 0.18), size=17, color=secondary, role="meta", align="right", prefix=prefix),
            ],
        )
    return objects


def eyebrow(
    slide: int,
    value: str,
    *,
    x: float = 0.78,
    y: float = 0.72,
    color: str = ACCENT,
    prefix: str = "final",
) -> TextObject:
    return t(
        slide,
        "eyebrow",
        value.upper(),
        f(x, y, 4.6, 0.22),
        size=17,
        color=color,
        role="meta",
        emphasis=True,
        prefix=prefix,
    )


def intent(
    slide: int,
    key_message: str,
    relationship: RelationshipType,
    *,
    purpose: str,
    density: str = "medium",
) -> SlideIntent:
    return SlideIntent(
        id=f"art-intent-{slide}",
        purpose=purpose,
        key_message=key_message,
        relationship_type=relationship,
        visual_role="editorial evidence",
        semantic_layout="explicit-art-direction",
        source_refs=[],
        density=density,
    )


INTENTS = {
    1: intent(1, "企业 AI 的第二阶段是任务经营", RelationshipType.HERO_STATEMENT, purpose="open with the strategic shift", density="low"),
    2: intent(2, "分水岭不在模型，而在任务是否被改写", RelationshipType.THESIS, purpose="state the central thesis", density="medium"),
    3: intent(3, "组织价值沿四个成熟阶段跃迁", RelationshipType.PROCESS, purpose="show maturity progression", density="high"),
    4: intent(4, "AI 原生运营把线性交接改成闭环经营", RelationshipType.BEFORE_AFTER, purpose="contrast operating models", density="high"),
    5: intent(5, "规模化之前必须回答四个经营问题", RelationshipType.EVIDENCE_COLLECTION, purpose="frame operating questions", density="high"),
    6: intent(6, "方法被固化后，价值才同时出现在速度、质量与复用", RelationshipType.METRIC_DASHBOARD, purpose="anchor the value field", density="high"),
    7: intent(7, "价值在方法固化后出现跃迁，而非线性增长", RelationshipType.DATA_TREND, purpose="explain the change curve", density="high"),
    8: intent(8, "一个闭环案例比十个功能列表更能证明价值", RelationshipType.BEFORE_AFTER, purpose="ground the thesis in a case", density="high"),
    9: intent(9, "AI 原生工作台是五层任务系统，而不是一个聊天框", RelationshipType.HIERARCHY, purpose="show the operating system", density="high"),
    10: intent(10, "可规模化的前提，是把失败模式提前变成控制点", RelationshipType.MATRIX, purpose="show risk controls", density="high"),
    11: intent(11, "90 天只做三件事：找任务、固方法、建复利", RelationshipType.ROADMAP, purpose="translate strategy into action", density="high"),
    12: intent(12, "先让任务被看见，再让方法被复用，最后形成组织复利", RelationshipType.SUMMARY, purpose="close with a memorable synthesis", density="low"),
}


def slide_1(prefix: str = "final") -> DeckSlide:
    objects: list[Any] = [bg(1, CANVAS, prefix=prefix)]
    objects += [
        rule(1, "rail-a", f(0.74, 0.56, 2.3, 0), color=INK, width=1.2, prefix=prefix),
        t(1, "series", "ENTERPRISE AI / OPERATING THESIS", f(0.74, 0.68, 5.8, 0.26), size=17, color=MUTED, role="meta", emphasis=True, prefix=prefix),
        t(1, "title-top", "企业 AI 的", f(0.74, 1.6, 6.2, 0.86), size=52, color=INK, role="title", emphasis=True, prefix=prefix),
        t(1, "title-main", "第二阶段", f(0.74, 2.42, 6.2, 1.12), size=66, color=ACCENT, role="title", emphasis=True, prefix=prefix),
        t(1, "subtitle", "从工具采购，到任务经营", f(0.8, 3.92, 5.5, 0.5), size=24, color=INK, role="heading", emphasis=True, prefix=prefix),
        t(1, "dek", "当模型能力快速趋同时，组织价值取决于：\n任务如何被拆解、方法如何被沉淀、结果如何被复用。", f(0.8, 4.72, 5.9, 1.0), size=18, color=MUTED, prefix=prefix),
        t(1, "index", "01", f(9.1, 1.28, 2.8, 1.4), size=72, color=INK, role="heading", emphasis=True, align="right", prefix=prefix),
        rule(1, "index-rule", f(8.36, 2.88, 3.5, 0), color=ACCENT, width=4, prefix=prefix),
        t(1, "index-note", "一份关于企业 AI\n进入经营深水区的观察", f(8.36, 3.16, 3.5, 0.88), size=18, color=INK, align="right", prefix=prefix),
        t(1, "date", "2026 · CONTROLLED BENCHMARK", f(8.36, 5.86, 3.5, 0.28), size=17, color=MUTED, role="meta", align="right", prefix=prefix),
        rule(1, "footer-rule", f(0.74, 6.82, 11.12, 0), color=INK, width=0.9, prefix=prefix),
        t(1, "footer", "LANTAI PRESENTATION / EDITORIAL EXECUTIVE", f(0.74, 6.96, 7.0, 0.22), size=17, color=MUTED, role="meta", prefix=prefix),
    ]
    return DeckSlide(id=f"art-{prefix}-slide-1", layout_id="freeform", intent_id=INTENTS[1].id, objects=objects)


def slide_2(variant: str, prefix: str) -> DeckSlide:
    objects: list[Any] = [bg(2, CANVAS, prefix=prefix), *meta(2, "central thesis", rail="footer", prefix=prefix)]
    if variant == "a":
        objects += [
            eyebrow(2, "THE DIVIDE", prefix=prefix),
            t(2, "title", "真正的分水岭，\n不在模型能力", f(0.76, 1.22, 7.0, 1.6), size=46, role="title", emphasis=True, prefix=prefix),
            t(2, "pivot", "而在", f(8.55, 1.3, 2.8, 0.6), size=20, color=MUTED, align="right", prefix=prefix),
            t(2, "anchor", "任务有没有\n被改写", f(7.18, 2.02, 4.3, 1.65), size=48, color=ACCENT, role="title", emphasis=True, align="right", prefix=prefix),
            rule(2, "divider", f(0.78, 3.98, 11.72, 0), color=INK, width=1.2, prefix=prefix),
        ]
        xs = [0.8, 4.25, 8.35]
        blocks = [
            ("01", "输入是否清楚", "谁提出、为谁服务、什么结果才算完成。"),
            ("02", "过程是否可见", "判断、证据、审批与修改能否留下轨迹。"),
            ("03", "方法是否复用", "下一次任务能否站在这一次的经验之上。"),
        ]
        for index, (number, title, body) in enumerate(blocks):
            x = xs[index]
            objects += [
                t(2, f"num-{index}", number, f(x, 4.38, 0.5, 0.3), size=17, color=ACCENT, emphasis=True, prefix=prefix),
                t(2, f"block-title-{index}", title, f(x, 4.9, 3.0, 0.36), size=21, role="heading", emphasis=True, prefix=prefix),
                t(2, f"block-body-{index}", body, f(x, 5.48, 3.05, 0.82), size=17, color=MUTED, prefix=prefix),
            ]
    elif variant == "b":
        objects += [
            eyebrow(2, "THE DIVIDE", prefix=prefix),
            t(2, "premise", "模型能力", f(0.76, 1.26, 3.0, 0.42), size=22, color=MUTED, role="heading", prefix=prefix),
            t(2, "strike", "会快速趋同", f(0.76, 1.74, 3.6, 0.7), size=38, color=INK, role="title", emphasis=True, prefix=prefix),
            rule(2, "premise-rule", f(0.78, 2.66, 3.58, 0), color=SUBTLE, width=1, prefix=prefix),
            t(2, "premise-detail", "更大的参数、更长的上下文，\n很快会从优势变成标配。", f(0.78, 3.02, 3.42, 0.9), size=17, color=MUTED, prefix=prefix),
            sh(2, "anchor-field", f(4.88, 0.0, 5.05, 7.06), fill=BLACK, prefix=prefix),
            t(2, "anchor-kicker", "真正拉开差距的", f(5.28, 1.22, 4.18, 0.34), size=18, color="BDBAB2", emphasis=True, prefix=prefix),
            t(2, "anchor", "是任务\n被重新设计", f(5.24, 1.86, 4.16, 1.78), size=45, color=WHITE, role="title", emphasis=True, prefix=prefix),
            rule(2, "anchor-rule", f(5.3, 4.08, 2.82, 0), color=ACCENT, width=4, prefix=prefix),
            t(2, "anchor-detail", "输入 → 判断 → 协作 → 交付\n每一环都变成可运行、可检查、可复用的系统。", f(5.28, 4.48, 4.1, 1.22), size=18, color="D5D2CB", prefix=prefix),
            t(2, "right-index", "01 / 02 / 03", f(10.45, 1.24, 1.7, 0.24), size=17, color=MUTED, role="meta", prefix=prefix),
            t(2, "right-a", "清楚的结果", f(10.45, 1.86, 2.1, 0.34), size=19, emphasis=True, prefix=prefix),
            t(2, "right-a-body", "目标、受众与验收\n不再隐含在对话里。", f(10.45, 2.36, 2.05, 0.68), size=17, color=MUTED, prefix=prefix),
            t(2, "right-b", "可见的过程", f(10.45, 3.46, 2.1, 0.34), size=19, emphasis=True, prefix=prefix),
            t(2, "right-b-body", "证据、修改与责任\n都能被追溯。", f(10.45, 3.96, 2.05, 0.68), size=17, color=MUTED, prefix=prefix),
            t(2, "right-c", "复用的方法", f(10.45, 5.08, 2.1, 0.34), size=19, emphasis=True, prefix=prefix),
            t(2, "right-c-body", "好的工作方式\n成为组织资产。", f(10.45, 5.58, 2.05, 0.68), size=17, color=MUTED, prefix=prefix),
        ]
    else:
        objects += [
            eyebrow(2, "THE DIVIDE", prefix=prefix),
            t(2, "title", "买到一个更强的模型，\n不等于得到一个更强的组织", f(0.78, 1.2, 10.9, 1.25), size=40, role="title", emphasis=True, prefix=prefix),
            t(2, "anchor", "任务被改写", f(0.78, 3.0, 5.6, 0.92), size=48, color=ACCENT, role="title", emphasis=True, prefix=prefix),
            t(2, "anchor-detail", "才是能力从个人效率进入组织复利的起点。", f(0.82, 4.02, 5.4, 0.5), size=20, color=INK, emphasis=True, prefix=prefix),
            rule(2, "vertical", f(7.12, 1.22, 0.01, 4.96), color=INK, width=1, prefix=prefix),
            t(2, "question", "判断一个任务有没有被改写，\n只看三个问题：", f(7.72, 1.44, 4.2, 0.8), size=21, emphasis=True, prefix=prefix),
            rt(2, "questions", [
                RichTextRun(text="01  ", bold=True, color=ACCENT, font_size=18), RichTextRun(text="结果是否明确且可验证\n", bold=True, font_size=20),
                RichTextRun(text="02  ", bold=True, color=ACCENT, font_size=18), RichTextRun(text="过程是否留下可追溯证据\n", bold=True, font_size=20),
                RichTextRun(text="03  ", bold=True, color=ACCENT, font_size=18), RichTextRun(text="方法是否能被下一次直接复用", bold=True, font_size=20),
            ], f(7.72, 2.76, 4.5, 2.5), prefix=prefix),
            t(2, "closing", "模型是能力供给；任务设计才是价值系统。", f(7.72, 5.6, 4.4, 0.45), size=18, color=MUTED, prefix=prefix),
        ]
    return DeckSlide(id=f"art-{prefix}-slide-2", layout_id="freeform", intent_id=INTENTS[2].id, objects=objects)


def slide_3(prefix: str = "final") -> DeckSlide:
    objects: list[Any] = [bg(3, CANVAS, prefix=prefix), *meta(3, "maturity progression", rail="top", prefix=prefix)]
    objects += [
        t(3, "title", "组织价值不是匀速增长，\n而是在方法固化处发生跃迁", f(0.76, 0.92, 8.1, 1.0), size=38, role="title", emphasis=True, prefix=prefix),
        t(3, "side-note", "四个阶段不是采购清单，\n而是 operating model 的变化。", f(9.62, 1.0, 2.7, 0.78), size=17, color=MUTED, align="right", prefix=prefix),
    ]
    stages = [
        ("01", "工具接入", "个人效率", "会问、会写、会总结", 2.34, 0.0),
        ("02", "场景验证", "局部收益", "找到高频且高摩擦任务", 3.0, 0.35),
        ("03", "流程重构", "结构跃迁", "把判断、证据与协作写进流程", 3.72, 0.75),
        ("04", "任务经营", "组织复利", "方法可度量、可复用、可持续改进", 4.44, 1.18),
    ]
    xs = [0.78, 3.78, 6.82, 9.82]
    widths = [2.52, 2.52, 2.52, 2.72]
    for index, (number, label, value, detail, y, offset) in enumerate(stages):
        x = xs[index]
        objects += [
            t(3, f"stage-number-{index}", number, f(x, y - 0.34, 0.52, 0.28), size=17, color=ACCENT if index >= 2 else MUTED, emphasis=True, prefix=prefix),
            rule(3, f"stage-rule-{index}", f(x, y, widths[index], 0), color=ACCENT if index >= 2 else INK, width=3 if index == 2 else 1.2, prefix=prefix),
            t(3, f"stage-label-{index}", label, f(x, y + 0.25, widths[index], 0.36), size=21, emphasis=True, prefix=prefix),
            t(3, f"stage-value-{index}", value, f(x, y + 0.86, widths[index], 0.5), size=27, color=ACCENT if index >= 2 else INK, role="heading", emphasis=True, prefix=prefix),
            t(3, f"stage-detail-{index}", detail, f(x, y + 1.48, widths[index], 0.82), size=17, color=MUTED, prefix=prefix),
        ]
        if index < 3:
            objects.append(rule(3, f"bridge-{index}", f(x + widths[index] + 0.16, y + 0.02, 0.22, 0), color=SUBTLE, width=1, prefix=prefix))
    objects += [
        rule(3, "threshold", f(6.54, 2.02, 0.01, 4.52), color=ACCENT, width=1.6, prefix=prefix),
        t(3, "threshold-label", "方法固化阈值", f(5.42, 6.58, 2.25, 0.26), size=17, color=ACCENT, role="meta", align="center", emphasis=True, prefix=prefix),
    ]
    return DeckSlide(id=f"art-{prefix}-slide-3", layout_id="freeform", intent_id=INTENTS[3].id, objects=objects)


def slide_4(variant: str, prefix: str) -> DeckSlide:
    objects: list[Any] = [bg(4, CANVAS, prefix=prefix), *meta(4, "operating model", rail="footer", prefix=prefix)]
    title = t(4, "title", "把线性交接改成闭环经营，\n才是 AI 原生工作方式", f(0.76, 0.64, 8.2, 1.0), size=36, role="title", emphasis=True, prefix=prefix)
    objects.append(title)
    rows = [
        ("入口", "收到需求，再找材料", "意图、受众、约束同时进入"),
        ("判断", "人在工具之间搬运信息", "证据与判断在同一条任务链"),
        ("协作", "一次次发文件、等反馈", "人只在关键分歧与审批点介入"),
        ("沉淀", "交付完成，经验随人离开", "过程与方法自动成为下一次起点"),
    ]
    if variant == "a":
        objects += [
            t(4, "old-label", "旧：线性交接", f(0.78, 1.94, 4.72, 0.42), size=21, color=MUTED, emphasis=True, prefix=prefix),
            t(4, "new-label", "新：任务闭环", f(7.16, 1.94, 4.72, 0.42), size=21, color=ACCENT, emphasis=True, prefix=prefix),
            rule(4, "center", f(6.58, 1.84, 0.01, 4.52), color=INK, width=1.2, prefix=prefix),
        ]
        for index, (label, old, new) in enumerate(rows):
            y = 2.58 + index * 0.9
            objects += [
                t(4, f"label-{index}", label, f(0.8, y, 0.64, 0.3), size=17, color=MUTED, emphasis=True, prefix=prefix),
                t(4, f"old-{index}", old, f(1.6, y, 3.9, 0.42), size=18, prefix=prefix),
                t(4, f"new-{index}", new, f(7.18, y, 4.65, 0.42), size=18, emphasis=True, prefix=prefix),
                rule(4, f"row-{index}", f(0.8, y + 0.62, 11.08, 0), color=SUBTLE, width=0.7, prefix=prefix),
            ]
    elif variant == "b":
        objects += [
            sh(4, "old-field", f(0, 1.82, 5.18, 5.24), fill=FAINT, prefix=prefix),
            t(4, "old-label", "LINEAR", f(0.78, 2.12, 2.0, 0.24), size=17, color=MUTED, role="meta", emphasis=True, prefix=prefix),
            t(4, "new-label", "CLOSED LOOP", f(7.0, 2.12, 2.5, 0.24), size=17, color=ACCENT, role="meta", emphasis=True, prefix=prefix),
            t(4, "old-stack", "需求\n\n材料\n\n草稿\n\n审批\n\n交付", f(0.82, 2.62, 1.3, 3.65), size=20, color=INK, emphasis=True, prefix=prefix),
            rule(4, "old-spine", f(2.52, 2.7, 0.01, 3.3), color=MUTED, width=1.2, prefix=prefix),
            t(4, "old-detail", "每一步都在等待上一步完成；\n信息随文件流转，责任随消息漂移。", f(2.9, 2.64, 1.75, 2.2), size=17, color=MUTED, prefix=prefix),
            sh(4, "loop", f(7.05, 2.55, 3.82, 3.02), shape="ellipse", line=INK, width=1.4, prefix=prefix),
            t(4, "loop-a", "意图", f(6.68, 2.44, 1.2, 0.32), size=18, emphasis=True, prefix=prefix),
            t(4, "loop-b", "证据", f(10.62, 3.58, 1.2, 0.32), size=18, emphasis=True, prefix=prefix),
            t(4, "loop-c", "交付", f(8.68, 5.48, 1.2, 0.32), size=18, emphasis=True, prefix=prefix),
            t(4, "loop-d", "修订", f(6.34, 4.58, 1.2, 0.32), size=18, emphasis=True, prefix=prefix),
            t(4, "loop-center", "任务状态\n始终可见", f(8.1, 3.62, 1.72, 0.78), size=20, color=ACCENT, emphasis=True, align="center", valign="mid", prefix=prefix),
            t(4, "loop-note", "人只在判断、分歧与授权点介入", f(7.0, 6.16, 4.42, 0.34), size=17, color=MUTED, align="center", prefix=prefix),
        ]
    else:
        objects += [
            t(4, "old-label", "从前：四次交接", f(0.78, 1.94, 3.2, 0.36), size=20, color=MUTED, emphasis=True, prefix=prefix),
            t(4, "new-label", "现在：一个任务闭环", f(6.25, 1.94, 4.4, 0.36), size=20, color=ACCENT, emphasis=True, prefix=prefix),
        ]
        for index, (label, old, new) in enumerate(rows):
            y = 2.58 + index * 0.92
            objects += [
                t(4, f"row-num-{index}", f"0{index + 1}", f(0.8, y, 0.5, 0.3), size=17, color=MUTED, emphasis=True, prefix=prefix),
                t(4, f"row-label-{index}", label, f(1.42, y, 0.75, 0.3), size=17, color=INK, emphasis=True, prefix=prefix),
                t(4, f"old-{index}", old, f(2.38, y, 2.75, 0.38), size=17, color=MUTED, prefix=prefix),
                sh(4, f"pivot-{index}", f(5.44, y + 0.02, 0.42, 0.22), shape="chevron", fill=ACCENT if index == 3 else SUBTLE, line=ACCENT if index == 3 else SUBTLE, prefix=prefix),
                t(4, f"new-{index}", new, f(6.28, y, 5.3, 0.42), size=18, emphasis=True, prefix=prefix),
                rule(4, f"row-rule-{index}", f(0.8, y + 0.62, 10.78, 0), color=SUBTLE, width=0.7, prefix=prefix),
            ]
        objects += [
            sh(4, "ownership", f(9.9, 0.0, 3.43, 1.58), fill=BLACK, prefix=prefix),
            t(4, "ownership-kicker", "OWNERSHIP", f(10.28, 0.36, 2.58, 0.22), size=17, color="B9B6AE", role="meta", emphasis=True, prefix=prefix),
            t(4, "ownership-main", "一个 writer\n一条版本链", f(10.28, 0.78, 2.62, 0.64), size=20, color=WHITE, emphasis=True, prefix=prefix),
            t(4, "verdict", "闭环不是把步骤画成圆；而是让状态、责任与证据始终属于同一个任务。", f(8.44, 6.22, 3.14, 0.58), size=17, color=ACCENT, emphasis=True, align="right", prefix=prefix),
        ]
    return DeckSlide(id=f"art-{prefix}-slide-4", layout_id="freeform", intent_id=INTENTS[4].id, objects=objects)


def slide_5(prefix: str = "final") -> DeckSlide:
    objects: list[Any] = [bg(5, CANVAS, prefix=prefix), *meta(5, "operating questions", rail="top", prefix=prefix)]
    objects += [
        t(5, "title", "规模化之前，先回答四个经营问题", f(0.76, 0.9, 8.6, 0.62), size=38, role="title", emphasis=True, prefix=prefix),
        t(5, "dek", "答案不是一句口号，而是一组能被任务日志、版本记录与交付结果持续证明的事实。", f(0.78, 1.62, 7.5, 0.5), size=18, color=MUTED, prefix=prefix),
        t(5, "question-index", "01", f(0.78, 2.54, 0.76, 0.52), size=30, color=ACCENT, emphasis=True, prefix=prefix),
        t(5, "question-main", "谁真正使用？", f(1.7, 2.48, 4.5, 0.58), size=30, emphasis=True, prefix=prefix),
        t(5, "question-main-body", "不是注册用户，而是每周把真实任务交给系统、并愿意在关键节点继续反馈的人。", f(1.72, 3.24, 4.35, 1.12), size=18, color=MUTED, prefix=prefix),
        t(5, "main-proof", "看任务周活，\n不看对话次数", f(1.72, 4.72, 3.95, 0.86), size=24, color=ACCENT, emphasis=True, prefix=prefix),
        rule(5, "main-bottom", f(1.72, 5.9, 3.98, 0), color=INK, width=2, prefix=prefix),
    ]
    questions = [
        ("02", "价值出现在哪？", "时间、质量、风险或复用，必须至少有一项发生可核验改变。"),
        ("03", "方法能否复用？", "下一次是否从结构化经验开始，而不是重新解释一遍背景。"),
        ("04", "成本是否可控？", "模型、人工与协作成本，是否随规模扩大而被持续看见。"),
    ]
    for index, (number, heading, body) in enumerate(questions):
        y = 2.44 + index * 1.28
        objects += [
            t(5, f"side-num-{index}", number, f(7.06, y, 0.54, 0.28), size=17, color=ACCENT, emphasis=True, prefix=prefix),
            t(5, f"side-title-{index}", heading, f(7.86, y - 0.04, 3.72, 0.38), size=21, emphasis=True, prefix=prefix),
            t(5, f"side-body-{index}", body, f(7.86, y + 0.48, 4.05, 0.66), size=17, color=MUTED, prefix=prefix),
            rule(5, f"side-rule-{index}", f(7.06, y + 1.04, 4.86, 0), color=SUBTLE, width=0.8, prefix=prefix),
        ]
    objects += [
        sh(5, "verdict-field", f(6.82, 6.26, 5.78, 0.58), fill=BLACK, prefix=prefix),
        t(5, "verdict", "四个问题都没有证据时，规模化只是把不确定性放大。", f(7.12, 6.4, 5.16, 0.26), size=17, color=WHITE, emphasis=True, align="right", prefix=prefix),
    ]
    return DeckSlide(id=f"art-{prefix}-slide-5", layout_id="freeform", intent_id=INTENTS[5].id, objects=objects)


def slide_6(variant: str, prefix: str) -> DeckSlide:
    objects: list[Any] = [bg(6, CANVAS, prefix=prefix), *meta(6, "value field", rail="footer", prefix=prefix)]
    objects += [
        eyebrow(6, "CONTROLLED BENCHMARK / N=30", prefix=prefix),
        t(6, "title", "方法被固化后，价值才同时出现在\n速度、质量与复用", f(0.76, 1.08, 8.55, 0.92), size=36, role="title", emphasis=True, prefix=prefix),
    ]
    if variant == "a":
        objects += [
            t(6, "hero-value", "57%", f(0.78, 2.42, 4.18, 1.26), size=70, color=ACCENT, emphasis=True, prefix=prefix),
            t(6, "hero-label", "平均交付时长缩短", f(0.82, 3.7, 3.86, 0.36), size=20, emphasis=True, prefix=prefix),
            t(6, "hero-detail", "42 min → 18 min\n同一受控任务集，连续 12 周", f(0.82, 4.32, 3.62, 0.86), size=17, color=MUTED, prefix=prefix),
            rule(6, "hero-rule", f(5.08, 2.42, 0.01, 3.18), color=INK, width=1.1, prefix=prefix),
        ]
        metrics = [("94%", "关键事实保留"), ("2.3", "平均人工交接"), ("4.2×", "方法周复用")]
        for index, (value, label) in enumerate(metrics):
            y = 2.38 + index * 1.04
            objects += [
                t(6, f"metric-value-{index}", value, f(5.7, y, 2.0, 0.52), size=31, emphasis=True, prefix=prefix),
                t(6, f"metric-label-{index}", label, f(7.72, y + 0.08, 2.64, 0.34), size=18, color=MUTED, prefix=prefix),
                rule(6, f"metric-rule-{index}", f(5.72, y + 0.72, 4.76, 0), color=SUBTLE, width=0.8, prefix=prefix),
            ]
        objects += [t(6, "callout", "单点提速不是终点；\n可复用，才形成经营杠杆。", f(10.72, 2.58, 1.72, 2.1), size=20, color=ACCENT, emphasis=True, align="right", prefix=prefix)]
    elif variant == "b":
        objects += [
            sh(6, "hero-field", f(0.0, 2.24, 5.3, 4.82), fill=BLACK, prefix=prefix),
            t(6, "hero-value", "18", f(0.76, 2.66, 2.55, 1.22), size=72, color=WHITE, emphasis=True, prefix=prefix),
            t(6, "hero-unit", "分钟", f(3.04, 3.18, 1.2, 0.48), size=24, color=ACCENT, emphasis=True, prefix=prefix),
            t(6, "hero-label", "平均交付时长", f(0.82, 4.12, 3.2, 0.38), size=21, color=WHITE, emphasis=True, prefix=prefix),
            t(6, "hero-detail", "基线 42 分钟\n固化后第 12 周", f(0.82, 4.78, 2.92, 0.74), size=17, color="BBB8B0", prefix=prefix),
            t(6, "hero-delta", "−57%", f(3.44, 5.7, 1.08, 0.38), size=20, color=ACCENT, emphasis=True, align="right", prefix=prefix),
        ]
        metrics = [
            ("94%", "事实保留率", "关键数字与来源不漂移"),
            ("2.3", "人工交接", "从 6.1 次降至 2.3 次"),
            ("4.2×", "方法复用", "同类任务周复用倍数"),
            ("30", "真实使用者", "连续 12 周受控观察"),
        ]
        coords = [(5.84, 2.46), (9.14, 2.46), (5.84, 4.52), (9.14, 4.52)]
        for index, ((value, label, detail), (x, y)) in enumerate(zip(metrics, coords, strict=True)):
            objects += [
                t(6, f"metric-value-{index}", value, f(x, y, 2.5, 0.62), size=37, color=ACCENT if index == 2 else INK, emphasis=True, prefix=prefix),
                t(6, f"metric-label-{index}", label, f(x, y + 0.72, 2.45, 0.34), size=19, emphasis=True, prefix=prefix),
                t(6, f"metric-detail-{index}", detail, f(x, y + 1.17, 2.62, 0.5), size=17, color=MUTED, prefix=prefix),
                rule(6, f"metric-rule-{index}", f(x, y + 1.78, 2.62, 0), color=SUBTLE, width=0.8, prefix=prefix),
            ]
    else:
        objects += [
            t(6, "hero", "57%", f(8.98, 2.12, 3.0, 1.04), size=62, color=ACCENT, emphasis=True, align="right", prefix=prefix),
            t(6, "hero-label", "更短的交付时长", f(8.92, 3.16, 3.1, 0.34), size=19, emphasis=True, align="right", prefix=prefix),
        ]
        values = [("42 → 18 min", "速度"), ("94%", "事实保留"), ("6.1 → 2.3", "交接"), ("4.2×", "复用")]
        widths = [3.8, 2.4, 2.8, 2.5]
        y = 5.62
        x = 0.78
        for index, ((value, label), width) in enumerate(zip(values, widths, strict=True)):
            objects += [
                rule(6, f"band-rule-{index}", f(x, y, width - 0.25, 0), color=ACCENT if index in {0, 3} else INK, width=3 if index == 0 else 1.2, prefix=prefix),
                t(6, f"band-value-{index}", value, f(x, y + 0.22, width - 0.25, 0.42), size=24, emphasis=True, prefix=prefix),
                t(6, f"band-label-{index}", label, f(x, y + 0.72, width - 0.25, 0.26), size=17, color=MUTED, prefix=prefix),
            ]
            x += width
        objects += [t(6, "thesis", "速度只是最先被看见的结果。\n真正的改变，是结果开始可靠、过程开始变短、方法开始复用。", f(0.78, 2.52, 6.5, 1.55), size=25, emphasis=True, prefix=prefix)]
    objects.append(t(6, "source", "受控 benchmark 场景数据，仅用于视觉与可编辑性验证。", f(8.22, 6.62, 4.15, 0.22), size=17, color=MUTED, role="meta", align="right", prefix=prefix))
    return DeckSlide(id=f"art-{prefix}-slide-6", layout_id="freeform", intent_id=INTENTS[6].id, objects=objects)


def _trend_chart(slide: int, prefix: str, *, style: str) -> list[Any]:
    values = [12, 15, 18, 22, 28, 36, 49, 63, 76, 84, 89, 93]
    x0, y0, width, height = 1.02, 2.76, 8.0, 2.92
    points = []
    for index, value in enumerate(values):
        x = x0 + index * (width / (len(values) - 1))
        y = y0 + height - (value / 100) * height
        points.append((x, y))
    objects: list[Any] = []
    if style == "bars":
        for index, ((x, y), value) in enumerate(zip(points, values, strict=True)):
            objects.append(sh(slide, f"bar-{index}", f(x - 0.14, y, 0.28, y0 + height - y), fill=ACCENT if index >= 6 else "AAA69D", prefix=prefix))
    else:
        for index in range(len(points) - 1):
            x1, y1 = points[index]
            x2, y2 = points[index + 1]
            segment_color = ACCENT if index >= 5 else INK
            segment_width = 3 if index >= 5 else 1.8
            # ObjectFrame deliberately rejects negative dimensions.  Express
            # the rising curve as native horizontal/vertical steps so every
            # segment remains editable without relying on backend-specific
            # negative-height line behavior.
            objects.append(rule(slide, f"trend-h-{index}", f(x1, y1, x2 - x1, 0), color=segment_color, width=segment_width, prefix=prefix))
            objects.append(rule(slide, f"trend-v-{index}", f(x2, y2, 0.01, y1 - y2), color=segment_color, width=segment_width, prefix=prefix))
        for index in {0, 5, 7, 11}:
            x, y = points[index]
            objects.append(sh(slide, f"dot-{index}", f(x - 0.08, y - 0.08, 0.16, 0.16), shape="ellipse", fill=ACCENT if index >= 6 else INK, prefix=prefix))
    objects += [
        rule(slide, "axis", f(x0, y0 + height, width, 0), color=INK, width=0.8, prefix=prefix),
        t(slide, "week-1", "W1", f(x0 - 0.14, y0 + height + 0.16, 0.5, 0.22), size=17, color=MUTED, role="meta", prefix=prefix),
        t(slide, "week-6", "W6", f(points[5][0] - 0.2, y0 + height + 0.16, 0.5, 0.22), size=17, color=MUTED, role="meta", prefix=prefix),
        t(slide, "week-12", "W12", f(points[-1][0] - 0.38, y0 + height + 0.16, 0.65, 0.22), size=17, color=MUTED, role="meta", prefix=prefix),
        t(slide, "axis-label", "任务按新方法完成的比例", f(x0, y0 - 0.42, 3.8, 0.24), size=17, color=MUTED, role="meta", prefix=prefix),
        rule(slide, "threshold", f(points[6][0], y0 - 0.14, 0.01, height + 0.14), color=SUBTLE, width=1, prefix=prefix),
        t(slide, "threshold-label", "方法进入工作区", f(points[6][0] + 0.16, y0 + 0.02, 1.9, 0.24), size=17, color=ACCENT, role="meta", emphasis=True, prefix=prefix),
    ]
    return objects


def slide_7(variant: str, prefix: str) -> DeckSlide:
    objects: list[Any] = [bg(7, CANVAS, prefix=prefix), *meta(7, "adoption curve", rail="top", prefix=prefix)]
    objects += [
        t(7, "title", "价值在方法固化后出现跃迁，\n而不是随着使用次数线性增长", f(0.76, 0.88, 8.65, 0.98), size=36, role="title", emphasis=True, prefix=prefix),
        t(7, "source", "受控 benchmark：30 名参与者 · 12 周", f(9.62, 1.02, 2.72, 0.3), size=17, color=MUTED, role="meta", align="right", prefix=prefix),
    ]
    if variant == "a":
        objects += _trend_chart(7, prefix, style="line")
        objects += [
            t(7, "end-value", "93%", f(9.54, 2.72, 2.2, 0.72), size=42, color=ACCENT, emphasis=True, align="right", prefix=prefix),
            t(7, "end-label", "按新方法完成", f(9.54, 3.48, 2.2, 0.32), size=18, emphasis=True, align="right", prefix=prefix),
            t(7, "explain", "W1–W5\n靠个人摸索\n\nW6–W8\n方法进入工作区\n\nW9–W12\n复用形成惯性", f(9.72, 4.2, 2.22, 1.7), size=17, color=MUTED, align="right", prefix=prefix),
        ]
    elif variant == "b":
        objects += _trend_chart(7, prefix, style="bars")
        objects += [
            sh(7, "annotation-field", f(9.58, 2.36, 2.62, 3.6), fill=BLACK, prefix=prefix),
            t(7, "annotation-main", "拐点不是\n换了模型", f(9.9, 2.78, 1.94, 0.94), size=28, color=WHITE, emphasis=True, prefix=prefix),
            rule(7, "annotation-rule", f(9.9, 4.02, 1.34, 0), color=ACCENT, width=4, prefix=prefix),
            t(7, "annotation-body", "而是共识、证据与动作\n第一次被写进了同一条任务链。", f(9.9, 4.4, 1.94, 1.0), size=17, color="C7C4BC", prefix=prefix),
        ]
    else:
        objects += [
            t(7, "phase-a", "01 个人摸索", f(0.82, 2.48, 2.7, 0.34), size=18, color=MUTED, emphasis=True, prefix=prefix),
            t(7, "phase-b", "02 方法固化", f(4.76, 2.48, 2.7, 0.34), size=18, color=ACCENT, emphasis=True, prefix=prefix),
            t(7, "phase-c", "03 组织惯性", f(8.92, 2.48, 2.7, 0.34), size=18, color=INK, emphasis=True, prefix=prefix),
            sh(7, "step-a", f(0.82, 4.86, 3.28, 0.62), fill="BDB9B0", prefix=prefix),
            sh(7, "step-b", f(4.12, 4.04, 4.12, 1.44), fill=ACCENT, prefix=prefix),
            sh(7, "step-c", f(8.26, 3.08, 4.05, 2.4), fill=BLACK, prefix=prefix),
            t(7, "value-a", "28%", f(1.08, 4.98, 1.52, 0.38), size=26, color=WHITE, emphasis=True, prefix=prefix),
            t(7, "value-b", "63%", f(4.5, 4.44, 1.72, 0.48), size=32, color=WHITE, emphasis=True, prefix=prefix),
            t(7, "value-c", "93%", f(8.72, 3.78, 2.2, 0.62), size=42, color=WHITE, emphasis=True, prefix=prefix),
            t(7, "detail-a", "会用工具", f(1.08, 5.74, 2.4, 0.3), size=17, color=MUTED, prefix=prefix),
            t(7, "detail-b", "按共识做事", f(4.5, 5.74, 2.6, 0.3), size=17, color=MUTED, prefix=prefix),
            t(7, "detail-c", "方法自然复用", f(8.72, 5.74, 2.6, 0.3), size=17, color=MUTED, prefix=prefix),
            t(7, "annotation", "增长来自方法进入系统，\n而不是提醒大家多用几次。", f(0.82, 3.28, 2.82, 0.9), size=20, emphasis=True, prefix=prefix),
        ]
    objects.append(t(7, "footnote", "比例为受控场景观测值，不代表生产经营结论。", f(8.36, 6.62, 3.92, 0.22), size=17, color=MUTED, role="meta", align="right", prefix=prefix))
    return DeckSlide(id=f"art-{prefix}-slide-7", layout_id="freeform", intent_id=INTENTS[7].id, objects=objects)


def slide_8(prefix: str = "final") -> DeckSlide:
    objects: list[Any] = [bg(8, CANVAS, prefix=prefix), *meta(8, "evidence case", rail="footer", prefix=prefix)]
    objects += [
        eyebrow(8, "CASE / SERVICE OPERATIONS", prefix=prefix),
        t(8, "title", "一个闭环案例，比十个功能列表\n更能证明价值", f(0.76, 1.08, 8.4, 0.9), size=36, role="title", emphasis=True, prefix=prefix),
        t(8, "case-name", "客服经营月报", f(0.8, 2.52, 3.2, 0.45), size=25, emphasis=True, prefix=prefix),
        t(8, "case-context", "30 人团队 · 每周汇总 · 多源材料", f(0.82, 3.06, 3.62, 0.3), size=17, color=MUTED, prefix=prefix),
        t(8, "before-value", "6h", f(0.78, 3.72, 1.58, 0.72), size=43, color=MUTED, emphasis=True, prefix=prefix),
        t(8, "before-label", "人工整理", f(2.42, 4.02, 1.62, 0.3), size=17, color=MUTED, prefix=prefix),
        sh(8, "case-arrow", f(4.08, 3.98, 0.54, 0.3), shape="chevron", fill=ACCENT, prefix=prefix),
        t(8, "after-value", "90m", f(4.9, 3.68, 2.12, 0.78), size=45, color=ACCENT, emphasis=True, prefix=prefix),
        t(8, "after-label", "人工复核", f(7.0, 4.02, 1.62, 0.3), size=17, color=INK, prefix=prefix),
        rule(8, "case-rule", f(0.82, 4.82, 7.18, 0), color=INK, width=1, prefix=prefix),
        t(8, "case-result", "更重要的是：数字、来源与判断\n第一次能够逐项追溯。", f(0.82, 5.18, 5.25, 0.9), size=23, emphasis=True, prefix=prefix),
    ]
    evidence = [
        ("输入", "工单摘要 / 满意度 / 异常原因", "材料在任务开始时即被锁定"),
        ("判断", "变化 → 原因 → 经营动作", "标题直接表达经营结论"),
        ("复核", "关键数字 / 证据 / 负责人", "人只处理分歧与授权"),
        ("沉淀", "版本 / 批注 / 修订原因", "下一周从上周方法继续"),
    ]
    for index, (label, value, note) in enumerate(evidence):
        y = 2.34 + index * 1.08
        objects += [
            t(8, f"evidence-num-{index}", f"0{index + 1}", f(8.76, y, 0.52, 0.28), size=17, color=ACCENT, emphasis=True, prefix=prefix),
            t(8, f"evidence-label-{index}", label, f(9.42, y, 0.7, 0.3), size=17, emphasis=True, prefix=prefix),
            t(8, f"evidence-value-{index}", value, f(10.18, y, 2.26, 0.3), size=17, emphasis=True, prefix=prefix),
            t(8, f"evidence-note-{index}", note, f(9.42, y + 0.42, 3.02, 0.34), size=17, color=MUTED, prefix=prefix),
            rule(8, f"evidence-rule-{index}", f(8.76, y + 0.84, 3.7, 0), color=SUBTLE, width=0.7, prefix=prefix),
        ]
    objects.append(t(8, "source", "受控 benchmark 案例；指标仅用于作品与链路验证。", f(8.86, 6.7, 3.58, 0.22), size=17, color=MUTED, role="meta", align="right", prefix=prefix))
    return DeckSlide(id=f"art-{prefix}-slide-8", layout_id="freeform", intent_id=INTENTS[8].id, objects=objects)


def slide_9(variant: str, prefix: str) -> DeckSlide:
    objects: list[Any] = [bg(9, CANVAS, prefix=prefix), *meta(9, "operating system", rail="top", prefix=prefix)]
    objects += [t(9, "title", "AI 原生工作台是五层任务系统，\n不是一个聊天框", f(0.76, 0.9, 8.6, 0.9), size=36, role="title", emphasis=True, prefix=prefix)]
    layers = [
        ("01", "意图层", "受众 / 目标 / 约束", "定义为什么做、为谁做、什么不能错"),
        ("02", "证据层", "来源 / 数字 / 权限", "让每个判断都有可追溯依据"),
        ("03", "方法层", "步骤 / 规则 / 技能", "把好的工作方式写进任务"),
        ("04", "执行层", "工具 / Agent / 人", "分工协作，但只有一个写入所有者"),
        ("05", "记忆层", "版本 / 反馈 / 结果", "让下一次从这一次继续"),
    ]
    if variant == "a":
        for index, (number, name, labels, detail) in enumerate(layers):
            y = 2.12 + index * 0.88
            objects += [
                t(9, f"layer-num-{index}", number, f(0.82, y, 0.52, 0.28), size=17, color=ACCENT if index == 2 else MUTED, emphasis=True, prefix=prefix),
                t(9, f"layer-name-{index}", name, f(1.55, y - 0.04, 1.35, 0.36), size=21, emphasis=True, prefix=prefix),
                t(9, f"layer-labels-{index}", labels, f(3.32, y, 2.64, 0.3), size=17, color=MUTED, prefix=prefix),
                t(9, f"layer-detail-{index}", detail, f(6.42, y, 5.38, 0.34), size=18, emphasis=index == 2, color=ACCENT if index == 2 else INK, prefix=prefix),
                rule(9, f"layer-rule-{index}", f(0.82, y + 0.62, 10.98, 0), color=SUBTLE, width=0.8, prefix=prefix),
            ]
    elif variant == "b":
        objects += [
            sh(9, "spine", f(0.0, 2.08, 3.86, 4.98), fill=BLACK, prefix=prefix),
            t(9, "spine-label", "TASK\nOPERATING\nSYSTEM", f(0.76, 2.62, 2.78, 1.82), size=28, color=WHITE, emphasis=True, prefix=prefix),
            t(9, "spine-note", "五层不是五个模块，\n而是同一任务的五种可见性。", f(0.78, 5.12, 2.42, 0.92), size=17, color="C5C2BB", prefix=prefix),
        ]
        for index, (number, name, labels, detail) in enumerate(layers):
            y = 2.14 + index * 0.86
            objects += [
                t(9, f"layer-num-{index}", number, f(4.42, y, 0.5, 0.26), size=17, color=ACCENT, emphasis=True, prefix=prefix),
                t(9, f"layer-name-{index}", name, f(5.1, y - 0.03, 1.32, 0.34), size=20, emphasis=True, prefix=prefix),
                t(9, f"layer-labels-{index}", labels, f(6.72, y, 2.4, 0.28), size=17, color=MUTED, prefix=prefix),
                t(9, f"layer-detail-{index}", detail, f(9.16, y, 3.22, 0.34), size=17, prefix=prefix),
                rule(9, f"layer-rule-{index}", f(4.42, y + 0.58, 7.92, 0), color=SUBTLE, width=0.7, prefix=prefix),
            ]
    else:
        widths = [2.0, 2.15, 2.35, 2.52, 2.72]
        xs = [0.82, 2.92, 5.18, 7.64, 10.26]
        colors = ["D9D5CC", "C9C4BA", "A9A398", ACCENT, BLACK]
        for index, ((number, name, labels, detail), x, width, color) in enumerate(zip(layers, xs, widths, colors, strict=True)):
            top = 2.48 - index * 0.12
            height = 3.48 + index * 0.12
            objects += [
                sh(9, f"layer-field-{index}", f(x, top, width, height), fill=color, prefix=prefix),
                t(9, f"layer-num-{index}", number, f(x + 0.22, top + 0.24, width - 0.42, 0.28), size=17, color=WHITE if index >= 3 else INK, emphasis=True, prefix=prefix),
                t(9, f"layer-name-{index}", name, f(x + 0.22, top + 0.72, width - 0.42, 0.38), size=21, color=WHITE if index >= 3 else INK, emphasis=True, prefix=prefix),
                t(9, f"layer-labels-{index}", labels, f(x + 0.22, top + 1.42, width - 0.42, 0.58), size=17, color=WHITE if index >= 3 else INK, prefix=prefix),
                t(9, f"layer-detail-{index}", detail, f(x + 0.22, top + 2.18, width - 0.42, 0.9), size=17, color="D5D2CB" if index >= 3 else "5E5A53", prefix=prefix),
            ]
    return DeckSlide(id=f"art-{prefix}-slide-9", layout_id="freeform", intent_id=INTENTS[9].id, objects=objects)


def slide_10(prefix: str = "final") -> DeckSlide:
    objects: list[Any] = [bg(10, CANVAS, prefix=prefix), *meta(10, "scale controls", rail="footer", prefix=prefix)]
    objects += [
        eyebrow(10, "CONTROL BEFORE SCALE", prefix=prefix),
        t(10, "title", "可规模化的前提，是把失败模式\n提前变成控制点", f(0.76, 1.04, 8.5, 0.9), size=36, role="title", emphasis=True, prefix=prefix),
        t(10, "col-a", "失败模式", f(0.82, 2.22, 2.48, 0.3), size=17, color=MUTED, role="meta", emphasis=True, prefix=prefix),
        t(10, "col-b", "控制动作", f(4.38, 2.22, 3.38, 0.3), size=17, color=MUTED, role="meta", emphasis=True, prefix=prefix),
        t(10, "col-c", "通过证据", f(9.02, 2.22, 2.98, 0.3), size=17, color=MUTED, role="meta", emphasis=True, prefix=prefix),
        rule(10, "header-rule", f(0.82, 2.62, 11.16, 0), color=INK, width=1.2, prefix=prefix),
    ]
    rows = [
        ("事实漂移", "immutable facts + source refs", "关键数字逐项一致"),
        ("写入打架", "single writer + base hash", "版本链无静默覆盖"),
        ("模板化", "候选构图 + actual render", "整套节奏不机械复用"),
        ("只跑通不交付", "visual critic + redo slide", "修订前后可见、可解释"),
    ]
    for index, (risk, control, proof) in enumerate(rows):
        y = 2.92 + index * 0.82
        objects += [
            t(10, f"risk-num-{index}", f"0{index + 1}", f(0.82, y, 0.48, 0.28), size=17, color=ACCENT, emphasis=True, prefix=prefix),
            t(10, f"risk-{index}", risk, f(1.5, y - 0.02, 2.48, 0.34), size=20, emphasis=True, prefix=prefix),
            t(10, f"control-{index}", control, f(4.38, y, 3.65, 0.32), size=18, emphasis=index in {1, 2}, prefix=prefix),
            t(10, f"proof-{index}", proof, f(9.02, y, 2.98, 0.32), size=17, color=MUTED, prefix=prefix),
            rule(10, f"row-rule-{index}", f(0.82, y + 0.56, 11.16, 0), color=SUBTLE, width=0.7, prefix=prefix),
        ]
    objects += [
        t(10, "bottom-number", "4", f(0.82, 6.24, 0.84, 0.54), size=38, color=ACCENT, emphasis=True, prefix=prefix),
        t(10, "bottom-claim", "个控制点，把“不确定”变成可管理的产品状态。", f(1.72, 6.34, 6.2, 0.36), size=20, emphasis=True, prefix=prefix),
        t(10, "bottom-note", "控制不是限制创造，而是保证创造可以安全抵达用户。", f(8.26, 6.34, 3.7, 0.36), size=17, color=MUTED, align="right", prefix=prefix),
    ]
    return DeckSlide(id=f"art-{prefix}-slide-10", layout_id="freeform", intent_id=INTENTS[10].id, objects=objects)


def slide_11(prefix: str = "final") -> DeckSlide:
    objects: list[Any] = [bg(11, BLACK, prefix=prefix), *meta(11, "90 day roadmap", dark=True, rail="top", prefix=prefix)]
    objects += [
        t(11, "title", "90 天只做三件事：\n找任务、固方法、建复利", f(0.76, 0.94, 7.8, 1.02), size=39, color=WHITE, role="title", emphasis=True, prefix=prefix),
        t(11, "thesis", "不追求一次铺满；先让一条价值链真正闭环。", f(9.08, 1.02, 3.32, 0.56), size=18, color="C4C1B9", align="right", prefix=prefix),
    ]
    phases = [
        ("01", "D1–30", "找任务", "选 3 个高频、高摩擦、\n结果可验证的任务", "任务基线 / 责任人 / 退出条件"),
        ("02", "D31–60", "固方法", "把意图、证据、审批与交付\n写进同一条工作流", "结构化方法 / 质量门 / 复盘记录"),
        ("03", "D61–90", "建复利", "让第二次任务直接复用\n第一次的有效方法", "复用率 / 周期 / 事实保留率"),
    ]
    xs = [0.8, 4.7, 8.72]
    heights = [3.0, 3.5, 4.0]
    for index, ((number, days, heading, body, proof), x, height) in enumerate(zip(phases, xs, heights, strict=True)):
        top = 6.08 - height
        field = ACCENT if index == 1 else ("232321" if index == 0 else WHITE)
        text_color = WHITE if index < 2 else INK
        muted = "D0CDC5" if index < 2 else MUTED
        objects += [
            sh(11, f"phase-field-{index}", f(x, top, 3.28, height), fill=field, prefix=prefix),
            t(11, f"phase-number-{index}", number, f(x + 0.28, top + 0.26, 0.55, 0.28), size=17, color=text_color, emphasis=True, prefix=prefix),
            t(11, f"phase-days-{index}", days, f(x + 1.76, top + 0.26, 1.18, 0.28), size=17, color=muted, align="right", prefix=prefix),
            t(11, f"phase-heading-{index}", heading, f(x + 0.28, top + 0.82, 2.4, 0.52), size=29, color=text_color, emphasis=True, prefix=prefix),
            t(11, f"phase-body-{index}", body, f(x + 0.28, top + 1.54, 2.64, 0.65), size=17, color=text_color, prefix=prefix),
            rule(11, f"phase-rule-{index}", f(x + 0.28, top + height - 0.7, 2.64, 0), color=muted, width=0.8, prefix=prefix),
            t(11, f"phase-proof-{index}", proof, f(x + 0.28, top + height - 0.5, 2.64, 0.32), size=17, color=muted, prefix=prefix),
        ]
    objects += [
        rule(11, "footer-rule", f(0.8, 6.5, 11.2, 0), color=ACCENT, width=2.2, prefix=prefix),
        t(11, "footer-thesis", "选择一个任务闭环，胜过采购十个孤立功能。", f(0.82, 6.72, 7.0, 0.32), size=18, color=WHITE, emphasis=True, prefix=prefix),
        t(11, "footer-folio", "11 / 12", f(11.22, 6.72, 0.82, 0.28), size=17, color="B7B4AC", align="right", prefix=prefix),
    ]
    return DeckSlide(id=f"art-{prefix}-slide-11", layout_id="freeform", intent_id=INTENTS[11].id, objects=objects)


def slide_12(prefix: str = "final") -> DeckSlide:
    objects: list[Any] = [bg(12, CANVAS, prefix=prefix)]
    objects += [
        rule(12, "top", f(0.76, 0.6, 11.82, 0), color=INK, width=0.9, prefix=prefix),
        t(12, "series", "ENTERPRISE AI / CLOSING THESIS", f(0.76, 0.76, 5.2, 0.22), size=17, color=MUTED, role="meta", emphasis=True, prefix=prefix),
        rt(12, "statement-a", [RichTextRun(text="先让任务", font_size=38), RichTextRun(text="被看见", font_size=38, bold=True, color=ACCENT)], f(1.18, 2.0, 6.15, 0.68), prefix=prefix),
        rt(12, "statement-b", [RichTextRun(text="再让方法", font_size=38), RichTextRun(text="被复用", font_size=38, bold=True, color=ACCENT)], f(3.44, 3.06, 6.15, 0.68), prefix=prefix),
        rt(12, "statement-c", [RichTextRun(text="最后让组织形成", font_size=38), RichTextRun(text="复利", font_size=38, bold=True, color=ACCENT)], f(5.72, 4.12, 6.15, 0.68), prefix=prefix),
        rule(12, "accent-a", f(1.18, 2.84, 1.48, 0), color=SUBTLE, width=1, prefix=prefix),
        rule(12, "accent-b", f(3.44, 3.9, 1.48, 0), color=SUBTLE, width=1, prefix=prefix),
        rule(12, "accent-c", f(5.72, 4.96, 1.48, 0), color=SUBTLE, width=1, prefix=prefix),
        t(12, "closing", "模型能力会趋同；任务系统决定价值能否留下。", f(5.72, 5.56, 6.08, 0.4), size=20, emphasis=True, align="right", prefix=prefix),
        rule(12, "footer-rule", f(0.76, 6.82, 11.82, 0), color=INK, width=0.9, prefix=prefix),
        t(12, "footer", "LANTAI PRESENTATION / EDITORIAL EXECUTIVE", f(0.76, 6.96, 7.0, 0.22), size=17, color=MUTED, role="meta", prefix=prefix),
        t(12, "folio", "12 / 12", f(11.5, 6.96, 1.08, 0.22), size=17, color=MUTED, role="meta", align="right", prefix=prefix),
    ]
    return DeckSlide(id=f"art-{prefix}-slide-12", layout_id="freeform", intent_id=INTENTS[12].id, objects=objects)


def visual_plan() -> DeckVisualPlan:
    families = [
        (1, DeckRhythmRole.OPENING, "opening-asymmetric", True, "low"),
        (2, DeckRhythmRole.ESCALATION, "thesis-contrast-field", True, "medium"),
        (3, DeckRhythmRole.DENSE, "progressive-stair", False, "high"),
        (4, DeckRhythmRole.CONTRAST, "before-after-editorial", True, "high"),
        (5, DeckRhythmRole.DENSE, "dense-editorial-field", False, "high"),
        (6, DeckRhythmRole.EVIDENCE, "asymmetric-metric-field", True, "high"),
        (7, DeckRhythmRole.EVIDENCE, "annotated-data-story", True, "high"),
        (8, DeckRhythmRole.EVIDENCE, "case-evidence-chain", False, "high"),
        (9, DeckRhythmRole.DENSE, "hierarchy-system", True, "high"),
        (10, DeckRhythmRole.DENSE, "risk-control-matrix", False, "high"),
        (11, DeckRhythmRole.SYNTHESIS, "roadmap-staggered-fields", True, "high"),
        (12, DeckRhythmRole.CONCLUSION, "closing-staggered-thesis", True, "low"),
    ]
    return DeckVisualPlan(
        beats=[DeckVisualBeat(slide_id=f"art-final-slide-{number}", role=role, composition_family=family, dominant=dominant, density=density) for number, role, family, dominant, density in families],
        narrative_peaks=["art-final-slide-2", "art-final-slide-7", "art-final-slide-11"],
        repeated_motifs=["micro-rails", "red judgment accent", "variable editorial fields"],
        variation_notes=["alternate quiet, dense, data and dark-field pages", "avoid identical title rails on consecutive pages"],
    )


def _candidate(
    slide_no: int,
    variant: str,
    builder: Callable[[str, str], DeckSlide],
    rationale: str,
) -> Callable[[ArtDirectionBrief], CompositionCandidate]:
    def build(_brief: ArtDirectionBrief) -> CompositionCandidate:
        prefix = f"candidate-{slide_no}-{variant}"
        slide = builder(variant, prefix)
        anchors = tuple(obj.id for obj in slide.objects if obj.kind in {"text", "shape"} and (getattr(obj, "role", "") == "title" or "hero" in obj.id or "anchor" in obj.id or "field" in obj.id))
        if not anchors:
            anchors = (slide.objects[0].id,)
        return CompositionCandidate(
            candidate_id=f"slide-{slide_no:02d}-{variant}",
            composition_family=f"{INTENTS[slide_no].relationship_type.value}-{variant}",
            slide=slide,
            rationale=rationale,
            visual_anchor_object_ids=anchors[:4],
            density="high" if slide_no not in {2} else "medium",
            content_compression="edited to one judgment plus supporting proof",
        )

    return build


CANDIDATE_BUILDERS: dict[int, tuple[Callable[[ArtDirectionBrief], CompositionCandidate], ...]] = {
    2: (
        _candidate(2, "a", slide_2, "large thesis pair over three proof columns"),
        _candidate(2, "b", slide_2, "dark central judgment field with asymmetric evidence rail"),
        _candidate(2, "c", slide_2, "quiet thesis field divided from numbered diagnostic questions"),
    ),
    4: (
        _candidate(4, "a", slide_4, "balanced row comparison"),
        _candidate(4, "b", slide_4, "linear stack contrasted with an operating loop"),
        _candidate(4, "c", slide_4, "micro-typographic transformation table with ownership callout"),
    ),
    6: (
        _candidate(6, "a", slide_6, "one dominant metric plus a vertical support rail"),
        _candidate(6, "b", slide_6, "dark hero metric and an asymmetric two-by-two evidence field"),
        _candidate(6, "c", slide_6, "judgment-led metric band with variable spans"),
    ),
    7: (
        _candidate(7, "a", slide_7, "annotated line curve plus endpoint analysis"),
        _candidate(7, "b", slide_7, "bar trajectory with a dark explanatory annotation"),
        _candidate(7, "c", slide_7, "three-phase stepped data story"),
    ),
    9: (
        _candidate(9, "a", slide_9, "five horizontal system layers"),
        _candidate(9, "b", slide_9, "dark operating-system spine with aligned layer evidence"),
        _candidate(9, "c", slide_9, "progressively weighted layer fields"),
    ),
}


SELECTION_RATIONALE = {
    2: "The dark central judgment field creates the clearest thesis hierarchy while the side rail preserves diagnostic evidence.",
    4: "The editorial transformation table carries the densest before/after evidence without reducing the relationship to two decorative cards.",
    6: "The dark hero metric establishes a single visual anchor; the asymmetric two-by-two field makes the supporting measures readable as evidence.",
    7: "The stepped three-phase composition explains when the value inflects, instead of presenting an unexplained generic line chart.",
    9: "The dark system spine gives the hierarchy an operating-system identity while aligned evidence keeps all five layers comparable.",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_context(path: Path) -> PresentationDesignContext:
    if not path.is_file():
        raise FileNotFoundError(path)
    return PresentationDesignContext.model_validate_json(path.read_text(encoding="utf-8"))


def _build_deck(context: PresentationDesignContext, slides: list[DeckSlide]) -> DeckIR:
    return DeckIR(
        id="lantai-editorial-art-director-benchmark-v1",
        title="企业 AI 的第二阶段：从工具采购到任务经营",
        theme_id="editorial-executive",
        seed=913,
        slides=slides,
        source_refs=[],
        immutable_data=["30 名参与者", "12 周", "42 分钟", "18 分钟", "94%", "6.1 次", "2.3 次", "4.2×"],
        design_profile=context.profile,
        design_context=context,
        composition_tokens=TOKENS,
        visual_plan=visual_plan(),
    )


def _montage(images: list[Path] | tuple[Path, ...], output: Path, *, columns: int = 3, label: str = "") -> None:
    thumb_w, thumb_h, gap, header = 640, 360, 22, 72
    rows = math.ceil(len(images) / columns)
    canvas = Image.new("RGB", (columns * thumb_w + (columns + 1) * gap, rows * thumb_h + (rows + 1) * gap + header), "#D0CDC6")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    if label:
        draw.text((gap, 22), label, fill="#11110F", font=font)
    for index, path in enumerate(images):
        with Image.open(path) as source:
            thumb = ImageOps.fit(source.convert("RGB"), (thumb_w, thumb_h), method=Image.Resampling.LANCZOS)
        x = gap + (index % columns) * (thumb_w + gap)
        y = header + gap + (index // columns) * (thumb_h + gap)
        canvas.paste(thumb, (x, y))
        draw.rectangle((x, y, x + thumb_w - 1, y + thumb_h - 1), outline="#AAA69D", width=1)
        draw.text((x + 10, y + 9), f"{index + 1:02d}", fill="#C94732", font=font)
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output, "PNG")


def _side_by_side(reference: Path, lantai: Path, output: Path) -> None:
    with Image.open(reference) as ref, Image.open(lantai) as lan:
        width = 1400
        ref_h = round(ref.height * (width / ref.width))
        lan_h = round(lan.height * (width / lan.width))
        ref_img = ref.convert("RGB").resize((width, ref_h), Image.Resampling.LANCZOS)
        lan_img = lan.convert("RGB").resize((width, lan_h), Image.Resampling.LANCZOS)
    gap = 46
    canvas = Image.new("RGB", (width, ref_h + lan_h + gap + 100), "#11110F")
    draw = ImageDraw.Draw(canvas)
    draw.text((18, 18), "REFERENCE BENCHMARK / EDITORIAL EXECUTIVE", fill="#FFFFFF")
    canvas.paste(ref_img, (0, 52))
    draw.text((18, ref_h + 66), "LANTAI ART DIRECTOR BENCHMARK / NEW CONTENT", fill="#FFFFFF")
    canvas.paste(lan_img, (0, ref_h + gap + 100))
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output, "PNG")


def run(output_dir: Path, context_path: Path, reference_render: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    context = _load_context(context_path)
    compiler = NativePptxCompiler()
    director = SlideArtDirector()
    selection_records: list[dict[str, Any]] = []
    candidate_root = output_dir / "candidates"
    candidate_montages: list[str] = []
    selected: dict[int, DeckSlide] = {}
    for slide_no, builders in CANDIDATE_BUILDERS.items():
        candidates = director.propose(
            intent=INTENTS[slide_no],
            design_context=context,
            candidate_builders=builders,
            visual_plan=visual_plan(),
            reference_exemplars=[str(reference_render)],
        )
        images: list[Path] = []
        evidence: list[dict[str, Any]] = []
        for candidate in candidates:
            variant_dir = candidate_root / f"slide-{slide_no:02d}" / candidate.candidate_id
            candidate_deck = _build_deck(context, [candidate.slide])
            pptx = variant_dir / "candidate.pptx"
            compilation = compiler.compile(candidate_deck, pptx)
            rendered = render_pptx(pptx, variant_dir / "render")
            images.append(rendered.slide_images[0])
            render_issues = inspect_rendered_slides(rendered.slide_images)
            evidence.append({
                "candidate_id": candidate.candidate_id,
                "composition_family": candidate.composition_family,
                "rationale": candidate.rationale,
                "geometry_fingerprint": director.geometry_fingerprint(candidate),
                "editable_objects": compilation.editable_object_count,
                "render": str(rendered.slide_images[0]),
                "render_issues": [issue.model_dump(mode="json") for issue in render_issues],
            })
        candidate_montage = candidate_root / f"slide-{slide_no:02d}" / "comparison.png"
        _montage(images, candidate_montage, columns=3, label=f"SLIDE {slide_no:02d} / COMPOSITION CANDIDATES")
        candidate_montages.append(str(candidate_montage))
        selected_variant = SELECTION[slide_no]
        chosen = next(candidate for candidate in candidates if candidate.candidate_id.endswith(f"-{selected_variant}"))
        selected[slide_no] = {2: slide_2, 4: slide_4, 6: slide_6, 7: slide_7, 9: slide_9}[slide_no](selected_variant, "final")
        selection_records.append({
            "slide": slide_no,
            "selected": chosen.candidate_id,
            "selection_basis": SELECTION_RATIONALE[slide_no],
            "review_mode": "rendered_candidate_visual_comparison",
            "candidates": evidence,
        })

    slides = [
        slide_1(), selected[2], slide_3(), selected[4], slide_5(), selected[6],
        selected[7], slide_8(), selected[9], slide_10(), slide_11(), slide_12(),
    ]
    deck = _build_deck(context, slides)
    deck_path = output_dir / "deck-ir.json"
    deck_path.write_text(deck.model_dump_json(indent=2), encoding="utf-8")
    selection_path = output_dir / "candidate-selection.json"
    selection_path.write_text(json.dumps(selection_records, ensure_ascii=False, indent=2), encoding="utf-8")
    pptx_path = output_dir / "澜台-企业AI第二阶段-Editorial-Executive.pptx"
    compilation = compiler.compile(deck, pptx_path)
    rendered = render_pptx(pptx_path, output_dir / "render")
    structural = inspect_pptx(deck, pptx_path, rendered_slide_count=len(rendered.slide_images))
    render_issues = inspect_rendered_slides(rendered.slide_images)
    issues = [issue.model_dump(mode="json") for issue in [*structural.issues, *render_issues]]
    lantai_montage = output_dir / "lantai-montage.png"
    _montage(rendered.slide_images, lantai_montage, columns=3, label="LANTAI / EDITORIAL EXECUTIVE / 12 SLIDES")
    reference_images = sorted(reference_render.glob("slide-*.png"), key=lambda path: int(path.stem.split("-")[-1]))
    reference_montage = output_dir / "reference-montage.png"
    _montage(reference_images, reference_montage, columns=4, label="REFERENCE / EDITORIAL EXECUTIVE / 16 SLIDES")
    comparison = output_dir / "reference-vs-lantai.png"
    _side_by_side(reference_montage, lantai_montage, comparison)
    result = {
        "schema": "com.lantai.editorial-art-director-benchmark.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "pptx": str(pptx_path),
        "deck_ir": str(deck_path),
        "candidate_selection": str(selection_path),
        "candidate_montages": candidate_montages,
        "lantai_montage": str(lantai_montage),
        "reference_montage": str(reference_montage),
        "reference_vs_lantai": str(comparison),
        "slide_count": len(deck.slides),
        "editable_object_count": compilation.editable_object_count,
        "degradations": compilation.degradations,
        "quality_issues": issues,
        "deterministic_checks_passed": not compilation.degradations and not any(issue["severity"] == "error" for issue in issues),
        "visual_acceptance": "pending_manual_visual_review",
        "pptx_sha256": _sha256(pptx_path),
        "montage_sha256": _sha256(lantai_montage),
        "reference_montage_sha256": _sha256(reference_montage),
        "renderer": rendered.renderer_id,
        "renderer_version": rendered.renderer_version,
    }
    (output_dir / "benchmark-results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--context", type=Path, default=DEFAULT_CONTEXT)
    parser.add_argument("--reference-render", type=Path, default=DEFAULT_REFERENCE_RENDER)
    args = parser.parse_args()
    result = run(args.output.resolve(), args.context.resolve(), args.reference_render.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["deterministic_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
