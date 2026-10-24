"""Security and transport behavior for the TokenDance account client."""

from __future__ import annotations

import pytest

from lantai.providers import tokendance_account as account_module
from lantai.providers.tokendance_account import (
    TokenDanceAccountClient,
    TokenDanceAccountError,
)


class _Response:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    @property
    def is_error(self) -> bool:
        return self.status_code >= 400

    def json(self):
        return self._payload


class _AsyncClient:
    response = _Response(200, {"balance": {"balance": 1}})
    calls: list[dict] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def request(self, method, url, **kwargs):
        self.__class__.calls.append(
            {"method": method, "url": url, **kwargs},
        )
        return self.__class__.response


@pytest.fixture(autouse=True)
def _fake_http(monkeypatch) -> None:
    _AsyncClient.calls = []
    _AsyncClient.response = _Response(200, {"balance": {"balance": 1}})
    TokenDanceAccountClient._usage_cache = {}
    TokenDanceAccountClient._usage_last_request_at = {}
    monkeypatch.setattr(account_module.httpx, "AsyncClient", _AsyncClient)


@pytest.mark.asyncio
async def test_balance_uses_saved_key_only_in_authorization_header() -> None:
    payload = await TokenDanceAccountClient("secret-key").balance()

    assert payload == {"balance": {"balance": 1}}
    call = _AsyncClient.calls[0]
    assert call["url"] == (
        "https://tokendance.space/portal/api/v1/user/balance"
    )
    assert call["headers"]["Authorization"] == "Bearer secret-key"
    assert "secret-key" not in call["url"]


@pytest.mark.asyncio
async def test_redemption_posts_code_to_official_endpoint_without_echo() -> None:
    _AsyncClient.response = _Response(200, {"credits": 1_000_000})

    payload = await TokenDanceAccountClient("secret-key").redeem("gift-code")

    assert payload == {"credits": 1_000_000}
    call = _AsyncClient.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == (
        "https://tokendance.space/portal/api/v1/redemption/redeem"
    )
    assert call["json"] == {"code": "gift-code"}
    assert call["headers"]["Authorization"] == "Bearer secret-key"
    assert "gift-code" not in call["url"]


@pytest.mark.asyncio
async def test_redemption_error_never_uses_upstream_code_as_a_diagnostic() -> None:
    _AsyncClient.response = _Response(
        400,
        {"error": {"code": "gift-code", "message": "gift-code is invalid"}},
    )

    with pytest.raises(TokenDanceAccountError) as caught:
        await TokenDanceAccountClient("secret-key").redeem("gift-code")

    assert caught.value.status_code == 400
    assert caught.value.code == "tokendance_request_failed"
    assert "gift-code" not in str(caught.value)


@pytest.mark.asyncio
async def test_upstream_auth_error_is_sanitized() -> None:
    _AsyncClient.response = _Response(
        401,
        {"error": {"code": "unauthorized", "message": "secret-key invalid"}},
    )

    with pytest.raises(TokenDanceAccountError) as caught:
        await TokenDanceAccountClient("secret-key").balance()

    assert caught.value.status_code == 409
    assert caught.value.code == "unauthorized"
    assert caught.value.kind == "authentication"
    assert "secret-key" not in caught.value.message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "payload", "expected_status", "expected_kind", "unknown"),
    [
        (401, {"error": {"code": "bad_key", "message": "secret-key"}}, 409, "authentication", False),
        (403, {"error": {"code": "forbidden", "message": "secret-key"}}, 403, "authorization", False),
        (402, {"error": {"code": "insufficient_balance", "message": "secret-key"}}, 402, "insufficient_balance", False),
        (429, {"error": {"code": "slow_down", "message": "secret-key"}}, 429, "rate_limit", False),
        (500, {"error": {"code": "internal", "message": "secret-key"}}, 502, "server", True),
    ],
)
async def test_t050_account_errors_are_typed_without_leaking_upstream_body(
    status: int,
    payload: dict,
    expected_status: int,
    expected_kind: str,
    unknown: bool,
) -> None:
    _AsyncClient.response = _Response(status, payload)

    with pytest.raises(TokenDanceAccountError) as caught:
        await TokenDanceAccountClient("secret-key").balance()

    assert caught.value.status_code == expected_status
    assert caught.value.kind == expected_kind
    assert caught.value.outcome_unknown is unknown
    assert "secret-key" not in str(caught.value)


@pytest.mark.asyncio
async def test_t050_transport_and_bad_json_are_not_authentication_failures(
    monkeypatch,
) -> None:
    async def timeout(*_args, **_kwargs):
        raise account_module.httpx.ReadTimeout("secret-key timed out")

    monkeypatch.setattr(_AsyncClient, "request", timeout)
    with pytest.raises(TokenDanceAccountError) as unavailable:
        await TokenDanceAccountClient("secret-key").balance()

    assert unavailable.value.kind == "transport"
    assert unavailable.value.outcome_unknown is True
    assert unavailable.value.code == "tokendance_unavailable"
    assert "secret-key" not in str(unavailable.value)

    class _InvalidJsonResponse(_Response):
        def json(self):
            raise ValueError("secret-key malformed")

    async def invalid_json(*_args, **_kwargs):
        return _InvalidJsonResponse(200, {})

    monkeypatch.setattr(_AsyncClient, "request", invalid_json)
    with pytest.raises(TokenDanceAccountError) as malformed:
        await TokenDanceAccountClient("secret-key").balance()

    assert malformed.value.kind == "invalid_response"
    assert malformed.value.outcome_unknown is True
    assert malformed.value.code == "tokendance_invalid_response"
    assert "secret-key" not in str(malformed.value)


@pytest.mark.asyncio
async def test_usage_cache_is_separated_by_account() -> None:
    _AsyncClient.response = _Response(200, {"items": []})
    params = {"limit": 5, "window": "7d"}

    await TokenDanceAccountClient("account-a").usage(params)
    await TokenDanceAccountClient("account-a").usage(params)
    await TokenDanceAccountClient("account-b").usage(params)

    assert len(_AsyncClient.calls) == 2


@pytest.mark.asyncio
async def test_usage_cache_and_throttle_state_are_bounded() -> None:
    _AsyncClient.response = _Response(200, {"items": []})

    for index in range(TokenDanceAccountClient._USAGE_CACHE_LIMIT + 6):
        await TokenDanceAccountClient(
            f"account-{index}",
            authorization_context_id=f"ctx-{index}",
            monotonic=lambda: 10.0,
        ).usage({"window": "7d"})

    assert len(_AsyncClient.calls) == (
        TokenDanceAccountClient._USAGE_CACHE_LIMIT + 6
    )
    assert len(TokenDanceAccountClient._usage_cache) == (
        TokenDanceAccountClient._USAGE_CACHE_LIMIT
    )
    assert len(TokenDanceAccountClient._usage_last_request_at) == (
        TokenDanceAccountClient._USAGE_CACHE_LIMIT
    )


@pytest.mark.asyncio
async def test_usage_throttles_different_queries_in_one_authorization(
) -> None:
    ticks = iter([10.0, 10.0, 10.0, 11.0, 11.0])
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
    TokenDanceAccountClient._usage_cache = {}
    TokenDanceAccountClient._usage_last_request_at = {}
    client = TokenDanceAccountClient(
        "account-a",
        authorization_context_id="ctx-a",
        monotonic=lambda: next(ticks),
        sleep=fake_sleep,
    )

    await client.usage({"window": "7d"})
    await client.usage({"window": "30d"})

    assert sleeps == [1.0]
    assert len(_AsyncClient.calls) == 2


@pytest.mark.asyncio
async def test_usage_cache_is_fenced_by_authorization_context() -> None:
    TokenDanceAccountClient._usage_cache = {}
    TokenDanceAccountClient._usage_last_request_at = {}
    params = {"window": "7d"}

    await TokenDanceAccountClient(
        "same-key",
        authorization_context_id="ctx-a",
    ).usage(params)
    await TokenDanceAccountClient(
        "same-key",
        authorization_context_id="ctx-b",
    ).usage(params)

    assert len(_AsyncClient.calls) == 2
