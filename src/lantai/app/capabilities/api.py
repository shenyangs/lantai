# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..agent_context import get_agent_for_request
from .collaboration_distillation import (
    encode_sse_event,
    stream_collaboration_draft_events,
)
from .models import (
    Capability,
    CapabilityDraft,
    CapabilityRun,
    CapabilityRunRequest,
    CollaborationDraftRequest,
    EvaluationReport,
    EvolutionProposal,
    EvolutionProposalReviewRequest,
    Pipeline,
    PipelineRun,
    PipelineRunRequest,
    TaskRun,
    TaskStep,
)
from .policy import is_app_store_distribution
from .repo import CapabilityRepository
from .service import CapabilityService
from .task_run_materializer import materialize_task_runs

capabilities_router = APIRouter(prefix="/capabilities", tags=["capabilities"])
pipelines_router = APIRouter(prefix="/pipelines", tags=["pipelines"])
task_runs_router = APIRouter(prefix="/task-runs", tags=["task-runs"])


async def _service_for_request(request: Request) -> tuple[Any, CapabilityService]:
    workspace = await get_agent_for_request(request)
    repo = CapabilityRepository(workspace.workspace_dir)
    return workspace, CapabilityService(repo)


def _status_from_chat(status: str | None) -> str:
    return "running" if status == "running" else "succeeded"


def _status_from_cron(status: str | None) -> str:
    if status == "running":
        return "running"
    if status == "error":
        return "failed"
    if status == "cancelled":
        return "cancelled"
    return "succeeded" if status == "success" else "running"


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


async def _synthetic_task_runs(workspace: Any) -> list[TaskRun]:
    runs: list[TaskRun] = []

    chat_manager = getattr(workspace, "chat_manager", None)
    if chat_manager is not None:
        try:
            chats = await chat_manager.list_chats()
            for chat in chats[:80]:
                pending = {}
                if isinstance(getattr(chat, "meta", None), dict):
                    raw_pending = chat.meta.get("pending_turn")
                    if isinstance(raw_pending, dict):
                        pending = raw_pending
                started_at = _parse_dt(
                    pending.get("created_at")
                    or getattr(chat, "updated_at", None)
                    or getattr(chat, "created_at", None)
                )
                intent = str(pending.get("text") or getattr(chat, "name", "") or "")
                runs.append(
                    TaskRun(
                        id=f"chat:{chat.id}",
                        agent_id=workspace.agent_id,
                        source="chat",
                        chat_id=chat.id,
                        session_id=chat.session_id,
                        title=chat.name or "聊天任务",
                        user_intent=intent,
                        status=_status_from_chat(chat.status),
                        started_at=started_at,
                        ended_at=None if chat.status == "running" else started_at,
                        steps=[
                            TaskStep(
                                index=0,
                                kind="message",
                                title="聊天任务",
                                detail=intent or "已记录会话",
                                status=(
                                    "running"
                                    if chat.status == "running"
                                    else "succeeded"
                                ),
                                started_at=started_at,
                                ended_at=None if chat.status == "running" else started_at,
                            )
                        ],
                    )
                )
        except Exception:
            pass

    cron_manager = getattr(workspace, "cron_manager", None)
    if cron_manager is not None:
        try:
            jobs = await cron_manager.list_jobs()
            for job in jobs:
                state = cron_manager.get_state(job.id) if job.id else None
                status = getattr(state, "last_status", None) if state else None
                run_time = _parse_dt(
                    getattr(state, "last_run_at", None)
                    or getattr(state, "next_run_at", None)
                )
                runs.append(
                    TaskRun(
                        id=f"cron:{job.id}",
                        agent_id=workspace.agent_id,
                        source="cron",
                        title=job.name,
                        user_intent=(
                            job.text
                            or getattr(job.request, "input", None)
                            or "定时能力任务"
                        ),
                        status=_status_from_cron(status),
                        started_at=run_time,
                        ended_at=run_time if status else None,
                        steps=[
                            TaskStep(
                                index=0,
                                kind="message",
                                title="定时任务",
                                detail=str(
                                    job.text
                                    or getattr(job.request, "input", None)
                                    or getattr(job.capability_request, "capability_id", "")
                                ),
                                status="succeeded" if status == "success" else "running",
                                started_at=run_time,
                                ended_at=run_time if status else None,
                            )
                        ],
                    )
                )
        except Exception:
            pass

    return runs


async def _find_task_run(request: Request, run_id: str) -> TaskRun | None:
    workspace, service = await _service_for_request(request)
    persisted = await service.get_task_run(run_id)
    if persisted is not None:
        return persisted
    for run in materialize_task_runs(
        workspace.workspace_dir,
        agent_id=workspace.agent_id,
        limit=300,
    ):
        if run.id == run_id:
            return run
    for run in await _synthetic_task_runs(workspace):
        if run.id == run_id:
            return run
    return None


async def _all_task_runs(request: Request) -> tuple[Any, CapabilityService, list[TaskRun]]:
    workspace, service = await _service_for_request(request)
    persisted = await service.list_task_runs()
    materialized = materialize_task_runs(
        workspace.workspace_dir,
        agent_id=workspace.agent_id,
        limit=300,
    )
    synthetic = await _synthetic_task_runs(workspace)
    by_id = {run.id: run for run in synthetic}
    by_id.update({run.id: run for run in materialized})
    by_id.update({run.id: run for run in persisted})
    runs = sorted(by_id.values(), key=lambda item: item.started_at, reverse=True)[:200]
    return workspace, service, runs


@capabilities_router.get("/policy")
async def get_capability_policy():
    app_store = is_app_store_distribution()
    return {
        "app_store_distribution": app_store,
        "gui_steps_enabled": not app_store,
        "gui_steps_manual_only": True,
    }


@capabilities_router.get("", response_model=list[Capability])
async def list_capabilities(request: Request):
    _, service = await _service_for_request(request)
    return await service.list_capabilities()


@capabilities_router.post("", response_model=Capability)
async def create_capability(capability: Capability, request: Request):
    _, service = await _service_for_request(request)
    return await service.create_capability(capability)


@capabilities_router.get("/runs/{run_id}", response_model=CapabilityRun)
async def get_capability_run(run_id: str, request: Request):
    _, service = await _service_for_request(request)
    run = await service.get_capability_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="capability run not found")
    return run


@capabilities_router.get("/{capability_id}", response_model=Capability)
async def get_capability(capability_id: str, request: Request):
    _, service = await _service_for_request(request)
    capability = await service.get_capability(capability_id)
    if capability is None:
        raise HTTPException(status_code=404, detail="capability not found")
    return capability


@capabilities_router.put("/{capability_id}", response_model=Capability)
async def update_capability(
    capability_id: str,
    capability: Capability,
    request: Request,
):
    _, service = await _service_for_request(request)
    return await service.update_capability(capability_id, capability)


@capabilities_router.delete("/{capability_id}")
async def delete_capability(capability_id: str, request: Request):
    _, service = await _service_for_request(request)
    ok = await service.delete_capability(capability_id)
    if not ok:
        raise HTTPException(status_code=404, detail="capability not found")
    return {"deleted": True}


@capabilities_router.post("/{capability_id}/run", response_model=CapabilityRun)
async def run_capability(
    capability_id: str,
    payload: CapabilityRunRequest,
    request: Request,
):
    workspace, service = await _service_for_request(request)
    try:
        return await service.run_capability(
            capability_id,
            payload,
            runner=workspace.runner,
            workspace_dir=str(workspace.workspace_dir),
            agent_id=workspace.agent_id,
            channel_manager=workspace.channel_manager,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="capability not found") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@pipelines_router.get("", response_model=list[Pipeline])
async def list_pipelines(request: Request):
    _, service = await _service_for_request(request)
    return await service.list_pipelines()


@pipelines_router.post("", response_model=Pipeline)
async def create_pipeline(pipeline: Pipeline, request: Request):
    _, service = await _service_for_request(request)
    try:
        return await service.create_pipeline(pipeline)
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@pipelines_router.get("/runs/{run_id}", response_model=PipelineRun)
async def get_pipeline_run(run_id: str, request: Request):
    _, service = await _service_for_request(request)
    run = await service.get_pipeline_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="pipeline run not found")
    return run


@pipelines_router.get("/{pipeline_id}", response_model=Pipeline)
async def get_pipeline(pipeline_id: str, request: Request):
    _, service = await _service_for_request(request)
    pipeline = await service.get_pipeline(pipeline_id)
    if pipeline is None:
        raise HTTPException(status_code=404, detail="pipeline not found")
    return pipeline


@pipelines_router.put("/{pipeline_id}", response_model=Pipeline)
async def update_pipeline(
    pipeline_id: str,
    pipeline: Pipeline,
    request: Request,
):
    _, service = await _service_for_request(request)
    try:
        return await service.update_pipeline(pipeline_id, pipeline)
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@pipelines_router.delete("/{pipeline_id}")
async def delete_pipeline(pipeline_id: str, request: Request):
    _, service = await _service_for_request(request)
    ok = await service.delete_pipeline(pipeline_id)
    if not ok:
        raise HTTPException(status_code=404, detail="pipeline not found")
    return {"deleted": True}


@pipelines_router.post("/{pipeline_id}/run", response_model=PipelineRun)
async def run_pipeline(
    pipeline_id: str,
    payload: PipelineRunRequest,
    request: Request,
):
    workspace, service = await _service_for_request(request)
    try:
        return await service.run_pipeline(
            pipeline_id,
            payload,
            runner=workspace.runner,
            workspace_dir=str(workspace.workspace_dir),
            agent_id=workspace.agent_id,
            channel_manager=workspace.channel_manager,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="pipeline not found") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@task_runs_router.get("", response_model=list[TaskRun])
async def list_task_runs(request: Request):
    _, _, runs = await _all_task_runs(request)
    return runs


@task_runs_router.post("/collaboration-drafts/stream")
async def stream_collaboration_draft(
    payload: CollaborationDraftRequest,
    request: Request,
):
    workspace, _ = await _service_for_request(request)

    async def events():
        async for event in stream_collaboration_draft_events(workspace, payload):
            if await request.is_disconnected():
                break
            yield encode_sse_event(event)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@task_runs_router.get(
    "/evolution-proposals",
    response_model=list[EvolutionProposal],
)
async def list_evolution_proposals(request: Request):
    _, service, runs = await _all_task_runs(request)
    return await service.list_evolution_proposals(runs)


async def _find_evolution_proposal(
    request: Request,
    proposal_id: str,
) -> tuple[CapabilityService, list[TaskRun], EvolutionProposal]:
    _, service, runs = await _all_task_runs(request)
    proposal = next(
        (
            item
            for item in await service.list_evolution_proposals(runs)
            if item.id == proposal_id
        ),
        None,
    )
    if proposal is None:
        raise HTTPException(status_code=404, detail="evolution proposal not found")
    return service, runs, proposal


@task_runs_router.post(
    "/evolution-proposals/{proposal_id}/draft",
    response_model=CapabilityDraft,
)
async def draft_capability_from_evolution_proposal(
    proposal_id: str,
    request: Request,
):
    service, runs, proposal = await _find_evolution_proposal(request, proposal_id)
    if proposal.status in {"dismissed", "rolled_back"}:
        raise HTTPException(status_code=409, detail="evolution proposal is closed")
    try:
        return await service.build_evolution_draft(proposal, runs)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@task_runs_router.patch(
    "/evolution-proposals/{proposal_id}",
    response_model=EvolutionProposal,
)
async def review_evolution_proposal(
    proposal_id: str,
    payload: EvolutionProposalReviewRequest,
    request: Request,
):
    service, _, proposal = await _find_evolution_proposal(request, proposal_id)
    if payload.status == "pending":
        raise HTTPException(status_code=400, detail="pending is not a review action")
    return await service.review_evolution_proposal(
        proposal,
        status=payload.status,
        capability_id=payload.capability_id,
    )


@task_runs_router.get("/{run_id}/evaluations", response_model=list[EvaluationReport])
async def list_task_run_evaluations(run_id: str, request: Request):
    _, service = await _service_for_request(request)
    return await service.list_evaluation_reports(run_id)


@task_runs_router.get("/{run_id}", response_model=TaskRun)
async def get_task_run(run_id: str, request: Request):
    run = await _find_task_run(request, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="task run not found")
    return run


@task_runs_router.post("/{run_id}/draft-capability", response_model=CapabilityDraft)
async def draft_capability_from_task(run_id: str, request: Request):
    workspace, service = await _service_for_request(request)
    run = await _find_task_run(request, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="task run not found")
    if run.agent_id != workspace.agent_id:
        raise HTTPException(status_code=403, detail="task run belongs to another agent")
    if run.status in {"dispatched", "acked", "running", "blocked", "stalled", "timed_out"}:
        raise HTTPException(status_code=409, detail="task run is not draftable")
    return await service.build_draft(run)
