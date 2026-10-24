"""Agent-scoped local research evidence APIs; no implicit external searches."""
import asyncio
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, ConfigDict
from typing import Literal
from ...research.projects import ResearchService, RevisionConflict
from ...research.review import configured_review
from ...research.library import LibraryService
from ...research.zotero import import_local_zotero
from ...research.citations import format_citations
from ...research.fulltext import import_open_fulltext
from ...research.exports import export_report, export_file
from starlette.responses import FileResponse

from ..agent_context import get_agent_for_request
from ...research.evidence import Claim, create_packet, audit_packet, load_record

router = APIRouter(prefix='/workspace/research', tags=['research'])


class PacketRequest(BaseModel):
    source_paths: list[str] = Field(min_length=1, max_length=20)


class AuditRequest(BaseModel):
    packet_id: str = Field(pattern=r'^packet-[0-9a-f]{32}$')
    claims: list[Claim] = Field(max_length=200)
    open_issues: list[str] = Field(default_factory=list, max_length=100)


async def _local(request: Request, function, *args) -> dict[str, Any]:
    workspace = await get_agent_for_request(request)
    try:
        return await asyncio.to_thread(function, workspace.workspace_dir, *args)
    except FileNotFoundError as exc:
        raise HTTPException(404, 'Research record or source not found') from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post('/packets')
async def packet(body: PacketRequest, request: Request):
    return await _local(request, create_packet, body.source_paths)


@router.post('/audits')
async def audit(body: AuditRequest, request: Request):
    return await _local(request, audit_packet, body.packet_id,
                        [claim.model_dump() for claim in body.claims], body.open_issues)


@router.get('/records/{record_id}')
async def record(record_id: str, request: Request):
    return await _local(request, load_record, record_id)


class ProjectRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    question: str = Field(min_length=1, max_length=5000)
    source_paths: list[str] = Field(default_factory=list)
    subquestions: list[str] | None = Field(default=None, max_length=100)
    outline: list[str] | None = Field(default=None, max_length=100)
    budget: dict[str, Any] | None = None
    source_levels: dict[str, Literal['fulltext', 'abstract', 'excerpt']] | None = None
    local_ocr: bool | None = None
    query_preview_required: bool = False
    policy: Literal['local_sources', 'public_search', 'offline'] = 'local_sources'
    scope: str = Field(default='', max_length=5000)
    exclusions: str = Field(default='', max_length=5000)
    idempotency_key: str = Field(min_length=1, max_length=160)


class RevisionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=1)
    question: str | None = Field(default=None, min_length=1, max_length=5000)
    source_paths: list[str] | None = Field(default=None)
    subquestions: list[str] | None = Field(default=None, max_length=100)
    outline: list[str] | None = Field(default=None, max_length=100)
    budget: dict[str, Any] | None = None
    source_levels: dict[str, Literal['fulltext', 'abstract', 'excerpt']] | None = None
    local_ocr: bool | None = None
    query_preview_required: bool | None = None
    policy: Literal['local_sources', 'public_search', 'offline'] | None = None
    refresh_sources: bool = False
    scope: str | None = Field(default=None, max_length=5000)
    exclusions: str | None = Field(default=None, max_length=5000)
    idempotency_key: str = Field(min_length=1, max_length=160)


class BibliographyAdoptionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=160)


class ProjectLifecycleRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=1)


class EvidenceSpanRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_version_id: str
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, gt=0)
    quote: str = Field(min_length=1, max_length=20000)


class ReportClaimRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, gt=0)
    text: str | None = Field(default=None, min_length=1, max_length=200000)
    evidence: list[EvidenceSpanRequest] = Field(default_factory=list, max_length=50)


class ReportRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=200000)
    claims: list[ReportClaimRequest] = Field(default_factory=list, max_length=500)
    expected_report_id: str | None = None
    idempotency_key: str = Field(min_length=1, max_length=160)


class FigureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1, strict=True)
    data: str = Field(min_length=1, max_length=8388608)
    idempotency_key: str = Field(min_length=1, max_length=150)


class ReadRequest(BaseModel):
    revision: int = Field(ge=1)
    source_version_id: str
    first_block: int = Field(default=0, ge=0)
    block_count: int = Field(default=1, ge=1, le=4)


class HumanReviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1, strict=True)
    report_id: str
    expected_review_id: str | None
    text: str = Field(min_length=1, max_length=10000)
    verdict: Literal['agree', 'disagree', 'question']
    anchor: dict[str, Any] | None = None
    idempotency_key: str = Field(min_length=1, max_length=140)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1)
    report_id: str


async def _service_call(request, method, *args, **kwargs):
    return await _domain_call(request, ResearchService, method, *args, **kwargs)


async def _domain_call(request, domain, method, *args, **kwargs):
    workspace = await get_agent_for_request(request)
    try:
        service = await asyncio.to_thread(domain, workspace.workspace_dir)
        return await asyncio.to_thread(getattr(service, method), *args, **kwargs)
    except RevisionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, 'Research record not found in this Agent workspace') from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


class LibraryImportRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    format: Literal['csl-json', 'bibtex', 'ris']
    text: str = Field(min_length=1, max_length=5000000)


class LiteratureSearchRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    project_id: str
    revision: int = Field(ge=1)
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=5, ge=1, le=20)
    query_mode: Literal['general', 'title_author'] = 'general'
    author: str = Field(default='', max_length=200)


class QueryDecisionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    approve: bool


class CandidateImportRequest(BaseModel):
    search_id: str
    index: int = Field(ge=0)


class LibraryVersionRequest(BaseModel):
    version_id: str
    expected_version_id: str


class LibraryOrganizationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=0, strict=True)
    tags: list[str] = Field(max_length=100)
    collections: list[str] = Field(max_length=100)


class LibraryNoteRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = Field(min_length=1, max_length=100000)
    anchor: dict[str, Any] | None = None
    region_anchor: dict[str, Any] | None = None


class NoteSourceRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    project_id: str
    revision: int = Field(ge=1, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=120)


class SourceLinkRequest(BaseModel):
    source_version_id: str
    version_id: str | None = None


class BibliographyRequest(BaseModel):
    work_ids: list[str] = Field(min_length=1, max_length=1000)
    style: Literal['apa', 'numeric', 'gb7714'] = 'gb7714'


class FulltextRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    project_id: str
    revision: int = Field(ge=1)
    location_index: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=160)


async def _until_disconnect(request, awaitable):
    task = asyncio.create_task(awaitable)
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=0.3)
            if await request.is_disconnected():
                task.cancel()
                raise asyncio.CancelledError('Research request disconnected')
        return await task
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1)
    report_id: str
    format: Literal['markdown', 'docx', 'pdf'] = 'markdown'
    style: Literal['apa', 'numeric', 'gb7714'] = 'gb7714'
    idempotency_key: str = Field(min_length=1, max_length=150)


@router.post('/projects/{project_id}/exports')
async def export(project_id: str, body: ExportRequest, request: Request):
    workspace = await get_agent_for_request(request)
    try:
        service = await asyncio.to_thread(ResearchService, workspace.workspace_dir)
        return await asyncio.to_thread(export_report, service, project_id, body.revision,
            body.report_id, body.format, body.style, body.idempotency_key)
    except RevisionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, 'Research report not found') from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get('/exports/{export_id}/file')
async def download_export(export_id: str, request: Request):
    workspace = await get_agent_for_request(request)
    try:
        service = await asyncio.to_thread(ResearchService, workspace.workspace_dir)
        path = await asyncio.to_thread(export_file, service, export_id)
        record = await asyncio.to_thread(service.store.get, export_id, kind='export')
        return FileResponse(path, filename=record['filename'], media_type=record['media_type'])
    except FileNotFoundError as exc:
        raise HTTPException(404, 'Research export not found') from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post('/library/works/{work_id}/fulltext')
async def fulltext(work_id: str, body: FulltextRequest, request: Request):
    workspace = await get_agent_for_request(request)
    try:
        research = await asyncio.to_thread(ResearchService, workspace.workspace_dir)
        library = await asyncio.to_thread(LibraryService, workspace.workspace_dir)
        return await _until_disconnect(request, import_open_fulltext(research, library, body.project_id,
            body.revision, work_id, body.location_index, body.idempotency_key, workspace.agent_id))
    except RevisionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, 'Research work not found') from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get('/library')
async def library(request: Request):
    return {'items': await _domain_call(request, LibraryService, 'list')}


@router.post('/library/import')
async def import_library(body: LibraryImportRequest, request: Request):
    return await _domain_call(request, LibraryService, 'import_text', body.text, body.format)


@router.post('/library/zotero')
async def zotero(request: Request):
    workspace = await get_agent_for_request(request)
    library = await asyncio.to_thread(LibraryService, workspace.workspace_dir)
    return await asyncio.to_thread(import_local_zotero, library)


@router.post('/library/search')
async def search_library(body: LiteratureSearchRequest, request: Request):
    return await _domain_call(request, LibraryService, 'search', **body.model_dump())


@router.get('/library/searches/{search_id}')
async def search_record(search_id: str, request: Request):
    return await _domain_call(request, LibraryService, 'search_record', search_id)


@router.get('/library/searches/{search_id}/{source}/{capability}')
async def discovery_result(search_id: str, source: str, capability: str, request: Request):
    return await _domain_call(request, LibraryService, 'discovery_result', search_id, source, capability)


@router.get('/projects/{project_id}/query-previews')
async def query_previews(project_id: str, request: Request):
    return await _domain_call(request, LibraryService, 'query_previews', project_id)


@router.post('/library/query-previews/{preview_id}/decision')
async def decide_query(preview_id: str, body: QueryDecisionRequest, request: Request):
    return await _domain_call(request, LibraryService, 'decide_query', preview_id, body.approve)


@router.post('/library/candidates')
async def candidate(body: CandidateImportRequest, request: Request):
    return await _domain_call(request, LibraryService, 'import_candidate', body.search_id, body.index)


@router.get('/library/export/{format}')
async def export_library(format: Literal['csl-json', 'bibtex', 'ris'], request: Request):
    return {'format': format, 'text': await _domain_call(request, LibraryService, 'export', format)}


@router.post('/library/bibliography')
async def bibliography(body: BibliographyRequest, request: Request):
    works = [await _domain_call(request, LibraryService, 'work', identifier) for identifier in body.work_ids]
    items = [{**w['csl'], 'id': w['selected_version_id']} for w in works]
    try:
        return await asyncio.to_thread(format_citations, items, [[{'id': i['id']}] for i in items], body.style)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get('/library/works/{work_id}')
async def work_detail(work_id: str, request: Request):
    return await _domain_call(request, LibraryService, 'work', work_id)


@router.post('/library/works/{work_id}/version')
async def choose_version(work_id: str, body: LibraryVersionRequest, request: Request):
    return await _domain_call(request, LibraryService, 'select_version', work_id, body.version_id, body.expected_version_id)


@router.post('/library/works/{work_id}/organization')
async def organize_work(work_id: str, body: LibraryOrganizationRequest, request: Request):
    return await _domain_call(request, LibraryService, 'organize', work_id, **body.model_dump())


@router.post('/library/works/{work_id}/notes')
async def note(work_id: str, body: LibraryNoteRequest, request: Request):
    return await _domain_call(request, LibraryService, 'annotate', work_id, body.text, origin={'provider': 'user'}, anchor=body.anchor, region_anchor=body.region_anchor)


@router.post('/library/notes/{annotation_id}/source')
async def note_source(annotation_id: str, body: NoteSourceRequest, request: Request):
    return await _domain_call(request, LibraryService, 'add_note_source', annotation_id,
                              body.project_id, body.revision, idempotency_key=body.idempotency_key)


@router.post('/library/works/{work_id}/sources')
async def link_source(work_id: str, body: SourceLinkRequest, request: Request):
    return await _domain_call(request, LibraryService, 'link_source', work_id, body.source_version_id, body.version_id)


@router.post('/projects')
async def create_project(body: ProjectRequest, request: Request):
    return await _service_call(request, 'create_project', **body.model_dump())


@router.get('/projects')
async def projects(request: Request, archived: bool = False):
    return {'items': await _service_call(request, 'list_projects', archived),
            'pending_deletions': await _service_call(request, 'pending_deletions')}


@router.post('/projects/{project_id}/archive')
async def archive_project(project_id: str, body: ProjectLifecycleRequest, request: Request):
    return await _service_call(request, 'archive_project', project_id, body.expected_revision)


@router.post('/projects/{project_id}/restore')
async def restore_project(project_id: str, body: ProjectLifecycleRequest, request: Request):
    return await _service_call(request, 'restore_project', project_id, body.expected_revision)


@router.post('/projects/{project_id}/delete')
async def delete_project(project_id: str, body: ProjectLifecycleRequest, request: Request):
    return await _service_call(request, 'delete_project', project_id, body.expected_revision)


@router.get('/projects/{project_id}')
async def project_detail(project_id: str, request: Request):
    return await _service_call(request, 'evidence', project_id)


@router.get('/projects/{project_id}/estimate')
async def project_estimate(project_id: str, revision: int, request: Request):
    return await _service_call(request, 'estimate', project_id, revision)


@router.post('/projects/{project_id}/revisions')
async def revise_project(project_id: str, body: RevisionRequest, request: Request):
    return await _service_call(request, 'revise_project', project_id, **body.model_dump())


@router.post('/projects/{project_id}/bibliography')
async def adopt_bibliography(project_id: str, body: BibliographyAdoptionRequest, request: Request):
    return await _service_call(request, 'adopt_bibliography', project_id, **body.model_dump())


@router.post('/projects/{project_id}/read')
async def read_source(project_id: str, body: ReadRequest, request: Request):
    return await _service_call(request, 'read_source', project_id, body.revision,
                               body.source_version_id, body.first_block, body.block_count)


@router.post('/projects/{project_id}/figures')
async def upload_figure(project_id: str, body: FigureRequest, request: Request):
    from ...research.figures import FigureService
    return await _domain_call(request, FigureService, 'upload', project_id, **body.model_dump())


@router.post('/projects/{project_id}/reports')
async def save_report(project_id: str, body: ReportRequest, request: Request):
    return await _service_call(request, 'save_report', project_id, **body.model_dump())


@router.get('/sources/{source_id}')
async def source_version(source_id: str, request: Request):
    return await _service_call(request, 'source', source_id)


@router.get('/sources/{source_id}/pages/{page_number}')
async def source_page(source_id: str, page_number: int, request: Request):
    return await _service_call(request, 'source_page', source_id, page_number)


@router.get('/sources/{source_id}/pages/{page_number}/tables')
async def source_tables(source_id: str, page_number: int, request: Request):
    return await _service_call(request, 'source_tables', source_id, page_number)


@router.get('/reports/{report_id}')
async def report_version(report_id: str, request: Request):
    return await _service_call(request, 'report', report_id)


@router.post('/projects/{project_id}/human-reviews')
async def human_review(project_id: str, body: HumanReviewRequest, request: Request):
    return await _service_call(request, 'add_human_review', project_id, **body.model_dump())


@router.post('/projects/{project_id}/reviews')
async def review(project_id: str, body: ReviewRequest, request: Request):
    workspace = await get_agent_for_request(request)
    try:
        service = await asyncio.to_thread(ResearchService, workspace.workspace_dir)
        return await _until_disconnect(request, configured_review(service, project_id, body.revision, body.report_id, workspace.agent_id))
    except RevisionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
