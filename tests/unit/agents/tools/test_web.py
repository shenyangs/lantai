# -*- coding: utf-8 -*-
"""Behavior locks for Lantai's provider-independent Web baseline."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx2
import pytest

from lantai.agents.tools import web as web_tools
from lantai.app.web_search import WebSearchStore


@pytest.fixture(autouse=True)
def baseline_web_config(monkeypatch, tmp_path):
    store = WebSearchStore(path=tmp_path / "web_search.json")
    monkeypatch.setattr(web_tools, "get_web_search_store", lambda: store)


@pytest.mark.asyncio
async def test_public_web_request_uses_the_pinned_clients_timeout_type(
    monkeypatch,
) -> None:
    """Lock the httpx/httpx2 boundary that previously crashed before connect."""

    captured: dict[str, object] = {}

    async def fake_resolve(url: str, **_kwargs):
        return SimpleNamespace(
            url=url,
            host="example.com",
            pinned_ip="93.184.216.34",
        )

    class ClientCaptured(RuntimeError):
        pass

    def fake_create(_endpoint, **kwargs):
        captured.update(kwargs)
        raise ClientCaptured

    monkeypatch.setattr(web_tools, "resolve_mcp_endpoint", fake_resolve)
    monkeypatch.setattr(
        web_tools,
        "create_pinned_mcp_http_client",
        fake_create,
    )

    with pytest.raises(ClientCaptured):
        await web_tools._request_public_document("https://example.com/")

    assert isinstance(captured["timeout"], httpx2.Timeout)


def test_web_prompt_keeps_lantai_baseline_independent_from_guanlan() -> None:
    baseline = web_tools.build_lantai_web_prompt(
        language="zh",
        guanlan_available=False,
    )
    enhanced = web_tools.build_lantai_web_prompt(
        language="zh",
        guanlan_available=True,
    )

    assert "澜台自身的基础联网能力" in baseline
    assert "不依赖观澜" in baseline
    assert "web_search" in baseline
    assert "web_fetch" in baseline
    assert "观澜不可用、超时、报错或结果不足" in enhanced
    assert "先降级到 `web_search`" in enhanced
    assert "不能据此回答“没有资料”" in enhanced
    assert "recommended_route=guanlan_read" in enhanced
    assert "同一 URL" in enhanced


@pytest.mark.asyncio
async def test_web_search_returns_normalized_public_results_without_guanlan(
    monkeypatch,
) -> None:
    requested_urls: list[str] = []
    rss = b"""<?xml version="1.0" encoding="utf-8"?>
    <rss><channel>
      <item>
        <title>Lantai release notes</title>
        <link>https://example.com/releases</link>
        <description>Latest stable release and changes.</description>
      </item>
      <item>
        <title>Architecture guide</title>
        <link>https://docs.example.com/architecture</link>
        <description>Runtime and tool boundaries.</description>
      </item>
    </channel></rss>"""

    async def fake_request(url: str, **_kwargs):
        requested_urls.append(url)
        return SimpleNamespace(
            final_url=url,
            content_type="application/rss+xml; charset=utf-8",
            body=rss,
        )

    monkeypatch.setattr(web_tools, "_request_public_document", fake_request)

    response = await web_tools.web_search("Lantai latest", limit=2)
    payload = json.loads(response.content[0]["text"])

    assert "q=Lantai+latest" in requested_urls[0]
    assert payload["status"] == "ok"
    assert payload["provider"] == "bing_rss"
    assert payload["results"] == [
        {
            "rank": 1,
            "title": "Lantai release notes",
            "url": "https://example.com/releases",
            "snippet": "Latest stable release and changes.",
            "can_cite_as_page_body": False,
        },
        {
            "rank": 2,
            "title": "Architecture guide",
            "url": "https://docs.example.com/architecture",
            "snippet": "Runtime and tool boundaries.",
            "can_cite_as_page_body": False,
        },
    ]


@pytest.mark.asyncio
async def test_web_search_returns_product_safe_unavailable_result_on_network_failure(
    monkeypatch,
) -> None:
    """A provider outage must not leak a low-level ConnectError into chat."""

    async def failed_request(_url: str, **_kwargs):
        raise httpx2.ConnectError("socket route refused")

    monkeypatch.setattr(web_tools, "_request_public_document", failed_request)

    response = await web_tools.web_search("今日热点")
    payload = json.loads(response.content[0]["text"])

    assert payload == {
        "schema_version": "lantai_web_search_v1",
        "status": "unavailable",
        "provider": "bing_rss",
        "query": "今日热点",
        "result_count": 0,
        "results": [],
        "error_code": "network_unavailable",
        "retryable": True,
        "model_guidance": (
            "Do not claim that no public evidence exists. Try another available "
            "source, including Guanlan when ready, or explain that search is "
            "temporarily unavailable."
        ),
    }


@pytest.mark.asyncio
async def test_web_fetch_returns_citable_readable_page_body(monkeypatch) -> None:
    html = b"""<!doctype html><html><head>
    <title>Release notes</title><style>.hidden {display:none}</style>
    <script>window.secret = 'ignore';</script></head><body>
    <nav>Home Products</nav><main><h1>Lantai 3.2</h1>
    <p>Released on 2026-08-01 with a safer runtime.</p></main></body></html>"""

    async def fake_request(url: str, **_kwargs):
        return SimpleNamespace(
            final_url="https://example.com/releases/3.2",
            content_type="text/html; charset=utf-8",
            body=html,
        )

    monkeypatch.setattr(web_tools, "_request_public_document", fake_request)

    response = await web_tools.web_fetch(
        "https://example.com/releases/latest",
        max_chars=5_000,
    )
    payload = json.loads(response.content[0]["text"])

    assert payload["status"] == "ok"
    assert payload["retrieval_status"] == "usable"
    assert payload["quality_score"] >= 80
    assert payload["url"] == "https://example.com/releases/3.2"
    assert payload["title"] == "Release notes"
    assert payload["can_cite_as_page_body"] is True
    assert payload["retry_recommended"] is False
    assert payload["truncated"] is False
    assert "Lantai 3.2" in payload["text"]
    assert "Released on 2026-08-01" in payload["text"]
    assert "window.secret" not in payload["text"]
    assert "display:none" not in payload["text"]


@pytest.mark.asyncio
async def test_web_fetch_falls_back_to_reader_without_calling_guanlan(
    monkeypatch,
) -> None:
    calls: list[str] = []

    async def fake_request(url: str, **_kwargs):
        calls.append(url)
        if len(calls) == 1:
            request = httpx2.Request("GET", url)
            response = httpx2.Response(403, request=request)
            raise httpx2.HTTPStatusError(
                "blocked",
                request=request,
                response=response,
            )
        return SimpleNamespace(
            final_url=url,
            content_type="text/plain; charset=utf-8",
            body=(
                b"Title: Release notes\n\n"
                b"Markdown Content:\nLantai 3.2 shipped safely."
            ),
        )

    monkeypatch.setattr(web_tools, "_request_public_document", fake_request)

    response = await web_tools.web_fetch("https://example.com/releases")
    payload = json.loads(response.content[0]["text"])

    assert calls == [
        "https://example.com/releases",
        "https://r.jina.ai/https://example.com/releases",
    ]
    assert payload["status"] == "ok"
    assert payload["provider"] == "jina_reader"
    assert payload["url"] == "https://example.com/releases"
    assert payload["title"] == "Release notes"
    assert "Lantai 3.2 shipped safely" in payload["text"]
    assert payload["can_cite_as_page_body"] is True


@pytest.mark.asyncio
async def test_web_fetch_rejects_nonempty_weibo_visitor_shell(monkeypatch) -> None:
    calls: list[str] = []
    html = b"""<!doctype html><html><head><title>Sina Visitor System</title></head>
    <body><h1>Sina Visitor System</h1><p>Please verify before continuing.</p>
    <p>This page is not the requested Weibo post.</p></body></html>"""

    async def fake_request(url: str, **_kwargs):
        calls.append(url)
        return SimpleNamespace(
            final_url="https://passport.weibo.com/visitor/visitor",
            content_type="text/html; charset=utf-8",
            body=html,
        )

    monkeypatch.setattr(web_tools, "_request_public_document", fake_request)

    response = await web_tools.web_fetch(
        "https://weibo.com/5787529871/RfCYMlg7q",
    )
    payload = json.loads(response.content[0]["text"])

    assert calls == ["https://weibo.com/5787529871/RfCYMlg7q"]
    assert payload["status"] == "blocked"
    assert payload["retrieval_status"] == "blocked"
    assert payload["can_cite_as_page_body"] is False
    assert payload["retry_recommended"] is True
    assert payload["recommended_route"] == "guanlan_read"
    assert "auth_or_verification_page" in payload["quality_signals"]


@pytest.mark.asyncio
async def test_web_fetch_rejects_nonempty_lofter_login_redirect(monkeypatch) -> None:
    html = """<!doctype html><html><head><title>登录 - LOFTER</title></head>
    <body><h1>登录网易 LOFTER</h1><p>手机号登录 验证码登录</p></body></html>""".encode()

    async def fake_request(url: str, **_kwargs):
        return SimpleNamespace(
            final_url="https://www.lofter.com/login?target=post",
            content_type="text/html; charset=utf-8",
            body=html,
        )

    monkeypatch.setattr(web_tools, "_request_public_document", fake_request)

    response = await web_tools.web_fetch(
        "https://shujuyuan42597.lofter.com/post/741e37b1_34f2e9d0e",
    )
    payload = json.loads(response.content[0]["text"])

    assert payload["retrieval_status"] == "blocked"
    assert payload["can_cite_as_page_body"] is False
    assert payload["recommended_route"] == "guanlan_read"


@pytest.mark.asyncio
async def test_web_fetch_keeps_a_short_real_social_post_citable(monkeypatch) -> None:
    html = """<!doctype html><html><head><title>DataYuanChina on X</title></head>
    <body><article>今天正式发布 V-Hub，完整活动信息见置顶链接。</article></body></html>""".encode()

    async def fake_request(url: str, **_kwargs):
        return SimpleNamespace(
            final_url=url,
            content_type="text/html; charset=utf-8",
            body=html,
        )

    monkeypatch.setattr(web_tools, "_request_public_document", fake_request)

    response = await web_tools.web_fetch(
        "https://x.com/DataYuanChina/status/2093582651995349139",
    )
    payload = json.loads(response.content[0]["text"])

    assert payload["retrieval_status"] == "usable"
    assert payload["can_cite_as_page_body"] is True
    assert payload["retry_recommended"] is False


@pytest.mark.asyncio
async def test_web_fetch_uses_jina_once_for_a_generic_dynamic_shell(
    monkeypatch,
) -> None:
    calls: list[str] = []

    async def fake_request(url: str, **_kwargs):
        calls.append(url)
        if len(calls) == 1:
            return SimpleNamespace(
                final_url=url,
                content_type="text/html; charset=utf-8",
                body=(
                    b"<html><head><title>Article</title></head>"
                    b"<body>Please enable JavaScript to continue.</body></html>"
                ),
            )
        return SimpleNamespace(
            final_url=url,
            content_type="text/plain; charset=utf-8",
            body=(
                b"Title: Article\n\nMarkdown Content:\n"
                b"The full article explains the release, its migration path, "
                b"and the compatibility guarantees for existing users."
            ),
        )

    monkeypatch.setattr(web_tools, "_request_public_document", fake_request)

    response = await web_tools.web_fetch("https://example.com/article")
    payload = json.loads(response.content[0]["text"])

    assert calls == [
        "https://example.com/article",
        "https://r.jina.ai/https://example.com/article",
    ]
    assert payload["provider"] == "jina_reader"
    assert payload["retrieval_status"] == "usable"
    assert payload["can_cite_as_page_body"] is True
    assert payload["attempted_routes"] == ["direct", "jina_reader"]


@pytest.mark.asyncio
async def test_web_fetch_preserves_direct_partial_text_when_jina_fails(
    monkeypatch,
) -> None:
    calls: list[str] = []

    async def fake_request(url: str, **_kwargs):
        calls.append(url)
        if len(calls) == 1:
            return SimpleNamespace(
                final_url=url,
                content_type="text/html; charset=utf-8",
                body=(
                    b"<html><head><title>Article</title></head>"
                    b"<body>Please enable JavaScript to continue.</body></html>"
                ),
            )
        request = httpx2.Request("GET", url)
        response = httpx2.Response(503, request=request)
        raise httpx2.HTTPStatusError(
            "reader unavailable",
            request=request,
            response=response,
        )

    monkeypatch.setattr(web_tools, "_request_public_document", fake_request)

    response = await web_tools.web_fetch("https://example.com/article")
    payload = json.loads(response.content[0]["text"])

    assert payload["retrieval_status"] == "dynamic_shell"
    assert payload["text"] == "Article Please enable JavaScript to continue."
    assert payload["recommended_route"] == "browser"
    assert payload["quality_signals"] == [
        "javascript_shell",
        "jina_reader_failed",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/private",
        "https://198.18.0.8/private",
        "https://router.local/private",
        "https://user:password@example.com/private",
    ],
)
async def test_web_fetch_blocks_local_or_credential_bearing_urls(url: str) -> None:
    with pytest.raises(ValueError):
        await web_tools.web_fetch(url)
