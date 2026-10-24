# -*- coding: utf-8 -*-
"""Tests for desktop browser pairing helpers."""

from lantai.app.browser_pairing import (
    BROWSER_SESSION_COOKIE,
    BrowserPairingManager,
    is_browser_pairing_path,
    is_public_browser_pairing_path,
)
from lantai.app.routers.browser_pairing import router as browser_pairing_router


def _build_pairing_app():
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(browser_pairing_router, prefix="/api")
    return app


def test_pairing_code_can_be_consumed_once():
    manager = BrowserPairingManager()

    code = manager.create_code()
    session = manager.consume_code(code.code)

    assert session is not None
    assert manager.is_valid_session(session.token) is True
    assert manager.consume_code(code.code) is None


def test_session_revoke_invalidates_browser_cookie_token():
    manager = BrowserPairingManager()
    code = manager.create_code()
    session = manager.consume_code(code.code)
    assert session is not None

    manager.revoke_session(session.token)

    assert manager.is_valid_session(session.token) is False


def test_browser_pairing_public_paths_are_limited():
    assert BROWSER_SESSION_COOKIE == "lantai_browser_session"
    assert is_public_browser_pairing_path("/api/browser-pairing/status")
    assert is_public_browser_pairing_path("/api/browser-pairing/complete")
    assert not is_public_browser_pairing_path("/api/browser-pairing/code")
    assert is_browser_pairing_path("/api/browser-pairing/code")


async def test_pairing_endpoint_sets_httponly_cookie():
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=_build_pairing_app())
    async with AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        code_resp = await client.post("/api/browser-pairing/code")
        assert code_resp.status_code == 200
        code = code_resp.json()["code"]

        complete_resp = await client.post(
            "/api/browser-pairing/complete",
            json={"code": code},
        )

    assert complete_resp.status_code == 200
    set_cookie = complete_resp.headers["set-cookie"]
    assert BROWSER_SESSION_COOKIE in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
