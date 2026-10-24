"""Workspace-scoped reading API; no implicit model calls or memory writes."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Query
from pydantic import BaseModel, Field

from ..agent_context import get_agent_for_request
from ..reading import service, catalog
from ..reading.store import ReadingStore, normalize_url
from ..reading.managed import ManagedRSSHub, ReadingRuntimeError


@asynccontextmanager
async def reading_lifespan(app):
    try:
        yield
    finally:
        managers = getattr(app.state, "reading_services", {})
        await asyncio.gather(
            *(manager.close() for manager in managers.values()), return_exceptions=True
        )


router = APIRouter(prefix="/reading", tags=["reading"], lifespan=reading_lifespan)


class Subscription(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    folder: str = Field(default="", max_length=80)


class FeedPatch(BaseModel):
    folder: str | None = Field(default=None, max_length=80)
    paused: bool | None = None


class EntryPatch(BaseModel):
    is_read: bool | None = None
    starred: bool | None = None
    tags: list[str] | None = Field(default=None, max_length=20)


class EntryArchiveRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=100)


class RSSHubSettings(BaseModel):
    mode: Literal["local", "external"] = "external"
    instance_url: str = Field(min_length=1, max_length=2048)
    allow_private: bool = False


class CustomChannel(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    path: str = Field(min_length=1, max_length=2048)
    id: str | None = Field(default=None, max_length=80)


class ChannelSubscription(BaseModel):
    parameters: dict[str, str] = Field(default_factory=dict, max_length=30)
    folder: str = Field(default="", max_length=80)
    path_override: str = Field(default="", max_length=2048)


class LocalServiceAction(BaseModel):
    action: Literal["prepare", "browser", "start", "stop"] = "prepare"


class LocalConfiguration(BaseModel):
    values: dict[str, str] = Field(default_factory=dict, max_length=30)


async def context(request):
    workspace = await get_agent_for_request(request)
    store = await asyncio.to_thread(ReadingStore, workspace.workspace_dir)
    return workspace, store


async def local_manager(request, workspace, store):
    managers = getattr(request.app.state, "reading_services", None)
    if managers is None:
        managers = {}
        request.app.state.reading_services = managers
    lock = getattr(request.app.state, "reading_services_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        request.app.state.reading_services_lock = lock
    key = str(workspace.workspace_dir)
    async with lock:
        if key not in managers:
            manager = ManagedRSSHub(workspace.workspace_dir)
            manager.config = await asyncio.to_thread(store.local_config)
            managers[key] = manager
        return managers[key]


async def network_call(operation):
    try:
        return await operation
    except KeyError as exc:
        raise HTTPException(404, "文章或订阅源已不存在，请刷新列表") from exc
    except ReadingRuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            502, "暂时无法读取该地址，请稍后重试。已有内容保持不变。"
        ) from exc


@router.get("/channels")
async def channels(
    request: Request,
    q: str = Query("", max_length=200),
    category: str = "",
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    _, store = await context(request)
    return await asyncio.to_thread(
        catalog.list_channels, store, q, category, offset, limit
    )


@router.get("/rsshub")
async def rsshub_settings(request: Request):
    _, store = await context(request)
    return await asyncio.to_thread(store.settings)


@router.put("/rsshub")
async def save_rsshub_settings(body: RSSHubSettings, request: Request):
    _, store = await context(request)
    try:
        values = {
            "mode": body.mode,
            "instance_url": catalog.instance_url(body.instance_url),
            "allow_private": body.allow_private,
        }
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return await asyncio.to_thread(store.save_settings, values)


@router.get("/local")
async def local_status(request: Request):
    workspace, store = await context(request)
    manager = await local_manager(request, workspace, store)
    return {
        **manager.snapshot(),
        "configured_keys": await asyncio.to_thread(store.local_config_keys),
    }


@router.post("/local")
async def local_action(body: LocalServiceAction, request: Request):
    workspace, store = await context(request)
    manager = await local_manager(request, workspace, store)
    if body.action in {"prepare", "browser"}:
        return {
            **manager.prepare(browser=body.action == "browser"),
            "configured_keys": await asyncio.to_thread(store.local_config_keys),
        }
    if body.action == "stop":
        await manager.stop()
    else:
        await network_call(manager.start())
    return {
        **manager.snapshot(),
        "configured_keys": await asyncio.to_thread(store.local_config_keys),
    }


@router.put("/local/config")
async def local_configuration(body: LocalConfiguration, request: Request):
    if set(body.values) - catalog.allowed_config_keys() or any(
        len(value) > 20000 for value in body.values.values()
    ):
        raise HTTPException(400, "请只填写频道列出的配置项，单项最多 2 万字符")
    workspace, store = await context(request)
    keys = await asyncio.to_thread(store.save_local_config, body.values)
    manager = await local_manager(request, workspace, store)
    await manager.configure(await asyncio.to_thread(store.local_config))
    return {"configured_keys": keys}


@router.post("/channels/custom")
async def save_custom_channel(body: CustomChannel, request: Request):
    from uuid import uuid4

    _, store = await context(request)
    try:
        catalog.validate_path(body.path, template=True)
        if body.id and not body.id.startswith("custom-"):
            raise ValueError("内置频道不能被覆盖，请另存为自定义频道")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    item = {
        "id": body.id or "custom-" + uuid4().hex,
        "namespace": "custom",
        "site": "我的频道",
        "name": body.name,
        "path": body.path,
        "example": "",
        "description": "用户自定义频道",
        "parameters": {},
        "features": {},
        "categories": ["custom"],
        "docs": "",
    }
    await asyncio.to_thread(store.save_channel, item)
    return catalog.channel_view(item)


def channel_url(store, channel_id, body):
    settings = store.settings()
    item = catalog.get_channel(store, channel_id)
    if any(len(value) > 1000 for value in body.parameters.values()):
        raise ValueError("频道参数过长")
    base = (
        "https://local.invalid"
        if settings["mode"] == "local"
        else settings["instance_url"]
    )
    url = (
        catalog.instance_url(base) + catalog.validate_path(body.path_override)
        if body.path_override
        else catalog.compose(item, body.parameters, base)
    )
    if settings["mode"] == "local":
        url = "rsshub://" + url.removeprefix("https://local.invalid/")
    return url, settings


@router.post("/channels/{channel_id}/preview")
async def preview_channel(channel_id: str, body: ChannelSubscription, request: Request):
    _, store = await context(request)
    url, settings = await network_call(
        asyncio.to_thread(channel_url, store, channel_id, body)
    )
    return {"url": url, "instance_url": settings["instance_url"]}


@router.post("/channels/{channel_id}/subscribe")
async def subscribe_channel(
    channel_id: str, body: ChannelSubscription, request: Request
):
    workspace, store = await context(request)
    url, settings = await network_call(
        asyncio.to_thread(channel_url, store, channel_id, body)
    )
    if settings["mode"] == "local":
        manager = await local_manager(request, workspace, store)
        return await network_call(
            service.subscribe(store, url, body.folder, "managed", False, manager)
        )
    return await network_call(
        service.subscribe(
            store, url, body.folder, settings["instance_url"], settings["allow_private"]
        )
    )


@router.get("/feeds")
async def feeds(request: Request):
    _, store = await context(request)
    return await asyncio.to_thread(store.feeds)


@router.post("/feeds")
async def add_feed(body: Subscription, request: Request):
    _, store = await context(request)
    return await network_call(service.subscribe(store, body.url, body.folder))


@router.patch("/feeds/{feed_id}")
async def patch_feed(feed_id: str, body: FeedPatch, request: Request):
    _, store = await context(request)
    return await network_call(
        asyncio.to_thread(store.update_feed, feed_id, **body.model_dump())
    )


@router.delete("/feeds/{feed_id}")
async def delete_feed(feed_id: str, request: Request):
    _, store = await context(request)
    await asyncio.to_thread(store.unsubscribe, feed_id)
    return {"status": "ok"}


@router.post("/feeds/{feed_id}/refresh")
async def refresh_feed(feed_id: str, request: Request):
    workspace, store = await context(request)
    feed = await network_call(asyncio.to_thread(store.feed, feed_id))
    manager = (
        await local_manager(request, workspace, store)
        if feed.get("instance_url") == "managed"
        else None
    )
    return await network_call(service.refresh(store, feed_id, manager))


@router.get("/entries")
async def entries(
    request: Request,
    q: str = Query("", max_length=200),
    feed_id: str = "",
    view: Literal["all", "unread", "starred", "trash"] = "all",
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
):
    _, store = await context(request)
    return await asyncio.to_thread(
        store.entries, q=q, feed_id=feed_id, view=view, offset=offset, limit=limit
    )


@router.post("/entries")
async def save_link(body: Subscription, request: Request):
    _, store = await context(request)
    try:
        url = normalize_url(body.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    ids = await asyncio.to_thread(
        store.ingest, [{"url": url, "title": url}], starred=True
    )
    return await asyncio.to_thread(store.entry, ids[0])


@router.get("/entries/{entry_id}")
async def entry(entry_id: str, request: Request):
    _, store = await context(request)
    return await network_call(asyncio.to_thread(store.entry, entry_id))


@router.post("/entries/archive")
async def archive_entries(body: EntryArchiveRequest, request: Request):
    _, store = await context(request)
    return await asyncio.to_thread(store.archive_entries, body.ids)


@router.post("/entries/{entry_id}/restore")
async def restore_entry(entry_id: str, request: Request):
    _, store = await context(request)
    return await network_call(asyncio.to_thread(store.restore_entry, entry_id))


@router.delete("/entries/{entry_id}")
async def purge_entry(entry_id: str, request: Request):
    _, store = await context(request)
    return await network_call(asyncio.to_thread(store.purge_entry, entry_id))


@router.patch("/entries/{entry_id}")
async def patch_entry(entry_id: str, body: EntryPatch, request: Request):
    if body.tags and any(len(tag) > 80 for tag in body.tags):
        raise HTTPException(400, "每个标签最多 80 个字符")
    _, store = await context(request)
    return await network_call(
        asyncio.to_thread(store.update_entry, entry_id, **body.model_dump())
    )


@router.post("/entries/{entry_id}/fetch")
async def fetch_entry(entry_id: str, request: Request):
    _, store = await context(request)
    return await network_call(service.fetch_article(store, entry_id))


@router.post("/entries/{entry_id}/knowledge")
async def add_to_knowledge(entry_id: str, request: Request):
    workspace, store = await context(request)
    item = await network_call(asyncio.to_thread(store.entry, entry_id))
    try:
        service.export_markdown(item)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Existing ingestion owns parsing, deduplication, indexing and citations.
    return await asyncio.to_thread(
        service.import_to_knowledge, workspace.workspace_dir, item, workspace.agent_id
    )
