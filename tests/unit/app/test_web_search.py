"""Migration and wire-contract locks for unified Web search."""

import asyncio
import json

import httpx
import pytest

from lantai.app.web_search import (
    DeepSeekWebClient,
    DeepSeekWebError,
    WebSearchStore,
)
from lantai.security.secret_store import encrypt

pytestmark = pytest.mark.usefixtures("isolated_web_search_secrets")


@pytest.mark.parametrize("enabled", [True, False])
def test_legacy_key_and_consent_migrate_without_recounting_tokens(
    tmp_path, enabled
):
    old = tmp_path / "deepseek_quick_search.json"
    legacy = {
        "version": 1,
        "api_key": encrypt("sk-old-search"),
        "enabled": enabled,
        "model": "deepseek-v4-flash",
        "call_count": 7,
        "input_tokens": 120,
        "output_tokens": 20,
        "last_used_at": "old-date",
    }
    old.write_text(json.dumps(legacy))
    store = WebSearchStore(path=tmp_path / "web_search.json", legacy_path=old)
    assert store.status().enabled is enabled
    assert store.status().migrated_from_quick_search is True
    assert store.status().search_count == store.status().fetch_count == 0
    assert not store.path.exists()  # Reading configuration never mutates it.
    store.save(api_key="", enabled=enabled)
    assert store.load_api_key() == "sk-old-search"
    store.record_request("search")
    store.record_request("fetch")
    reloaded = WebSearchStore(path=store.path, legacy_path=old)
    assert reloaded.status().search_count == reloaded.status().fetch_count == 1
    assert reloaded.status().enabled is enabled
    data = json.loads(store.path.read_text())
    assert data["legacy_usage"]["input_tokens"] == 120
    assert data["legacy_usage"]["call_count"] == 7
    assert "sk-old-search" not in store.path.read_text()
    assert json.loads(old.read_text()) == legacy


def test_missing_key_never_comes_from_chat_provider_or_environment(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-chat-provider")
    store = WebSearchStore(
        path=tmp_path / "web.json", legacy_path=tmp_path / "old.json"
    )
    assert store.status().provider == "bing_rss"
    assert not store.load_api_key()
    with pytest.raises(ValueError, match="key"):
        store.save(api_key="", enabled=True)
    assert store.save(api_key="", enabled=False).enabled is False


@pytest.mark.asyncio
async def test_raw_endpoints_need_no_model_and_do_not_send_private_context():
    requests = []

    def handler(request):
        requests.append(request)
        data = (
            {"results": []}
            if request.url.path.endswith("search")
            else {
                "title": "Article",
                "content": "# Article\n\nPublic body.",
            }
        )
        return httpx.Response(200, json=data)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http:
        client = DeepSeekWebClient(api_key="sk-search", http_client=http)
        assert await client.search("site:example.com news", count=2) == []
        assert (await client.fetch("https://example.com/news"))[
            "title"
        ] == "Article"
    assert [str(r.url) for r in requests] == [
        "https://api.deepseek.com/v1/tools/search",
        "https://api.deepseek.com/v1/tools/fetch",
    ]
    assert json.loads(requests[0].content) == {
        "query": "site:example.com news",
        "count": 2,
        "need_content": False,
    }
    assert json.loads(requests[1].content) == {
        "url": "https://example.com/news"
    }
    assert requests[0].headers["authorization"] == "Bearer sk-search"


@pytest.mark.asyncio
async def test_request_limits_are_checked_before_network():
    def unexpected(request):
        pytest.fail("Invalid input must not reach the API")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(unexpected)
    ) as http:
        client = DeepSeekWebClient(api_key="sk-limits", http_client=http)
        for query in [" ", "中" * 129]:
            with pytest.raises(ValueError):
                await client.search(query)
        with pytest.raises(ValueError):
            await client.search("valid", count=51)
        for url in [
            "file:///tmp/a",
            "https://user:pass@example.com",
            "https://example.com/" + "a" * 8192,
        ]:
            with pytest.raises(ValueError):
                await client.fetch(url)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,attempts", [(401, 1), (402, 1), (429, 2), (503, 2)]
)
async def test_retries_are_bounded_and_errors_do_not_echo_provider_body(
    status, attempts
):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status, text="sk-secret private provider response"
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http:
        client = DeepSeekWebClient(
            api_key=f"sk-error-{status}", http_client=http
        )
        with pytest.raises(DeepSeekWebError) as caught:
            await client.search("news")
    assert len(calls) == attempts
    assert "sk-secret" not in str(caught.value)
    assert str(status) in str(caught.value)


@pytest.mark.asyncio
async def test_cancellation_never_retries():
    calls = []

    async def handler(request):
        calls.append(request)
        raise asyncio.CancelledError

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http:
        with pytest.raises(asyncio.CancelledError):
            await DeepSeekWebClient(
                api_key="sk-cancel", http_client=http
            ).search("news")
    assert len(calls) == 1
