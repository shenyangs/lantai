# -*- coding: utf-8 -*-
"""End-to-end behaviour locks for the deep retrieval runtime."""

from tests.unit.retrieval.test_vector_index import _SemanticEmbedding

from lantai.retrieval import HybridRetrievalRuntime, IndexedDocument


def test_hybrid_runtime_combines_lexical_and_semantic_recall(tmp_path):
    runtime = HybridRetrievalRuntime(
        tmp_path / "knowledge-retrieval",
        embedding=_SemanticEmbedding(),
    )
    runtime.sync(
        [
            IndexedDocument(
                source_id="page:budget",
                source_kind="wiki",
                title="媒体专项",
                content="都市报整版报价为 120000 元。",
                citation="budget.xlsx#总表!A8:F8",
            ),
            IndexedDocument(
                source_id="page:song",
                source_kind="wiki",
                title="写歌",
                content="副歌旋律需要有记忆点。",
                citation="song.md#副歌",
            ),
        ],
        source_prefixes=("page:",),
    )

    semantic = runtime.search("不同渠道分别花费多少", limit=2)
    lexical = runtime.search("都市报", limit=2)

    assert semantic[0].citation == "budget.xlsx#总表!A8:F8"
    assert lexical[0].citation == "budget.xlsx#总表!A8:F8"
    assert runtime.status()["mode"] == "hybrid"


def test_health_probe_verifies_real_vector_write_query_and_delete(tmp_path):
    runtime = HybridRetrievalRuntime(
        tmp_path / "memory-retrieval",
        embedding=_SemanticEmbedding(),
    )
    before = runtime.status()["vector"]["records"]

    health = runtime.health_probe()

    assert health["overall"] == "ready"
    assert health["embedding"]["ready"] is True
    assert health["vector"]["write"] is True
    assert health["vector"]["query"] is True
    assert health["vector"]["delete"] is True
    assert runtime.status()["vector"]["records"] == before
