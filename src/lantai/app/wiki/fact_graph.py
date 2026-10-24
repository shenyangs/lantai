# -*- coding: utf-8 -*-
"""Source-grounded, disposable fact graph for local Wiki retrieval."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from collections import defaultdict, deque
from collections.abc import Iterable
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


GraphPolarity = Literal["positive", "negative"]
ClaimStatus = Literal["supported", "contradicted", "conflicted", "unknown"]
ResolutionDecisionKind = Literal[
    "created_singleton",
    "exact_alias",
    "ambiguous_singleton",
]
COLLISION_PRONE_ENTITY_KINDS = {
    "person",
    "organization",
    "company",
    "team",
    "人物",
    "人",
    "组织",
    "公司",
    "团队",
}


class GraphDocument(BaseModel):
    """One original document whose facts may be projected into the graph."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(..., min_length=1, max_length=600)
    source_path: str = Field(..., min_length=1, max_length=1_200)
    content: str
    revision: str = ""
    trust: float = Field(default=1.0, ge=0.0, le=1.0)

    @field_validator("source_path")
    @classmethod
    def validate_source_path(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        relative = Path(normalized)
        if (
            not normalized
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ValueError("source_path must stay inside the workspace namespace")
        return relative.as_posix()

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


class ExtractedEntity(BaseModel):
    """One document-local entity candidate produced by an extractor."""

    model_config = ConfigDict(extra="forbid")

    local_id: str = Field(..., min_length=1, max_length=160)
    name: str = Field(..., min_length=1, max_length=300)
    kind: str = Field(default="entity", min_length=1, max_length=100)
    aliases: list[str] = Field(default_factory=list, max_length=16)


class ExtractedRelation(BaseModel):
    """One candidate relation with a verbatim evidence quote."""

    model_config = ConfigDict(extra="forbid")

    subject_id: str = Field(..., min_length=1, max_length=160)
    predicate: str = Field(..., min_length=1, max_length=200)
    object_id: str = Field(..., min_length=1, max_length=160)
    evidence_quote: str = Field(..., min_length=1, max_length=4_000)
    evidence_start: int | None = Field(default=None, ge=0)
    evidence_end: int | None = Field(default=None, ge=0)
    section: str = Field(default="", max_length=300)
    polarity: GraphPolarity = "positive"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class GraphExtraction(BaseModel):
    """Validated extractor output for one source document."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    extractor_version: str = "evidence-graph-v1"
    prompt_version: str = "evidence-graph-prompt-v1"
    model_label: str = ""
    entities: list[ExtractedEntity] = Field(default_factory=list, max_length=120)
    relations: list[ExtractedRelation] = Field(default_factory=list, max_length=240)


class GraphSyncReport(BaseModel):
    """Observable result of atomically replacing one source projection."""

    source_id: str
    changed: bool
    entities_retained: int = 0
    relations_written: int = 0
    relations_rejected: int = 0


class GraphStats(BaseModel):
    """Small shadow-mode health snapshot for the derived graph."""

    sources: int = 0
    entities: int = 0
    aliases: int = 0
    relations: int = 0
    evidence: int = 0


class GraphRebuildReport(BaseModel):
    """Aggregate result for rebuilding disposable graph state."""

    sources_rebuilt: int = 0
    entities_retained: int = 0
    relations_written: int = 0
    relations_rejected: int = 0


class GraphResolutionDecision(BaseModel):
    """One auditable entity-resolution decision from a source run."""

    run_id: str
    source_id: str
    local_id: str
    entity_id: str
    decision: ResolutionDecisionKind
    candidate_ids: list[str] = Field(default_factory=list)


class GraphIngestionRun(BaseModel):
    """Versioned summary for one completed source synchronization."""

    run_id: str
    source_id: str
    status: str
    entities_retained: int
    relations_written: int
    relations_rejected: int
    extractor_version: str
    schema_version: int
    prompt_version: str
    model_label: str = ""
    extracted_at: str = ""


class GraphSourceAudit(BaseModel):
    """Bounded audit history for shadow metrics and review."""

    runs: list[GraphIngestionRun] = Field(default_factory=list)
    resolution_decisions: list[GraphResolutionDecision] = Field(default_factory=list)


class GraphEvidence(BaseModel):
    """A relation citation that resolves to an exact source substring."""

    source_id: str
    source_path: str
    source_sha256: str
    section: str = ""
    span_start: int
    span_end: int
    quote: str
    trust: float
    confidence: float
    extractor_version: str
    schema_version: int
    prompt_version: str
    model_label: str = ""
    extracted_at: str = ""

    @property
    def citation(self) -> str:
        return f"{self.source_path}#char={self.span_start}-{self.span_end}"


class GraphFact(BaseModel):
    """One directed fact and its source evidence."""

    relation_id: str
    subject_id: str
    subject_name: str
    predicate: str
    object_id: str
    object_name: str
    polarity: GraphPolarity
    evidence: list[GraphEvidence] = Field(default_factory=list)


class GraphPath(BaseModel):
    """One bounded path returned to the retrieval Adapter."""

    seed_entity_ids: list[str] = Field(default_factory=list)
    facts: list[GraphFact] = Field(default_factory=list)
    score: float = 0.0


class ClaimAssessment(BaseModel):
    """Evidence-aware result for one exact subject/predicate/object claim."""

    status: ClaimStatus
    facts: list[GraphFact] = Field(default_factory=list)


class EvidenceGraphIndex:
    """Deep Module for source replacement, entity resolution, and bounded traversal."""

    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS graph_sources (
                    source_id TEXT PRIMARY KEY,
                    source_path TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    trust REAL NOT NULL,
                    extractor_version TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    prompt_version TEXT NOT NULL,
                    model_label TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS graph_entities (
                    entity_id TEXT PRIMARY KEY,
                    canonical_name TEXT NOT NULL,
                    entity_kind TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS graph_aliases (
                    entity_id TEXT NOT NULL REFERENCES graph_entities(entity_id)
                        ON DELETE CASCADE,
                    alias TEXT NOT NULL,
                    normalized_alias TEXT NOT NULL,
                    entity_kind TEXT NOT NULL,
                    PRIMARY KEY(entity_id, normalized_alias)
                );
                CREATE INDEX IF NOT EXISTS graph_alias_lookup
                    ON graph_aliases(normalized_alias, entity_kind);
                CREATE TABLE IF NOT EXISTS graph_entity_sources (
                    source_id TEXT NOT NULL REFERENCES graph_sources(source_id)
                        ON DELETE CASCADE,
                    local_id TEXT NOT NULL,
                    entity_id TEXT NOT NULL REFERENCES graph_entities(entity_id)
                        ON DELETE CASCADE,
                    PRIMARY KEY(source_id, local_id)
                );
                CREATE TABLE IF NOT EXISTS graph_relations (
                    relation_id TEXT PRIMARY KEY,
                    subject_entity_id TEXT NOT NULL REFERENCES graph_entities(entity_id),
                    predicate TEXT NOT NULL,
                    normalized_predicate TEXT NOT NULL,
                    object_entity_id TEXT NOT NULL REFERENCES graph_entities(entity_id),
                    polarity TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS graph_relation_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    relation_id TEXT NOT NULL REFERENCES graph_relations(relation_id)
                        ON DELETE CASCADE,
                    source_id TEXT NOT NULL REFERENCES graph_sources(source_id)
                        ON DELETE CASCADE,
                    source_path TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    section TEXT NOT NULL,
                    span_start INTEGER NOT NULL,
                    span_end INTEGER NOT NULL,
                    quote TEXT NOT NULL,
                    trust REAL NOT NULL,
                    confidence REAL NOT NULL,
                    extractor_version TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    prompt_version TEXT NOT NULL,
                    model_label TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS graph_evidence_relation
                    ON graph_relation_evidence(relation_id);
                CREATE TABLE IF NOT EXISTS graph_resolution_decisions (
                    decision_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    local_id TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    candidate_ids_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS graph_resolution_source
                    ON graph_resolution_decisions(source_id, created_at);
                CREATE TABLE IF NOT EXISTS graph_ingestion_runs (
                    run_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    entities_retained INTEGER NOT NULL,
                    relations_written INTEGER NOT NULL,
                    relations_rejected INTEGER NOT NULL,
                    extractor_version TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    prompt_version TEXT NOT NULL,
                    model_label TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS graph_ingestion_source
                    ON graph_ingestion_runs(source_id, created_at);
                """,
            )

    def sync_document(
        self,
        document: GraphDocument,
        extraction: GraphExtraction,
    ) -> GraphSyncReport:
        """Atomically replace all derived facts for one source document."""

        with self._connect() as conn:
            current = conn.execute(
                """SELECT source_sha256, revision, extractor_version,
                schema_version, prompt_version, model_label
                FROM graph_sources WHERE source_id = ?""",
                (document.source_id,),
            ).fetchone()
            signature = (
                document.content_hash,
                document.revision,
                extraction.extractor_version,
                extraction.schema_version,
                extraction.prompt_version,
                extraction.model_label,
            )
            if current is not None and tuple(current) == signature:
                return GraphSyncReport(
                    source_id=document.source_id,
                    changed=False,
                    entities_retained=len(extraction.entities),
                )

            self._delete_source_projection(conn, document.source_id)
            run_id = f"run:{uuid4()}"
            conn.execute(
                """INSERT INTO graph_sources(
                    source_id, source_path, source_sha256, revision, trust,
                    extractor_version, schema_version, prompt_version, model_label
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    document.source_id,
                    document.source_path,
                    document.content_hash,
                    document.revision,
                    document.trust,
                    extraction.extractor_version,
                    extraction.schema_version,
                    extraction.prompt_version,
                    extraction.model_label,
                ),
            )

            entity_ids: dict[str, str] = {}
            for entity in extraction.entities:
                entity_id, decision, candidate_ids = self._resolve_entity(
                    conn,
                    document.source_id,
                    entity,
                )
                entity_ids[entity.local_id] = entity_id
                conn.execute(
                    """INSERT INTO graph_entity_sources(source_id, local_id, entity_id)
                    VALUES (?, ?, ?)""",
                    (document.source_id, entity.local_id, entity_id),
                )
                conn.execute(
                    """INSERT INTO graph_resolution_decisions(
                        decision_id, run_id, source_id, local_id, entity_id,
                        decision, candidate_ids_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        self._stable_id("decision", run_id, entity.local_id),
                        run_id,
                        document.source_id,
                        entity.local_id,
                        entity_id,
                        decision,
                        json.dumps(candidate_ids, ensure_ascii=False),
                    ),
                )

            written = 0
            rejected = 0
            for relation in extraction.relations:
                subject_id = entity_ids.get(relation.subject_id)
                object_id = entity_ids.get(relation.object_id)
                if subject_id is None or object_id is None:
                    rejected += 1
                    continue
                span = self._resolve_evidence_span(document.content, relation)
                if span is None:
                    rejected += 1
                    continue
                start, end = span
                relation_id = self._relation_id(
                    subject_id,
                    relation.predicate,
                    object_id,
                    relation.polarity,
                )
                conn.execute(
                    """INSERT OR IGNORE INTO graph_relations(
                        relation_id, subject_entity_id, predicate,
                        normalized_predicate, object_entity_id, polarity
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        relation_id,
                        subject_id,
                        relation.predicate.strip(),
                        self._normalize(relation.predicate),
                        object_id,
                        relation.polarity,
                    ),
                )
                evidence_id = self._stable_id(
                    "ev",
                    relation_id,
                    document.source_id,
                    str(start),
                    str(end),
                    relation.evidence_quote,
                )
                conn.execute(
                    """INSERT OR REPLACE INTO graph_relation_evidence(
                        evidence_id, relation_id, source_id, source_path,
                        source_sha256, section, span_start, span_end, quote,
                        trust, confidence, extractor_version, schema_version,
                        prompt_version, model_label
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        evidence_id,
                        relation_id,
                        document.source_id,
                        document.source_path,
                        document.content_hash,
                        relation.section,
                        start,
                        end,
                        relation.evidence_quote,
                        document.trust,
                        relation.confidence,
                        extraction.extractor_version,
                        extraction.schema_version,
                        extraction.prompt_version,
                        extraction.model_label,
                    ),
                )
                written += 1
            self._remove_orphans(conn)
            conn.execute(
                """INSERT INTO graph_ingestion_runs(
                    run_id, source_id, status, entities_retained,
                    relations_written, relations_rejected, extractor_version,
                    schema_version, prompt_version, model_label
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    document.source_id,
                    "completed",
                    len(entity_ids),
                    written,
                    rejected,
                    extraction.extractor_version,
                    extraction.schema_version,
                    extraction.prompt_version,
                    extraction.model_label,
                ),
            )
            return GraphSyncReport(
                source_id=document.source_id,
                changed=True,
                entities_retained=len(entity_ids),
                relations_written=written,
                relations_rejected=rejected,
            )

    def search(
        self,
        query: str,
        *,
        max_hops: int = 2,
        limit: int = 8,
    ) -> list[GraphPath]:
        """Return simple, evidence-bearing paths starting at query entities."""

        normalized_query = self._normalize(query)
        if not normalized_query or limit <= 0:
            return []
        max_hops = max(1, min(max_hops, 3))
        with self._connect() as conn:
            alias_rows = conn.execute(
                "SELECT entity_id, normalized_alias FROM graph_aliases",
            ).fetchall()
            seed_scores: dict[str, float] = {}
            for row in alias_rows:
                alias = str(row["normalized_alias"])
                if alias and alias in normalized_query:
                    seed_scores[str(row["entity_id"])] = max(
                        seed_scores.get(str(row["entity_id"]), 0.0),
                        float(len(alias)),
                    )
            if not seed_scores:
                return []
            facts = self._load_facts(conn)

        adjacency: dict[str, list[GraphFact]] = defaultdict(list)
        for fact in facts:
            adjacency[fact.subject_id].append(fact)
            adjacency[fact.object_id].append(fact)

        paths: list[GraphPath] = []
        seen_paths: set[tuple[str, ...]] = set()
        for seed_id, seed_score in seed_scores.items():
            queue = deque([(seed_id, [], {seed_id})])
            while queue:
                current_id, current_facts, visited_entities = queue.popleft()
                if len(current_facts) >= max_hops:
                    continue
                for fact in adjacency.get(current_id, []):
                    next_id = (
                        fact.object_id
                        if fact.subject_id == current_id
                        else fact.subject_id
                    )
                    if next_id in visited_entities:
                        continue
                    next_facts = [*current_facts, fact]
                    key = tuple(item.relation_id for item in next_facts)
                    reverse_key = tuple(reversed(key))
                    canonical_key = min(key, reverse_key)
                    if canonical_key not in seen_paths:
                        seen_paths.add(canonical_key)
                        evidence_score = sum(
                            max(
                                (
                                    evidence.trust * evidence.confidence
                                    for evidence in item.evidence
                                ),
                                default=0.0,
                            )
                            for item in next_facts
                        )
                        paths.append(
                            GraphPath(
                                seed_entity_ids=[seed_id],
                                facts=next_facts,
                                score=seed_score + evidence_score / len(next_facts),
                            ),
                        )
                    queue.append(
                        (
                            next_id,
                            next_facts,
                            {*visited_entities, next_id},
                        ),
                    )
        paths.sort(key=lambda item: (-item.score, len(item.facts)))
        return paths[:limit]

    def list_facts(self, *, limit: int | None = None) -> list[GraphFact]:
        """Return source-grounded facts for read-only derived projections."""

        with self._connect() as conn:
            facts = self._load_facts(conn)
        ordered = sorted(
            facts,
            key=lambda fact: (
                fact.subject_name,
                fact.predicate,
                fact.object_name,
                fact.relation_id,
            ),
        )
        if limit is None:
            return ordered
        return ordered[: max(0, limit)]

    def evaluate_claim(
        self,
        subject: str,
        predicate: str,
        object_: str,
    ) -> ClaimAssessment:
        """Evaluate only explicit matching edges; absence remains unknown."""

        with self._connect() as conn:
            subject_id = self._resolve_unique_alias(conn, subject)
            object_id = self._resolve_unique_alias(conn, object_)
            if subject_id is None or object_id is None:
                return ClaimAssessment(status="unknown")
            normalized_predicate = self._normalize(predicate)
            matching = [
                fact
                for fact in self._load_facts(conn)
                if fact.subject_id == subject_id
                and fact.object_id == object_id
                and self._normalize(fact.predicate) == normalized_predicate
            ]
        positive = [fact for fact in matching if fact.polarity == "positive"]
        negative = [fact for fact in matching if fact.polarity == "negative"]
        if positive and negative:
            status: ClaimStatus = "conflicted"
        elif positive:
            status = "supported"
        elif negative:
            status = "contradicted"
        else:
            status = "unknown"
        return ClaimAssessment(status=status, facts=matching)

    def delete_source(self, source_id: str) -> None:
        """Delete one source projection without reading or changing originals."""

        with self._connect() as conn:
            self._delete_source_projection(conn, source_id)

    def source_is_current(
        self,
        document: GraphDocument,
        *,
        extractor_version: str,
        schema_version: int,
        prompt_version: str,
    ) -> bool:
        """Check the stable extraction signature before spending a model call."""

        with self._connect() as conn:
            row = conn.execute(
                """SELECT source_sha256, revision, extractor_version,
                schema_version, prompt_version
                FROM graph_sources WHERE source_id = ?""",
                (document.source_id,),
            ).fetchone()
        return row is not None and tuple(row) == (
            document.content_hash,
            document.revision,
            extractor_version,
            schema_version,
            prompt_version,
        )

    def prune_sources(self, expected_source_ids: set[str]) -> int:
        """Remove projections for Wiki sources that no longer exist."""

        with self._connect() as conn:
            existing = {
                str(row["source_id"])
                for row in conn.execute("SELECT source_id FROM graph_sources")
            }
            stale = sorted(existing - expected_source_ids)
            for source_id in stale:
                self._delete_source_projection(conn, source_id)
        return len(stale)

    def rebuild(
        self,
        items: Iterable[tuple[GraphDocument, GraphExtraction]],
    ) -> GraphRebuildReport:
        """Replace all derived state without touching supplied source files."""

        materialized = list(items)
        with self._connect() as conn:
            conn.execute("DELETE FROM graph_relation_evidence")
            conn.execute("DELETE FROM graph_relations")
            conn.execute("DELETE FROM graph_entity_sources")
            conn.execute("DELETE FROM graph_sources")
            conn.execute("DELETE FROM graph_aliases")
            conn.execute("DELETE FROM graph_entities")
        output = GraphRebuildReport()
        for document, extraction in materialized:
            report = self.sync_document(document, extraction)
            if report.changed:
                output.sources_rebuilt += 1
            output.entities_retained += report.entities_retained
            output.relations_written += report.relations_written
            output.relations_rejected += report.relations_rejected
        return output

    def stats(self) -> GraphStats:
        """Return aggregate derived-state counts without exposing SQLite rows."""

        tables = {
            "sources": "graph_sources",
            "entities": "graph_entities",
            "aliases": "graph_aliases",
            "relations": "graph_relations",
            "evidence": "graph_relation_evidence",
        }
        with self._connect() as conn:
            counts = {
                name: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for name, table in tables.items()
            }
        return GraphStats(**counts)

    def source_audit(
        self,
        source_id: str,
        *,
        limit: int = 20,
    ) -> GraphSourceAudit:
        """Return bounded run and resolution history for one source."""

        limit = max(1, min(limit, 100))
        with self._connect() as conn:
            run_rows = conn.execute(
                """SELECT * FROM graph_ingestion_runs
                WHERE source_id = ? ORDER BY rowid DESC LIMIT ?""",
                (source_id, limit),
            ).fetchall()
            decision_rows = conn.execute(
                """SELECT * FROM graph_resolution_decisions
                WHERE source_id = ? ORDER BY rowid DESC LIMIT ?""",
                (source_id, limit * 8),
            ).fetchall()
        return GraphSourceAudit(
            runs=[
                GraphIngestionRun(
                    run_id=row["run_id"],
                    source_id=row["source_id"],
                    status=row["status"],
                    entities_retained=int(row["entities_retained"]),
                    relations_written=int(row["relations_written"]),
                    relations_rejected=int(row["relations_rejected"]),
                    extractor_version=row["extractor_version"],
                    schema_version=int(row["schema_version"]),
                    prompt_version=row["prompt_version"],
                    model_label=row["model_label"],
                    extracted_at=row["created_at"],
                )
                for row in run_rows
            ],
            resolution_decisions=[
                GraphResolutionDecision(
                    run_id=row["run_id"],
                    source_id=row["source_id"],
                    local_id=row["local_id"],
                    entity_id=row["entity_id"],
                    decision=row["decision"],
                    candidate_ids=json.loads(row["candidate_ids_json"]),
                )
                for row in decision_rows
            ],
        )

    def _resolve_entity(
        self,
        conn: sqlite3.Connection,
        source_id: str,
        entity: ExtractedEntity,
    ) -> tuple[str, ResolutionDecisionKind, list[str]]:
        aliases = self._unique_aliases([entity.name, *entity.aliases])
        candidate_ids: set[str] = set()
        strong_candidate_ids: set[str] = set()
        normalized_name = self._normalize(entity.name)
        for alias in aliases:
            normalized_alias = self._normalize(alias)
            rows = conn.execute(
                """SELECT entity_id FROM graph_aliases
                WHERE normalized_alias = ? AND entity_kind = ?""",
                (normalized_alias, entity.kind),
            ).fetchall()
            matched_ids = {str(row["entity_id"]) for row in rows}
            candidate_ids.update(matched_ids)
            if normalized_alias != normalized_name:
                strong_candidate_ids.update(matched_ids)
        normalized_kind = self._normalize(entity.kind)
        if len(strong_candidate_ids) == 1:
            entity_id = next(iter(strong_candidate_ids))
            decision: ResolutionDecisionKind = "exact_alias"
        elif (
            len(candidate_ids) == 1
            and normalized_kind not in COLLISION_PRONE_ENTITY_KINDS
        ):
            entity_id = next(iter(candidate_ids))
            decision = "exact_alias"
        elif not candidate_ids:
            entity_id = self._stable_id(
                "ent",
                entity.kind,
                self._normalize(entity.name),
            )
            decision = "created_singleton"
        else:
            entity_id = self._stable_id(
                "ent",
                entity.kind,
                self._normalize(entity.name),
                source_id,
                entity.local_id,
            )
            decision = "ambiguous_singleton"
        conn.execute(
            """INSERT OR IGNORE INTO graph_entities(
                entity_id, canonical_name, entity_kind, normalized_name
            ) VALUES (?, ?, ?, ?)""",
            (
                entity_id,
                entity.name.strip(),
                entity.kind,
                self._normalize(entity.name),
            ),
        )
        for alias in aliases:
            conn.execute(
                """INSERT OR IGNORE INTO graph_aliases(
                    entity_id, alias, normalized_alias, entity_kind
                ) VALUES (?, ?, ?, ?)""",
                (entity_id, alias, self._normalize(alias), entity.kind),
            )
        return entity_id, decision, sorted(candidate_ids)

    def _load_facts(self, conn: sqlite3.Connection) -> list[GraphFact]:
        relation_rows = conn.execute(
            """SELECT r.*, subject.canonical_name AS subject_name,
            object.canonical_name AS object_name
            FROM graph_relations r
            JOIN graph_entities subject
              ON subject.entity_id = r.subject_entity_id
            JOIN graph_entities object
              ON object.entity_id = r.object_entity_id""",
        ).fetchall()
        facts: list[GraphFact] = []
        for row in relation_rows:
            evidence_rows = conn.execute(
                """SELECT * FROM graph_relation_evidence
                WHERE relation_id = ?
                ORDER BY trust * confidence DESC, source_path, span_start""",
                (row["relation_id"],),
            ).fetchall()
            evidence = [
                GraphEvidence(
                    source_id=item["source_id"],
                    source_path=item["source_path"],
                    source_sha256=item["source_sha256"],
                    section=item["section"],
                    span_start=int(item["span_start"]),
                    span_end=int(item["span_end"]),
                    quote=item["quote"],
                    trust=float(item["trust"]),
                    confidence=float(item["confidence"]),
                    extractor_version=item["extractor_version"],
                    schema_version=int(item["schema_version"]),
                    prompt_version=item["prompt_version"],
                    model_label=item["model_label"],
                    extracted_at=item["created_at"],
                )
                for item in evidence_rows
            ]
            if not evidence:
                continue
            facts.append(
                GraphFact(
                    relation_id=row["relation_id"],
                    subject_id=row["subject_entity_id"],
                    subject_name=row["subject_name"],
                    predicate=row["predicate"],
                    object_id=row["object_entity_id"],
                    object_name=row["object_name"],
                    polarity=row["polarity"],
                    evidence=evidence,
                ),
            )
        return facts

    def _resolve_unique_alias(
        self,
        conn: sqlite3.Connection,
        alias: str,
    ) -> str | None:
        rows = conn.execute(
            """SELECT DISTINCT entity_id FROM graph_aliases
            WHERE normalized_alias = ?""",
            (self._normalize(alias),),
        ).fetchall()
        if len(rows) != 1:
            return None
        return str(rows[0]["entity_id"])

    @staticmethod
    def _resolve_evidence_span(
        content: str,
        relation: ExtractedRelation,
    ) -> tuple[int, int] | None:
        quote = relation.evidence_quote
        if relation.evidence_start is not None:
            start = relation.evidence_start
            end = (
                relation.evidence_end
                if relation.evidence_end is not None
                else start + len(quote)
            )
            if end < start or content[start:end] != quote:
                return None
            return start, end
        start = content.find(quote)
        if start < 0:
            return None
        return start, start + len(quote)

    def _delete_source_projection(
        self,
        conn: sqlite3.Connection,
        source_id: str,
    ) -> None:
        conn.execute(
            "DELETE FROM graph_relation_evidence WHERE source_id = ?",
            (source_id,),
        )
        conn.execute(
            "DELETE FROM graph_entity_sources WHERE source_id = ?",
            (source_id,),
        )
        conn.execute("DELETE FROM graph_sources WHERE source_id = ?", (source_id,))
        self._remove_orphans(conn)

    @staticmethod
    def _remove_orphans(conn: sqlite3.Connection) -> None:
        conn.execute(
            """DELETE FROM graph_relations
            WHERE relation_id NOT IN (
                SELECT DISTINCT relation_id FROM graph_relation_evidence
            )""",
        )
        conn.execute(
            """DELETE FROM graph_entities
            WHERE entity_id NOT IN (
                SELECT entity_id FROM graph_entity_sources
            )
            AND entity_id NOT IN (
                SELECT subject_entity_id FROM graph_relations
                UNION
                SELECT object_entity_id FROM graph_relations
            )""",
        )

    @classmethod
    def _relation_id(
        cls,
        subject_id: str,
        predicate: str,
        object_id: str,
        polarity: GraphPolarity,
    ) -> str:
        return cls._stable_id(
            "rel",
            subject_id,
            cls._normalize(predicate),
            object_id,
            polarity,
        )

    @staticmethod
    def _stable_id(prefix: str, *parts: str) -> str:
        payload = "\x1f".join(parts)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
        return f"{prefix}:{digest}"

    @staticmethod
    def _normalize(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value).casefold()
        return " ".join(normalized.split())

    @classmethod
    def _unique_aliases(cls, aliases: list[str]) -> list[str]:
        output: list[str] = []
        seen: set[str] = set()
        for alias in aliases:
            clean = alias.strip()
            normalized = cls._normalize(clean)
            if clean and normalized and normalized not in seen:
                seen.add(normalized)
                output.append(clean)
        return output
