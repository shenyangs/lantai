# -*- coding: utf-8 -*-
"""Declarative metadata for third-party channel plugins."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .schema import ChannelCapabilities

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
_PYTHON_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PYTHON_MODULE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$",
)


class ChannelPluginConfigField(BaseModel):
    """One console-renderable plugin setting."""

    model_config = ConfigDict(extra="forbid")

    name: str
    label: str
    type: Literal[
        "string",
        "password",
        "number",
        "boolean",
        "select",
        "path",
    ] = "string"
    required: bool = False
    secret: bool = False
    default: Any = None
    description: str = ""
    options: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not _PYTHON_NAME.fullmatch(value):
            raise ValueError("config field name must be a Python identifier")
        return value


class ChannelPluginCapabilities(BaseModel):
    """JSON representation of :class:`ChannelCapabilities`."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    inbound_media: list[str] = Field(default=["text"], alias="inboundMedia")
    outbound_media: list[str] = Field(default=["text"], alias="outboundMedia")
    threads: bool = False
    message_edit: bool = Field(default=False, alias="messageEdit")
    streaming: bool = False
    interactions: bool = False
    proactive_send: bool = Field(default=False, alias="proactiveSend")
    reactions: bool = False
    multiple_accounts: bool = Field(default=False, alias="multipleAccounts")
    requires_public_webhook: bool = Field(
        default=False,
        alias="requiresPublicWebhook",
    )

    def to_runtime(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            inbound_media=frozenset(self.inbound_media),
            outbound_media=frozenset(self.outbound_media),
            threads=self.threads,
            message_edit=self.message_edit,
            streaming=self.streaming,
            interactions=self.interactions,
            proactive_send=self.proactive_send,
            reactions=self.reactions,
            multiple_accounts=self.multiple_accounts,
            requires_public_webhook=self.requires_public_webhook,
        )


class ChannelPluginManifest(BaseModel):
    """Validated ``channel.json`` contract (schema version 1)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: Literal[1] = Field(alias="schemaVersion")
    key: str
    name: str
    module: str = "channel"
    class_name: str = Field(alias="className")
    description: str = ""
    version: str = ""
    author: str = ""
    docs_url: str = Field(default="", alias="docsUrl")
    requires: list[str] = Field(default_factory=list)
    config_fields: list[ChannelPluginConfigField] = Field(
        default_factory=list,
        alias="configFields",
    )
    capabilities: ChannelPluginCapabilities = Field(
        default_factory=ChannelPluginCapabilities,
    )

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError(
                "key must be 2-64 lowercase letters, numbers, '_' or '-'",
            )
        return value

    @field_validator("module")
    @classmethod
    def validate_module(cls, value: str) -> str:
        if not _PYTHON_MODULE.fullmatch(value):
            raise ValueError("module must be a dotted Python module name")
        return value

    @field_validator("class_name")
    @classmethod
    def validate_class_name(cls, value: str) -> str:
        if not _PYTHON_NAME.fullmatch(value):
            raise ValueError("className must be a Python identifier")
        return value

    @field_validator("requires")
    @classmethod
    def validate_requires(cls, values: list[str]) -> list[str]:
        for value in values:
            if not _PYTHON_MODULE.fullmatch(value):
                raise ValueError(
                    "requires entries must be importable Python module names",
                )
        return values

    @classmethod
    def from_path(cls, path: Path) -> "ChannelPluginManifest":
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def missing_requirements(self) -> list[str]:
        return [
            name for name in self.requires if importlib.util.find_spec(name) is None
        ]

    def descriptor(self, *, builtin: bool, available: bool) -> dict[str, Any]:
        result = self.model_dump(by_alias=True)
        result.update({"isBuiltin": builtin, "available": available})
        return result
