# -*- coding: utf-8 -*-
"""Legacy message conversions for model client adapters."""

from __future__ import annotations

import json
from typing import Any

from ..converters import legacy_message
from ..models import ModelContentBlock, ModelMessage


def model_message_from_legacy_msg(msg: Any) -> ModelMessage:
    """Convert a legacy message object to a provider-neutral model message."""

    content = getattr(msg, "content", "")
    blocks: list[ModelContentBlock] = []
    if isinstance(content, str):
        blocks.append(ModelContentBlock(type="text", text=content))
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                blocks.append(ModelContentBlock(type="text", text=str(block)))
                continue
            block_type = str(block.get("type") or "text")
            data = {key: value for key, value in block.items() if key != "type"}
            if block_type == "text":
                blocks.append(
                    ModelContentBlock(
                        type="text",
                        text=str(block.get("text") or ""),
                        data=data,
                    ),
                )
            elif block_type == "thinking":
                blocks.append(
                    ModelContentBlock(
                        type="thinking",
                        text=str(block.get("thinking") or block.get("text") or ""),
                        data=data,
                    ),
                )
            elif block_type == "tool_use":
                blocks.append(
                    ModelContentBlock(
                        type="tool_call",
                        data={
                            "id": block.get("id"),
                            "name": block.get("name"),
                            "arguments": block.get("input", {}),
                            **(
                                {"raw_input": block.get("raw_input")}
                                if "raw_input" in block
                                else {}
                            ),
                            **(
                                {"extra_content": block.get("extra_content")}
                                if "extra_content" in block
                                else {}
                            ),
                        },
                    ),
                )
            elif block_type == "tool_result":
                blocks.append(
                    ModelContentBlock(
                        type="tool_result",
                        data={
                            "id": block.get("id"),
                            "name": block.get("name"),
                            "output": block.get("output"),
                        },
                    ),
                )
            elif block_type in {"image", "audio", "video", "file"}:
                blocks.append(ModelContentBlock(type=block_type, data=data))
            else:
                blocks.append(
                    ModelContentBlock(
                        type="text",
                        text=json.dumps(block, ensure_ascii=False),
                        metadata={"legacy_type": block_type},
                    ),
                )
    else:
        blocks.append(ModelContentBlock(type="text", text=str(content)))

    return ModelMessage(
        id=getattr(msg, "id", None),
        role=getattr(msg, "role", None) or "assistant",
        name=getattr(msg, "name", None),
        content=blocks,
        metadata={
            "source": "legacy",
            "metadata": getattr(msg, "metadata", None),
        },
    )


def model_messages_from_legacy_msgs(
    messages: list[Any],
) -> list[ModelMessage]:
    """Convert legacy messages to model messages."""

    return [model_message_from_legacy_msg(message) for message in messages]


def legacy_msg_from_model_message(message: ModelMessage) -> Any:
    """Convert a model message back to a legacy message object."""

    if len(message.content) == 1 and message.content[0].type == "text":
        content: str | list[dict[str, Any]] = message.content[0].text or ""
    else:
        content = []
        for block in message.content:
            if block.type == "text":
                content.append({"type": "text", "text": block.text or ""})
            elif block.type == "thinking":
                # Anthropic-compatible reasoning models (notably MiniMax-M3)
                # require the complete thinking block to be replayed unchanged
                # across tool-use turns. Dropping the provider signature here
                # breaks interleaved thinking after the first tool call.
                thinking_block = {
                    "type": "thinking",
                    **dict(block.data),
                    "thinking": block.text or block.data.get("thinking") or "",
                }
                content.append(thinking_block)
            elif block.type == "tool_call":
                tool_use = {
                    "type": "tool_use",
                    "id": block.data.get("id"),
                    "name": block.data.get("name"),
                    "input": block.data.get("arguments", {}),
                }
                if "raw_input" in block.data:
                    tool_use["raw_input"] = block.data.get("raw_input")
                if "extra_content" in block.data:
                    tool_use["extra_content"] = block.data.get("extra_content")
                content.append(tool_use)
            elif block.type == "tool_result":
                content.append(
                    {
                        "type": "tool_result",
                        "id": block.data.get("id"),
                        "name": block.data.get("name"),
                        "output": block.data.get("output"),
                    },
                )
            elif block.type in {"image", "audio", "video", "file"}:
                content.append({"type": block.type, **block.data})
            elif block.type == "error":
                content.append({"type": "text", "text": block.text or ""})
    legacy_role = "user" if message.role == "tool" else message.role
    msg = legacy_message(
        name=message.name or message.role,
        role=legacy_role,
        content=content,
        metadata=message.metadata,
    )
    if message.id:
        msg.id = message.id
    return msg


def legacy_msgs_from_model_messages(
    messages: list[ModelMessage],
) -> list[Any]:
    """Convert model messages to legacy message objects."""

    return [legacy_msg_from_model_message(message) for message in messages]
