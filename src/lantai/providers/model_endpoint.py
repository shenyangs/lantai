# -*- coding: utf-8 -*-
"""Provider-to-runtime model endpoint contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ModelEndpointFamily = Literal["openai", "anthropic", "gemini"]
ModelEndpointNativeKind = Literal[
    "openai_compatible",
    "openai_responses",
    "legacy",
]


@dataclass(frozen=True, slots=True)
class ProviderModelEndpoint:
    """Resolved model endpoint used by Lantai model clients."""

    provider_id: str
    model_id: str
    family: ModelEndpointFamily = "openai"
    base_url: str = ""
    api_key: str = ""
    is_local: bool = False
    chat_model: str = "OpenAIChatModel"
    generate_kwargs: dict[str, Any] = field(default_factory=dict)
    default_headers: dict[str, str] | None = None
    supports_multimodal: bool | None = None
    native_kind: ModelEndpointNativeKind = "openai_compatible"
    normalize_content_to_text: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
