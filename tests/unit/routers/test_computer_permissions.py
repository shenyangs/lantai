# -*- coding: utf-8 -*-
"""Unit tests for the computer permissions settings router."""
from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from lantai.app.routers import computer_permissions as permissions_router

app = FastAPI()
app.include_router(permissions_router.router, prefix="/api")


def _client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_get_computer_permissions_marks_unsupported_on_non_macos(
    monkeypatch,
):
    monkeypatch.setattr(permissions_router, "_is_macos", lambda: False)

    async with _client() as client:
        resp = await client.get("/api/settings/computer-permissions")

    assert resp.status_code == 200
    body = resp.json()
    assert body["is_macos"] is False
    assert {item["status"] for item in body["permissions"]} == {"unsupported"}
    assert all(item["settings_url"] is None for item in body["permissions"])
    assert "microphone" not in {item["key"] for item in body["permissions"]}

    async with _client() as client:
        request = await client.post(
            "/api/settings/computer-permissions/microphone/request",
        )

    assert request.status_code == 404


async def test_get_computer_permissions_reports_accessibility_when_checkable(
    monkeypatch,
):
    monkeypatch.setattr(permissions_router, "_is_macos", lambda: True)
    monkeypatch.setattr(
        permissions_router,
        "_check_accessibility",
        lambda: "granted",
    )

    async with _client() as client:
        resp = await client.get("/api/settings/computer-permissions")

    assert resp.status_code == 200
    items = {item["key"]: item for item in resp.json()["permissions"]}
    assert items["accessibility"]["status"] == "granted"
    assert items["accessibility"]["can_check"] is True
    assert items["full_disk_access"]["status"] == "unknown"


async def test_open_permission_rejects_unknown_key():
    async with _client() as client:
        resp = await client.post(
            "/api/settings/computer-permissions/not-real/open",
        )

    assert resp.status_code == 404


async def test_open_permission_invokes_macos_settings(monkeypatch):
    opened: list[list[str]] = []
    monkeypatch.setattr(permissions_router, "_is_macos", lambda: True)
    monkeypatch.setattr(
        permissions_router.subprocess,
        "run",
        lambda args, **_: opened.append(args),
    )

    async with _client() as client:
        resp = await client.post(
            "/api/settings/computer-permissions/accessibility/open",
        )

    assert resp.status_code == 200
    assert opened
    assert opened[0][0] == "open"
    assert "Privacy_Accessibility" in opened[0][1]
