"""Behavior locks for source typography, truthful evidence and safe repair."""
from __future__ import annotations

import json

import pytest

from lantai.agents.tools.presentation import (
    _bind_grounded_storyboard_facts,
    _build_authored_storyboard_plan,
    create_presentation,
)
from lantai.config.context import (
    WorkspaceContext,
    set_current_workspace_context,
)
from lantai.presentation.models import (
    DeckIR,
    DeckSlide,
    InputMaterial,
    PresentationJobRequest,
)
from lantai.presentation.planner import build_brief


def _bind(source: str, quotes: list[str], *, other_source: str | None = None):
    materials = [
        InputMaterial(id="source-0", name="source.pdf", content=source)
    ]
    if other_source:
        materials.append(
            InputMaterial(
                id="source-1", name="other.pdf", content=other_source
            )
        )
    deck = DeckIR(
        title="报告",
        source_refs=[m.id for m in materials],
        slides=[DeckSlide(id="slide-1", layout_id="cover")],
    )
    storyboard = [
        {
            "purpose": "交代证据",
            "key_message": "模型撰写的摘要不必是引语",
            "evidence": quotes,
            "relationship_type": "thesis",
            "visual_role": "opening",
            "source_indexes": [0],
        }
    ]
    if other_source:
        deck = deck.model_copy(
            update={
                "slides": deck.slides
                + [DeckSlide(id="slide-2", layout_id="closing")]
            }
        )
        storyboard.append(
            {**storyboard[0], "evidence": [], "source_indexes": [1]}
        )
    deck, plan = _build_authored_storyboard_plan(
        deck, materials, storyboard, "判断与证据分开。"
    )
    brief = build_brief(
        PresentationJobRequest(
            conversation_id="test",
            user_request="制作报告",
            materials=materials,
        )
    )
    return _bind_grounded_storyboard_facts(deck, plan, brief)


@pytest.mark.parametrize(
    ("source", "quote"),
    [
        ("## Page 1\n汽车行业及竞争格\n局深度研究", "汽车行业及竞争格局深度研究"),
        ("其中 NIO 主品牌 160,670 辆、乐道 61,428 辆。", "其中NIO主品牌160,670辆、乐道61,428辆。"),
        ("研究观察窗\r\n口为2023—2026年。", "研究观察窗口为2023—2026年。"),
        ("增长与盈利\n\n互为条件。", "互为条件。"),
    ],
)
def test_cjk_pdf_spacing_is_typography_not_a_fact_change(source, quote):
    bound = _bind(source, [quote])
    assert len(bound.facts) == 1
    assert bound.facts[0].text == quote
    assert bound.facts[0].source_refs == ["source-0"]
    assert bound.slides[0].source_fact_ids == [bound.facts[0].fact_id]


@pytest.mark.parametrize(
    ("source", "quote"),
    [
        ("金额1 00万元。", "金额100万元。"),
        ("The new line is here.", "The newline is here."),
        ("交付量160,670辆。", "交付量160670辆。"),
        ("样本只有11人。", "样本足够大。"),
        ("增幅为10.5%。", "增幅为105%。"),
        ("增长与盈利\n\n## 下一页\n互为条件", "增长与盈利互为条件"),
        ("增长与盈利\n\n互为条件", "增长与盈利互为条件"),
        ("增长与盈利\r\n \t\r\n互为条件", "增长与盈利互为条件"),
        ("Growth and profit\n\nare linked.", "Growth and profit are linked."),
        ("增长与盈利\n\n互为条件", "增长与盈利\n\n互为条件"),
        ("增长与盈利互为条件", "增长与盈利\n \n互为条件"),
    ],
)
def test_normalization_does_not_rewrite_facts_or_cross_paragraph_boundaries(
    source, quote
):
    with pytest.raises(ValueError, match="not present in its declared source"):
        _bind(source, [quote])


def test_evidence_cannot_borrow_from_an_undeclared_page_source():
    with pytest.raises(ValueError, match="not present in its declared source"):
        _bind("样本为11人。", ["样本为1000人。"], other_source="样本为1000人。")


@pytest.mark.asyncio
async def test_grounding_rejection_reports_all_errors_and_has_no_artifact_effect(
    tmp_path, monkeypatch
):
    import lantai.agents.tools.presentation as tool

    context = WorkspaceContext(
        agent_home=tmp_path / "home",
        state_root=tmp_path / "state",
        project_root=tmp_path / "workspace",
        workspace_grant_id="ppt-repair",
        trust_state="trusted",
    )
    context.project_root.mkdir()
    source = context.project_root / "source.txt"
    source.write_text("实际样本只有11人。\n收入为12万元。", encoding="utf-8")
    invoked = []
    monkeypatch.setattr(
        tool,
        "compose_deck_ir_with_design_context",
        lambda *args, **kwargs: invoked.append("compose"),
    )
    set_current_workspace_context(context)
    try:
        response = await create_presentation(
            title="不伪造证据",
            source_paths=[str(source)],
            slides=[
                {
                    "layout_id": "cover",
                    "objects": [
                        {
                            "kind": "text",
                            "role": "title",
                            "text": "报告",
                            "slot": "title",
                        }
                    ],
                }
            ],
            narrative_arc="判断与原文分开。",
            storyboard=[
                {
                    "purpose": "交代证据",
                    "key_message": "这是摘要",
                    "evidence": ["样本为1000人。", "收入为100万元。"],
                    "relationship_type": "thesis",
                    "visual_role": "opening",
                    "source_indexes": [0],
                }
            ],
        )
    finally:
        set_current_workspace_context(None)
    assert response.is_error
    assert response.metadata["error_code"] == "invalid_presentation_arguments"
    assert response.metadata["side_effect_outcome"] == "known_not_applied"
    diagnostics = response.metadata["grounding_repair"]
    assert diagnostics["error_count"] == 2
    assert [item["evidence"] for item in diagnostics["errors"]] == [
        "样本为1000人。",
        "收入为100万元。",
    ]
    assert all(item["page"] == 1 for item in diagnostics["errors"])
    assert "实际样本只有11人。" in diagnostics["source_catalog"][0]["text"]
    # The model consumes content, not metadata alone; both rejected quotes and
    # exact extracted source must be visible in a single repair result.
    assert "收入为100万元。" in response.content[0]["text"]
    assert "实际样本只有11人。" in response.content[0]["text"]
    assert invoked == []
    assert not list(
        context.state_root.glob("presentation-jobs/*/checkpoint.json")
    )
    assert not list(context.project_root.glob("*.pptx"))


def test_grounding_repair_catalog_is_bounded_and_marks_truncation():
    with pytest.raises(ValueError) as caught:
        _bind("原文。" * 20000, [f"虚构事实{i}" for i in range(100)])
    diagnostics = caught.value.repair_diagnostics
    assert diagnostics["error_count"] == 100
    assert diagnostics["errors_truncated"] is True
    assert diagnostics["source_catalog_truncated"] is True
    assert len(json.dumps(diagnostics, ensure_ascii=False)) < 50000


@pytest.mark.asyncio
@pytest.mark.parametrize("semantic_layout", ["cover", "closing"])
async def test_tool_cannot_supply_runtime_owned_semantic_page_type(
    tmp_path, monkeypatch, semantic_layout
):
    import lantai.agents.tools.presentation as tool

    context = WorkspaceContext(
        agent_home=tmp_path / "home",
        state_root=tmp_path / "state",
        project_root=tmp_path / "workspace",
        workspace_grant_id="ppt-repair",
        trust_state="trusted",
    )
    context.project_root.mkdir()
    invoked = []

    def unexpected_compose(*args, **kwargs):
        invoked.append("compose")
        raise ValueError("runtime page metadata reached composition")

    monkeypatch.setattr(tool, "compose_deck_ir_with_design_context", unexpected_compose)
    set_current_workspace_context(context)
    try:
        response = await create_presentation(
            title="正文不能冒充封面",
            slides=[
                {
                    "layout_id": "freeform",
                    "semantic_layout": semantic_layout,
                    "objects": [
                        {
                            "kind": "text",
                            "role": "title",
                            "text": "正文仍须证据",
                            "frame": {"x": 1, "y": 1, "w": 10, "h": 1},
                        }
                    ],
                }
            ],
        )
    finally:
        set_current_workspace_context(None)
    assert response.is_error
    assert response.metadata["error_code"] == "invalid_presentation_arguments"
    assert "semantic_layout" in response.content[0]["text"]
    assert response.metadata["side_effect_outcome"] == "known_not_applied"
    assert invoked == []
    assert not list(context.state_root.glob("presentation-jobs/*/checkpoint.json"))
    assert not list(context.project_root.glob("*.pptx"))
