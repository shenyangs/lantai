from __future__ import annotations

import ssl

import httpx
import pytest

from lantai.providers import openai_http
from lantai.providers.openai_chat_model_compat import OpenAIChatModelCompat
from lantai.providers.model_endpoint import ProviderModelEndpoint
from lantai.runtime.adapters import openai_model_client


def _endpoint() -> ProviderModelEndpoint:
    return ProviderModelEndpoint(
        provider_id="provider-a",
        model_id="model-a",
        family="openai",
        base_url="http://127.0.0.1:9/v1",
        api_key="test-key",
        chat_model="OpenAIChatModel",
        native_kind="openai_compatible",
    )


def test_shared_openai_ssl_context_is_built_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    openai_http.shared_openai_ssl_context.cache_clear()
    calls = 0

    def create_context(**_kwargs) -> ssl.SSLContext:
        nonlocal calls
        calls += 1
        return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    monkeypatch.setattr(httpx, "create_ssl_context", create_context)

    first = openai_http.shared_openai_ssl_context()
    second = openai_http.shared_openai_ssl_context()

    assert first is second
    assert calls == 1
    openai_http.shared_openai_ssl_context.cache_clear()


@pytest.mark.asyncio
async def test_legacy_openai_model_preserves_injected_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = httpx.AsyncClient(verify=False)
    monkeypatch.setattr(
        "lantai.providers.openai_chat_model_compat."
        "create_openai_async_http_client",
        lambda: pytest.fail("factory must not replace an injected client"),
    )
    model = OpenAIChatModelCompat(
        "model-a",
        api_key="test-key",
        client_kwargs={
            "base_url": "http://127.0.0.1:9/v1",
            "http_client": client,
        },
    )
    try:
        assert (
            model.client._client is client  # pylint: disable=protected-access
        )
    finally:
        await model.client.close()


@pytest.mark.asyncio
async def test_native_openai_model_uses_shared_context_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = httpx.AsyncClient(verify=False)
    monkeypatch.setattr(
        openai_model_client,
        "create_openai_async_http_client",
        lambda: client,
    )
    model = openai_model_client.OpenAICompatibleModelClient(_endpoint())
    try:
        assert (
            model._client._client is client  # pylint: disable=protected-access
        )
    finally:
        await model._client.close()  # pylint: disable=protected-access
