# -*- coding: utf-8 -*-
"""Approval API endpoints for tool guard approvals."""
from __future__ import annotations

import logging
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..approvals import get_approval_service
from ..approvals.service import ApprovalQueueChangedError
from ...security.tool_guard.approval import ApprovalDecision

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/approval", tags=["approval"])


class ApprovalActionRequest(BaseModel):
    """Request body for approval actions."""

    request_id: str = Field(..., description="Approval request ID (UUID)")
    session_id: str = Field(..., description="Session ID")
    user_id: Optional[str] = Field(
        None,
        description="User ID (optional, for validation)",
    )
    reason: Optional[str] = Field(
        None,
        description="Optional reason for denial",
    )
    decision_scope: Optional[str] = Field(
        None,
        description="Optional UI-selected approval scope",
    )


class ApprovalActionResponse(BaseModel):
    """Response for approval actions."""

    success: bool
    message: str
    tool_name: Optional[str] = None
    request_id: str
    decision_scope: Optional[str] = None
    remembered: bool = False
    resolved_request_ids: list[str] = Field(default_factory=list)


class SessionPermissionRequest(BaseModel):
    """Live permission update for an existing root session."""

    session_id: str
    user_id: str
    permission_mode: Literal[
        "default",
        "plan",
        "accept_edits",
        "quiet",
        "full_auto",
    ]
    permission_profile: Literal["observe", "workspace", "full"]
    expected_queue_revision: Optional[str] = None


class SessionPermissionPreviewRequest(BaseModel):
    """Read-only request to preview queued approvals affected by full access."""

    session_id: str
    user_id: str


class SessionPermissionPreviewItem(BaseModel):
    """Safe display fields for one queued approval."""

    request_id: str
    tool_name: str
    severity: str


class SessionPermissionPreviewResponse(BaseModel):
    """Atomic review token and the queue it represents."""

    queue_revision: str
    requests: list[SessionPermissionPreviewItem] = Field(default_factory=list)


class SessionPermissionResponse(BaseModel):
    """Result of applying a live session permission update."""

    success: bool = True
    permission_mode: str
    permission_profile: str
    resolved_request_ids: list[str] = Field(default_factory=list)


class ApprovalListResponse(BaseModel):
    """Response for listing pending approvals."""

    pending_approvals: list[dict]
    count: int


def _normalise_owner_id(value: Optional[str]) -> str:
    return str(value or "").strip()


def _verify_pending_scope(
    pending,
    body: ApprovalActionRequest,
    *,
    action: str,
) -> None:
    if pending.root_session_id != body.session_id:
        logger.warning(
            "Root session mismatch: request %s (root: %s) not in session %s",
            body.request_id[:16],
            pending.root_session_id,
            body.session_id,
        )
        raise HTTPException(
            status_code=403,
            detail="Root session mismatch: cannot approve other session trees",
        )

    pending_user_id = _normalise_owner_id(getattr(pending, "user_id", None))
    request_user_id = _normalise_owner_id(body.user_id)
    if pending_user_id and request_user_id != pending_user_id:
        logger.warning(
            "Approval owner mismatch: action=%s request=%s owner=%s caller=%s",
            action,
            body.request_id[:16],
            pending_user_id[:32],
            request_user_id[:32] if request_user_id else "<missing>",
        )
        raise HTTPException(
            status_code=403,
            detail="Approval owner mismatch: cannot resolve another user's request",
        )


@router.post(
    "/approve",
    response_model=ApprovalActionResponse,
    summary="Approve a pending tool execution",
)
async def post_approval_approve(
    request: Request,  # pylint: disable=unused-argument
    body: ApprovalActionRequest,
) -> ApprovalActionResponse:
    """Approve a pending tool execution.

    Resolves the Future associated with the approval request,
    allowing the agent to continue executing the tool.
    """
    svc = get_approval_service()

    logger.info(
        "Approval approve request: request_id=%s session_id=%s",
        body.request_id[:16],
        body.session_id,
    )

    # Verify the request belongs to the root session (support cross-session)
    pending = await svc.get_request(body.request_id)
    if pending is None:
        logger.warning(
            "Approval request not found: %s",
            body.request_id[:16],
        )
        raise HTTPException(
            status_code=404,
            detail=f"Approval request not found: {body.request_id[:16]}",
        )

    _verify_pending_scope(pending, body, action="approve")

    remembered = await svc.remember_approval_scope(
        pending,
        decision_scope=body.decision_scope,
    )
    decision_scope = str(pending.extra.get("decision_scope") or "once")

    # Resolve the Future
    resolved = await svc.resolve_request(
        body.request_id,
        ApprovalDecision.APPROVED,
        decision_scope=decision_scope,
    )
    if resolved is None:
        raise HTTPException(
            status_code=409,
            detail=f"Approval request already resolved: {body.request_id[:16]}",
        )

    resolved_request_ids: list[str] = []
    if decision_scope == "session":
        drain_pending = getattr(
            svc,
            "approve_pending_for_root_session",
            None,
        )
        if callable(drain_pending):
            resolved_request_ids = await drain_pending(
                root_session_id=resolved.root_session_id,
                user_id=resolved.user_id,
                session_rememberable_only=True,
            )

    logger.info(
        "Approval approved: request_id=%s session=%s tool=%s",
        body.request_id[:16],
        body.session_id,
        resolved.tool_name,
    )

    return ApprovalActionResponse(
        success=True,
        message=f"Tool '{resolved.tool_name}' approved, executing...",
        tool_name=resolved.tool_name,
        request_id=body.request_id,
        decision_scope=decision_scope,
        remembered=remembered is not None,
        resolved_request_ids=resolved_request_ids,
    )


@router.post(
    "/session-permission/preview",
    response_model=SessionPermissionPreviewResponse,
    summary="Preview queued approvals affected by a full-access grant",
)
async def post_session_permission_preview(
    request: Request,  # pylint: disable=unused-argument
    body: SessionPermissionPreviewRequest,
) -> SessionPermissionPreviewResponse:
    """Describe queued effects without exposing tool arguments or changing state."""

    svc = get_approval_service()
    try:
        preview = await svc.preview_pending_for_root_session(
            root_session_id=body.session_id,
            user_id=body.user_id,
            session_rememberable_only=False,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SessionPermissionPreviewResponse(**preview)


@router.post(
    "/session-permission",
    response_model=SessionPermissionResponse,
    summary="Update the permission policy for an active session",
)
async def post_session_permission(
    request: Request,  # pylint: disable=unused-argument
    body: SessionPermissionRequest,
) -> SessionPermissionResponse:
    """Make a composer permission change visible to the active runtime."""

    svc = get_approval_service()
    try:
        update = await svc.set_session_permission_override(
            root_session_id=body.session_id,
            user_id=body.user_id,
            permission_mode=body.permission_mode,
            permission_profile=body.permission_profile,
            expected_queue_revision=body.expected_queue_revision,
        )
    except ApprovalQueueChangedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SessionPermissionResponse(
        permission_mode=update["permission_mode"],
        permission_profile=update["permission_profile"],
        resolved_request_ids=list(update.get("resolved_request_ids") or []),
    )


@router.post(
    "/deny",
    response_model=ApprovalActionResponse,
    summary="Deny a pending tool execution",
)
async def post_approval_deny(
    request: Request,  # pylint: disable=unused-argument
    body: ApprovalActionRequest,
) -> ApprovalActionResponse:
    """Deny a pending tool execution.

    Resolves the Future with DENIED decision, preventing
    the agent from executing the tool.
    """
    svc = get_approval_service()

    reason = body.reason or "User denied"

    logger.info(
        "Approval deny request: request_id=%s session_id=%s reason=%s",
        body.request_id[:16],
        body.session_id,
        reason,
    )

    # Verify the request belongs to the root session (support cross-session)
    pending = await svc.get_request(body.request_id)
    if pending is None:
        logger.warning(
            "Approval request not found: %s",
            body.request_id[:16],
        )
        raise HTTPException(
            status_code=404,
            detail=f"Approval request not found: {body.request_id[:16]}",
        )

    _verify_pending_scope(pending, body, action="deny")

    # Resolve the Future
    resolved = await svc.resolve_request(
        body.request_id,
        ApprovalDecision.DENIED,
        decision_scope=body.decision_scope,
    )
    if resolved is None:
        raise HTTPException(
            status_code=409,
            detail=f"Approval request already resolved: {body.request_id[:16]}",
        )

    logger.info(
        "Approval denied: request_id=%s session=%s tool=%s",
        body.request_id[:16],
        body.session_id,
        resolved.tool_name,
    )

    return ApprovalActionResponse(
        success=True,
        message=f"Tool '{resolved.tool_name}' denied: {reason}",
        tool_name=resolved.tool_name,
        request_id=body.request_id,
    )


@router.get(
    "/list",
    response_model=ApprovalListResponse,
    summary="List pending approval requests",
)
async def get_approval_list(
    request: Request,  # pylint: disable=unused-argument
    session_id: Optional[str] = None,
) -> ApprovalListResponse:
    """List all pending approval requests.

    Optionally filter by session_id.
    """
    svc = get_approval_service()

    if session_id:
        logger.debug(
            "Listing approvals for root session (includes children): %s",
            session_id,
        )
        # Use get_pending_by_root_session for cross-session support
        pending_list = await svc.get_pending_by_root_session(session_id)
    else:
        logger.debug("Listing all pending approvals")
        # pylint: disable=protected-access
        async with svc._lock:
            pending_list = list(svc._pending.values())

    # Serialize pending approvals
    result = []
    for pending in pending_list:
        result.append(
            {
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "root_session_id": pending.root_session_id,
                "user_id": getattr(pending, "user_id", ""),
                "agent_id": pending.agent_id,
                "tool_name": pending.tool_name,
                "severity": pending.severity,
                "findings_count": pending.findings_count,
                "created_at": pending.created_at,
                "timeout_seconds": pending.timeout_seconds,
                "result_summary": pending.result_summary,
                "policy_decision": pending.policy_decision,
                "approval_options": pending.approval_options,
                "session_rememberable": pending.session_rememberable,
                "rememberable": pending.rememberable,
                "permission_scope": pending.permission_scope,
                "permission_group_key": pending.permission_group_key,
                "source_run_id": pending.source_run_id,
                "source_agent_name": pending.source_agent_name,
            },
        )

    logger.info("Listed %d pending approvals", len(result))

    return ApprovalListResponse(
        pending_approvals=result,
        count=len(result),
    )
