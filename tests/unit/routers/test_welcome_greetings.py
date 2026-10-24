# -*- coding: utf-8 -*-
from __future__ import annotations

from unittest.mock import patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from lantai.app.routers.welcome_greetings import router
from lantai.app.welcome_greetings import WELCOME_POLICY_VERSION


app = FastAPI()
app.include_router(router, prefix="/api")


def _payload(**overrides):
    payload = {
        "locale": "zh-CN",
        "local_date": "2026-09-27",
        "timezone": "Asia/Shanghai",
        "scenario_keys": ["universal", "time.evening"],
        "cultural_calendar": "none",
        "tone": "natural",
        "policy_version": WELCOME_POLICY_VERSION,
    }
    payload.update(overrides)
    return payload


async def test_background_off_returns_before_resolving_a_queue():
    transport = ASGITransport(app=app)
    with patch(
        "lantai.app.routers.welcome_greetings.background_processing_enabled",
        return_value=False,
    ), patch(
        "lantai.app.routers.welcome_greetings.BackgroundSemanticService.for_agent",
        side_effect=AssertionError("queue must not be resolved"),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/welcome-greetings/prepare",
                json=_payload(),
            )

    assert response.status_code == 200
    assert response.json()["status"] == "disabled"


async def test_prepare_rejects_old_context_fields_at_the_http_boundary():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/welcome-greetings/prepare",
            json=_payload(chat_history=[{"role": "user", "content": "旧任务"}]),
        )

    assert response.status_code == 422


async def test_prepare_reports_unavailable_without_a_background_service():
    transport = ASGITransport(app=app)
    with patch(
        "lantai.app.routers.welcome_greetings.background_processing_enabled",
        return_value=True,
    ), patch(
        "lantai.app.routers.welcome_greetings.BackgroundSemanticService.for_agent",
        return_value=None,
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/welcome-greetings/prepare",
                json=_payload(),
            )

    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
