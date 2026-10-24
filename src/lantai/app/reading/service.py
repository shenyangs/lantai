"""Bounded feed parsing and public-page reading using Lantai's transport."""

from __future__ import annotations

import asyncio
import hashlib
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

from ...agents.tools.web import (
    _request_public_document,
    _plain_text,
    _readable_document,
    _assess_web_content,
)
from .store import ReadingStore, normalize_url


def parse_feed(body: bytes, url: str):
    if len(body) > 2_097_152:
        raise ValueError("订阅源超过 2 MB 限制")
    # Reject declarations before parsing, including UTF-16 encoded entities.
    probe = body.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in probe or b"<!ENTITY" in probe:
        raise ValueError("订阅源包含不支持的 XML 声明")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ValueError("此地址没有返回有效的 RSS/Atom 订阅源") from exc
    local = lambda node: node.tag.rsplit("}", 1)[-1]
    if local(root) not in {"rss", "feed", "RDF"}:
        raise ValueError("请提供 RSS/Atom 地址，而非普通网页地址")

    def child_text(node, names):
        return next(
            (
                "".join(child.itertext()).strip()
                for child in node
                if local(child) in names
            ),
            "",
        )

    channel = next((node for node in root if local(node) == "channel"), root)
    title = _plain_text(child_text(channel, {"title"})) or url
    result = []
    for node in root.iter():
        if local(node) not in {"item", "entry"}:
            continue
        link = next(
            (
                child.get("href", "") or (child.text or "")
                for child in node
                if local(child) == "link"
                and child.get("rel", "alternate") == "alternate"
            ),
            "",
        )
        link_kind = "article" if link.strip() else "feed"
        try:
            link = (
                normalize_url(urljoin(url, link.strip()), article=True)
                if link.strip()
                else url
            )
        except ValueError:
            continue
        content = child_text(node, {"encoded", "content"})
        summary = child_text(node, {"description", "summary"})
        text = _plain_text(content or summary)
        body_kind = "feed" if content else ("summary" if text else "none")
        if len(text) > 100000:
            text = (
                text[:99950]
                + "\n\n[订阅内容超过保存上限，当前为截取摘要，请查看原文。]"
            )
            body_kind = "summary"
        item_title = _plain_text(child_text(node, {"title"})) or link
        published = child_text(node, {"pubDate", "published", "updated", "date"})
        guid = child_text(node, {"guid", "id"}) or (item_title + "\n" + published)
        result.append(
            {
                "url": link,
                "title": item_title,
                "link_kind": link_kind,
                "source_feed": url,
                **(
                    {
                        "source_key": "feed:"
                        + hashlib.sha256((url + "\n" + guid).encode()).hexdigest()
                    }
                    if link_kind == "feed"
                    else {}
                ),
                "body": text,
                "body_kind": body_kind,
                "published": published,
            }
        )
        if len(result) >= 500:
            break
    return title, result


def parse_local_feed(data, feed_url=""):
    if not isinstance(data, dict) or not isinstance(data.get("item"), list):
        raise ValueError("本机频道未返回有效文章列表")
    result = []
    for item in data["item"][:500]:
        if not isinstance(item, dict):
            continue
        raw_link = str(item.get("link") or "")
        link_kind = "article" if raw_link else "feed"
        try:
            url = (
                normalize_url(raw_link, article=True)
                if raw_link
                else normalize_url(str(data.get("link") or feed_url), instance=True)
            )
        except ValueError:
            continue
        body = _plain_text(str(item.get("description") or ""))
        title = _plain_text(str(item.get("title") or url))
        published = str(item.get("pubDate") or item.get("updated") or "")
        guid = str(item.get("guid") or item.get("id") or title + "\n" + published)
        clipped = len(body) > 100000
        result.append(
            {
                "url": url,
                "title": title,
                "link_kind": link_kind,
                "source_feed": feed_url,
                **(
                    {
                        "source_key": "feed:"
                        + hashlib.sha256((feed_url + "\n" + guid).encode()).hexdigest()
                    }
                    if link_kind == "feed"
                    else {}
                ),
                "body": body[:99950] + "\n\n[内容较长，当前为截取摘要，请查看原文。]"
                if clipped
                else body,
                "body_kind": "summary" if clipped else ("feed" if body else "none"),
                "published": published,
            }
        )
    if data["item"] and not result:
        raise ValueError("频道返回了数据，但没有可保存的文章链接")
    return str(data.get("title") or "RSSHub 频道"), result


async def read_feed(url, instance_url="", allow_private=False, local=None):
    if instance_url == "managed":
        if local is None:
            raise ValueError("本机阅读服务未连接")
        return parse_local_feed(
            await local.request("/" + url.removeprefix("rsshub://")), url
        )
    url = normalize_url(url, instance=bool(instance_url))
    if instance_url:
        from .rsshub import request_feed

        operation = request_feed(url, instance_url, allow_private)
    else:
        operation = _request_public_document(
            url, timeout_seconds=12, max_bytes=2_097_152
        )
    document = await asyncio.wait_for(operation, timeout=30)
    return parse_feed(document.body, document.final_url)


async def subscribe(
    store: ReadingStore, url, folder, instance_url="", allow_private=False, local=None
):
    title, entries = await read_feed(url, instance_url, allow_private, local)
    feed = await asyncio.to_thread(
        store.subscribe, url, title, folder, instance_url, allow_private
    )
    await asyncio.to_thread(store.ingest, entries, feed["id"])
    await asyncio.to_thread(store.finish_refresh, feed["id"])
    return {
        **await asyncio.to_thread(store.feed, feed["id"]),
        "entry_count": len(entries),
    }


async def refresh(store: ReadingStore, feed_id, local=None):
    feed = await asyncio.to_thread(store.feed, feed_id)
    if feed["paused"]:
        return {"status": "paused", "count": 0}
    try:
        _, entries = await read_feed(
            feed["url"],
            feed.get("instance_url", ""),
            feed.get("allow_private", False),
            local,
        )
        ids = await asyncio.to_thread(store.ingest, entries, feed_id)
    except Exception:
        await asyncio.to_thread(
            store.finish_refresh,
            feed_id,
            "更新失败，请检查订阅地址或稍后重试。已有文章仍可阅读。",
        )
        raise
    await asyncio.to_thread(store.finish_refresh, feed_id)
    return {"status": "ok", "count": len(ids)}


async def fetch_article(store: ReadingStore, entry_id):
    entry = await asyncio.to_thread(store.entry, entry_id)
    if entry.get("link_kind") == "feed":
        raise ValueError("此条内容由订阅频道提供，没有单独原文地址；已保存频道正文")
    # Direct transport only: reading never silently forwards URLs to a cloud reader.
    document = await asyncio.wait_for(
        _request_public_document(entry["url"], timeout_seconds=12, max_bytes=2_097_152),
        timeout=30,
    )
    title, body = _readable_document(document)
    assessment = _assess_web_content(
        original_url=entry["url"], final_url=document.final_url, title=title, text=body
    )
    if not assessment.can_cite_as_page_body:
        raise ValueError("未能提取可靠正文，已保留原有内容，请打开原文阅读")
    if len(body) > 100000:
        raise ValueError("正文超过 10 万字符，已保留原有内容，请打开原文阅读")
    return await asyncio.to_thread(store.save_body, entry_id, title, body)


def export_markdown(entry):
    from datetime import datetime, timezone

    if not entry["body"]:
        raise ValueError("尚未获取正文，请先读取全文")
    kind = {"page": "网页正文", "feed": "订阅源内容", "summary": "订阅源摘要"}.get(
        entry["body_kind"], "内容"
    )
    fetched = datetime.fromtimestamp(
        entry["fetched_at"] or entry["created_at"], timezone.utc
    ).isoformat()
    link_label = (
        "来源频道（未提供单篇链接）" if entry.get("link_kind") == "feed" else "来源原文"
    )
    return (
        f"# {entry['title']}\n\n{link_label}：{entry['url']}\n\n订阅源：{entry.get('source_feed') or '未记录额外订阅源'}\n\n"
        f"内容范围：{kind}\n\n发布时间：{entry['published'] or '未知'}\n\n"
        f"抓取时间：{fetched}\n\n---\n\n{entry['body']}\n"
    )


def import_to_knowledge(workspace, entry, agent_id):
    from ..wiki.core import (
        import_wiki_source_upload,
        update_wiki_source,
        WikiSourceUpdateRequest,
    )

    result = import_wiki_source_upload(
        workspace,
        f"reading-{entry['id']}.md",
        export_markdown(entry).encode("utf-8"),
        agent_id,
    )
    for source in result["imported"]:
        # Reuse the existing metadata API; a reading title is not a filename.
        updated = update_wiki_source(
            workspace,
            source["path"],
            WikiSourceUpdateRequest(display_title=entry["title"][:160]),
        )
        source.update(updated.model_dump())
    return result
