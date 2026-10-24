# -*- coding: utf-8 -*-
"""RRF Adapter locks for multi-citation fact paths."""

from lantai.app.wiki.fact_graph import (
    EvidenceGraphIndex,
    ExtractedEntity,
    ExtractedRelation,
    GraphDocument,
    GraphExtraction,
)
from lantai.retrieval import RetrievalCandidate, RetrievalOrchestrator


class _LexicalAdapter:
    name = "wiki_lexical"

    def search(self, _query, *, limit):
        return [
            RetrievalCandidate(
                identifier="lexical:1",
                source=self.name,
                content="Engine Nova is mentioned.",
                citation="docs/engine.md",
                rank=1,
                score=1.0,
                trust=0.4,
                diversity_key="docs/engine.md",
            ),
        ][:limit]


def test_rrf_graph_adapter_preserves_every_edge_citation(tmp_path):
    """Fusing a graph path must not collapse its two original citations."""

    from lantai.app.wiki.fact_retrieval import FactGraphRetrievalAdapter

    index = EvidenceGraphIndex(tmp_path / "evidence-graph.sqlite")
    documents = [
        (
            GraphDocument(
                source_id="doc:engine",
                source_path="docs/engine.md",
                content="Project Aurora uses Engine Nova.",
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
                        evidence_quote="Project Aurora uses Engine Nova.",
                    ),
                ],
            ),
        ),
        (
            GraphDocument(
                source_id="doc:leader",
                source_path="docs/leader.md",
                content="Lin leads Project Aurora.",
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
                        evidence_quote="Lin leads Project Aurora.",
                    ),
                ],
            ),
        ),
    ]
    for document, extraction in documents:
        index.sync_document(document, extraction)

    hits = RetrievalOrchestrator(
        [_LexicalAdapter(), FactGraphRetrievalAdapter(index)],
    ).search("Who leads the project using Engine Nova?", limit=8)

    graph_hit = next(hit for hit in hits if hit.source == "wiki_fact_graph")
    assert len(graph_hit.citations) == 2
    assert graph_hit.citations[0].startswith("docs/")
    assert graph_hit.citations[1].startswith("docs/")
    assert "Project Aurora --uses--> Engine Nova" in graph_hit.content
    assert "Lin --leads--> Project Aurora" in graph_hit.content
