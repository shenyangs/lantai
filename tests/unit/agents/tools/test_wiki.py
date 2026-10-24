# -*- coding: utf-8 -*-
"""Regression tests for local Wiki tool argument handling."""

import json

import pytest


@pytest.mark.asyncio
async def test_wiki_search_accepts_numeric_string_limit(monkeypatch, tmp_path):
    """Provider tool calls may encode integer arguments as JSON strings."""
    from lantai.agents.tools import wiki as wiki_tools
    from lantai.app.wiki import core as wiki_core

    captured: dict[str, object] = {}

    def fake_build_wiki_query(workspace_dir, request):
        captured["workspace_dir"] = workspace_dir
        captured["limit"] = request.limit
        return wiki_core.WikiQueryResponse(query=request.query)

    monkeypatch.setattr(wiki_tools, "_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(wiki_core, "build_wiki_query", fake_build_wiki_query)

    response = await wiki_tools.wiki_search("作品集", limit="5")  # type: ignore[arg-type]

    payload = json.loads(response.content[0]["text"])
    assert captured == {"workspace_dir": tmp_path, "limit": 5}
    assert payload["query"] == "作品集"


@pytest.mark.asyncio
async def test_exact_scope_reader_rejects_unselected_source(monkeypatch, tmp_path):
    from lantai.agents.tools import wiki as wiki_tools
    from lantai.app.wiki import core as wiki_core

    alpha, _, _ = wiki_core.import_single_wiki_source(
        tmp_path, "alpha.txt", b"alpha evidence"
    )
    beta, _, _ = wiki_core.import_single_wiki_source(
        tmp_path, "beta.txt", b"beta evidence"
    )
    wiki_core.write_wiki_page(
        tmp_path,
        "synthesis/mixed.md",
        (
            "---\n"
            "sources:\n"
            f"  - {alpha.path}\n"
            f"  - {beta.path}\n"
            "---\n\n# Mixed\n\nUNSELECTED_BETA_SECRET\n"
        ),
    )
    monkeypatch.setattr(wiki_tools, "_workspace_dir", lambda: tmp_path)

    allowed = await wiki_tools.wiki_read_for_sources(beta.path, [alpha.path])
    selected = await wiki_tools.wiki_read_for_sources(alpha.path, [alpha.path])
    mixed = await wiki_tools.wiki_read_for_sources(
        "synthesis/mixed.md", [alpha.path]
    )
    search = await wiki_tools.wiki_search_for_sources(
        "alpha evidence", [alpha.path], 8
    )
    search_payload = json.loads(search.content[0]["text"])
    page_hit = next(
        hit for hit in search_payload["citations"] if hit["kind"] == "page"
    )
    page_read = await wiki_tools.wiki_read_for_sources(
        page_hit["path"], [alpha.path]
    )

    assert "不在当前对话选择的资料范围" in allowed.content[0]["text"]
    assert "alpha evidence" in selected.content[0]["text"]
    assert "不在当前对话选择的资料范围" in mixed.content[0]["text"]
    assert "UNSELECTED_BETA_SECRET" not in mixed.content[0]["text"]
    assert page_hit["path"].startswith(".wiki/pages/")
    assert "alpha evidence" in page_read.content[0]["text"]
