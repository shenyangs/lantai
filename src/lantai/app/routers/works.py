# -*- coding: utf-8 -*-
"""User-facing Works library API."""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import uuid4
from zipfile import BadZipFile, LargeZipFile, ZipFile

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ..agent_context import get_agent_for_request
from ..experience import resolve_effective_workspace_dir_from_meta
from ..office_documents import preview_office_document
from ..office_conversion import OfficeConversionUnavailable
from ..runner.api import ensure_chat_topic_workspace
from ..runner.models import ChatSpec
from ...config import load_config
from ...runtime.artifact_effects import (
    RuntimeArtifactEffectJournal,
    RuntimeArtifactEffectRecord,
)
from ...runtime.runs import RuntimeRunContract, RuntimeValidationMetadata
from ...runtime.store import RuntimeRunStore
from ..work_covers import generate_ai_cover
from ..work_previews import get_or_create_work_preview, get_work_preview_page
from ..works import (
    WorkDerivationConflict,
    WorkItem,
    WorksIndexInspection,
    WorksIndexRecoveryPreview,
    WorksIndexRestoreResult,
    WorksIndexUnavailable,
    add_work,
    build_work_edit_context,
    create_work_edit_copy,
    derive_work_to_pdf,
    get_managed_work_file,
    get_work_state,
    get_work_cover_file,
    inspect_works_index,
    list_works,
    preview_works_index_recovery,
    remove_work,
    restore_works_index,
)


router = APIRouter(prefix="/works", tags=["works"])

_MAX_OFFICE_PREVIEW_SOURCE_BYTES = 128 * 1024 * 1024
_MAX_OFFICE_PREVIEW_ARCHIVE_MEMBERS = 4096
_MAX_OFFICE_PREVIEW_EXPANDED_BYTES = 256 * 1024 * 1024
_MAX_OFFICE_PREVIEW_MEMBER_BYTES = 64 * 1024 * 1024


class _OfficePreviewLimitError(ValueError):
    """An Office package exceeded a bounded local preview resource limit."""


def _preflight_office_preview(path: Path) -> None:
    """Inspect OOXML metadata without extracting or executing content."""

    source_size = path.stat().st_size
    if source_size > _MAX_OFFICE_PREVIEW_SOURCE_BYTES:
        raise _OfficePreviewLimitError(
            "Office preview source exceeds the 128 MiB safety limit",
        )
    try:
        with ZipFile(path) as archive:
            entries = archive.infolist()
    except (BadZipFile, LargeZipFile) as exc:
        raise ValueError("Office preview package is damaged") from exc
    if len(entries) > _MAX_OFFICE_PREVIEW_ARCHIVE_MEMBERS:
        raise _OfficePreviewLimitError(
            "Office preview package has too many entries",
        )
    expanded_bytes = 0
    for entry in entries:
        if entry.flag_bits & 0x1:
            raise ValueError(
                "Encrypted Office preview packages are unsupported",
            )
        if entry.file_size > _MAX_OFFICE_PREVIEW_MEMBER_BYTES:
            raise _OfficePreviewLimitError(
                "Office preview package entry exceeds the 64 MiB safety limit",
            )
        expanded_bytes += entry.file_size
        if expanded_bytes > _MAX_OFFICE_PREVIEW_EXPANDED_BYTES:
            raise _OfficePreviewLimitError(
                "Office preview expanded content exceeds the 256 MiB "
                "safety limit",
            )


def _preview_managed_office_document(path: Path):
    _preflight_office_preview(path)
    return preview_office_document(str(path), mode="text")


def _raise_works_index_http(exc: WorksIndexUnavailable) -> None:
    unavailable_codes = {
        "works_index_permission_denied",
        "works_index_io_error",
        "works_index_assets_io_error",
        "works_index_history_io_error",
        "works_index_backup_failed",
        "works_index_backup_unavailable",
        "works_index_quarantine_failed",
        "works_index_restore_failed",
        "works_index_write_failed",
        "works_index_write_verification_failed",
        "works_index_restore_verification_failed",
    }
    raise HTTPException(
        status_code=503 if exc.code in unavailable_codes else 409,
        detail=exc.public_detail(),
    ) from exc


def _require_usable_work(workspace_dir: str | Path, work_id: str) -> WorkItem:
    try:
        item = get_work_state(workspace_dir, work_id)
    except WorksIndexUnavailable as exc:
        _raise_works_index_http(exc)
    if item is None:
        raise HTTPException(status_code=404, detail="work not found")
    availability_errors = {
        "missing": (410, "saved work copy is missing"),
        "permission_denied": (403, "saved work copy cannot be read"),
        "unknown": (409, "saved work copy availability is unknown"),
    }
    if item.availability_status != "available":
        status_code, detail = availability_errors[item.availability_status]
        raise HTTPException(status_code=status_code, detail=detail)
    if item.integrity_status == "modified":
        raise HTTPException(
            status_code=409,
            detail="saved work version was modified",
        )
    if item.integrity_status != "intact":
        raise HTTPException(
            status_code=409,
            detail="saved work integrity is unknown",
        )
    return item


def _require_managed_work_file(
    workspace_dir: str | Path,
    work_id: str,
) -> tuple[WorkItem, Path]:
    """Re-read a managed Work while preserving index diagnostics on races."""

    try:
        managed = get_managed_work_file(workspace_dir, work_id)
    except WorksIndexUnavailable as exc:
        _raise_works_index_http(exc)
    if managed is None:
        raise HTTPException(
            status_code=409,
            detail="saved work state changed; refresh and retry",
        )
    return managed


def _require_chat_owned_work(item: WorkItem, chat_id: str | None) -> WorkItem:
    """Enforce canonical-chat scope without weakening Works access."""

    if chat_id is not None and item.source_chat_id != chat_id:
        raise HTTPException(
            status_code=409,
            detail="work is not an outcome of the requested canonical chat",
        )
    return item


def _revalidate_rendered_work(
    workspace_dir: str | Path,
    work_id: str,
    *,
    chat_id: str | None,
    expected_digest: str | None,
) -> WorkItem:
    """Recheck scope, permission, and revision after a slow preview."""

    current = _require_usable_work(workspace_dir, work_id)
    _require_chat_owned_work(current, chat_id)
    if not expected_digest or current.source_digest != expected_digest:
        raise HTTPException(
            status_code=409,
            detail="saved work revision changed while rendering",
        )
    return current


class AddWorkRequest(BaseModel):
    path: str = Field(min_length=1)
    title: str | None = None
    source_chat_id: str | None = None
    source_run_id: str | None = None


class WorkEditCopyRequest(BaseModel):
    chat_id: str = Field(min_length=1)


class WorkDerivationRequest(BaseModel):
    target_format: Literal["pdf"] = "pdf"
    chat_id: str | None = None


class WorksIndexRestoreRequest(BaseModel):
    expected_current_digest: str = Field(min_length=1)
    snapshot_digest: str = Field(min_length=1)
    confirm: Literal[True]


class WorkEditCopyResponse(BaseModel):
    work_id: str
    asset_id: str
    edit_path: str
    base_version: int
    base_digest: str
    lineage_root_id: str


class WorkPreviewPageResponse(BaseModel):
    number: int
    width: int
    height: int


class WorkPreviewResponse(BaseModel):
    work_id: str
    source_digest: str
    kind: str
    renderer: str
    renderer_version: str
    page_count: int
    pages: list[WorkPreviewPageResponse]


class WorkTextPreviewResponse(BaseModel):
    work_id: str
    source_digest: str
    kind: Literal["document", "spreadsheet"]
    method: str
    content_type: str
    content: str
    truncated: bool = False
    message: str = ""


class WorkValidationCheckProjection(BaseModel):
    id: str
    outcome: Literal["passed", "failed", "not_run", "error", "waived"]
    required: bool
    detail: str
    evidence_refs: list[str] = Field(default_factory=list)


class WorkValidationProjection(BaseModel):
    schema_version: Literal[1] = 1
    record_id: str
    status: Literal["passed", "failed", "partial", "not_run", "error"]
    source_run_id: str
    source_attempt_id: str
    work_id: str
    artifact_id: str
    artifact_version: int
    artifact_digest: str | None
    validator_id: str
    validator_version: str
    ruleset_id: str
    ruleset_version: str
    checked_at: str
    required_scope: list[str]
    observed_scope: list[str]
    checks: list[WorkValidationCheckProjection]
    invalidation_rules: list[str]


class WorkProjection(WorkItem):
    """Read-only canonical evidence attached to a durable Work item."""

    artifact_role: Literal["candidate", "primary", "delivery"] | None = None
    completion_bound: bool = False
    delivery_status: str | None = None
    validation: WorkValidationProjection | None = None


def _project_validation(
    item: WorkItem,
    contract: RuntimeRunContract,
) -> WorkValidationProjection | None:
    evidence = contract.completion_evidence
    if evidence is None or item.id not in evidence.validation_work_item_ids:
        return None
    metadata: RuntimeValidationMetadata | None = evidence.validation
    if metadata is None:
        return None
    checks: list[WorkValidationCheckProjection] = []
    allowed_outcomes = {"passed", "failed", "not_run", "error", "waived"}
    for raw in evidence.acceptance_results:
        if raw.get("source") == "model_reported" or not raw.get("id"):
            continue
        passed = bool(raw.get("passed"))
        raw_outcome = str(raw.get("outcome") or "").strip()
        invalid_outcome = bool(
            raw_outcome and raw_outcome not in allowed_outcomes,
        )
        outcome = (
            "error"
            if invalid_outcome
            else raw_outcome
            if raw_outcome
            else ("passed" if passed else "failed")
        )
        evidence_refs = raw.get("evidence_refs")
        normalized_evidence_refs = (
            [str(value) for value in evidence_refs if str(value).strip()]
            if isinstance(evidence_refs, (list, tuple))
            else []
        )
        compatible = passed is (outcome in {"passed", "waived"})
        waiver_is_approved = outcome != "waived" or bool(
            normalized_evidence_refs,
        )
        detail = str(raw.get("detail") or "")
        if invalid_outcome or not compatible or not waiver_is_approved:
            outcome = "error"
            detail = (
                f"Invalid validation record: unknown outcome {raw_outcome!r}."
                if invalid_outcome
                else (
                    "Invalid validation record: outcome conflicts with "
                    "passed flag."
                )
                if not compatible
                else (
                    "Invalid validation record: waiver has no approval "
                    "evidence ref."
                )
            )
        checks.append(
            WorkValidationCheckProjection(
                id=str(raw["id"]),
                outcome=outcome,
                required=bool(raw.get("required", True)),
                detail=detail,
                evidence_refs=normalized_evidence_refs,
            ),
        )
    required_checks = [check for check in checks if check.required]
    required_observed = set(metadata.required_scope).issubset(
        metadata.observed_scope,
    )
    required_outcomes = {check.outcome for check in required_checks}
    if "error" in required_outcomes:
        status = "error"
    elif "failed" in required_outcomes:
        status = "failed"
    elif required_checks and required_outcomes == {"not_run"}:
        status = "not_run"
    elif "not_run" in required_outcomes or not required_observed:
        status = "partial"
    elif required_checks:
        status = "passed"
    else:
        status = "not_run"
    digest = item.source_digest or "missing-digest"
    return WorkValidationProjection(
        record_id=(
            "validation:"
            f"{contract.run_id}:{contract.attempt_id}:{item.id}:{digest}"
        ),
        status=status,
        source_run_id=contract.run_id,
        source_attempt_id=contract.attempt_id,
        work_id=item.id,
        artifact_id=item.artifact_id,
        artifact_version=item.version,
        artifact_digest=item.source_digest,
        validator_id=metadata.validator_id,
        validator_version=metadata.validator_version,
        ruleset_id=metadata.ruleset_id,
        ruleset_version=metadata.ruleset_version,
        checked_at=metadata.assessed_at.isoformat(),
        required_scope=list(metadata.required_scope),
        observed_scope=list(metadata.observed_scope),
        checks=checks,
        invalidation_rules=list(metadata.invalidation_rules),
    )


def _sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"
    except OSError:
        return None


def _latest_artifact_records(
    records: list[RuntimeArtifactEffectRecord],
) -> tuple[RuntimeArtifactEffectRecord, ...]:
    latest: dict[str, RuntimeArtifactEffectRecord] = {}
    for record in records:
        current = latest.get(record.artifact_effect_id)
        if current is None or record.revision > current.revision:
            latest[record.artifact_effect_id] = record
    return tuple(latest.values())


def _canonical_role_for_work(
    item: WorkItem,
    records: tuple[RuntimeArtifactEffectRecord, ...],
    *,
    primary_artifact_ids: set[str],
    workspace_root: str | Path | None,
) -> Literal["candidate", "primary", "delivery"] | None:
    """Match immutable evidence; never infer a role from extension."""

    digest = _sha256_file(Path(item.path).expanduser())
    if digest is None or workspace_root is None:
        return None
    root_path = Path(workspace_root).expanduser()
    source_path = Path(item.source_path).expanduser()
    if not root_path.is_absolute() or not source_path.is_absolute():
        return None
    try:
        relative_path = source_path.resolve(strict=False).relative_to(
            root_path.resolve(strict=False),
        )
    except (OSError, ValueError):
        return None
    canonical_relative_path = PurePosixPath(*relative_path.parts).as_posix()

    def has_same_canonical_path(record: RuntimeArtifactEffectRecord) -> bool:
        record_path = PurePosixPath(record.relative_path)
        return (
            not record_path.is_absolute()
            and ".." not in record_path.parts
            and record_path.as_posix() == canonical_relative_path
        )

    matching = [
        record
        for record in records
        if record.status in {"committed", "delivered"}
        and record.sha256 == digest
        and has_same_canonical_path(record)
    ]
    if not matching:
        return None
    explicitly_primary = [
        record
        for record in matching
        if record.artifact_id in primary_artifact_ids
    ]
    trusted = explicitly_primary or matching
    roles = {record.artifact_role for record in trusted}
    return next(iter(roles)) if len(roles) == 1 else None


async def _project_works(
    workspace_dir: str | Path,
    works: list[WorkItem],
) -> list[WorkProjection]:
    run_store = RuntimeRunStore(workspace_dir)
    artifact_journal = RuntimeArtifactEffectJournal(workspace_dir)
    contracts: dict[str, tuple[RuntimeRunContract, ...]] = {}
    records_by_attempt: dict[
        tuple[str, str],
        tuple[RuntimeArtifactEffectRecord, ...],
    ] = {}
    projected: list[WorkProjection] = []
    for item in works:
        run_contracts: tuple[RuntimeRunContract, ...] = ()
        if item.source_run_id:
            if item.source_run_id not in contracts:
                try:
                    contracts[
                        item.source_run_id
                    ] = await run_store.list_contract_history(
                        item.source_run_id,
                    )
                except (OSError, ValueError):
                    contracts[item.source_run_id] = ()
            run_contracts = contracts[item.source_run_id]

        completion_contracts = tuple(
            contract
            for contract in run_contracts
            if item.id
            in set(
                getattr(
                    getattr(contract, "completion_evidence", None),
                    "works_item_ids",
                    (),
                )
                or (),
            )
        )
        validation_contracts = tuple(
            contract
            for contract in run_contracts
            if item.id
            in set(
                getattr(
                    getattr(contract, "completion_evidence", None),
                    "validation_work_item_ids",
                    (),
                )
                or (),
            )
        )
        # Completion and validation are distinct facts.  A rejected candidate
        # may have exact validation evidence without ever becoming a delivery.
        role_contracts = (
            completion_contracts or validation_contracts or run_contracts[:1]
        )
        roles: list[Literal["candidate", "primary", "delivery"] | None] = []
        delivery_statuses: set[str | None] = set()
        for contract in role_contracts:
            key = (contract.run_id, contract.attempt_id)
            if key not in records_by_attempt:
                try:
                    records_by_attempt[key] = _latest_artifact_records(
                        artifact_journal.list_records(
                            contract.run_id,
                            contract.attempt_id,
                        ),
                    )
                except (OSError, ValueError):
                    records_by_attempt[key] = ()
            evidence = getattr(contract, "completion_evidence", None)
            primary_artifact_ids = set(
                getattr(evidence, "primary_artifact_ids", ()) or (),
            )
            owner = getattr(contract, "owner", None)
            roles.append(
                _canonical_role_for_work(
                    item,
                    records_by_attempt[key],
                    primary_artifact_ids=primary_artifact_ids,
                    workspace_root=getattr(owner, "workspace_id", None),
                ),
            )
            if contract in completion_contracts:
                delivery_statuses.add(
                    getattr(contract, "delivery_status", None),
                )

        artifact_role = (
            roles[0]
            if roles and all(role == roles[0] for role in roles)
            else None
        )
        completion_bound = bool(completion_contracts)
        validations = [
            validation
            for contract in validation_contracts
            if (validation := _project_validation(item, contract)) is not None
        ]
        validation = max(
            validations,
            key=lambda candidate: (
                candidate.checked_at,
                candidate.source_attempt_id,
                candidate.record_id,
            ),
            default=None,
        )
        projected.append(
            WorkProjection(
                **item.model_dump(),
                artifact_role=artifact_role,
                completion_bound=completion_bound,
                delivery_status=(
                    next(iter(delivery_statuses))
                    if completion_bound and len(delivery_statuses) == 1
                    else None
                ),
                validation=validation,
            ),
        )
    return projected


@router.get("", response_model=list[WorkProjection])
async def get_works(request: Request, chat_id: str | None = None):
    workspace = await get_agent_for_request(request)
    try:
        works = await run_in_threadpool(
            list_works,
            workspace.workspace_dir,
            source_chat_id=chat_id,
        )
    except WorksIndexUnavailable as exc:
        _raise_works_index_http(exc)
    return await _project_works(workspace.workspace_dir, works)


@router.get("/index/diagnostics", response_model=WorksIndexInspection)
async def get_works_index_diagnostics(request: Request):
    workspace = await get_agent_for_request(request)
    try:
        return await run_in_threadpool(
            inspect_works_index,
            workspace.workspace_dir,
        )
    except WorksIndexUnavailable as exc:
        _raise_works_index_http(exc)


@router.get(
    "/index/recovery-preview",
    response_model=WorksIndexRecoveryPreview,
)
async def get_works_index_recovery_preview(
    snapshot_digest: str,
    request: Request,
):
    workspace = await get_agent_for_request(request)
    try:
        return await run_in_threadpool(
            preview_works_index_recovery,
            workspace.workspace_dir,
            snapshot_digest=snapshot_digest,
        )
    except WorksIndexUnavailable as exc:
        _raise_works_index_http(exc)


@router.post("/index/recovery", response_model=WorksIndexRestoreResult)
async def recover_works_index(
    payload: WorksIndexRestoreRequest,
    request: Request,
):
    workspace = await get_agent_for_request(request)
    try:
        return await run_in_threadpool(
            restore_works_index,
            workspace.workspace_dir,
            expected_current_digest=payload.expected_current_digest,
            snapshot_digest=payload.snapshot_digest,
        )
    except WorksIndexUnavailable as exc:
        _raise_works_index_http(exc)


@router.post("", response_model=WorkItem)
async def create_work(payload: AddWorkRequest, request: Request):
    workspace = await get_agent_for_request(request)
    allowed_roots: list[Path] = [Path(workspace.workspace_dir)]
    if payload.source_chat_id:
        chat = await workspace.chat_manager.get_chat(payload.source_chat_id)
        if chat is None:
            raise HTTPException(
                status_code=404,
                detail="source chat not found",
            )
        effective = resolve_effective_workspace_dir_from_meta(
            chat.meta or {},
            root_workspace_dir=workspace.workspace_dir,
        )
        if effective is not None:
            allowed_roots.append(effective)
    try:
        return add_work(
            workspace.workspace_dir,
            source_path=payload.path,
            allowed_roots=allowed_roots,
            title=payload.title,
            source_chat_id=payload.source_chat_id,
            source_run_id=payload.source_run_id,
        )
    except WorksIndexUnavailable as exc:
        _raise_works_index_http(exc)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail="work source no longer exists",
        ) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{work_id}/derivations", response_model=WorkItem)
async def create_work_derivation(
    work_id: str,
    payload: WorkDerivationRequest,
    request: Request,
):
    """Create a PDF child with a durable relation to one Work revision."""

    workspace = await get_agent_for_request(request)
    item = _require_usable_work(workspace.workspace_dir, work_id)
    _require_chat_owned_work(item, payload.chat_id)
    try:
        return await run_in_threadpool(
            derive_work_to_pdf,
            workspace.workspace_dir,
            work_id,
        )
    except WorksIndexUnavailable as exc:
        _raise_works_index_http(exc)
    except OfficeConversionUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WorkDerivationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/{work_id}/edit-session", response_model=ChatSpec)
async def create_work_edit_session(work_id: str, request: Request):
    """Create an independent chat whose active workspace is this saved work."""

    workspace = await get_agent_for_request(request)
    _require_usable_work(workspace.workspace_dir, work_id)
    item, saved_entry_path = _require_managed_work_file(
        workspace.workspace_dir,
        work_id,
    )
    try:
        entry_path, parent_digest = create_work_edit_copy(
            workspace.workspace_dir,
            item,
            saved_entry_path,
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    source_chat_name = None
    if item.source_chat_id:
        source_chat = await workspace.chat_manager.get_chat(
            item.source_chat_id,
        )
        if source_chat is not None:
            source_chat_name = source_chat.name

    session_id = f"console:work-edit:{work_id}:{uuid4()}"
    chat = await workspace.chat_manager.get_or_create_chat(
        session_id,
        "default",
        "console",
        name=f"修改：{item.title}",
    )
    coding_mode = item.kind in {"html", "markdown"} or (
        item.mime_type or ""
    ).startswith("text/")
    binding = await ensure_chat_topic_workspace(
        workspace,
        workspace.chat_manager,
        chat,
        manual_workspace_dir=str(entry_path.parent),
        coding_mode=coding_mode,
        agent_id=workspace.agent_id,
    )
    context = build_work_edit_context(
        item,
        entry_path,
        source_chat_name=source_chat_name,
    )
    updated = await workspace.chat_manager.merge_chat_meta(
        binding.chat.id,
        {
            "work_edit_session": {
                "schema_version": 1,
                "work_id": item.id,
                "title": item.title,
                "kind": item.kind,
                "source_version": item.version,
            },
            "work_edit_id": item.id,
            "work_edit_entry_path": str(entry_path),
            "work_edit_parent_digest": parent_digest,
            "work_edit_parent_version": item.version,
            "work_edit_lineage_root_id": item.lineage_root_id or item.id,
            "work_edit_context": context,
            "work_edit_source_chat_id": item.source_chat_id or "",
            "work_edit_source_run_id": item.source_run_id or "",
            **({"coding_context_snapshot": context} if coding_mode else {}),
        },
    )
    return updated or binding.chat


@router.post("/{work_id}/edit-copy", response_model=WorkEditCopyResponse)
async def create_canonical_work_edit_copy(
    work_id: str,
    payload: WorkEditCopyRequest,
    request: Request,
):
    """Prepare a revision-bound edit branch for the canonical chat."""

    workspace = await get_agent_for_request(request)
    item = _require_usable_work(workspace.workspace_dir, work_id)
    chat = await workspace.chat_manager.get_chat(payload.chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="canonical chat not found")
    if item.source_chat_id != payload.chat_id:
        raise HTTPException(
            status_code=409,
            detail="work is not an outcome of the requested canonical chat",
        )
    item, saved_entry_path = _require_managed_work_file(
        workspace.workspace_dir,
        work_id,
    )
    try:
        entry_path, parent_digest = await run_in_threadpool(
            create_work_edit_copy,
            workspace.workspace_dir,
            item,
            saved_entry_path,
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return WorkEditCopyResponse(
        work_id=item.id,
        asset_id=item.artifact_id,
        edit_path=str(entry_path),
        base_version=item.version,
        base_digest=parent_digest,
        lineage_root_id=item.lineage_root_id or item.id,
    )


@router.get("/{work_id}/preview", response_model=WorkPreviewResponse)
async def get_work_preview(
    work_id: str,
    request: Request,
    chat_id: str | None = None,
):
    workspace = await get_agent_for_request(request)
    item = _require_usable_work(workspace.workspace_dir, work_id)
    _require_chat_owned_work(item, chat_id)
    try:
        preview = await run_in_threadpool(
            get_or_create_work_preview,
            workspace.workspace_dir,
            work_id,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        detail = str(exc)
        status_code = 415 if "not previewable" in detail else 409
        raise HTTPException(status_code=status_code, detail=detail) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    _revalidate_rendered_work(
        workspace.workspace_dir,
        work_id,
        chat_id=chat_id,
        expected_digest=item.source_digest,
    )
    return WorkPreviewResponse(
        work_id=preview.work_id,
        source_digest=preview.source_digest,
        kind=preview.kind,
        renderer=preview.renderer,
        renderer_version=preview.renderer_version,
        page_count=preview.page_count,
        pages=[
            WorkPreviewPageResponse(
                number=page.number,
                width=page.width,
                height=page.height,
            )
            for page in preview.pages
        ],
    )


@router.get("/{work_id}/text-preview", response_model=WorkTextPreviewResponse)
async def get_work_text_preview(
    work_id: str,
    request: Request,
    chat_id: str | None = None,
):
    """Extract readable text from a chat-owned, immutable Office Work."""

    workspace = await get_agent_for_request(request)
    item = _require_usable_work(workspace.workspace_dir, work_id)
    _require_chat_owned_work(item, chat_id)
    if item.kind not in {"document", "spreadsheet"}:
        raise HTTPException(
            status_code=415,
            detail=f"work format has no text preview: {item.kind}",
        )
    managed_item, path = _require_managed_work_file(
        workspace.workspace_dir,
        work_id,
    )
    if not managed_item.source_digest:
        raise HTTPException(
            status_code=409,
            detail="saved work integrity is unknown",
        )
    try:
        preview = await run_in_threadpool(
            _preview_managed_office_document,
            path,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except _OfficePreviewLimitError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    _revalidate_rendered_work(
        workspace.workspace_dir,
        work_id,
        chat_id=chat_id,
        expected_digest=managed_item.source_digest,
    )

    maximum_bytes = 2 * 1024 * 1024
    encoded = preview.content.encode("utf-8")
    truncated = len(encoded) > maximum_bytes
    content = (
        encoded[:maximum_bytes].decode("utf-8", errors="ignore")
        if truncated
        else preview.content
    )
    return WorkTextPreviewResponse(
        work_id=managed_item.id,
        source_digest=managed_item.source_digest,
        kind=managed_item.kind,
        method=preview.method,
        content_type=preview.content_type,
        content=content,
        truncated=truncated,
        message=preview.message,
    )


@router.get("/{work_id}/preview/pages/{page}")
async def get_work_preview_image(
    work_id: str,
    page: int,
    digest: str,
    request: Request,
    chat_id: str | None = None,
):
    if page < 1:
        raise HTTPException(status_code=404, detail="preview page not found")
    workspace = await get_agent_for_request(request)
    item = _require_usable_work(workspace.workspace_dir, work_id)
    _require_chat_owned_work(item, chat_id)
    try:
        path = await run_in_threadpool(
            get_work_preview_page,
            workspace.workspace_dir,
            work_id,
            page=page,
            source_digest=digest,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    _revalidate_rendered_work(
        workspace.workspace_dir,
        work_id,
        chat_id=chat_id,
        expected_digest=digest,
    )
    return FileResponse(
        path,
        media_type="image/png",
        filename=path.name,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@router.get("/{work_id}/content")
async def get_work_content(
    work_id: str,
    request: Request,
    chat_id: str | None = None,
):
    workspace = await get_agent_for_request(request)
    item = _require_usable_work(workspace.workspace_dir, work_id)
    _require_chat_owned_work(item, chat_id)
    item, path = _require_managed_work_file(workspace.workspace_dir, work_id)
    return FileResponse(
        path,
        media_type=item.mime_type,
        filename=path.name,
        content_disposition_type="inline",
    )


@router.get("/{work_id}/cover")
async def get_work_cover(work_id: str, request: Request):
    workspace = await get_agent_for_request(request)
    try:
        managed = get_work_cover_file(workspace.workspace_dir, work_id)
    except WorksIndexUnavailable as exc:
        _raise_works_index_http(exc)
    if managed is None:
        raise HTTPException(status_code=404, detail="work cover not found")
    item, path = managed
    return FileResponse(
        path,
        media_type=item.cover_mime_type or "image/png",
        filename=path.name,
        content_disposition_type="inline",
    )


@router.post("/{work_id}/cover", response_model=WorkItem)
async def create_work_cover(work_id: str, request: Request):
    workspace = await get_agent_for_request(request)
    _require_usable_work(workspace.workspace_dir, work_id)
    item, _ = _require_managed_work_file(workspace.workspace_dir, work_id)
    config = load_config().image_generation
    if not config.works_cover_enabled:
        raise HTTPException(
            status_code=409,
            detail="AI Works covers are disabled",
        )
    try:
        return await generate_ai_cover(workspace.workspace_dir, item, config)
    except WorksIndexUnavailable as exc:
        _raise_works_index_http(exc)
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.delete("/{work_id}")
async def delete_work(work_id: str, request: Request):
    workspace = await get_agent_for_request(request)
    try:
        removed = remove_work(workspace.workspace_dir, work_id)
    except WorksIndexUnavailable as exc:
        _raise_works_index_http(exc)
    if removed is None:
        raise HTTPException(status_code=404, detail="work not found")
    return {"removed": True, "source_preserved": True}
