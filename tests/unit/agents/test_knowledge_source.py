"""Tests for persistent knowledge-source chat instructions."""

from lantai.agents.knowledge_source import build_knowledge_source_prompt


def test_knowledge_source_prompt_preserves_source_and_spreadsheet_citations():
    prompt = build_knowledge_source_prompt(
        {
            "knowledge_source": {
                "filename": "7-9月媒体专项预算.xlsx",
                "path": ".wiki/raw/sources/7-9月媒体专项预算.xlsx",
            },
            "knowledge_source_context": "[媒体合作明细!A32:E32] 名称=36氪; 折后价=30",
        }
    )

    assert "7-9月媒体专项预算.xlsx" in prompt
    assert "工作表名和行号/单元格范围" in prompt
    assert "[媒体合作明细!A32:E32]" in prompt
    assert "若资料中找不到，直接说明" in prompt
    assert "[资料名](Citation URL)" in prompt


def test_knowledge_source_prompt_requires_complete_reference():
    assert build_knowledge_source_prompt({}) == ""
    assert (
        build_knowledge_source_prompt(
            {"knowledge_source": {"filename": "budget.xlsx"}}
        )
        == ""
    )


def test_multiple_knowledge_sources_keep_exact_scope_explicit():
    prompt = build_knowledge_source_prompt(
        {
            "knowledge_sources": [
                {"filename": "alpha.pdf", "path": ".wiki/raw/alpha.pdf"},
                {"filename": "beta.xlsx", "path": ".wiki/raw/beta.xlsx"},
            ],
            "knowledge_scope_mode": "exact",
        }
    )

    assert "alpha.pdf" in prompt
    assert "beta.xlsx" in prompt
    assert "仅依据这些资料" in prompt
    assert "不得用未选择的知识库内容" in prompt
