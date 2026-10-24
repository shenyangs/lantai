# -*- coding: utf-8 -*-

from lantai.retrieval import RetrievalCandidate, RetrievalOrchestrator


class _FakeAdapter:
    def __init__(self, name, candidates):
        self.name, self.candidates = name, candidates

    def search(self, _query, *, limit):
        return self.candidates[:limit]


def test_rrf_fuses_sources_and_deduplicates_same_citation():
    wiki = _FakeAdapter("wiki", [
        RetrievalCandidate("w1", "wiki", "wiki evidence", "doc#a", 1, 1, .3, "doc"),
        RetrievalCandidate("w2", "wiki", "duplicate", "doc#a", 2, .5, .3, "doc"),
    ])
    memory = _FakeAdapter("memory", [
        RetrievalCandidate("m1", "memory", "confirmed fact", "memory#1", 1, 1, 1, "memory"),
    ])

    hits = RetrievalOrchestrator([wiki, memory]).search("evidence", limit=4)

    assert [hit.citation for hit in hits] == ["memory#1", "doc#a"]
    assert hits[0].source == "memory"
