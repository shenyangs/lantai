# -*- coding: utf-8 -*-
"""Runtime message contracts owned by Lantai."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

RuntimeRole = Literal["system", "user", "assistant", "tool"]
ContentBlockType = Literal[
    "text",
    "image",
    "file",
    "tool_call",
    "tool_result",
    "thinking",
    "error",
]


class ContentBlock(BaseModel):
    """One normalized content block in a runtime message."""

    type: ContentBlockType
    text: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RuntimeMessage(BaseModel):
    """Message shape used by Lantai Runtime adapters and facades."""

    id: str | None = None
    role: RuntimeRole
    content: list[ContentBlock] = Field(default_factory=list)
    name: str | None = None
    created_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def text(
        cls,
        role: RuntimeRole,
        text: str,
        *,
        id: str | None = None,  # pylint: disable=redefined-builtin
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "RuntimeMessage":
        """Build a text-only message without exposing provider types."""

        return cls(
            id=id,
            role=role,
            name=name,
            content=[ContentBlock(type="text", text=text)],
            metadata=dict(metadata or {}),
        )

    def text_content(self) -> str:
        """Return visible text blocks joined in order."""

        return "\n".join(
            block.text or ""
            for block in self.content
            if block.type == "text" and block.text
        )
