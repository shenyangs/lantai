# -*- coding: utf-8 -*-
"""OpenCode Zen provider with live catalog normalization."""

from __future__ import annotations

from typing import Any, List

from .model_endpoint import ProviderModelEndpoint
from .legacy_chat_models import resolve_legacy_chat_model_cls
from .openai_provider import OpenAIProvider
from .provider import ModelInfo


class OpenCodeProvider(OpenAIProvider):
    """OpenCode Zen provider backed by its live ``/models`` catalog."""

    @staticmethod
    def _normalize_models_payload(payload: Any) -> List[ModelInfo]:
        models = OpenAIProvider._normalize_models_payload(payload)
        for model in models:
            model.is_free = model.id == "big-pickle" or model.id.endswith(
                "-free",
            )
        return models

    @staticmethod
    def _protocol_for_model(model_id: str) -> str:
        normalized = (model_id or "").strip().lower()
        if normalized.startswith("gemini-"):
            return "gemini"
        if normalized.startswith(("claude-", "qwen")):
            return "anthropic"
        if normalized.startswith(("gpt-", "grok-", "muse-spark-")):
            return "openai_responses"
        return "openai_compatible"

    def resolve_model_endpoint(self, model_id: str) -> ProviderModelEndpoint:
        protocol = self._protocol_for_model(model_id)
        common = {
            "provider_id": self.id,
            "model_id": model_id,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "is_local": False,
            "generate_kwargs": self.get_effective_generate_kwargs(model_id),
            "supports_multimodal": self._model_supports_multimodal(model_id),
            "metadata": {"opencode_protocol": protocol},
        }
        if protocol == "anthropic":
            return ProviderModelEndpoint(
                **common,
                family="anthropic",
                chat_model="AnthropicChatModel",
                native_kind="legacy",
            )
        if protocol == "gemini":
            return ProviderModelEndpoint(
                **common,
                family="gemini",
                chat_model="GeminiChatModel",
                native_kind="legacy",
            )
        return ProviderModelEndpoint(
            **common,
            family="openai",
            chat_model="OpenAIChatModel",
            native_kind=protocol,
        )

    def get_chat_model_instance(self, model_id: str) -> Any:
        protocol = self._protocol_for_model(model_id)
        if protocol == "anthropic":
            chat_model_cls = resolve_legacy_chat_model_cls(
                "AnthropicChatModel",
                provider_name=self.name,
            )
            default_headers = (
                {"Authorization": f"Bearer {self.api_key}"}
                if self.api_key
                else None
            )
            return chat_model_cls(
                model_name=model_id,
                max_tokens=16384,
                stream=True,
                api_key=self.api_key,
                stream_tool_parsing=False,
                client_kwargs={
                    "base_url": self.base_url,
                    **(
                        {"default_headers": default_headers}
                        if default_headers
                        else {}
                    ),
                },
                generate_kwargs=self.get_effective_generate_kwargs(model_id),
            )
        if protocol == "gemini":
            from google.genai import types as genai_types

            chat_model_cls = resolve_legacy_chat_model_cls(
                "GeminiChatModel",
                provider_name=self.name,
            )
            headers = (
                {"Authorization": f"Bearer {self.api_key}"}
                if self.api_key
                else None
            )
            return chat_model_cls(
                model_name=model_id,
                stream=True,
                api_key=self.api_key,
                client_kwargs={
                    "http_options": genai_types.HttpOptions(
                        base_url=self.base_url,
                        headers=headers,
                    ),
                },
                generate_kwargs=self.get_effective_generate_kwargs(model_id),
            )
        return super().get_chat_model_instance(model_id)
