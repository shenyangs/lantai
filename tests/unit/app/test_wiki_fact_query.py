# -*- coding: utf-8 -*-
"""Wiki query integration locks for hybrid lexical + fact evidence."""

from lantai.app.wiki.fact_graph import (
    ExtractedEntity,
    ExtractedRelation,
    GraphDocument,
    GraphExtraction,
)


def test_wiki_query_adds_rrf_ranked_two_hop_fact_path_as_untrusted_evidence(
    monkeypatch,
    tmp_path,
):
    """Hybrid query returns the path and both exact source citations."""

    from lantai.app.wiki import core
    from lantai.app.wiki.fact_ingest import open_workspace_fact_graph

    core.ensure_wiki_layout(tmp_path)
    engine_path = core.wiki_pages_dir(tmp_path) / "engine.md"
    leader_path = core.wiki_pages_dir(tmp_path) / "leader.md"
    engine_content = "# Engine\n\nProject Aurora uses Engine Nova."
    leader_content = "# Leadership\n\nLin leads Project Aurora."
    engine_path.write_text(engine_content, encoding="utf-8")
    leader_path.write_text(leader_content, encoding="utf-8")

    index = open_workspace_fact_graph(tmp_path)
    index.sync_document(
        GraphDocument(
            source_id="page:engine.md",
            source_path=".wiki/pages/engine.md",
            content=engine_content,
            trust=0.4,
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
    )
    index.sync_document(
        GraphDocument(
            source_id="page:leader.md",
            source_path=".wiki/pages/leader.md",
            content=leader_content,
            trust=0.4,
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
    )

    response = core.build_wiki_query(
        tmp_path,
        core.WikiQueryRequest(
            query="Who leads the project using Engine Nova?",
            limit=8,
        ),
    )

    two_hop = next(path for path in response.fact_paths if path.hops == 2)
    assert len(two_hop.citations) == 2
    assert "<untrusted-wiki-fact-path" in response.context_summary
    assert ".wiki/pages/engine.md#char=" in response.context_summary
    assert ".wiki/pages/leader.md#char=" in response.context_summary
    assert {
        citation.path
        for citation in response.citations
        if citation.kind == "fact"
    } == {".wiki/pages/engine.md", ".wiki/pages/leader.md"}
    assert all(
        citation.span_end > citation.span_start
        for citation in response.citations
        if citation.kind == "fact"
    )

    leader_path.write_text(
        "# Leadership\n\nKai leads Project Aurora.",
        encoding="utf-8",
    )
    stale = core.build_wiki_query(
        tmp_path,
        core.WikiQueryRequest(
            query="Who leads the project using Engine Nova?",
            limit=8,
        ),
    )
    assert not any(path.hops == 2 for path in stale.fact_paths)
    assert not any(
        citation.path == ".wiki/pages/leader.md"
        for citation in stale.citations
        if citation.kind == "fact"
    )

    monkeypatch.setenv("LANTAI_WIKI_FACT_GRAPH_READ", "0")
    lexical_only = core.build_wiki_query(
        tmp_path,
        core.WikiQueryRequest(query="Engine Nova", limit=8),
    )
    assert lexical_only.citations
    assert lexical_only.fact_paths == []
    assert "<untrusted-wiki-fact-path" not in lexical_only.context_summary
