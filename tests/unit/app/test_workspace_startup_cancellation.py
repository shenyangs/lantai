# -*- coding: utf-8 -*-
"""Cancellation locks for partially started agent workspaces."""

from __future__ import annotations

import asyncio

import pytest

import lantai.agents.skills_manager as skills_manager_module
import lantai.app.workspace.workspace as workspace_module
from lantai.app.workspace.workspace import Workspace


@pytest.mark.asyncio
async def test_workspace_startup_cancellation_stops_partial_services(
    monkeypatch,
) -> None:
    class PartialServices:
        def __init__(self) -> None:
            self.stop_calls: list[bool] = []

        async def start_all(self) -> None:
            raise asyncio.CancelledError

        async def stop_all(self, *, final: bool = False) -> None:
            self.stop_calls.append(final)

    services = PartialServices()
    workspace = object.__new__(Workspace)
    workspace.agent_id = "cancelled-start"
    workspace._service_manager = services
    workspace._started = False
    workspace._recovery_task = None
    workspace._migrate_legacy_weixin_data = lambda: None

    monkeypatch.setattr(
        skills_manager_module,
        "ensure_skill_pool_initialized",
        lambda: None,
    )
    monkeypatch.setattr(
        workspace_module,
        "load_agent_config",
        lambda _agent_id: object(),
    )

    with pytest.raises(asyncio.CancelledError):
        await workspace.start()

    assert services.stop_calls == [True]
    assert workspace._started is False
