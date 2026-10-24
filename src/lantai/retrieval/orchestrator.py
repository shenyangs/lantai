# -*- coding: utf-8 -*-
"""Fuse independent Wiki/Memory retrieval sources without merging their facts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence

from .local_index import RetrievalHit


@dataclass(frozen=True)
class RetrievalCandidate:
    identifier: str
    source: str
    content: str
    citation: str
    rank: int
    score: float
    trust: float
    diversity_key: str = ""
    citations: tuple[str, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)


class RetrievalAdapter(Protocol):
    name: str

    def search(self, query: str, *, limit: int) -> Sequence[RetrievalCandidate]: ...


class NativeIndexAdapter:
    """Expose a local index as a source-specific retrieval adapter."""

    def __init__(self, name: str, index, allowed_kinds: set[str] | None = None) -> None:
        self.name = name
        self.index = index
        self.allowed_kinds = allowed_kinds or set()

    def search(self, query: str, *, limit: int) -> Sequence[RetrievalCandidate]:
        hits: list[RetrievalHit] = self.index.search(query, limit=limit * 3)
        candidates = []
        for rank, hit in enumerate(hits, start=1):
            if self.allowed_kinds and hit.source_kind not in self.allowed_kinds:
                continue
            candidates.append(
                RetrievalCandidate(
                    identifier=hit.chunk_id, source=self.name, content=hit.content,
                    citation=hit.citation, rank=rank, score=hit.score, trust=hit.trust,
                    diversity_key=hit.source_id,
                    citations=(hit.citation,) if hit.citation else (),
                    metadata={
                        "raw_score": hit.score,
                        "retrieval_source": self.name,
                        "source_kind": hit.source_kind,
                        "source_id": hit.source_id,
                        "source_revision": hit.source_revision,
                        "source_text_hash": hit.source_text_hash,
                        "source_start": hit.source_start,
                        "source_end": hit.source_end,
                        "source_position_unit": "unicode_codepoint",
                        "title": hit.title,
                        **(
                            {"semantic_score": hit.score}
                            if "vector" in self.name.lower()
                            else {}
                        ),
                    },
                ),
            )
            if len(candidates) >= limit:
                break
        return candidates


class LegacyRetrievalAdapter:
    """Adapter for ReMe/older stores while native indexing is rolled out."""

    def __init__(self, name: str, lookup) -> None:
        self.name = name
        self._lookup = lookup

    def search(self, query: str, *, limit: int) -> Sequence[RetrievalCandidate]:
        return list(self._lookup(query, limit=limit))


class RetrievalOrchestrator:
    """RRF fusion with trust-aware ranking and source diversity."""

    def __init__(self, adapters: Sequence[RetrievalAdapter], *, rrf_k: int = 60) -> None:
        self.adapters = list(adapters)
        self.rrf_k = max(1, rrf_k)

    def search(self, query: str, *, limit: int = 8) -> list[RetrievalCandidate]:
        fused: dict[str, tuple[float, RetrievalCandidate]] = {}
        for adapter in self.adapters:
            for candidate in adapter.search(query, limit=limit):
                key = (
                    candidate.identifier
                    if (
                        len(candidate.citations) > 1
                        or candidate.metadata.get("dedupe_by_identifier") is True
                    )
                    else candidate.citation or candidate.identifier
                )
                contribution = (1 / (self.rrf_k + candidate.rank)) * max(0.05, candidate.trust)
                prior = fused.get(key)
                if prior is None:
                    fused[key] = (contribution, candidate)
                else:
                    retained = prior[1]
                    metadata = dict(retained.metadata)
                    sources = set(metadata.get("retrieval_sources") or ())
                    sources.add(retained.source)
                    sources.add(candidate.source)
                    metadata["retrieval_sources"] = tuple(sorted(sources))
                    if "semantic_score" in candidate.metadata:
                        metadata["semantic_score"] = max(
                            float(metadata.get("semantic_score") or -1.0),
                            float(candidate.metadata["semantic_score"]),
                        )
                    fused[key] = (
                        prior[0] + contribution,
                        RetrievalCandidate(**{**retained.__dict__, "metadata": metadata}),
                    )
        ranked = sorted(fused.values(), key=lambda item: item[0], reverse=True)
        output: list[RetrievalCandidate] = []
        seen_diversity: set[str] = set()
        for score, candidate in ranked:
            diversity = candidate.diversity_key or candidate.identifier
            if diversity in seen_diversity:
                continue
            seen_diversity.add(diversity)
            output.append(
                RetrievalCandidate(
                    **{**candidate.__dict__, "score": score},
                ),
            )
            if len(output) >= limit:
                break
        return output
