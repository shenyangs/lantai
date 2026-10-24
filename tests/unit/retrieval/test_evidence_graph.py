# -*- coding: utf-8 -*-
"""Behaviour locks for Wiki's source-grounded derived fact graph."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from lantai.app.wiki.fact_graph import (
    EvidenceGraphIndex,
    ExtractedEntity,
    ExtractedRelation,
    GraphDocument,
    GraphExtraction,
)


GOLD_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "retrieval"
    / "knowledge_graph_v1.json"
)


def test_two_hop_query_keeps_each_relation_resolvable_to_original_text(tmp_path):
    """A cross-document path must preserve the evidence for both edges."""

    source_dir = tmp_path / "docs"
    source_dir.mkdir()
    engine_text = "Project Aurora uses Engine Nova for retrieval."
    leader_text = "Lin leads Project Aurora."
    (source_dir / "engine.md").write_text(engine_text, encoding="utf-8")
    (source_dir / "leader.md").write_text(leader_text, encoding="utf-8")

    index = EvidenceGraphIndex(tmp_path / ".wiki" / "evidence-graph.sqlite")
    index.sync_document(
        GraphDocument(
            source_id="doc:engine",
            source_path="docs/engine.md",
            content=engine_text,
            revision="v1",
            trust=0.8,
        ),
        GraphExtraction(
            entities=[
                ExtractedEntity(local_id="project", name="Project Aurora", kind="project"),
                ExtractedEntity(local_id="engine", name="Engine Nova", kind="technology"),
            ],
            relations=[
                ExtractedRelation(
                    subject_id="project",
                    predicate="uses",
                    object_id="engine",
                    evidence_quote=engine_text,
                    confidence=0.95,
                ),
            ],
        ),
    )
    index.sync_document(
        GraphDocument(
            source_id="doc:leader",
            source_path="docs/leader.md",
            content=leader_text,
            revision="v1",
            trust=0.9,
        ),
        GraphExtraction(
            entities=[
                ExtractedEntity(local_id="leader", name="Lin", kind="person"),
                ExtractedEntity(local_id="project", name="Project Aurora", kind="project"),
            ],
            relations=[
                ExtractedRelation(
                    subject_id="leader",
                    predicate="leads",
                    object_id="project",
                    evidence_quote=leader_text,
                    confidence=0.98,
                ),
            ],
        ),
    )

    paths = index.search(
        "Who leads the project using Engine Nova?",
        max_hops=2,
        limit=8,
    )

    two_hop = next(path for path in paths if len(path.facts) == 2)
    assert {
        (fact.subject_name, fact.predicate, fact.object_name)
        for fact in two_hop.facts
    } == {
        ("Project Aurora", "uses", "Engine Nova"),
        ("Lin", "leads", "Project Aurora"),
    }
    assert {
        evidence.source_path
        for fact in two_hop.facts
        for evidence in fact.evidence
    } == {"docs/engine.md", "docs/leader.md"}

    for fact in two_hop.facts:
        evidence = fact.evidence[0]
        original = (tmp_path / evidence.source_path).read_text(encoding="utf-8")
        assert original[evidence.span_start:evidence.span_end] == evidence.quote
        assert evidence.citation == (
            f"{evidence.source_path}#char={evidence.span_start}-{evidence.span_end}"
        )


def test_claim_status_only_contradicts_when_negative_evidence_is_explicit(tmp_path):
    """A missing edge is unknown; only a cited negative edge is contradiction."""

    index = EvidenceGraphIndex(tmp_path / "evidence-graph.sqlite")
    positive_text = "Lin leads Project Aurora."
    index.sync_document(
        GraphDocument(
            source_id="doc:positive",
            source_path="docs/positive.md",
            content=positive_text,
        ),
        GraphExtraction(
            entities=[
                ExtractedEntity(
                    local_id="lin",
                    name="Lin",
                    kind="person",
                    aliases=["person:lin"],
                ),
                ExtractedEntity(local_id="aurora", name="Project Aurora", kind="project"),
            ],
            relations=[
                ExtractedRelation(
                    subject_id="lin",
                    predicate="leads",
                    object_id="aurora",
                    evidence_quote=positive_text,
                ),
            ],
        ),
    )

    assert index.evaluate_claim("Lin", "leads", "Project Aurora").status == "supported"
    assert index.evaluate_claim("Lin", "owns", "Project Aurora").status == "unknown"

    negative_text = "Lin does not lead Project Aurora."
    index.sync_document(
        GraphDocument(
            source_id="doc:negative",
            source_path="docs/negative.md",
            content=negative_text,
        ),
        GraphExtraction(
            entities=[
                ExtractedEntity(
                    local_id="lin",
                    name="Lin",
                    kind="person",
                    aliases=["person:lin"],
                ),
                ExtractedEntity(local_id="aurora", name="Project Aurora", kind="project"),
            ],
            relations=[
                ExtractedRelation(
                    subject_id="lin",
                    predicate="leads",
                    object_id="aurora",
                    polarity="negative",
                    evidence_quote=negative_text,
                ),
            ],
        ),
    )

    assessment = index.evaluate_claim("Lin", "leads", "Project Aurora")
    assert assessment.status == "conflicted"
    assert {fact.polarity for fact in assessment.facts} == {"positive", "negative"}

    index.delete_source("doc:positive")
    negative_only = index.evaluate_claim("Lin", "leads", "Project Aurora")
    assert negative_only.status == "contradicted"
    assert {fact.polarity for fact in negative_only.facts} == {"negative"}


def test_source_revision_replaces_stale_edges_and_delete_never_touches_original(tmp_path):
    """Graph lifecycle operations only mutate the disposable SQLite projection."""

    source_path = tmp_path / "docs" / "project.md"
    source_path.parent.mkdir()
    source_path.write_text("Aurora uses Engine One.", encoding="utf-8")
    index = EvidenceGraphIndex(tmp_path / ".wiki" / "evidence-graph.sqlite")

    first_document = GraphDocument(
        source_id="doc:project",
        source_path="docs/project.md",
        content=source_path.read_text(encoding="utf-8"),
        revision="v1",
    )
    first_extraction = GraphExtraction(
        entities=[
            ExtractedEntity(local_id="project", name="Aurora", kind="project"),
            ExtractedEntity(local_id="engine", name="Engine One", kind="technology"),
        ],
        relations=[
            ExtractedRelation(
                subject_id="project",
                predicate="uses",
                object_id="engine",
                evidence_quote=first_document.content,
            ),
        ],
    )

    first_report = index.sync_document(first_document, first_extraction)
    unchanged_report = index.sync_document(first_document, first_extraction)
    assert first_report.relations_written == 1
    assert first_report.changed is True
    assert unchanged_report.changed is False

    source_path.write_text("Aurora uses Engine Two.", encoding="utf-8")
    second_document = GraphDocument(
        source_id="doc:project",
        source_path="docs/project.md",
        content=source_path.read_text(encoding="utf-8"),
        revision="v2",
    )
    second_extraction = GraphExtraction(
        entities=[
            ExtractedEntity(local_id="project", name="Aurora", kind="project"),
            ExtractedEntity(local_id="engine", name="Engine Two", kind="technology"),
        ],
        relations=[
            ExtractedRelation(
                subject_id="project",
                predicate="uses",
                object_id="engine",
                evidence_quote=second_document.content,
            ),
        ],
    )

    assert index.sync_document(second_document, second_extraction).relations_written == 1
    assert index.evaluate_claim("Aurora", "uses", "Engine One").status == "unknown"
    assert index.evaluate_claim("Aurora", "uses", "Engine Two").status == "supported"

    index.delete_source("doc:project")

    assert index.evaluate_claim("Aurora", "uses", "Engine Two").status == "unknown"
    assert source_path.read_text(encoding="utf-8") == "Aurora uses Engine Two."

    rebuild = index.rebuild([(second_document, second_extraction)])

    assert rebuild.sources_rebuilt == 1
    assert rebuild.relations_written == 1
    assert index.evaluate_claim("Aurora", "uses", "Engine Two").status == "supported"
    assert source_path.read_text(encoding="utf-8") == "Aurora uses Engine Two."


def test_invalid_evidence_rejects_relation_but_retains_unmatched_entities(tmp_path):
    """Extractor mistakes cannot create edges or silently erase entities."""

    index = EvidenceGraphIndex(tmp_path / "evidence-graph.sqlite")
    document = GraphDocument(
        source_id="doc:invalid",
        source_path="docs/invalid.md",
        content="Aurora is discussed without a verified ownership statement.",
    )

    report = index.sync_document(
        document,
        GraphExtraction(
            entities=[
                ExtractedEntity(local_id="aurora", name="Aurora", kind="project"),
                ExtractedEntity(local_id="owner", name="Northwind", kind="organization"),
            ],
            relations=[
                ExtractedRelation(
                    subject_id="owner",
                    predicate="owns",
                    object_id="aurora",
                    evidence_quote="Northwind owns Aurora.",
                ),
            ],
        ),
    )

    assert report.entities_retained == 2
    assert report.relations_written == 0
    assert report.relations_rejected == 1
    assert index.stats().entities == 2
    assert index.evaluate_claim("Northwind", "owns", "Aurora").status == "unknown"
    audit = index.source_audit("doc:invalid")
    assert audit.runs[0].status == "completed"
    assert audit.runs[0].relations_rejected == 1
    assert len(audit.resolution_decisions) == 2
    assert {
        decision.decision
        for decision in audit.resolution_decisions
    } == {"created_singleton"}


def test_versioned_gold_fixture_covers_chinese_and_english_alias_two_hop_queries(tmp_path):
    """The graph gold set must require a real relation chain, not shared keywords."""

    fixture = json.loads(GOLD_FIXTURE.read_text(encoding="utf-8"))
    assert fixture["suite_id"] == "knowledge_graph_v1"
    assert fixture["version"] == 1

    index = EvidenceGraphIndex(tmp_path / ".wiki" / "evidence-graph.sqlite")
    for item in fixture["documents"]:
        document = GraphDocument.model_validate(item["document"])
        extraction = GraphExtraction.model_validate(item["extraction"])
        source_path = tmp_path / document.source_path
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(document.content, encoding="utf-8")
        index.sync_document(document, extraction)

    for case in fixture["cases"]:
        paths = index.search(case["query"], max_hops=2, limit=8)
        path = next(item for item in paths if len(item.facts) == case["expected_hops"])
        citations = {
            evidence.source_path
            for fact in path.facts
            for evidence in fact.evidence
        }
        assert citations == set(case["expected_citations"]), case["id"]


def test_same_alias_with_different_entity_kinds_stays_ambiguous(tmp_path):
    """Conservative resolution keeps a false-merge canary as two entities."""

    index = EvidenceGraphIndex(tmp_path / "evidence-graph.sqlite")
    cases = [
        (
            "doc:project",
            "Aurora uses Engine Nova.",
            [
                ExtractedEntity(local_id="aurora", name="Aurora", kind="project"),
                ExtractedEntity(local_id="engine", name="Engine Nova", kind="technology"),
            ],
            ExtractedRelation(
                subject_id="aurora",
                predicate="uses",
                object_id="engine",
                evidence_quote="Aurora uses Engine Nova.",
            ),
        ),
        (
            "doc:company",
            "Aurora acquired Northwind.",
            [
                ExtractedEntity(local_id="aurora", name="Aurora", kind="organization"),
                ExtractedEntity(local_id="target", name="Northwind", kind="organization"),
            ],
            ExtractedRelation(
                subject_id="aurora",
                predicate="acquired",
                object_id="target",
                evidence_quote="Aurora acquired Northwind.",
            ),
        ),
    ]
    for source_id, content, entities, relation in cases:
        index.sync_document(
            GraphDocument(
                source_id=source_id,
                source_path=f"docs/{source_id.removeprefix('doc:')}.md",
                content=content,
            ),
            GraphExtraction(entities=entities, relations=[relation]),
        )

    paths = index.search("Aurora", max_hops=1, limit=8)
    assert len({path.seed_entity_ids[0] for path in paths}) == 2
    assert index.evaluate_claim("Aurora", "uses", "Engine Nova").status == "unknown"


def test_same_person_name_without_shared_alias_does_not_auto_merge(tmp_path):
    """A collision-prone name needs stronger identity evidence across sources."""

    index = EvidenceGraphIndex(tmp_path / "evidence-graph.sqlite")
    cases = [
        (
            "doc:lead",
            "Alex leads Aurora.",
            ExtractedEntity(local_id="alex", name="Alex", kind="person"),
            ExtractedEntity(local_id="target", name="Aurora", kind="project"),
            "leads",
        ),
        (
            "doc:found",
            "Alex founded Northwind.",
            ExtractedEntity(local_id="alex", name="Alex", kind="person"),
            ExtractedEntity(local_id="target", name="Northwind", kind="organization"),
            "founded",
        ),
    ]
    for source_id, content, person, target, predicate in cases:
        index.sync_document(
            GraphDocument(
                source_id=source_id,
                source_path=f"docs/{source_id.removeprefix('doc:')}.md",
                content=content,
            ),
            GraphExtraction(
                entities=[person, target],
                relations=[
                    ExtractedRelation(
                        subject_id="alex",
                        predicate=predicate,
                        object_id="target",
                        evidence_quote=content,
                    ),
                ],
            ),
        )

    paths = index.search("Alex", max_hops=1, limit=8)
    assert len({path.seed_entity_ids[0] for path in paths}) == 2


def test_same_fact_keeps_independent_evidence_from_multiple_sources(tmp_path):
    """Relation dedupe must never discard corroborating source citations."""

    index = EvidenceGraphIndex(tmp_path / "evidence-graph.sqlite")
    for suffix, text in (
        ("one", "The release plan names Kai as owner."),
        ("two", "Kai is the owner of the release plan."),
    ):
        index.sync_document(
            GraphDocument(
                source_id=f"doc:{suffix}",
                source_path=f"docs/{suffix}.md",
                content=text,
            ),
            GraphExtraction(
                entities=[
                    ExtractedEntity(
                        local_id="kai",
                        name="Kai",
                        kind="person",
                        aliases=["person:kai"],
                    ),
                    ExtractedEntity(
                        local_id="plan",
                        name="Release Plan",
                        kind="project",
                    ),
                ],
                relations=[
                    ExtractedRelation(
                        subject_id="kai",
                        predicate="owns",
                        object_id="plan",
                        evidence_quote=text,
                    ),
                ],
            ),
        )

    assessment = index.evaluate_claim("Kai", "owns", "Release Plan")
    assert assessment.status == "supported"
    assert len(assessment.facts) == 1
    assert {
        evidence.source_path
        for evidence in assessment.facts[0].evidence
    } == {"docs/one.md", "docs/two.md"}


def test_graph_document_rejects_evidence_paths_outside_workspace_namespace():
    """Derived citations never accept absolute or parent-traversal paths."""

    for unsafe_path in ("../secret.md", "/tmp/secret.md"):
        with pytest.raises(ValidationError):
            GraphDocument(
                source_id="doc:unsafe",
                source_path=unsafe_path,
                content="unsafe",
            )
