# -*- coding: utf-8 -*-
"""Conversions and payload formatting for Lantai model clients."""

from __future__ import annotations

from .messages import RuntimeMessage
from .models import ModelContentBlock, ModelMessage


def model_message_from_runtime_message(message: RuntimeMessage) -> ModelMessage:
    """Convert one runtime message to a model message."""

    return ModelMessage(
        id=message.id,
        role=message.role,
        name=message.name,
        content=[
            ModelContentBlock(
                type=block.type if block.type != "error" else "error",
                text=block.text,
                data=dict(block.data),
                metadata=dict(block.metadata),
            )
            for block in message.content
        ],
        metadata=dict(message.metadata),
    )


def model_messages_from_runtime_messages(
    messages: list[RuntimeMessage],
) -> list[ModelMessage]:
    """Convert runtime messages to model messages."""

    return [model_message_from_runtime_message(message) for message in messages]
