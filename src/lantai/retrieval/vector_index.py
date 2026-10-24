# -*- coding: utf-8 -*-
"""Persistent exact-cosine vector projection backed by SQLite."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Protocol

import numpy as np

from .local_index import IndexedDocument, RetrievalHit, chunk_document_with_spans


class EmbeddingProvider(Protocol):
    """Small public seam implemented by local and remote embedding runtimes."""

    model_name: str
    dimensions: int
    fingerprint: str

    def embed(self, texts: list[str]) -> np.ndarray: ...


class SQLiteVectorIndex:
    """Disposable vector projection with deterministic deletion and rebuilds."""

    SCHEMA_VERSION = 2

    def __init__(
        self,
        path: str | Path,
        *,
        embedding: EmbeddingProvider,
        chunk_chars: int = 1200,
        min_similarity: float = 0.25,
        embedding_batch_size: int = 32,
    ) -> None:
        self.path = Path(path)
        self.embedding = embedding
        self.chunk_chars = max(256, chunk_chars)
        self.min_similarity = max(-1.0, min(1.0, float(min_similarity)))
        self.embedding_batch_size = max(1, int(embedding_batch_size))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS vector_sources (
                    source_id TEXT PRIMARY KEY, source_kind TEXT NOT NULL,
                    content_hash TEXT NOT NULL, revision TEXT NOT NULL,
                    model_fingerprint TEXT NOT NULL,
                    projection_version INTEGER NOT NULL DEFAULT 2,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )""",
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS vector_chunks (
                    chunk_id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
                    source_kind TEXT NOT NULL, title TEXT NOT NULL,
                    content TEXT NOT NULL, citation TEXT NOT NULL,
                    trust REAL NOT NULL, model_fingerprint TEXT NOT NULL,
                    dimensions INTEGER NOT NULL, vector BLOB NOT NULL,
                    source_revision TEXT NOT NULL DEFAULT '',
                    source_text_hash TEXT NOT NULL DEFAULT '',
                    source_start INTEGER NOT NULL DEFAULT -1,
                    source_end INTEGER NOT NULL DEFAULT -1
                )""",
            )
            source_columns = {row["name"] for row in conn.execute("PRAGMA table_info(vector_sources)")}
            if "projection_version" not in source_columns:
                conn.execute("ALTER TABLE vector_sources ADD COLUMN projection_version INTEGER NOT NULL DEFAULT 1")
            chunk_columns = {row["name"] for row in conn.execute("PRAGMA table_info(vector_chunks)")}
            for name, sql_type, default in (
                ("source_revision", "TEXT", "''"),
                ("source_text_hash", "TEXT", "''"),
                ("source_start", "INTEGER", "-1"),
                ("source_end", "INTEGER", "-1"),
            ):
                if name not in chunk_columns:
                    conn.execute(f"ALTER TABLE vector_chunks ADD COLUMN {name} {sql_type} NOT NULL DEFAULT {default}")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS vector_chunks_source_idx "
                "ON vector_chunks(source_id)",
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS vector_chunks_model_idx "
                "ON vector_chunks(model_fingerprint)",
            )

    def upsert(self, documents: Iterable[IndexedDocument]) -> int:
        fingerprint = str(self.embedding.fingerprint)
        materialized = list(documents)
        changed_documents: list[IndexedDocument] = []
        with self._connect() as conn:
            for document in materialized:
                row = conn.execute(
                    "SELECT content_hash, model_fingerprint, projection_version FROM vector_sources "
                    "WHERE source_id = ?",
                    (document.source_id,),
                ).fetchone()
                if (
                    row
                    and row["content_hash"] == document.content_hash
                    and row["model_fingerprint"] == fingerprint
                    and row["projection_version"] == self.SCHEMA_VERSION
                ):
                    continue
                changed_documents.append(document)

        # Compute every replacement before opening the write transaction. A
        # failed model migration therefore leaves the previous generation
        # fully queryable instead of producing a half-reembedded projection.
        chunk_groups: list[tuple[IndexedDocument, list[tuple[str, int, int]]]] = []
        all_chunks: list[str] = []
        for document in changed_documents:
            chunks = list(chunk_document_with_spans(document, chunk_chars=self.chunk_chars))
            chunk_groups.append((document, chunks))
            all_chunks.extend(content for content, _start, _end in chunks)
        vector_batches = [
            self._validated_embeddings(
                all_chunks[index : index + self.embedding_batch_size],
            )
            for index in range(0, len(all_chunks), self.embedding_batch_size)
        ]
        all_vectors = (
            np.vstack(vector_batches)
            if vector_batches
            else np.empty((0, int(self.embedding.dimensions)), dtype=np.float32)
        )
        prepared: list[tuple[IndexedDocument, list[tuple[str, int, int]], np.ndarray]] = []
        offset = 0
        for document, chunks in chunk_groups:
            end = offset + len(chunks)
            prepared.append((document, chunks, all_vectors[offset:end]))
            offset = end

        with self._connect() as conn:
            for document, chunks, vectors in prepared:
                conn.execute(
                    "DELETE FROM vector_chunks WHERE source_id = ?",
                    (document.source_id,),
                )
                for index, ((content, start, end), vector) in enumerate(zip(chunks, vectors)):
                    conn.execute(
                        """INSERT INTO vector_chunks (
                        chunk_id, source_id, source_kind, title, content, citation,
                        trust, model_fingerprint, dimensions, vector,
                        source_revision, source_text_hash, source_start, source_end
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            f"{document.source_id}:{index}",
                            document.source_id,
                            document.source_kind,
                            document.title,
                            content,
                            document.citation,
                            document.trust,
                            fingerprint,
                            int(vector.shape[0]),
                            vector.astype(np.float32, copy=False).tobytes(),
                            document.revision,
                            document.source_text_hash,
                            start,
                            end,
                        ),
                    )
                conn.execute(
                    """INSERT INTO vector_sources(
                        source_id, source_kind, content_hash, revision,
                        model_fingerprint, projection_version
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_id) DO UPDATE SET
                        source_kind=excluded.source_kind,
                        content_hash=excluded.content_hash,
                        revision=excluded.revision,
                        model_fingerprint=excluded.model_fingerprint,
                        projection_version=excluded.projection_version,
                        updated_at=CURRENT_TIMESTAMP""",
                    (
                        document.source_id,
                        document.source_kind,
                        document.content_hash,
                        document.revision,
                        fingerprint,
                        self.SCHEMA_VERSION,
                    ),
                )
        return len(prepared)

    def sync(
        self,
        documents: Iterable[IndexedDocument],
        *,
        source_prefixes: Iterable[str],
    ) -> int:
        materialized = list(documents)
        changed = self.upsert(materialized)
        expected = {document.source_id for document in materialized}
        prefixes = tuple(prefix for prefix in source_prefixes if prefix)
        if not prefixes:
            return changed
        with self._connect() as conn:
            rows = conn.execute("SELECT source_id FROM vector_sources").fetchall()
        stale = [
            str(row["source_id"])
            for row in rows
            if str(row["source_id"]).startswith(prefixes)
            and str(row["source_id"]) not in expected
        ]
        for source_id in stale:
            self.delete_source(source_id)
        return changed + len(stale)

    def search(self, query: str, *, limit: int = 10) -> list[RetrievalHit]:
        query = query.strip()
        if not query or limit <= 0:
            return []
        query_vector = self._validated_embeddings([query])[0]
        fingerprint = str(self.embedding.fingerprint)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM vector_chunks WHERE model_fingerprint = ? "
                "AND dimensions = ?",
                (fingerprint, int(query_vector.shape[0])),
            ).fetchall()
        ranked: list[tuple[float, sqlite3.Row]] = []
        query_norm = float(np.linalg.norm(query_vector))
        if query_norm <= 0:
            return []
        for row in rows:
            vector = np.frombuffer(row["vector"], dtype=np.float32)
            denominator = query_norm * float(np.linalg.norm(vector))
            if denominator <= 0:
                continue
            score = float(np.dot(query_vector, vector) / denominator)
            if score < self.min_similarity:
                continue
            ranked.append((score, row))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [self._hit(row, score=score) for score, row in ranked[:limit]]

    def delete_source(self, source_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM vector_chunks WHERE source_id = ?", (source_id,))
            conn.execute("DELETE FROM vector_sources WHERE source_id = ?", (source_id,))

    def rebuild(self, documents: Iterable[IndexedDocument]) -> int:
        with self._connect() as conn:
            conn.execute("DELETE FROM vector_chunks")
            conn.execute("DELETE FROM vector_sources")
        return self.upsert(documents)

    @property
    def record_count(self) -> int:
        return self.projection_stats["records"]

    @property
    def projection_stats(self) -> dict[str, int]:
        fingerprint = str(self.embedding.fingerprint)
        with self._connect() as conn:
            records = conn.execute(
                "SELECT COUNT(*) AS count FROM vector_chunks "
                "WHERE model_fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            sources = conn.execute(
                "SELECT COUNT(*) AS count FROM vector_sources "
                "WHERE model_fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            stale = conn.execute(
                "SELECT COUNT(*) AS count FROM vector_sources "
                "WHERE model_fingerprint != ?",
                (fingerprint,),
            ).fetchone()
        return {
            "records": int(records["count"] if records else 0),
            "sources": int(sources["count"] if sources else 0),
            "stale_sources": int(stale["count"] if stale else 0),
        }

    def _validated_embeddings(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, int(self.embedding.dimensions)), dtype=np.float32)
        vectors = np.asarray(self.embedding.embed(texts), dtype=np.float32)
        expected = (len(texts), int(self.embedding.dimensions))
        if vectors.shape != expected:
            raise ValueError(
                f"embedding shape {vectors.shape} does not match expected {expected}",
            )
        if not np.isfinite(vectors).all():
            raise ValueError("embedding output contains non-finite values")
        return vectors

    @staticmethod
    def _hit(row: sqlite3.Row, *, score: float) -> RetrievalHit:
        return RetrievalHit(
            chunk_id=row["chunk_id"],
            source_id=row["source_id"],
            source_kind=row["source_kind"],
            title=row["title"],
            content=row["content"],
            citation=row["citation"],
            score=score,
            trust=float(row["trust"]),
            source_revision=row["source_revision"],
            source_text_hash=row["source_text_hash"],
            source_start=int(row["source_start"]),
            source_end=int(row["source_end"]),
        )
