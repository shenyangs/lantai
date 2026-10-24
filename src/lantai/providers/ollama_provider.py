# -*- coding: utf-8 -*-
"""An Ollama provider implementation with native runtime discovery."""

import asyncio
import os
import re
from typing import Any

import httpx
from openai import AsyncOpenAI

from lantai.providers.model_endpoint import ProviderModelEndpoint
from lantai.providers.openai_provider import OpenAIProvider
from lantai.providers.provider import ModelInfo


class OllamaProvider(OpenAIProvider):
    """Provider implementation for Ollama local LLM hosting platform."""

    _SHOW_CONCURRENCY = 6

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        normalized_base_url = (base_url or "").rstrip("/")
        if normalized_base_url.endswith("/v1"):
            # Accept either the native server root or the OpenAI-compatible
            # suffix and store one canonical native root.
            normalized_base_url = normalized_base_url[:-3].rstrip("/")
        return normalized_base_url

    def _openai_compatible_base_url(self) -> str:
        return self._normalize_base_url(self.base_url) + "/v1"

    def _native_headers(self) -> dict[str, str] | None:
        token = (self.api_key or "").strip()
        if not token:
            return None
        return {"Authorization": f"Bearer {token}"}

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        timeout: float,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._normalize_base_url(self.base_url)}{path}"
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(
                method,
                url,
                headers=self._native_headers(),
                json=json_body,
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"Ollama returned an invalid response from {path}")
        return payload

    def _connection_failure_message(self, exc: Exception) -> str:
        endpoint = self._normalize_base_url(self.base_url)
        if isinstance(exc, httpx.TimeoutException):
            reason = "The request timed out"
        elif isinstance(exc, httpx.HTTPStatusError):
            reason = f"HTTP {exc.response.status_code}"
        else:
            reason = "The service could not be reached"
        return (
            f"{reason} at {endpoint}. Start Ollama with `ollama serve`, "
            "then try again."
        )

    @staticmethod
    def _positive_int(value: Any) -> int | None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @classmethod
    def _model_max_context(cls, show_payload: dict[str, Any]) -> int | None:
        model_info = show_payload.get("model_info")
        if not isinstance(model_info, dict):
            return None
        candidates = [
            cls._positive_int(value)
            for key, value in model_info.items()
            if str(key).endswith(".context_length")
            and ".vision." not in str(key)
            and ".audio." not in str(key)
        ]
        values = [value for value in candidates if value is not None]
        return max(values) if values else None

    @classmethod
    def _configured_context(cls, show_payload: dict[str, Any]) -> int | None:
        parameters = show_payload.get("parameters")
        if not isinstance(parameters, str):
            return None
        match = re.search(r"(?:^|\n)\s*num_ctx\s+(\d+)", parameters)
        return cls._positive_int(match.group(1)) if match else None

    @classmethod
    def _model_from_native_payload(
        cls,
        summary: dict[str, Any],
        show_payload: dict[str, Any],
        running_context: int | None,
    ) -> ModelInfo | None:
        model_id = str(summary.get("model") or summary.get("name") or "").strip()
        if not model_id:
            return None

        raw_capabilities = show_payload.get("capabilities")
        capabilities = (
            {
                str(value).strip().lower()
                for value in raw_capabilities
                if str(value).strip()
            }
            if isinstance(raw_capabilities, list)
            else set()
        )
        if capabilities and "embedding" in capabilities and not (
            {"completion", "chat"} & capabilities
        ):
            return None

        summary_details = summary.get("details")
        show_details = show_payload.get("details")
        details: dict[str, Any] = {}
        if isinstance(summary_details, dict):
            details.update(summary_details)
        if isinstance(show_details, dict):
            details.update(show_details)

        supports_image = "vision" in capabilities if capabilities else None
        supports_video = False if capabilities else None
        max_context_window = cls._model_max_context(show_payload)
        configured_context = cls._configured_context(show_payload)
        effective_context = running_context or configured_context
        return ModelInfo(
            id=model_id,
            name=str(summary.get("name") or model_id).strip() or model_id,
            supports_tool_use=(
                bool({"tools", "tool", "tool_use"} & capabilities)
                if capabilities
                else None
            ),
            supports_image=supports_image,
            supports_video=supports_video,
            supports_multimodal=(supports_image if capabilities else None),
            probe_source="runtime" if capabilities else None,
            context_window=effective_context,
            max_context_window=max_context_window,
            capability_source="runtime:ollama-native",
            runtime_state="loaded" if running_context else "available",
            parameter_size=(
                str(details.get("parameter_size") or "").strip() or None
            ),
            quantization=(
                str(details.get("quantization_level") or "").strip() or None
            ),
            size_bytes=cls._positive_int(summary.get("size")),
        )

    def model_post_init(self, __context: Any) -> None:
        if not self.base_url:  # type: ignore
            self.base_url = (
                os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434"
            )
        self.base_url = self._normalize_base_url(self.base_url)

    def update_config(self, config: dict[str, Any]) -> None:
        super().update_config(config)
        self.base_url = self._normalize_base_url(self.base_url)

    def _client(self, timeout: float = 5) -> AsyncOpenAI:
        return AsyncOpenAI(
            base_url=self._openai_compatible_base_url(),
            api_key=self.api_key,
            timeout=timeout,
        )

    async def check_connection(self, timeout: float = 5) -> tuple[bool, str]:
        """Check the native Ollama endpoint and return actionable guidance."""
        try:
            payload = await self._request_json(
                "GET",
                "/api/tags",
                timeout=timeout,
            )
        except Exception as exc:  # local transports raise several subclasses
            return False, self._connection_failure_message(exc)

        models = payload.get("models")
        count = len(models) if isinstance(models, list) else 0
        if count == 0:
            return (
                True,
                "Ollama is running, but no local models are installed. "
                "Run `ollama pull <model>` and sync again.",
            )
        return True, f"Ollama is running with {count} local model(s)."

    async def fetch_models(self, timeout: float = 5) -> list[ModelInfo]:
        """Discover installed models and enrich them through native APIs."""
        try:
            tags_payload = await self._request_json(
                "GET",
                "/api/tags",
                timeout=timeout,
            )
        except Exception as exc:
            raise ValueError(self._connection_failure_message(exc)) from exc

        summaries = tags_payload.get("models")
        if not isinstance(summaries, list):
            raise ValueError("Ollama returned an invalid model catalog.")

        running_contexts: dict[str, int] = {}
        try:
            running_payload = await self._request_json(
                "GET",
                "/api/ps",
                timeout=timeout,
            )
            running_models = running_payload.get("models")
            if isinstance(running_models, list):
                for row in running_models:
                    if not isinstance(row, dict):
                        continue
                    model_id = str(
                        row.get("model") or row.get("name") or "",
                    ).strip()
                    context = self._positive_int(row.get("context_length"))
                    if model_id and context:
                        running_contexts[model_id] = context
        except Exception:
            # /api/ps is optional enrichment; /api/tags remains authoritative.
            pass

        semaphore = asyncio.Semaphore(self._SHOW_CONCURRENCY)

        async def enrich(summary: Any) -> ModelInfo | None:
            if not isinstance(summary, dict):
                return None
            model_id = str(
                summary.get("model") or summary.get("name") or "",
            ).strip()
            if not model_id:
                return None
            show_payload: dict[str, Any] = {}
            try:
                async with semaphore:
                    show_payload = await self._request_json(
                        "POST",
                        "/api/show",
                        timeout=timeout,
                        json_body={"model": model_id},
                    )
            except Exception:
                # A custom/older artifact may fail /api/show. Keep it
                # selectable and mark its agent capabilities as unknown.
                pass
            return self._model_from_native_payload(
                summary,
                show_payload,
                running_contexts.get(model_id),
            )

        enriched = await asyncio.gather(*(enrich(row) for row in summaries))
        return [model for model in enriched if model is not None]

    async def check_model_connection(
        self,
        model_id: str,
        timeout: float = 5,
    ) -> tuple[bool, str]:
        """Check if a specific model is reachable/usable."""
        models = await self.fetch_models(timeout=timeout)
        if any(model.id == model_id for model in models):
            return True, ""
        return False, f"Model '{model_id}' not found"

    def get_chat_model_instance(self, model_id: str) -> Any:
        from .openai_chat_model_compat import OpenAIChatModelCompat

        return OpenAIChatModelCompat(
            model_name=model_id,
            stream=True,
            api_key=self.api_key,
            stream_tool_parsing=False,
            normalize_content_to_text=True,
            client_kwargs={"base_url": self._openai_compatible_base_url()},
            generate_kwargs=self.get_effective_generate_kwargs(model_id),
        )

    def resolve_model_endpoint(self, model_id: str) -> ProviderModelEndpoint:
        """Resolve Ollama's OpenAI-compatible /v1 endpoint."""

        return ProviderModelEndpoint(
            provider_id=self.id,
            model_id=model_id,
            family="openai",
            base_url=self._openai_compatible_base_url(),
            api_key=self.api_key,
            is_local=True,
            chat_model=self.chat_model,
            generate_kwargs=self.get_effective_generate_kwargs(model_id),
            supports_multimodal=self._model_supports_multimodal(model_id),
            native_kind="openai_compatible",
            normalize_content_to_text=True,
        )
