from __future__ import annotations

import json

import httpx
import pytest

from lantai.app import cloud_account as module


@pytest.mark.asyncio
async def test_watcha_flow_keeps_exchange_capability_and_tokens_out_of_renderer(
    monkeypatch,
):
    saved: dict = {}

    def save(payload):
        saved.clear()
        saved.update(payload)

    monkeypatch.setattr(module, "save_cloud_account", save)
    monkeypatch.setattr(module, "load_cloud_account", lambda: dict(saved))

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/watcha/start":
            body = json.loads(request.content)
            assert body["desktopRedirectUri"].startswith("http://127.0.0.1:19090/")
            assert len(body["desktopCodeChallenge"]) == 43
            return httpx.Response(
                200,
                json={
                    "flowId": "remote-flow",
                    "authorizationUrl": "https://watcha.cn/oauth/authorize?state=safe",
                    "expiresAt": "2030-01-01T00:00:00Z",
                },
            )
        if request.url.path == "/api/auth/watcha/exchange":
            body = json.loads(request.content)
            assert body["ticket"] == "one-time-ticket"
            assert len(body["codeVerifier"]) >= 43
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "status": "complete",
                    "provider": "watcha",
                    "user": {"id": "user-1", "displayName": "观察员"},
                    "membership": {"plan": "free", "status": "active"},
                    "tokens": {
                        "accessToken": "lantai-access",
                        "refreshToken": "lantai-refresh",
                    },
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    transport = httpx.MockTransport(handler)
    service = module.CloudAccountService(
        base_url="https://auth.lantai.guanlan.xin",
        client_factory=lambda **kwargs: httpx.AsyncClient(
            transport=transport,
            **kwargs,
        ),
    )

    started = await service.start_watcha(
        desktop_redirect_uri=(
            "http://127.0.0.1:19090/api/cloud-account/watcha/callback"
        ),
    )
    assert set(started) == {
        "attempt_id",
        "authorization_url",
        "expires_at",
        "poll_interval_seconds",
    }
    assert await service.poll_watcha(started["attempt_id"]) == {
        "status": "pending",
    }

    assert await service.complete_watcha(
        attempt_id=started["attempt_id"],
        ticket="one-time-ticket",
    )
    completed = await service.poll_watcha(started["attempt_id"])
    assert completed["connected"] is True
    assert completed["user"]["id"] == "user-1"
    assert completed.get("access_token") is None
    assert completed.get("refresh_token") is None
    assert saved["access_token"] == "lantai-access"
    assert saved["refresh_token"] == "lantai-refresh"


def test_account_service_url_rejects_non_https(monkeypatch):
    monkeypatch.setattr(
        module.EnvVarLoader,
        "get_str",
        lambda *_args, **_kwargs: "http://example.com",
    )
    with pytest.raises(module.CloudAccountError) as raised:
        module.account_service_url()
    assert raised.value.code == "account_service_url_invalid"


def test_loopback_watcha_callback_is_public_for_the_system_browser():
    from lantai.app.auth import _PUBLIC_PATHS

    assert "/api/cloud-account/watcha/callback" in _PUBLIC_PATHS


def test_cancelled_login_discards_the_local_pkce_verifier():
    service = module.CloudAccountService(
        base_url="https://auth.lantai.guanlan.xin",
    )
    attempt_id = module.pending_cloud_logins.create(
        attempt_id="cancel-me",
        code_verifier="local-secret",
        expires_at="2030-01-01T00:00:00Z",
    )
    assert service.cancel_watcha(attempt_id) == {"cancelled": True}
    assert module.pending_cloud_logins.get(attempt_id) is None


@pytest.mark.asyncio
async def test_failed_ticket_exchange_stops_local_polling():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/watcha/start":
            return httpx.Response(
                200,
                json={
                    "flowId": "failed-flow",
                    "authorizationUrl": "https://watcha.cn/oauth/authorize",
                    "expiresAt": "2030-01-01T00:00:00Z",
                },
            )
        return httpx.Response(
            410,
            json={"code": "login_ticket_invalid", "error": "expired"},
        )

    service = module.CloudAccountService(
        base_url="https://auth.lantai.guanlan.xin",
        client_factory=lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            **kwargs,
        ),
    )
    started = await service.start_watcha(
        desktop_redirect_uri=(
            "http://127.0.0.1:19090/api/cloud-account/watcha/callback"
        ),
    )
    with pytest.raises(module.CloudAccountError):
        await service.complete_watcha(
            attempt_id=started["attempt_id"],
            ticket="expired-ticket",
        )
    with pytest.raises(module.CloudAccountError) as raised:
        await service.poll_watcha(started["attempt_id"])
    assert raised.value.code == "login_ticket_invalid"
