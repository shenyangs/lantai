# -*- coding: utf-8 -*-
"""Behaviour locks for the persistent local vector index."""

from __future__ import annotations

import numpy as np

from lantai.retrieval import IndexedDocument, SQLiteVectorIndex
from lantai.retrieval.local_index import resolve_hit_source


class _SemanticEmbedding:
    """Tiny deterministic test double at the public embedding seam."""

    model_name = "test/semantic"
    dimensions = 3
    fingerprint = "test-semantic-v1"

    def embed(self, texts: list[str]) -> np.ndarray:
        rows: list[list[float]] = []
        for text in texts:
            if any(term in text for term in ("预算", "报价", "花费", "多少钱")):
                rows.append([1.0, 0.0, 0.0])
            elif any(term in text for term in ("歌曲", "旋律", "歌词")):
                rows.append([0.0, 1.0, 0.0])
            else:
                rows.append([0.0, 0.0, 1.0])
        return np.asarray(rows, dtype=np.float32)


def test_vector_index_persists_and_recalls_semantically_related_content(tmp_path):
    path = tmp_path / "vectors.sqlite"
    documents = [
        IndexedDocument(
            source_id="wiki:media-budget",
            source_kind="wiki",
            title="7-9 月媒体专项",
            content="都市报整版报价为 120000 元。",
            citation="7-9月媒体专项预算.xlsx#总表!A8:F8",
        ),
        IndexedDocument(
            source_id="wiki:song",
            source_kind="wiki",
            title="写歌",
            content="副歌需要更有记忆点的旋律。",
            citation="song.md#副歌",
        ),
    ]

    assert SQLiteVectorIndex(path, embedding=_SemanticEmbedding()).upsert(documents) == 2

    reopened = SQLiteVectorIndex(path, embedding=_SemanticEmbedding())
    hits = reopened.search("不同渠道分别花费多少", limit=2)

    assert hits
    assert hits[0].source_id == "wiki:media-budget"
    assert hits[0].citation == "7-9月媒体专项预算.xlsx#总表!A8:F8"


def test_vector_index_reembeds_unchanged_sources_after_model_migration(tmp_path):
    path = tmp_path / "vectors.sqlite"
    document = IndexedDocument(
        source_id="memory:preference",
        source_kind="memory",
        title="工作方式",
        content="预算问题需要逐项列出媒体报价。",
        citation="memory-ledger/facts.json#preference",
    )
    first = _SemanticEmbedding()
    assert SQLiteVectorIndex(path, embedding=first).upsert([document]) == 1

    migrated = _SemanticEmbedding()
    migrated.fingerprint = "test-semantic-v2"
    migrated_index = SQLiteVectorIndex(path, embedding=migrated)

    assert migrated_index.upsert([document]) == 1
    assert migrated_index.record_count == 1
    assert migrated_index.search("各项分别多少钱")[0].source_id == document.source_id


def test_vector_sync_prunes_deleted_sources_without_touching_other_families(tmp_path):
    index = SQLiteVectorIndex(tmp_path / "vectors.sqlite", embedding=_SemanticEmbedding())
    wiki = IndexedDocument(
        source_id="page:obsolete",
        source_kind="wiki",
        title="旧预算",
        content="媒体报价为 100 元。",
        citation="obsolete.md",
    )
    memory = IndexedDocument(
        source_id="memory:confirmed",
        source_kind="memory",
        title="预算习惯",
        content="预算需要列出每个渠道多少钱。",
        citation="memory#confirmed",
    )
    index.upsert([wiki, memory])

    index.sync([], source_prefixes=("page:", "source:"))

    assert index.record_count == 1
    assert index.search("花费", limit=5)[0].source_id == memory.source_id


def test_failed_model_migration_keeps_the_previous_projection_intact(tmp_path):
    path = tmp_path / "vectors.sqlite"
    documents = [
        IndexedDocument(
            source_id="page:budget",
            source_kind="wiki",
            title="预算",
            content="媒体报价为 100 元。",
            citation="budget.md",
        ),
        IndexedDocument(
            source_id="page:song",
            source_kind="wiki",
            title="歌曲",
            content="副歌旋律需要记忆点。",
            citation="song.md",
        ),
    ]
    original = _SemanticEmbedding()
    SQLiteVectorIndex(path, embedding=original).upsert(documents)

    broken = _SemanticEmbedding()
    broken.fingerprint = "test-semantic-broken-v2"

    def fail_on_second_batch(texts: list[str]) -> np.ndarray:
        if any("副歌" in text for text in texts):
            raise RuntimeError("migration embedding failed")
        return _SemanticEmbedding().embed(texts)

    broken.embed = fail_on_second_batch

    with __import__("pytest").raises(RuntimeError, match="migration embedding failed"):
        SQLiteVectorIndex(path, embedding=broken).upsert(documents)

    reopened = SQLiteVectorIndex(path, embedding=original)
    assert reopened.record_count == 2
    assert reopened.search("预算", limit=2)[0].source_id == "page:budget"


def test_vector_search_does_not_return_unrelated_zero_similarity_rows(tmp_path):
    index = SQLiteVectorIndex(
        tmp_path / "vectors.sqlite",
        embedding=_SemanticEmbedding(),
    )
    index.upsert(
        [
            IndexedDocument(
                source_id="page:budget",
                source_kind="wiki",
                title="预算",
                content="媒体报价为 100 元。",
                citation="budget.md",
            ),
            IndexedDocument(
                source_id="page:song",
                source_kind="wiki",
                title="歌曲",
                content="副歌旋律需要记忆点。",
                citation="song.md",
            ),
        ],
    )

    assert [hit.source_id for hit in index.search("花费", limit=10)] == [
        "page:budget",
    ]


def test_projection_stats_expose_sources_waiting_for_model_migration(tmp_path):
    path = tmp_path / "vectors.sqlite"
    document = IndexedDocument(
        source_id="page:budget",
        source_kind="wiki",
        title="预算",
        content="媒体报价为 100 元。",
        citation="budget.md",
    )
    SQLiteVectorIndex(path, embedding=_SemanticEmbedding()).upsert([document])
    migrated = _SemanticEmbedding()
    migrated.fingerprint = "test-semantic-v2"

    stats = SQLiteVectorIndex(path, embedding=migrated).projection_stats

    assert stats == {"records": 0, "sources": 0, "stale_sources": 1}


def test_incremental_projection_batches_embedding_work(tmp_path):
    embedding = _SemanticEmbedding()
    original_embed = embedding.embed
    calls: list[list[str]] = []

    def counting_embed(texts: list[str]) -> np.ndarray:
        calls.append(texts)
        return original_embed(texts)

    embedding.embed = counting_embed
    index = SQLiteVectorIndex(tmp_path / "vectors.sqlite", embedding=embedding)
    documents = [
        IndexedDocument(
            source_id=f"page:{number}",
            source_kind="wiki",
            title=f"预算 {number}",
            content=f"第 {number} 家媒体报价。",
            citation=f"budget.md#{number}",
        )
        for number in range(5)
    ]

    index.upsert(documents)

    assert len(calls) == 1
    assert len(calls[0]) == 5


def test_vector_hit_resolves_exact_original_span(tmp_path):
    document = IndexedDocument(
        source_id="source:budget", source_kind="wiki", title="预算",
        content="背景😀\n\n媒体报价只在周五有效。", citation="budget.md", revision="2026-09-27",
    )
    index = SQLiteVectorIndex(tmp_path / "vectors.sqlite", embedding=_SemanticEmbedding())
    index.upsert([document])
    hit = index.search("报价", limit=1)[0]
    assert resolve_hit_source(hit, document) == {
        "status": "ready", "excerpt": hit.content,
        "source_start": hit.source_start, "source_end": hit.source_end,
    }
