# -*- coding: utf-8 -*-
"""Runner public API without eager initialization of the chat runtime."""
from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS: dict[str, tuple[str, str]] = {
    "AgentRunner": ("runner", "AgentRunner"),
    "router": ("api", "router"),
    "ChatManager": ("manager", "ChatManager"),
    "ChatSpec": ("models", "ChatSpec"),
    "ChatHistory": ("models", "ChatHistory"),
    "ChatsFile": ("models", "ChatsFile"),
    "BaseChatRepository": ("repo", "BaseChatRepository"),
    "JsonChatRepository": ("repo", "JsonChatRepository"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(import_module(f"{__name__}.{module_name}"), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
