# -*- coding: utf-8 -*-
"""Channel registry: built-in + custom channels from working dir."""

from __future__ import annotations

import importlib
import logging
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from ...constant import CUSTOM_CHANNELS_DIR
from .base import BaseChannel
from .plugin_manifest import ChannelPluginManifest

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_BUILTIN_SPECS: dict[str, tuple[str, str]] = {
    "imessage": (".imessage", "IMessageChannel"),
    "discord": (".discord_", "DiscordChannel"),
    "slack": (".slack", "SlackChannel"),
    "dingtalk": (".dingtalk", "DingTalkChannel"),
    "feishu": (".feishu", "FeishuChannel"),
    "qq": (".qq", "QQChannel"),
    "telegram": (".telegram", "TelegramChannel"),
    "mattermost": (".mattermost", "MattermostChannel"),
    "mqtt": (".mqtt", "MQTTChannel"),
    "console": (".console", "ConsoleChannel"),
    "matrix": (".matrix", "MatrixChannel"),
    "voice": (".voice", "VoiceChannel"),
    "sip": (".sip", "SIPChannel"),
    "wecom": (".wecom", "WecomChannel"),
    "xiaoyi": (".xiaoyi", "XiaoYiChannel"),
    "wechat": (".wechat", "WeChatChannel"),
    "onebot": (".onebot", "OneBotChannel"),
    "yuanbao": (".yuanbao", "YuanbaoChannel"),
}

# Required channels must load; failures are raised, not skipped.
_REQUIRED_CHANNEL_KEYS: frozenset[str] = frozenset({"console"})

_BUILTIN_CHANNEL_CACHE: dict[str, type[BaseChannel]] | None = None
_BUILTIN_CHANNEL_CACHE_LOCK = threading.Lock()


def _load_builtin_channels() -> dict[str, type[BaseChannel]]:
    """Load built-in channels safely.

    A single optional dependency failure should not break CLI startup.
    """
    out: dict[str, type[BaseChannel]] = {}
    for key, (module_name, class_name) in _BUILTIN_SPECS.items():
        try:
            mod = importlib.import_module(module_name, package=__package__)
            cls = getattr(mod, class_name)
            if not (
                isinstance(cls, type)
                and issubclass(cls, BaseChannel)
                and cls is not BaseChannel
            ):
                raise TypeError(
                    f"{module_name}.{class_name} is not a BaseChannel subtype",
                )
        except Exception:
            if key in _REQUIRED_CHANNEL_KEYS:
                logger.error(
                    'failed to load required built-in channel "%s"',
                    key,
                    exc_info=True,
                )
                raise
            logger.debug(
                "built-in channel unavailable: %s",
                key,
                exc_info=True,
            )
            continue
        out[key] = cls
    return out


def _get_cached_builtin_channels() -> dict[str, type[BaseChannel]]:
    """Return cached built-in channels (loaded once per process)."""
    global _BUILTIN_CHANNEL_CACHE
    with _BUILTIN_CHANNEL_CACHE_LOCK:
        if _BUILTIN_CHANNEL_CACHE is None:
            _BUILTIN_CHANNEL_CACHE = _load_builtin_channels()
        return dict(_BUILTIN_CHANNEL_CACHE)


def clear_builtin_channel_cache() -> None:
    """Reset built-in channel cache. Primarily for tests."""
    global _BUILTIN_CHANNEL_CACHE
    with _BUILTIN_CHANNEL_CACHE_LOCK:
        _BUILTIN_CHANNEL_CACHE = None


def _discover_custom_channels() -> dict[str, type[BaseChannel]]:
    """Load channel classes from CUSTOM_CHANNELS_DIR."""
    out: dict[str, type[BaseChannel]] = {}
    if not CUSTOM_CHANNELS_DIR.is_dir():
        return out

    dir_str = str(CUSTOM_CHANNELS_DIR)
    if dir_str not in sys.path:
        sys.path.insert(0, dir_str)

    for path in sorted(CUSTOM_CHANNELS_DIR.iterdir()):
        manifest = _load_plugin_manifest(path)
        if manifest is not None:
            if manifest.key in BUILTIN_CHANNEL_KEYS:
                logger.warning(
                    "custom channel manifest cannot shadow built-in key: %s",
                    manifest.key,
                )
                continue
            missing = manifest.missing_requirements()
            if missing:
                logger.warning(
                    "custom channel %s is missing dependencies: %s",
                    manifest.key,
                    ", ".join(missing),
                )
                continue
            module_name = _manifest_module_name(path, manifest)
            try:
                mod = importlib.import_module(module_name)
                obj = getattr(mod, manifest.class_name)
                if not (
                    isinstance(obj, type)
                    and issubclass(obj, BaseChannel)
                    and obj is not BaseChannel
                ):
                    raise TypeError(
                        f"{module_name}.{manifest.class_name} is not a "
                        "BaseChannel subtype",
                    )
                if getattr(obj, "channel", None) != manifest.key:
                    raise ValueError(
                        "manifest key does not match channel class key",
                    )
                obj.capabilities = manifest.capabilities.to_runtime()
                obj.channel_manifest = manifest
                out[manifest.key] = obj
            except Exception:
                logger.exception(
                    "failed to load manifest channel: %s",
                    manifest.key,
                )
            continue
        if path.is_dir() and (path / "channel.json").exists():
            # A malformed declaration is rejected instead of silently
            # dropping into the more permissive legacy loader.
            continue
        if path.suffix == ".py" and path.stem != "__init__":
            name = path.stem
        elif path.is_dir() and (path / "__init__.py").exists():
            name = path.name
        else:
            continue
        try:
            mod = importlib.import_module(name)
        except Exception:
            logger.exception("failed to load custom channel: %s", name)
            continue
        for obj in vars(mod).values():
            if (
                isinstance(obj, type)
                and issubclass(obj, BaseChannel)
                and obj is not BaseChannel
            ):
                key = getattr(obj, "channel", None)
                if key:
                    out[key] = obj
                    logger.debug("custom channel registered: %s", key)
    return out


def _load_plugin_manifest(path: Path) -> ChannelPluginManifest | None:
    """Read a manifest directory, logging invalid declarations safely."""
    manifest_path = path / "channel.json" if path.is_dir() else None
    if manifest_path is None or not manifest_path.is_file():
        return None
    try:
        return ChannelPluginManifest.from_path(manifest_path)
    except Exception:
        logger.exception("invalid custom channel manifest: %s", manifest_path)
        return None


def _manifest_module_name(
    path: Path,
    manifest: ChannelPluginManifest,
) -> str:
    """Resolve a manifest module inside its plugin package only."""
    if manifest.module == path.name or manifest.module.startswith(
        f"{path.name}.",
    ):
        return manifest.module
    return f"{path.name}.{manifest.module}"


def _discover_custom_manifests() -> dict[str, ChannelPluginManifest]:
    if not CUSTOM_CHANNELS_DIR.is_dir():
        return {}
    manifests: dict[str, ChannelPluginManifest] = {}
    for path in sorted(CUSTOM_CHANNELS_DIR.iterdir()):
        manifest = _load_plugin_manifest(path)
        if manifest is None:
            continue
        if manifest.key in BUILTIN_CHANNEL_KEYS:
            logger.warning(
                "custom channel manifest cannot shadow built-in key: %s",
                manifest.key,
            )
            continue
        manifests[manifest.key] = manifest
    return manifests


BUILTIN_CHANNEL_ORDER = tuple(_BUILTIN_SPECS.keys())
BUILTIN_CHANNEL_KEYS = frozenset(BUILTIN_CHANNEL_ORDER)


def get_configurable_channel_keys() -> tuple[str, ...]:
    """Return channel keys that can be configured in the console.

    This intentionally includes every built-in channel declared by the app,
    even if an optional runtime dependency is unavailable in the current
    process. Runtime startup still uses :func:`get_channel_registry`, but the
    settings UI should not hide supported channels just because their SDK has
    not been imported yet.
    """

    keys = list(BUILTIN_CHANNEL_ORDER)
    custom_keys = set(_discover_custom_manifests())
    custom_keys.update(_discover_custom_channels())
    for key in custom_keys:
        if key not in BUILTIN_CHANNEL_KEYS:
            keys.append(key)
    return tuple(keys)


def register_custom_channel_routes(app) -> None:
    """Let custom channels register additional HTTP routes on the FastAPI app.

    Custom channel modules may define a module-level callable
    ``register_app_routes(app)``.  If present, it is called so the
    channel can mount its own API endpoints (e.g. QR login pages,
    webhook handlers, etc.).

    Must be called at module level (before the SPA catch-all route)
    to ensure route priority.  Channels that need access to
    ``app.state.multi_agent_manager`` should read it lazily at
    request time.

    **All routes MUST be under the ``/api/`` prefix.** Routes without
    this prefix will be silently swallowed by the SPA catch-all
    (``/{full_path:path}``). A warning is emitted at startup if any
    non-``/api/`` routes are detected.

    Errors in individual channel hooks are logged but never propagated.
    """
    if not CUSTOM_CHANNELS_DIR.is_dir():
        return

    dir_str = str(CUSTOM_CHANNELS_DIR)
    if dir_str not in sys.path:
        sys.path.insert(0, dir_str)

    for path in sorted(CUSTOM_CHANNELS_DIR.iterdir()):
        manifest = _load_plugin_manifest(path)
        if manifest is not None:
            name = _manifest_module_name(path, manifest)
        elif path.is_dir() and (path / "channel.json").exists():
            continue
        elif path.suffix == ".py" and path.stem != "__init__":
            name = path.stem
        elif path.is_dir() and (path / "__init__.py").exists():
            name = path.name
        else:
            continue
        try:
            mod = importlib.import_module(name)
            hook = getattr(mod, "register_app_routes", None)
            if not callable(hook):
                continue
            prev_routes = {r.path for r in app.routes}
            hook(app)
            new_routes = {r.path for r in app.routes} - prev_routes
            non_api = {p for p in new_routes if not p.startswith("/api/")}
            if non_api:
                logger.warning(
                    "Custom channel %s registered routes without /api/ "
                    "prefix: %s. These will be swallowed by the SPA "
                    "catch-all.",
                    name,
                    non_api,
                )
        except Exception:
            logger.exception("Failed to load custom channel routes: %s", name)


def get_channel_registry() -> dict[str, type[BaseChannel]]:
    """Built-in channel classes + custom channels from custom_channels/."""
    out = _get_cached_builtin_channels()
    out.update(_discover_custom_channels())
    return out


def get_channel_capabilities() -> dict[str, dict]:
    """Return transport support facts without instantiating channels."""
    return {
        key: channel_class.capabilities.to_dict()
        for key, channel_class in get_channel_registry().items()
    }


def get_channel_descriptors() -> list[dict]:
    """Return console-facing metadata without requiring plugin execution."""
    registry = get_channel_registry()
    descriptors: list[dict] = []
    for key in BUILTIN_CHANNEL_ORDER:
        channel_class = registry.get(key)
        capabilities = (
            channel_class.capabilities.to_dict()
            if channel_class is not None
            else {}
        )
        descriptors.append(
            {
                "schemaVersion": 1,
                "key": key,
                "name": key,
                "description": "",
                "configFields": [],
                "capabilities": capabilities,
                "isBuiltin": True,
                "available": channel_class is not None,
            },
        )
    for key, manifest in _discover_custom_manifests().items():
        descriptors.append(
            manifest.descriptor(
                builtin=False,
                available=(
                    not manifest.missing_requirements() and key in registry
                ),
            ),
        )
    return descriptors
