# -*- coding: utf-8 -*-
"""One deep local retrieval module for lexical and semantic projections."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Iterable

from .local_index import IndexedDocument, SQLiteRetrievalIndex
from .orchestrator import NativeIndexAdapter, RetrievalCandidate, RetrievalOrchestrator
from .vector_index import EmbeddingProvider, SQLiteVectorIndex


logger = logging.getLogger(__name__)


class HybridRetrievalRuntime:
    """Own projection, fusion, fallback and status behind one small interface."""

    def __init__(
        self,
        root: str | Path,
        *,
        embedding: EmbeddingProvider | None,
        rrf_k: int = 60,
        lexical_filename: str = "lexical.sqlite",
        vector_filename: str = "vectors.sqlite",
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lexical_index = SQLiteRetrievalIndex(self.root / lexical_filename)
        self.vector_index = (
            SQLiteVectorIndex(self.root / vector_filename, embedding=embedding)
            if embedding is not None
            else None
        )
        self.rrf_k = max(1, int(rrf_k))
        self._vector_error = "" if self.vector_index is not None else "embedding unavailable"

    def sync(
        self,
        documents: Iterable[IndexedDocument],
        *,
        source_prefixes: Iterable[str],
    ) -> dict[str, int]:
        materialized = list(documents)
        prefixes = tuple(source_prefixes)
        lexical_changed = self.lexical_index.sync(
            materialized,
            source_prefixes=prefixes,
        )
        vector_changed = 0
        if self.vector_index is not None:
            try:
                vector_changed = self.vector_index.sync(
                    materialized,
                    source_prefixes=prefixes,
                )
                self._vector_error = ""
            except Exception as exc:  # lexical retrieval must remain usable
                self._vector_error = str(exc)
                logger.warning("Vector projection sync failed; using lexical recall", exc_info=True)
        return {"lexical_changed": lexical_changed, "vector_changed": vector_changed}

    def search(self, query: str, *, limit: int = 8) -> list[RetrievalCandidate]:
        lexical = NativeIndexAdapter("lexical", self.lexical_index)
        if self.vector_index is not None and not self._vector_error:
            try:
                return RetrievalOrchestrator(
                    [lexical, NativeIndexAdapter("vector", self.vector_index)],
                    rrf_k=self.rrf_k,
                ).search(query, limit=limit)
            except Exception as exc:
                self._vector_error = str(exc)
                logger.warning("Vector retrieval failed; using lexical recall", exc_info=True)
        return RetrievalOrchestrator([lexical], rrf_k=self.rrf_k).search(
            query,
            limit=limit,
        )

    def delete_source(self, source_id: str) -> None:
        self.lexical_index.delete_source(source_id)
        if self.vector_index is not None:
            self.vector_index.delete_source(source_id)

    def rebuild(self, documents: Iterable[IndexedDocument]) -> dict[str, int]:
        materialized = list(documents)
        lexical_changed = self.lexical_index.rebuild(materialized)
        vector_changed = 0
        if self.vector_index is not None:
            try:
                vector_changed = self.vector_index.rebuild(materialized)
                self._vector_error = ""
            except Exception as exc:
                self._vector_error = str(exc)
                logger.warning("Vector projection rebuild failed", exc_info=True)
        return {"lexical_changed": lexical_changed, "vector_changed": vector_changed}

    def status(self) -> dict[str, object]:
        vector_stats = (
            self.vector_index.projection_stats
            if self.vector_index
            else {"records": 0, "sources": 0, "stale_sources": 0}
        )
        vector_ready = self.vector_index is not None and not self._vector_error
        return {
            "mode": "hybrid" if vector_ready else "lexical",
            "lexical": {"ready": True, "fts5": self.lexical_index.fts_enabled},
            "embedding": {
                "ready": vector_ready,
                "model": (
                    self.vector_index.embedding.model_name if self.vector_index else None
                ),
                "dimensions": (
                    self.vector_index.embedding.dimensions if self.vector_index else None
                ),
                "fingerprint": (
                    self.vector_index.embedding.fingerprint if self.vector_index else None
                ),
            },
            "vector": {
                "ready": vector_ready,
                **vector_stats,
                "error": self._vector_error or None,
            },
        }

    def health_probe(self) -> dict[str, object]:
        """Prove embedding plus vector write/query/delete instead of inferring it."""

        if self.vector_index is None:
            return {
                "overall": "degraded",
                "mode": "lexical",
                "embedding": {"ready": False, "error": self._vector_error},
                "vector": {
                    "ready": False,
                    "write": False,
                    "query": False,
                    "delete": False,
                    "records": 0,
                    "error": self._vector_error,
                },
            }
        source_id = "__health__:vector-runtime"
        before = self.vector_index.record_count
        embedding_ready = False
        wrote = False
        queried = False
        deleted = False
        error = ""
        started = time.perf_counter()
        try:
            vector = self.vector_index.embedding.embed(["澜台向量健康检查"])
            embedding_ready = vector.shape == (
                1,
                int(self.vector_index.embedding.dimensions),
            )
            if not embedding_ready:
                raise RuntimeError(f"unexpected health embedding shape: {vector.shape}")
            probe = IndexedDocument(
                source_id=source_id,
                source_kind="file",
                title="retrieval health probe",
                content="澜台向量健康检查",
                citation="internal://retrieval-health",
                trust=1.0,
            )
            self.vector_index.upsert([probe])
            wrote = self.vector_index.record_count == before + 1
            queried = any(
                hit.source_id == source_id
                for hit in self.vector_index.search("向量健康检查", limit=max(1, before + 1))
            )
        except Exception as exc:
            error = str(exc)
        finally:
            try:
                self.vector_index.delete_source(source_id)
                deleted = self.vector_index.record_count == before
            except Exception as exc:  # pragma: no cover - catastrophic SQLite failure
                error = error or str(exc)
        ready = embedding_ready and wrote and queried and deleted and not error
        self._vector_error = "" if ready else (error or "vector health probe failed")
        return {
            "overall": "ready" if ready else "degraded",
            "mode": "hybrid" if ready else "lexical",
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "embedding": {
                "ready": embedding_ready,
                "model": self.vector_index.embedding.model_name,
                "dimensions": self.vector_index.embedding.dimensions,
                "fingerprint": self.vector_index.embedding.fingerprint,
                "error": error or None,
            },
            "vector": {
                "ready": ready,
                "write": wrote,
                "query": queried,
                "delete": deleted,
                **self.vector_index.projection_stats,
                "error": error or None,
            },
        }
