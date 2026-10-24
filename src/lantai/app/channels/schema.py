# -*- coding: utf-8 -*-
"""
Channel schema: channel type identifiers, routing (ChannelAddress),
and conversion protocol.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Dict, Optional, Protocol, runtime_checkable


@dataclass
class ChannelAddress:
    """
    Unified routing for send: kind + id + extra.
    Replaces ad-hoc meta keys (channel_id, user_id, session_webhook, etc.).
    """

    kind: str  # "dm" | "channel" | "webhook" | "console" | ...
    id: str
    extra: Optional[Dict[str, Any]] = None

    def to_handle(self) -> str:
        """String handle for to_handle (e.g. discord:ch:123)."""
        if self.extra and "to_handle" in self.extra:
            return str(self.extra["to_handle"])
        return f"{self.kind}:{self.id}"


@dataclass(frozen=True)
class ChannelCapabilities:
    """Machine-readable behavior supported by a channel adapter.

    These flags describe transport facts, not permissions. A plugin may
    advertise that it can edit messages or send files, while Tool Guard and
    channel access control still decide whether an action is allowed.
    """

    inbound_media: frozenset[str] = field(
        default_factory=lambda: frozenset({"text"}),
    )
    outbound_media: frozenset[str] = field(
        default_factory=lambda: frozenset({"text"}),
    )
    threads: bool = False
    message_edit: bool = False
    streaming: bool = False
    interactions: bool = False
    proactive_send: bool = False
    reactions: bool = False
    multiple_accounts: bool = False
    requires_public_webhook: bool = False

    def with_runtime_streaming(self, enabled: bool) -> "ChannelCapabilities":
        """Return configured capabilities without mutating class metadata."""
        return replace(self, streaming=self.streaming and enabled)

    def to_dict(self) -> Dict[str, Any]:
        """Return stable camelCase data for APIs and plugin manifests."""
        return {
            "inboundMedia": sorted(self.inbound_media),
            "outboundMedia": sorted(self.outbound_media),
            "threads": self.threads,
            "messageEdit": self.message_edit,
            "streaming": self.streaming,
            "interactions": self.interactions,
            "proactiveSend": self.proactive_send,
            "reactions": self.reactions,
            "multipleAccounts": self.multiple_accounts,
            "requiresPublicWebhook": self.requires_public_webhook,
        }


# Built-in channel type identifiers. Plugin channels use arbitrary str keys.
BUILTIN_CHANNEL_TYPES = (
    "imessage",
    "discord",
    "dingtalk",
    "feishu",
    "qq",
    "telegram",
    "mqtt",
    "console",
    "voice",
    "sip",
    "xiaoyi",
)

# ChannelType is str to allow plugin channels; built-in set above.
ChannelType = str

# Default channel when none is specified (runner / config).
DEFAULT_CHANNEL: ChannelType = "console"


@runtime_checkable
class ChannelMessageConverter(Protocol):
    """
    Protocol for channel message conversion.
    Channels convert native payloads to AgentRequest and send responses.
    """

    def build_agent_request_from_native(self, native_payload: Any) -> Any:
        """
        Convert this channel's native message payload to AgentRequest.
        Use runtime Message and Content types; no intermediate envelope.
        """

    async def send_response(
        self,
        to_handle: str,
        response: Any,
        meta: Optional[dict] = None,
    ) -> None:
        """Convert AgentResponse to channel reply and send."""
