# -*- coding: utf-8 -*-
"""Bounded API for the opt-in private-editor experiment."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from ..agent_context import get_agent_for_request
from ..private_editor import (
    AdoptionReceipt,
    AdoptionRequest,
    AngleProposal,
    Candidate,
    ChangeGroupDraft,
    ExperimentDisabledError,
    PrivateEditorConflictError,
    PrivateEditorDeletionReceipt,
    PrivateEditorDiagnostics,
    PrivateEditorError,
    PrivateEditorNotFoundError,
    PrivateEditorProject,
    PrivateEditorResearchExport,
    PrivateEditorSettings,
    PrivateEditorStore,
    ProtectedContentError,
    RecordEntry,
    RecordEntryDraft,
    RevertReceipt,
    RevertRequest,
    build_private_editor_context,
)
from ..runner.api import ensure_chat_topic_workspace
from ..runner.models import ChatSpec
from ..works import create_work_edit_copy, get_managed_work_file


router = APIRouter(prefix="/private-editor", tags=["private-editor"])


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    research_consent: bool | None = None


class PrivateEditorSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: PrivateEditorProject
    chat: ChatSpec


class AuthorRecordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_current_work_id: str
    expected_current_digest: str
    expected_record_revision: int
    expected_revocation_epoch: int
    kind: Literal[
        "author_intent",
        "decision",
        "open_question",
        "protected_quote",
        "material_note",
    ]
    text: str
    supersedes: list[str] = Field(default_factory=list, max_length=100)


class ResolveRecordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_current_work_id: str
    expected_current_digest: str
    expected_record_revision: int
    expected_revocation_epoch: int
    resolution: Literal["accepted", "rejected"]


class ResolveAngleProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_current_work_id: str
    expected_current_digest: str
    expected_record_revision: int
    expected_revocation_epoch: int
    resolution: Literal["continue", "later", "not_suitable", "delete"]
    reason: str | None = Field(default=None, max_length=20_000)


class InvalidateSourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_current_work_id: str
    expected_current_digest: str
    expected_record_revision: int
    expected_revocation_epoch: int
    source_ref: str = Field(min_length=1, max_length=500)


class StageCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_base_work_id: str
    expected_base_digest: str
    expected_record_revision: int
    expected_revocation_epoch: int
    change_groups: list[ChangeGroupDraft]


class AdoptCandidateRequest(AdoptionRequest):
    """No model-supplied confirmation flag is accepted by this schema."""


class CaptureWorkingCopyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_base_work_id: str
    expected_base_digest: str
    expected_record_revision: int
    expected_revocation_epoch: int


class RevertProjectRequest(RevertRequest):
    """The server supplies the authorization source for this pointer change."""


class CurrentBodyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    work_id: str
    digest: str
    body: str
    import_scope: str
    warnings: list[str]


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, PrivateEditorNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ExperimentDisabledError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (PrivateEditorConflictError, ProtectedContentError)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


async def _sync_chat_context(workspace, project: PrivateEditorProject) -> ChatSpec | None:
    if not project.chat_id:
        return None
    chat = await workspace.chat_manager.get_chat(project.chat_id)
    if chat is None:
        return None
    context = build_private_editor_context(
        project, workspace_dir=workspace.workspace_dir,
    )
    return await workspace.chat_manager.merge_chat_meta(
        chat.id,
        {
            "private_editor_project_id": project.id,
            "private_editor_original_work_id": project.original_work_id,
            "private_editor_current_work_id": project.current_work_id,
            "private_editor_current_digest": project.current_digest,
            "private_editor_record_revision": project.record_revision,
            "private_editor_revocation_epoch": project.revocation_epoch,
            "work_method_id": "private_editor",
            "work_edit_context": context,
            **(
                {"coding_context_snapshot": context}
                if project.working_copy_path
                and Path(project.working_copy_path).suffix.lower()
                in {".txt", ".md", ".markdown"}
                else {}
            ),
        },
    )


@router.get("/settings", response_model=PrivateEditorSettings)
async def get_settings(request: Request) -> PrivateEditorSettings:
    workspace = await get_agent_for_request(request)
    return PrivateEditorStore(workspace.workspace_dir).get_settings()


@router.put("/settings", response_model=PrivateEditorSettings)
async def update_settings(
    payload: SettingsUpdate, request: Request
) -> PrivateEditorSettings:
    workspace = await get_agent_for_request(request)
    return PrivateEditorStore(workspace.workspace_dir).update_settings(
        enabled=payload.enabled,
        research_consent=payload.research_consent,
    )


@router.get("/projects", response_model=list[PrivateEditorProject])
async def list_projects(request: Request) -> list[PrivateEditorProject]:
    workspace = await get_agent_for_request(request)
    return PrivateEditorStore(workspace.workspace_dir).list_projects()


@router.post(
    "/projects/from-work/{work_id}",
    response_model=PrivateEditorSession,
    status_code=status.HTTP_201_CREATED,
)
async def start_or_continue_project(
    work_id: str, request: Request
) -> PrivateEditorSession:
    workspace = await get_agent_for_request(request)
    store = PrivateEditorStore(workspace.workspace_dir)
    try:
        managed = get_managed_work_file(workspace.workspace_dir, work_id)
        if managed is None:
            raise PrivateEditorNotFoundError("managed work version not found")
        source_item, _source_path = managed
        project = store.create_project(work_id, title=source_item.title)
        current = get_managed_work_file(
            workspace.workspace_dir, project.current_work_id
        )
        if current is None:
            raise PrivateEditorNotFoundError("current work version not found")
        current_item, current_path = current
        working_copy = (
            Path(project.working_copy_path).expanduser()
            if project.working_copy_path
            else None
        )
        created_working_copy = False
        if working_copy is None or not working_copy.is_file():
            working_copy, _parent_digest = create_work_edit_copy(
                workspace.workspace_dir,
                current_item,
                current_path,
            )
            created_working_copy = True

        chat = (
            await workspace.chat_manager.get_chat(project.chat_id)
            if project.chat_id
            else None
        )
        if chat is None:
            chat = await workspace.chat_manager.get_or_create_chat(
                f"console:private-editor:{project.id}",
                "default",
                "console",
                name=f"共作：{project.title}",
            )
        coding_mode = working_copy.suffix.lower() in {".md", ".markdown", ".txt"}
        binding = await ensure_chat_topic_workspace(
            workspace,
            workspace.chat_manager,
            chat,
            manual_workspace_dir=str(working_copy.parent),
            coding_mode=coding_mode,
            agent_id=workspace.agent_id,
        )
        project = store.bind_session(
            project.id,
            chat_id=binding.chat.id,
            working_copy_path=working_copy,
            owned_by_experiment=created_working_copy,
        )
        context = build_private_editor_context(
            project, workspace_dir=workspace.workspace_dir,
        )
        updated = await workspace.chat_manager.merge_chat_meta(
            binding.chat.id,
            {
                "private_editor_project_id": project.id,
                "private_editor_original_work_id": project.original_work_id,
                "private_editor_current_work_id": project.current_work_id,
                "private_editor_current_digest": project.current_digest,
                "private_editor_record_revision": project.record_revision,
                "private_editor_revocation_epoch": project.revocation_epoch,
                "work_method_id": "private_editor",
                "work_edit_id": project.current_work_id,
                "work_edit_entry_path": str(working_copy),
                "work_edit_parent_digest": project.current_digest,
                "work_edit_lineage_root_id": current_item.lineage_root_id
                or current_item.id,
                "work_edit_context": context,
                **({"coding_context_snapshot": context} if coding_mode else {}),
            },
        )
        return PrivateEditorSession(project=project, chat=updated or binding.chat)
    except (OSError, ValueError, PrivateEditorNotFoundError, ExperimentDisabledError) as exc:
        raise _http_error(exc) from exc


@router.get("/projects/{project_id}", response_model=PrivateEditorProject)
async def get_project(project_id: str, request: Request) -> PrivateEditorProject:
    workspace = await get_agent_for_request(request)
    try:
        return PrivateEditorStore(workspace.workspace_dir).get_project(project_id)
    except PrivateEditorNotFoundError as exc:
        raise _http_error(exc) from exc


@router.get(
    "/projects/{project_id}/current-body",
    response_model=CurrentBodyResponse,
)
async def get_current_body(
    project_id: str,
    request: Request,
) -> CurrentBodyResponse:
    workspace = await get_agent_for_request(request)
    store = PrivateEditorStore(workspace.workspace_dir)
    try:
        project = store.get_project(project_id)
        return CurrentBodyResponse(
            work_id=project.current_work_id,
            digest=project.current_digest,
            body=store.get_current_body(project_id),
            import_scope=project.import_scope,
            warnings=project.import_warnings,
        )
    except (OSError, ValueError, PrivateEditorError) as exc:
        raise _http_error(exc) from exc


@router.get(
    "/projects/{project_id}/diagnostics",
    response_model=PrivateEditorDiagnostics,
)
async def get_project_diagnostics(
    project_id: str,
    request: Request,
    response: Response,
) -> PrivateEditorDiagnostics:
    workspace = await get_agent_for_request(request)
    try:
        diagnostics = PrivateEditorStore(workspace.workspace_dir).get_diagnostics(
            project_id
        )
        response.headers["Content-Disposition"] = (
            'attachment; filename="private-editor-diagnostics.json"'
        )
        return diagnostics
    except PrivateEditorNotFoundError as exc:
        raise _http_error(exc) from exc


@router.get(
    "/projects/{project_id}/research-export",
    response_model=PrivateEditorResearchExport,
)
async def export_research_diagnostics(
    project_id: str,
    request: Request,
    response: Response,
) -> PrivateEditorResearchExport:
    workspace = await get_agent_for_request(request)
    try:
        export = PrivateEditorStore(workspace.workspace_dir).get_research_export(
            project_id
        )
        response.headers["Content-Disposition"] = (
            'attachment; filename="private-editor-research.json"'
        )
        return export
    except (PermissionError, PrivateEditorNotFoundError) as exc:
        raise _http_error(exc) from exc


@router.post("/projects/{project_id}/records", response_model=RecordEntry)
async def add_record(
    project_id: str, payload: AuthorRecordRequest, request: Request
) -> RecordEntry:
    workspace = await get_agent_for_request(request)
    store = PrivateEditorStore(workspace.workspace_dir)
    try:
        entry = store.add_record_entries(
            project_id,
            expected_current_work_id=payload.expected_current_work_id,
            expected_current_digest=payload.expected_current_digest,
            expected_record_revision=payload.expected_record_revision,
            expected_revocation_epoch=payload.expected_revocation_epoch,
            drafts=[
                RecordEntryDraft(
                    kind=payload.kind,
                    text=payload.text,
                    source="user_explicit",
                    source_ref="button:author-record",
                    status="open" if payload.kind == "open_question" else "accepted",
                    supersedes=payload.supersedes,
                )
            ],
        )[0]
        await _sync_chat_context(workspace, store.get_project(project_id))
        return entry
    except (ValueError, PrivateEditorError) as exc:
        raise _http_error(exc) from exc


@router.post(
    "/projects/{project_id}/records/{record_id}/resolve",
    response_model=RecordEntry,
)
async def resolve_record(
    project_id: str,
    record_id: str,
    payload: ResolveRecordRequest,
    request: Request,
) -> RecordEntry:
    workspace = await get_agent_for_request(request)
    store = PrivateEditorStore(workspace.workspace_dir)
    try:
        entry = store.resolve_record_entry(
            project_id,
            record_id,
            expected_current_work_id=payload.expected_current_work_id,
            expected_current_digest=payload.expected_current_digest,
            expected_record_revision=payload.expected_record_revision,
            expected_revocation_epoch=payload.expected_revocation_epoch,
            resolution=payload.resolution,
            source_ref="button:record-resolution",
        )
        await _sync_chat_context(workspace, store.get_project(project_id))
        return entry
    except (ValueError, PrivateEditorError) as exc:
        raise _http_error(exc) from exc


@router.post(
    "/projects/{project_id}/angle-proposals/{proposal_id}/resolve",
    response_model=AngleProposal,
)
async def resolve_angle_proposal(
    project_id: str,
    proposal_id: str,
    payload: ResolveAngleProposalRequest,
    request: Request,
) -> AngleProposal:
    workspace = await get_agent_for_request(request)
    store = PrivateEditorStore(workspace.workspace_dir)
    try:
        proposal = store.resolve_angle_proposal(
            project_id,
            proposal_id,
            expected_current_work_id=payload.expected_current_work_id,
            expected_current_digest=payload.expected_current_digest,
            expected_record_revision=payload.expected_record_revision,
            expected_revocation_epoch=payload.expected_revocation_epoch,
            resolution=payload.resolution,
            reason=payload.reason,
        )
        await _sync_chat_context(workspace, store.get_project(project_id))
        return proposal
    except (ValueError, PrivateEditorError) as exc:
        raise _http_error(exc) from exc


@router.post(
    "/projects/{project_id}/sources/invalidate",
    response_model=PrivateEditorProject,
)
async def invalidate_source(
    project_id: str,
    payload: InvalidateSourceRequest,
    request: Request,
) -> PrivateEditorProject:
    workspace = await get_agent_for_request(request)
    store = PrivateEditorStore(workspace.workspace_dir)
    try:
        project = store.invalidate_source_ref(
            project_id,
            payload.source_ref,
            expected_current_work_id=payload.expected_current_work_id,
            expected_current_digest=payload.expected_current_digest,
            expected_record_revision=payload.expected_record_revision,
            expected_revocation_epoch=payload.expected_revocation_epoch,
        )
        await _sync_chat_context(workspace, project)
        return project
    except (ValueError, PrivateEditorError) as exc:
        raise _http_error(exc) from exc


@router.post("/projects/{project_id}/candidates", response_model=Candidate)
async def stage_candidate(
    project_id: str, payload: StageCandidateRequest, request: Request
) -> Candidate:
    workspace = await get_agent_for_request(request)
    try:
        return PrivateEditorStore(workspace.workspace_dir).stage_candidate(
            project_id,
            expected_base_work_id=payload.expected_base_work_id,
            expected_base_digest=payload.expected_base_digest,
            expected_record_revision=payload.expected_record_revision,
            expected_revocation_epoch=payload.expected_revocation_epoch,
            change_groups=payload.change_groups,
        )
    except (ValueError, PrivateEditorError) as exc:
        raise _http_error(exc) from exc


@router.post(
    "/projects/{project_id}/candidates/from-working-copy",
    response_model=Candidate,
)
async def capture_working_copy_candidate(
    project_id: str,
    payload: CaptureWorkingCopyRequest,
    request: Request,
) -> Candidate:
    workspace = await get_agent_for_request(request)
    store = PrivateEditorStore(workspace.workspace_dir)
    try:
        candidate = store.capture_working_copy_candidate(
            project_id,
            expected_base_work_id=payload.expected_base_work_id,
            expected_base_digest=payload.expected_base_digest,
            expected_record_revision=payload.expected_record_revision,
            expected_revocation_epoch=payload.expected_revocation_epoch,
        )
        await _sync_chat_context(workspace, store.get_project(project_id))
        return candidate
    except (OSError, ValueError, PrivateEditorError) as exc:
        raise _http_error(exc) from exc


@router.post(
    "/projects/{project_id}/candidates/{candidate_id}/adopt",
    response_model=AdoptionReceipt,
)
async def adopt_candidate(
    project_id: str,
    candidate_id: str,
    payload: AdoptCandidateRequest,
    request: Request,
) -> AdoptionReceipt:
    if payload.candidate_id != candidate_id:
        raise HTTPException(status_code=409, detail="candidate identity mismatch")
    workspace = await get_agent_for_request(request)
    store = PrivateEditorStore(workspace.workspace_dir)
    try:
        receipt = store.adopt_candidate(
            project_id,
            AdoptionRequest.model_validate(payload.model_dump()),
            authorization_source="button:adopt-selected",
        )
        await _sync_chat_context(workspace, store.get_project(project_id))
        return receipt
    except (ValueError, PrivateEditorError) as exc:
        raise _http_error(exc) from exc


@router.post(
    "/projects/{project_id}/revert",
    response_model=RevertReceipt,
)
async def revert_project(
    project_id: str,
    payload: RevertProjectRequest,
    request: Request,
) -> RevertReceipt:
    workspace = await get_agent_for_request(request)
    store = PrivateEditorStore(workspace.workspace_dir)
    try:
        receipt = store.revert_project(
            project_id,
            RevertRequest.model_validate(payload.model_dump()),
            authorization_source="button:revert-version",
        )
        await _sync_chat_context(workspace, store.get_project(project_id))
        return receipt
    except (ValueError, PrivateEditorError) as exc:
        raise _http_error(exc) from exc


@router.get("/projects/{project_id}/export-current")
async def export_current(project_id: str, request: Request):
    workspace = await get_agent_for_request(request)
    temporary_path: Path | None = None
    try:
        store = PrivateEditorStore(workspace.workspace_dir)
        project = store.get_project(project_id)
        managed = get_managed_work_file(workspace.workspace_dir, project.current_work_id)
        if managed is None:
            raise PrivateEditorNotFoundError("current work version not found")
        item, path = managed
        file_descriptor, raw_temporary_path = tempfile.mkstemp(suffix=path.suffix)
        os.close(file_descriptor)
        temporary_path = Path(raw_temporary_path)
        store.write_clean_export(project_id, temporary_path)
        return FileResponse(
            temporary_path,
            media_type=item.mime_type,
            filename=f"{path.stem}-clean{path.suffix}",
            content_disposition_type="attachment",
            background=BackgroundTask(temporary_path.unlink, missing_ok=True),
        )
    except (OSError, ValueError, PrivateEditorNotFoundError) as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise _http_error(exc) from exc


_PRIVATE_EDITOR_CHAT_META_KEYS = [
    "private_editor_project_id",
    "private_editor_original_work_id",
    "private_editor_current_work_id",
    "private_editor_current_digest",
    "private_editor_record_revision",
    "private_editor_revocation_epoch",
    "work_edit_id",
    "work_edit_entry_path",
    "work_edit_parent_digest",
    "work_edit_lineage_root_id",
    "work_edit_context",
    "coding_context_snapshot",
]


@router.delete(
    "/projects/{project_id}",
    response_model=PrivateEditorDeletionReceipt,
)
async def delete_project(
    project_id: str, request: Request
) -> PrivateEditorDeletionReceipt:
    workspace = await get_agent_for_request(request)
    try:
        store = PrivateEditorStore(workspace.workspace_dir)
        project = store.get_project(project_id)
        if project.chat_id:
            chat = await workspace.chat_manager.get_chat(project.chat_id)
            if (
                chat is not None
                and (chat.meta or {}).get("private_editor_project_id") == project.id
            ):
                remove_keys = list(_PRIVATE_EDITOR_CHAT_META_KEYS)
                if (chat.meta or {}).get("work_method_id") == "private_editor":
                    remove_keys.append("work_method_id")
                await workspace.chat_manager.merge_chat_meta(
                    project.chat_id,
                    {},
                    remove_keys=remove_keys,
                )
        return store.delete_project(project_id)
    except PrivateEditorNotFoundError as exc:
        raise _http_error(exc) from exc
