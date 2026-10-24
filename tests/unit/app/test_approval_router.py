from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import lantai.app.routers.approval as approval_router
from lantai.app.routers.approval import (
    ApprovalActionRequest,
    SessionPermissionPreviewRequest,
    SessionPermissionRequest,
)
from lantai.app.approvals.service import ApprovalQueueChangedError


class _RaceApprovalService:
    def __init__(self) -> None:
        self.pending = SimpleNamespace(
            request_id="approval-race",
            root_session_id="root-session",
            session_id="root-session",
            agent_id="default",
            tool_name="execute_shell_command",
            extra={},
            rememberable=False,
        )

    async def get_request(self, _request_id: str):
        return self.pending

    async def remember_approval_scope(self, pending, *, decision_scope: str | None):
        pending.extra["decision_scope"] = decision_scope or "once"
        return None

    async def resolve_request(self, *_args, **_kwargs):
        return None


class _OwnerApprovalService:
    def __init__(self) -> None:
        self.pending = SimpleNamespace(
            request_id="approval-owner",
            root_session_id="root-session",
            session_id="root-session",
            user_id="owner-user",
            agent_id="default",
            tool_name="execute_shell_command",
            extra={},
            rememberable=False,
        )

    async def get_request(self, _request_id: str):
        return self.pending

    async def remember_approval_scope(self, pending, *, decision_scope: str | None):
        pending.extra["decision_scope"] = decision_scope or "once"
        return None

    async def resolve_request(self, *_args, **_kwargs):
        return self.pending


class _SessionPermissionService:
    def __init__(self) -> None:
        self.update = None

    async def set_session_permission_override(self, **kwargs):
        self.update = kwargs
        return {
            "permission_mode": kwargs["permission_mode"],
            "permission_profile": kwargs["permission_profile"],
            "resolved_request_ids": ["approval-1", "approval-2"],
        }

    async def preview_pending_for_root_session(self, **kwargs):
        return {
            "queue_revision": "queue-v1",
            "requests": [
                {
                    "request_id": "approval-1",
                    "tool_name": "execute_shell_command",
                    "severity": "high",
                }
            ],
        }


class _ChangedSessionPermissionService(_SessionPermissionService):
    async def set_session_permission_override(self, **kwargs):
        raise ApprovalQueueChangedError(
            "Approval queue changed after preview; review the current requests."
        )


@pytest.mark.asyncio
async def test_approve_returns_conflict_when_request_is_resolved_mid_flight(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        approval_router,
        "get_approval_service",
        lambda: _RaceApprovalService(),
    )

    with pytest.raises(HTTPException) as exc_info:
        await approval_router.post_approval_approve(
            SimpleNamespace(),
            ApprovalActionRequest(
                request_id="approval-race",
                session_id="root-session",
                decision_scope="once",
            ),
        )

    assert exc_info.value.status_code == 409
    assert "already resolved" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_deny_returns_conflict_when_request_is_resolved_mid_flight(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        approval_router,
        "get_approval_service",
        lambda: _RaceApprovalService(),
    )

    with pytest.raises(HTTPException) as exc_info:
        await approval_router.post_approval_deny(
            SimpleNamespace(),
            ApprovalActionRequest(
                request_id="approval-race",
                session_id="root-session",
                reason="no",
            ),
        )

    assert exc_info.value.status_code == 409
    assert "already resolved" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_approve_rejects_same_root_session_with_wrong_user(
    monkeypatch,
) -> None:
    service = _OwnerApprovalService()
    monkeypatch.setattr(
        approval_router,
        "get_approval_service",
        lambda: service,
    )

    with pytest.raises(HTTPException) as exc_info:
        await approval_router.post_approval_approve(
            SimpleNamespace(),
            ApprovalActionRequest(
                request_id="approval-owner",
                session_id="root-session",
                user_id="other-user",
                decision_scope="once",
            ),
        )

    assert exc_info.value.status_code == 403
    assert service.pending.extra == {}


@pytest.mark.asyncio
async def test_session_permission_endpoint_updates_active_root_and_returns_drain(
    monkeypatch,
) -> None:
    service = _SessionPermissionService()
    monkeypatch.setattr(
        approval_router,
        "get_approval_service",
        lambda: service,
    )

    response = await approval_router.post_session_permission(
        SimpleNamespace(),
        SessionPermissionRequest(
            session_id="root-session",
            user_id="owner-user",
            permission_mode="full_auto",
            permission_profile="full",
            expected_queue_revision="queue-v1",
        ),
    )

    assert service.update == {
        "root_session_id": "root-session",
        "user_id": "owner-user",
        "permission_mode": "full_auto",
        "permission_profile": "full",
        "expected_queue_revision": "queue-v1",
    }
    assert response.resolved_request_ids == ["approval-1", "approval-2"]


@pytest.mark.asyncio
async def test_session_permission_returns_conflict_when_preview_revision_changed(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        approval_router,
        "get_approval_service",
        lambda: _ChangedSessionPermissionService(),
    )

    with pytest.raises(HTTPException) as exc_info:
        await approval_router.post_session_permission(
            SimpleNamespace(),
            SessionPermissionRequest(
                session_id="root-session",
                user_id="owner-user",
                permission_mode="full_auto",
                permission_profile="full",
                expected_queue_revision="stale-revision",
            ),
        )

    assert exc_info.value.status_code == 409
    assert "queue changed" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_session_permission_preview_returns_safe_pending_summary(
    monkeypatch,
) -> None:
    service = _SessionPermissionService()
    monkeypatch.setattr(
        approval_router,
        "get_approval_service",
        lambda: service,
    )

    response = await approval_router.post_session_permission_preview(
        SimpleNamespace(),
        SessionPermissionPreviewRequest(
            session_id="root-session",
            user_id="owner-user",
        ),
    )

    assert response.queue_revision == "queue-v1"
    assert [item.model_dump() for item in response.requests] == [
        {
            "request_id": "approval-1",
            "tool_name": "execute_shell_command",
            "severity": "high",
        }
    ]


@pytest.mark.asyncio
async def test_deny_rejects_same_root_session_with_wrong_user(
    monkeypatch,
) -> None:
    service = _OwnerApprovalService()
    monkeypatch.setattr(
        approval_router,
        "get_approval_service",
        lambda: service,
    )

    with pytest.raises(HTTPException) as exc_info:
        await approval_router.post_approval_deny(
            SimpleNamespace(),
            ApprovalActionRequest(
                request_id="approval-owner",
                session_id="root-session",
                user_id="other-user",
                reason="no",
            ),
        )

    assert exc_info.value.status_code == 403
    assert service.pending.extra == {}


@pytest.mark.asyncio
async def test_approve_accepts_matching_owner_user(
    monkeypatch,
) -> None:
    service = _OwnerApprovalService()
    monkeypatch.setattr(
        approval_router,
        "get_approval_service",
        lambda: service,
    )

    response = await approval_router.post_approval_approve(
        SimpleNamespace(),
        ApprovalActionRequest(
            request_id="approval-owner",
            session_id="root-session",
            user_id="owner-user",
            decision_scope="once",
        ),
    )

    assert response.success is True
    assert response.tool_name == "execute_shell_command"
