# -*- coding: utf-8 -*-
"""LM Studio provider with native model and capability discovery."""

from __future__ import annotations

from typing import Any

import httpx

from lantai.providers.openai_provider import OpenAIProvider
from lantai.providers.provider import ModelInfo


class LMStudioProvider(OpenAIProvider):
    """Provider implementation for LM Studio local LLM hosting platform."""

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        value = (base_url or "http://localhost:1234").strip().rstrip("/")
        if value.endswith("/v1"):
            return value
        return f"{value}/v1"

    def _native_base_url(self) -> str:
        value = self._normalize_base_url(self.base_url)
        return value[:-3].rstrip("/")

    def model_post_init(self, __context: Any) -> None:
        self.base_url = self._normalize_base_url(self.base_url)

    def update_config(self, config: dict[str, Any]) -> None:
        super().update_config(config)
        self.base_url = self._normalize_base_url(self.base_url)

    def _native_headers(self) -> dict[str, str] | None:
        token = (self.api_key or "").strip()
        if not token:
            return None
        return {"Authorization": f"Bearer {token}"}

    async def _request_native_json(
        self,
        path: str,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        url = f"{self._native_base_url()}{path}"
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, headers=self._native_headers())
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"LM Studio returned an invalid response from {path}")
        return payload

    def _connection_failure_message(self, exc: Exception) -> str:
        endpoint = self._native_base_url()
        if isinstance(exc, httpx.TimeoutException):
            reason = "The request timed out"
        elif isinstance(exc, httpx.HTTPStatusError):
            reason = f"HTTP {exc.response.status_code}"
        else:
            reason = "The service could not be reached"
        return (
            f"{reason} at {endpoint}. In LM Studio, open Developer and "
            "start the local server, then try again."
        )

    @staticmethod
    def _positive_int(value: Any) -> int | None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    async def _native_catalog(
        self,
        *,
        timeout: float,
    ) -> tuple[str, dict[str, Any] | None]:
        try:
            return (
                "v1",
                await self._request_native_json(
                    "/api/v1/models",
                    timeout=timeout,
                ),
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
        try:
            return (
                "v0",
                await self._request_native_json(
                    "/api/v0/models",
                    timeout=timeout,
                ),
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
        return "openai", None

    @classmethod
    def _parse_v1_models(cls, payload: dict[str, Any]) -> list[ModelInfo]:
        rows = payload.get("models")
        if not isinstance(rows, list):
            raise ValueError("LM Studio returned an invalid native v1 catalog.")
        models: list[ModelInfo] = []
        for row in rows:
            if not isinstance(row, dict) or row.get("type") != "llm":
                continue
            model_id = str(row.get("key") or "").strip()
            if not model_id:
                continue
            instances = row.get("loaded_instances")
            loaded_instances = instances if isinstance(instances, list) else []
            loaded_contexts = []
            for instance in loaded_instances:
                if not isinstance(instance, dict):
                    continue
                config = instance.get("config")
                if not isinstance(config, dict):
                    continue
                context = cls._positive_int(config.get("context_length"))
                if context:
                    loaded_contexts.append(context)
            capabilities = row.get("capabilities")
            capability_map = capabilities if isinstance(capabilities, dict) else {}
            quantization = row.get("quantization")
            quantization_map = (
                quantization if isinstance(quantization, dict) else {}
            )
            supports_image = (
                bool(capability_map.get("vision"))
                if capability_map
                else None
            )
            models.append(
                ModelInfo(
                    id=model_id,
                    name=(
                        str(row.get("display_name") or model_id).strip()
                        or model_id
                    ),
                    supports_tool_use=(
                        bool(capability_map.get("trained_for_tool_use"))
                        if capability_map
                        else None
                    ),
                    supports_image=supports_image,
                    supports_video=False if capability_map else None,
                    supports_multimodal=(
                        supports_image if capability_map else None
                    ),
                    probe_source="runtime" if capability_map else None,
                    context_window=max(loaded_contexts) if loaded_contexts else None,
                    max_context_window=cls._positive_int(
                        row.get("max_context_length"),
                    ),
                    capability_source="runtime:lmstudio-native-v1",
                    runtime_state=(
                        "loaded" if loaded_instances else "not-loaded"
                    ),
                    parameter_size=(
                        str(row.get("params_string") or "").strip() or None
                    ),
                    quantization=(
                        str(quantization_map.get("name") or "").strip()
                        or None
                    ),
                    size_bytes=cls._positive_int(row.get("size_bytes")),
                ),
            )
        return models

    @classmethod
    def _parse_v0_models(cls, payload: dict[str, Any]) -> list[ModelInfo]:
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise ValueError("LM Studio returned an invalid native v0 catalog.")
        models: list[ModelInfo] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            model_type = str(row.get("type") or "").lower()
            if model_type in {"embedding", "embeddings"}:
                continue
            model_id = str(row.get("id") or "").strip()
            if not model_id:
                continue
            raw_capabilities = row.get("capabilities")
            capabilities = (
                {
                    str(value).strip().lower()
                    for value in raw_capabilities
                    if str(value).strip()
                }
                if isinstance(raw_capabilities, list)
                else set()
            )
            is_loaded = str(row.get("state") or "").lower() == "loaded"
            supports_image = (
                model_type == "vlm" or "vision" in capabilities
                if model_type or capabilities
                else None
            )
            max_context = cls._positive_int(row.get("max_context_length"))
            models.append(
                ModelInfo(
                    id=model_id,
                    name=model_id,
                    supports_tool_use=(
                        bool({"tools", "tool_use"} & capabilities)
                        if capabilities
                        else None
                    ),
                    supports_image=supports_image,
                    supports_video=False if supports_image is not None else None,
                    supports_multimodal=supports_image,
                    probe_source="runtime" if supports_image is not None else None,
                    context_window=max_context if is_loaded else None,
                    max_context_window=max_context,
                    capability_source="runtime:lmstudio-native-v0",
                    runtime_state="loaded" if is_loaded else "not-loaded",
                    parameter_size=(
                        str(row.get("params_string") or "").strip() or None
                    ),
                    quantization=(
                        str(row.get("quantization") or "").strip() or None
                    ),
                ),
            )
        return models

    async def check_connection(self, timeout: float = 5) -> tuple[bool, str]:
        """Check LM Studio without requiring a model to be loaded."""
        try:
            version, payload = await self._native_catalog(timeout=timeout)
            if version == "openai":
                return await super().check_connection(timeout=timeout)
            models = (
                self._parse_v1_models(payload or {})
                if version == "v1"
                else self._parse_v0_models(payload or {})
            )
        except Exception as exc:
            return False, self._connection_failure_message(exc)
        if not models:
            return (
                True,
                "LM Studio is running, but no local models were found. "
                "Download a model, then sync again.",
            )
        loaded = sum(model.runtime_state == "loaded" for model in models)
        return (
            True,
            f"LM Studio is running with {len(models)} local model(s); "
            f"{loaded} loaded.",
        )

    async def fetch_models(self, timeout: float = 5) -> list[ModelInfo]:
        """Discover downloaded LM Studio models and their agent readiness."""
        try:
            version, payload = await self._native_catalog(timeout=timeout)
            if version == "v1":
                return self._parse_v1_models(payload or {})
            if version == "v0":
                return self._parse_v0_models(payload or {})
            return await super().fetch_models(timeout=timeout)
        except Exception as exc:
            raise ValueError(self._connection_failure_message(exc)) from exc

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
