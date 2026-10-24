# -*- coding: utf-8 -*-
"""Adapter from evidence-bearing Wiki fact paths to RRF candidates."""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from ...retrieval.orchestrator import RetrievalCandidate
from .fact_graph import EvidenceGraphIndex, GraphFact, GraphPath


def _path_citations(path: GraphPath) -> tuple[str, ...]:
    citations: list[str] = []
    for fact in path.facts:
        for evidence in fact.evidence[:2]:
            if evidence.citation not in citations:
                citations.append(evidence.citation)
    return tuple(citations)


def _path_content(path: GraphPath) -> str:
    lines: list[str] = []
    citation_number = 0
    for fact in path.facts:
        evidence = fact.evidence[0]
        citation_number += 1
        predicate = (
            fact.predicate
            if fact.polarity == "positive"
            else f"NOT {fact.predicate}"
        )
        lines.append(
            f"{fact.subject_name} --{predicate}--> {fact.object_name} "
            f"[evidence {citation_number}: {evidence.citation}]",
        )
        lines.append(f'Quote: "{evidence.quote}"')
    return "\n".join(lines)


class FactGraphRetrievalAdapter:
    """Expose bounded graph paths at the existing RetrievalAdapter seam."""

    name = "wiki_fact_graph"

    def __init__(
        self,
        index: EvidenceGraphIndex,
        *,
        max_hops: int = 2,
        evidence_loader: Callable[[str], str] | None = None,
    ) -> None:
        self.index = index
        self.max_hops = max(1, min(max_hops, 3))
        self.evidence_loader = evidence_loader

    def _verified_path(self, path: GraphPath) -> GraphPath | None:
        if self.evidence_loader is None:
            return path
        verified_facts: list[GraphFact] = []
        for fact in path.facts:
            verified_evidence = []
            for evidence in fact.evidence:
                try:
                    content = self.evidence_loader(evidence.source_path)
                except Exception:
                    continue
                content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                if content_hash != evidence.source_sha256:
                    continue
                if content[evidence.span_start:evidence.span_end] != evidence.quote:
                    continue
                verified_evidence.append(evidence)
            if not verified_evidence:
                return None
            verified_facts.append(
                fact.model_copy(update={"evidence": verified_evidence}),
            )
        return path.model_copy(update={"facts": verified_facts})

    def search(
        self,
        query: str,
        *,
        limit: int,
    ) -> list[RetrievalCandidate]:
        paths = self.index.search(
            query,
            max_hops=self.max_hops,
            limit=limit,
        )
        candidates: list[RetrievalCandidate] = []
        for rank, path in enumerate(paths, start=1):
            path = self._verified_path(path)
            if path is None:
                continue
            citations = _path_citations(path)
            if not citations:
                continue
            trusts = [
                evidence.trust
                for fact in path.facts
                for evidence in fact.evidence[:1]
            ]
            relation_key = ":".join(fact.relation_id for fact in path.facts)
            candidates.append(
                RetrievalCandidate(
                    identifier=f"fact-path:{relation_key}",
                    source=self.name,
                    content=_path_content(path),
                    citation=citations[0],
                    citations=citations,
                    rank=rank,
                    score=path.score,
                    trust=sum(trusts) / len(trusts) if trusts else 0.0,
                    diversity_key=relation_key,
                    metadata={
                        "hops": len(path.facts),
                        "dedupe_by_identifier": True,
                    },
                ),
            )
        return candidates
