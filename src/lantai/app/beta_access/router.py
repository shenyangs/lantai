"""Unregistered local API adapter for the private-beta access UI.

Registration is intentionally kept outside this module so shared router
ownership can be coordinated without changing this protocol implementation.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from .service import BetaAccessApplicationError, BetaAccessApplicationService


class ActivateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Deliberately no max_length, pattern, normalization, or casing rule.
    code: str
    request_id: str | None = None
    device_label: str | None = None


class IssueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str | None = None
    device_label: str | None = None


class RecoverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str
    recovery_token: str | None = None


def _http_error(error: BetaAccessApplicationError) -> HTTPException:
    status_code = error.status_code if error.status_code is not None and 400 <= error.status_code <= 599 else 400
    return HTTPException(
        status_code=status_code,
        detail={"code": error.code, "message": str(error)},
    )


def create_beta_access_router(*, service: BetaAccessApplicationService | Any) -> APIRouter:
    api = APIRouter(prefix="/beta-access", tags=["beta-access"])

    @api.get("/status")
    async def local_status() -> dict[str, Any]:
        return service.local_status()

    @api.post("/activate")
    async def activate(request: ActivateRequest) -> dict[str, Any]:
        try:
            return await service.activate(
                code=request.code,
                request_id=request.request_id,
                device_label=request.device_label,
            )
        except BetaAccessApplicationError as error:
            raise _http_error(error) from error

    @api.post("/issue")
    async def issue(request: IssueRequest) -> dict[str, Any]:
        try:
            return await service.issue(
                request_id=request.request_id,
                device_label=request.device_label,
            )
        except BetaAccessApplicationError as error:
            raise _http_error(error) from error

    @api.post("/recover")
    async def recover(request: RecoverRequest) -> dict[str, Any]:
        try:
            return await service.recover(
                operation_id=request.operation_id,
                recovery_token=request.recovery_token,
            )
        except BetaAccessApplicationError as error:
            raise _http_error(error) from error

    return api
