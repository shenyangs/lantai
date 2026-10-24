"""Security contracts for TokenDance API-key OAuth + PKCE."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from lantai.providers.tokendance_oauth import (
    TOKENDANCE_APP_URL,
    TokenDanceOAuthRegistry,
    exchange_authorization_code,
    resolve_tokendance_callback_base_url,
)


def test_exchange_is_bound_to_the_started_callback_origin() -> None:
    registry = TokenDanceOAuthRegistry()
    callback = "https://cloud.example/api/models/tokendance/oauth/callback"
    started = registry.create(
        callback_base_url=callback,
        owner_subject_id="watcha:user-a",
        account_epoch=7,
    )
    with pytest.raises(ValueError, match="callback"):
        registry.begin_exchange(
            started.flow_id,
            callback_base_url="http://127.0.0.1:8088/api/models/tokendance/oauth/callback",
        )
    assert registry.status(started.flow_id).status == "pending"
    pending = registry.begin_exchange(started.flow_id, callback_base_url=callback)
    assert pending.callback_base_url == callback


def test_start_uses_s256_and_keeps_verifier_private() -> None:
    registry = TokenDanceOAuthRegistry(ttl_seconds=600)

    started = registry.create(
        callback_base_url=(
            "http://127.0.0.1:8088/api/models/tokendance/oauth/callback"
        ),
        owner_subject_id="watcha:user-a",
        account_epoch=7,
    )
    query = parse_qs(urlparse(started.authorization_url).query)

    assert query["code_challenge_method"] == ["S256"]
    assert query["app_url"] == [TOKENDANCE_APP_URL]
    assert query["key_name"] == ["澜台"]
    assert query["callback_url"][0].startswith(
        "http://127.0.0.1:8088/api/models/tokendance/oauth/callback?flow="
    )
    assert "code_verifier" not in started.model_dump()
    pending = registry.begin_exchange(
        started.flow_id,
        callback_base_url="http://127.0.0.1:8088/api/models/tokendance/oauth/callback",
    )
    assert 43 <= len(pending.code_verifier) <= 128
    assert pending.owner_subject_id == "watcha:user-a"
    assert pending.account_epoch == 7


def test_flow_is_single_use() -> None:
    registry = TokenDanceOAuthRegistry(ttl_seconds=600)
    started = registry.create(
        callback_base_url=(
            "http://127.0.0.1:8088/api/models/tokendance/oauth/callback"
        ),
        owner_subject_id="watcha:user-a",
        account_epoch=7,
    )

    callback = "http://127.0.0.1:8088/api/models/tokendance/oauth/callback"
    registry.begin_exchange(started.flow_id, callback_base_url=callback)

    with pytest.raises(ValueError, match="already being completed"):
        registry.begin_exchange(started.flow_id, callback_base_url=callback)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
def test_desktop_callback_keeps_the_current_backend_port(host) -> None:
    assert resolve_tokendance_callback_base_url(
        f"http://{host}:19088/models/tokendance/oauth/start",
    ) == (
        "http://127.0.0.1:19088/api/models/tokendance/oauth/callback"
    )


@pytest.mark.parametrize(
    "configured",
    [
        "http://cloud.example/api/models/tokendance/oauth/callback",
        "https://127.0.0.1/api/models/tokendance/oauth/callback",
        "https://[::1]/api/models/tokendance/oauth/callback",
        "https://localhost/api/models/tokendance/oauth/callback",
        "https://cloud.example/other-callback",
        "https://cloud.example/api/models/tokendance/oauth/callback?extra=1",
        "https://cloud.example/api/models/tokendance/oauth/callback#fragment",
        "https://user:password@cloud.example/api/models/tokendance/oauth/callback",
        "https://cloud.example:invalid/api/models/tokendance/oauth/callback",
        "https://cloud.example:9443/api/models/tokendance/oauth/callback",
    ],
)
def test_cloud_callback_configuration_rejects_unsafe_or_mismatched_urls(
    configured,
) -> None:
    with pytest.raises(ValueError):
        resolve_tokendance_callback_base_url(
            "https://cloud.example/models/tokendance/oauth/start", configured,
        )


@pytest.mark.asyncio
async def test_exchange_returns_key_without_logging_or_echoing_response(
    monkeypatch,
) -> None:
    captured: dict = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"key": "oauth-created-secret"}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            captured.update(url=url, kwargs=kwargs)
            return FakeResponse()

    monkeypatch.setattr(
        "lantai.providers.tokendance_oauth.httpx.AsyncClient",
        lambda **_kwargs: FakeClient(),
    )

    key = await exchange_authorization_code(
        code="one-time-code",
        code_verifier="v" * 64,
    )

    assert key == "oauth-created-secret"
    assert captured["kwargs"]["json"] == {
        "code": "one-time-code",
        "code_verifier": "v" * 64,
        "code_challenge_method": "S256",
    }
