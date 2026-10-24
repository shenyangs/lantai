# -*- coding: utf-8 -*-
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from ..agent_context import get_agent_for_request
from .models import (
    ReminderCreateRequest,
    ReminderSettings,
    NativeReminderClaimRequest,
    NativeReminderResolveRequest,
    ReminderItem,
    ReminderStatus,
    ReminderUpdateRequest,
    SnoozeReminderRequest,
    TodayBrief,
    WatchTopic,
    WatchTopicBriefUseRequest,
    WatchTopicCreateRequest,
    WatchTopicUpdateRequest,
)
from .repo import ReminderRepository
from .notification_outbox import ReminderNotificationOutbox
from .service import (
    ReminderService,
    WatchTopicConfirmationRequired,
    WatchTopicRevisionConflict,
)

router = APIRouter(prefix="/reminders", tags=["reminders"])


async def _service_for_request(request: Request) -> ReminderService:
    workspace = await get_agent_for_request(request)
    return ReminderService(ReminderRepository(workspace.workspace_dir))


@router.get("", response_model=list[ReminderItem])
async def list_reminders(
    request: Request,
    status: ReminderStatus | None = Query(default=None),
):
    service = await _service_for_request(request)
    return await service.list_reminders(status)


@router.post("", response_model=ReminderItem)
async def create_reminder(
    payload: ReminderCreateRequest,
    request: Request,
):
    service = await _service_for_request(request)
    return await service.create_reminder(payload)


@router.get("/brief/today", response_model=TodayBrief)
async def get_today_brief(request: Request):
    service = await _service_for_request(request)
    return await service.today_brief()


@router.get("/settings", response_model=ReminderSettings)
async def get_reminder_settings(request: Request):
    return await (await _service_for_request(request)).get_settings()


@router.patch("/settings", response_model=ReminderSettings)
async def set_reminder_settings(payload: ReminderSettings, request: Request):
    return await (await _service_for_request(request)).set_settings(payload)


@router.post("/native/claim")
async def claim_native_reminder(payload: NativeReminderClaimRequest, request: Request):
    workspace = await get_agent_for_request(request)
    outbox = ReminderNotificationOutbox(ReminderRepository(workspace.workspace_dir), workspace.agent_id)
    return await outbox.claim_native(payload.caller_id)


@router.post("/native/resolve")
async def resolve_native_reminder(payload: NativeReminderResolveRequest, request: Request):
    workspace = await get_agent_for_request(request)
    outbox = ReminderNotificationOutbox(ReminderRepository(workspace.workspace_dir), workspace.agent_id)
    try:
        await outbox.resolve_native(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@router.get("/watch-topics", response_model=list[WatchTopic])
async def list_watch_topics(request: Request):
    service = await _service_for_request(request)
    return await service.list_watch_topics()


@router.post("/watch-topics", response_model=WatchTopic)
async def create_watch_topic(
    payload: WatchTopicCreateRequest,
    request: Request,
):
    service = await _service_for_request(request)
    return await service.create_watch_topic(payload)


@router.patch("/watch-topics/{topic_id}", response_model=WatchTopic)
async def update_watch_topic(
    topic_id: str,
    payload: WatchTopicUpdateRequest,
    request: Request,
):
    service = await _service_for_request(request)
    try:
        topic = await service.update_watch_topic(topic_id, payload)
    except WatchTopicRevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if topic is None:
        raise HTTPException(status_code=404, detail="watch topic not found")
    return topic


@router.post("/watch-topics/{topic_id}/brief-use", response_model=WatchTopic)
async def configure_watch_topic_for_brief(
    topic_id: str,
    payload: WatchTopicBriefUseRequest,
    request: Request,
):
    service = await _service_for_request(request)
    try:
        topic = await service.configure_watch_topic_for_brief(topic_id, payload)
    except WatchTopicRevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WatchTopicConfirmationRequired as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if topic is None:
        raise HTTPException(status_code=404, detail="watch topic not found")
    return topic


@router.delete("/watch-topics/{topic_id}")
async def delete_watch_topic(topic_id: str, request: Request):
    service = await _service_for_request(request)
    deleted = await service.delete_watch_topic(topic_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="watch topic not found")
    return {"ok": True}


@router.patch("/{reminder_id}", response_model=ReminderItem)
async def update_reminder(
    reminder_id: str,
    payload: ReminderUpdateRequest,
    request: Request,
):
    service = await _service_for_request(request)
    reminder = await service.update_reminder(reminder_id, payload)
    if reminder is None:
        raise HTTPException(status_code=404, detail="reminder not found")
    return reminder


@router.post("/{reminder_id}/read", response_model=ReminderItem)
async def mark_reminder_read(reminder_id: str, request: Request):
    service = await _service_for_request(request)
    reminder = await service.set_status(reminder_id, "read")
    if reminder is None:
        raise HTTPException(status_code=404, detail="reminder not found")
    return reminder


@router.post("/{reminder_id}/snooze", response_model=ReminderItem)
async def snooze_reminder(
    reminder_id: str,
    payload: SnoozeReminderRequest,
    request: Request,
):
    service = await _service_for_request(request)
    reminder = await service.snooze_reminder(reminder_id, payload.until)
    if reminder is None:
        raise HTTPException(status_code=404, detail="reminder not found")
    return reminder


@router.post("/{reminder_id}/dismiss", response_model=ReminderItem)
async def dismiss_reminder(reminder_id: str, request: Request):
    service = await _service_for_request(request)
    reminder = await service.set_status(reminder_id, "dismissed")
    if reminder is None:
        raise HTTPException(status_code=404, detail="reminder not found")
    return reminder


@router.post("/{reminder_id}/done", response_model=ReminderItem)
async def complete_reminder(reminder_id: str, request: Request):
    service = await _service_for_request(request)
    reminder = await service.set_status(reminder_id, "done")
    if reminder is None:
        raise HTTPException(status_code=404, detail="reminder not found")
    return reminder
