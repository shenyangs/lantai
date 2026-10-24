# -*- coding: utf-8 -*-
"""Agent-scoped work method catalog and custom method persistence."""

from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from ...work_methods import (
    WorkMethod,
    WorkMethodDraft,
    WorkMethodRepository,
    compile_work_method_contract,
)
from ..agent_context import get_agent_for_request
from ..private_editor import PrivateEditorStore

router = APIRouter(prefix="/work-methods", tags=["work-methods"])


class WorkMethodContractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1, max_length=12000)


async def _repository(request: Request) -> WorkMethodRepository:
    workspace = await get_agent_for_request(request)
    return WorkMethodRepository(workspace.workspace_dir)


@router.get("", response_model=list[WorkMethod])
async def list_work_methods(request: Request) -> list[WorkMethod]:
    repository = await _repository(request)
    methods = await repository.list()
    if not PrivateEditorStore(repository.workspace_dir).get_settings().enabled:
        methods = [method for method in methods if method.id != "private_editor"]
    return methods


@router.put("/{method_id}", response_model=WorkMethod)
async def put_work_method(
    method_id: str,
    request: Request,
    body: WorkMethodDraft = Body(...),
) -> WorkMethod:
    try:
        return await (await _repository(request)).upsert(method_id, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{method_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_work_method(method_id: str, request: Request) -> Response:
    try:
        deleted = await (await _repository(request)).delete(method_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="work method not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{method_id}/contract")
async def compile_contract(
    method_id: str,
    request: Request,
    body: WorkMethodContractRequest = Body(...),
) -> dict:
    method = await (await _repository(request)).get(method_id)
    if method is None:
        raise HTTPException(status_code=404, detail="work method not found")
    return compile_work_method_contract(
        method,
        objective=body.objective,
    ).model_dump(mode="json")
