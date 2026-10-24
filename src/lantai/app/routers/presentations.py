# -*- coding: utf-8 -*-
"""Independent, conversation-scoped presentation generation API."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator

from ...presentation.models import MAX_OBJECT_EDIT_SLIDES, PresentationJobRequest, SourceFactCorrection
from ...presentation.intelligence import (
    UtilityPresentationObjectEditPlanner,
    UtilityPresentationPageAuthor,
    UtilityPresentationRepairDesigner,
    UtilityPresentationSemanticPlanner,
    UtilityPresentationVisualCritic,
)
from ...presentation.runtime import PresentationJobRuntime
from ..agent_context import get_agent_for_request


router = APIRouter(prefix="/presentations", tags=["presentations"])
_runtimes: dict[str, PresentationJobRuntime] = {}
_runtime_lock = threading.Lock()


class JobAction(BaseModel):
    conversation_id: str = Field(min_length=1)


class SlideRedo(JobAction):
    slide_id: str = Field(min_length=1)
    additional_slide_ids: list[str] = Field(
        default_factory=list, max_length=MAX_OBJECT_EDIT_SLIDES - 1,
    )
    required_slide_ids: list[str] = Field(
        default_factory=list, max_length=MAX_OBJECT_EDIT_SLIDES,
    )
    instruction: str = Field(min_length=1, max_length=4000)
    base_output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_fact_corrections: list[SourceFactCorrection] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_page_scope(self) -> "SlideRedo":
        pages = [self.slide_id, *self.additional_slide_ids]
        if any(not page for page in pages) or len(set(pages)) != len(pages):
            raise ValueError("edit page scope must contain unique nonempty slide IDs")
        required = self.required_slide_ids
        if (any(not page for page in required) or len(set(required)) != len(required)
                or not set(required).issubset(pages)):
            raise ValueError("required edit page scope must be a unique nonempty subset of requested scope")
        return self


class VersionRollback(JobAction):
    target_version_id: str = Field(min_length=1)
    base_output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExternalSave(JobAction):
    base_output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    writer: Literal["genoffice_human", "office_cli"] = "genoffice_human"
    auto_reconcile: bool = True


class ExternalReconcile(JobAction):
    base_output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


async def _runtime(request: Request) -> PresentationJobRuntime:
    workspace = await get_agent_for_request(request)
    root = Path(workspace.workspace_dir) / ".lantai" / "presentation-jobs"
    key = str(root.resolve())
    with _runtime_lock:
        runtime = _runtimes.get(key)
        if runtime is None:
            runtime = PresentationJobRuntime(
                root,
                semantic_planner=UtilityPresentationSemanticPlanner(
                    workspace.agent_id,
                ),
                page_author=UtilityPresentationPageAuthor(
                    workspace.agent_id,
                ),
                object_edit_planner=UtilityPresentationObjectEditPlanner(
                    workspace.agent_id,
                ),
                visual_critic=UtilityPresentationVisualCritic(
                    workspace.agent_id,
                ),
                repair_designer=UtilityPresentationRepairDesigner(
                    workspace.agent_id,
                ),
            )
            _runtimes[key] = runtime
        return runtime


def _raise_job_error(exc: Exception) -> None:
    if isinstance(exc, KeyError):
        raise HTTPException(
            status_code=404,
            detail="presentation job not found",
        ) from exc
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, FileNotFoundError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/jobs")
async def create_job(payload: PresentationJobRequest, request: Request):
    return (await _runtime(request)).create_and_start(payload)


@router.get("/jobs")
async def list_jobs(
    request: Request,
    conversation_id: str = Query(min_length=1),
):
    return (await _runtime(request)).list_jobs(conversation_id)


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    request: Request,
    conversation_id: str = Query(min_length=1),
):
    try:
        return (await _runtime(request)).get(job_id, conversation_id)
    except Exception as exc:
        _raise_job_error(exc)


@router.get("/jobs/{job_id}/events")
async def get_events(
    job_id: str,
    request: Request,
    conversation_id: str = Query(min_length=1),
    after_sequence: int = Query(default=0, ge=0),
):
    try:
        runtime = await _runtime(request)
        return runtime.service.store.events_after(
            job_id,
            after_sequence,
            conversation_id,
        )
    except Exception as exc:
        _raise_job_error(exc)


@router.get("/jobs/{job_id}/artifacts")
async def get_artifacts(
    job_id: str,
    request: Request,
    conversation_id: str = Query(min_length=1),
):
    try:
        return (await _runtime(request)).artifacts(job_id, conversation_id)
    except Exception as exc:
        _raise_job_error(exc)


@router.post("/jobs/{job_id}/pause")
async def pause_job(job_id: str, payload: JobAction, request: Request):
    try:
        return (await _runtime(request)).pause(job_id, payload.conversation_id)
    except Exception as exc:
        _raise_job_error(exc)


@router.post("/jobs/{job_id}/resume")
async def resume_job(job_id: str, payload: JobAction, request: Request):
    try:
        return (await _runtime(request)).resume(
            job_id,
            payload.conversation_id,
        )
    except Exception as exc:
        _raise_job_error(exc)


@router.post("/jobs/{job_id}/retry")
async def retry_failed_job(job_id: str, payload: JobAction, request: Request):
    """Retry a classified transient failure without creating another job."""

    try:
        return (await _runtime(request)).retry_failed(
            job_id,
            payload.conversation_id,
        )
    except Exception as exc:
        _raise_job_error(exc)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, payload: JobAction, request: Request):
    try:
        return (await _runtime(request)).cancel(
            job_id,
            payload.conversation_id,
        )
    except Exception as exc:
        _raise_job_error(exc)


@router.post("/jobs/{job_id}/redo-slide")
async def redo_slide(job_id: str, payload: SlideRedo, request: Request):
    try:
        runtime = await _runtime(request)
        return await asyncio.to_thread(
            runtime.redo_slide,
            job_id,
            payload.conversation_id,
            payload.slide_id,
            payload.instruction,
            payload.base_output_sha256,
            **({"additional_slide_ids": payload.additional_slide_ids}
               if payload.additional_slide_ids else {}),
            **({"required_slide_ids": payload.required_slide_ids}
               if payload.required_slide_ids else {}),
            **({"source_fact_corrections": payload.source_fact_corrections}
               if payload.source_fact_corrections else {}),
        )
    except Exception as exc:
        _raise_job_error(exc)


@router.post("/jobs/{job_id}/edit-slide")
async def edit_slide(job_id: str, payload: SlideRedo, request: Request):
    """Natural-language object edit alias used by the GenOffice handoff."""

    return await redo_slide(job_id, payload, request)


@router.post("/jobs/{job_id}/external-save")
async def register_external_save(
    job_id: str,
    payload: ExternalSave,
    request: Request,
):
    try:
        runtime = await _runtime(request)
        external = await asyncio.to_thread(
            runtime.register_editor_save,
            job_id,
            payload.conversation_id,
            base_output_sha256=payload.base_output_sha256,
            writer=payload.writer,
        )
        if not payload.auto_reconcile:
            return external
        return await asyncio.to_thread(
            runtime.reconcile_editor_save,
            job_id,
            payload.conversation_id,
            base_output_sha256=external.pptx_sha256,
        )
    except Exception as exc:
        _raise_job_error(exc)


@router.post("/jobs/{job_id}/reconcile-external-save")
async def reconcile_external_save(
    job_id: str,
    payload: ExternalReconcile,
    request: Request,
):
    try:
        runtime = await _runtime(request)
        return await asyncio.to_thread(
            runtime.reconcile_editor_save,
            job_id,
            payload.conversation_id,
            base_output_sha256=payload.base_output_sha256,
        )
    except Exception as exc:
        _raise_job_error(exc)


@router.post("/jobs/{job_id}/rollback-version")
async def rollback_version(
    job_id: str,
    payload: VersionRollback,
    request: Request,
):
    try:
        runtime = await _runtime(request)
        return await asyncio.to_thread(
            runtime.rollback_version,
            job_id,
            payload.conversation_id,
            target_version_id=payload.target_version_id,
            base_output_sha256=payload.base_output_sha256,
        )
    except Exception as exc:
        _raise_job_error(exc)


@router.get("/jobs/{job_id}/output")
async def download_output(
    job_id: str,
    request: Request,
    conversation_id: str = Query(min_length=1),
):
    try:
        path = (await _runtime(request)).output_path(job_id, conversation_id)
        return FileResponse(
            path,
            media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            filename=path.name,
        )
    except Exception as exc:
        _raise_job_error(exc)
