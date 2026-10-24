# -*- coding: utf-8 -*-
"""Incremental shadow ingestion from public Wiki materials into the fact graph."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from . import core
from .fact_extraction import (
    EXTRACTOR_VERSION,
    PROMPT_VERSION,
    extract_document_facts,
)
from .fact_graph import EvidenceGraphIndex, GraphDocument


EXACT_TEXT_SOURCE_KINDS = {"markdown", "data", "web", "code", "text"}


class WorkspaceFactGraphSyncReport(BaseModel):
    """Aggregate shadow ingest result; it never replaces the Wiki ingest status."""

    sources_seen: int = 0
    changed_sources: int = 0
    unchanged_sources: int = 0
    failed_sources: int = 0
    skipped_sources: int = 0
    pruned_sources: int = 0
    entities_retained: int = 0
    relations_written: int = 0
    relations_rejected: int = 0


def open_workspace_fact_graph(workspace_dir: Path) -> EvidenceGraphIndex:
    """Open disposable derived state under the existing private Wiki root."""

    return EvidenceGraphIndex(
        core.wiki_root(workspace_dir) / core.WIKI_FACT_GRAPH_FILE,
    )


def _workspace_graph_documents(workspace_dir: Path) -> list[GraphDocument]:
    core.ensure_wiki_layout(workspace_dir)
    data = core.collect_wiki_data(workspace_dir)
    documents: list[GraphDocument] = []
    for page in data.pages:
        page_path = core.resolve_public_page_path(workspace_dir, page.filename)
        content = core.read_text_safely(page_path)
        if not content.strip():
            continue
        documents.append(
            GraphDocument(
                source_id=f"page:{page.filename}",
                source_path=(
                    page_path.resolve()
                    .relative_to(workspace_dir.resolve())
                    .as_posix()
                ),
                content=content,
                revision=str(page.modified_time or ""),
                trust=0.4,
            ),
        )
    for source in data.sources:
        if source.kind not in EXACT_TEXT_SOURCE_KINDS:
            continue
        source_path = workspace_dir / source.path
        if not source_path.exists() or not source_path.is_file():
            continue
        content = core.read_text_safely(source_path)
        if not content.strip():
            continue
        documents.append(
            GraphDocument(
                source_id=f"source:{source.path}",
                source_path=source.path,
                content=content,
                revision=str(source.modified_time or ""),
                trust=0.25,
            ),
        )
    return documents


async def sync_workspace_fact_graph(
    workspace_dir: Path,
    *,
    agent_id: str,
) -> WorkspaceFactGraphSyncReport:
    """Incrementally extract changed documents; failures do not block Wiki."""

    documents = _workspace_graph_documents(workspace_dir)
    index = open_workspace_fact_graph(workspace_dir)
    report = WorkspaceFactGraphSyncReport(sources_seen=len(documents))

    for document in documents:
        if index.source_is_current(
            document,
            extractor_version=EXTRACTOR_VERSION,
            schema_version=1,
            prompt_version=PROMPT_VERSION,
        ):
            report.unchanged_sources += 1
            continue
        outcome = await extract_document_facts(
            agent_id=agent_id,
            document=document,
        )
        if outcome.chunks_succeeded == 0:
            if outcome.chunks_failed:
                report.failed_sources += 1
            else:
                report.skipped_sources += 1
            continue
        extraction = outcome.extraction.model_copy(
            update={
                "extractor_version": EXTRACTOR_VERSION,
                "schema_version": 1,
                "prompt_version": PROMPT_VERSION,
            },
        )
        source_report = index.sync_document(document, extraction)
        if source_report.changed:
            report.changed_sources += 1
        report.entities_retained += source_report.entities_retained
        report.relations_written += source_report.relations_written
        report.relations_rejected += (
            source_report.relations_rejected + outcome.candidates_rejected
        )

    report.pruned_sources = index.prune_sources(
        {document.source_id for document in documents},
    )
    return report


async def run_wiki_ingest_with_fact_graph(
    workspace_dir: Path,
    *,
    agent_id: str,
    request: core.WikiIngestRunRequest,
) -> dict[str, Any]:
    """Run primary ingest first, then attach non-blocking shadow graph status."""

    result = await asyncio.to_thread(
        core.run_wiki_ingest,
        workspace_dir,
        request,
    )
    if result.get("mode") != "deep":
        return result

    core.append_queue_event(
        workspace_dir,
        {"type": "fact_graph.sync", "status": "running", "mode": "shadow"},
    )
    try:
        graph_report = await sync_workspace_fact_graph(
            workspace_dir,
            agent_id=agent_id,
        )
    except Exception as exc:
        core.append_queue_event(
            workspace_dir,
            {
                "type": "fact_graph.sync",
                "status": "failed",
                "mode": "shadow",
                "error": str(exc),
            },
        )
        result["fact_graph"] = {
            "status": "failed",
            "mode": "shadow",
            "error": str(exc),
        }
        return result

    payload = graph_report.model_dump()
    core.append_queue_event(
        workspace_dir,
        {
            "type": "fact_graph.sync",
            "status": "finished",
            "mode": "shadow",
            **payload,
        },
    )
    result["fact_graph"] = {
        "status": "finished",
        "mode": "shadow",
        **payload,
    }
    return result
