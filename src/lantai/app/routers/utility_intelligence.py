# -*- coding: utf-8 -*-
"""Configuration and isolated calls for background utility intelligence."""
from __future__ import annotations

import hashlib

from fastapi import APIRouter, Body, HTTPException, Request

from ..agent_context import get_current_agent_id
from ..header_utils import decode_header_value
from ..utility_intelligence import (
    ArtifactClassificationRequest,
    ArtifactClassificationResponse,
    classify_text_artifacts,
)
from ...config import load_config, save_config
from ...config.config import UtilityIntelligenceConfig
from ...providers.provider_manager import ProviderManager
from ..background_semantics import BackgroundSemanticService

router = APIRouter(
    prefix="/utility-intelligence",
    tags=["utility-intelligence"],
)


@router.get("/config", response_model=UtilityIntelligenceConfig)
async def get_utility_intelligence_config() -> UtilityIntelligenceConfig:
    return load_config().utility_intelligence


@router.get("/status")
async def get_background_semantic_status(request: Request) -> dict:
    """Expose queue health without revealing prompts or semantic payloads."""

    agent_id = (
        getattr(request.state, "agent_id", None)
        or decode_header_value(request.headers.get("X-Agent-Id"))
        or get_current_agent_id()
    )
    config = load_config().utility_intelligence
    service = BackgroundSemanticService.for_agent(agent_id)
    queue = (
        service.status()
        if service is not None
        else {
            "agent_id": agent_id,
            "worker_running": False,
            "pending": 0,
            "running": 0,
            "completed": 0,
            "failed": 0,
            "last_error": "",
            "last_failed_at": None,
            "last_completed_at": None,
        }
    )
    return {
        "background_processing_enabled": bool(
            getattr(config, "background_processing_enabled", True),
        ),
        "routing_mode": config.mode,
        "queue": queue,
    }


@router.post("/status/retry-failed")
async def retry_failed_background_semantic_jobs(request: Request) -> dict:
    agent_id = (
        getattr(request.state, "agent_id", None)
        or decode_header_value(request.headers.get("X-Agent-Id"))
        or get_current_agent_id()
    )
    service = BackgroundSemanticService.for_agent(agent_id)
    return {"requeued": service.retry_failed() if service is not None else 0}


@router.put("/config", response_model=UtilityIntelligenceConfig)
async def put_utility_intelligence_config(
    body: UtilityIntelligenceConfig = Body(...),
) -> UtilityIntelligenceConfig:
    if body.mode == "dedicated":
        manager = ProviderManager.get_instance()
        if not manager.is_model_slot_available(body.model):
            raise HTTPException(
                status_code=400,
                detail="The selected utility model is not configured or available.",
            )

    config = load_config()
    config.utility_intelligence = body
    save_config(config)
    return body


@router.post(
    "/artifacts/classify",
    response_model=ArtifactClassificationResponse,
)
async def classify_artifacts(
    request: Request,
    body: ArtifactClassificationRequest = Body(...),
) -> ArtifactClassificationResponse:
    agent_id = (
        getattr(request.state, "agent_id", None)
        or decode_header_value(request.headers.get("X-Agent-Id"))
        or get_current_agent_id()
    )
    config = load_config().utility_intelligence
    service = BackgroundSemanticService.for_agent(agent_id)
    if (
        service is None
        or not bool(getattr(config, "background_processing_enabled", True))
    ):
        return await classify_text_artifacts(body.text, agent_id=agent_id)
    fingerprint = hashlib.sha256(body.text.encode("utf-8")).hexdigest()
    job = service.submit(
        kind="artifact_classification",
        payload={"text": body.text},
        idempotency_key=f"artifact:{agent_id}:{fingerprint}",
    )
    settled = await service.wait_for_job(job.job_id)
    if settled.status == "completed" and isinstance(settled.result, dict):
        return ArtifactClassificationResponse.model_validate(settled.result)
    return ArtifactClassificationResponse(status="failed")
