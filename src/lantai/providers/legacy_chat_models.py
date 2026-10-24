# -*- coding: utf-8 -*-
"""Legacy chat model class resolution for provider compatibility."""

from __future__ import annotations

from typing import Any, Type

from lantai.exceptions import ProviderError


def resolve_legacy_chat_model_cls(
    chat_model_name: str,
    *,
    provider_name: str,
) -> Type[Any]:
    """Resolve a configured legacy chat model class by name."""

    if chat_model_name == "OpenAIChatModel":
        from lantai.providers.openai_chat_model_compat import (
            OpenAIChatModelCompat,
        )

        return OpenAIChatModelCompat

    from lantai.agents.legacy_model_components import (
        AnthropicChatModel,
        GeminiChatModel,
    )

    chat_model_cls = {
        "AnthropicChatModel": AnthropicChatModel,
        "GeminiChatModel": GeminiChatModel,
    }.get(chat_model_name)
    if chat_model_cls is None:
        raise ProviderError(
            message=(
                f"Chat model class '{chat_model_name}' "
                f"not found for provider '{provider_name}'."
            ),
        )
    return chat_model_cls
