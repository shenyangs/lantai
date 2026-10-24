# -*- coding: utf-8 -*-
"""P0 readiness guards for core chat services."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import lantai.app.runner.api as runner_api
from lantai.app.routers.console import _get_console_channel


@pytest.mark.asyncio
async def test_get_chat_manager_returns_503_when_not_ready(monkeypatch) -> None:
    async def fake_get_workspace(_request):
        return SimpleNamespace(chat_manager=None)

    monkeypatch.setattr(runner_api, "get_workspace", fake_get_workspace)

    with pytest.raises(HTTPException) as exc:
        await runner_api.get_chat_manager(SimpleNamespace())

    assert exc.value.status_code == 503
    assert exc.value.detail == "Chat manager is not ready"


@pytest.mark.asyncio
async def test_get_session_returns_503_when_runner_session_not_ready(monkeypatch) -> None:
    async def fake_get_workspace(_request):
        return SimpleNamespace(runner=SimpleNamespace(session=None))

    monkeypatch.setattr(runner_api, "get_workspace", fake_get_workspace)

    with pytest.raises(HTTPException) as exc:
        await runner_api.get_session(SimpleNamespace())

    assert exc.value.status_code == 503
    assert exc.value.detail == "Runner session store is not ready"


@pytest.mark.asyncio
async def test_console_channel_returns_503_when_channel_manager_not_ready() -> None:
    with pytest.raises(HTTPException) as exc:
        await _get_console_channel(SimpleNamespace(channel_manager=None))

    assert exc.value.status_code == 503
    assert exc.value.detail == "Channel manager is not ready"


@pytest.mark.asyncio
async def test_console_channel_returns_503_when_console_missing() -> None:
    class EmptyChannelManager:
        async def get_channel(self, _name: str):
            return None

    with pytest.raises(HTTPException) as exc:
        await _get_console_channel(
            SimpleNamespace(channel_manager=EmptyChannelManager()),
        )

    assert exc.value.status_code == 503
    assert exc.value.detail == "Channel Console not found"
