# -*- coding: utf-8 -*-
"""Knowledge workspace recall must include local semantic candidates."""


def test_wiki_search_recalls_semantic_match_without_shared_words(tmp_path):
    from lantai.app.routers import workspace as workspace_router

    workspace_router._import_wiki_source_upload(
        tmp_path,
        "media-budget.md",
        "# 媒体专项\n\n都市报整版报价为 120000 元。".encode(),
    )

    result = workspace_router.wiki_core.search_wiki(
        tmp_path,
        "这些渠道分别花了多少钱？",
        limit=10,
    )

    assert any("media-budget" in hit.filename for hit in result.hits)
    assert any(hit.match_type == "semantic" for hit in result.hits)

    exact = workspace_router.wiki_core.search_wiki(
        tmp_path,
        "都市报媒体报价",
        limit=10,
    )
    matched = next(hit for hit in exact.hits if "media-budget" in hit.filename)
    assert matched.match_type == "hybrid"


def test_wiki_search_source_excerpt_is_bound_to_indexed_version(tmp_path):
    from lantai.app.routers import workspace as workspace_router

    imported = workspace_router._import_wiki_source_upload(
        tmp_path,
        "receipt.txt",
        "😀订单原文：蓝色灯具 320 元。".encode(),
    )
    path = imported["imported"][0]["path"]
    result = workspace_router.wiki_core.search_wiki(tmp_path, "蓝色灯具")
    hit = next(hit for hit in result.hits if hit.kind == "source" and hit.path == path)

    assert hit.source_text_hash
    assert hit.source_revision
    assert hit.source_start is not None
    assert hit.source_end is not None
    opened = workspace_router.wiki_core.read_wiki_source_content(
        tmp_path, path,
        expected_revision=hit.source_revision,
        expected_text_hash=hit.source_text_hash,
        source_start=hit.source_start,
        source_end=hit.source_end,
    )
    assert opened.content_status == "text_extracted"
    assert opened.source_excerpt == opened.content[hit.source_start:hit.source_end]
    assert "蓝色灯具" in opened.source_excerpt

    (tmp_path / path).write_text("😀订单已更正：红色灯具 280 元。", encoding="utf-8")
    stale = workspace_router.wiki_core.read_wiki_source_content(
        tmp_path, path,
        expected_revision=hit.source_revision,
        expected_text_hash=hit.source_text_hash,
        source_start=hit.source_start,
        source_end=hit.source_end,
    )
    assert stale.content_status == "version_unavailable"
    assert stale.content == ""
    assert stale.source_excerpt == ""
