# -*- coding: utf-8 -*-
"""TokenDance multi-protocol provider backed by its live model catalog."""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import json
import mimetypes
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

import httpx

from ..exceptions import ProviderError, VideoGenerationAttemptException
from .anthropic_provider import AnthropicProvider
from .model_endpoint import ProviderModelEndpoint
from .openai_provider import OpenAIProvider
from .provider import ModelInfo
from .provider_state import TOKENDANCE_VERIFIED_CAPABILITIES_META_KEY
from .tokendance_catalog import CATALOG
from .tokendance_ecosystem import (
    CatalogLayers,
    LiveCatalogSnapshot,
    LiveModel,
    RoutingPolicy,
    VerifiedCapabilitiesSnapshot,
    VerifiedCapability,
    resolve_protocol,
)
from .tokendance_oauth import TOKENDANCE_APP_URL

TOKENDANCE_OPENAI_BASE_URL = "https://tokendance.space/gateway/v1"
TOKENDANCE_ANTHROPIC_BASE_URL = "https://tokendance.space/gateway"
TOKENDANCE_ARK_BASE_URL = "https://tokendance.space/gateway/ark/v3"

_OPENAI_CHAT = "openai:chat-completions"
_OPENAI_RESPONSES = "openai:responses"
_ANTHROPIC_MESSAGES = "anthropic:messages"
_OPENAI_IMAGES = "openai:image-generations"
_ARK_IMAGES = "ark:image-generations"


@dataclass(frozen=True, slots=True)
class _VideoRequestProfile:
    protocol: str
    submit_url: str
    query_url_template: str
    payload: dict[str, Any]


class VideoSubmissionObserver(Protocol):
    """Runtime-owned durable hooks around one asynchronous provider task."""

    def before_submit(self) -> None: ...

    def response_received(self) -> None: ...

    def submission_unknown(self) -> None: ...

    def submitted(self, task_id: str) -> None: ...

    def succeeded(self, result_hash: str) -> None: ...

    def failed(self, failure_hash: str) -> None: ...


def _provider_payload_hash(payload: Any) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    value = getattr(row, key, default)
    if value is not default:
        return value
    extra = getattr(row, "model_extra", None)
    return extra.get(key, default) if isinstance(extra, dict) else default


class TokenDanceProvider(OpenAIProvider):
    """Route each TokenDance model through a catalog-supported protocol."""

    supports_video_generation: bool = True

    def _default_headers(self) -> dict[str, str]:
        headers = dict(super()._default_headers() or {})
        headers["X-App-URL"] = TOKENDANCE_APP_URL
        return headers

    @staticmethod
    def _normalize_models_payload(payload: Any) -> list[ModelInfo]:
        rows = (
            payload.get("data", [])
            if isinstance(payload, dict)
            else getattr(
                payload,
                "data",
                payload,
            )
        )
        models: list[ModelInfo] = []
        seen: set[str] = set()
        for row in rows or []:
            model_id = str(_row_value(row, "id", "") or "").strip()
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            model_name = (
                str(
                    _row_value(row, "name", "") or model_id,
                ).strip()
                or model_id
            )
            raw_protocols = _row_value(row, "supported_protocols", [])
            protocols = [
                str(protocol).strip()
                for protocol in (raw_protocols or [])
                if str(protocol).strip()
            ]
            raw_context = _row_value(row, "context_length", None)
            try:
                context_window = int(raw_context) if raw_context else None
            except (TypeError, ValueError):
                context_window = None
            models.append(
                ModelInfo(
                    id=model_id,
                    name=model_name,
                    supported_protocols=protocols,
                    context_window=context_window,
                    max_context_window=context_window,
                    capability_source="provider-catalog",
                    show_in_chat=False,
                ),
            )
        return models

    async def fetch_models(self, timeout: float = 5) -> list[ModelInfo]:
        """Read TokenDance's public live catalog without sending the API key."""

        url = f"{self.base_url.rstrip('/')}/models"
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
            response.raise_for_status()
            return self._normalize_models_payload(response.json())

    def _protocols_for(self, model_id: str) -> list[str]:
        model = self.get_model_info(model_id)
        if model and model.supported_protocols:
            return list(model.supported_protocols)
        if model_id in CATALOG:
            return list(CATALOG[model_id]["supported_protocols"])
        return []

    def _protocol_source_for(self, model_id: str) -> str:
        model = self.get_model_info(model_id)
        return (
            "live_or_configured"
            if model and model.supported_protocols
            else "verified_release_fallback"
        )

    @staticmethod
    def _purposes_for_protocols(protocols: list[str]) -> tuple[str, ...]:
        purposes: list[str] = []
        if any(
            protocol
            in {_OPENAI_CHAT, _OPENAI_RESPONSES, _ANTHROPIC_MESSAGES}
            for protocol in protocols
        ):
            purposes.append("chat")
        if any("image-generation" in protocol for protocol in protocols):
            purposes.append("image")
        if any(
            marker in protocol
            for protocol in protocols
            for marker in ("video", "generations")
        ):
            purposes.append("video")
        return tuple(purposes)

    def _catalog_layers(self) -> CatalogLayers:
        live = LiveCatalogSnapshot.create(
            (
                LiveModel(
                    model_id=model.id,
                    # A pre-v3 provider row has no protocol field. Keep that
                    # row usable with the release-owned catalogue while a
                    # fresh provider response remains authoritative whenever
                    # it supplies protocols explicitly.
                    protocols=self._protocols_for(model.id),
                    context_window=model.context_window,
                )
                for model in self.models + self.extra_models
            ),
            source="tokendance-provider-state",
            fetched_at=datetime.now(timezone.utc),
            source_version=str(self.configuration_revision),
        )
        verified_payload = self.meta.get(
            TOKENDANCE_VERIFIED_CAPABILITIES_META_KEY,
            {},
        )
        verified_rows = (
            verified_payload.get("models", {})
            if isinstance(verified_payload, dict)
            else {}
        )
        revision = (
            str(verified_payload.get("revision") or "")
            if isinstance(verified_payload, dict)
            else ""
        )
        if not verified_rows:
            # Migration is intentionally fail-safe: a disk write can fail and
            # leave the original provider JSON in place. The bundled catalogue
            # is the same release verification evidence the migration would
            # attach, so use it in memory without mutating user state. Models
            # absent from this allowlist remain unverified and unroutable.
            verified_rows = CATALOG
            revision = "release-2026-09-12"
        verified = []
        for model_id, row in verified_rows.items():
            if not isinstance(row, dict):
                continue
            protocols = [
                str(item).strip()
                for item in row.get("supported_protocols", [])
                if str(item).strip()
            ]
            verified.append(
                VerifiedCapability(
                    model_id=str(model_id),
                    protocols=protocols,
                    purposes=self._purposes_for_protocols(protocols),
                    evidence_ids=(
                        f"tokendance-capability:{revision or 'unversioned'}:{model_id}",
                    ),
                )
            )
        return CatalogLayers(
            live=live,
            verified=VerifiedCapabilitiesSnapshot.create(
                verified,
                revision=revision,
            ),
        )

    def resolve_model_endpoint(self, model_id: str) -> ProviderModelEndpoint:
        protocols = self._protocols_for(model_id)
        layers = self._catalog_layers()
        try:
            decision = resolve_protocol(
                layers=layers,
                model_id=model_id,
                purpose="chat",
                policy=RoutingPolicy(
                    allowed_data_scope="tokendance",
                    allow_provider_fallback=False,
                ),
            )
        except ValueError as error:
            raise ProviderError(
                message=(
                    f"TokenDance model '{model_id}' does not support a chat "
                    "protocol. Select it in the matching media feature instead."
                ),
                details={
                    "provider_id": self.id,
                    "model_id": model_id,
                    "routing_error": str(error),
                },
            ) from error
        routing_snapshot = {
            "model_id": decision.model_id,
            "protocol": decision.protocol,
            "endpoint_family": decision.endpoint_family,
            "native_kind": decision.native_kind,
            "purpose": decision.purpose,
            "adapter_version": decision.adapter_version,
            "allowed_data_scope": decision.allowed_data_scope,
            "allow_provider_fallback": decision.allow_provider_fallback,
            "reasons": list(decision.reasons),
            "catalog_revision": layers.live.catalog_hash,
            "verification_revision": layers.verified.revision,
        }
        metadata = {
            "supported_protocols": protocols,
            "protocol_source": self._protocol_source_for(model_id),
            "catalog_revision": layers.live.catalog_hash,
            "verification_revision": layers.verified.revision,
            "routing_snapshot": routing_snapshot,
        }
        if decision.native_kind == "openai_compatible":
            return replace(
                super().resolve_model_endpoint(model_id),
                metadata=metadata,
            )
        if decision.native_kind == "openai_responses":
            client_kwargs = self._chat_client_kwargs()
            return ProviderModelEndpoint(
                provider_id=self.id,
                model_id=model_id,
                family="openai",
                base_url=str(client_kwargs.get("base_url") or self.base_url),
                api_key=self.api_key,
                is_local=False,
                chat_model="OpenAIChatModel",
                generate_kwargs=self.get_effective_generate_kwargs(model_id),
                default_headers=client_kwargs.get("default_headers"),
                supports_multimodal=self._model_supports_multimodal(model_id),
                native_kind="openai_responses",
                metadata=metadata,
            )
        if decision.native_kind == "anthropic_messages":
            return ProviderModelEndpoint(
                provider_id=self.id,
                model_id=model_id,
                family="anthropic",
                base_url=TOKENDANCE_ANTHROPIC_BASE_URL,
                api_key=self.api_key,
                is_local=False,
                chat_model="AnthropicChatModel",
                generate_kwargs=self.get_effective_generate_kwargs(model_id),
                default_headers=self._default_headers(),
                supports_multimodal=self._model_supports_multimodal(model_id),
                native_kind="legacy",
                metadata=metadata,
            )
        raise AssertionError(f"unsupported routed protocol: {decision.protocol}")

    def _anthropic_delegate(self) -> AnthropicProvider:
        return AnthropicProvider(
            id=self.id,
            name=self.name,
            base_url=TOKENDANCE_ANTHROPIC_BASE_URL,
            api_key=self.api_key,
            models=list(self.models),
            extra_models=list(self.extra_models),
            chat_model="AnthropicChatModel",
            generate_kwargs=dict(self.generate_kwargs),
            meta={"default_headers": self._default_headers()},
        )

    def get_chat_model_instance(self, model_id: str) -> Any:
        endpoint = self.resolve_model_endpoint(model_id)
        if endpoint.family == "anthropic":
            return self._anthropic_delegate().get_chat_model_instance(model_id)
        return super().get_chat_model_instance(model_id)

    async def check_model_connection(
        self,
        model_id: str,
        timeout: float = 5,
    ) -> tuple[bool, str]:
        try:
            endpoint = self.resolve_model_endpoint(model_id)
        except ProviderError:
            return False, f"Model '{model_id}' is not a chat model"
        if endpoint.family == "anthropic":
            return await self._anthropic_delegate().check_model_connection(
                model_id,
                timeout=timeout,
            )
        if endpoint.native_kind == "openai_responses":
            try:
                stream = await self._client(timeout=timeout).responses.create(
                    model=model_id,
                    input="ping",
                    max_output_tokens=8,
                    stream=True,
                    timeout=timeout,
                )
                async for _ in stream:
                    break
                return True, ""
            except Exception:
                return False, f"Model '{model_id}' is not reachable or usable"
        # Chat Completions remains the least surprising lightweight probe when
        # the catalog advertises both OpenAI protocols.
        return await super().check_model_connection(model_id, timeout=timeout)

    @staticmethod
    def _normalize_ark_image_response(payload: Any) -> dict[str, Any]:
        record = payload if isinstance(payload, dict) else {}
        rows = record.get("data") or []
        assets: list[dict[str, Any]] = []
        data: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            item: dict[str, Any] = {}
            url = str(row.get("url") or "").strip()
            b64_json = str(row.get("b64_json") or "").strip()
            if url:
                item["url"] = url
                assets.append({"type": "image", "url": url})
            if b64_json:
                item["b64_json"] = b64_json
                assets.append(
                    {
                        "type": "image",
                        "url": f"data:image/png;base64,{b64_json}",
                        "mimeType": "image/png",
                    },
                )
            if item:
                data.append(item)
        return {"assets": assets, "data": data, "raw": record}

    async def generate_image(
        self,
        model_id: str,
        prompt: str,
        n: int = 1,
        size: str = "1024x1024",
        quality: str | None = "auto",
        response_format: str | None = None,
        timeout: float = 120,
    ) -> dict[str, Any]:
        protocols = self._protocols_for(model_id)
        if not protocols or _OPENAI_IMAGES in protocols:
            return await super().generate_image(
                model_id,
                prompt,
                n=n,
                size=size,
                quality=quality,
                response_format=response_format,
                timeout=timeout,
            )
        if _ARK_IMAGES not in protocols:
            raise ValueError(
                f"TokenDance model '{model_id}' does not support image generation",
            )
        clean_prompt = (prompt or "").strip()
        if not clean_prompt:
            raise ValueError("Empty image prompt")
        payload = {
            "model": model_id,
            "prompt": clean_prompt,
            "size": size or "2K",
            "output_format": "png",
            "response_format": response_format or "url",
            "watermark": False,
        }
        return await self._request_ark_images(
            payload,
            n=n,
            timeout=timeout,
        )

    async def _request_ark_images(
        self,
        payload: dict[str, Any],
        *,
        n: int,
        timeout: float,
    ) -> dict[str, Any]:
        result = {"assets": [], "data": [], "raw": {}}
        quantity = max(1, min(4, int(n or 1)))
        async with httpx.AsyncClient(timeout=timeout) as client:
            for _ in range(quantity):
                response = await client.post(
                    f"{TOKENDANCE_ARK_BASE_URL}/images/generations",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "X-App-URL": TOKENDANCE_APP_URL,
                    },
                    json=payload,
                )
                response.raise_for_status()
                normalized = self._normalize_ark_image_response(response.json())
                result["assets"].extend(normalized["assets"])
                result["data"].extend(normalized["data"])
                result["raw"] = normalized["raw"]
        result["requested_size"] = str(payload.get("size") or "2K")
        result["effective_size"] = str(payload.get("size") or "2K")
        return result

    @staticmethod
    def _reference_data_url(path_value: str) -> str:
        path = Path(path_value).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"Image editing reference does not exist: {path}")
        mime_type = mimetypes.guess_type(path.name)[0] or "image/png"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    async def edit_image(
        self,
        model_id: str,
        prompt: str,
        image_paths: list[str],
        n: int = 1,
        size: str = "1024x1024",
        quality: str | None = "auto",
        response_format: str | None = None,
        timeout: float = 120,
    ) -> dict[str, Any]:
        protocols = self._protocols_for(model_id)
        if not protocols:
            return await super().edit_image(
                model_id,
                prompt,
                image_paths,
                n=n,
                size=size,
                quality=quality,
                response_format=response_format,
                timeout=timeout,
            )
        if _ARK_IMAGES not in protocols and _OPENAI_IMAGES in protocols:
            return await super().edit_image(
                model_id,
                prompt,
                image_paths,
                n=n,
                size=size,
                quality=quality,
                response_format=response_format,
                timeout=timeout,
            )
        if _ARK_IMAGES not in protocols:
            raise ValueError(
                f"TokenDance model '{model_id}' does not support image editing",
            )
        clean_prompt = (prompt or "").strip()
        if not clean_prompt:
            raise ValueError("Empty image prompt")
        references = [self._reference_data_url(value) for value in image_paths]
        if not references:
            raise ValueError("Image editing requires at least one reference image")
        payload = {
            "model": model_id,
            "prompt": clean_prompt,
            "image": references[0] if len(references) == 1 else references,
            "size": size or "2K",
            "output_format": "png",
            "response_format": response_format or "url",
            "watermark": False,
        }
        return await self._request_ark_images(
            payload,
            n=n,
            timeout=timeout,
        )

    @staticmethod
    def _video_request_profile(
        *,
        protocol: str,
        model_id: str,
        prompt: str,
        duration: int,
        aspect_ratio: str,
        resolution: str,
    ) -> _VideoRequestProfile:
        root = "https://tokendance.space"
        if protocol == "seedance:generations":
            return _VideoRequestProfile(
                protocol=protocol,
                submit_url=f"{root}/gateway/ark/v3/generations/tasks",
                query_url_template=(
                    f"{root}/gateway/ark/v3/generations/tasks/{{task_id}}"
                ),
                payload={
                    "model": model_id,
                    "content": [{"type": "text", "text": prompt}],
                    "resolution": resolution.lower(),
                    "ratio": aspect_ratio,
                    "duration": max(4, duration),
                },
            )
        if protocol in {"kling:text2video", "kling:omni-video"}:
            endpoint = "text2video" if protocol == "kling:text2video" else "omni-video"
            contents = [{"type": "prompt", "text": prompt}]
            return _VideoRequestProfile(
                protocol=protocol,
                submit_url=f"{root}/gateway/kling/v1/{endpoint}",
                query_url_template=(f"{root}/gateway/kling/v1/{endpoint}/{{task_id}}"),
                payload={
                    "model_name": model_id,
                    **(
                        {"prompt": prompt}
                        if protocol == "kling:text2video"
                        else {"contents": contents}
                    ),
                    "settings": {
                        "resolution": resolution.lower(),
                        "duration": duration,
                        "aspect_ratio": aspect_ratio,
                        **({"audio": "off"} if protocol == "kling:omni-video" else {}),
                    },
                    **(
                        {"options": {"watermark_info": {"enabled": False}}}
                        if protocol == "kling:text2video"
                        else {}
                    ),
                },
            )
        if protocol == "wan3:video-synthesis":
            return _VideoRequestProfile(
                protocol=protocol,
                submit_url=(f"{root}/gateway/alibaba/wan3/v1/video-synthesis"),
                query_url_template=(
                    f"{root}/gateway/alibaba/wan3/v1/tasks/{{task_id}}"
                ),
                payload={
                    "model": model_id,
                    "input": {"prompt": prompt},
                    "parameters": {
                        "resolution": resolution.upper(),
                        "ratio": aspect_ratio,
                        "duration": duration,
                        "audio": False,
                        "watermark": False,
                    },
                },
            )
        if protocol == "happyhorse:video-synthesis":
            return _VideoRequestProfile(
                protocol=protocol,
                submit_url=(f"{root}/gateway/alibaba/happyhorse/v1/video-synthesis"),
                query_url_template=(
                    f"{root}/gateway/alibaba/happyhorse/v1/tasks/{{task_id}}"
                ),
                payload={
                    "model": model_id,
                    "input": {"prompt": prompt},
                    "parameters": {
                        "size": (
                            "1280*720"
                            if aspect_ratio == "16:9"
                            else "720*1280"
                            if aspect_ratio == "9:16"
                            else "1024*1024"
                        ),
                        "duration": duration,
                    },
                },
            )
        if protocol == "minimax:video_generation_v2":
            return _VideoRequestProfile(
                protocol=protocol,
                submit_url=f"{root}/gateway/minimax/v2/video_generation",
                query_url_template=(
                    f"{root}/gateway/minimax/v2/query/video_generation/{{task_id}}"
                ),
                payload={
                    "model": model_id,
                    "resolution": (
                        "768P" if resolution.lower() == "720p" else resolution.upper()
                    ),
                    "duration": 6 if duration < 6 else duration,
                    "ratio": aspect_ratio,
                    "content": [{"type": "text", "text": prompt}],
                },
            )
        raise ValueError(
            f"TokenDance protocol '{protocol}' is not supported for video tasks",
        )

    @staticmethod
    def _nested(payload: Any, *path: str) -> Any:
        current = payload
        for key in path:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current

    @classmethod
    def _video_task_id(cls, payload: dict[str, Any]) -> str:
        candidates = (
            payload.get("id"),
            payload.get("task_id"),
            cls._nested(payload, "data", "id"),
            cls._nested(payload, "output", "task_id"),
            cls._nested(payload, "task", "id"),
            cls._nested(payload, "task", "task_id"),
        )
        return next((str(value) for value in candidates if value), "")

    @classmethod
    def _video_status(cls, payload: dict[str, Any]) -> str:
        candidates = (
            payload.get("status"),
            cls._nested(payload, "task", "status"),
            cls._nested(payload, "output", "task_status"),
        )
        return next(
            (str(value).strip().lower() for value in candidates if value),
            "",
        )

    @classmethod
    def _video_url(cls, payload: dict[str, Any]) -> str:
        candidates = (
            cls._nested(payload, "content", "video_url"),
            cls._nested(payload, "task", "content", "url"),
            cls._nested(payload, "output", "video_url"),
        )
        for value in candidates:
            if value:
                return str(value)
        data = payload.get("data")
        rows = data if isinstance(data, list) else [data]
        for row in rows:
            if not isinstance(row, dict):
                continue
            outputs = row.get("outputs")
            if not isinstance(outputs, list):
                continue
            for output in outputs:
                if (
                    isinstance(output, dict)
                    and output.get("type") == "video"
                    and output.get("url")
                ):
                    return str(output["url"])
        creations = payload.get("creations")
        if isinstance(creations, list):
            for creation in creations:
                if isinstance(creation, dict) and creation.get("url"):
                    return str(creation["url"])
        return ""

    def _video_protocol_for(self, model_id: str) -> str:
        protocols = self._protocols_for(model_id)
        priority = (
            "seedance:generations",
            "kling:text2video",
            "kling:omni-video",
            "wan3:video-synthesis",
            "happyhorse:video-synthesis",
            "minimax:video_generation_v2",
        )
        selected = next(
            (protocol for protocol in priority if protocol in protocols), ""
        )
        if selected == "happyhorse:video-synthesis" and "t2v" not in model_id.lower():
            return ""
        return selected

    async def generate_video(
        self,
        model_id: str,
        prompt: str,
        *,
        duration: int = 5,
        aspect_ratio: str = "16:9",
        resolution: str = "720p",
        timeout: float = 600,
        poll_interval: float = 5,
        submission_observer: VideoSubmissionObserver | None = None,
    ) -> dict[str, Any]:
        """Submit exactly one text-to-video task and poll that task only."""

        clean_prompt = (prompt or "").strip()
        if not clean_prompt:
            raise ValueError("Empty video prompt")
        protocol = self._video_protocol_for(model_id)
        if not protocol:
            raise ValueError(
                f"TokenDance model '{model_id}' has no supported text-to-video protocol",
            )
        profile = self._video_request_profile(
            protocol=protocol,
            model_id=model_id,
            prompt=clean_prompt,
            duration=max(2, min(30, int(duration))),
            aspect_ratio=aspect_ratio,
            resolution=resolution,
        )
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-App-URL": TOKENDANCE_APP_URL,
        }
        if protocol == "happyhorse:video-synthesis":
            headers["X-DashScope-Async"] = "enable"
        deadline = time.monotonic() + max(10, float(timeout))
        async with httpx.AsyncClient(timeout=min(30, timeout)) as client:
            if submission_observer is not None:
                submission_observer.before_submit()
            try:
                submitted = await client.post(
                    profile.submit_url,
                    headers=headers,
                    json=profile.payload,
                )
            except Exception:
                if submission_observer is not None:
                    submission_observer.submission_unknown()
                raise VideoGenerationAttemptException(
                    "Video task submission outcome is unknown.",
                ) from None
            if submission_observer is not None:
                try:
                    submission_observer.response_received()
                except Exception:
                    submission_observer.submission_unknown()
                    raise
            try:
                submitted.raise_for_status()
            except Exception:
                if submission_observer is not None:
                    submission_observer.submission_unknown()
                raise VideoGenerationAttemptException(
                    "Video task submission was not accepted.",
                    response=submitted,
                ) from None
            try:
                task_id = self._video_task_id(submitted.json())
            except Exception:
                if submission_observer is not None:
                    submission_observer.submission_unknown()
                raise VideoGenerationAttemptException(
                    "Video task submission returned an invalid response.",
                    response=submitted,
                ) from None
            if not task_id:
                if submission_observer is not None:
                    submission_observer.submission_unknown()
                raise VideoGenerationAttemptException(
                    "Video task submission returned no task ID.",
                    response=submitted,
                )
            if submission_observer is not None:
                submission_observer.submitted(task_id)

            query_url = profile.query_url_template.format(task_id=task_id)
            while True:
                try:
                    queried = await client.get(query_url, headers=headers)
                except Exception:
                    raise VideoGenerationAttemptException(
                        "Video task status could not be read.",
                        task_id=task_id,
                    ) from None
                if submission_observer is not None:
                    submission_observer.response_received()
                try:
                    queried.raise_for_status()
                except Exception:
                    raise VideoGenerationAttemptException(
                        "Video task status request failed.",
                        task_id=task_id,
                        response=queried,
                    ) from None
                try:
                    payload = queried.json()
                except Exception:
                    raise VideoGenerationAttemptException(
                        "Video task status returned an invalid response.",
                        task_id=task_id,
                        response=queried,
                    ) from None
                video_url = self._video_url(payload)
                status = self._video_status(payload)
                if video_url and status in {"", "succeeded", "success", "completed"}:
                    if submission_observer is not None:
                        submission_observer.succeeded(_provider_payload_hash(payload))
                    return {
                        "task_id": task_id,
                        "protocol": protocol,
                        "assets": [{"type": "video", "url": video_url}],
                        "raw": payload,
                    }
                if status in {"failed", "cancelled", "canceled", "expired"}:
                    if submission_observer is not None:
                        submission_observer.failed(_provider_payload_hash(payload))
                    raise VideoGenerationAttemptException(
                        "Video task reached an explicit terminal failure.",
                        task_id=task_id,
                        may_fail_over=True,
                        response=queried,
                    )
                if status in {"succeeded", "success", "completed"}:
                    if submission_observer is not None:
                        submission_observer.failed(_provider_payload_hash(payload))
                    raise VideoGenerationAttemptException(
                        "Video task completed without a video asset.",
                        task_id=task_id,
                        response=queried,
                    )
                if status == "unknown":
                    raise VideoGenerationAttemptException(
                        "Video task returned an unknown status.",
                        task_id=task_id,
                        response=queried,
                    )
                if time.monotonic() >= deadline:
                    raise VideoGenerationAttemptException(
                        "Video task is still running after the client deadline.",
                        task_id=task_id,
                    )
                await asyncio.sleep(max(1, float(poll_interval)))
