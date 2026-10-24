# -*- coding: utf-8 -*-
"""Workspace and skill-pool APIs."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import shutil
import tempfile
import threading
import time
import uuid
from enum import Enum
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr
from lantai.exceptions import AppBaseException

from ...agents.skills_hub import (
    SkillImportCancelled,
    search_hub_skills,
    import_pool_skill_from_hub,
    install_skill_from_hub,
)
from ...agents.skills_manager import (
    _BUILTIN_SKILL_LANGUAGES,
    SkillConflictError,
    SkillPoolService,
    SkillInfo,
    SkillService,
    _default_pool_manifest,
    _default_workspace_manifest,
    _get_skill_mtime,
    _mutate_json,
    _normalize_skill_manifest_entry,
    _read_frontmatter_safe,
    _read_skill_from_dir,
    _extract_emoji_from_metadata,
    _extract_version,
    get_pool_builtin_update_notice,
    get_pool_builtin_sync_status,
    get_pool_skill_manifest_path,
    get_skill_pool_dir,
    get_workspace_skill_manifest_path,
    get_workspace_skills_dir,
    import_builtin_skills,
    list_builtin_import_candidates,
    list_workspaces,
    read_skill_pool_manifest,
    read_skill_manifest,
    reconcile_pool_manifest,
    reconcile_workspace_manifest,
    suggest_conflict_name,
    update_single_builtin,
)
from ...security.skill_scanner import SkillScanError
from ..experience import record_skills_changed
from ..kdocs_connection import (
    KdocsConnectionError,
    KdocsConnectionService,
    KdocsConnectionStatus,
)
from ..kdocs_documents import KdocsDocumentError, KdocsDocumentService
from ..wpsnote_connection import (
    WpsNoteConnectionError,
    WpsNoteConnectionService,
    WpsNoteConnectionStatus,
)
from ..utils import schedule_agent_reload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/skills", tags=["skills"])

MAX_TAGS = 8
MAX_TAG_LENGTH = 16


def _scan_error_payload(exc: SkillScanError) -> dict[str, Any]:
    """Normalize scanner exceptions into a stable API payload.

    Example response body:
        {
            "type": "security_scan_failed",
            "skill_name": "blocked_skill",
            "max_severity": "high",
            "findings": [...]
        }
    """
    result = exc.result
    return {
        "type": "security_scan_failed",
        "detail": str(exc),
        "skill_name": result.skill_name,
        "max_severity": result.max_severity.value,
        "findings": [
            {
                "severity": f.severity.value,
                "title": f.title,
                "description": f.description,
                "file_path": f.file_path,
                "line_number": f.line_number,
                "rule_id": f.rule_id,
            }
            for f in result.findings
        ],
    }


def _scan_error_response(exc: SkillScanError) -> JSONResponse:
    """Build a 422 JSON response for skill scan failures.

    Returns a JSONResponse so callers receive structured scan
    details rather than a bare HTTP error.
    """
    return JSONResponse(
        status_code=422,
        content=_scan_error_payload(exc),
    )


class SkillSpec(SkillInfo):
    enabled: bool = False
    channels: list[str] = Field(default_factory=lambda: ["all"])
    tags: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    last_updated: str = ""


class PoolSkillSpec(SkillInfo):
    protected: bool = False
    commit_text: str = ""
    sync_status: str = ""
    latest_version_text: str = ""
    builtin_language: str = ""
    available_builtin_languages: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    last_updated: str = ""


class SkillIndexEntry(BaseModel):
    name: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    source: str = "customized"
    enabled: bool = False
    channels: list[str] = Field(default_factory=lambda: ["all"])
    version: str = ""
    emoji: str = ""
    entry_path: str = ""
    last_updated: str = ""


class PoolSkillIndexEntry(BaseModel):
    name: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    source: str = "customized"
    enabled: bool = True
    channels: list[str] = Field(default_factory=lambda: ["all"])
    version: str = ""
    emoji: str = ""
    entry_path: str = ""
    protected: bool = False
    sync_status: str = ""
    last_updated: str = ""


class SkillContentResponse(BaseModel):
    name: str
    content: str


class KdocsConnectRequest(BaseModel):
    token: SecretStr = Field(min_length=1, max_length=8192)


class KdocsConnectionStatusResponse(BaseModel):
    installed: bool
    authenticated: bool
    cli_version: str = ""
    token_url: str


class KdocsDocumentIdentityRequest(BaseModel):
    file_id: str = Field(min_length=1, max_length=256)


class KdocsSheetRangeRequest(BaseModel):
    row_from: int | None = Field(default=None, ge=0)
    row_to: int | None = Field(default=None, ge=0)
    col_from: int | None = Field(default=None, ge=0)
    col_to: int | None = Field(default=None, ge=0)


class KdocsDocumentReadRequest(KdocsDocumentIdentityRequest):
    continuation: str = Field(default="", max_length=256)
    task_id: str = Field(default="", max_length=256)
    format: str = Field(default="markdown", max_length=16)
    max_chars: int = Field(default=500_000, ge=1, le=1_000_000)
    sheet_name: str = Field(default="", max_length=240)
    sheet_range: KdocsSheetRangeRequest | None = None


class WpsNoteConnectionStatusResponse(BaseModel):
    installed: bool
    connected: bool
    desktop_installed: bool = False
    cli_version: str = ""
    tool_count: int = 0
    install_method: str
    repository_url: str
    download_url: str
    next_step: str
    launch_requested: bool = False


class WorkspaceSkillSummary(BaseModel):
    agent_id: str
    agent_name: str = ""
    workspace_dir: str
    skills: list[SkillSpec] = Field(default_factory=list)


class HubSkillSpec(BaseModel):
    slug: str
    name: str
    description: str = ""
    version: str = ""
    source_url: str = ""


class BuiltinImportSpec(BaseModel):
    name: str
    description: str = ""
    version_text: str = ""
    current_version_text: str = ""
    current_source: str = ""
    current_language: str = ""
    available_languages: list[str] = Field(default_factory=list)
    languages: dict[str, dict[str, Any]] = Field(default_factory=dict)
    status: str = ""


class BuiltinRemovedSpec(BaseModel):
    name: str
    description: str = ""
    current_version_text: str = ""
    current_source: str = ""


class BuiltinUpdateNotice(BaseModel):
    fingerprint: str = ""
    has_updates: bool = False
    total_changes: int = 0
    actionable_skill_names: list[str] = Field(default_factory=list)
    added: list[BuiltinImportSpec] = Field(default_factory=list)
    missing: list[BuiltinImportSpec] = Field(default_factory=list)
    updated: list[BuiltinImportSpec] = Field(default_factory=list)
    removed: list[BuiltinRemovedSpec] = Field(default_factory=list)


class BuiltinImportSelection(BaseModel):
    skill_name: str
    language: str = ""


class ImportBuiltinRequest(BaseModel):
    skill_names: list[str] = Field(
        default_factory=list,
    )  # Deprecated: use imports
    imports: list[BuiltinImportSelection] = Field(default_factory=list)
    overwrite_conflicts: bool = False


class UpdateBuiltinRequest(BaseModel):
    language: str = ""


class CreateSkillRequest(BaseModel):
    name: str
    content: str
    references: dict[str, Any] | None = None
    scripts: dict[str, Any] | None = None
    config: dict[str, Any] | None = None
    enable: bool = True


class UploadToPoolRequest(BaseModel):
    workspace_id: str
    skill_name: str
    overwrite: bool = False
    preview_only: bool = False


class PoolDownloadTarget(BaseModel):
    workspace_id: str


class DownloadFromPoolRequest(BaseModel):
    skill_name: str
    targets: list[PoolDownloadTarget] = Field(default_factory=list)
    all_workspaces: bool = False
    overwrite: bool = False
    preview_only: bool = False


class SkillConfigRequest(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)


class SavePoolSkillRequest(BaseModel):
    name: str
    content: str
    source_name: str | None = None
    config: dict[str, Any] | None = None
    overwrite: bool = False


class SaveSkillRequest(BaseModel):
    name: str
    content: str
    source_name: str | None = None
    config: dict[str, Any] | None = None
    overwrite: bool = False


class HubInstallRequest(BaseModel):
    bundle_url: str = Field(..., description="Skill URL")
    version: str = Field(default="", description="Optional version tag")
    enable: bool = Field(default=True, description="Enable after import")
    target_name: str = Field(default="", description="Optional renamed skill")


class HubInstallTaskStatus(str, Enum):
    PENDING = "pending"
    IMPORTING = "importing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class HubInstallTask(BaseModel):
    task_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    bundle_url: str
    version: str = ""
    enable: bool = True
    status: HubInstallTaskStatus = HubInstallTaskStatus.PENDING
    error: str | None = None
    result: dict[str, Any] | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


_hub_install_tasks: dict[str, HubInstallTask] = {}
_hub_install_runtime_tasks: dict[str, asyncio.Task] = {}
_hub_install_cancel_events: dict[str, threading.Event] = {}
_hub_install_lock = asyncio.Lock()

_ALLOWED_ZIP_TYPES = {
    "application/zip",
    "application/x-zip-compressed",
    "application/octet-stream",
}
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB


def _workspace_dir_for_agent(agent_id: str) -> Path:
    for workspace in list_workspaces():
        if workspace["agent_id"] == agent_id:
            return Path(workspace["workspace_dir"])
    raise HTTPException(
        status_code=404,
        detail=f"Workspace '{agent_id}' not found",
    )


def _snapshot_workspace_skill(
    workspace_dir: Path,
    skill_name: str,
) -> dict[str, Any]:
    manifest = read_skill_manifest(workspace_dir)
    entry = manifest.get("skills", {}).get(skill_name)
    skill_dir = workspace_dir / "skills" / skill_name
    backup_dir: Path | None = None
    if skill_dir.exists():
        backup_root = Path(
            tempfile.mkdtemp(prefix=f"lantai_skill_rollback_{skill_name}_"),
        )
        backup_dir = backup_root / skill_name
        shutil.copytree(skill_dir, backup_dir)
    return {
        "workspace_dir": workspace_dir,
        "skill_name": skill_name,
        "entry": copy.deepcopy(entry) if entry is not None else None,
        "backup_dir": backup_dir,
    }


def _restore_workspace_skill(snapshot: dict[str, Any]) -> None:
    workspace_dir = Path(snapshot["workspace_dir"])
    skill_name = str(snapshot["skill_name"])
    skill_dir = workspace_dir / "skills" / skill_name
    backup_dir = snapshot.get("backup_dir")
    entry = snapshot.get("entry")

    if skill_dir.exists():
        shutil.rmtree(skill_dir)
    if backup_dir is not None and Path(backup_dir).exists():
        shutil.copytree(Path(backup_dir), skill_dir)

    def _restore(payload: dict[str, Any]) -> None:
        payload.setdefault("skills", {})
        if entry is None:
            payload["skills"].pop(skill_name, None)
            return
        payload["skills"][skill_name] = copy.deepcopy(entry)

    _mutate_json(
        get_workspace_skill_manifest_path(workspace_dir),
        _default_workspace_manifest(),
        _restore,
    )
    reconcile_workspace_manifest(workspace_dir)
    if backup_dir is not None:
        shutil.rmtree(Path(backup_dir).parent, ignore_errors=True)


async def _request_workspace_dir(request: Request) -> Path:
    from ..agent_context import get_agent_for_request

    workspace = await get_agent_for_request(request)
    return Path(workspace.workspace_dir)


async def _kdocs_connection_service(request: Request) -> KdocsConnectionService:
    workspace_dir = await _request_workspace_dir(request)
    skill_dir = get_workspace_skills_dir(workspace_dir) / "kdocs"
    if not skill_dir.is_dir() or not (skill_dir / "SKILL.md").is_file():
        skill_dir = (
            Path(__file__).resolve().parents[2]
            / "agents"
            / "skills"
            / "kdocs-zh"
        )
    if not skill_dir.is_dir() or not (skill_dir / "SKILL.md").is_file():
        raise HTTPException(status_code=503, detail="The KDocs Skill is unavailable.")
    return KdocsConnectionService(skill_dir=skill_dir)


async def _kdocs_document_service(request: Request) -> KdocsDocumentService:
    # Keep document access behind the same packaged-skill availability boundary
    # as connection management. The browser UI never receives CLI paths or tokens.
    await _kdocs_connection_service(request)
    return KdocsDocumentService(cache_scope=str(await _request_workspace_dir(request)))


async def _run_kdocs_document_call(call: Any, /, **kwargs: Any) -> Any:
    try:
        return await asyncio.to_thread(call, **kwargs)
    except KdocsDocumentError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.as_detail(),
        ) from exc


def _kdocs_status_response(
    status: KdocsConnectionStatus,
) -> KdocsConnectionStatusResponse:
    return KdocsConnectionStatusResponse(
        installed=status.installed,
        authenticated=status.authenticated,
        cli_version=status.cli_version,
        token_url=status.token_url,
    )


def _wpsnote_status_response(
    status: WpsNoteConnectionStatus,
) -> WpsNoteConnectionStatusResponse:
    return WpsNoteConnectionStatusResponse(
        installed=status.installed,
        connected=status.connected,
        desktop_installed=status.desktop_installed,
        cli_version=status.cli_version,
        tool_count=status.tool_count,
        install_method=status.install_method,
        repository_url=status.repository_url,
        download_url=status.download_url,
        next_step=status.next_step,
        launch_requested=status.launch_requested,
    )


async def _hub_task_set_status(
    task_id: str,
    status: HubInstallTaskStatus,
    *,
    error: str | None = None,
    result: dict[str, Any] | None = None,
) -> None:
    async with _hub_install_lock:
        task = _hub_install_tasks.get(task_id)
        if task is None:
            return
        task.status = status
        task.updated_at = time.time()
        if error is not None:
            task.error = error
        if result is not None:
            task.result = result


async def _hub_task_get(task_id: str) -> HubInstallTask | None:
    async with _hub_install_lock:
        return _hub_install_tasks.get(task_id)


async def _hub_task_register_runtime(task_id: str, task: asyncio.Task) -> None:
    async with _hub_install_lock:
        _hub_install_runtime_tasks[task_id] = task


async def _hub_task_pop_runtime(task_id: str) -> asyncio.Task | None:
    async with _hub_install_lock:
        return _hub_install_runtime_tasks.pop(task_id, None)


async def _read_validated_zip_upload(file: UploadFile) -> bytes:
    if file.content_type and file.content_type not in _ALLOWED_ZIP_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                "Expected a zip file, "
                f"got content-type: {file.content_type}"
            ),
        )

    data = await file.read()
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"File too large ({len(data) // (1024 * 1024)} MB). "
                f"Maximum is {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
            ),
        )
    return data


def _cleanup_imported_skill(workspace_dir: Path, skill_name: str) -> None:
    if not skill_name:
        return
    try:
        skill_service = SkillService(workspace_dir)
        skill_service.disable_skill(skill_name)
        skill_service.delete_skill(skill_name)
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "Cleanup after cancelled import failed for '%s': %s",
            skill_name,
            exc,
        )


def _build_skill_index_entry(
    *,
    skill_name: str,
    skill_dir: Path,
    entry: dict[str, Any],
    source: str,
) -> SkillIndexEntry | None:
    if not skill_dir.is_dir():
        return None
    post = _read_frontmatter_safe(skill_dir, skill_name)
    return SkillIndexEntry(
        name=str(post.get("name") or skill_name),
        description=str(post.get("description") or ""),
        tags=list(entry.get("tags") or []),
        source=source,
        enabled=bool(entry.get("enabled", False)),
        channels=list(entry.get("channels") or ["all"]),
        version=_extract_version(post),
        emoji=_extract_emoji_from_metadata(post.get("metadata", {})),
        entry_path=str(skill_dir / "SKILL.md"),
        last_updated=_get_skill_mtime(skill_dir),
    )


def _build_workspace_skill_index(workspace_dir: Path) -> list[SkillIndexEntry]:
    manifest = read_skill_manifest(workspace_dir)
    entries = manifest.get("skills", {})
    skill_root = get_workspace_skills_dir(workspace_dir)
    result: list[SkillIndexEntry] = []
    for skill_name, raw_entry in sorted(entries.items()):
        entry = _normalize_skill_manifest_entry(raw_entry)
        source = str(entry.get("source", "customized") or "customized")
        item = _build_skill_index_entry(
            skill_name=skill_name,
            skill_dir=skill_root / skill_name,
            entry=entry,
            source=source,
        )
        if item is not None:
            result.append(item)
    return result


def _build_pool_skill_index() -> list[PoolSkillIndexEntry]:
    manifest = read_skill_pool_manifest()
    entries = manifest.get("skills", {})
    pool_dir = get_skill_pool_dir()
    sync_info = get_pool_builtin_sync_status(pool_skills=entries)
    result: list[PoolSkillIndexEntry] = []
    for skill_name, raw_entry in sorted(entries.items()):
        entry = _normalize_skill_manifest_entry(raw_entry)
        skill_dir = pool_dir / skill_name
        if not skill_dir.is_dir():
            continue
        post = _read_frontmatter_safe(skill_dir, skill_name)
        info = sync_info.get(skill_name, {})
        result.append(
            PoolSkillIndexEntry(
                name=str(post.get("name") or skill_name),
                description=str(post.get("description") or ""),
                tags=list(entry.get("tags") or []),
                source=str(entry.get("source", "customized") or "customized"),
                enabled=True,
                channels=["all"],
                version=str(entry.get("version_text") or _extract_version(post)),
                emoji=_extract_emoji_from_metadata(post.get("metadata", {})),
                entry_path=str(skill_dir / "SKILL.md"),
                protected=bool(entry.get("protected", False)),
                sync_status=str(info.get("sync_status", "") or ""),
                last_updated=_get_skill_mtime(skill_dir),
            )
        )
    return result


async def _emit_skills_changed(
    *,
    request: Request | None,
    workspace_dir: Path,
    agent_id: str = "default",
    change_type: str,
    skill_name: str | None = None,
    source: str = "workspace",
    details: dict[str, Any] | None = None,
) -> None:
    del request
    try:
        await record_skills_changed(
            workspace_dir=workspace_dir,
            agent_id=agent_id,
            change_type=change_type,
            skill_name=skill_name,
            source=source,
            details=details,
        )
    except Exception:
        logger.debug("Failed to record skills.changed event", exc_info=True)


async def _run_hub_install_task(
    *,
    task_id: str,
    workspace_dir: Path,
    body: HubInstallRequest,
    cancel_event: threading.Event,
) -> None:
    await _hub_task_set_status(task_id, HubInstallTaskStatus.IMPORTING)
    imported_skill_name: str | None = None
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: install_skill_from_hub(
                workspace_dir=workspace_dir,
                bundle_url=body.bundle_url,
                version=body.version,
                enable=body.enable,
                target_name=body.target_name,
                cancel_checker=cancel_event.is_set,
            ),
        )
        imported_skill_name = result.name
        if cancel_event.is_set():
            _cleanup_imported_skill(workspace_dir, result.name)
            await _hub_task_set_status(
                task_id,
                HubInstallTaskStatus.CANCELLED,
                result={
                    "installed": False,
                    "name": result.name,
                    "enabled": False,
                    "source_url": result.source_url,
                },
            )
            return
        await _hub_task_set_status(
            task_id,
            HubInstallTaskStatus.COMPLETED,
            result={
                "installed": True,
                "name": result.name,
                "enabled": result.enabled,
                "source_url": result.source_url,
            },
        )
    except SkillImportCancelled:
        if imported_skill_name:
            _cleanup_imported_skill(workspace_dir, imported_skill_name)
        await _hub_task_set_status(task_id, HubInstallTaskStatus.CANCELLED)
    except SkillScanError as exc:
        await _hub_task_set_status(
            task_id,
            HubInstallTaskStatus.FAILED,
            error=str(exc),
            result=_scan_error_payload(exc),
        )
    except (ValueError, AppBaseException) as exc:
        await _hub_task_set_status(
            task_id,
            HubInstallTaskStatus.FAILED,
            error=str(exc),
        )
    except SkillConflictError as exc:
        await _hub_task_set_status(
            task_id,
            HubInstallTaskStatus.FAILED,
            error=str(exc),
            result=exc.detail,
        )
    except RuntimeError as exc:
        await _hub_task_set_status(
            task_id,
            HubInstallTaskStatus.FAILED,
            error=str(exc),
        )
    except Exception as exc:  # pragma: no cover
        await _hub_task_set_status(
            task_id,
            HubInstallTaskStatus.FAILED,
            error=f"Skill hub import failed: {exc}",
        )
    finally:
        await _hub_task_pop_runtime(task_id)


def _build_workspace_skill_specs(workspace_dir: Path) -> list[SkillSpec]:
    manifest = read_skill_manifest(workspace_dir)
    entries = manifest.get("skills", {})
    skill_root = get_workspace_skills_dir(workspace_dir)
    specs: list[SkillSpec] = []
    for skill_name, raw_entry in sorted(entries.items()):
        entry = _normalize_skill_manifest_entry(raw_entry)
        if raw_entry not in (None, entry):
            logger.warning(
                "Skipping malformed workspace skill entry '%s' in manifest",
                skill_name,
            )
        try:
            source = entry.get("source", "customized")
            skill_dir = skill_root / skill_name
            skill = _read_skill_from_dir(skill_dir, source)
            if skill is None:
                continue
            dump = skill.model_dump()
            dump["tags"] = entry.get("tags") or []
            specs.append(
                SkillSpec(
                    **dump,
                    enabled=entry.get("enabled", False),
                    channels=entry.get("channels") or ["all"],
                    config=entry.get("config") or {},
                    last_updated=_get_skill_mtime(skill_dir),
                ),
            )
        except Exception:
            logger.warning(
                "Skipping workspace skill '%s': failed to build spec",
                skill_name,
                exc_info=True,
            )
    return specs


def _build_pool_skill_specs() -> list[PoolSkillSpec]:
    manifest = read_skill_pool_manifest()
    entries = manifest.get("skills", {})
    pool_dir = get_skill_pool_dir()
    sync_info = get_pool_builtin_sync_status(pool_skills=entries)
    specs: list[PoolSkillSpec] = []
    for skill_name, raw_entry in sorted(entries.items()):
        entry = _normalize_skill_manifest_entry(raw_entry)
        if raw_entry not in (None, entry):
            logger.warning(
                "Skipping malformed pool skill entry '%s' in manifest",
                skill_name,
            )
        try:
            source = entry.get("source", "customized")
            skill_dir = pool_dir / skill_name
            skill = _read_skill_from_dir(skill_dir, source)
            if skill is None:
                continue
            info = sync_info.get(skill_name, {})
            dump = skill.model_dump(exclude={"version_text"})
            dump["tags"] = entry.get("tags") or []
            specs.append(
                PoolSkillSpec(
                    **dump,
                    protected=bool(entry.get("protected", False)),
                    version_text=str(entry.get("version_text", "") or ""),
                    commit_text=str(entry.get("commit_text", "") or ""),
                    sync_status=str(info.get("sync_status", "") or ""),
                    latest_version_text=str(
                        info.get("latest_version_text", "") or "",
                    ),
                    builtin_language=str(
                        entry.get("builtin_language", "") or "",
                    ),
                    available_builtin_languages=[
                        str(language)
                        for language in (
                            info.get("available_languages")
                            or entry.get("available_builtin_languages")
                            or []
                        )
                        if str(language)
                    ],
                    config=entry.get("config") or {},
                    last_updated=_get_skill_mtime(skill_dir),
                ),
            )
        except Exception:
            logger.warning(
                "Skipping pool skill '%s': failed to build spec",
                skill_name,
                exc_info=True,
            )
    return specs


@router.get("")
async def list_skills(request: Request) -> list[SkillSpec]:
    workspace_dir = await _request_workspace_dir(request)
    return _build_workspace_skill_specs(workspace_dir)


@router.get("/index")
async def list_skill_index(request: Request) -> list[SkillIndexEntry]:
    """Return a lightweight skill index without SKILL.md content."""

    workspace_dir = await _request_workspace_dir(request)
    return _build_workspace_skill_index(workspace_dir)


@router.post("/refresh")
async def refresh_skills(request: Request) -> list[SkillSpec]:
    """Force reconcile and return updated workspace skill list."""
    from ..agent_context import get_agent_for_request

    workspace = await get_agent_for_request(request)
    workspace_dir = Path(workspace.workspace_dir)
    reconcile_workspace_manifest(workspace_dir)
    await _emit_skills_changed(
        request=request,
        workspace_dir=workspace_dir,
        agent_id=workspace.agent_id,
        change_type="refresh",
    )
    return _build_workspace_skill_specs(workspace_dir)


@router.get(
    "/integrations/kdocs/status",
    response_model=KdocsConnectionStatusResponse,
)
async def get_kdocs_connection_status(
    request: Request,
) -> KdocsConnectionStatusResponse:
    service = await _kdocs_connection_service(request)
    try:
        status = await asyncio.to_thread(service.status)
    except KdocsConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _kdocs_status_response(status)


@router.post(
    "/integrations/kdocs/connect",
    response_model=KdocsConnectionStatusResponse,
)
async def connect_kdocs(
    request: Request,
    body: KdocsConnectRequest,
) -> KdocsConnectionStatusResponse:
    service = await _kdocs_connection_service(request)
    try:
        status = await asyncio.to_thread(
            service.connect,
            body.token.get_secret_value(),
        )
    except KdocsConnectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _kdocs_status_response(status)


@router.delete(
    "/integrations/kdocs/connection",
    response_model=KdocsConnectionStatusResponse,
)
async def disconnect_kdocs(
    request: Request,
) -> KdocsConnectionStatusResponse:
    service = await _kdocs_connection_service(request)
    try:
        status = await asyncio.to_thread(service.disconnect)
    except KdocsConnectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _kdocs_status_response(status)


@router.get("/integrations/kdocs/documents")
async def list_kdocs_documents(
    request: Request,
    view: str = "recent",
    drive_id: str = "",
    parent_id: str = "",
    query: str = "",
    page_token: str = "",
    page_size: int = 50,
) -> dict[str, Any]:
    service = await _kdocs_document_service(request)
    return await _run_kdocs_document_call(
        service.list_documents,
        view=view,
        drive_id=drive_id,
        parent_id=parent_id,
        query=query,
        page_token=page_token,
        page_size=page_size,
    )


@router.post("/integrations/kdocs/documents/read")
async def read_kdocs_document(
    request: Request,
    body: KdocsDocumentReadRequest,
) -> dict[str, Any]:
    service = await _kdocs_document_service(request)
    if body.continuation:
        info = await _run_kdocs_document_call(service.file_info, file_id=body.file_id)
        if info.get("permissions", {}).get("can_read") is False:
            raise HTTPException(status_code=403, detail="你暂时没有访问这个文档的权限。")
    sheet_range = body.sheet_range.model_dump() if body.sheet_range else None
    return await _run_kdocs_document_call(
        service.read_document,
        file_id=body.file_id,
        task_id=body.task_id,
        **({"continuation": body.continuation} if body.continuation else {}),
        format=body.format,
        max_chars=body.max_chars,
        sheet_name=body.sheet_name,
        sheet_range=sheet_range,
    )


@router.post("/integrations/kdocs/documents/info")
async def get_kdocs_document_info(
    request: Request,
    body: KdocsDocumentIdentityRequest,
) -> dict[str, Any]:
    service = await _kdocs_document_service(request)
    return await _run_kdocs_document_call(
        service.file_info,
        file_id=body.file_id,
    )


@router.post("/integrations/kdocs/documents/link")
async def get_kdocs_document_link(
    request: Request,
    body: KdocsDocumentIdentityRequest,
) -> dict[str, Any]:
    service = await _kdocs_document_service(request)
    return await _run_kdocs_document_call(
        service.file_link,
        file_id=body.file_id,
    )


@router.get(
    "/integrations/wps-note/status",
    response_model=WpsNoteConnectionStatusResponse,
)
async def get_wpsnote_connection_status() -> WpsNoteConnectionStatusResponse:
    try:
        status = await asyncio.to_thread(WpsNoteConnectionService().status)
    except WpsNoteConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _wpsnote_status_response(status)


@router.post(
    "/integrations/wps-note/connect",
    response_model=WpsNoteConnectionStatusResponse,
)
async def connect_wpsnote() -> WpsNoteConnectionStatusResponse:
    try:
        status = await asyncio.to_thread(WpsNoteConnectionService().connect)
    except WpsNoteConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _wpsnote_status_response(status)


@router.get("/hub/search")
async def search_hub(
    q: str = "",
    limit: int = 20,
) -> list[HubSkillSpec]:
    results = search_hub_skills(q, limit=limit)
    return [
        HubSkillSpec(
            slug=item.slug,
            name=item.name,
            description=item.description,
            version=item.version,
            source_url=item.source_url,
        )
        for item in results
    ]


@router.get("/workspaces")
async def list_workspace_skill_sources() -> list[WorkspaceSkillSummary]:
    summaries: list[WorkspaceSkillSummary] = []
    workspaces = list_workspaces()
    for workspace in workspaces:
        workspace_dir = Path(workspace["workspace_dir"])
        summaries.append(
            WorkspaceSkillSummary(
                agent_id=workspace["agent_id"],
                agent_name=workspace.get("agent_name", ""),
                workspace_dir=str(workspace_dir),
                skills=_build_workspace_skill_specs(workspace_dir),
            ),
        )
    return summaries


@router.post("/hub/install/start", response_model=HubInstallTask)
async def start_install_from_hub(
    request_body: HubInstallRequest,
    request: Request,
) -> HubInstallTask:
    workspace_dir = await _request_workspace_dir(request)
    task = HubInstallTask(
        bundle_url=request_body.bundle_url,
        version=request_body.version,
        enable=request_body.enable,
    )
    cancel_event = threading.Event()
    async with _hub_install_lock:
        _hub_install_tasks[task.task_id] = task
        _hub_install_cancel_events[task.task_id] = cancel_event

    runtime_task = asyncio.create_task(
        _run_hub_install_task(
            task_id=task.task_id,
            workspace_dir=workspace_dir,
            body=request_body,
            cancel_event=cancel_event,
        ),
        name=f"skill-hub-install-{task.task_id}",
    )
    await _hub_task_register_runtime(task.task_id, runtime_task)
    return task


@router.get("/hub/install/status/{task_id}", response_model=HubInstallTask)
async def get_hub_install_status(task_id: str) -> HubInstallTask:
    task = await _hub_task_get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="install task not found")
    return task


@router.post("/hub/install/cancel/{task_id}")
async def cancel_hub_install(task_id: str) -> dict[str, Any]:
    async with _hub_install_lock:
        task = _hub_install_tasks.get(task_id)
        if task is None:
            raise HTTPException(
                status_code=404,
                detail="install task not found",
            )
        if task.status in (
            HubInstallTaskStatus.COMPLETED,
            HubInstallTaskStatus.FAILED,
            HubInstallTaskStatus.CANCELLED,
        ):
            return {"task_id": task_id, "status": task.status.value}
        cancel_event = _hub_install_cancel_events.get(task_id)
        if cancel_event is not None:
            cancel_event.set()
        task.status = HubInstallTaskStatus.CANCELLED
        task.updated_at = time.time()
    return {"task_id": task_id, "status": "cancelled"}


@router.get("/pool")
async def list_pool_skills() -> list[PoolSkillSpec]:
    return _build_pool_skill_specs()


@router.get("/pool/index")
async def list_pool_skill_index() -> list[PoolSkillIndexEntry]:
    """Return a lightweight skill-pool index without SKILL.md content."""

    return _build_pool_skill_index()


@router.post("/pool/refresh")
async def refresh_pool_skills() -> list[PoolSkillSpec]:
    """Force reconcile and return updated pool skill list."""
    reconcile_pool_manifest()
    await _emit_skills_changed(
        request=None,
        workspace_dir=get_skill_pool_dir(),
        agent_id="default",
        change_type="refresh",
        source="pool",
    )
    return _build_pool_skill_specs()


@router.get("/pool/builtin-sources")
async def list_pool_builtin_sources() -> list[BuiltinImportSpec]:
    return [
        BuiltinImportSpec(**item) for item in list_builtin_import_candidates()
    ]


@router.get("/pool/builtin-notice")
async def get_pool_builtin_notice() -> BuiltinUpdateNotice:
    notice = get_pool_builtin_update_notice()
    return BuiltinUpdateNotice(
        fingerprint=str(notice.get("fingerprint", "") or ""),
        has_updates=bool(notice.get("has_updates", False)),
        total_changes=int(notice.get("total_changes", 0) or 0),
        actionable_skill_names=[
            str(name)
            for name in notice.get("actionable_skill_names", [])
            if str(name)
        ],
        added=[BuiltinImportSpec(**item) for item in notice.get("added", [])],
        missing=[
            BuiltinImportSpec(**item) for item in notice.get("missing", [])
        ],
        updated=[
            BuiltinImportSpec(**item) for item in notice.get("updated", [])
        ],
        removed=[
            BuiltinRemovedSpec(**item) for item in notice.get("removed", [])
        ],
    )


@router.post("")
async def create_skill(
    request: Request,
    body: CreateSkillRequest,
) -> dict[str, Any]:
    from ..agent_context import get_agent_for_request

    workspace = await get_agent_for_request(request)
    workspace_dir = Path(workspace.workspace_dir)
    try:
        created = SkillService(workspace_dir).create_skill(
            name=body.name,
            content=body.content,
            references=body.references,
            scripts=body.scripts,
            config=body.config,
            enable=body.enable,
        )
    except SkillScanError as exc:
        return _scan_error_response(exc)
    except (ValueError, AppBaseException) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not created:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "conflict",
                "suggested_name": suggest_conflict_name(body.name),
            },
        )
    if body.enable:
        schedule_agent_reload(request, workspace.agent_id)
    await _emit_skills_changed(
        request=request,
        workspace_dir=workspace_dir,
        agent_id=workspace.agent_id,
        change_type="create",
        skill_name=created,
    )
    return {"created": True, "name": created}


@router.post("/upload")
async def upload_skill_zip(
    request: Request,
    file: UploadFile = File(...),
    enable: bool = True,
    target_name: str = "",
    rename_map: str = "",
) -> dict[str, Any]:
    from ..agent_context import get_agent_for_request

    workspace = await get_agent_for_request(request)
    workspace_dir = Path(workspace.workspace_dir)
    data = await _read_validated_zip_upload(file)
    parsed_rename: dict[str, str] | None = None
    if rename_map.strip():
        try:
            parsed_rename = json.loads(rename_map)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="rename_map must be valid JSON",
            ) from exc
        if not isinstance(parsed_rename, dict):
            raise HTTPException(
                status_code=400,
                detail="rename_map must be a JSON object",
            )
    try:
        result = await asyncio.to_thread(
            SkillService(workspace_dir).import_from_zip,
            data=data,
            enable=enable,
            target_name=target_name,
            rename_map=parsed_rename,
        )
    except SkillScanError as exc:
        return _scan_error_response(exc)
    except (ValueError, AppBaseException) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result.get("conflicts"):
        raise HTTPException(status_code=409, detail=result)
    if enable and result.get("count", 0) > 0:
        schedule_agent_reload(request, workspace.agent_id)
    if result.get("count", 0) > 0:
        await _emit_skills_changed(
            request=request,
            workspace_dir=workspace_dir,
            agent_id=workspace.agent_id,
            change_type="import",
            details={"count": result.get("count", 0)},
        )
    return result


@router.post("/pool/create")
async def create_pool_skill(body: CreateSkillRequest) -> dict[str, Any]:
    try:
        created = SkillPoolService().create_skill(
            name=body.name,
            content=body.content,
            references=body.references,
            scripts=body.scripts,
            config=body.config,
        )
    except SkillScanError as exc:
        return _scan_error_response(exc)
    except (ValueError, AppBaseException) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not created:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "conflict",
                "suggested_name": suggest_conflict_name(body.name),
            },
        )
    await _emit_skills_changed(
        request=None,
        workspace_dir=get_skill_pool_dir(),
        agent_id="default",
        change_type="create",
        skill_name=created,
        source="pool",
    )
    return {"created": True, "name": created}


@router.put("/pool/save")
async def save_pool_skill(body: SavePoolSkillRequest) -> dict[str, Any]:
    """Save one pool skill.

    ``overwrite`` only matters when the save would replace an existing target
    skill during rename/save-as.
    """
    service = SkillPoolService()
    try:
        result = service.save_pool_skill(
            skill_name=body.source_name or body.name,
            target_name=body.name,
            content=body.content,
            config=body.config,
            overwrite=body.overwrite,
        )
    except SkillScanError as exc:
        return _scan_error_response(exc)
    except (ValueError, AppBaseException) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not result.get("success"):
        reason = result.get("reason")
        status = 404 if reason == "not_found" else 409
        raise HTTPException(status_code=status, detail=result)
    if result.get("mode") != "noop":
        await _emit_skills_changed(
            request=None,
            workspace_dir=get_skill_pool_dir(),
            agent_id="default",
            change_type=str(result.get("mode") or "save"),
            skill_name=str(result.get("name") or body.name),
            source="pool",
        )
    return result


@router.post("/pool/upload-zip")
async def upload_skill_pool_zip(
    file: UploadFile = File(...),
    target_name: str = "",
    rename_map: str = "",
) -> dict[str, Any]:
    data = await _read_validated_zip_upload(file)
    parsed_rename: dict[str, str] | None = None
    if rename_map.strip():
        try:
            parsed_rename = json.loads(rename_map)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="rename_map must be valid JSON",
            ) from exc
        if not isinstance(parsed_rename, dict):
            raise HTTPException(
                status_code=400,
                detail="rename_map must be a JSON object",
            )
    try:
        result = await asyncio.to_thread(
            SkillPoolService().import_from_zip,
            data=data,
            target_name=target_name,
            rename_map=parsed_rename,
        )
    except SkillScanError as exc:
        return _scan_error_response(exc)
    except (ValueError, AppBaseException) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result.get("conflicts"):
        raise HTTPException(status_code=409, detail=result)
    if result.get("count", 0) > 0:
        await _emit_skills_changed(
            request=None,
            workspace_dir=get_skill_pool_dir(),
            agent_id="default",
            change_type="import",
            source="pool",
            details={"count": result.get("count", 0)},
        )
    return result


@router.post("/pool/import")
async def import_skill_pool_from_hub(
    body: HubInstallRequest,
) -> dict[str, Any]:
    try:
        result = import_pool_skill_from_hub(
            bundle_url=body.bundle_url,
            version=body.version,
            target_name=body.target_name,
        )
    except SkillScanError as exc:
        return _scan_error_response(exc)
    except (ValueError, AppBaseException) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SkillConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _emit_skills_changed(
        request=None,
        workspace_dir=get_skill_pool_dir(),
        agent_id="default",
        change_type="import",
        skill_name=result.name,
        source="pool",
    )
    return {
        "installed": True,
        "name": result.name,
        "enabled": False,
        "source_url": result.source_url,
    }


@router.post("/pool/upload")
async def upload_workspace_skill_to_pool(
    body: UploadToPoolRequest,
) -> dict[str, Any]:
    workspace_dir = _workspace_dir_for_agent(body.workspace_id)
    try:
        result = SkillPoolService().upload_from_workspace(
            workspace_dir=workspace_dir,
            skill_name=body.skill_name,
            overwrite=body.overwrite,
            preview_only=body.preview_only,
        )
    except SkillScanError as exc:
        return _scan_error_response(exc)
    except (ValueError, AppBaseException) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not result.get("success"):
        status = 404 if result.get("reason") == "not_found" else 409
        raise HTTPException(status_code=status, detail=result)
    return result


def _preflight_download_conflicts(
    hub_service: SkillPoolService,
    targets: list[PoolDownloadTarget],
    skill_name: str,
    overwrite: bool,
) -> list[dict[str, Any]]:
    """Check all targets for conflicts before downloading."""
    conflicts: list[dict[str, Any]] = []
    for target in targets:
        workspace_dir = _workspace_dir_for_agent(target.workspace_id)
        result = hub_service.preflight_download_to_workspace(
            skill_name=skill_name,
            workspace_dir=workspace_dir,
            overwrite=overwrite,
        )
        if not result.get("success"):
            conflicts.append(result)
    return conflicts


def _resolve_and_preflight(
    body: DownloadFromPoolRequest,
) -> tuple[list[PoolDownloadTarget], SkillPoolService]:
    """Resolve targets and reject if any conflicts exist."""
    targets = list(body.targets)
    if body.all_workspaces:
        targets = [
            PoolDownloadTarget(workspace_id=workspace["agent_id"])
            for workspace in list_workspaces()
        ]
    if not targets:
        raise HTTPException(
            status_code=400,
            detail="No workspace targets provided",
        )
    hub_service = SkillPoolService()
    try:
        conflicts = _preflight_download_conflicts(
            hub_service,
            targets,
            body.skill_name,
            body.overwrite,
        )
    except (ValueError, AppBaseException) as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc
    if conflicts:
        raise HTTPException(
            status_code=409,
            detail={
                "downloaded": [],
                "conflicts": conflicts,
            },
        )
    return targets, hub_service


def _build_download_plan(
    targets: list[PoolDownloadTarget],
    skill_name: str,
) -> list[dict[str, Any]]:
    """Build execution plan with rollback snapshots."""
    plan: list[dict[str, Any]] = []
    for target in targets:
        workspace_dir = _workspace_dir_for_agent(target.workspace_id)
        snapshot = _snapshot_workspace_skill(
            workspace_dir,
            str(skill_name),
        )
        plan.append(
            {
                "workspace_id": target.workspace_id,
                "workspace_dir": workspace_dir,
                "snapshot": snapshot,
            },
        )
    return plan


@router.post("/pool/download")
async def download_pool_skill_to_workspaces(
    body: DownloadFromPoolRequest,
) -> dict[str, Any]:
    """Download one pool skill into one or more workspaces.

    All-or-nothing: if any target conflicts, reject everything.
    """
    targets, hub_service = _resolve_and_preflight(body)
    if body.preview_only:
        return {"downloaded": []}

    execution_plan = _build_download_plan(targets, body.skill_name)

    downloaded: list[dict[str, str]] = []
    try:
        for plan in execution_plan:
            result = hub_service.download_to_workspace(
                skill_name=body.skill_name,
                workspace_dir=plan["workspace_dir"],
                overwrite=body.overwrite,
            )
            if not result.get("success"):
                for rollback in reversed(execution_plan):
                    _restore_workspace_skill(rollback["snapshot"])
                raise HTTPException(
                    status_code=409,
                    detail={
                        "downloaded": [],
                        "conflicts": [result],
                    },
                )
            downloaded.append(
                {
                    "workspace_id": str(plan["workspace_id"]),
                    "workspace_name": str(
                        result.get("workspace_name", "") or "",
                    ),
                    "name": str(result.get("name", "")),
                },
            )
    except HTTPException:
        raise
    except SkillScanError as exc:
        for rollback in reversed(execution_plan):
            _restore_workspace_skill(rollback["snapshot"])
        return _scan_error_response(exc)
    except Exception:
        for rollback in reversed(execution_plan):
            _restore_workspace_skill(rollback["snapshot"])
        raise
    finally:
        for plan in execution_plan:
            backup_dir = plan["snapshot"].get("backup_dir")
            if backup_dir is not None:
                shutil.rmtree(Path(backup_dir).parent, ignore_errors=True)

    return {"downloaded": downloaded}


@router.post("/pool/import-builtin")
async def import_pool_builtins(
    body: ImportBuiltinRequest,
) -> dict[str, Any]:
    imports: list[dict[str, Any]] = (
        [item.model_dump() for item in body.imports]
        if body.imports
        else [{"skill_name": skill_name} for skill_name in body.skill_names]
    )
    result = import_builtin_skills(
        imports,
        overwrite_conflicts=body.overwrite_conflicts,
    )
    if result.get("conflicts") and not body.overwrite_conflicts:
        raise HTTPException(status_code=409, detail=result)
    return result


@router.post("/pool/{skill_name}/update-builtin")
async def update_pool_builtin(
    skill_name: str,
    body: UpdateBuiltinRequest | None = Body(default=None),
) -> dict[str, Any]:
    language = body.language if body is not None else ""
    if language and language not in _BUILTIN_SKILL_LANGUAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid language '{language}', "
            f"must be one of {_BUILTIN_SKILL_LANGUAGES}",
        )
    try:
        return update_single_builtin(skill_name, language=language or None)
    except (ValueError, AppBaseException) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/pool/{skill_name}")
async def delete_pool_skill(skill_name: str) -> dict[str, Any]:
    deleted = SkillPoolService().delete_skill(skill_name)
    if not deleted:
        raise HTTPException(
            status_code=409,
            detail="Skill pool entry cannot be deleted",
        )
    await _emit_skills_changed(
        request=None,
        workspace_dir=get_skill_pool_dir(),
        agent_id="default",
        change_type="delete",
        skill_name=skill_name,
        source="pool",
    )
    return {"deleted": True}


@router.get("/pool/{skill_name}/config")
async def get_pool_skill_config(skill_name: str) -> dict[str, Any]:
    manifest = read_skill_pool_manifest()
    entry = manifest.get("skills", {}).get(skill_name)
    if entry is None:
        raise HTTPException(status_code=404, detail="Pool skill not found")
    return {"config": entry.get("config", {})}


@router.put("/pool/{skill_name}/config")
async def update_pool_skill_config(
    skill_name: str,
    body: SkillConfigRequest,
) -> dict[str, Any]:
    manifest_path = get_pool_skill_manifest_path()

    def _update(payload: dict[str, Any]) -> bool:
        entry = payload.get("skills", {}).get(skill_name)
        if entry is None:
            return False
        entry["config"] = dict(body.config)
        return True

    updated = _mutate_json(manifest_path, _default_pool_manifest(), _update)
    if not updated:
        raise HTTPException(status_code=404, detail="Pool skill not found")
    await _emit_skills_changed(
        request=None,
        workspace_dir=get_skill_pool_dir(),
        agent_id="default",
        change_type="config",
        skill_name=skill_name,
        source="pool",
    )
    return {"updated": True}


@router.delete("/pool/{skill_name}/config")
async def delete_pool_skill_config(skill_name: str) -> dict[str, Any]:
    manifest_path = get_pool_skill_manifest_path()

    def _update(payload: dict[str, Any]) -> bool:
        entry = payload.get("skills", {}).get(skill_name)
        if entry is None:
            return False
        entry.pop("config", None)
        return True

    updated = _mutate_json(manifest_path, _default_pool_manifest(), _update)
    if not updated:
        raise HTTPException(status_code=404, detail="Pool skill not found")
    await _emit_skills_changed(
        request=None,
        workspace_dir=get_skill_pool_dir(),
        agent_id="default",
        change_type="config-cleared",
        skill_name=skill_name,
        source="pool",
    )
    return {"cleared": True}


def _validate_tags(tags: list[str]) -> list[str]:
    if len(tags) > MAX_TAGS:
        raise HTTPException(
            status_code=422,
            detail=f"At most {MAX_TAGS} tags allowed",
        )
    cleaned: list[str] = []
    for t in tags:
        t = str(t).strip()[:MAX_TAG_LENGTH]
        if t:
            cleaned.append(t)
    return cleaned


@router.put("/pool/{skill_name}/tags")
async def update_pool_skill_tags(
    skill_name: str,
    tags: list[str],
) -> dict[str, Any]:
    tags = _validate_tags(tags)
    updated = SkillPoolService().set_pool_skill_tags(skill_name, tags)
    if not updated:
        raise HTTPException(
            status_code=404,
            detail="Pool skill not found",
        )
    await _emit_skills_changed(
        request=None,
        workspace_dir=get_skill_pool_dir(),
        agent_id="default",
        change_type="tags",
        skill_name=skill_name,
        source="pool",
        details={"tags": tags},
    )
    return {"updated": True, "tags": tags}


@router.post("/batch-delete")
async def batch_delete_skills(
    request: Request,
    skills: list[str],
) -> dict[str, Any]:
    """Auto-disable then delete each skill. Per-skill results."""
    from ..agent_context import get_agent_for_request

    workspace = await get_agent_for_request(request)
    workspace_dir = Path(workspace.workspace_dir)
    service = SkillService(workspace_dir)
    results: dict[str, Any] = {}
    for skill_name in skills:
        try:
            service.disable_skill(skill_name)
            deleted = service.delete_skill(skill_name)
            results[skill_name] = {
                "success": deleted,
                "reason": None if deleted else "delete_failed",
            }
        except Exception as exc:
            results[skill_name] = {
                "success": False,
                "reason": str(exc),
            }
    if any(result.get("success") for result in results.values()):
        await _emit_skills_changed(
            request=request,
            workspace_dir=workspace_dir,
            agent_id=workspace.agent_id,
            change_type="batch-delete",
            details={"skills": skills},
        )
    return {"results": results}


@router.post("/pool/batch-delete")
async def batch_delete_pool_skills(
    skills: list[str],
) -> dict[str, Any]:
    """Delete multiple pool skills. Per-skill results."""
    service = SkillPoolService()
    results: dict[str, Any] = {}
    for skill_name in skills:
        try:
            deleted = service.delete_skill(skill_name)
            results[skill_name] = {
                "success": deleted,
                "reason": None if deleted else "delete_failed",
            }
        except Exception as exc:
            results[skill_name] = {
                "success": False,
                "reason": str(exc),
            }
    if any(result.get("success") for result in results.values()):
        await _emit_skills_changed(
            request=None,
            workspace_dir=get_skill_pool_dir(),
            agent_id="default",
            change_type="batch-delete",
            source="pool",
            details={"skills": skills},
        )
    return {"results": results}


@router.post("/batch-disable")
async def batch_disable_skills(
    request: Request,
    skills: list[str],
) -> dict[str, Any]:
    from ..agent_context import get_agent_for_request

    workspace = await get_agent_for_request(request)
    workspace_dir = Path(workspace.workspace_dir)
    service = SkillService(workspace_dir)
    results = {skill: service.disable_skill(skill) for skill in skills}
    if any(result.get("success") for result in results.values()):
        schedule_agent_reload(request, workspace.agent_id)
        await _emit_skills_changed(
            request=request,
            workspace_dir=workspace_dir,
            agent_id=workspace.agent_id,
            change_type="batch-disable",
            details={"skills": skills},
        )
    return {"results": results}


@router.post("/batch-enable")
async def batch_enable_skills(
    request: Request,
    skills: list[str],
) -> dict[str, Any]:
    """Enable each requested skill independently and collect per-skill results.

    Example:
        enabling ``["ok_skill", "blocked_skill"]`` returns success for the
        first item and ``reason="security_scan_failed"`` for the second,
        rather than aborting the entire batch.
    """
    from ..agent_context import get_agent_for_request

    workspace = await get_agent_for_request(request)
    workspace_dir = Path(workspace.workspace_dir)
    service = SkillService(workspace_dir)
    results: dict[str, Any] = {}
    for skill in skills:
        try:
            results[skill] = service.enable_skill(skill)
        except SkillScanError as exc:
            results[skill] = {
                "success": False,
                "reason": "security_scan_failed",
                "detail": _scan_error_payload(exc),
            }
    if any(
        isinstance(result, dict) and result.get("success")
        for result in results.values()
    ):
        schedule_agent_reload(request, workspace.agent_id)
        await _emit_skills_changed(
            request=request,
            workspace_dir=workspace_dir,
            agent_id=workspace.agent_id,
            change_type="batch-enable",
            details={"skills": skills},
        )
    return {"results": results}


@router.post("/{skill_name}/disable")
async def disable_skill(
    request: Request,
    skill_name: str,
) -> dict[str, Any]:
    from ..agent_context import get_agent_for_request

    workspace = await get_agent_for_request(request)
    workspace_dir = Path(workspace.workspace_dir)
    result = SkillService(workspace_dir).disable_skill(skill_name)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail="Skill not found")
    schedule_agent_reload(request, workspace.agent_id)
    await _emit_skills_changed(
        request=request,
        workspace_dir=workspace_dir,
        agent_id=workspace.agent_id,
        change_type="disable",
        skill_name=skill_name,
    )
    return {"disabled": True, **result}


@router.post("/{skill_name}/enable")
async def enable_skill(
    request: Request,
    skill_name: str,
) -> dict[str, Any]:
    """Enable one workspace skill after a fresh scan."""
    from ..agent_context import get_agent_for_request

    workspace = await get_agent_for_request(request)
    workspace_dir = Path(workspace.workspace_dir)
    try:
        result = SkillService(workspace_dir).enable_skill(skill_name)
    except SkillScanError as exc:
        return _scan_error_response(exc)
    if not result.get("success"):
        raise HTTPException(
            status_code=404,
            detail=result.get("reason", "Skill not found"),
        )
    schedule_agent_reload(request, workspace.agent_id)
    await _emit_skills_changed(
        request=request,
        workspace_dir=workspace_dir,
        agent_id=workspace.agent_id,
        change_type="enable",
        skill_name=skill_name,
    )
    return {"enabled": True, **result}


@router.delete("/{skill_name}")
async def delete_skill(
    request: Request,
    skill_name: str,
) -> dict[str, Any]:
    from ..agent_context import get_agent_for_request

    workspace = await get_agent_for_request(request)
    workspace_dir = Path(workspace.workspace_dir)
    service = SkillService(workspace_dir)
    service.disable_skill(skill_name)
    deleted = service.delete_skill(skill_name)
    if not deleted:
        raise HTTPException(
            status_code=409,
            detail="Only disabled workspace skills can be deleted",
        )
    await _emit_skills_changed(
        request=request,
        workspace_dir=workspace_dir,
        agent_id=workspace.agent_id,
        change_type="delete",
        skill_name=skill_name,
    )
    return {"deleted": True}


@router.get("/{skill_name}/content", response_model=SkillContentResponse)
async def load_skill_content(
    request: Request,
    skill_name: str,
) -> SkillContentResponse:
    """Load full SKILL.md content on demand."""

    workspace_dir = await _request_workspace_dir(request)
    content = SkillService(workspace_dir).load_skill_file(
        skill_name=skill_name,
        file_path="SKILL.md",
    )
    if content is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return SkillContentResponse(name=skill_name, content=content)


@router.get("/{skill_name}/files/{file_path:path}")
async def load_skill_file(
    request: Request,
    skill_name: str,
    file_path: str,
) -> dict[str, Any]:
    workspace_dir = await _request_workspace_dir(request)
    content = SkillService(workspace_dir).load_skill_file(
        skill_name=skill_name,
        file_path=file_path,
    )
    if content is None:
        raise HTTPException(status_code=404, detail="File not found")
    return {"content": content}


@router.put("/save")
async def save_workspace_skill(
    request: Request,
    body: SaveSkillRequest,
) -> dict[str, Any]:
    from ..agent_context import get_agent_for_request

    workspace = await get_agent_for_request(request)
    workspace_dir = Path(workspace.workspace_dir)
    try:
        result = SkillService(workspace_dir).save_skill(
            skill_name=body.source_name or body.name,
            content=body.content,
            target_name=body.name if body.source_name else None,
            config=body.config,
            overwrite=body.overwrite,
        )
    except SkillScanError as exc:
        return _scan_error_response(exc)
    except (ValueError, AppBaseException) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not result.get("success"):
        if result.get("reason") == "conflict":
            raise HTTPException(status_code=409, detail=result)
        raise HTTPException(status_code=404, detail="Skill not found")
    if result.get("mode") != "noop":
        schedule_agent_reload(request, workspace.agent_id)
        await _emit_skills_changed(
            request=request,
            workspace_dir=workspace_dir,
            agent_id=workspace.agent_id,
            change_type=str(result.get("mode") or "save"),
            skill_name=str(result.get("name") or body.name),
        )
    return result


@router.put("/{skill_name}/channels")
async def update_skill_channels_endpoint(
    request: Request,
    skill_name: str,
    channels: list[str],
) -> dict[str, Any]:
    from ..agent_context import get_agent_for_request

    workspace = await get_agent_for_request(request)
    workspace_dir = Path(workspace.workspace_dir)
    updated = SkillService(workspace_dir).set_skill_channels(
        skill_name,
        channels,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Skill not found")
    schedule_agent_reload(request, workspace.agent_id)
    await _emit_skills_changed(
        request=request,
        workspace_dir=workspace_dir,
        agent_id=workspace.agent_id,
        change_type="channels",
        skill_name=skill_name,
        details={"channels": channels},
    )
    return {"updated": True, "channels": channels}


@router.put("/{skill_name}/tags")
async def update_skill_tags(
    request: Request,
    skill_name: str,
    tags: list[str],
) -> dict[str, Any]:
    from ..agent_context import get_agent_for_request

    tags = _validate_tags(tags)
    workspace = await get_agent_for_request(request)
    workspace_dir = Path(workspace.workspace_dir)
    updated = SkillService(workspace_dir).set_skill_tags(
        skill_name,
        tags,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Skill not found")
    await _emit_skills_changed(
        request=request,
        workspace_dir=workspace_dir,
        agent_id=workspace.agent_id,
        change_type="tags",
        skill_name=skill_name,
        details={"tags": tags},
    )
    return {"updated": True, "tags": tags}


@router.get("/{skill_name}/config")
async def get_skill_config_endpoint(
    request: Request,
    skill_name: str,
) -> dict[str, Any]:
    workspace_dir = await _request_workspace_dir(request)
    manifest = read_skill_manifest(workspace_dir)
    entry = manifest.get("skills", {}).get(skill_name)
    if entry is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return {"config": entry.get("config", {})}


@router.put("/{skill_name}/config")
async def update_skill_config_endpoint(
    request: Request,
    skill_name: str,
    body: SkillConfigRequest,
) -> dict[str, Any]:
    from ..agent_context import get_agent_for_request

    workspace = await get_agent_for_request(request)
    workspace_dir = Path(workspace.workspace_dir)
    manifest_path = get_workspace_skill_manifest_path(workspace_dir)

    def _update(payload: dict[str, Any]) -> bool:
        entry = payload.get("skills", {}).get(skill_name)
        if entry is None:
            return False
        entry["config"] = dict(body.config)
        return True

    updated = _mutate_json(
        manifest_path,
        _default_workspace_manifest(),
        _update,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Skill not found")
    await _emit_skills_changed(
        request=request,
        workspace_dir=workspace_dir,
        agent_id=workspace.agent_id,
        change_type="config",
        skill_name=skill_name,
    )
    return {"updated": True}


@router.delete("/{skill_name}/config")
async def delete_skill_config_endpoint(
    request: Request,
    skill_name: str,
) -> dict[str, Any]:
    from ..agent_context import get_agent_for_request

    workspace = await get_agent_for_request(request)
    workspace_dir = Path(workspace.workspace_dir)
    manifest_path = get_workspace_skill_manifest_path(workspace_dir)

    def _update(payload: dict[str, Any]) -> bool:
        entry = payload.get("skills", {}).get(skill_name)
        if entry is None:
            return False
        entry.pop("config", None)
        return True

    updated = _mutate_json(
        manifest_path,
        _default_workspace_manifest(),
        _update,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Skill not found")
    await _emit_skills_changed(
        request=request,
        workspace_dir=workspace_dir,
        agent_id=workspace.agent_id,
        change_type="config-cleared",
        skill_name=skill_name,
    )
    return {"cleared": True}
