from concurrent.futures import ThreadPoolExecutor

import pytest

from lantai.app.reading.store import ReadingStore, normalize_url
from lantai.app.reading.service import parse_feed, export_markdown


def test_refresh_deduplicates_across_feeds_without_resetting_state(tmp_path):
    store = ReadingStore(tmp_path)
    first = store.subscribe("https://example.com/feed", "First")
    second = store.subscribe("https://example.org/feed", "Second")
    item = {
        "url": "https://example.com/a#section",
        "title": "Article",
        "body": "Summary",
        "body_kind": "summary",
    }
    entry_id = store.ingest([item], first["id"])[0]
    store.update_entry(entry_id, is_read=True, starred=True, tags=["研究", "研究"])
    store.save_body(entry_id, "Full article", "Saved full content")
    store.ingest([item], second["id"])
    store.ingest([item], first["id"])
    reopened = ReadingStore(tmp_path)
    assert reopened.entries()["total"] == 1
    entry = reopened.entry(entry_id)
    assert entry["body"] == "Saved full content"
    assert entry["is_read"] and entry["starred"]
    assert entry["tags"] == ["研究"]
    store.unsubscribe(first["id"])
    assert store.entry(entry_id)["starred"]
    assert store.entries(feed_id=second["id"])["total"] == 1
    assert store.entries(view="unread")["total"] == 0
    assert store.entries(q="研究", view="starred")["total"] == 1
    assert store.entries(q="' OR 1=1 --")["total"] == 0


def test_concurrent_ingest_is_idempotent_and_workspace_isolated(tmp_path):
    store = ReadingStore(tmp_path / "first")

    def add(_):
        return store.ingest([{"url": "https://example.com/a", "title": "A"}])[0]

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert len(set(pool.map(add, range(12)))) == 1
    assert ReadingStore(tmp_path / "second").entries()["total"] == 0
    with pytest.raises(KeyError):
        store.ingest([{"url": "https://example.com/b", "title": "B"}], "deleted-feed")
    assert store.entries()["total"] == 1


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://user:pass@example.com",
        "http://example.com",
        "https://example.com:8080",
    ],
)
def test_invalid_urls(url):
    with pytest.raises(ValueError):
        normalize_url(url)


def test_atom_and_rss_preserve_scope_and_strip_scripts():
    title, items = parse_feed(
        b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>News</title>
    <entry><title>A</title><link rel="self" href="https://example.com/api"/>
    <link href="/article"/><summary>&lt;p&gt;A summary&lt;/p&gt;&lt;script&gt;bad()&lt;/script&gt;</summary></entry>
    <entry><title>Unsafe</title><link href="javascript:alert(1)"/></entry></feed>""",
        "https://example.com/feed",
    )
    assert title == "News" and len(items) == 1
    assert items[0]["url"] == "https://example.com/article"
    assert items[0]["body_kind"] == "summary"
    assert "bad()" not in items[0]["body"]
    _, rss = parse_feed(
        b"""<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><title>RSS</title>
    <item><title>B</title><link>https://example.com/b</link><content:encoded><![CDATA[<p>Full content</p>]]></content:encoded></item></channel></rss>""",
        "https://example.com/rss",
    )
    assert rss[0]["body_kind"] == "feed"


@pytest.mark.parametrize(
    "body",
    [
        b"<html>Not a feed</html>",
        b"<rss",
        b'<!DOCTYPE rss [<!ENTITY x "test">]><rss/>',
        ("<!DOCTYPE rss><rss/>").encode("utf-16"),
    ],
)
def test_rejects_nonfeeds_and_entity_declarations(body):
    with pytest.raises(ValueError):
        parse_feed(body, "https://example.com/feed")


def test_export_contains_provenance_and_no_implicit_knowledge(tmp_path):
    store = ReadingStore(tmp_path)
    entry_id = store.ingest(
        [
            {
                "url": "https://example.com/a",
                "title": "A",
                "body": "Summary",
                "body_kind": "summary",
            }
        ],
        starred=True,
    )[0]
    text = export_markdown(store.entry(entry_id))
    assert "https://example.com/a" in text and "订阅源摘要" in text
    assert not (tmp_path / "wiki").exists()


def test_linkless_feed_entries_keep_distinct_identity_and_provenance(tmp_path):
    from lantai.app.reading.service import parse_local_feed

    data = {
        "title": "电报",
        "link": "https://example.com/telegraph",
        "item": [
            {
                "title": "First dispatch",
                "description": "First content",
                "pubDate": "2026-09-08T01:00:00Z",
            },
            {
                "title": "Second dispatch",
                "description": "Second content",
                "pubDate": "2026-09-08T02:00:00Z",
            },
        ],
    }
    _, items = parse_local_feed(data, "rsshub://cls/telegraph")
    store = ReadingStore(tmp_path)
    feed = store.subscribe("rsshub://cls/telegraph", "电报", instance_url="managed")
    ids = store.ingest(items, feed["id"])
    store.ingest(items, feed["id"])
    assert store.entries()["total"] == 2
    assert len(set(ids)) == 2
    entry = store.entry(ids[0])
    assert entry["link_kind"] == "feed"
    assert entry["url"] == "https://example.com/telegraph"
    assert "未提供单篇链接" in export_markdown(entry)
    assert "rsshub://cls/telegraph" in export_markdown(entry)


def test_v1_migration_preserves_content_states_and_subscription_membership(tmp_path):
    import sqlite3

    root = tmp_path / "reading"
    root.mkdir()
    with sqlite3.connect(root / "library.sqlite3") as db:
        db.executescript("""
        CREATE TABLE feeds(id TEXT PRIMARY KEY,url TEXT NOT NULL UNIQUE,title TEXT NOT NULL,folder TEXT NOT NULL DEFAULT '',paused INTEGER NOT NULL DEFAULT 0,checked_at REAL,error TEXT NOT NULL DEFAULT '');
        CREATE TABLE entries(id TEXT PRIMARY KEY,url TEXT NOT NULL UNIQUE,title TEXT NOT NULL,body TEXT NOT NULL DEFAULT '',body_kind TEXT NOT NULL DEFAULT 'none',published TEXT NOT NULL DEFAULT '',created_at REAL NOT NULL,fetched_at REAL,is_read INTEGER NOT NULL DEFAULT 0,starred INTEGER NOT NULL DEFAULT 0,tags TEXT NOT NULL DEFAULT '[]');
        CREATE TABLE feed_entries(feed_id TEXT REFERENCES feeds(id) ON DELETE CASCADE,entry_id TEXT REFERENCES entries(id) ON DELETE CASCADE,PRIMARY KEY(feed_id,entry_id));
        INSERT INTO feeds(id,url,title) VALUES('feed','https://example.com/rss','Original');
        INSERT INTO entries(id,url,title,body,created_at,is_read,starred,tags) VALUES('entry','https://example.com/a','Title','Do not lose',1,1,1,'["saved"]');
        INSERT INTO feed_entries VALUES('feed','entry');
        """)
    store = ReadingStore(tmp_path)
    assert store.entry("entry")["body"] == "Do not lose"
    assert store.entry("entry")["starred"] and store.entry("entry")["is_read"]
    assert store.entry("entry")["tags"] == ["saved"]
    assert store.entry("entry")["source_feed"] == "https://example.com/rss"
    assert store.entries(feed_id="feed")["total"] == 1
    with store.connect() as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3


def test_reading_delete_is_recoverable_and_purge_requires_archived_state(tmp_path):
    store = ReadingStore(tmp_path)
    first, second = store.ingest(
        [
            {"url": "https://example.com/first", "title": "First"},
            {"url": "https://example.com/second", "title": "Second"},
        ]
    )
    store.update_entry(first, starred=True, is_read=True, tags=["keep"])

    assert store.archive_entries([first, "missing"])["archived"] == 1
    assert store.entries()["total"] == 1
    assert store.entries(view="trash")["total"] == 1
    restored = store.restore_entry(first)
    assert restored["starred"] and restored["is_read"] and restored["tags"] == ["keep"]
    with pytest.raises(KeyError):
        store.purge_entry(first)

    store.archive_entries([first, second])
    assert store.purge_entry(first)["deleted"] is True
    with pytest.raises(KeyError):
        store.entry(first)
    assert store.entries(view="trash")["total"] == 1


@pytest.mark.asyncio
async def test_failed_fetch_preserves_previous_content(tmp_path, monkeypatch):
    from lantai.app.reading import service
    from lantai.agents.tools.web import PublicWebDocument

    store = ReadingStore(tmp_path)
    entry_id = store.ingest(
        [
            {
                "url": "https://example.com/a",
                "title": "A",
                "body": "Keep this",
                "body_kind": "feed",
            }
        ]
    )[0]

    async def fetch(*args, **kwargs):
        return PublicWebDocument(
            "https://example.com/a",
            "text/html",
            b"<html><title>Access denied</title><body>Access denied</body></html>",
        )

    monkeypatch.setattr(service, "_request_public_document", fetch)
    with pytest.raises(ValueError):
        await service.fetch_article(store, entry_id)
    assert store.entry(entry_id)["body"] == "Keep this"


@pytest.mark.asyncio
async def test_api_to_real_knowledge_ingestion_round_trip(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from lantai.app.routers import reading

    async def workspace(_):
        return SimpleNamespace(workspace_dir=tmp_path, agent_id=None)

    monkeypatch.setattr(reading, "get_agent_for_request", workspace)
    app = FastAPI()
    app.include_router(reading.router)
    with TestClient(app) as client:
        response = client.post(
            "/reading/entries", json={"url": "https://example.com/article"}
        )
        assert response.status_code == 200
        item = response.json()
        assert client.get("/reading/entries/missing").status_code == 404
        assert (
            client.post(f"/reading/entries/{item['id']}/knowledge").status_code == 400
        )
        ReadingStore(tmp_path).save_body(
            item["id"], "Research", "A preserved article about reading and research."
        )
        result = client.post(f"/reading/entries/{item['id']}/knowledge")
        assert result.status_code == 200, result.text
        source = result.json()["imported"][0]
        assert source["display_title"] == "Research"
        assert (tmp_path / source["path"]).exists()
        assert "https://example.com/article" in (tmp_path / source["path"]).read_text()
        duplicate = client.post(f"/reading/entries/{item['id']}/knowledge").json()
        assert duplicate["imported"] == []
        assert duplicate["skipped"][0]["path"] == source["path"]
        assert (
            client.patch(
                f"/reading/entries/{item['id']}", json={"tags": ["x" * 81]}
            ).status_code
            == 400
        )
