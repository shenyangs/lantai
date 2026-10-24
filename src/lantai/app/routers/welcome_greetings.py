# -*- coding: utf-8 -*-
"""New-chat welcome candidate preparation API."""
from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Body, Request

from ..agent_context import get_current_agent_id
from ..background_semantics import BackgroundSemanticService
from ..header_utils import decode_header_value
from ..welcome_greetings import (
    WelcomeGenerationRequest,
    WelcomeGenerationResponse,
    background_processing_enabled,
)


router = APIRouter(prefix="/welcome-greetings", tags=["welcome-greetings"])


@router.post("/prepare", response_model=WelcomeGenerationResponse)
async def prepare_welcome_greetings(
    request: Request,
    body: WelcomeGenerationRequest = Body(...),
) -> WelcomeGenerationResponse:
    """Prepare reviewed candidates for a later empty-page entry."""
    if not background_processing_enabled():
        return WelcomeGenerationResponse(status="disabled")
    agent_id = (
        getattr(request.state, "agent_id", None)
        or decode_header_value(request.headers.get("X-Agent-Id"))
        or get_current_agent_id()
    )
    service = BackgroundSemanticService.for_agent(agent_id)
    if service is None:
        return WelcomeGenerationResponse(status="unavailable")
    payload = body.model_dump(mode="json")
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    job = service.submit(
        kind="welcome_greeting",
        payload=payload,
        idempotency_key=f"welcome:{agent_id}:{body.local_date}:{fingerprint}",
    )
    settled = await service.wait_for_job(job.job_id)
    if settled.status == "completed" and isinstance(settled.result, dict):
        return WelcomeGenerationResponse.model_validate(settled.result)
    return WelcomeGenerationResponse(status="unavailable")
