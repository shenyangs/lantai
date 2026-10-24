# -*- coding: utf-8 -*-
"""Tests for Electron desktop-only readiness proof."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_desktop_readiness_requires_token_and_returns_instance_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from httpx import ASGITransport, AsyncClient

    monkeypatch.setenv("LANTAI_DESKTOP_TOKEN", "desktop-token")
    monkeypatch.setenv("LANTAI_DESKTOP_INSTANCE_NONCE", "desktop-nonce")

    from lantai.app._app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        missing_token = await client.get("/api/desktop/readiness")
        ok = await client.get(
            "/api/desktop/readiness",
            headers={"X-Lantai-Desktop-Token": "desktop-token"},
        )

    assert missing_token.status_code == 403
    assert missing_token.json()["code"] == "desktop_token_required"

    assert ok.status_code == 200
    payload = ok.json()
    assert payload["status"] == "ok"
    assert payload["desktop"] is True
    assert payload["instance_nonce"] == "desktop-nonce"
    assert isinstance(payload["pid"], int)
