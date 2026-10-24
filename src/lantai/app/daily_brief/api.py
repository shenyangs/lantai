# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import date
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import field_validator

from ..agent_context import get_agent_for_request
from .activation import DailyBriefActivationService
from .delivery import NativeBriefDeliveryDispatcher, NativeBriefNotification
from .models import (
    BriefActivationRequest,
    BriefActivationResult,
    BriefCycle,
    BriefDiscussionDraft,
    BriefDiscussionDraftCreateRequest,
    BriefDiscussionDraftView,
    BriefDeliveryReceipt,
    BriefEdition,
    BriefFeedback,
    BriefFeedbackCreateRequest,
    BriefGrant,
    BriefGrantCreateRequest,
    BriefSubscription,
    BriefSubscriptionCreateRequest,
    BriefSubscriptionUpdateRequest,
    BriefWorkspaceSnapshot,
    BriefRuntimeStatus,
    BriefModelCapability,
    BriefSavedItem,
    DailyBriefHomeSnapshot,
    StrictModel,
)
from .service import (
    BriefConflict,
    BriefModelPolicyRejected,
    BriefModelUnavailable,
    BriefNotFound,
    ConsentRequired,
    DailyBriefService,
    ScopeFenceFailed,
)
from .model_task import brief_model_capability, require_brief_model_capability

router = APIRouter(prefix="/daily-brief", tags=["daily-brief"])


class InvitationPreferenceRequest(StrictModel):
    hidden: bool


class GrantRevocationRequest(StrictModel):
    expected_revision: int
    action_id: str


class PreviewRequest(StrictModel):
    grant_id: str
    local_date: date
    timezone: str = "Asia/Shanghai"
    action_id: str

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be an IANA timezone") from exc
        return value


class RetryRequest(StrictModel):
    action_id: str
    explicit_consent: bool


class EditionReadRequest(StrictModel):
    revision: int


class DeliveryResolveRequest(StrictModel):
    state: Literal["submitted", "failed"]
    adapter_message_id: str | None = None
    error: str = ""


async def _service_for_request(request: Request) -> DailyBriefService:
    workspace = await get_agent_for_request(request)
    return DailyBriefService(workspace.workspace_dir)


def _runtime(workspace) -> BriefRuntimeStatus:
    lifecycle = getattr(workspace, "daily_brief", None)
    enabled = bool(getattr(lifecycle, "enabled", False))
    running = bool(getattr(lifecycle, "running", False))
    return BriefRuntimeStatus(enabled=enabled, running=running,
        last_tick_at=getattr(lifecycle, "last_tick_at", None),
        message="后台执行器正在运行" if running else "后台执行器暂未运行，请重启澜台后重试")


def _require_worker(workspace) -> None:
    lifecycle = getattr(workspace, "daily_brief", None)
    if lifecycle is None or getattr(lifecycle, "enabled", True) is False or getattr(lifecycle, "running", True) is False:
        raise BriefModelUnavailable("简报后台执行器暂未运行，请重启澜台后重试。")


def _raise_domain_error(exc: Exception) -> None:
    if isinstance(exc, BriefNotFound):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, BriefConflict):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(
        exc,
        (ConsentRequired, ScopeFenceFailed, BriefModelPolicyRejected),
    ):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if isinstance(exc, BriefModelUnavailable):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    raise exc


@router.get("/home", response_model=DailyBriefHomeSnapshot)
async def get_daily_brief_home(request: Request) -> DailyBriefHomeSnapshot:
    """Metadata-only homepage read. This endpoint cannot queue any work."""

    service = await _service_for_request(request)
    return await service.get_home_snapshot()


@router.get("/workspace", response_model=BriefWorkspaceSnapshot)
async def get_daily_brief_workspace(request: Request) -> BriefWorkspaceSnapshot:
    """Read existing workbench state without researching or generating."""

    workspace = await get_agent_for_request(request)
    snapshot = await DailyBriefService(workspace.workspace_dir).get_workspace_snapshot()
    return snapshot.model_copy(update={"runtime": _runtime(workspace)})


@router.get("/model-capability", response_model=BriefModelCapability)
async def get_brief_model_capability(request: Request) -> BriefModelCapability:
    workspace = await get_agent_for_request(request)
    return brief_model_capability(workspace.agent_id, allow_configured_model=True)


@router.post("/activate", response_model=BriefActivationResult)
async def activate_daily_brief(
    payload: BriefActivationRequest,
    request: Request,
) -> BriefActivationResult:
    """Create one consented preview or recurring brief from product-level choices."""

    workspace = await get_agent_for_request(request)
    service = DailyBriefActivationService(
        workspace.workspace_dir,
        agent_id=workspace.agent_id,
    )
    try:
        _require_worker(workspace)
        result = await service.activate(payload)
        lifecycle = getattr(workspace, "daily_brief", None)
        wake = getattr(lifecycle, "wake", None)
        if callable(wake):
            wake()
        return result
    except Exception as exc:
        _raise_domain_error(exc)
        raise AssertionError("unreachable")


@router.get("/editions", response_model=list[BriefEdition])
async def list_daily_brief_editions(request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[BriefEdition]:
    service = await _service_for_request(request)
    return await service.list_editions(limit=limit, offset=offset)


@router.get("/editions/{edition_id}", response_model=BriefEdition)
async def get_daily_brief_edition(
    edition_id: str,
    request: Request,
    revision: int | None = Query(default=None, ge=1),
) -> BriefEdition:
    service = await _service_for_request(request)
    try:
        return await service.get_edition(edition_id, revision)
    except Exception as exc:
        _raise_domain_error(exc)
        raise AssertionError("unreachable")


@router.put("/invitation", status_code=204)
async def set_daily_brief_invitation(
    payload: InvitationPreferenceRequest,
    request: Request,
) -> None:
    service = await _service_for_request(request)
    await service.set_invitation_hidden(payload.hidden)


@router.post("/grants", response_model=BriefGrant)
async def create_daily_brief_grant(
    payload: BriefGrantCreateRequest,
    request: Request,
) -> BriefGrant:
    service = await _service_for_request(request)
    try:
        return await service.create_grant(payload)
    except Exception as exc:
        _raise_domain_error(exc)
        raise AssertionError("unreachable")


@router.post("/grants/{grant_id}/revoke", response_model=BriefGrant)
async def revoke_daily_brief_grant(
    grant_id: str,
    payload: GrantRevocationRequest,
    request: Request,
) -> BriefGrant:
    service = await _service_for_request(request)
    try:
        return await service.revoke_grant(
            grant_id,
            expected_revision=payload.expected_revision,
            action_id=payload.action_id,
        )
    except Exception as exc:
        _raise_domain_error(exc)
        raise AssertionError("unreachable")


@router.post("/subscriptions", response_model=BriefSubscription)
async def create_daily_brief_subscription(
    payload: BriefSubscriptionCreateRequest,
    request: Request,
) -> BriefSubscription:
    service = await _service_for_request(request)
    try:
        return await service.create_subscription(payload)
    except Exception as exc:
        _raise_domain_error(exc)
        raise AssertionError("unreachable")


@router.patch("/subscriptions/{subscription_id}", response_model=BriefSubscription)
async def update_daily_brief_subscription(
    subscription_id: str,
    payload: BriefSubscriptionUpdateRequest,
    request: Request,
) -> BriefSubscription:
    service = await _service_for_request(request)
    try:
        return await service.update_subscription(subscription_id, payload)
    except Exception as exc:
        _raise_domain_error(exc)
        raise AssertionError("unreachable")


@router.post("/previews", response_model=BriefCycle)
async def create_daily_brief_preview(
    payload: PreviewRequest,
    request: Request,
) -> BriefCycle:
    """Queue metadata for an explicitly requested one-time preview only."""

    workspace = await get_agent_for_request(request)
    service = DailyBriefService(workspace.workspace_dir)
    try:
        _require_worker(workspace)
        require_brief_model_capability(workspace.agent_id)
        cycle = await service.create_preview_cycle(
            grant_id=payload.grant_id,
            local_date=payload.local_date,
            timezone=payload.timezone,
            action_id=payload.action_id,
        )
        lifecycle = getattr(workspace, "daily_brief", None)
        wake = getattr(lifecycle, "wake", None)
        if callable(wake):
            wake()
        return cycle
    except Exception as exc:
        _raise_domain_error(exc)
        raise AssertionError("unreachable")


@router.post("/cycles/{cycle_id}/retry", response_model=BriefCycle)
async def retry_daily_brief_cycle(cycle_id: str, payload: RetryRequest, request: Request) -> BriefCycle:
    workspace = await get_agent_for_request(request)
    try:
        _require_worker(workspace)
        require_brief_model_capability(workspace.agent_id)
        cycle = await DailyBriefService(workspace.workspace_dir).retry_cycle(
            cycle_id, action_id=payload.action_id, explicit_consent=payload.explicit_consent)
        workspace.daily_brief.wake()
        return cycle
    except Exception as exc:
        _raise_domain_error(exc)
        raise AssertionError("unreachable")


@router.post("/editions/{edition_id}/read", status_code=204)
async def mark_daily_brief_edition_read(edition_id: str, payload: EditionReadRequest, request: Request) -> None:
    try:
        await (await _service_for_request(request)).mark_edition_read(edition_id, payload.revision)
    except Exception as exc:
        _raise_domain_error(exc)


@router.get("/saved", response_model=list[BriefSavedItem])
async def list_saved_brief_items(request: Request) -> list[BriefSavedItem]:
    return await (await _service_for_request(request)).list_saved_items()


@router.delete("/saved/{saved_id}", status_code=204)
async def remove_saved_brief_item(saved_id: str, request: Request) -> None:
    await (await _service_for_request(request)).remove_saved_item(saved_id)


@router.get("/deliveries/{receipt_id}", response_model=BriefDeliveryReceipt)
async def get_daily_brief_delivery(receipt_id: str, request: Request) -> BriefDeliveryReceipt:
    try:
        return await (await _service_for_request(request)).get_delivery_receipt(receipt_id)
    except Exception as exc:
        _raise_domain_error(exc)
        raise AssertionError("unreachable")


@router.post(
    "/deliveries/native/claim-next",
    response_model=NativeBriefNotification | None,
)
async def claim_next_native_brief_delivery(
    request: Request,
) -> NativeBriefNotification | None:
    """Claim one consented outbox row; no receipt is treated as delivery."""

    workspace = await get_agent_for_request(request)
    dispatcher = NativeBriefDeliveryDispatcher(workspace.workspace_dir)
    try:
        return await dispatcher.claim_next()
    except Exception as exc:
        _raise_domain_error(exc)
        raise AssertionError("unreachable")


@router.post(
    "/deliveries/{receipt_id}/resolve",
    response_model=BriefDeliveryReceipt,
)
async def resolve_native_brief_delivery(
    receipt_id: str,
    payload: DeliveryResolveRequest,
    request: Request,
) -> BriefDeliveryReceipt:
    service = await _service_for_request(request)
    try:
        return await service.resolve_delivery(
            receipt_id,
            state=payload.state,
            adapter_message_id=payload.adapter_message_id,
            error=payload.error,
        )
    except Exception as exc:
        _raise_domain_error(exc)
        raise AssertionError("unreachable")


@router.post(
    "/deliveries/{receipt_id}/read",
    response_model=BriefDeliveryReceipt,
)
async def mark_native_brief_delivery_read(
    receipt_id: str,
    request: Request,
) -> BriefDeliveryReceipt:
    service = await _service_for_request(request)
    try:
        return await service.mark_delivery_read(receipt_id)
    except Exception as exc:
        _raise_domain_error(exc)
        raise AssertionError("unreachable")


@router.post("/editions/{edition_id}/feedback", response_model=BriefFeedback)
async def add_daily_brief_feedback(
    edition_id: str,
    payload: BriefFeedbackCreateRequest,
    request: Request,
    item_id: str | None = Query(default=None),
) -> BriefFeedback:
    service = await _service_for_request(request)
    try:
        return await service.add_feedback(edition_id, item_id, payload)
    except Exception as exc:
        _raise_domain_error(exc)
        raise AssertionError("unreachable")


@router.get("/discussion-drafts/{draft_id}", response_model=BriefDiscussionDraftView)
async def get_daily_brief_discussion_draft(draft_id: str, request: Request) -> BriefDiscussionDraftView:
    service = await _service_for_request(request)
    try:
        return await service.get_discussion_draft(draft_id)
    except Exception as exc:
        _raise_domain_error(exc)
        raise AssertionError("unreachable")


@router.post(
    "/editions/{edition_id}/discussion-drafts",
    response_model=BriefDiscussionDraft,
)
async def create_daily_brief_discussion_draft(
    edition_id: str,
    payload: BriefDiscussionDraftCreateRequest,
    request: Request,
) -> BriefDiscussionDraft:
    """Create a BriefContextRef draft; it does not send a chat Command."""

    service = await _service_for_request(request)
    try:
        return await service.create_discussion_draft(edition_id, payload)
    except Exception as exc:
        _raise_domain_error(exc)
        raise AssertionError("unreachable")
