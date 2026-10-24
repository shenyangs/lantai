"""New backend shares the existing tools, evidence rules and safety boundary."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import httpx2
import pytest

from lantai.agents.tools import web
from lantai.app.web_search import DeepSeekWebClient, WebSearchStore

pytestmark = pytest.mark.usefixtures("isolated_web_search_secrets")


@pytest.fixture
def configured(monkeypatch, tmp_path):
    store = WebSearchStore(
        path=tmp_path / "new.json", legacy_path=tmp_path / "old.json"
    )
    store.save(api_key="sk-tools-only", enabled=True)
    monkeypatch.setattr(web, "get_web_search_store", lambda: store)
    return store


def payload(response):
    return json.loads(response.content[0]["text"])


@pytest.mark.asyncio
async def test_search_preserves_sources_and_provider_scores_as_discovery_only(
    monkeypatch, configured
):
    async def search(self, query, *, count):
        return [
            {
                "title": "News",
                "url": "https://example.com/news",
                "snippet": "Short",
                "summary": "Long summary",
                "rerank_score": 2.4,
                "authority": 3,
                "date": None,
                "site_name": "Example",
                "site_icon": None,
                "content": "",
            }
        ]

    monkeypatch.setattr(DeepSeekWebClient, "search", search)
    result = payload(await web.web_search("news", limit=2))
    assert result["provider"] == "deepseek"
    assert result["results"][0]["url"] == "https://example.com/news"
    assert result["results"][0]["provider_scores"] == {
        "rerank_score": 2.4,
        "authority": 3,
    }
    assert result["results"][0]["can_cite_as_page_body"] is False
    assert configured.status().search_count == 1


@pytest.mark.asyncio
async def test_paid_backend_failure_falls_back_with_real_provenance(
    monkeypatch, configured
):
    def failed(_request):
        return httpx.Response(402)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(failed)
    ) as http:
        monkeypatch.setattr(
            web,
            "DeepSeekWebClient",
            lambda **kw: DeepSeekWebClient(http_client=http, **kw),
        )

        async def rss(url, **kw):
            return SimpleNamespace(
                body=b"<rss><channel><item><title>Fallback</title><link>https://example.com</link></item></channel></rss>"
            )

        monkeypatch.setattr(web, "_request_public_document", rss)
        result = payload(await web.web_search("news"))
    assert result["provider"] == "bing_rss"
    assert result["attempted_routes"] == ["deepseek", "bing_rss"]
    assert result["route_errors"][0]["error_code"] == "http_402"
    assert configured.status().search_count == 0


@pytest.mark.asyncio
async def test_fetch_checks_public_target_and_keeps_citation_and_truncation(
    monkeypatch, configured
):
    checked = []

    async def check(url):
        checked.append(url)

    async def fetch(self, url):
        assert checked == [url]
        return {
            "title": "News",
            "content": "# News\n\n" + "Public article details. " * 200,
        }

    monkeypatch.setattr(web, "_resolve_public_endpoint", check)
    monkeypatch.setattr(DeepSeekWebClient, "fetch", fetch)
    result = payload(
        await web.web_fetch("https://example.com/news", max_chars=1000)
    )
    assert result["provider"] == "deepseek"
    assert result["can_cite_as_page_body"] is True
    assert result["truncated"] is True
    assert len(result["text"]) == 1000
    assert result["url"] == "https://example.com/news"
    assert configured.status().fetch_count == 1


@pytest.mark.asyncio
async def test_cancelled_search_does_not_start_a_fallback(
    monkeypatch, configured
):
    async def cancel(self, *a, **kw):
        raise asyncio.CancelledError

    async def fallback(*a, **kw):
        pytest.fail("Cancelled work must stay stopped")

    monkeypatch.setattr(DeepSeekWebClient, "search", cancel)
    monkeypatch.setattr(web, "_request_public_document", fallback)
    with pytest.raises(asyncio.CancelledError):
        await web.web_search("news")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content,expected_count", [("", 0), ("Please enable JavaScript", 1)]
)
async def test_fetch_empty_or_shell_content_uses_quality_checked_fallback(
    monkeypatch,
    configured,
    content,
    expected_count,
):
    async def public(_url):
        return None

    async def fetch(self, _url):
        return {"title": "Page", "content": content}

    async def request(url, **_kwargs):
        return web.PublicWebDocument(
            final_url=url,
            content_type="text/html",
            body=(
                "<html><title>Article</title><body><article>"
                + "Public article details with verified source links. " * 100
                + "</article></body></html>"
            ).encode(),
        )

    monkeypatch.setattr(web, "_resolve_public_endpoint", public)
    monkeypatch.setattr(web.DeepSeekWebClient, "fetch", fetch)
    monkeypatch.setattr(web, "_request_public_document", request)
    result = payload(await web.web_fetch("https://example.com/news"))
    assert result["provider"] == "direct"
    assert result["attempted_routes"] == ["deepseek", "direct"]
    assert result["can_cite_as_page_body"] is True
    assert configured.status().fetch_count == expected_count


@pytest.mark.asyncio
async def test_private_url_never_reaches_remote_fetch(monkeypatch, configured):
    async def unexpected(*_args, **_kwargs):
        pytest.fail("Private destinations must be rejected before disclosure")

    monkeypatch.setattr(DeepSeekWebClient, "fetch", unexpected)
    with pytest.raises(ValueError):
        await web.web_fetch("https://127.0.0.1/private")
    assert configured.status().fetch_count == 0


@pytest.mark.asyncio
async def test_disabled_provider_keeps_baseline_without_loading_key(
    monkeypatch, configured
):
    configured.save(api_key="", enabled=False)

    def unexpected():
        pytest.fail("Disabled provider must not load its key")

    async def rss(_url, **_kwargs):
        return SimpleNamespace(body=b"<rss><channel/></rss>")

    monkeypatch.setattr(configured, "load_api_key", unexpected)
    monkeypatch.setattr(web, "_request_public_document", rss)
    result = payload(await web.web_search("news"))
    assert result["provider"] == "bing_rss"
    assert configured.status().search_count == 0


@pytest.mark.asyncio
async def test_cancelled_fetch_does_not_start_fallback(
    monkeypatch, configured
):
    async def public(_url):
        return None

    async def cancel(*_args, **_kwargs):
        raise asyncio.CancelledError

    async def unexpected(*_args, **_kwargs):
        pytest.fail("Cancelled work must stay stopped")

    monkeypatch.setattr(web, "_resolve_public_endpoint", public)
    monkeypatch.setattr(DeepSeekWebClient, "fetch", cancel)
    monkeypatch.setattr(web, "_request_public_document", unexpected)
    with pytest.raises(asyncio.CancelledError):
        await web.web_fetch("https://example.com/news")


@pytest.mark.asyncio
async def test_weak_fetch_does_not_claim_unobserved_fallback_routes(
    monkeypatch, configured
):
    async def public(_url):
        return None

    async def fetch(self, _url):
        return {"title": "Page", "content": "Please enable JavaScript"}

    async def failed(_url, **_kwargs):
        raise httpx2.ConnectError("unavailable")

    monkeypatch.setattr(web, "_resolve_public_endpoint", public)
    monkeypatch.setattr(DeepSeekWebClient, "fetch", fetch)
    monkeypatch.setattr(web, "_baseline_web_fetch", failed)
    result = payload(await web.web_fetch("https://example.com/news"))
    assert result["provider"] == "deepseek"
    assert result["can_cite_as_page_body"] is False
    assert result["attempted_routes"] == ["deepseek", "baseline"]
