from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lantai.app.reading import catalog, rsshub
from lantai.app.reading.store import ReadingStore
from lantai.agents.tools.web import PublicWebDocument
from lantai.app.mcp.network_security import ResolvedMCPEndpoint


def test_complete_catalog_has_every_official_route_and_parameter_form(tmp_path):
    data = catalog.snapshot()
    assert data["route_count"] == len(data["channels"]) == 3865
    assert len({item["id"] for item in data["channels"]}) == 3865
    assert data["namespace_count"] == 1984
    assert data["source_revision"] == "913f11220a1bd5b2c1dd169d9890ff24001988de"
    for item in data["channels"]:
        view = catalog.channel_view(item)
        assert isinstance(view["fields"], list)
        assert item["path"].startswith("/")
        # No executable upstream modules or radar functions are shipped.
        assert not {"module", "handler", "radar"}.intersection(item)
    store = ReadingStore(tmp_path)
    assert catalog.list_channels(store, q="微博")["total"] > 0
    assert catalog.list_channels(store, q="github trending")["total"] == 1
    assert catalog.list_channels(store, category="finance")["total"] > 0
    assert catalog.list_channels(store, offset=3850)["total"] == 3865


def test_parameter_binding_supports_optionals_wildcards_and_custom_base():
    channel = {"path": "/github/trending/:since?/:language?/:spoken?"}
    assert (
        catalog.compose(channel, {}, "https://rsshub.app")
        == "https://rsshub.app/github/trending"
    )
    assert (
        catalog.compose(
            channel,
            {"since": "daily", "language": "python"},
            "http://localhost:1200/hub",
        )
        == "http://localhost:1200/hub/github/trending/daily/python"
    )
    with pytest.raises(ValueError):
        catalog.compose(channel, {"language": "python"}, "https://rsshub.app")
    assert catalog.compose(
        {"path": "/news/:keyword"}, {"keyword": "AI/新闻?x=1"}, "https://rsshub.app"
    ).endswith("AI%2F%E6%96%B0%E9%97%BB%3Fx%3D1")
    assert catalog.compose(
        {
            "path": "/npm/package/:name{(@[a-z0-9-~][a-z0-9-._~]*/)?[a-z0-9-~][a-z0-9-._~]*}"
        },
        {"name": "@scope/pkg"},
        "https://rsshub.app",
    ).endswith("/@scope/pkg")
    assert catalog.compose(
        {"path": "/microsoft/product/*"},
        {"wildcard": "windows/server"},
        "https://rsshub.app",
    ).endswith("/windows/server")


@pytest.mark.parametrize(
    "path",
    [
        "//evil.example/path",
        "/../secrets",
        "/%2e%2e/secrets",
        "/%252e%252e/secrets",
        "/foo\\bar",
        "/test\nHeader",
    ],
)
def test_route_paths_cannot_escape_instance(path):
    with pytest.raises(ValueError):
        catalog.validate_path(path)


def test_custom_channel_and_instance_survive_restart(tmp_path):
    store = ReadingStore(tmp_path)
    store.save_settings(
        {"instance_url": "http://localhost:1200", "allow_private": True}
    )
    store.save_channel(
        {
            "id": "custom-one",
            "site": "我的频道",
            "name": "My channel",
            "path": "/github/issue/:user/:repo",
            "categories": ["custom"],
        }
    )
    reopened = ReadingStore(tmp_path)
    assert reopened.settings()["allow_private"] is True
    assert (
        catalog.list_channels(reopened, category="custom")["items"][0]["fields"][0][
            "name"
        ]
        == "user"
    )


@pytest.mark.asyncio
async def test_private_instance_requires_explicit_grant(monkeypatch):
    async def endpoint(*args, **kwargs):
        return ResolvedMCPEndpoint(args[0], "localhost", 1200, "127.0.0.1")

    monkeypatch.setattr(rsshub, "resolve_mcp_endpoint", endpoint)
    with pytest.raises(ValueError, match="允许访问"):
        await rsshub.request_feed(
            "http://localhost:1200/github/trending", "http://localhost:1200", False
        )
    with pytest.raises(ValueError, match="离开"):
        await rsshub.request_feed(
            "https://evil.example/a", "http://localhost:1200", True
        )


def test_channels_api_preview_and_subscribe_use_exact_configured_instance(
    tmp_path, monkeypatch
):
    from lantai.app.routers import reading

    async def workspace(_):
        return SimpleNamespace(workspace_dir=tmp_path, agent_id=None)

    monkeypatch.setattr(reading, "get_agent_for_request", workspace)
    calls = []

    async def request_feed(url, base, allow_private):
        calls.append((url, base, allow_private))
        return PublicWebDocument(
            url,
            "application/rss+xml",
            b"<rss><channel><title>Test feed</title><item><title>News</title><link>http://example.com/article</link></item></channel></rss>",
        )

    monkeypatch.setattr(rsshub, "request_feed", request_feed)
    app = FastAPI()
    app.include_router(reading.router)
    with TestClient(app) as client:
        assert (
            client.put(
                "/reading/rsshub",
                json={"instance_url": "http://localhost:1200", "allow_private": True},
            ).status_code
            == 200
        )
        item = client.post(
            "/reading/channels/custom",
            json={"name": "Mine", "path": "/github/issue/:user/:repo"},
        ).json()
        payload = {"parameters": {"user": "DIYgod", "repo": "RSSHub"}, "folder": "技术"}
        preview = client.post(f"/reading/channels/{item['id']}/preview", json=payload)
        assert (
            preview.json()["url"] == "http://localhost:1200/github/issue/DIYgod/RSSHub"
        )
        assert calls == []
        response = client.post(
            f"/reading/channels/{item['id']}/subscribe", json=payload
        )
        assert response.status_code == 200, response.text
        assert calls == [(preview.json()["url"], "http://localhost:1200", True)]
        assert client.get("/reading/entries").json()["total"] == 1
        feeds = client.get("/reading/feeds").json()
        assert (
            feeds[0]["instance_url"] == "http://localhost:1200"
            and feeds[0]["allow_private"]
        )
        client.post(f"/reading/feeds/{feeds[0]['id']}/refresh")
        assert len(calls) == 2


def test_package_specs_include_catalog_data():
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    assert '"app/reading/data/*"' in (root / "pyproject.toml").read_text()
    assert "src/lantai/app/reading/data" in (root / "lantai-server.spec").read_text()
