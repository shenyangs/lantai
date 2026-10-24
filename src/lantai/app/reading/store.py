"""Transactional, workspace-scoped subscriptions and reading state."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from time import time
from uuid import uuid4
from urllib.parse import urlsplit, urlunsplit


def normalize_url(value: str, *, instance: bool = False, article: bool = False) -> str:
    parts = urlsplit(value.strip())
    schemes = (
        {"http", "https", "rsshub"}
        if instance
        else ({"http", "https"} if article else {"https"})
    )
    if (
        parts.scheme not in schemes
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        raise ValueError("请输入不含登录凭据的公开 HTTPS 地址")
    if not instance and parts.port not in (
        None,
        443 if parts.scheme == "https" else 80,
    ):
        raise ValueError("阅读服务仅支持标准 HTTPS 端口")
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if (
        instance
        and parts.port
        and parts.port != (443 if parts.scheme == "https" else 80)
    ):
        host += f":{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path or "/", parts.query, ""))


class ReadingStore:
    def __init__(self, workspace: Path):
        self.path = Path(workspace) / "reading" / "library.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS feeds (
                    id TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL, folder TEXT NOT NULL DEFAULT '',
                    paused INTEGER NOT NULL DEFAULT 0,
                    checked_at REAL, error TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS entries (
                    id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE, url TEXT NOT NULL,
                    title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
                    body_kind TEXT NOT NULL DEFAULT 'none',
                    link_kind TEXT NOT NULL DEFAULT 'article', source_feed TEXT NOT NULL DEFAULT '',
                    published TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
                    fetched_at REAL, is_read INTEGER NOT NULL DEFAULT 0,
                    starred INTEGER NOT NULL DEFAULT 0, tags TEXT NOT NULL DEFAULT '[]',
                    archived_at REAL
                );
                CREATE TABLE IF NOT EXISTS feed_entries (
                    feed_id TEXT NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
                    entry_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
                    PRIMARY KEY(feed_id, entry_id)
                );
                CREATE INDEX IF NOT EXISTS feed_entries_entry ON feed_entries(entry_id);
                CREATE INDEX IF NOT EXISTS entries_created ON entries(created_at DESC);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS custom_channels (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                PRAGMA user_version = 1;
            """)
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("BEGIN IMMEDIATE")
            entry_columns = {row[1] for row in db.execute("PRAGMA table_info(entries)")}
            if "source_key" not in entry_columns:
                db.execute("""CREATE TABLE entries_v2 (
                    id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE, url TEXT NOT NULL,
                    title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', body_kind TEXT NOT NULL DEFAULT 'none',
                    link_kind TEXT NOT NULL DEFAULT 'article', source_feed TEXT NOT NULL DEFAULT '',
                    published TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, fetched_at REAL,
                    is_read INTEGER NOT NULL DEFAULT 0, starred INTEGER NOT NULL DEFAULT 0,
                    tags TEXT NOT NULL DEFAULT '[]', archived_at REAL)""")
                db.execute("""INSERT INTO entries_v2 (id,source_key,url,title,body,body_kind,published,created_at,fetched_at,is_read,starred,tags)
                    SELECT id,'url:' || url,url,title,body,body_kind,published,created_at,fetched_at,is_read,starred,tags FROM entries""")
                db.execute("DROP TABLE entries")
                db.execute("ALTER TABLE entries_v2 RENAME TO entries")
                db.execute("CREATE INDEX entries_created ON entries(created_at DESC)")
            entry_columns = {row[1] for row in db.execute("PRAGMA table_info(entries)")}
            if "archived_at" not in entry_columns:
                db.execute("ALTER TABLE entries ADD COLUMN archived_at REAL")
            columns = {row[1] for row in db.execute("PRAGMA table_info(feeds)")}
            for name, definition in (
                ("instance_url", "TEXT NOT NULL DEFAULT ''"),
                ("allow_private", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE feeds ADD COLUMN {name} {definition}")
            db.execute("PRAGMA user_version=3")
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def feeds(self):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute("""
                SELECT f.*, (SELECT count(*) FROM feed_entries fe JOIN entries e
                ON e.id=fe.entry_id WHERE fe.feed_id=f.id AND e.is_read=0 AND e.archived_at IS NULL) AS unread
                FROM feeds f ORDER BY folder, title
            """)
            ]

    def feed(self, feed_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM feeds WHERE id=?", (feed_id,)).fetchone()
            if row is None:
                raise KeyError(feed_id)
            return dict(row)

    def subscribe(self, url, title, folder="", instance_url="", allow_private=False):
        normalized = normalize_url(url, instance=bool(instance_url))
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO feeds(id,url,title,folder,instance_url,allow_private) VALUES(?,?,?,?,?,?)",
                (
                    uuid4().hex,
                    normalized,
                    title[:300],
                    folder[:80],
                    instance_url,
                    int(allow_private),
                ),
            )
            return dict(
                db.execute("SELECT * FROM feeds WHERE url=?", (normalized,)).fetchone()
            )

    def settings(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key='rsshub'").fetchone()
            return (
                {"mode": "external", **json.loads(row[0])}
                if row
                else {
                    "mode": "local",
                    "instance_url": "https://rsshub.app",
                    "allow_private": False,
                }
            )

    def save_settings(self, values):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES('rsshub',?)",
                (json.dumps(values),),
            )
        return values

    def custom_channels(self):
        with self.connect() as db:
            return [
                json.loads(row[0])
                for row in db.execute("SELECT data FROM custom_channels ORDER BY id")
            ]

    def local_config(self):
        from ...security.secret_store import decrypt

        with self.connect() as db:
            row = db.execute(
                "SELECT value FROM settings WHERE key='rsshub_config'"
            ).fetchone()
            return (
                {key: decrypt(value) for key, value in json.loads(row[0]).items()}
                if row
                else {}
            )

    def local_config_keys(self):
        with self.connect() as db:
            row = db.execute(
                "SELECT value FROM settings WHERE key='rsshub_config'"
            ).fetchone()
            return sorted(json.loads(row[0])) if row else []

    def save_local_config(self, values):
        from ...security.secret_store import encrypt

        with self.connect() as db:
            row = db.execute(
                "SELECT value FROM settings WHERE key='rsshub_config'"
            ).fetchone()
            current = json.loads(row[0]) if row else {}
            for key, value in values.items():
                if value:
                    current[key] = encrypt(value)
                else:
                    current.pop(key, None)
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES('rsshub_config',?)",
                (json.dumps(current),),
            )
        return sorted(current)

    def save_channel(self, data):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO custom_channels VALUES(?,?)",
                (data["id"], json.dumps(data, ensure_ascii=False)),
            )
        return data

    def update_feed(self, feed_id, *, folder=None, paused=None):
        self.feed(feed_id)
        with self.connect() as db:
            if folder is not None:
                db.execute(
                    "UPDATE feeds SET folder=? WHERE id=?",
                    (folder.strip()[:80], feed_id),
                )
            if paused is not None:
                db.execute(
                    "UPDATE feeds SET paused=? WHERE id=?", (int(paused), feed_id)
                )
        return self.feed(feed_id)

    def unsubscribe(self, feed_id):
        # Unsubscribing never deletes saved articles or their reading state.
        with self.connect() as db:
            db.execute("DELETE FROM feeds WHERE id=?", (feed_id,))

    def ingest(self, entries, feed_id=None, starred=False):
        ids = []
        with self.connect() as db:
            if (
                feed_id
                and not db.execute(
                    "SELECT 1 FROM feeds WHERE id=?", (feed_id,)
                ).fetchone()
            ):
                raise KeyError(feed_id)
            # Feeds conventionally arrive newest first; preserve that order for
            # newly discovered entries even when publication dates are absent.
            for entry in reversed(entries):
                link_kind = entry.get("link_kind", "article")
                url = (
                    normalize_url(
                        entry["url"], article=True, instance=link_kind == "feed"
                    )
                    if entry["url"]
                    else ""
                )
                source_key = entry.get("source_key") or "url:" + url
                db.execute(
                    """INSERT OR IGNORE INTO entries
                    (id,source_key,url,title,body,body_kind,published,created_at,starred,link_kind,source_feed)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        uuid4().hex,
                        source_key,
                        url,
                        entry["title"][:500],
                        entry.get("body", "")[:100000],
                        entry.get("body_kind", "none"),
                        entry.get("published", "")[:100],
                        time(),
                        int(starred),
                        link_kind,
                        entry.get("source_feed", ""),
                    ),
                )
                row = db.execute(
                    "SELECT id FROM entries WHERE source_key=?", (source_key,)
                ).fetchone()
                entry_id = row["id"]
                if starred:
                    db.execute("UPDATE entries SET starred=1 WHERE id=?", (entry_id,))
                if entry.get("source_feed"):
                    db.execute(
                        "UPDATE entries SET source_feed=? WHERE id=? AND source_feed=''",
                        (entry["source_feed"], entry_id),
                    )
                if feed_id:
                    db.execute(
                        "INSERT OR IGNORE INTO feed_entries VALUES(?,?)",
                        (feed_id, entry_id),
                    )
                ids.append(entry_id)
        return ids

    def finish_refresh(self, feed_id, error=""):
        with self.connect() as db:
            db.execute(
                "UPDATE feeds SET checked_at=?,error=? WHERE id=?",
                (time(), error[:300], feed_id),
            )

    @staticmethod
    def decode(row):
        value = dict(row)
        value["tags"] = json.loads(value["tags"])
        value["is_read"] = bool(value["is_read"])
        value["starred"] = bool(value["starred"])
        return value

    def entry(self, entry_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
            if row is None:
                raise KeyError(entry_id)
            item = self.decode(row)
            if not item["source_feed"]:
                source = db.execute(
                    "SELECT f.url FROM feeds f JOIN feed_entries fe ON f.id=fe.feed_id WHERE fe.entry_id=? ORDER BY f.id LIMIT 1",
                    (entry_id,),
                ).fetchone()
                if source:
                    item["source_feed"] = source[0]
            return item

    def entries(self, *, q="", feed_id="", view="all", offset=0, limit=100):
        where, args = (
            ["archived_at IS NOT NULL" if view == "trash" else "archived_at IS NULL"],
            [],
        )
        if feed_id:
            where.append(
                "EXISTS(SELECT 1 FROM feed_entries fe WHERE fe.entry_id=e.id AND fe.feed_id=?)"
            )
            args.append(feed_id)
        if view == "unread":
            where.append("is_read=0")
        elif view == "starred":
            where.append("starred=1")
        if q:
            where.append(
                "(instr(lower(title),lower(?))>0 OR instr(lower(body),lower(?))>0 OR instr(lower(tags),lower(?))>0)"
            )
            args.extend([q, q, q])
        clause = " AND ".join(where)
        with self.connect() as db:
            total = db.execute(
                f"SELECT count(*) FROM entries e WHERE {clause}", args
            ).fetchone()[0]
            rows = db.execute(
                f"SELECT e.*, substr(body,1,160) AS excerpt FROM entries e WHERE {clause} ORDER BY created_at DESC,id LIMIT ? OFFSET ?",
                [*args, limit, offset],
            )
            items = []
            for row in rows:
                item = self.decode(row)
                item.pop("body")
                items.append(item)
            return {"items": items, "total": total}

    def archive_entries(self, entry_ids):
        ids = list(dict.fromkeys(str(value) for value in entry_ids if value))
        if not ids:
            return {"archived": 0}
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as db:
            existing = db.execute(
                f"SELECT count(*) FROM entries WHERE id IN ({placeholders}) AND archived_at IS NULL",
                ids,
            ).fetchone()[0]
            db.execute(
                f"UPDATE entries SET archived_at=? WHERE id IN ({placeholders}) AND archived_at IS NULL",
                [time(), *ids],
            )
        return {"archived": existing}

    def restore_entry(self, entry_id):
        with self.connect() as db:
            changed = db.execute(
                "UPDATE entries SET archived_at=NULL WHERE id=? AND archived_at IS NOT NULL",
                (entry_id,),
            ).rowcount
        if not changed:
            raise KeyError(entry_id)
        return self.entry(entry_id)

    def purge_entry(self, entry_id):
        with self.connect() as db:
            changed = db.execute(
                "DELETE FROM entries WHERE id=? AND archived_at IS NOT NULL",
                (entry_id,),
            ).rowcount
        if not changed:
            raise KeyError(entry_id)
        return {"deleted": True, "id": entry_id}

    def update_entry(self, entry_id, *, is_read=None, starred=None, tags=None):
        self.entry(entry_id)
        with self.connect() as db:
            for key, value in (("is_read", is_read), ("starred", starred)):
                if value is not None:
                    db.execute(
                        f"UPDATE entries SET {key}=? WHERE id=?", (int(value), entry_id)
                    )
            if tags is not None:
                cleaned = list(
                    dict.fromkeys(tag.strip() for tag in tags if tag.strip())
                )
                db.execute(
                    "UPDATE entries SET tags=? WHERE id=?",
                    (json.dumps(cleaned, ensure_ascii=False), entry_id),
                )
        return self.entry(entry_id)

    def save_body(self, entry_id, title, body):
        with self.connect() as db:
            db.execute(
                "UPDATE entries SET title=?,body=?,body_kind='page',fetched_at=? WHERE id=?",
                (title[:500], body[:100000], time(), entry_id),
            )
        return self.entry(entry_id)
