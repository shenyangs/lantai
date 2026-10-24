"""Locks for the desktop's missing-source / erased-mapping retry loop."""
from __future__ import annotations

import json

import pytest

from lantai.agents.tools.presentation import (
    _build_authored_storyboard_plan,
    create_presentation,
)
from lantai.config.context import WorkspaceContext, set_current_workspace_context
from lantai.presentation.models import DeckIR, DeckSlide, InputMaterial


def _page(indexes):
    return {
        "purpose": "交代来源",
        "key_message": "来源与判断分开",
        "visual_role": "opening",
        "relationship_type": "thesis",
        "evidence": [],
        "source_indexes": indexes,
    }


def _plan(indexes, source_count=1):
    materials = [InputMaterial(id=f"source-{i}", name=f"report-{i}.pdf", content="真实材料。")
                 for i in range(source_count)]
    deck = DeckIR(title="报告", slides=[
        DeckSlide(id=f"slide-{i}", layout_id="cover")
        for i in range(len(indexes))
    ])
    return _build_authored_storyboard_plan(
        deck, materials, [_page(item) for item in indexes], "先判断，再证据。"
    )


@pytest.mark.parametrize("indexes", [[True], [-1], [1], [0, 0], "0", None])
def test_invalid_mapping_is_not_silently_shifted_or_assigned(indexes):
    with pytest.raises(ValueError) as caught:
        _plan([indexes])
    repair = caught.value.repair_diagnostics
    assert repair["source_count"] == 1
    assert repair["source_catalog"] == [{"index": 0, "id": "source-0", "name": "report-0.pdf"}]
    assert repair["errors"][0]["page"] == 1
    assert "PDF" in " ".join(repair["instructions"])


def test_mapping_reports_all_bad_pages_and_unmapped_files_together():
    with pytest.raises(ValueError) as caught:
        _plan([[2], [], [True], [0, 0]], source_count=2)
    repair = caught.value.repair_diagnostics
    assert [item["page"] for item in repair["errors"]] == [1, 3, 4]
    assert repair["missing_source_indexes"] == [1]
    assert repair["source_catalog"][1]["index"] == 1


def test_original_content_and_unreferenced_cover_still_work():
    _, original = _plan([[]], source_count=0)
    assert original.slides[0].intent.source_refs == []
    _, sourced = _plan([[], [0]])
    assert sourced.slides[0].intent.source_refs == []
    assert sourced.slides[1].intent.source_refs == ["source-0"]


def test_source_index_repair_feedback_is_bounded():
    with pytest.raises(ValueError) as caught:
        _plan([[9999]] * 60, source_count=200)
    repair = caught.value.repair_diagnostics
    assert repair["error_count"] == 60
    assert repair["errors_truncated"] is True
    assert repair["source_catalog_truncated"] is True
    assert repair["missing_source_count"] == 200
    assert repair["missing_source_indexes_truncated"] is True
    assert len(json.dumps(repair, ensure_ascii=False)) < 20000


@pytest.mark.asyncio
@pytest.mark.parametrize("has_sources,indexes", [(False, [0]), (True, [])])
async def test_desktop_retry_loop_gets_actionable_feedback_before_any_write(
    tmp_path, monkeypatch, has_sources, indexes
):
    import lantai.agents.tools.presentation as tool

    context = WorkspaceContext(
        agent_home=tmp_path / "home", state_root=tmp_path / "state",
        project_root=tmp_path / "workspace", workspace_grant_id="index-repair",
        trust_state="trusted",
    )
    context.project_root.mkdir()
    source = context.project_root / "report.txt"
    source.write_text("真实材料。", encoding="utf-8")
    invoked = []
    monkeypatch.setattr(tool, "build_brief", lambda *_: invoked.append("extract"))
    monkeypatch.setattr(tool, "compose_deck_ir_with_design_context",
                        lambda *_, **__: invoked.append("compose"))
    set_current_workspace_context(context)
    try:
        response = await create_presentation(
            title="报告", slides=[{"layout_id": "cover", "objects": [
                {"kind": "text", "role": "title", "text": "报告", "slot": "title"}
            ]}],
            source_paths=[str(source)] if has_sources else None,
            narrative_arc="先判断，再证据。", storyboard=[_page(indexes)],
        )
    finally:
        set_current_workspace_context(None)
    assert response.is_error
    assert response.metadata["error_code"] == "invalid_presentation_arguments"
    assert response.metadata["side_effect_outcome"] == "known_not_applied"
    repair = response.metadata["source_index_repair"]
    assert repair["source_count"] == int(has_sources)
    assert repair["missing_source_indexes"] == ([0] if has_sources else [])
    assert "source_paths" in response.content[0]["text"]
    assert json.dumps(repair, ensure_ascii=False) in response.content[0]["text"]
    assert "create_presentation" in response.content[0]["text"]
    assert invoked == []
    assert not list(context.state_root.glob("presentation-jobs/*/checkpoint.json"))
    assert not list(context.project_root.glob("*.pptx"))
