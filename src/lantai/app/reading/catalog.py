"""Inert RSSHub metadata and parameterized channel URLs; never executes routes."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from .store import normalize_url

PARAMETER = re.compile(r":([A-Za-z_][A-Za-z_0-9]*)(\{[^}]*\})?(\?)?|(?P<wildcard>\*)")


@lru_cache(maxsize=1)
def snapshot():
    return json.loads(
        (Path(__file__).parent / "data" / "rsshub-catalog.json").read_text()
    )


def parameter_schema(channel):
    metadata = channel.get("parameters", {})
    result = []
    for match in PARAMETER.finditer(channel["path"]):
        name = match.group(1) or "wildcard"
        raw = metadata.get(name, {})
        raw = {"description": raw} if isinstance(raw, str) else raw
        result.append(
            {
                "name": name,
                "optional": bool(match.group(3)) or bool(match.group("wildcard")),
                "description": str(raw.get("description", "")),
                "default": str(raw.get("default", "")),
                "options": raw.get("options", []),
            }
        )
    return result


def channel_view(channel):
    return {**channel, "fields": parameter_schema(channel)}


def list_channels(store, q="", category="", offset=0, limit=50):
    data = snapshot()
    all_channels = [*store.custom_channels(), *data["channels"]]
    featured = ("/weibo/search/hot", "/github/trending", "/cls/telegraph")
    all_channels.sort(
        key=lambda item: (
            0
            if item["id"].startswith("custom-")
            else next(
                (
                    index + 1
                    for index, path in enumerate(featured)
                    if item["path"].startswith(path)
                ),
                10,
            ),
            item.get("site", ""),
            item["path"],
        )
    )
    categories = sorted(
        {cat for channel in all_channels for cat in channel.get("categories", [])}
    )
    words = q.casefold().split()
    filtered = [
        channel
        for channel in all_channels
        if (not category or category in channel.get("categories", []))
        and all(
            word
            in " ".join(
                str(channel.get(key, ""))
                for key in ("site", "name", "namespace", "path", "description")
            ).casefold()
            for word in words
        )
    ]
    return {
        "items": [channel_view(item) for item in filtered[offset : offset + limit]],
        "total": len(filtered),
        "catalog_total": len(all_channels),
        "categories": categories,
        "snapshot": {key: value for key, value in data.items() if key != "channels"},
    }


def get_channel(store, channel_id):
    channel = next(
        (
            item
            for item in [*store.custom_channels(), *snapshot()["channels"]]
            if item["id"] == channel_id
        ),
        None,
    )
    if channel is None:
        raise KeyError(channel_id)
    return channel


def instance_url(value):
    if urlsplit(value).scheme not in {"http", "https"}:
        raise ValueError("自建实例地址必须使用 http 或 https")
    normalized = normalize_url(value, instance=True).rstrip("/")
    if urlsplit(normalized).query:
        raise ValueError("实例地址不应包含查询参数，请将参数放在频道路由中")
    return normalized


def allowed_config_keys():
    keys = {"PROXY_URI"}
    for channel in snapshot()["channels"]:
        configs = channel.get("features", {}).get("requireConfig")
        if isinstance(configs, list):
            keys.update(
                config["name"]
                for config in configs
                if re.fullmatch(r"[A-Z][A-Z0-9_]+", config.get("name", ""))
            )
    return keys - {
        "HOME",
        "PATH",
        "NODE_OPTIONS",
        "NODE_PATH",
        "ELECTRON_RUN_AS_NODE",
        "NODE_TLS_REJECT_UNAUTHORIZED",
        "CHROMIUM_EXECUTABLE_PATH",
        "PLAYWRIGHT_WS_ENDPOINT",
        "PLAYWRIGHT_CDP_ENDPOINT",
        "PUPPETEER_WS_ENDPOINT",
        "REMOTE_CONFIG",
        "REDIS_URL",
    }


def validate_path(path, *, template=False):
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "\\" in path
        or any(ord(c) < 32 for c in path)
    ):
        raise ValueError("频道路由必须是以 / 开头的相对路径")
    decoded = path
    for _ in range(3):
        decoded = unquote(decoded)
    if any(segment in {".", ".."} for segment in decoded.split("?", 1)[0].split("/")):
        raise ValueError("频道路由不允许包含路径跳转")
    if not template and (
        PARAMETER.search(path.split("?", 1)[0]) or "{" in path or "}" in path
    ):
        raise ValueError("请填写频道所需参数，或编辑为完整路由")
    return path


def compose(channel, params, base):
    missing = []

    def replace(match):
        name = match.group(1) or "wildcard"
        value = str(params.get(name, "")).strip()
        optional = bool(match.group(3)) or bool(match.group("wildcard"))
        if not value and not optional:
            missing.append(name)
        if any(piece in {".", ".."} for piece in unquote(value).split("/")):
            raise ValueError("参数不允许包含路径跳转")
        wildcard = match.group(2) or match.group("wildcard")
        return quote(value, safe="/@" if wildcard else "")

    path = PARAMETER.sub(replace, channel["path"])
    if missing:
        raise ValueError("请填写必填参数：" + "、".join(missing))
    if "//" in path:
        # Removing an optional middle parameter would change the meaning of later values.
        if path.rstrip("/").find("//") >= 0:
            raise ValueError("后续参数已有值，请补齐前面的可选参数")
    path = path.rstrip("/") or "/"
    return instance_url(base) + validate_path(path)
