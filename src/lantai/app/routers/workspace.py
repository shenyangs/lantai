# -*- coding: utf-8 -*-
"""Workspace API – download / upload the entire WORKING_DIR as a zip.

Also includes agent file management, language settings, audio/transcription
configuration, running config, and system prompt files.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import shutil
import tempfile
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, UploadFile, File, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from apscheduler.triggers.cron import CronTrigger
from starlette.background import BackgroundTask

from ..wiki import core as wiki_core
from ..utils import schedule_agent_reload
from ...config import (
    load_config,
    save_config,
    AgentsRunningConfig,
)
from ...config.config import load_agent_config, save_agent_config
from ...agents.memory.agent_md_manager import AgentMdManager
from ...agents.memory.layered_memory import LayeredMemoryStore
from ...agents.memory.canonical_ledger import CanonicalMemoryLedger
from ...agents.memory.dreaming import MemoryDreamingService
from ...agents.memory.maintenance import (
    clear_all_long_term_memory,
    memory_clear_preview,
)
from ...agents.memory.memory_import import (
    MemoryImportConflictError,
    MemoryImportError,
    MemoryImportNotFoundError,
    commit_memory_import,
    get_memory_import_prompt,
    get_recent_memory_import,
    preview_memory_import,
    undo_memory_import,
)
from ...agents.memory.memory_pack import (
    MEMORY_PACK_MAX_TOTAL_BYTES,
    MemoryPackError,
    adopt_memory_pack,
    export_memory_pack,
    inspect_memory_pack,
)
from ...agents.memory.migration import migrate_memory_governance
from ...agents.memory.structured_user_memory import (
    MEMORY_TYPES,
    StructuredUserMemoryStore,
    infer_memory_type,
)
from ...agents.persona.opportunity_queue import OpportunityStore
from ...agents.persona.user_understanding import UserUnderstandingStore
from ...agents.templates import get_workspace_md_template_id
from ...agents.utils import copy_workspace_md_files
from ...agents.utils.file_handling import read_text_file_with_encoding_fallback
from ...constant import BUILTIN_QA_AGENT_ID, SUPPORTED_AGENT_LANGUAGES
from ...config.config import AGENT_INTERNAL_WORKSPACE_MD_FILES
from ..agent_context import get_agent_for_request
from ..reminders.repo import ReminderRepository
from ..reminders.service import ReminderService


router = APIRouter(prefix="/workspace", tags=["workspace"])

INTERNAL_WORKSPACE_MD_FILES = {
    filename.upper() for filename in AGENT_INTERNAL_WORKSPACE_MD_FILES
}

WIKI_INTERNAL_DIR = ".wiki"
LANTAI_INTERNAL_DIR = ".lantai"
WIKI_SOURCES_DIR = "sources"
WIKI_PURPOSE_FILE = "purpose.md"
WIKI_TEXT_SOURCE_EXTENSIONS = {
    ".md",
    ".markdown",
    ".txt",
    ".csv",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".xml",
    ".html",
    ".htm",
    ".log",
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".css",
    ".less",
}
WIKI_LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
FRONTMATTER_FIELD_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*?)\s*$")
MARKDOWN_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
WIKI_SOURCE_FRONTMATTER_RE = re.compile(r"(?m)^source:\s*[\"']?(.+?)[\"']?\s*$")
MAX_WIKI_TEXT_SOURCE_CHARS = 80_000
DEFAULT_WIKI_PURPOSE = """# 知识库目的

## 目标
- 把上传资料里的长期知识沉淀下来。
- 让重要事实都能追溯到原始资料。
- 把反复出现的研究任务整理成可维护的知识页。

## 关键问题
- 这个知识库要帮助智能体回答哪些问题？
- 哪些资料类型更权威、更应该优先引用？
- 后续导入资料时要持续补齐哪些开放问题？

## 范围
- 在这里补充主题边界、面向对象和判断标准。
"""


class MdFileInfo(BaseModel):
    """Markdown file metadata."""

    filename: str = Field(..., description="File name")
    path: str = Field(..., description="File path")
    size: int = Field(..., description="Size in bytes")
    created_time: str = Field(..., description="Created time")
    modified_time: str = Field(..., description="Modified time")


class MdFileContent(BaseModel):
    """Markdown file content."""

    content: str = Field(..., description="File content")


class WikiPageSummary(MdFileInfo):
    """Public wiki page metadata."""

    title: str = Field(..., description="Resolved page title")
    summary: str = Field(default="", description="Short page summary")
    links: list[str] = Field(default_factory=list, description="Outgoing wiki links")
    backlinks: list[str] = Field(default_factory=list, description="Incoming wiki links")
    sources: list[str] = Field(default_factory=list, description="Source file references")


class WikiPageDetail(WikiPageSummary):
    """Public wiki page metadata with markdown content."""

    content: str = Field(..., description="Markdown content")


class WikiSourceInfo(MdFileInfo):
    """Imported source metadata."""

    kind: str = Field(..., description="Source kind")
    extension: str = Field(default="", description="File extension")
    sha256: str = Field(default="", description="Content hash")
    original_path: str = Field(default="", description="Original source path")
    linked_pages: list[str] = Field(
        default_factory=list,
        description="Wiki pages generated from this source",
    )


class WikiGraphNode(BaseModel):
    """Node in the local wiki graph."""

    id: str
    label: str
    type: str


class WikiGraphEdge(BaseModel):
    """Edge in the local wiki graph."""

    source: str
    target: str
    type: str


class WikiIndexResponse(BaseModel):
    """Complete wiki dashboard payload."""

    pages: list[WikiPageSummary] = Field(default_factory=list)
    sources: list[WikiSourceInfo] = Field(default_factory=list)
    graph: dict[str, list[dict]] = Field(default_factory=dict)
    stats: dict[str, int] = Field(default_factory=dict)


class WikiPurposeResponse(MdFileInfo):
    """Editable wiki purpose document."""

    content: str = Field(..., description="Purpose markdown")


class WikiPurposeUpdate(BaseModel):
    """Wiki purpose update payload."""

    content: str = Field(..., description="Purpose markdown")


class WikiSearchHit(BaseModel):
    """A wiki search result."""

    kind: str
    filename: str
    title: str
    path: str
    snippet: str
    score: int


class WikiSearchResponse(BaseModel):
    """Wiki search results."""

    query: str
    hits: list[WikiSearchHit] = Field(default_factory=list)


class WikiLintIssue(BaseModel):
    """A wiki health issue."""

    severity: str
    code: str
    title: str
    message: str
    file: str


class WikiLintResponse(BaseModel):
    """Wiki health check result."""

    issues: list[WikiLintIssue] = Field(default_factory=list)
    stats: dict[str, int] = Field(default_factory=dict)


class WikiInsight(BaseModel):
    """Actionable wiki insight."""

    kind: str
    severity: str
    title: str
    message: str
    targets: list[str] = Field(default_factory=list)


class WikiInsightsResponse(BaseModel):
    """Actionable wiki insights."""

    insights: list[WikiInsight] = Field(default_factory=list)
    stats: dict[str, int] = Field(default_factory=dict)


class WikiSchemaHeading(BaseModel):
    """A markdown heading in a wiki page."""

    level: int
    title: str


class WikiSchemaPage(BaseModel):
    """Structural schema extracted from one wiki page."""

    filename: str
    title: str
    headings: list[WikiSchemaHeading] = Field(default_factory=list)
    frontmatter: dict[str, str] = Field(default_factory=dict)
    links: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)


class WikiSchemaField(BaseModel):
    """Aggregated frontmatter field usage."""

    name: str
    pages: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)


class WikiSchemaResponse(BaseModel):
    """Structural schema extracted from the public wiki."""

    pages: list[WikiSchemaPage] = Field(default_factory=list)
    fields: list[WikiSchemaField] = Field(default_factory=list)
    stats: dict[str, int] = Field(default_factory=dict)


class UserMemoryCreate(BaseModel):
    """Structured user memory create payload."""

    content: str = Field(..., min_length=1, max_length=600)
    type: str | None = Field(
        default=None,
        description="identity, workflow, style, or instruction",
    )
    confidence: float = Field(default=1.0, ge=0, le=1)
    reason: str = Field(default="")


class UserMemoryUpdate(BaseModel):
    """Structured user memory update payload."""

    content: str | None = Field(default=None, max_length=600)
    type: str | None = Field(default=None)
    status: str | None = Field(default=None)
    confidence: float | None = Field(default=None, ge=0, le=1)


class MemoryFactAction(BaseModel):
    """Confirmation/rejection payload for a canonical memory suggestion."""

    scope: str | None = Field(default=None)
    scope_id: str = Field(default="")
    reason: str = Field(default="")


class MemoryPreferencesUpdate(BaseModel):
    """Runtime/capture controls for no-memory conversations."""

    runtime_enabled: bool | None = None
    capture_enabled: bool | None = None


class UserUnderstandingFeedback(BaseModel):
    """Explicit correction signal for the disposable understanding layer."""

    verdict: str = Field(pattern="^(confirmed|corrected|ignored)$")


class ProactiveOpportunityFeedback(BaseModel):
    """User control over one queued proactive opportunity."""

    verdict: str = Field(pattern="^(presented|ignored|completed|dismissed)$")


class MemoryClearRequest(BaseModel):
    """Explicit confirmation for clearing every long-term memory layer."""

    confirmation: str = Field(..., min_length=1, max_length=32)
    create_backup: bool = True


class MemoryDreamingRunRequest(BaseModel):
    """Manual governed consolidation request."""

    shadow: bool = False


class MemoryDreamingPreferencesUpdate(BaseModel):
    """Opt-in schedule and deterministic promotion threshold."""

    enabled: bool
    schedule: str = Field(min_length=9, max_length=80)
    rem_enabled: bool = True
    min_score: float = Field(default=0.68, ge=0.0, le=1.0)


class MemoryImportPreviewRequest(BaseModel):
    """Pasted, provider-neutral memory export for local review."""

    text: str = Field(..., min_length=4, max_length=50_000)
    provider: str = Field(default="其他 AI", max_length=40)


class MemoryImportCommitRequest(BaseModel):
    """Candidate IDs explicitly selected by the user."""

    selected_ids: list[str] = Field(..., min_length=1, max_length=60)


async def _save_memory_pack_upload(file: UploadFile) -> Path:
    """Copy one bounded upload to a private temporary file."""

    fd, raw_path = tempfile.mkstemp(suffix=".lantai-memory.zip")
    path = Path(raw_path)
    total = 0
    try:
        with os.fdopen(fd, "wb") as target:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > MEMORY_PACK_MAX_TOTAL_BYTES:
                    raise MemoryPackError("记忆包压缩文件超过大小限制")
                target.write(chunk)
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _normalize_md_name(md_name: str) -> str:
    name = Path(md_name).name
    return name if name.lower().endswith(".md") else f"{name}.md"


def _is_internal_workspace_md(md_name: str) -> bool:
    return _normalize_md_name(md_name).upper() in INTERNAL_WORKSPACE_MD_FILES


def _wiki_root(workspace_dir: Path) -> Path:
    root = workspace_dir / WIKI_INTERNAL_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def _wiki_sources_dir(workspace_dir: Path) -> Path:
    sources_dir = _wiki_root(workspace_dir) / WIKI_SOURCES_DIR
    sources_dir.mkdir(parents=True, exist_ok=True)
    return sources_dir


def _wiki_purpose_path(workspace_dir: Path) -> Path:
    return _wiki_root(workspace_dir) / WIKI_PURPOSE_FILE


def _ensure_wiki_purpose(workspace_dir: Path) -> Path:
    purpose_path = _wiki_purpose_path(workspace_dir)
    if not purpose_path.exists():
        purpose_path.write_text(DEFAULT_WIKI_PURPOSE, encoding="utf-8")
    return purpose_path


def _read_wiki_purpose(workspace_dir: Path) -> WikiPurposeResponse:
    purpose_path = _ensure_wiki_purpose(workspace_dir)
    info = _file_info_from_path(purpose_path)
    return WikiPurposeResponse(
        **info.model_dump(),
        content=_read_text_safely(purpose_path),
    )


def _safe_public_wiki_md_name(md_name: str) -> str:
    filename = Path(md_name).name.strip()
    if not filename:
        raise HTTPException(status_code=400, detail="Wiki file name is required")
    if _is_internal_workspace_md(filename):
        raise HTTPException(status_code=403, detail="Internal files are protected")
    if not filename.lower().endswith(".md"):
        filename = f"{filename}.md"
    if filename.startswith(".") or filename in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid wiki file name")
    return filename


def _safe_source_filename(filename: str) -> str:
    candidate = Path(filename or "source.txt").name.strip()
    if not candidate:
        candidate = "source.txt"
    if _is_internal_workspace_md(candidate):
        raise HTTPException(
            status_code=400,
            detail=f"Internal instruction file is not a wiki source: {candidate}",
        )
    stem = Path(candidate).stem or "source"
    suffix = Path(candidate).suffix or ".txt"
    safe_stem = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+", "-", stem).strip(".-_")
    if not safe_stem:
        safe_stem = hashlib.sha1(candidate.encode("utf-8")).hexdigest()[:12]
    return f"{safe_stem[:80]}{suffix.lower()}"


def _safe_source_relative_path(filename: str) -> Path:
    raw_path = Path(filename or "source.txt")
    if raw_path.is_absolute():
        raise HTTPException(status_code=400, detail="Invalid source path")

    safe_parts: list[str] = []
    parts = raw_path.parts
    for index, part in enumerate(parts):
        if part in {"", ".", ".."}:
            raise HTTPException(status_code=400, detail="Invalid source path")
        if index == len(parts) - 1:
            safe_parts.append(_safe_source_filename(part))
            continue
        safe_part = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+", "-", part).strip(".-_")
        safe_parts.append(safe_part or hashlib.sha1(part.encode("utf-8")).hexdigest()[:12])

    if not safe_parts:
        safe_parts = ["source.txt"]
    return Path(*safe_parts)


def _unique_child_path(directory: Path, filename: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    base = Path(filename).stem
    suffix = Path(filename).suffix
    candidate = directory / filename
    counter = 2
    while candidate.exists():
        candidate = directory / f"{base}-{counter}{suffix}"
        counter += 1
    return candidate


def _is_internal_runtime_relative_path(relative_path: Path) -> bool:
    return bool(relative_path.parts) and relative_path.parts[0] == LANTAI_INTERNAL_DIR


def _unique_source_path(workspace_dir: Path, relative_path: Path) -> Path:
    source_root = _wiki_sources_dir(workspace_dir)
    target_dir = source_root / relative_path.parent
    return _unique_child_path(target_dir, relative_path.name)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _find_existing_source_by_sha256(workspace_dir: Path, sha256: str) -> Path | None:
    sources_dir = workspace_dir / WIKI_INTERNAL_DIR / WIKI_SOURCES_DIR
    if not sources_dir.exists():
        return None
    for path in sources_dir.rglob("*"):
        if path.is_file() and _sha256_file(path) == sha256:
            return path
    return None


def _source_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown"}:
        return "markdown"
    if suffix in {".csv", ".json", ".jsonl", ".yaml", ".yml", ".xml"}:
        return "data"
    if suffix in {".html", ".htm"}:
        return "web"
    if suffix in {".py", ".js", ".jsx", ".ts", ".tsx", ".css", ".less"}:
        return "code"
    if suffix in WIKI_TEXT_SOURCE_EXTENSIONS:
        return "text"
    return "binary"


def _file_info_from_path(path: Path) -> MdFileInfo:
    stat = path.stat()
    return MdFileInfo(
        filename=path.name,
        path=str(path),
        size=stat.st_size,
        created_time=datetime.fromtimestamp(stat.st_ctime).isoformat(),
        modified_time=datetime.fromtimestamp(stat.st_mtime).isoformat(),
    )


def _read_text_safely(path: Path, max_chars: int | None = None) -> str:
    content = read_text_file_with_encoding_fallback(path)
    if max_chars is not None and len(content) > max_chars:
        return content[:max_chars]
    return content


def _strip_frontmatter(content: str) -> str:
    return FRONTMATTER_RE.sub("", content, count=1)


def _extract_frontmatter(content: str) -> dict[str, str]:
    match = FRONTMATTER_RE.match(content)
    if not match:
        return {}

    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        field_match = FRONTMATTER_FIELD_RE.match(line.strip())
        if field_match:
            fields[field_match.group(1)] = field_match.group(2).strip("\"'")
    return fields


def _extract_headings(content: str) -> list[WikiSchemaHeading]:
    headings: list[WikiSchemaHeading] = []
    in_code_block = False
    for line in _strip_frontmatter(content).splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        match = MARKDOWN_HEADING_RE.match(stripped)
        if match:
            headings.append(
                WikiSchemaHeading(
                    level=len(match.group(1)),
                    title=match.group(2).strip(),
                ),
            )
    return headings


def _extract_wiki_title(content: str, filename: str) -> str:
    body = _strip_frontmatter(content)
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped.lstrip("#").strip() or Path(filename).stem
    return Path(filename).stem.replace("_", " ").replace("-", " ").strip() or filename


def _extract_wiki_summary(content: str) -> str:
    body = _strip_frontmatter(content)
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("```"):
            continue
        return stripped[:180]
    return ""


def _extract_wiki_links(content: str) -> list[str]:
    links: list[str] = []
    for match in WIKI_LINK_RE.finditer(content):
        ref = match.group(1).strip()
        if ref and ref not in links:
            links.append(ref)
    return links


def _extract_source_refs(content: str) -> list[str]:
    refs: list[str] = []
    for match in WIKI_SOURCE_FRONTMATTER_RE.finditer(content[:2000]):
        ref = match.group(1).strip()
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def _public_wiki_page_paths(workspace_dir: Path) -> list[Path]:
    if not workspace_dir.exists():
        return []
    pages: list[Path] = []
    for path in sorted(
        workspace_dir.glob("*.md"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    ):
        if path.is_file() and not _is_internal_workspace_md(path.name):
            pages.append(path)
    return pages


def _resolve_wiki_link(
    link: str,
    page_names: set[str],
    page_name_by_stem: dict[str, str],
) -> str:
    filename = Path(link).name.strip()
    if not filename:
        return link
    if not filename.lower().endswith(".md"):
        filename = f"{filename}.md"
    if filename in page_names:
        return filename
    return page_name_by_stem.get(Path(filename).stem.lower(), filename)


def _slugify_wiki_page_name(title: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+", "-", title).strip(".-_")
    if not stem:
        stem = hashlib.sha1(title.encode("utf-8")).hexdigest()[:12]
    return f"{stem[:80]}.md"


def _collect_wiki_data(workspace_dir: Path) -> WikiIndexResponse:
    page_paths = _public_wiki_page_paths(workspace_dir)
    page_names = {path.name for path in page_paths}
    page_name_by_stem = {path.stem.lower(): path.name for path in page_paths}

    content_by_page: dict[str, str] = {}
    raw_links_by_page: dict[str, list[str]] = {}
    resolved_links_by_page: dict[str, list[str]] = {}
    source_refs_by_page: dict[str, list[str]] = {}
    backlinks_by_page: dict[str, list[str]] = {path.name: [] for path in page_paths}
    broken_links = 0

    for path in page_paths:
        content = _read_text_safely(path)
        content_by_page[path.name] = content
        raw_links = _extract_wiki_links(content)
        raw_links_by_page[path.name] = raw_links
        resolved_links: list[str] = []
        for link in raw_links:
            resolved = _resolve_wiki_link(link, page_names, page_name_by_stem)
            if resolved not in page_names:
                broken_links += 1
            if resolved not in resolved_links:
                resolved_links.append(resolved)
            if resolved in backlinks_by_page and path.name not in backlinks_by_page[resolved]:
                backlinks_by_page[resolved].append(path.name)
        resolved_links_by_page[path.name] = resolved_links
        source_refs_by_page[path.name] = _extract_source_refs(content)

    pages: list[WikiPageSummary] = []
    for path in page_paths:
        info = _file_info_from_path(path)
        content = content_by_page.get(path.name, "")
        pages.append(
            WikiPageSummary(
                **info.model_dump(),
                title=_extract_wiki_title(content, path.name),
                summary=_extract_wiki_summary(content),
                links=resolved_links_by_page.get(path.name, []),
                backlinks=backlinks_by_page.get(path.name, []),
                sources=source_refs_by_page.get(path.name, []),
            ),
        )

    sources = _list_wiki_sources(workspace_dir, source_refs_by_page)

    nodes: list[WikiGraphNode] = [
        WikiGraphNode(id=page.filename, label=page.title, type="page")
        for page in pages
    ]
    nodes.extend(
        WikiGraphNode(id=source.path, label=source.filename, type="source")
        for source in sources
    )
    edges: list[WikiGraphEdge] = []
    for page in pages:
        for target in page.links:
            edges.append(WikiGraphEdge(source=page.filename, target=target, type="link"))
        for source_ref in page.sources:
            edges.append(
                WikiGraphEdge(source=source_ref, target=page.filename, type="source")
            )

    stats = {
        "pages": len(pages),
        "sources": len(sources),
        "links": sum(len(page.links) for page in pages),
        "backlinks": sum(len(page.backlinks) for page in pages),
        "broken_links": broken_links,
    }
    return WikiIndexResponse(
        pages=pages,
        sources=sources,
        graph={
            "nodes": [node.model_dump() for node in nodes],
            "edges": [edge.model_dump() for edge in edges],
        },
        stats=stats,
    )


def _list_wiki_sources(
    workspace_dir: Path,
    source_refs_by_page: dict[str, list[str]] | None = None,
) -> list[WikiSourceInfo]:
    sources_dir = workspace_dir / WIKI_INTERNAL_DIR / WIKI_SOURCES_DIR
    if not sources_dir.exists():
        return []

    linked_pages_by_source: dict[str, list[str]] = {}
    if source_refs_by_page:
        for page, refs in source_refs_by_page.items():
            for ref in refs:
                linked_pages_by_source.setdefault(ref, []).append(page)

    sources: list[WikiSourceInfo] = []
    for path in sorted(
        (item for item in sources_dir.rglob("*") if item.is_file()),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    ):
        sources.append(
            _wiki_source_info_from_path(
                path,
                workspace_dir,
                linked_pages_by_source.get(
                    path.relative_to(workspace_dir).as_posix(),
                    [],
                ),
            ),
        )
    return sources


def _wiki_source_info_from_path(
    path: Path,
    workspace_dir: Path,
    linked_pages: list[str] | None = None,
) -> WikiSourceInfo:
    info = _file_info_from_path(path)
    rel = path.relative_to(workspace_dir).as_posix()
    sources_dir = workspace_dir / WIKI_INTERNAL_DIR / WIKI_SOURCES_DIR
    payload = info.model_dump()
    payload["path"] = rel
    return WikiSourceInfo(
        **payload,
        kind=_source_kind(path),
        extension=path.suffix.lower(),
        sha256=_sha256_file(path),
        original_path=(
            path.relative_to(sources_dir).as_posix()
            if path.is_relative_to(sources_dir)
            else path.name
        ),
        linked_pages=linked_pages or [],
    )


def _linked_pages_for_source(workspace_dir: Path, source_path: Path) -> list[str]:
    source_ref = source_path.relative_to(workspace_dir).as_posix()
    data = _collect_wiki_data(workspace_dir)
    return [page.filename for page in data.pages if source_ref in page.sources]


def _create_wiki_page_from_source(source_path: Path, workspace_dir: Path) -> str:
    relative_source = source_path.relative_to(workspace_dir).as_posix()
    source_context = source_path.relative_to(_wiki_sources_dir(workspace_dir)).parent
    title = source_path.stem.replace("_", " ").replace("-", " ").strip() or source_path.name
    page_path = _unique_child_path(workspace_dir, _slugify_wiki_page_name(title))
    imported_at = datetime.now(timezone.utc).isoformat()

    if _source_kind(source_path) != "binary":
        source_content = _read_text_safely(
            source_path,
            max_chars=MAX_WIKI_TEXT_SOURCE_CHARS,
        )
        body = _strip_frontmatter(source_content).strip()
        if not body.startswith("# "):
            body = f"# {title}\n\n{body}".rstrip()
    else:
        body = (
            f"# {title}\n\n"
            f"Imported binary source `{source_path.name}`. Add notes, links, "
            "and summaries here as the knowledge page develops."
        )

    page_content = (
        "---\n"
        f"source: {relative_source}\n"
        f"source_sha256: {_sha256_file(source_path)}\n"
        f"source_context: {source_context.as_posix() if source_context.as_posix() != '.' else ''}\n"
        f"imported_at: {imported_at}\n"
        "---\n\n"
        f"{body}\n"
    )
    page_path.write_text(page_content, encoding="utf-8")
    return page_path.name


def _import_single_wiki_source(
    workspace_dir: Path,
    filename: str,
    data: bytes,
) -> tuple[WikiSourceInfo, str | None, bool]:
    source_hash = _sha256_bytes(data)
    existing_source = _find_existing_source_by_sha256(workspace_dir, source_hash)
    if existing_source is not None:
        linked_pages = _linked_pages_for_source(workspace_dir, existing_source)
        if not linked_pages:
            page_name = _create_wiki_page_from_source(existing_source, workspace_dir)
            linked_pages = [page_name]
            return (
                _wiki_source_info_from_path(
                    existing_source,
                    workspace_dir,
                    linked_pages,
                ),
                page_name,
                False,
            )
        return (
            _wiki_source_info_from_path(
                existing_source,
                workspace_dir,
                linked_pages,
            ),
            linked_pages[0] if linked_pages else None,
            True,
        )

    safe_relative_path = _safe_source_relative_path(filename)
    source_path = _unique_source_path(workspace_dir, safe_relative_path)
    source_path.write_bytes(data)
    page_name = _create_wiki_page_from_source(source_path, workspace_dir)
    return (
        _wiki_source_info_from_path(source_path, workspace_dir, [page_name]),
        page_name,
        False,
    )


def _import_wiki_source_upload(
    workspace_dir: Path,
    filename: str,
    data: bytes,
) -> dict:
    imported_sources: list[WikiSourceInfo] = []
    generated_pages: list[str] = []
    skipped_sources: list[WikiSourceInfo] = []

    if filename.lower().endswith(".zip") or zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for member in sorted(
                (item for item in archive.infolist() if not item.is_dir()),
                key=lambda item: item.filename,
            ):
                relative = Path(member.filename)
                if relative.is_absolute():
                    raise HTTPException(status_code=400, detail="Invalid zip path")
                if any(part in {"", ".", ".."} for part in relative.parts):
                    raise HTTPException(status_code=400, detail="Invalid zip path")
                if _is_internal_workspace_md(relative.name):
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "Internal instruction file is not a wiki source: "
                            f"{relative.name}"
                        ),
                    )
                source_info, page_name, skipped = _import_single_wiki_source(
                    workspace_dir,
                    relative.as_posix(),
                    archive.read(member),
                )
                if skipped:
                    skipped_sources.append(source_info)
                else:
                    imported_sources.append(source_info)
                if page_name and not skipped:
                    generated_pages.append(page_name)
    else:
        source_info, page_name, skipped = _import_single_wiki_source(
            workspace_dir,
            filename,
            data,
        )
        if skipped:
            skipped_sources.append(source_info)
        else:
            imported_sources.append(source_info)
        if page_name and not skipped:
            generated_pages.append(page_name)

    return {
        "imported": [source.model_dump() for source in imported_sources],
        "skipped": [source.model_dump() for source in skipped_sources],
        "pages": generated_pages,
    }


def _snippet_for_query(content: str, query: str) -> str:
    if not query:
        return content[:160]
    lower_content = content.lower()
    lower_query = query.lower()
    index = lower_content.find(lower_query)
    if index < 0:
        return content[:160]
    start = max(0, index - 60)
    end = min(len(content), index + len(query) + 100)
    prefix = "..." if start else ""
    suffix = "..." if end < len(content) else ""
    return f"{prefix}{content[start:end].strip()}{suffix}"


def _build_wiki_schema(workspace_dir: Path) -> WikiSchemaResponse:
    data = _collect_wiki_data(workspace_dir)
    pages: list[WikiSchemaPage] = []
    field_pages: dict[str, list[str]] = {}
    field_examples: dict[str, list[str]] = {}

    for page in data.pages:
        content = _read_text_safely(workspace_dir / page.filename)
        frontmatter = _extract_frontmatter(content)
        for field_name, field_value in frontmatter.items():
            field_pages.setdefault(field_name, []).append(page.filename)
            if field_value and field_value not in field_examples.setdefault(field_name, []):
                field_examples[field_name].append(field_value[:80])

        pages.append(
            WikiSchemaPage(
                filename=page.filename,
                title=page.title,
                headings=_extract_headings(content),
                frontmatter=frontmatter,
                links=page.links,
                sources=page.sources,
            ),
        )

    fields = [
        WikiSchemaField(
            name=name,
            pages=field_pages[name],
            examples=field_examples.get(name, [])[:3],
        )
        for name in sorted(field_pages)
    ]

    return WikiSchemaResponse(
        pages=pages,
        fields=fields,
        stats={
            "pages": len(pages),
            "fields": len(fields),
            "headings": sum(len(page.headings) for page in pages),
            "sources": data.stats.get("sources", 0),
        },
    )


def _lint_wiki(workspace_dir: Path) -> WikiLintResponse:
    data = _collect_wiki_data(workspace_dir)
    page_names = {page.filename for page in data.pages}
    issues: list[WikiLintIssue] = []

    for page in data.pages:
        path = workspace_dir / page.filename
        content = _read_text_safely(path)
        if not page.title or page.title == path.stem.replace("-", " "):
            issues.append(
                WikiLintIssue(
                    severity="warning",
                    code="missing-title",
                    title="缺少页面标题",
                    message="请添加一级 # 标题，让页面拥有稳定标题。",
                    file=page.filename,
                ),
            )
        if not page.summary:
            issues.append(
                WikiLintIssue(
                    severity="info",
                    code="missing-summary",
                    title="缺少摘要",
                    message="请添加简短开头段落，方便浏览和搜索摘要展示。",
                    file=page.filename,
                ),
            )
        if not page.links and not page.backlinks and page.filename.lower() != "overview.md":
            issues.append(
                WikiLintIssue(
                    severity="warning",
                    code="orphan-page",
                    title="孤岛知识页",
                    message="请添加至少一个知识页链接或反链，让它加入知识图谱。",
                    file=page.filename,
                ),
            )
        for raw_link in _extract_wiki_links(content):
            resolved = _resolve_wiki_link(
                raw_link,
                page_names,
                {Path(name).stem.lower(): name for name in page_names},
            )
            if resolved not in page_names:
                issues.append(
                    WikiLintIssue(
                        severity="error",
                        code="broken-link",
                        title="知识页链接断开",
                        message=f"[[{raw_link}]] 没有对应的公开知识页。",
                        file=page.filename,
                    ),
                )

    for source in data.sources:
        if not source.linked_pages:
            issues.append(
                WikiLintIssue(
                    severity="warning",
                    code="unlinked-source",
                    title="资料未关联知识页",
                    message="这份导入资料尚未被任何公开知识页引用。",
                    file=source.path,
                ),
            )

    stats = dict(data.stats)
    stats["issues"] = len(issues)
    return WikiLintResponse(issues=issues, stats=stats)


def _build_wiki_insights(workspace_dir: Path) -> WikiInsightsResponse:
    data = _collect_wiki_data(workspace_dir)
    lint = _lint_wiki(workspace_dir)
    issues_by_code: dict[str, list[WikiLintIssue]] = {}
    for issue in lint.issues:
        issues_by_code.setdefault(issue.code, []).append(issue)

    insights: list[WikiInsight] = []
    broken_links = issues_by_code.get("broken-link", [])
    if broken_links:
        insights.append(
            WikiInsight(
                kind="knowledge-gap",
                severity="high",
                title="断链意味着知识缺口",
                message=(
                    f"{len(broken_links)} 个知识页链接指向缺失页面。"
                    "补齐这些页面会让图谱更可用。"
                ),
                targets=[issue.file for issue in broken_links[:8]],
            ),
        )

    orphan_pages = issues_by_code.get("orphan-page", [])
    if orphan_pages:
        insights.append(
            WikiInsight(
                kind="graph-island",
                severity="medium",
                title="有些页面还没有接入图谱",
                message=(
                    f"{len(orphan_pages)} 个页面没有链接或反链。"
                    "建议连接到总览、概念页或实体页。"
                ),
                targets=[issue.file for issue in orphan_pages[:8]],
            ),
        )

    unlinked_sources = issues_by_code.get("unlinked-source", [])
    if unlinked_sources:
        insights.append(
            WikiInsight(
                kind="traceability",
                severity="medium",
                title="有些资料还没有沉淀成知识页",
                message=(
                    f"{len(unlinked_sources)} 份资料未被知识页引用。"
                    "可以重新生成页面，或关联到已有页面。"
                ),
                targets=[issue.file for issue in unlinked_sources[:8]],
            ),
        )

    if data.pages and data.stats.get("links", 0) < max(1, len(data.pages) - 1):
        insights.append(
            WikiInsight(
                kind="low-connectivity",
                severity="info",
                title="图谱还可以增加更多交叉链接",
                message=(
                    "当前内部链接数量少于页面数量。"
                    "编辑页面时可以补充概念和实体链接。"
                ),
                targets=[page.filename for page in data.pages[:8]],
            ),
        )

    if not insights:
        insights.append(
            WikiInsight(
                kind="healthy",
                severity="info",
                title="知识图谱状态良好",
                message="暂未发现明显缺口。可以继续导入资料并整理链接。",
                targets=[],
            ),
        )

    return WikiInsightsResponse(
        insights=insights,
        stats={
            **data.stats,
            "issues": len(lint.issues),
            "insights": len(insights),
        },
    )


def _get_user_memory_store(workspace) -> StructuredUserMemoryStore:
    return StructuredUserMemoryStore(workspace.workspace_dir)


def _get_layered_memory_store(workspace) -> LayeredMemoryStore:
    return LayeredMemoryStore(
        workspace.workspace_dir,
        agent_id=workspace.agent_id,
        user_memory_store=_get_user_memory_store(workspace),
    )


def _get_memory_ledger(workspace) -> CanonicalMemoryLedger:
    manager = getattr(workspace, "memory_manager", None)
    ledger = getattr(manager, "memory_ledger", None)
    if isinstance(ledger, CanonicalMemoryLedger):
        return ledger
    return CanonicalMemoryLedger(workspace.workspace_dir)


def _get_user_understanding_store(workspace) -> UserUnderstandingStore:
    manager = getattr(workspace, "memory_manager", None)
    store = getattr(manager, "user_understanding_store", None)
    if isinstance(store, UserUnderstandingStore):
        return store
    return UserUnderstandingStore(workspace.workspace_dir)


async def _invalidate_brief_understanding_dependency(
    workspace,
    atom_id: str,
) -> int:
    """Fence brief derivatives after a user-understanding clue is retired."""

    service = ReminderService(ReminderRepository(workspace.workspace_dir))
    return await service.invalidate_watch_topics_by_evidence_ref(
        f"understanding:{atom_id}",
    )


async def _invalidate_user_understanding_sources(
    workspace,
    source_ids: list[str],
) -> list[str]:
    """Retire forgotten understanding evidence and fence dependent briefs."""

    clean_source_ids = list(
        dict.fromkeys(
            str(item or "").strip()
            for item in source_ids
            if str(item or "").strip()
        )
    )
    if not clean_source_ids:
        return []
    understanding = _get_user_understanding_store(workspace)
    before = {
        atom.id
        for atom in understanding.list_atoms()
        if atom.status in {"observed", "working_hypothesis", "confirmed"}
        and any(source_id in atom.evidence_ids for source_id in clean_source_ids)
    }
    for source_id in clean_source_ids:
        understanding.invalidate_source(source_id)
    after = {atom.id: atom for atom in understanding.list_atoms()}
    retired = sorted(
        atom_id
        for atom_id in before
        if atom_id not in after
        or after[atom_id].status not in {"observed", "working_hypothesis", "confirmed"}
    )
    for atom_id in retired:
        await _invalidate_brief_understanding_dependency(workspace, atom_id)
    return retired


def _get_opportunity_store(workspace) -> OpportunityStore:
    manager = getattr(workspace, "memory_manager", None)
    store = getattr(manager, "opportunity_store", None)
    if isinstance(store, OpportunityStore):
        return store
    return OpportunityStore(workspace.workspace_dir)


def _dir_stats(root: Path) -> tuple[int, int]:
    """Return (file_count, total_size) for *root* recursively."""
    count = 0
    size = 0
    if root.is_dir():
        for p in root.rglob("*"):
            if p.is_file():
                count += 1
                size += p.stat().st_size
    return count, size


def _zip_directory(root: Path) -> io.BytesIO:
    """Create an in-memory public wiki zip archive of *root*.

    Root-level internal instruction markdown files are intentionally skipped.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for entry in sorted(root.rglob("*")):
            relative_entry = entry.relative_to(root)
            if _is_internal_runtime_relative_path(relative_entry):
                continue
            if wiki_core.is_private_wiki_relative_path(relative_entry):
                continue
            if (
                len(relative_entry.parts) == 1
                and entry.is_file()
                and _is_internal_workspace_md(entry.name)
            ):
                continue

            arcname = relative_entry.as_posix()
            if entry.is_file():
                zf.write(entry, arcname)
            elif entry.is_dir():
                # Zip spec: directory entries end with '/'
                zf.write(entry, arcname + "/")
    buf.seek(0)
    return buf


MdFileInfo = wiki_core.MdFileInfo
MdFileContent = wiki_core.MdFileContent
WikiPageSummary = wiki_core.WikiPageSummary
WikiPageDetail = wiki_core.WikiPageDetail
WikiSourceInfo = wiki_core.WikiSourceInfo
WikiSourceContentResponse = wiki_core.WikiSourceContentResponse
WikiSourceUpdateRequest = wiki_core.WikiSourceUpdateRequest
WikiArchivedSourceInfo = wiki_core.WikiArchivedSourceInfo
WikiCollection = wiki_core.WikiCollection
WikiCollectionCreateRequest = wiki_core.WikiCollectionCreateRequest
WikiCollectionUpdateRequest = wiki_core.WikiCollectionUpdateRequest
WikiIndexResponse = wiki_core.WikiIndexResponse
WikiPurposeResponse = wiki_core.WikiPurposeResponse
WikiPurposeUpdate = wiki_core.WikiPurposeUpdate
WikiSearchHit = wiki_core.WikiSearchHit
WikiSearchResponse = wiki_core.WikiSearchResponse
WikiLintIssue = wiki_core.WikiLintIssue
WikiLintResponse = wiki_core.WikiLintResponse
WikiInsight = wiki_core.WikiInsight
WikiInsightsResponse = wiki_core.WikiInsightsResponse
WikiSchemaHeading = wiki_core.WikiSchemaHeading
WikiSchemaPage = wiki_core.WikiSchemaPage
WikiSchemaField = wiki_core.WikiSchemaField
WikiSchemaResponse = wiki_core.WikiSchemaResponse
WikiReviewItem = wiki_core.WikiReviewItem
WikiReviewResponse = wiki_core.WikiReviewResponse
WikiHomeResponse = wiki_core.WikiHomeResponse
WikiReviewResolveRequest = wiki_core.WikiReviewResolveRequest
WikiQueryRequest = wiki_core.WikiQueryRequest
WikiQueryResponse = wiki_core.WikiQueryResponse
WikiIngestRunRequest = wiki_core.WikiIngestRunRequest
WikiUrlImportRequest = wiki_core.WikiUrlImportRequest
WikiTrashItem = wiki_core.WikiTrashItem

_is_internal_workspace_md = wiki_core.is_internal_workspace_md
_is_internal_runtime_relative_path = wiki_core.is_internal_runtime_relative_path
_is_private_wiki_relative_path = wiki_core.is_private_wiki_relative_path
_collect_wiki_data = wiki_core.collect_wiki_data
_read_wiki_purpose = wiki_core.read_wiki_purpose
_wiki_purpose_path = wiki_core.purpose_path
_safe_public_wiki_md_name = wiki_core.safe_public_wiki_md_name
_read_text_safely = wiki_core.read_text_safely
_list_wiki_sources = wiki_core.list_wiki_sources
_import_wiki_source_upload = wiki_core.import_wiki_source_upload
_snippet_for_query = wiki_core.snippet_for_query
_strip_frontmatter = wiki_core.strip_frontmatter
_lint_wiki = wiki_core.lint_wiki
_build_wiki_insights = wiki_core.build_wiki_insights
_build_wiki_schema = wiki_core.build_wiki_schema


# ---------------------------------------------------------------------------
# Agent File Management Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/files",
    response_model=list[MdFileInfo],
    summary="List working files",
    description="List all working files (uses active agent)",
)
async def list_working_files(
    request: Request,
) -> list[MdFileInfo]:
    """List working directory markdown files."""
    try:
        workspace = await get_agent_for_request(request)
        workspace_manager = AgentMdManager(
            str(workspace.workspace_dir),
            agent_id=workspace.agent_id,
        )
        files = [
            MdFileInfo.model_validate(file)
            for file in workspace_manager.list_working_mds()
            if not _is_internal_workspace_md(file["filename"])
        ]
        return files
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/files/{md_name}",
    response_model=MdFileContent,
    summary="Read a working file",
    description="Read a working markdown file (uses active agent)",
)
async def read_working_file(
    md_name: str,
    request: Request,
) -> MdFileContent:
    """Read a working directory markdown file."""
    if _is_internal_workspace_md(md_name):
        raise HTTPException(
            status_code=404,
            detail="Wiki file not found",
        )

    try:
        workspace = await get_agent_for_request(request)
        workspace_manager = AgentMdManager(
            str(workspace.workspace_dir),
            agent_id=workspace.agent_id,
        )
        content = workspace_manager.read_working_md(md_name)
        return MdFileContent(content=content)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.put(
    "/files/{md_name}",
    response_model=dict,
    summary="Write a working file",
    description="Create or update a working file (uses active agent)",
)
async def write_working_file(
    md_name: str,
    body: MdFileContent,
    request: Request,
) -> dict:
    """Write a working directory markdown file."""
    if _is_internal_workspace_md(md_name):
        raise HTTPException(
            status_code=403,
            detail="Internal instruction files cannot be edited here",
        )

    try:
        workspace = await get_agent_for_request(request)
        workspace_manager = AgentMdManager(
            str(workspace.workspace_dir),
            agent_id=workspace.agent_id,
        )
        workspace_manager.write_working_md(md_name, body.content)
        return {"written": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Wiki Workbench Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/wiki",
    response_model=WikiIndexResponse,
    summary="Read the public wiki index",
    description="Return public wiki pages, imported sources, graph, and stats.",
)
async def get_wiki_index(request: Request) -> WikiIndexResponse:
    """Return the local public wiki dashboard data."""
    try:
        workspace = await get_agent_for_request(request)
        return await asyncio.to_thread(_collect_wiki_data, workspace.workspace_dir)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/wiki/home",
    response_model=WikiHomeResponse,
    summary="Read the user-facing knowledge workspace",
)
async def get_wiki_home(request: Request) -> WikiHomeResponse:
    """Return one projection for the desktop knowledge experience."""
    try:
        workspace = await get_agent_for_request(request)
        return await asyncio.to_thread(
            wiki_core.build_wiki_home,
            workspace.workspace_dir,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/wiki/purpose",
    response_model=WikiPurposeResponse,
    summary="Read the wiki purpose document",
)
async def get_wiki_purpose(request: Request) -> WikiPurposeResponse:
    """Read the editable purpose document for this wiki."""
    try:
        workspace = await get_agent_for_request(request)
        return await asyncio.to_thread(_read_wiki_purpose, workspace.workspace_dir)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.put(
    "/wiki/purpose",
    response_model=dict,
    summary="Update the wiki purpose document",
)
async def update_wiki_purpose(
    body: WikiPurposeUpdate,
    request: Request,
) -> dict:
    """Update the editable purpose document for this wiki."""
    try:
        workspace = await get_agent_for_request(request)
        return await asyncio.to_thread(
            wiki_core.write_wiki_purpose,
            workspace.workspace_dir,
            body.content,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/wiki/pages/{md_name:path}",
    response_model=WikiPageDetail,
    summary="Read a public wiki page",
)
async def read_wiki_page(md_name: str, request: Request) -> WikiPageDetail:
    """Read a public wiki markdown page."""
    try:
        workspace = await get_agent_for_request(request)
        return await asyncio.to_thread(
            wiki_core.read_wiki_page,
            workspace.workspace_dir,
            md_name,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.put(
    "/wiki/pages/{md_name:path}",
    response_model=dict,
    summary="Create or update a public wiki page",
)
async def write_wiki_page(
    md_name: str,
    body: MdFileContent,
    request: Request,
) -> dict:
    """Create or update a public wiki markdown page."""
    try:
        workspace = await get_agent_for_request(request)
        return await asyncio.to_thread(
            wiki_core.write_wiki_page,
            workspace.workspace_dir,
            md_name,
            body.content,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.delete(
    "/wiki/pages/{md_name:path}",
    response_model=dict,
    summary="Move a public wiki page to the recycle bin",
)
async def delete_wiki_page(md_name: str, request: Request) -> dict:
    """Remove a public page from retrieval while keeping it recoverable."""
    try:
        workspace = await get_agent_for_request(request)
        return await asyncio.to_thread(
            wiki_core.delete_wiki_page,
            workspace.workspace_dir,
            md_name,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/wiki/sources",
    response_model=list[WikiSourceInfo],
    summary="List imported wiki sources",
)
async def list_wiki_sources(request: Request) -> list[WikiSourceInfo]:
    """List imported source files used to build the public wiki."""
    try:
        workspace = await get_agent_for_request(request)
        return await asyncio.to_thread(_list_wiki_sources, workspace.workspace_dir)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/wiki/sources/content",
    response_model=WikiSourceContentResponse,
    summary="Read extracted knowledge source content",
)
async def read_wiki_source_content(
    request: Request,
    path: str = Query(..., min_length=1, max_length=1200),
    expected_revision: str = Query(default="", max_length=120),
    expected_text_hash: str = Query(default="", max_length=64),
    source_start: int | None = Query(default=None, ge=0, le=80_000),
    source_end: int | None = Query(default=None, ge=0, le=80_000),
) -> WikiSourceContentResponse:
    try:
        workspace = await get_agent_for_request(request)
        return await asyncio.to_thread(
            wiki_core.read_wiki_source_content,
            workspace.workspace_dir,
            path,
            expected_revision=expected_revision,
            expected_text_hash=expected_text_hash,
            source_start=source_start,
            source_end=source_end,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.patch(
    "/wiki/sources/metadata",
    response_model=WikiSourceInfo,
    summary="Update a knowledge source display title or collections",
)
async def update_wiki_source_metadata(
    body: WikiSourceUpdateRequest,
    request: Request,
    path: str = Query(..., min_length=1, max_length=1200),
) -> WikiSourceInfo:
    try:
        workspace = await get_agent_for_request(request)
        return await asyncio.to_thread(
            wiki_core.update_wiki_source,
            workspace.workspace_dir,
            path,
            body,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.delete(
    "/wiki/sources",
    response_model=dict,
    summary="Remove a source from the knowledge base",
)
async def archive_wiki_source(
    request: Request,
    path: str = Query(..., min_length=1, max_length=1200),
) -> dict:
    try:
        workspace = await get_agent_for_request(request)
        return await asyncio.to_thread(
            wiki_core.archive_wiki_source,
            workspace.workspace_dir,
            path,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/wiki/sources/archived",
    response_model=list[WikiArchivedSourceInfo],
    summary="List recoverable archived knowledge sources",
)
async def list_archived_wiki_sources(
    request: Request,
) -> list[WikiArchivedSourceInfo]:
    workspace = await get_agent_for_request(request)
    return await asyncio.to_thread(
        wiki_core.list_archived_wiki_sources,
        workspace.workspace_dir,
    )


@router.post(
    "/wiki/sources/archived/{archive_id}/restore",
    response_model=WikiSourceInfo,
    summary="Restore a source removed from the knowledge base",
)
async def restore_archived_wiki_source(
    archive_id: str,
    request: Request,
) -> WikiSourceInfo:
    try:
        workspace = await get_agent_for_request(request)
        return await asyncio.to_thread(
            wiki_core.restore_archived_wiki_source,
            workspace.workspace_dir,
            archive_id,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/wiki/trash",
    response_model=list[WikiTrashItem],
    summary="List recoverable knowledge pages and sources",
)
async def list_wiki_trash(request: Request) -> list[WikiTrashItem]:
    workspace = await get_agent_for_request(request)
    return await asyncio.to_thread(wiki_core.list_wiki_trash, workspace.workspace_dir)


@router.post(
    "/wiki/trash/{archive_id}/restore",
    response_model=dict,
    summary="Restore a page or source from the recycle bin",
)
async def restore_wiki_trash_item(archive_id: str, request: Request) -> dict:
    try:
        workspace = await get_agent_for_request(request)
        return await asyncio.to_thread(
            wiki_core.restore_wiki_trash_item,
            workspace.workspace_dir,
            archive_id,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.delete(
    "/wiki/trash/{archive_id}",
    response_model=dict,
    summary="Permanently delete one recycle-bin item",
)
async def purge_wiki_trash_item(archive_id: str, request: Request) -> dict:
    try:
        workspace = await get_agent_for_request(request)
        return await asyncio.to_thread(
            wiki_core.purge_wiki_trash_item,
            workspace.workspace_dir,
            archive_id,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/wiki/collections",
    response_model=list[WikiCollection],
    summary="List knowledge collections",
)
async def list_wiki_collections(request: Request) -> list[WikiCollection]:
    workspace = await get_agent_for_request(request)
    return await asyncio.to_thread(
        wiki_core.list_wiki_collections,
        workspace.workspace_dir,
    )


@router.post(
    "/wiki/collections",
    response_model=WikiCollection,
    summary="Create a knowledge collection",
)
async def create_wiki_collection(
    body: WikiCollectionCreateRequest,
    request: Request,
) -> WikiCollection:
    workspace = await get_agent_for_request(request)
    return await asyncio.to_thread(
        wiki_core.create_wiki_collection,
        workspace.workspace_dir,
        body,
    )


@router.patch(
    "/wiki/collections/{collection_id}",
    response_model=WikiCollection,
    summary="Update a knowledge collection",
)
async def update_wiki_collection(
    collection_id: str,
    body: WikiCollectionUpdateRequest,
    request: Request,
) -> WikiCollection:
    workspace = await get_agent_for_request(request)
    return await asyncio.to_thread(
        wiki_core.update_wiki_collection,
        workspace.workspace_dir,
        collection_id,
        body,
    )


@router.delete(
    "/wiki/collections/{collection_id}",
    response_model=dict,
    summary="Delete a collection without deleting its sources",
)
async def delete_wiki_collection(collection_id: str, request: Request) -> dict:
    workspace = await get_agent_for_request(request)
    return await asyncio.to_thread(
        wiki_core.delete_wiki_collection,
        workspace.workspace_dir,
        collection_id,
    )


@router.post(
    "/wiki/sources/upload",
    response_model=dict,
    summary="Import wiki source files",
)
async def upload_wiki_source(
    request: Request,
    file: UploadFile = File(...),
) -> dict:
    """Import a source file or zip and generate public wiki pages."""
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded source is empty")
    try:
        workspace = await get_agent_for_request(request)
        return await asyncio.to_thread(
            _import_wiki_source_upload,
            workspace.workspace_dir,
            file.filename or "source.txt",
            data,
            workspace.agent_id,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post(
    "/wiki/sources/url",
    response_model=dict,
    summary="Import a web page URL as a wiki source",
)
async def import_wiki_source_url(
    body: WikiUrlImportRequest,
    request: Request,
) -> dict:
    """Fetch a user-provided URL and preserve it as a raw wiki source."""
    try:
        workspace = await get_agent_for_request(request)
        return await asyncio.to_thread(
            wiki_core.import_wiki_url,
            workspace.workspace_dir,
            body,
            workspace.agent_id,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/wiki/search",
    response_model=WikiSearchResponse,
    summary="Search public wiki pages and sources",
)
async def search_wiki(
    request: Request,
    q: str = Query(default="", max_length=200),
) -> WikiSearchResponse:
    """Search local wiki pages and imported source text."""
    query = q.strip()
    if not query:
        return WikiSearchResponse(query=query, hits=[])

    try:
        workspace = await get_agent_for_request(request)
        return await asyncio.to_thread(
            wiki_core.search_wiki,
            workspace.workspace_dir,
            query,
            50,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/wiki/graph",
    response_model=dict,
    summary="Read the public wiki graph",
)
async def get_wiki_graph(request: Request) -> dict:
    """Return wiki graph nodes and edges."""
    try:
        workspace = await get_agent_for_request(request)
        data = await asyncio.to_thread(_collect_wiki_data, workspace.workspace_dir)
        return data.graph
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/wiki/lint",
    response_model=WikiLintResponse,
    summary="Lint public wiki pages",
)
async def lint_wiki(request: Request) -> WikiLintResponse:
    """Find missing titles, missing summaries, and broken wiki links."""
    try:
        workspace = await get_agent_for_request(request)
        return await asyncio.to_thread(_lint_wiki, workspace.workspace_dir)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/wiki/insights",
    response_model=WikiInsightsResponse,
    summary="Read actionable wiki insights",
)
async def get_wiki_insights(request: Request) -> WikiInsightsResponse:
    """Return graph and traceability insights for the public wiki."""
    try:
        workspace = await get_agent_for_request(request)
        return await asyncio.to_thread(_build_wiki_insights, workspace.workspace_dir)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/wiki/schema",
    response_model=WikiSchemaResponse,
    summary="Read extracted wiki schema",
)
async def get_wiki_schema(request: Request) -> WikiSchemaResponse:
    """Return structural page, field, heading, and source schema."""
    try:
        workspace = await get_agent_for_request(request)
        return await asyncio.to_thread(_build_wiki_schema, workspace.workspace_dir)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post(
    "/wiki/ingest/run",
    response_model=dict,
    summary="Run the local wiki ingest queue",
)
async def run_wiki_ingest(
    body: WikiIngestRunRequest,
    request: Request,
) -> dict:
    """Run local Wiki ingest plus non-blocking fact-graph shadow work."""
    try:
        workspace = await get_agent_for_request(request)
        from ..wiki.fact_ingest import run_wiki_ingest_with_fact_graph

        return await run_wiki_ingest_with_fact_graph(
            workspace.workspace_dir,
            agent_id=workspace.agent_id,
            request=body,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/wiki/ingest/status",
    response_model=wiki_core.WikiQueueSummary,
    summary="Read the local wiki ingest queue status",
)
async def get_wiki_ingest_status(request: Request) -> wiki_core.WikiQueueSummary:
    """Return queue counters and recent ingest events."""
    try:
        workspace = await get_agent_for_request(request)
        return await asyncio.to_thread(wiki_core.queue_summary, workspace.workspace_dir)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/wiki/review",
    response_model=WikiReviewResponse,
    summary="List wiki review items",
)
async def get_wiki_review(request: Request) -> WikiReviewResponse:
    """Return pending and resolved wiki review items."""
    try:
        workspace = await get_agent_for_request(request)
        return await asyncio.to_thread(wiki_core.list_review_items, workspace.workspace_dir)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post(
    "/wiki/review/{item_id}/resolve",
    response_model=WikiReviewItem,
    summary="Resolve a wiki review item",
)
async def resolve_wiki_review(
    item_id: str,
    body: WikiReviewResolveRequest,
    request: Request,
) -> WikiReviewItem:
    """Mark a wiki review item as accepted, skipped, or resolved."""
    try:
        workspace = await get_agent_for_request(request)
        return await asyncio.to_thread(
            wiki_core.resolve_review_item,
            workspace.workspace_dir,
            item_id,
            body,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post(
    "/wiki/query",
    response_model=WikiQueryResponse,
    summary="Build a wiki-backed query context",
)
async def query_wiki(body: WikiQueryRequest, request: Request) -> WikiQueryResponse:
    """Search the wiki and assemble cited local context for an answer."""
    try:
        workspace = await get_agent_for_request(request)
        return await asyncio.to_thread(
            wiki_core.build_wiki_query,
            workspace.workspace_dir,
            body,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/wiki/graph/neighbors",
    response_model=dict,
    summary="Read neighboring wiki graph nodes",
)
async def get_wiki_graph_neighbors(
    request: Request,
    node: str = Query(..., min_length=1, max_length=300),
    depth: int = Query(default=1, ge=1, le=3),
) -> dict:
    """Return a small neighborhood around a wiki page or source node."""
    try:
        workspace = await get_agent_for_request(request)
        return await asyncio.to_thread(
            wiki_core.graph_neighbors,
            workspace.workspace_dir,
            node,
            depth,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post(
    "/wiki/save-query",
    response_model=dict,
    summary="Save a chat answer into the wiki",
)
async def save_query_answer_to_wiki(
    request: Request,
    title: str = Body(..., embed=True, min_length=1, max_length=120),
    content: str = Body(..., embed=True, min_length=1),
) -> dict:
    """Persist a valuable chat answer as a query knowledge page."""
    try:
        workspace = await get_agent_for_request(request)
        return await asyncio.to_thread(
            wiki_core.save_query_to_wiki,
            workspace.workspace_dir,
            title,
            content,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/memory",
    response_model=list[MdFileInfo],
    summary="List memory files",
    description="List all memory files (uses active agent)",
)
async def list_memory_files(
    request: Request,
) -> list[MdFileInfo]:
    """List memory directory markdown files."""
    try:
        workspace = await get_agent_for_request(request)
        workspace_manager = AgentMdManager(
            str(workspace.workspace_dir),
            agent_id=workspace.agent_id,
        )
        files = [
            MdFileInfo.model_validate(file)
            for file in workspace_manager.list_memory_mds()
        ]
        return files
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/memory/{md_name}",
    response_model=MdFileContent,
    summary="Read a memory file",
    description="Read a memory markdown file (uses active agent)",
)
async def read_memory_file(
    md_name: str,
    request: Request,
) -> MdFileContent:
    """Read a memory directory markdown file."""
    try:
        workspace = await get_agent_for_request(request)
        workspace_manager = AgentMdManager(
            str(workspace.workspace_dir),
            agent_id=workspace.agent_id,
        )
        content = workspace_manager.read_memory_md(md_name)
        return MdFileContent(content=content)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.put(
    "/memory/{md_name}",
    response_model=dict,
    summary="Write a memory file",
    description="Create or update a memory file (uses active agent)",
)
async def write_memory_file(
    md_name: str,
    body: MdFileContent,
    request: Request,
) -> dict:
    """Write a memory directory markdown file."""
    try:
        workspace = await get_agent_for_request(request)
        workspace_manager = AgentMdManager(
            str(workspace.workspace_dir),
            agent_id=workspace.agent_id,
        )
        workspace_manager.write_memory_md(md_name, body.content)
        return {"written": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/memory-clear/preview",
    response_model=dict,
    summary="Preview a complete long-term memory deletion",
)
async def preview_memory_clear(request: Request) -> dict:
    """Count deletable memory artifacts without changing the workspace."""

    try:
        workspace = await get_agent_for_request(request)
        return await asyncio.to_thread(
            memory_clear_preview,
            workspace.workspace_dir,
            agent_id=workspace.agent_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post(
    "/memory-clear",
    response_model=dict,
    summary="Clear all long-term memories while preserving chats",
)
async def clear_memory(
    body: MemoryClearRequest,
    request: Request,
) -> dict:
    """Back up and clear every durable memory layer and retrieval projection."""

    manager = None
    clear_started = False
    try:
        workspace = await get_agent_for_request(request)
        manager = getattr(workspace, "memory_manager", None)
        quiesce = getattr(manager, "quiesce_capture_for_clear", None)
        if callable(quiesce):
            await quiesce()
            clear_started = True
        result = await asyncio.to_thread(
            clear_all_long_term_memory,
            workspace.workspace_dir,
            confirmation=body.confirmation,
            create_backup=body.create_backup,
            agent_id=workspace.agent_id,
        )
        schedule_agent_reload(request, workspace.agent_id)
        return result
    except ValueError as exc:
        if clear_started:
            resume = getattr(manager, "resume_capture_after_failed_clear", None)
            if callable(resume):
                resume()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        if clear_started:
            resume = getattr(manager, "resume_capture_after_failed_clear", None)
            if callable(resume):
                resume()
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _get_memory_dreaming_service(workspace) -> MemoryDreamingService:
    return MemoryDreamingService(
        workspace.workspace_dir,
        agent_id=workspace.agent_id,
        ledger=_get_memory_ledger(workspace),
    )


@router.get(
    "/memory-dreaming",
    response_model=dict,
    summary="Read governed memory Dreaming status and recent runs",
)
async def get_memory_dreaming(request: Request) -> dict:
    workspace = await get_agent_for_request(request)
    config = load_agent_config(workspace.agent_id)
    dream_config = config.running.reme_light_memory_config
    service = _get_memory_dreaming_service(workspace)
    runs = await asyncio.to_thread(service.list_runs, limit=10)
    diary_excerpt = ""
    if service.diary_path.exists():
        try:
            diary_excerpt = service.diary_path.read_text(encoding="utf-8")[-6000:]
        except OSError:
            diary_excerpt = ""
    cron_manager = getattr(workspace, "cron_manager", None)
    schedule_state = (
        cron_manager.get_dream_schedule_state()
        if cron_manager is not None
        and hasattr(cron_manager, "get_dream_schedule_state")
        else {"scheduled": False, "next_run_at": ""}
    )
    return {
        "enabled": bool(getattr(dream_config, "dreaming_enabled", False)),
        "schedule": str(getattr(dream_config, "dream_cron", "0 3 * * *")),
        "rem_enabled": bool(
            getattr(dream_config, "dreaming_rem_enabled", True),
        ),
        "min_score": float(
            getattr(dream_config, "dreaming_min_score", 0.68),
        ),
        "latest_run": runs[0] if runs else None,
        "runs": runs,
        "scheduled": bool(schedule_state.get("scheduled")),
        "next_run_at": str(schedule_state.get("next_run_at") or ""),
        "diary_path": str(service.diary_path),
        "diary_excerpt": diary_excerpt,
        "safety": {
            "confirmed_only_injection": True,
            "assistant_and_tool_evidence_blocked": True,
            "diary_is_promotion_source": False,
        },
    }


@router.put(
    "/memory-dreaming",
    response_model=dict,
    summary="Update governed memory Dreaming preferences",
)
async def update_memory_dreaming(
    body: MemoryDreamingPreferencesUpdate,
    request: Request,
) -> dict:
    workspace = await get_agent_for_request(request)
    try:
        parts = [part for part in body.schedule.split() if part]
        if len(parts) != 5:
            raise ValueError("Dreaming schedule must use five cron fields")
        CronTrigger.from_crontab(body.schedule)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail="Dreaming schedule is not a valid five-field cron expression",
        ) from exc

    config = load_agent_config(workspace.agent_id)
    dream_config = config.running.reme_light_memory_config
    dream_config.dreaming_enabled = body.enabled
    dream_config.dream_cron = body.schedule
    dream_config.dreaming_rem_enabled = body.rem_enabled
    dream_config.dreaming_min_score = body.min_score
    save_agent_config(workspace.agent_id, config)

    cron_manager = getattr(workspace, "cron_manager", None)
    if cron_manager is not None:
        await cron_manager.reschedule_dream()
    return {
        "enabled": dream_config.dreaming_enabled,
        "schedule": dream_config.dream_cron,
        "rem_enabled": dream_config.dreaming_rem_enabled,
        "min_score": dream_config.dreaming_min_score,
    }


@router.post(
    "/memory-dreaming/run",
    response_model=dict,
    summary="Run governed memory Dreaming now",
)
async def run_memory_dreaming(
    body: MemoryDreamingRunRequest,
    request: Request,
) -> dict:
    workspace = await get_agent_for_request(request)
    memory_manager = getattr(workspace, "memory_manager", None)
    if memory_manager is not None and hasattr(memory_manager, "dream"):
        return await memory_manager.dream(
            trigger="manual",
            shadow=body.shadow,
        )
    config = load_agent_config(workspace.agent_id)
    dream_config = config.running.reme_light_memory_config
    service = _get_memory_dreaming_service(workspace)
    return await asyncio.to_thread(
        service.run,
        trigger="manual",
        shadow=body.shadow,
        min_score=float(
            getattr(dream_config, "dreaming_min_score", 0.68),
        ),
        rem_enabled=bool(
            getattr(dream_config, "dreaming_rem_enabled", True),
        ),
    )


@router.post(
    "/memory-dreaming/runs/{run_id}/rollback",
    response_model=dict,
    summary="Rollback untouched staging changes from one Dreaming run",
)
async def rollback_memory_dreaming(run_id: str, request: Request) -> dict:
    workspace = await get_agent_for_request(request)
    try:
        return await asyncio.to_thread(
            _get_memory_dreaming_service(workspace).rollback,
            run_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/memory-imports/prompt",
    response_model=dict,
    summary="Read the provider-neutral memory migration prompt",
)
async def read_memory_import_prompt() -> dict:
    """Return the prompt copied into another AI; it contains no user data."""

    return get_memory_import_prompt()


@router.get(
    "/memory-pack/export",
    summary="Export a checksummed portable memory pack",
)
async def export_portable_memory_pack(request: Request) -> FileResponse:
    """Export durable memory sources without chats, secrets, or runtime data."""

    workspace = await get_agent_for_request(request)
    fd, raw_path = tempfile.mkstemp(suffix=".lantai-memory.zip")
    os.close(fd)
    path = Path(raw_path)
    try:
        await asyncio.to_thread(
            export_memory_pack,
            workspace.workspace_dir,
            path,
            agent_id=workspace.agent_id,
        )
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return FileResponse(
        path,
        media_type="application/zip",
        filename=f"{workspace.agent_id}.lantai-memory.zip",
        background=BackgroundTask(path.unlink, missing_ok=True),
    )


@router.post(
    "/memory-pack/inspect",
    response_model=dict,
    summary="Verify and inspect a portable memory pack",
)
async def inspect_portable_memory_pack(
    file: UploadFile = File(...),
) -> dict:
    """Run integrity, bounds, schema, and category checks without writes."""

    path: Path | None = None
    try:
        path = await _save_memory_pack_upload(file)
        report = await asyncio.to_thread(inspect_memory_pack, path)
        return report.model_dump(mode="json")
    except MemoryPackError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


@router.post(
    "/memory-pack/adopt",
    response_model=dict,
    summary="Dry-run or adopt a verified portable memory pack",
)
async def adopt_portable_memory_pack(
    request: Request,
    file: UploadFile = File(...),
    dry_run: bool = Query(default=True),
    confirmation: str = Query(default="", max_length=32),
) -> dict:
    """Adopt with explicit confirmation and an automatic recovery pack."""

    path: Path | None = None
    manager = None
    quiesced = False
    adopted = False
    try:
        workspace = await get_agent_for_request(request)
        path = await _save_memory_pack_upload(file)
        manager = getattr(workspace, "memory_manager", None)
        if not dry_run:
            quiesce = getattr(manager, "quiesce_capture_for_clear", None)
            if callable(quiesce):
                await quiesce()
                quiesced = True
        report = await asyncio.to_thread(
            adopt_memory_pack,
            path,
            workspace.workspace_dir,
            agent_id=workspace.agent_id,
            dry_run=dry_run,
            confirmation=confirmation,
        )
        if report.adopted:
            adopted = True
            schedule_agent_reload(request, workspace.agent_id)
        return report.model_dump(mode="json")
    except MemoryPackError as exc:
        if quiesced and not adopted:
            resume = getattr(manager, "resume_capture_after_failed_clear", None)
            if callable(resume):
                resume()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        if quiesced and not adopted:
            resume = getattr(manager, "resume_capture_after_failed_clear", None)
            if callable(resume):
                resume()
        raise
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


@router.post(
    "/memory-imports/preview",
    response_model=dict,
    summary="Parse and classify a pasted memory export locally",
)
async def preview_imported_memories(
    body: MemoryImportPreviewRequest,
    request: Request,
) -> dict:
    """Create a review batch without writing anything into active memory."""

    try:
        workspace = await get_agent_for_request(request)
        return await asyncio.to_thread(
            preview_memory_import,
            workspace.workspace_dir,
            text=body.text,
            provider=body.provider,
            agent_id=workspace.agent_id,
        )
    except MemoryImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post(
    "/memory-imports/{batch_id}/commit",
    response_model=dict,
    summary="Commit reviewed memories from one migration batch",
)
async def commit_imported_memories(
    batch_id: str,
    body: MemoryImportCommitRequest,
    request: Request,
) -> dict:
    """Write only the candidates explicitly selected in the preview."""

    try:
        workspace = await get_agent_for_request(request)
        result = await asyncio.to_thread(
            commit_memory_import,
            workspace.workspace_dir,
            batch_id=batch_id,
            selected_ids=body.selected_ids,
            agent_id=workspace.agent_id,
        )
        schedule_agent_reload(request, workspace.agent_id)
        return result
    except MemoryImportNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MemoryImportConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MemoryImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/memory-imports/recent",
    response_model=dict,
    summary="Read the latest committed or undone memory migration batch",
)
async def read_recent_memory_import(request: Request) -> dict:
    workspace = await get_agent_for_request(request)
    return await asyncio.to_thread(
        get_recent_memory_import,
        workspace.workspace_dir,
        agent_id=workspace.agent_id,
    )


@router.post(
    "/memory-imports/{batch_id}/undo",
    response_model=dict,
    summary="Undo one reviewed memory migration batch",
)
async def undo_imported_memories(batch_id: str, request: Request) -> dict:
    """Remove unchanged imported projections and forget their canonical facts."""

    try:
        workspace = await get_agent_for_request(request)
        result = await asyncio.to_thread(
            undo_memory_import,
            workspace.workspace_dir,
            batch_id=batch_id,
            agent_id=workspace.agent_id,
        )
        schedule_agent_reload(request, workspace.agent_id)
        return result
    except MemoryImportNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MemoryImportConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MemoryImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/user-memory",
    response_model=dict,
    summary="List structured user memories",
    description="Read structured user profile memories for the active agent.",
)
async def list_user_memory(
    request: Request,
    include_archived: bool = False,
) -> dict:
    """Return structured user memory snapshot."""
    try:
        workspace = await get_agent_for_request(request)
        await asyncio.to_thread(
            migrate_memory_governance,
            workspace.workspace_dir,
            apply=True,
            agent_id=workspace.agent_id,
        )
        store = _get_user_memory_store(workspace)
        return store.snapshot(include_archived=include_archived)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/user-understanding",
    response_model=dict,
    summary="Read the sparse, auditable user-understanding projection",
)
async def list_user_understanding(request: Request) -> dict:
    """Separate known facts, working guesses and current open loops."""

    workspace = await get_agent_for_request(request)
    atoms = _get_user_understanding_store(workspace).list_atoms()
    active = {"observed", "working_hypothesis", "confirmed"}
    confirmed = [
        item.to_dict()
        for item in atoms
        if item.status == "confirmed"
        and item.kind not in {"open_loop", "prospective_intent"}
    ]
    hypotheses = [
        item.to_dict()
        for item in atoms
        if item.status == "working_hypothesis"
        and item.kind not in {"open_loop", "prospective_intent"}
    ]
    open_loops = [
        item.to_dict()
        for item in atoms
        if item.status in active
        and item.kind in {"open_loop", "prospective_intent"}
    ]
    return {
        "confirmed": confirmed,
        "hypotheses": hypotheses,
        "open_loops": open_loops,
        "stats": {
            "confirmed": len(confirmed),
            "hypotheses": len(hypotheses),
            "open_loops": len(open_loops),
            "retired": sum(1 for item in atoms if item.status not in active),
        },
    }


@router.post(
    "/user-understanding/{atom_id}/feedback",
    response_model=dict,
    summary="Confirm, correct or ignore one understanding atom",
)
async def feedback_user_understanding(
    atom_id: str,
    body: UserUnderstandingFeedback,
    request: Request,
) -> dict:
    workspace = await get_agent_for_request(request)
    atom = _get_user_understanding_store(workspace).apply_feedback(
        atom_id,
        body.verdict,  # type: ignore[arg-type]
    )
    if atom is None:
        raise HTTPException(status_code=404, detail="Understanding atom not found")
    if atom.status in {"contradicted", "superseded", "expired"}:
        await _invalidate_brief_understanding_dependency(workspace, atom.id)
    return {"updated": True, "atom": atom.to_dict()}


@router.delete(
    "/user-understanding/{atom_id}",
    response_model=dict,
    summary="Delete one disposable understanding atom",
)
async def delete_user_understanding(atom_id: str, request: Request) -> dict:
    workspace = await get_agent_for_request(request)
    deleted = _get_user_understanding_store(workspace).delete_atom(atom_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Understanding atom not found")
    await _invalidate_brief_understanding_dependency(workspace, atom_id)
    return {"deleted": True}


@router.get(
    "/proactive-opportunities",
    response_model=dict,
    summary="List evidence-linked proactive opportunities",
)
async def list_proactive_opportunities(request: Request) -> dict:
    workspace = await get_agent_for_request(request)
    items = _get_opportunity_store(workspace).list_all()
    return {
        "opportunities": [item.to_dict() for item in items],
        "stats": {
            status: sum(1 for item in items if item.status == status)
            for status in ("queued", "completed", "dismissed", "expired")
        },
    }


@router.post(
    "/proactive-opportunities/{opportunity_id}/feedback",
    response_model=dict,
    summary="Complete, ignore or dismiss a proactive opportunity",
)
async def feedback_proactive_opportunity(
    opportunity_id: str,
    body: ProactiveOpportunityFeedback,
    request: Request,
) -> dict:
    workspace = await get_agent_for_request(request)
    item = _get_opportunity_store(workspace).apply_feedback(
        opportunity_id,
        body.verdict,  # type: ignore[arg-type]
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Proactive opportunity not found")
    return {"updated": True, "opportunity": item.to_dict()}


@router.get(
    "/memory-facts",
    response_model=dict,
    summary="List canonical memory facts and suggestions",
)
async def list_memory_facts(
    request: Request,
    status: str = "pending",
    limit: int = Query(default=5, ge=1, le=50),
) -> dict:
    """Return canonical facts; the suggestion queue is hard-capped at five."""

    try:
        workspace = await get_agent_for_request(request)
        ledger = _get_memory_ledger(workspace)
        requested = {
            item.strip()
            for item in status.split(",")
            if item.strip()
        }
        if not requested or requested == {"pending"}:
            facts = ledger.list_pending(limit=min(limit, 5))
        else:
            facts = ledger.list_facts(statuses=requested, limit=limit)
        all_facts = ledger.list_facts()
        return {
            "facts": [fact.to_dict() for fact in facts],
            "stats": {
                name: sum(1 for fact in all_facts if fact.status == name)
                for name in (
                    "observed",
                    "pending",
                    "confirmed",
                    "conflict",
                    "rejected",
                    "expired",
                )
            },
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/memory-facts/{fact_id}/sources",
    response_model=dict,
    summary="Open the original evidence for a memory fact",
)
async def open_memory_fact_sources(fact_id: str, request: Request) -> dict:
    """Resolve fact evidence only within the active agent's workspace."""

    try:
        workspace = await get_agent_for_request(request)
        result = _get_memory_ledger(workspace).open_fact_sources(fact_id)
        if not result["found"]:
            raise HTTPException(status_code=404, detail="Memory fact not found")
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.delete(
    "/memory-facts/{fact_id}/sources/{event_id}",
    response_model=dict,
    summary="Delete an original memory source and invalidate unsupported projections",
)
async def delete_memory_fact_source(fact_id: str, event_id: str, request: Request) -> dict:
    """Delete only a source attached to this fact in the active workspace."""

    try:
        workspace = await get_agent_for_request(request)
        ledger = _get_memory_ledger(workspace)
        fact = ledger.get_fact(fact_id)
        if fact is None or event_id not in fact.event_ids:
            raise HTTPException(status_code=404, detail="Memory source not found")
        result = ledger.delete_source_event(event_id)
        if not result["deleted"]:
            raise HTTPException(status_code=404, detail="Memory source not found")
        store = _get_user_memory_store(workspace)
        layered = _get_layered_memory_store(workspace)
        for affected in result["affected_facts"]:
            if affected["status"] == "forgotten":
                for entry_id in affected["legacy_entry_ids"]:
                    store.delete_entry(entry_id)
            elif affected["status"] == "pending":
                for entry_id in affected["legacy_entry_ids"]:
                    store.archive_entry(entry_id)
            if affected["status"] != "confirmed":
                layered.set_atom_status(affected["atom_ids"], "archived")
        if event_id.startswith("event_l0_"):
            layered.purge_source_record(event_id.removeprefix("event_"))
        manager = getattr(workspace, "memory_manager", None)
        understanding = getattr(manager, "user_understanding_store", None)
        if understanding is not None:
            understanding.invalidate_source(event_id)
            for affected in result["affected_facts"]:
                if affected["status"] != "confirmed":
                    understanding.invalidate_source(affected["fact_id"])
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/memory-preferences",
    response_model=dict,
    summary="Read memory runtime preferences",
)
async def get_memory_preferences(request: Request) -> dict:
    workspace = await get_agent_for_request(request)
    return _get_memory_ledger(workspace).get_preferences()


@router.get(
    "/memory-recall/last",
    response_model=dict,
    summary="Read the latest answer memory usage trace",
)
async def get_last_memory_recall(request: Request) -> dict:
    """Expose the latest readable memory retrieval trace without inventing zeroes."""

    workspace = await get_agent_for_request(request)
    recall_path = _get_memory_ledger(workspace).root / "last-recall.json"
    if not recall_path.exists():
        return {
            "query": "",
            "updated_at": "",
            "items": [],
            "status": "unavailable",
            "reason": "no_record",
        }
    try:
        payload = json.loads(recall_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {
            "query": "",
            "updated_at": "",
            "items": [],
            "status": "error",
            "reason": "read_failed",
        }
    if not isinstance(payload, dict) or not isinstance(payload.get("items", []), list):
        return {
            "query": "",
            "updated_at": "",
            "items": [],
            "status": "error",
            "reason": "read_failed",
        }
    items = payload.get("items", [])
    return {
        **payload,
        "query": str(payload.get("query") or ""),
        "updated_at": str(payload.get("updated_at") or ""),
        "items": items,
        "status": "ready" if items else "empty",
        "reason": "",
    }


@router.put(
    "/memory-preferences",
    response_model=dict,
    summary="Pause or resume long-term memory",
)
async def update_memory_preferences(
    body: MemoryPreferencesUpdate,
    request: Request,
) -> dict:
    workspace = await get_agent_for_request(request)
    return _get_memory_ledger(workspace).set_preferences(
        runtime_enabled=body.runtime_enabled,
        capture_enabled=body.capture_enabled,
    )


@router.post(
    "/memory-facts/{fact_id}/confirm",
    response_model=dict,
    summary="Confirm a canonical memory suggestion",
)
async def confirm_memory_fact(
    fact_id: str,
    body: MemoryFactAction,
    request: Request,
) -> dict:
    """Confirm a fact and rebuild its structured/L1 projections."""

    try:
        workspace = await get_agent_for_request(request)
        ledger = _get_memory_ledger(workspace)
        resolved_scope_id = body.scope_id
        if body.scope == "project" and not resolved_scope_id:
            resolved_scope_id = f"workspace:{workspace.agent_id}"
        fact = ledger.confirm(
            fact_id,
            scope=body.scope,
            scope_id=resolved_scope_id,
        )
        if fact is None:
            raise HTTPException(status_code=404, detail="Memory fact not found")
        store = _get_user_memory_store(workspace)
        decision = store.add_entry(
            content=fact.content,
            memory_type=fact.memory_type,
            source="agent",
            confidence=max(fact.confidence, 0.95),
            reason=fact.decision_reason or "用户确认",
            evidence=f"canonical:{fact.id}",
            scope=fact.scope,
            scope_id=fact.scope_id,
        )
        entry_payload = decision.get("entry") if isinstance(decision, dict) else None
        if isinstance(entry_payload, dict):
            entry_id = str(entry_payload.get("id") or "")
            ledger.link_legacy_entry(fact.id, entry_id)
            store.mark_confirmed(
                entry_id,
                canonical_fact_id=fact.id,
                scope=fact.scope,
                scope_id=fact.scope_id,
            )
        _get_layered_memory_store(workspace).set_atom_status(fact.atom_ids, "active")
        return {"confirmed": True, "fact": fact.to_dict(), "projection": decision}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post(
    "/memory-facts/{fact_id}/reject",
    response_model=dict,
    summary="Reject a canonical memory suggestion",
)
async def reject_memory_fact(
    fact_id: str,
    body: MemoryFactAction,
    request: Request,
) -> dict:
    """Reject a suggestion and archive its disposable L1 projection."""

    try:
        workspace = await get_agent_for_request(request)
        ledger = _get_memory_ledger(workspace)
        fact = ledger.reject(fact_id, reason=body.reason or "用户忽略")
        if fact is None:
            raise HTTPException(status_code=404, detail="Memory fact not found")
        _get_layered_memory_store(workspace).set_atom_status(fact.atom_ids, "archived")
        return {"rejected": True, "fact": fact.to_dict()}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post(
    "/user-memory",
    response_model=dict,
    summary="Create structured user memory",
    description="Manually add a durable user memory to the active agent.",
)
async def create_user_memory(
    body: UserMemoryCreate,
    request: Request,
) -> dict:
    """Create a structured user memory entry."""
    try:
        workspace = await get_agent_for_request(request)
        store = _get_user_memory_store(workspace)
        memory_type = body.type or infer_memory_type(body.content)
        if memory_type not in MEMORY_TYPES:
            raise HTTPException(
                status_code=400,
                detail="Invalid memory type",
            )
        decision = store.add_entry(
            content=body.content,
            memory_type=memory_type,
            source="manual",
            confidence=body.confidence,
            reason=body.reason or "用户手动补充",
        )
        ledger = _get_memory_ledger(workspace)
        event = ledger.record_event(
            body.content,
            source_message_id=f"manual-memory:{datetime.now(timezone.utc).timestamp()}",
            source_role="manual",
        )
        fact = (
            ledger.capture_fact(
                body.content,
                memory_type=memory_type,
                event=event,
                explicit_user_request=True,
                confidence=body.confidence,
            )
            if event is not None
            else None
        )
        entry_payload = decision.get("entry") if isinstance(decision, dict) else None
        if fact is not None and isinstance(entry_payload, dict):
            entry_id = str(entry_payload.get("id") or "")
            ledger.link_legacy_entry(fact.id, entry_id)
            confirmed = store.mark_confirmed(
                entry_id,
                canonical_fact_id=fact.id,
                scope=fact.scope,
                scope_id=fact.scope_id,
            )
            if confirmed is not None:
                decision["entry"] = confirmed.to_dict()
            decision["ledger_fact_id"] = fact.id
        return decision
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.put(
    "/user-memory/{entry_id}",
    response_model=dict,
    summary="Update structured user memory",
    description="Edit content, category, or status for a user memory.",
)
async def update_user_memory(
    entry_id: str,
    body: UserMemoryUpdate,
    request: Request,
) -> dict:
    """Update a structured user memory entry."""
    try:
        workspace = await get_agent_for_request(request)
        store = _get_user_memory_store(workspace)
        if body.type is not None and body.type not in MEMORY_TYPES:
            raise HTTPException(
                status_code=400,
                detail="Invalid memory type",
            )
        entry = store.update_entry(
            entry_id=entry_id,
            content=body.content,
            memory_type=body.type,
            status=body.status,
            confidence=body.confidence,
        )
        if entry is None:
            raise HTTPException(status_code=404, detail="Memory not found")
        if entry.confirmed:
            ledger = _get_memory_ledger(workspace)
            old_facts = [
                fact for fact in ledger.list_facts()
                if entry_id in fact.legacy_entry_ids
            ]
            event = ledger.record_event(
                entry.content,
                source_message_id=f"memory-edit:{entry_id}",
                source_role="manual",
            )
            if event is not None:
                replacement = ledger.capture_fact(
                    entry.content,
                    memory_type=entry.type,
                    event=event,
                    explicit_user_request=True,
                    confidence=entry.confidence,
                )
                if replacement is not None:
                    ledger.link_legacy_entry(replacement.id, entry_id)
                    store.mark_confirmed(
                        entry_id,
                        canonical_fact_id=replacement.id,
                        scope=replacement.scope,
                        scope_id=replacement.scope_id,
                    )
                    for old_fact in old_facts:
                        if old_fact.id != replacement.id:
                            ledger.supersede(old_fact.id, replacement.id)
            await _invalidate_user_understanding_sources(
                workspace,
                [entry_id, *(old_fact.id for old_fact in old_facts)],
            )
        return {"updated": True, "entry": entry.to_dict()}
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post(
    "/user-memory/{entry_id}/archive",
    response_model=dict,
    summary="Archive structured user memory",
)
async def archive_user_memory(entry_id: str, request: Request) -> dict:
    """Archive a structured user memory entry."""
    try:
        workspace = await get_agent_for_request(request)
        store = _get_user_memory_store(workspace)
        entry = store.archive_entry(entry_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="Memory not found")
        ledger = _get_memory_ledger(workspace)
        linked_fact_ids = [
            fact.id for fact in ledger.list_facts()
            if entry_id in fact.legacy_entry_ids
        ]
        ledger.forget_matching_content(
            entry.content,
            legacy_entry_id=entry_id,
        )
        _get_layered_memory_store(workspace).forget_projection_content(entry.content)
        await _invalidate_user_understanding_sources(
            workspace,
            [entry_id, *linked_fact_ids],
        )
        return {"archived": True, "entry": entry.to_dict()}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.delete(
    "/user-memory/{entry_id}",
    response_model=dict,
    summary="Delete structured user memory",
)
async def delete_user_memory(entry_id: str, request: Request) -> dict:
    """Permanently delete a structured user memory entry."""
    try:
        workspace = await get_agent_for_request(request)
        store = _get_user_memory_store(workspace)
        entry = next(
            (item for item in store.load_entries() if item.id == entry_id),
            None,
        )
        deleted = store.delete_entry(entry_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Memory not found")
        if entry is not None:
            ledger = _get_memory_ledger(workspace)
            linked_fact_ids = [
                fact.id for fact in ledger.list_facts()
                if entry_id in fact.legacy_entry_ids
            ]
            ledger.forget_matching_content(
                entry.content,
                legacy_entry_id=entry_id,
            )
            _get_layered_memory_store(workspace).forget_projection_content(entry.content)
            await _invalidate_user_understanding_sources(
                workspace,
                [entry_id, *linked_fact_ids],
            )
        return {"deleted": True}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/layered-memory",
    response_model=dict,
    summary="Read layered memory diagnostics",
    description="Return L0/L1/L2/L3 memory and offload diagnostics.",
)
async def get_layered_memory(request: Request) -> dict:
    """Return layered memory snapshot for the active agent."""
    try:
        workspace = await get_agent_for_request(request)
        memory_manager = getattr(workspace, "memory_manager", None)
        if memory_manager is not None and hasattr(
            memory_manager,
            "layered_snapshot",
        ):
            snapshot = memory_manager.layered_snapshot()
            if snapshot is not None:
                return snapshot
        return _get_layered_memory_store(workspace).snapshot()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/layered-memory/source/{source_id}",
    response_model=dict,
    summary="Open a layered memory source",
    description="Open an L0 source record or offloaded tool-result ref.",
)
async def open_layered_memory_source(
    source_id: str,
    request: Request,
) -> dict:
    """Open a layered memory source record."""
    try:
        workspace = await get_agent_for_request(request)
        result = _get_layered_memory_store(workspace).open_source(source_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Source not found")
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/language",
    summary="Get agent language",
    description="Get the language setting for agent MD files (en/zh/ru)",
)
async def get_agent_language(request: Request) -> dict:
    """Get agent language setting for current agent."""
    workspace = await get_agent_for_request(request)
    agent_config = load_agent_config(workspace.agent_id)
    return {
        "language": agent_config.language,
        "agent_id": workspace.agent_id,
    }


@router.put(
    "/language",
    summary="Update agent language",
    description=(
        "Update the language for agent MD files (en/zh/ru). "
        "Optionally copies MD files for the new language to agent workspace."
    ),
)
async def put_agent_language(
    request: Request,
    body: dict = Body(
        ...,
        description='Language setting, e.g. {"language": "zh"}',
    ),
) -> dict:
    """
    Update agent language and optionally re-copy MD files to agent workspace.
    """
    language = (body.get("language") or "").strip().lower()
    valid = SUPPORTED_AGENT_LANGUAGES
    if language not in valid:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid language '{language}'. "
                f"Must be one of: {', '.join(sorted(valid))}"
            ),
        )

    workspace = await get_agent_for_request(request)
    agent_id = workspace.agent_id

    agent_config = load_agent_config(agent_id)
    old_language = agent_config.language

    agent_config.language = language
    save_agent_config(agent_id, agent_config)

    copied_files: list[str] = []
    if old_language != language:
        copied_files = copy_workspace_md_files(
            language,
            workspace.workspace_dir,
            md_template_id=get_workspace_md_template_id(
                agent_config.template_id
                or ("qa" if agent_id == BUILTIN_QA_AGENT_ID else None),
            ),
            only_if_missing=False,
        )

    return {
        "language": language,
        "copied_files": copied_files,
        "agent_id": agent_id,
    }


@router.get(
    "/running-config",
    response_model=AgentsRunningConfig,
    summary="Get agent running config",
    description="Get running configuration for active agent",
)
async def get_agents_running_config(
    request: Request,
) -> AgentsRunningConfig:
    """Get agent running configuration."""
    workspace = await get_agent_for_request(request)
    agent_config = load_agent_config(workspace.agent_id)
    running = agent_config.running or AgentsRunningConfig()
    running.approval_level = getattr(agent_config, "approval_level", "AUTO")
    from ...config.ui_preferences import ququ_enabled

    running.enableQuqu = ququ_enabled()
    return running


@router.put(
    "/running-config",
    response_model=AgentsRunningConfig,
    summary="Update agent running config",
    description="Update running configuration for active agent",
)
async def put_agents_running_config(
    running_config: AgentsRunningConfig = Body(
        ...,
        description="Updated agent running configuration",
    ),
    request: Request = None,
) -> AgentsRunningConfig:
    """Update agent running configuration."""
    workspace = await get_agent_for_request(request)
    agent_config = load_agent_config(workspace.agent_id)

    if running_config.approval_level is not None:
        agent_config.approval_level = running_config.approval_level
    if running_config.enableQuqu is not None:
        from ...config.ui_preferences import set_ququ_enabled

        set_ququ_enabled(running_config.enableQuqu)

    running_config.approval_level = None
    running_config.enableQuqu = None
    agent_config.running = running_config
    save_agent_config(workspace.agent_id, agent_config)

    schedule_agent_reload(request, workspace.agent_id)

    running_config.approval_level = agent_config.approval_level
    from ...config.ui_preferences import ququ_enabled

    running_config.enableQuqu = ququ_enabled()
    return running_config


@router.get(
    "/system-prompt-files",
    response_model=list[str],
    summary="Get system prompt files",
    description="Get system prompt files for active agent",
)
async def get_system_prompt_files(
    request: Request,
) -> list[str]:
    """Get list of enabled system prompt files."""
    workspace = await get_agent_for_request(request)
    agent_config = load_agent_config(workspace.agent_id)
    return agent_config.system_prompt_files or []


@router.put(
    "/system-prompt-files",
    response_model=list[str],
    summary="Update system prompt files",
    description="Update system prompt files for active agent",
)
async def put_system_prompt_files(
    files: list[str] = Body(
        ...,
        description="Markdown filenames to load into system prompt",
    ),
    request: Request = None,
) -> list[str]:
    """Update list of enabled system prompt files."""
    workspace = await get_agent_for_request(request)
    agent_config = load_agent_config(workspace.agent_id)
    agent_config.system_prompt_files = files
    save_agent_config(workspace.agent_id, agent_config)

    schedule_agent_reload(request, workspace.agent_id)

    return files


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_zip_data(data: bytes, workspace_dir: Path) -> None:
    """Ensure *data* is a valid zip without path-traversal entries."""
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise HTTPException(
            status_code=400,
            detail="Uploaded file is not a valid zip archive",
        )
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            relative = Path(name)
            if _is_internal_runtime_relative_path(relative) or _is_private_wiki_relative_path(relative):
                raise HTTPException(
                    status_code=400,
                    detail=f"Zip contains an internal runtime path: {name}",
                )
            resolved = (workspace_dir / name).resolve()
            if not str(resolved).startswith(str(workspace_dir)):
                raise HTTPException(
                    status_code=400,
                    detail=f"Zip contains unsafe path: {name}",
                )


def _extract_and_merge_zip(data: bytes, workspace_dir: Path) -> None:
    """Extract zip data and merge into workspace_dir (blocking operation)."""
    tmp_dir = None
    try:
        tmp_dir = Path(tempfile.mkdtemp(prefix="lantai_upload_"))
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            zf.extractall(tmp_dir)

        top_entries = list(tmp_dir.iterdir())
        extract_root = tmp_dir
        if len(top_entries) == 1 and top_entries[0].is_dir():
            extract_root = top_entries[0]
        if extract_root != tmp_dir and (
            extract_root.name == LANTAI_INTERNAL_DIR
            or _is_private_wiki_relative_path(Path(extract_root.name))
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Zip contains an internal runtime directory that "
                    "cannot be imported here"
                ),
            )

        workspace_dir.mkdir(parents=True, exist_ok=True)

        for item in extract_root.iterdir():
            if item.name == LANTAI_INTERNAL_DIR or _is_private_wiki_relative_path(Path(item.name)):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Zip contains an internal runtime directory that "
                        "cannot be imported here"
                    ),
                )
            if item.is_file() and _is_internal_workspace_md(item.name):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Zip contains an internal instruction file that "
                        f"cannot be imported here: {item.name}"
                    ),
                )

            dest = workspace_dir / item.name
            if item.is_file():
                shutil.copy2(item, dest)
            else:
                if dest.exists() and dest.is_file():
                    dest.unlink()
                shutil.copytree(item, dest, dirs_exist_ok=True)
    finally:
        if tmp_dir and tmp_dir.is_dir():
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _validate_and_extract_zip(data: bytes, workspace_dir: Path) -> None:
    """Validate and extract zip data (blocking operation)."""
    _validate_zip_data(data, workspace_dir)
    _extract_and_merge_zip(data, workspace_dir)


# ---------------------------------------------------------------------------
# Workspace Download/Upload Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/download",
    summary="Download public wiki files as zip",
    description=(
        "Package public workspace files into a zip archive and stream it back "
        "as a downloadable file. Internal instruction markdown is excluded."
    ),
    responses={
        200: {
            "content": {"application/zip": {}},
            "description": "Zip archive of agent workspace",
        },
    },
)
async def download_workspace(request: Request):
    """Stream public agent workspace files as a zip file."""

    agent = await get_agent_for_request(request)
    workspace_dir = agent.workspace_dir

    if not workspace_dir.is_dir():
        raise HTTPException(
            status_code=404,
            detail=f"Workspace does not exist: {workspace_dir}",
        )

    buf = await asyncio.to_thread(_zip_directory, workspace_dir)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"lantai_workspace_{agent.agent_id}_{timestamp}.zip"

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.post(
    "/upload",
    response_model=dict,
    summary="Upload zip and merge into workspace",
    description=(
        "Upload a zip archive.  Paths present in the zip are merged into "
        "agent workspace (files overwritten, dirs merged).  Paths not in "
        "the zip are left unchanged (e.g. lantai.db, runtime dirs). "
        "Download packs the entire workspace; upload only "
        "overwrites/merges zip contents."
    ),
)
async def upload_workspace(
    request: Request,
    file: UploadFile = File(
        ...,
        description="Zip archive to merge into agent workspace",
    ),
) -> dict:
    """
    Merge uploaded zip contents into agent workspace (overwrite, not clear).
    """

    if file.content_type and file.content_type not in (
        "application/zip",
        "application/x-zip-compressed",
        "application/octet-stream",
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Expected a zip file, got content-type: {file.content_type}"
            ),
        )

    agent = await get_agent_for_request(request)
    workspace_dir = agent.workspace_dir
    data = await file.read()

    try:
        await asyncio.to_thread(_validate_and_extract_zip, data, workspace_dir)
        return {"success": True}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to merge workspace: {exc}",
        ) from exc
