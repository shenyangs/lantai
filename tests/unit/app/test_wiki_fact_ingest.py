# -*- coding: utf-8 -*-
"""End-to-end shadow ingest locks for the derived Wiki fact graph."""

from types import SimpleNamespace

from lantai.app.wiki.fact_extraction import FactExtractionOutcome
from lantai.app.wiki.fact_graph import (
    ExtractedEntity,
    ExtractedRelation,
    GraphExtraction,
)
from lantai.app.wiki.core import WikiIngestRunRequest


async def test_shadow_ingest_skips_unchanged_pages_and_builds_two_hop_graph(
    monkeypatch,
    tmp_path,
):
    """Deep graph work is incremental and remains independent of raw pages."""

    from lantai.app.wiki import core, fact_ingest

    core.ensure_wiki_layout(tmp_path)
    engine_path = core.wiki_pages_dir(tmp_path) / "engine.md"
    leader_path = core.wiki_pages_dir(tmp_path) / "leader.md"
    engine_path.write_text(
        "# Engine\n\nProject Aurora uses Engine Nova.",
        encoding="utf-8",
    )
    leader_path.write_text(
        "# Leadership\n\nLin leads Project Aurora.",
        encoding="utf-8",
    )
    calls = []

    async def fake_extract(*, document, **_kwargs):
        calls.append(document.source_id)
        if "Engine Nova" in document.content:
            extraction = GraphExtraction(
                model_label="test/model",
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
            )
        else:
            extraction = GraphExtraction(
                model_label="test/model",
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
            )
        return FactExtractionOutcome(
            extraction=extraction,
            chunks_total=1,
            chunks_succeeded=1,
            chunks_failed=0,
        )

    monkeypatch.setattr(fact_ingest, "extract_document_facts", fake_extract)

    first = await fact_ingest.sync_workspace_fact_graph(
        tmp_path,
        agent_id="agent-1",
    )
    second = await fact_ingest.sync_workspace_fact_graph(
        tmp_path,
        agent_id="agent-1",
    )

    assert first.changed_sources == 2
    assert first.relations_written == 2
    assert second.changed_sources == 0
    assert second.unchanged_sources == 2
    assert calls == ["page:engine.md", "page:leader.md"]

    index = fact_ingest.open_workspace_fact_graph(tmp_path)
    paths = index.search("Who leads the project using Engine Nova?", max_hops=2)
    assert any(len(path.facts) == 2 for path in paths)
    assert engine_path.read_text(encoding="utf-8").endswith("Engine Nova.")
    assert leader_path.read_text(encoding="utf-8").endswith("Project Aurora.")

    leader_path.unlink()
    pruned = await fact_ingest.sync_workspace_fact_graph(
        tmp_path,
        agent_id="agent-1",
    )

    assert pruned.pruned_sources == 1
    assert pruned.unchanged_sources == 1
    assert not any(
        len(path.facts) == 2
        for path in index.search(
            "Who leads the project using Engine Nova?",
            max_hops=2,
        )
    )
    assert engine_path.read_text(encoding="utf-8").endswith("Engine Nova.")


async def test_deep_ingest_reports_shadow_graph_without_blocking_primary_ingest(
    monkeypatch,
    tmp_path,
):
    """The existing deep ingest result carries graph status on the same run."""

    from lantai.app.wiki import fact_ingest

    async def fake_sync(*_args, **_kwargs):
        return fact_ingest.WorkspaceFactGraphSyncReport(
            sources_seen=2,
            changed_sources=2,
            relations_written=2,
        )

    monkeypatch.setattr(
        fact_ingest,
        "sync_workspace_fact_graph",
        fake_sync,
    )

    result = await fact_ingest.run_wiki_ingest_with_fact_graph(
        tmp_path,
        agent_id="agent-1",
        request=WikiIngestRunRequest(mode="deep"),
    )

    assert result["ok"] is True
    assert result["mode"] == "deep"
    assert result["fact_graph"]["status"] == "finished"
    assert result["fact_graph"]["changed_sources"] == 2
    assert result["fact_graph"]["relations_written"] == 2


async def test_workspace_deep_ingest_route_uses_fact_graph_shadow_runner(
    monkeypatch,
    tmp_path,
):
    """The existing public ingest route, not a second task API, owns the run."""

    from lantai.app.routers import workspace as workspace_router
    from lantai.app.wiki import fact_ingest

    async def fake_agent(_request):
        return SimpleNamespace(agent_id="agent-1", workspace_dir=tmp_path)

    async def fake_run(workspace_dir, *, agent_id, request):
        assert workspace_dir == tmp_path
        assert agent_id == "agent-1"
        assert request.mode == "deep"
        return {
            "ok": True,
            "mode": "deep",
            "fact_graph": {"status": "finished", "relations_written": 2},
        }

    monkeypatch.setattr(
        workspace_router,
        "get_agent_for_request",
        fake_agent,
    )
    monkeypatch.setattr(
        fact_ingest,
        "run_wiki_ingest_with_fact_graph",
        fake_run,
    )

    result = await workspace_router.run_wiki_ingest(
        workspace_router.WikiIngestRunRequest(mode="deep"),
        SimpleNamespace(),
    )

    assert result["fact_graph"]["status"] == "finished"
    assert result["fact_graph"]["relations_written"] == 2
