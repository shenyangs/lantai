# -*- coding: utf-8 -*-
"""Runtime-owned tool response blocks.

These shapes intentionally mirror the small attribute surface used by the
legacy toolkit while remaining independent from any runtime framework.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import time_ns
from typing import Any


ContentBlockDict = dict[str, Any]


def TextBlock(*, type: str = "text", text: str = "") -> ContentBlockDict:
    """Return a text content block."""

    return {"type": type, "text": text}


def ToolResultBlock(
    *,
    type: str = "tool_result",
    id: str,
    name: str,
    output: list[ContentBlockDict] | None = None,
    **kwargs: Any,
) -> ContentBlockDict:
    """Return a tool-result content block."""

    return {
        "type": type,
        "id": id,
        "name": name,
        "output": list(output or []),
        **kwargs,
    }


def ToolUseBlock(
    *,
    type: str = "tool_use",
    id: str,
    name: str,
    input: dict[str, Any] | None = None,  # pylint: disable=redefined-builtin
    raw_input: str | None = None,
    **kwargs: Any,
) -> ContentBlockDict:
    """Return a tool-use content block."""

    block = {
        "type": type,
        "id": id,
        "name": name,
        "input": dict(input or {}),
        **kwargs,
    }
    if raw_input is not None:
        block["raw_input"] = raw_input
    return block


def ImageBlock(**kwargs: Any) -> ContentBlockDict:
    """Return an image content block."""

    return {"type": "image", **kwargs}


def AudioBlock(**kwargs: Any) -> ContentBlockDict:
    """Return an audio content block."""

    return {"type": "audio", **kwargs}


def VideoBlock(**kwargs: Any) -> ContentBlockDict:
    """Return a video content block."""

    return {"type": "video", **kwargs}


def FileBlock(**kwargs: Any) -> ContentBlockDict:
    """Return a file content block."""

    return {"type": "file", **kwargs}


@dataclass
class ToolResponse:
    """Normalized response chunk returned by Lantai tools."""

    content: list[ContentBlockDict]
    metadata: dict[str, Any] | None = None
    is_error: bool = False
    stream: bool = False
    is_last: bool = True
    is_interrupted: bool = False
    id: str = field(default_factory=lambda: str(time_ns()))
