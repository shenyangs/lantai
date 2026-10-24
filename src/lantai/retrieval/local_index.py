# -*- coding: utf-8 -*-
"""Small dependency-free FTS5/BM25 index with auditable incremental updates."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal


SourceKind = Literal["wiki", "memory", "conversation", "atom", "system2", "file"]


@dataclass(frozen=True)
class IndexedDocument:
    source_id: str
    source_kind: SourceKind
    title: str
    content: str
    citation: str
    revision: str = ""
    trust: float = 1.0

    @property
    def content_hash(self) -> str:
        payload = "\x1f".join((self.source_id, self.title, self.content, self.revision))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def source_text_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RetrievalHit:
    chunk_id: str
    source_id: str
    source_kind: SourceKind
    title: str
    content: str
    citation: str
    score: float
    trust: float
    source_revision: str = ""
    source_text_hash: str = ""
    source_start: int = -1
    source_end: int = -1


def resolve_hit_source(hit: RetrievalHit, document: IndexedDocument) -> dict[str, object]:
    """Resolve codepoint offsets only against the exact indexed source version."""

    if (
        hit.source_id != document.source_id
        or not hit.source_text_hash
        or hit.source_revision != document.revision
        or hit.source_text_hash != document.source_text_hash
    ):
        return {"status": "version_unavailable", "excerpt": ""}
    if not (0 <= hit.source_start <= hit.source_end <= len(document.content)):
        return {"status": "position_unavailable", "excerpt": ""}
    excerpt = document.content[hit.source_start:hit.source_end]
    if excerpt != hit.content:
        return {"status": "position_unavailable", "excerpt": ""}
    return {
        "status": "ready", "excerpt": excerpt,
        "source_start": hit.source_start, "source_end": hit.source_end,
    }


def chunk_document_with_spans(
    document: IndexedDocument,
    *,
    chunk_chars: int = 1200,
) -> Iterable[tuple[str, int, int]]:
    """Yield exact source substrings and Python/Unicode codepoint offsets."""

    chunk_chars = max(256, chunk_chars)
    content = document.content
    separator = re.compile(r"\n\s*\n")
    pieces: list[tuple[int, int]] = []
    start = 0
    for match in separator.finditer(content):
        pieces.append((start, match.start()))
        start = match.end()
    pieces.append((start, len(content)))
    buffer_start = -1
    buffer_end = -1

    def trimmed(bounds: tuple[int, int]) -> tuple[int, int]:
        left, right = bounds
        while left < right and content[left].isspace():
            left += 1
        while right > left and content[right - 1].isspace():
            right -= 1
        return left, right

    for bounds in pieces:
        part_start, part_end = trimmed(bounds)
        if part_start == part_end:
            continue
        if buffer_start >= 0 and part_end - buffer_start > chunk_chars:
            yield content[buffer_start:buffer_end], buffer_start, buffer_end
            buffer_start = buffer_end = -1
        if part_end - part_start > chunk_chars:
            cursor = part_start
            while cursor < part_end:
                end = min(cursor + chunk_chars, part_end)
                yield content[cursor:end], cursor, end
                if end == part_end:
                    break
                cursor = max(cursor + 1, end - 120)
            continue
        if buffer_start < 0:
            buffer_start = part_start
        buffer_end = part_end
    if buffer_start >= 0:
        yield content[buffer_start:buffer_end], buffer_start, buffer_end


def chunk_document(
    document: IndexedDocument,
    *,
    chunk_chars: int = 1200,
) -> Iterable[str]:
    """Compatibility text-only view of exact source chunks."""

    for chunk, _start, _end in chunk_document_with_spans(document, chunk_chars=chunk_chars):
        yield chunk


class SQLiteRetrievalIndex:
    """Persistent lexical index; safe when FTS5 is absent or index is rebuilt."""

    SCHEMA_VERSION = 2

    def __init__(self, path: str | Path, *, chunk_chars: int = 1200) -> None:
        self.path = Path(path)
        self.chunk_chars = max(256, chunk_chars)
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
                """CREATE TABLE IF NOT EXISTS retrieval_sources (
                    source_id TEXT PRIMARY KEY, source_kind TEXT NOT NULL,
                    content_hash TEXT NOT NULL, revision TEXT NOT NULL,
                    projection_version INTEGER NOT NULL DEFAULT 2,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )""",
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS retrieval_chunks (
                    chunk_id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
                    source_kind TEXT NOT NULL, title TEXT NOT NULL,
                    content TEXT NOT NULL, citation TEXT NOT NULL,
                    trust REAL NOT NULL,
                    source_revision TEXT NOT NULL DEFAULT '',
                    source_text_hash TEXT NOT NULL DEFAULT '',
                    source_start INTEGER NOT NULL DEFAULT -1,
                    source_end INTEGER NOT NULL DEFAULT -1
                )""",
            )
            source_columns = {row["name"] for row in conn.execute("PRAGMA table_info(retrieval_sources)")}
            if "projection_version" not in source_columns:
                conn.execute("ALTER TABLE retrieval_sources ADD COLUMN projection_version INTEGER NOT NULL DEFAULT 1")
            chunk_columns = {row["name"] for row in conn.execute("PRAGMA table_info(retrieval_chunks)")}
            for name, sql_type, default in (
                ("source_revision", "TEXT", "''"),
                ("source_text_hash", "TEXT", "''"),
                ("source_start", "INTEGER", "-1"),
                ("source_end", "INTEGER", "-1"),
            ):
                if name not in chunk_columns:
                    conn.execute(f"ALTER TABLE retrieval_chunks ADD COLUMN {name} {sql_type} NOT NULL DEFAULT {default}")
            try:
                conn.execute(
                    """CREATE VIRTUAL TABLE IF NOT EXISTS retrieval_fts
                    USING fts5(chunk_id UNINDEXED, title, content,
                    tokenize='unicode61')""",
                )
                self._fts_enabled = True
            except sqlite3.OperationalError:
                self._fts_enabled = False

    @property
    def fts_enabled(self) -> bool:
        return self._fts_enabled

    def upsert(self, documents: Iterable[IndexedDocument]) -> int:
        """Replace only sources whose content hash/revision changed."""
        changed = 0
        with self._connect() as conn:
            for document in documents:
                row = conn.execute(
                    "SELECT content_hash, projection_version FROM retrieval_sources WHERE source_id = ?",
                    (document.source_id,),
                ).fetchone()
                if row and row["content_hash"] == document.content_hash and row["projection_version"] == self.SCHEMA_VERSION:
                    continue
                conn.execute("DELETE FROM retrieval_chunks WHERE source_id = ?", (document.source_id,))
                if self._fts_enabled:
                    conn.execute("DELETE FROM retrieval_fts WHERE chunk_id LIKE ?", (f"{document.source_id}:%",))
                chunks = list(chunk_document_with_spans(document, chunk_chars=self.chunk_chars))
                for index, (content, start, end) in enumerate(chunks):
                    chunk_id = f"{document.source_id}:{index}"
                    conn.execute(
                        """INSERT INTO retrieval_chunks (
                        chunk_id, source_id, source_kind, title, content, citation, trust,
                        source_revision, source_text_hash, source_start, source_end
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (chunk_id, document.source_id, document.source_kind, document.title,
                         content, document.citation, document.trust, document.revision,
                         document.source_text_hash, start, end),
                    )
                    if self._fts_enabled:
                        conn.execute(
                            "INSERT INTO retrieval_fts (chunk_id, title, content) VALUES (?, ?, ?)",
                            (chunk_id, document.title, content),
                        )
                conn.execute(
                    """INSERT INTO retrieval_sources(source_id, source_kind, content_hash, revision, projection_version)
                    VALUES (?, ?, ?, ?, ?) ON CONFLICT(source_id) DO UPDATE SET
                    source_kind=excluded.source_kind, content_hash=excluded.content_hash,
                    revision=excluded.revision, projection_version=excluded.projection_version,
                    updated_at=CURRENT_TIMESTAMP""",
                    (document.source_id, document.source_kind, document.content_hash, document.revision, self.SCHEMA_VERSION),
                )
                changed += 1
        return changed

    def sync(
        self,
        documents: Iterable[IndexedDocument],
        *,
        source_prefixes: Iterable[str],
    ) -> int:
        """Incrementally upsert a source family and prune removed members.

        ``upsert`` intentionally never guesses which caller owns a source. A
        synchronizing importer supplies its own prefixes, so removing a Wiki
        page or a forgotten memory cannot leave a stale FTS candidate while
        unrelated index families remain untouched.
        """

        materialized = list(documents)
        changed = self.upsert(materialized)
        expected = {document.source_id for document in materialized}
        prefixes = tuple(prefix for prefix in source_prefixes if prefix)
        if not prefixes:
            return changed
        with self._connect() as conn:
            rows = conn.execute("SELECT source_id FROM retrieval_sources").fetchall()
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
        if not query:
            return []
        with self._connect() as conn:
            if self._fts_enabled:
                terms = self._fts_query(query)
                try:
                    rows = conn.execute(
                        """SELECT c.*, bm25(retrieval_fts, 2.0, 1.0) AS rank
                        FROM retrieval_fts JOIN retrieval_chunks c USING(chunk_id)
                        WHERE retrieval_fts MATCH ? ORDER BY rank LIMIT ?""",
                        (terms, limit),
                    ).fetchall()
                    if rows:
                        return [
                            self._hit(row, score=-float(row["rank"]))
                            for row in rows
                        ]
                except sqlite3.OperationalError:
                    # Chinese or punctuation-heavy queries can be invalid FTS
                    # syntax. The portable lexical fallback remains available.
                    pass
            rows = conn.execute("SELECT * FROM retrieval_chunks").fetchall()
        tokens = self._tokens(query)
        ranked = []
        for row in rows:
            text = f"{row['title']}\n{row['content']}".lower()
            score = sum(text.count(token) for token in tokens)
            if score:
                ranked.append(self._hit(row, score=float(score)))
        return sorted(ranked, key=lambda item: item.score, reverse=True)[:limit]

    def delete_source(self, source_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM retrieval_chunks WHERE source_id = ?", (source_id,))
            conn.execute("DELETE FROM retrieval_sources WHERE source_id = ?", (source_id,))
            if self._fts_enabled:
                conn.execute("DELETE FROM retrieval_fts WHERE chunk_id LIKE ?", (f"{source_id}:%",))

    def rebuild(self, documents: Iterable[IndexedDocument]) -> int:
        """Delete index state only; source materials stay untouched."""
        with self._connect() as conn:
            conn.execute("DELETE FROM retrieval_chunks")
            conn.execute("DELETE FROM retrieval_sources")
            if self._fts_enabled:
                conn.execute("DELETE FROM retrieval_fts")
        return self.upsert(documents)

    @staticmethod
    def _tokens(value: str) -> list[str]:
        words = re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{1,2}", value.lower())
        return words or [value.lower()]

    @classmethod
    def _fts_query(cls, value: str) -> str:
        # FTS5 understands quoted strings and OR. CJK char pairs improve its
        # unicode tokenizer fallback without injecting raw query operators.
        return " OR ".join(f'"{term.replace(chr(34), "")}"' for term in cls._tokens(value))

    @staticmethod
    def _hit(row: sqlite3.Row, *, score: float) -> RetrievalHit:
        return RetrievalHit(
            chunk_id=row["chunk_id"], source_id=row["source_id"],
            source_kind=row["source_kind"], title=row["title"], content=row["content"],
            citation=row["citation"], score=score, trust=float(row["trust"]),
            source_revision=row["source_revision"], source_text_hash=row["source_text_hash"],
            source_start=int(row["source_start"]), source_end=int(row["source_end"]),
        )
