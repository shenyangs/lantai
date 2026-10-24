# -*- coding: utf-8 -*-
"""Golden behaviour locks for dependency-free local retrieval."""

from lantai.retrieval import IndexedDocument, SQLiteRetrievalIndex
from lantai.retrieval.local_index import resolve_hit_source


def test_fts_or_lexical_index_has_precise_citations_and_incremental_hashes(tmp_path):
    index = SQLiteRetrievalIndex(tmp_path / "retrieval.sqlite")
    document = IndexedDocument(
        source_id="wiki:pricing.md",
        source_kind="wiki",
        title="定价说明",
        content="企业版在 2026 年提供年度订阅与用量审计。",
        citation="wiki/pricing.md#年度订阅",
        revision="v1",
        trust=0.4,
    )

    assert index.upsert([document]) == 1
    assert index.upsert([document]) == 0
    hits = index.search("年度订阅")

    assert hits
    assert hits[0].citation == "wiki/pricing.md#年度订阅"
    assert hits[0].trust == 0.4


def test_rebuild_and_delete_only_touch_index_not_source_data(tmp_path):
    source = tmp_path / "source.md"
    source.write_text("memory retrieval uses citations", encoding="utf-8")
    index = SQLiteRetrievalIndex(tmp_path / "retrieval.sqlite")
    document = IndexedDocument(
        source_id="memory:atom-1",
        source_kind="atom",
        title="workflow",
        content=source.read_text(encoding="utf-8"),
        citation="layered-memory/atoms/atom-1",
    )

    assert index.rebuild([document]) == 1
    assert index.search("citations")
    index.delete_source(document.source_id)

    assert not index.search("citations")
    assert source.read_text(encoding="utf-8") == "memory retrieval uses citations"


def test_sync_prunes_deleted_sources_only_inside_the_owned_prefix(tmp_path):
    index = SQLiteRetrievalIndex(tmp_path / "retrieval.sqlite")
    wiki = IndexedDocument(
        source_id="page:obsolete", source_kind="wiki", title="obsolete",
        content="legacy retrieval content", citation="obsolete.md",
    )
    memory = IndexedDocument(
        source_id="memory:confirmed", source_kind="memory", title="memory",
        content="legacy retrieval content", citation="memory:confirmed",
    )
    index.upsert([wiki, memory])

    index.sync([], source_prefixes=("page:", "source:"))

    hits = index.search("legacy retrieval", limit=10)
    assert not [hit for hit in hits if hit.source_id == wiki.source_id]
    assert [hit for hit in hits if hit.source_id == memory.source_id]


def test_lexical_chunks_resolve_exact_unicode_original_and_reject_new_revision(tmp_path):
    index = SQLiteRetrievalIndex(tmp_path / "retrieval.sqlite", chunk_chars=256)
    original = " 开头😀\n\n" + "背景。" * 120 + "\n\n但是，只有周五可以。\n尾部"
    document = IndexedDocument(
        source_id="source:policy", source_kind="wiki", title="条件",
        content=original, citation="policy.md", revision="v1",
    )
    index.upsert([document])

    hit = index.search("周五", limit=10)[0]
    opened = resolve_hit_source(hit, document)
    assert opened["status"] == "ready"
    assert opened["excerpt"] == hit.content
    assert original[hit.source_start:hit.source_end] == hit.content
    assert hit.source_revision == "v1"
    assert resolve_hit_source(hit, IndexedDocument(**{
        **document.__dict__, "content": original + "新内容", "revision": "v2",
    }))["status"] == "version_unavailable"


def test_existing_lexical_index_migrates_and_reindexes_offsets(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE retrieval_sources (source_id TEXT PRIMARY KEY, source_kind TEXT NOT NULL, content_hash TEXT NOT NULL, revision TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("CREATE TABLE retrieval_chunks (chunk_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_kind TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL, citation TEXT NOT NULL, trust REAL NOT NULL)")
    document = IndexedDocument(source_id="source:old", source_kind="wiki", title="旧版",
                               content="原文有精确位置", citation="old.md", revision="v1")
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO retrieval_sources(source_id,source_kind,content_hash,revision) VALUES (?,?,?,?)",
                     (document.source_id, document.source_kind, document.content_hash, document.revision))
        conn.execute("INSERT INTO retrieval_chunks VALUES (?,?,?,?,?,?,?)",
                     ("source:old:0", document.source_id, document.source_kind, document.title,
                      document.content, document.citation, document.trust))
    index = SQLiteRetrievalIndex(path)
    assert index.upsert([document]) == 1
    hit = index.search("精确")[-1]
    assert resolve_hit_source(hit, document)["status"] == "ready"
