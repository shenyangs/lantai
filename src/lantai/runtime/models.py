# -*- coding: utf-8 -*-
"""Runtime-owned model contracts for Lantai."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from .tools import ToolCall, ToolSpec

ModelRole = Literal["system", "user", "assistant", "tool"]
ModelContentBlockType = Literal[
    "text",
    "image",
    "audio",
    "video",
    "file",
    "thinking",
    "tool_call",
    "tool_result",
    "error",
]
ModelStreamEventKind = Literal[
    "provider_activity",
    "message_start",
    "text_delta",
    "thinking_delta",
    "tool_call_delta",
    "tool_call_completed",
    "usage",
    "message_completed",
    "error",
]


class ModelContentBlock(BaseModel):
    """One provider-neutral content block in a model message."""

    type: ModelContentBlockType
    text: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelMessage(BaseModel):
    """Provider-neutral message consumed by Lantai model clients."""

    id: str | None = None
    role: ModelRole
    content: list[ModelContentBlock] = Field(default_factory=list)
    name: str | None = None
    tool_call_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def text(
        cls,
        role: ModelRole,
        text: str,
        *,
        id: str | None = None,  # pylint: disable=redefined-builtin
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "ModelMessage":
        """Build a text-only model message."""

        return cls(
            id=id,
            role=role,
            name=name,
            content=[ModelContentBlock(type="text", text=text)],
            metadata=dict(metadata or {}),
        )

    def text_content(self) -> str:
        """Return visible text content joined in order."""

        return "\n".join(
            block.text or ""
            for block in self.content
            if block.type == "text" and block.text
        )


class ModelUsage(BaseModel):
    """Normalized usage payload emitted by model clients."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int | None = None
    # Providers commonly omit billing data. Missing evidence must remain
    # unknown; an explicit numeric zero is the only representation of free.
    cost_cny: float | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ModelRequest(BaseModel):
    """Provider-neutral model request used by Native Runtime."""

    messages: list[ModelMessage] = Field(default_factory=list)
    tools: list[ToolSpec] = Field(default_factory=list)
    tool_choice: Literal["auto", "none", "required"] | str | None = "auto"
    model: str | None = None
    provider_id: str | None = None
    stream: bool = True
    generation: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelResponse(BaseModel):
    """Final model response for non-streaming or collected streaming calls."""

    message: ModelMessage
    usage: ModelUsage | None = None
    finish_reason: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelStreamEvent(BaseModel):
    """One native model stream event."""

    kind: ModelStreamEventKind
    message_id: str | None = None
    delta: str = ""
    content_block: ModelContentBlock | None = None
    tool_call: ToolCall | None = None
    usage: ModelUsage | None = None
    response: ModelResponse | None = None
    finish_reason: str | None = None
    error: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class LantaiModelClient(Protocol):
    """Interface implemented by all Lantai-owned model adapters."""

    provider_id: str
    model_name: str

    async def stream(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        """Yield provider-neutral stream events for one model request."""
        ...

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Return a final model response."""
        ...
