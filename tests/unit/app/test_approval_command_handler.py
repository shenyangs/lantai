from __future__ import annotations

from types import SimpleNamespace

import pytest

import lantai.app.runner.control_commands.approval_handler as approval_handler
from lantai.app.runner.control_commands.approval_handler import (
    ApprovalCommandHandler,
)
from lantai.app.runner.control_commands.base import ControlContext


class _RaceApprovalService:
    def __init__(self) -> None:
        self.pending = SimpleNamespace(
            request_id="approval-race",
            root_session_id="root-session",
            session_id="root-session",
            agent_id="default",
            tool_name="execute_shell_command",
            extra={},
        )

    async def get_request(self, _request_id: str):
        return self.pending

    async def get_pending_by_session(self, _session_id: str):
        return self.pending

    async def resolve_request(self, *_args, **_kwargs):
        return None


def _context(action: str) -> ControlContext:
    return ControlContext(
        workspace=SimpleNamespace(),
        payload=SimpleNamespace(),
        channel=SimpleNamespace(channel="console"),
        session_id="root-session",
        user_id="user",
        agent_id="default",
        args={"action": action, "request_id": "approval-race"},
    )


@pytest.mark.asyncio
async def test_approve_command_reports_already_resolved_race(monkeypatch) -> None:
    monkeypatch.setattr(
        approval_handler,
        "get_approval_service",
        lambda: _RaceApprovalService(),
    )

    result = await ApprovalCommandHandler().handle(_context("approve"))

    assert "审批请求已处理" in result
    assert "approval-race" in result


@pytest.mark.asyncio
async def test_deny_command_reports_already_resolved_race(monkeypatch) -> None:
    monkeypatch.setattr(
        approval_handler,
        "get_approval_service",
        lambda: _RaceApprovalService(),
    )

    result = await ApprovalCommandHandler().handle(_context("deny"))

    assert "审批请求已处理" in result
    assert "approval-race" in result
