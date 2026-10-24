from __future__ import annotations

import pytest

from lantai.presentation.design_intelligence import (
    ColorStrategyEngine,
    CompositionPlanner,
    compose_deck_ir_with_design_context,
    load_style_profile,
    resolve_design_context,
    resolve_native_composition_tokens,
    resolve_template_mode,
    visual_direction_from_context,
)
from lantai.presentation.models import (
    ConstraintLevel,
    DeckIR,
    DeckPlan,
    DeckPlanSlide,
    DeckRhythmRole,
    DeckSlide,
    DeckVisualBeat,
    DesignConstraint,
    ImageObject,
    ObjectFrame,
    PresentationBrief,
    PresentationJobRequest,
    QualityBand,
    RelationshipType,
    ShapeObject,
    SlideIntent,
    TableObject,
    TextObject,
    TemplateMode,
    TemplateQualityProfile,
    VisualDirection,
)


def _request() -> PresentationJobRequest:
    return PresentationJobRequest(
        conversation_id="design-intelligence-test",
        user_request="为管理层制作一份关于经营取舍的专业汇报",
        audience="管理层",
        slide_count=1,
        seed=17,
    )


def _brief() -> PresentationBrief:
    return PresentationBrief(
        objective="让管理层理解经营取舍",
        audience="管理层",
        narrative="先判断，再展示证据与行动",
    )


def _planned_slide(relationship: RelationshipType) -> DeckPlanSlide:
    return DeckPlanSlide(
        id="slide-1",
        order=1,
        title="增长成立，但效率问题已经先出现",
        intent=SlideIntent(
            id="intent-1",
            purpose="形成经营判断",
            key_message="增长成立，但效率问题已经先出现",
            evidence=["获客成本继续上升", "留存改善有限", "交付周期拉长"],
            relationship_type=relationship,
            visual_role="explanation",
        ),
        content_blocks=["主判断", "证据一", "证据二", "证据三"],
    )


def _objects() -> list[TextObject]:
    return [
        TextObject(id=f"body-{index}", role="body", text=text, slot="body")
        for index, text in enumerate(
            ("主判断", "证据一", "证据二", "证据三"),
            start=1,
        )
    ]


def _beat() -> DeckVisualBeat:
    return DeckVisualBeat(
        slide_id="slide-1",
        role=DeckRhythmRole.EVIDENCE,
        composition_family="test",
        density="medium",
    )


def test_explicit_editorial_visual_direction_outranks_content_keywords() -> None:
    request = PresentationJobRequest(
        conversation_id="design-intelligence-editorial-incident",
        user_request=(
            "制作 WPS 品牌传播 PPT，包含产品发布、用户增长和数据。"
            "风格要简洁品牌传播风、大面积留白、红色 #D93025 主色、"
            "现代无衬线、克制可信、有新闻感，不用照片。"
        ),
        audience="公开传播",
        slide_count=7,
        seed=17,
    )
    brief = PresentationBrief(
        objective="介绍 WPS 品牌成果",
        audience="公开传播",
        narrative="品牌榜、付费用户、研发投入与产品发布",
    )

    context = resolve_design_context(request, brief, None)

    assert context.profile.identity == "autonomous:editorial-executive:17"
    assert context.profile.color_profile is not None
    assert context.profile.color_profile.background == "F3F1E9"
    assert context.profile.color_profile.accent_primary == "D93025"


def test_explicit_launch_visual_direction_remains_product_launch() -> None:
    request = PresentationJobRequest(
        conversation_id="design-intelligence-launch-counterexample",
        user_request=(
            "为新品发布制作演示文稿，明确采用新品发布会风格，"
            "高对比、动感、舞台感。"
        ),
        audience="媒体与客户",
        slide_count=7,
        seed=17,
    )
    brief = PresentationBrief(
        objective="发布新品并展示产品价值",
        audience="媒体与客户",
        narrative="新品亮相、功能演示、上市节奏",
    )

    context = resolve_design_context(request, brief, None)

    assert context.profile.identity == "autonomous:product-launch:17"


def test_authored_visual_direction_guides_autonomous_profile_and_native_color() -> None:
    direction = VisualDirection(
        visual_concept="新品发布会风格，克制的舞台感",
        palette=["#10131A", "#F8FAFC", "#C73542"],
        typography="Noto Sans SC / Noto Sans SC",
        density="low",
        image_language="真实产品特写",
        chart_language="原生图表，突出结论",
        shape_language="简洁几何",
        grid="12 columns",
        recurring_motif="页码和细线",
    )
    deck = DeckIR(
        title="新品",
        slides=[
            DeckSlide(
                id="slide-1",
                layout_id="freeform",
                composition_authority="author",
                objects=[
                    TextObject(
                        id="title",
                        role="title",
                        text="新品价值",
                        frame=ObjectFrame(x=1, y=1, w=8, h=1),
                    )
                ],
            )
        ],
        visual_direction=direction,
    )
    composed = compose_deck_ir_with_design_context(
        deck,
        _request(),
        _brief(),
        author_direction=direction,
    )

    assert composed.design_profile.identity == "autonomous:product-launch:17"
    assert composed.design_profile.color_profile.palette_source == ["author_direction"]
    assert composed.composition_tokens.canvas == "10131A"
    assert composed.composition_tokens.ink == "F8FAFC"
    assert composed.composition_tokens.accent == "C73542"
    assert composed.visual_direction == direction
    assert composed.slides[0].objects == deck.slides[0].objects


def test_context_projection_keeps_native_palette_role_order() -> None:
    context = resolve_design_context(_request(), _brief(), [_planned_slide(RelationshipType.THESIS)])
    direction = visual_direction_from_context(context)
    tokens = resolve_native_composition_tokens(context)

    assert direction.palette[:3] == [
        f"#{tokens.canvas}",
        f"#{tokens.ink}",
        f"#{tokens.accent}",
    ]


def test_dense_timeline_decoration_stays_within_canvas() -> None:
    slide = _planned_slide(RelationshipType.CHRONOLOGY)
    context = resolve_design_context(_request(), _brief(), [slide])
    planner = CompositionPlanner()
    composition = planner.plan_slide(slide, context, _beat())
    objects = [
        TextObject(id=f"stage-{index}", role="body", text=f"事件 {index}", slot="body")
        for index in range(8)
    ]
    placed = planner.compose_objects(slide, objects, context, composition)
    assert {obj.id for obj in objects}.issubset({obj.id for obj in placed})
    assert all(obj.frame.y + obj.frame.h <= 7.5 for obj in placed)
    assert not any("stage-separator" in obj.id for obj in placed)
    assert not any("stage-number" in obj.id for obj in placed)


def test_overfull_evidence_is_rejected_without_invalid_geometry() -> None:
    slide = _planned_slide(RelationshipType.COMPOSITION)
    context = resolve_design_context(_request(), _brief(), [slide])
    planner = CompositionPlanner()
    composition = planner.plan_slide(slide, context, _beat())
    objects = [
        TextObject(id=f"evidence-{index}", role="body", text=f"证据 {index}")
        for index in range(22)
    ]
    with pytest.raises(ValueError, match="composition capacity exceeded"):
        planner.compose_objects(slide, objects, context, composition)


def test_production_storyboard_preserves_native_chart_data(tmp_path) -> None:
    from lantai.presentation.models import ChartObject
    from lantai.presentation.planner import build_deck_ir
    from lantai.presentation.design_intelligence import visual_direction_from_context
    from lantai.presentation.pptx_compiler import NativePptxCompiler
    from xml.etree import ElementTree
    import zipfile

    slide = _planned_slide(RelationshipType.DATA_TREND).model_copy(update={
        "content_blocks": ["两次峰值需要分别响应"],
        "charts": [ChartObject(
            id="incident-peaks", categories=["6/27", "7/6"],
            series={"监测量": [4557, 4608]}, unit="条",
            source_refs=["monitoring-data"],
            derivation="直接摘录两次监测峰值，不插值或估算其他日期",
        )],
    })
    context = resolve_design_context(_request(), _brief(), [slide])
    plan = DeckPlan(title="波形", narrative_arc="证据与响应",
                    visual_direction=visual_direction_from_context(context), slides=[slide])
    deck = build_deck_ir(_request(), plan)
    charts = [obj for obj in deck.slides[0].objects if isinstance(obj, ChartObject)]
    output = tmp_path / "native-chart.pptx"
    NativePptxCompiler().compile(deck, output)
    with zipfile.ZipFile(output) as package:
        chart_xml = ElementTree.fromstring(package.read("ppt/charts/chart1.xml"))
        values = [node.text for node in chart_xml.iter()
                  if node.tag.endswith("}v")]
        assert {"4557", "4608", "6/27", "7/6"}.issubset(values)
    assert len(charts) == 1
    assert charts[0].series == {"监测量": [4557, 4608]}
    assert charts[0].categories == ["6/27", "7/6"]
    assert charts[0].source_refs == ["monitoring-data"]
    assert charts[0].frame is not None


def test_composition_relationship_is_not_reduced_to_a_two_by_two_grid() -> None:
    request = _request()
    brief = _brief()
    slide = _planned_slide(RelationshipType.COMPOSITION)
    context = resolve_design_context(request, brief, [slide])
    planner = CompositionPlanner()
    composition = planner.plan_slide(slide, context, _beat())

    placed = planner.compose_objects(slide, _objects(), context, composition)
    frames = [obj.frame for obj in placed if obj.id.startswith("body-")]

    assert all(frame is not None for frame in frames)
    assert frames[0].w > frames[1].w
    assert frames[1].x == frames[2].x == frames[3].x
    assert len({frame.y for frame in frames[1:]}) == 3


def test_matrix_relationship_is_the_only_four_item_two_by_two_path() -> None:
    request = _request()
    brief = _brief()
    slide = _planned_slide(RelationshipType.MATRIX)
    context = resolve_design_context(request, brief, [slide])
    planner = CompositionPlanner()
    composition = planner.plan_slide(slide, context, _beat())

    placed = planner.compose_objects(slide, _objects(), context, composition)
    frames = [obj.frame for obj in placed if obj.id.startswith("body-")]

    assert len({frame.x for frame in frames}) == 2
    assert len({frame.y for frame in frames}) == 2
    assert len({round(frame.w, 3) for frame in frames}) == 1
    assert len({round(frame.h, 3) for frame in frames}) == 1


def test_comparison_fields_receive_native_typographic_hierarchy() -> None:
    request = _request()
    brief = _brief()
    slide = _planned_slide(RelationshipType.COMPARISON)
    context = resolve_design_context(request, brief, [slide])
    planner = CompositionPlanner()
    composition = planner.plan_slide(slide, context, _beat())
    objects = [
        TextObject(
            id="before",
            role="body",
            text="过去\n功能清单式介绍\n信息多但记不住",
        ),
        TextObject(
            id="after",
            role="body",
            text="现在\n一个鲜明主张\n证据紧跟结论",
        ),
    ]

    placed = planner.compose_objects(slide, objects, context, composition)
    fields = [obj for obj in placed if obj.id in {"before", "after"}]

    assert all(obj.font_size is not None and obj.font_size >= 24 for obj in fields)
    assert all(obj.emphasis for obj in fields)
    assert all(len(obj.runs) == 2 for obj in fields)
    assert all(obj.runs[0].bold for obj in fields)
    assert all(obj.runs[0].font_size is not None and obj.runs[0].font_size >= 26 for obj in fields)
    assert all(obj.runs[0].text.endswith("\n") for obj in fields)


def test_data_annotation_is_not_rendered_as_timid_caption_copy() -> None:
    request = _request()
    brief = _brief()
    slide = _planned_slide(RelationshipType.DATA_TREND)
    context = resolve_design_context(request, brief, [slide])
    planner = CompositionPlanner()
    composition = planner.plan_slide(slide, context, _beat())
    conclusion = TextObject(
        id="conclusion",
        role="body",
        text="收入增长成立，但利润仍受交付成本挤压",
    )

    placed = planner.compose_objects(slide, [conclusion], context, composition)
    rendered = next(obj for obj in placed if obj.id == "conclusion")

    assert rendered.font_size is not None and rendered.font_size >= 22
    assert rendered.emphasis is True


def test_authored_native_composition_keeps_explicit_geometry() -> None:
    request = _request()
    brief = _brief()
    slide = _planned_slide(RelationshipType.PROCESS)
    context = resolve_design_context(request, brief, [slide])
    planner = CompositionPlanner()
    composition = planner.plan_slide(slide, context, _beat())
    authored = [
        TextObject(
            id="authored-title",
            role="title",
            text=slide.title,
            frame=ObjectFrame(x=0.8, y=0.55, w=7.2, h=0.72),
        ),
        ShapeObject(
            id="authored-node",
            shape_type="rect",
            frame=ObjectFrame(x=1.1, y=2.2, w=2.2, h=1.1),
        ),
        TextObject(
            id="authored-copy",
            role="body",
            text="第一步：保留已经形成的自由构图",
            frame=ObjectFrame(x=1.35, y=2.45, w=1.7, h=0.55),
        ),
        ShapeObject(
            id="authored-connector",
            shape_type="line",
            frame=ObjectFrame(x=3.3, y=2.72, w=1.4, h=0.05),
        ),
    ]

    placed = planner.compose_objects(slide, authored, context, composition)
    by_id = {obj.id: obj for obj in placed}

    for obj in authored:
        assert by_id[obj.id].frame == obj.frame


def test_explicit_author_composition_preserves_text_and_image_without_shape() -> None:
    title = TextObject(
        id="author-title",
        role="title",
        text="作者已经完成页级构图",
        frame=ObjectFrame(x=0.82, y=0.56, w=6.8, h=0.76),
    )
    body = TextObject(
        id="author-body",
        role="body",
        text="下游只能测量和核验，不得重排。",
        frame=ObjectFrame(x=0.86, y=2.18, w=4.1, h=1.22),
    )
    image = ImageObject(
        id="author-image",
        path="/tmp/author-composition.png",
        alt="作者选定的主视觉",
        frame=ObjectFrame(x=7.2, y=1.26, w=5.1, h=5.32),
    )
    deck = DeckIR(
        id="author-composition-deck",
        title="显式作者构图",
        slides=[
            DeckSlide(
                id="author-composition-slide",
                layout_id="freeform",
                composition_authority="author",
                objects=[title, body, image],
            ),
        ],
    )

    composed = compose_deck_ir_with_design_context(deck, _request(), _brief())
    slide = composed.slides[0]

    assert [obj.id for obj in slide.objects] == [
        "author-title",
        "author-body",
        "author-image",
    ]
    assert [obj.frame for obj in slide.objects] == [
        title.frame,
        body.frame,
        image.frame,
    ]


def test_existing_deck_is_promoted_to_explicit_geometry_with_one_context() -> None:
    request = _request()
    brief = _brief()
    planned = _planned_slide(RelationshipType.CAUSE_EFFECT)
    plan = DeckPlan(
        title="经营判断",
        narrative_arc=brief.narrative,
        visual_direction=VisualDirection(
            visual_concept="temporary planning projection",
            palette=["#FFFFFF", "#111111"],
            typography="temporary",
            image_language="temporary",
            chart_language="temporary",
            shape_language="temporary",
            grid="temporary",
            recurring_motif="temporary",
        ),
        slides=[planned],
    )
    deck = DeckIR(
        id="deck-1",
        title="经营判断",
        slides=[
            DeckSlide(
                id="slide-1",
                layout_id="three_cards",
                intent_id="intent-1",
                objects=[
                    TextObject(
                        id="title-1",
                        role="title",
                        text=planned.title,
                        slot="title",
                    ),
                    *_objects(),
                ],
            ),
        ],
    )

    composed = compose_deck_ir_with_design_context(
        deck,
        request,
        brief,
        plan=plan,
    )

    assert composed.design_context is not None
    assert composed.design_context.profile == composed.design_profile
    assert composed.composition_tokens is not None
    assert composed.slides[0].layout_id == "freeform"
    assert all(obj.frame is not None for obj in composed.slides[0].objects)
    assert any(
        relationship.relationship_type == RelationshipType.CAUSE_EFFECT
        for relationship in composed.slides[0].relationships
    )


def test_text_heavy_comparison_table_remains_one_native_editable_object() -> None:
    headers = ["维度", "截图能确认的", "澜台已确认的", "融合裁决"]
    rows = [
        ["问题组织", "输入被展开为讨论题", "合同有目标和验收", "放第一层"],
        ["分工可见", "三个视角直接呈现", "成员有角色与依赖", "具体问题在前"],
        ["交叉讨论", "截图不足以确认", "有第二轮审阅", "保留真执行"],
        ["结论交付", "截屏未显示", "子结果与交付区分", "最终答案直接可读"],
        ["过程可控", "单张截图无法验证", "有停止暂停恢复", "不为换外观破坏保护"],
    ]
    deck = DeckIR(
        id="comparison-table-deck",
        title="截图与澜台",
        slides=[
            DeckSlide(
                id="comparison-table-slide",
                layout_id="table",
                objects=[
                    TextObject(
                        id="comparison-table-title",
                        role="title",
                        text="截图与澜台：到底差在哪里",
                        slot="title",
                    ),
                    TableObject(
                        id="comparison-table",
                        headers=headers,
                        rows=rows,
                    ),
                ],
            ),
        ],
    )

    composed = compose_deck_ir_with_design_context(deck, _request(), _brief())
    slide = composed.slides[0]
    native_tables = [obj for obj in slide.objects if isinstance(obj, TableObject)]
    repeated_story_cards = [
        obj
        for obj in slide.objects
        if isinstance(obj, TextObject)
        and (
            obj.id == "comparison-table"
            or obj.id.startswith("comparison-table-row-")
        )
    ]

    assert len(native_tables) == 1
    assert native_tables[0].headers == headers
    assert native_tables[0].rows == rows
    assert native_tables[0].frame is not None
    assert repeated_story_cards == []
    assert any(
        relationship.relationship_type == RelationshipType.COMPARISON
        for relationship in slide.relationships
    )


def test_template_mode_is_inferred_only_when_not_explicitly_selected() -> None:
    strict = _request().model_copy(
        update={"user_request": "严格按集团模板，一个地方都不要改"},
    )
    explicit = strict.model_copy(
        update={"template_mode": TemplateMode.STYLE_REFERENCE},
    )

    assert resolve_template_mode(strict) == TemplateMode.STRICT
    assert resolve_template_mode(explicit) == TemplateMode.STYLE_REFERENCE


def test_no_reference_synthesizes_role_based_color_profile() -> None:
    context = resolve_design_context(_request(), _brief(), [_planned_slide(RelationshipType.COMPARISON)])

    assert context.reference_input_present is False
    assert context.profile.color_profile is not None
    assert context.profile.color_profile.palette_source == ["semantic_synthesis"]
    assert len(context.profile.color_profile.chart_series) >= 4
    assert context.profile.color_profile.accent_primary != context.profile.color_profile.background


def test_editorial_style_supports_controlled_color_variation() -> None:
    style = load_style_profile("editorial-executive")
    engine = ColorStrategyEngine()

    accents = {
        engine.resolve(
            style=style,
            user_request="为管理层制作一份经营研究汇报",
            audience="管理层",
            seed=seed,
        ).accent_primary
        for seed in range(12)
    }

    assert len(accents) >= 2


def test_hard_brand_color_overrides_explicit_user_color() -> None:
    profile = ColorStrategyEngine().resolve(
        style=load_style_profile("editorial-executive"),
        user_request="使用红色做管理层汇报",
        constraints=[
            DesignConstraint(
                category="brand_color",
                level=ConstraintLevel.HARD,
                value="#6B3FA0",
                rationale="集团品牌紫不可修改",
            ),
        ],
        seed=3,
    )

    assert profile.brand_anchor == "6B3FA0"
    assert profile.accent_primary == "6B3FA0"
    assert profile.palette_source[0] == "brand_constraint"
    assert profile.chart_series[0] == "6B3FA0"


def test_bad_reference_palette_is_rejected_by_quality_arbitration() -> None:
    profile = ColorStrategyEngine().resolve(
        style=load_style_profile("editorial-executive").model_copy(
            update={
                "source_format": "pptx",
                "palette": ["#FF00FF", "#00FFFF", "#FFFF00", "#00FF00"],
            },
        ),
        user_request="用公司模板做得专业一些",
        quality=TemplateQualityProfile(color_discipline=QualityBand.UNACCEPTABLE),
        constraints=[
            DesignConstraint(
                category="palette",
                level=ConstraintLevel.REJECT,
                value="uncontrolled_palette",
                rationale="七种高饱和颜色且无语义角色",
            ),
        ],
        reference_palette=["FF00FF", "00FFFF", "FFFF00", "00FF00"],
        seed=5,
    )

    assert "reference_deck" not in profile.palette_source
    assert profile.accent_primary not in {"FF00FF", "00FFFF", "FFFF00", "00FF00"}
    assert "uncontrolled_palette" in profile.forbidden_usage


def test_native_chart_tokens_consume_resolved_color_profile() -> None:
    context = resolve_design_context(_request(), _brief(), [_planned_slide(RelationshipType.DATA_TREND)])
    tokens = resolve_native_composition_tokens(context)

    assert context.profile.color_profile is not None
    assert tokens.chart_series == context.profile.color_profile.chart_series
    assert tokens.accent == context.profile.color_profile.accent_primary


def test_comparison_keeps_three_audiences_separate_from_takeaway() -> None:
    from lantai.presentation.planner import build_deck_ir
    from lantai.presentation.design_intelligence import visual_direction_from_context

    blocks = ['媒体\n提供可引用证据', '意见领袖\n解释传播争议', '用户\n给出操作路径']
    slide = _planned_slide(RelationshipType.COMPARISON).model_copy(update={
        'content_blocks': blocks,
        'takeaway': '三类对象分别提供不同证据',
    })
    context = resolve_design_context(_request(), _brief(), [slide])
    deck = build_deck_ir(_request(), DeckPlan(
        title='响应对象', narrative_arc='分对象提供证据', slides=[slide],
        visual_direction=visual_direction_from_context(context),
    ))
    objects = deck.slides[0].objects
    units = [obj for obj in objects if isinstance(obj, TextObject) and obj.role == 'comparison']
    takeaway = next(obj for obj in objects if obj.slot == 'takeaway')
    assert [obj.text for obj in units] == blocks
    assert len({obj.frame.x for obj in units}) == 3
    assert len({obj.frame.y for obj in units}) == 1
    assert all(obj.frame.y + obj.frame.h < takeaway.frame.y for obj in units)
    assert takeaway.text == slide.takeaway
