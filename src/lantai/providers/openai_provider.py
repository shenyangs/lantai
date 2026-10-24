# -*- coding: utf-8 -*-
"""An OpenAI provider implementation."""

from __future__ import annotations

import importlib.util
import json
import logging
import math
import os
import re
import time
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any, List

from openai import APIError

from lantai.providers.provider import ModelInfo, Provider
from lantai.providers.model_endpoint import ProviderModelEndpoint
from lantai.providers.request_profiles import (
    KIMI_CODING_MODEL_ID,
    default_headers_for_base_url,
    is_kimi_coding_base_url,
)

if TYPE_CHECKING:
    from lantai.providers.multimodal_prober import ProbeResult

logger = logging.getLogger(__name__)

DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
CODING_DASHSCOPE_BASE_URL = "https://coding.dashscope.aliyuncs.com/v1"
TOKEN_PLAN_BASE_URL = (
    "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
)
_EXPLICIT_IMAGE_SIZE_RE = re.compile(
    r"^\s*(?P<width>\d+)\s*[x×]\s*(?P<height>\d+)\s*$",
    re.IGNORECASE,
)
_MINIMUM_IMAGE_PIXELS_RE = re.compile(
    r"(?:at\s+least|minimum(?:\s+of)?)\s+"
    r"(?P<pixels>\d[\d,_]*)\s+pixels?",
    re.IGNORECASE,
)
_IMAGE_SIZE_ALIGNMENT = 32
if os.environ.get("LANGFUSE_SECRET_KEY") and importlib.util.find_spec(
    "langfuse",
):
    from langfuse.openai import AsyncOpenAI  # type: ignore[import]
else:
    if os.environ.get("LANGFUSE_SECRET_KEY"):
        logger.warning(
            "LANGFUSE_SECRET_KEY is set but langfuse is not installed; "
            "install with `pip install langfuse` to enable tracing",
        )
    from openai import AsyncOpenAI  # pylint: disable=ungrouped-imports


class OpenAIProvider(Provider):
    """Provider implementation for OpenAI API and compatible endpoints."""

    def _is_kimi_coding_endpoint(self) -> bool:
        return is_kimi_coding_base_url(self.base_url)

    def _default_headers(self) -> dict[str, str] | None:
        """Return provider-specific default headers, if any."""
        return default_headers_for_base_url(self.base_url)

    @staticmethod
    def _format_api_error(exc: APIError) -> str:
        status_code = getattr(exc, "status_code", None)
        code = getattr(exc, "code", None)
        message = str(exc).strip() or exc.__class__.__name__
        parts = []
        if status_code:
            parts.append(f"HTTP {status_code}")
        if code:
            parts.append(str(code))
        parts.append(message)
        return " / ".join(parts)

    @staticmethod
    def _retry_size_for_minimum_pixel_error(
        size: str,
        exc: APIError,
    ) -> str | None:
        """Upscale an explicit size when an endpoint reports a pixel floor."""

        size_match = _EXPLICIT_IMAGE_SIZE_RE.match(str(size or ""))
        pixels_match = _MINIMUM_IMAGE_PIXELS_RE.search(str(exc))
        if not size_match or not pixels_match:
            return None

        width = int(size_match.group("width"))
        height = int(size_match.group("height"))
        minimum_pixels = int(
            pixels_match.group("pixels").replace(",", "").replace("_", ""),
        )
        current_pixels = width * height
        if width <= 0 or height <= 0 or minimum_pixels <= current_pixels:
            return None

        scale = math.sqrt(minimum_pixels / current_pixels)
        aligned_width = (
            math.ceil((width * scale) / _IMAGE_SIZE_ALIGNMENT)
            * _IMAGE_SIZE_ALIGNMENT
        )
        aligned_height = (
            math.ceil((height * scale) / _IMAGE_SIZE_ALIGNMENT)
            * _IMAGE_SIZE_ALIGNMENT
        )
        return f"{aligned_width}x{aligned_height}"

    def _requires_plain_text_content(self) -> bool:
        """Whether this provider should receive string-only message content."""
        return bool(self.is_local)

    def _client(self, timeout: float = 5) -> AsyncOpenAI:
        kwargs: dict[str, Any] = {
            "base_url": self.base_url,
            "api_key": self.api_key,
            "timeout": timeout,
        }
        headers = self._default_headers()
        if headers:
            kwargs["default_headers"] = headers
        return AsyncOpenAI(**kwargs)

    def _chat_client_kwargs(self) -> dict[str, Any]:
        """Return chat-completions client kwargs shared by old/new clients."""

        client_kwargs: dict[str, Any] = {"base_url": self.base_url}

        if self.base_url == DASHSCOPE_BASE_URL:
            client_kwargs["default_headers"] = {
                "x-dashscope-agentapp": json.dumps(
                    {
                        "agentType": "Lantai",
                        "deployType": "UnKnown",
                        "moduleCode": "model",
                        "agentCode": "UnKnown",
                    },
                    ensure_ascii=False,
                ),
            }
        elif self.base_url == CODING_DASHSCOPE_BASE_URL:
            client_kwargs["default_headers"] = {
                "X-DashScope-Cdpl": json.dumps(
                    {
                        "agentType": "Lantai",
                        "deployType": "UnKnown",
                        "moduleCode": "model",
                        "agentCode": "UnKnown",
                    },
                    ensure_ascii=False,
                ),
            }
        elif self.base_url == TOKEN_PLAN_BASE_URL:
            client_kwargs["default_headers"] = {
                "X-DashScope-Cdpl": json.dumps(
                    {
                        "agentType": "Lantai",
                        "deployType": "UnKnown",
                        "moduleCode": "model",
                        "agentCode": "UnKnown",
                    },
                    ensure_ascii=False,
                ),
            }

        headers = self._default_headers()
        if headers:
            client_kwargs.setdefault("default_headers", {})
            client_kwargs["default_headers"].update(headers)

        return client_kwargs

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
        """Generate images through an OpenAI-compatible Images API."""

        clean_prompt = (prompt or "").strip()
        if not clean_prompt:
            raise ValueError("Empty image prompt")

        quantity = max(1, min(4, int(n or 1)))
        effective_kwargs = self.get_effective_generate_kwargs(model_id)
        image_kwargs: dict[str, Any] = {}
        nested_kwargs = effective_kwargs.get("image_generation")
        if isinstance(nested_kwargs, dict):
            image_kwargs.update(nested_kwargs)

        payload: dict[str, Any] = dict(image_kwargs)
        payload.update(
            {
                "model": model_id,
                "prompt": clean_prompt,
                "n": quantity,
                "size": size or "1024x1024",
            },
        )
        if quality and quality != "auto":
            payload["quality"] = quality
        if response_format:
            payload["response_format"] = response_format

        requested_size = str(payload["size"])
        effective_size = requested_size
        client = self._client(timeout=timeout)
        try:
            response = await client.images.generate(
                **payload,
                timeout=timeout,
            )
        except APIError as exc:
            retry_size = self._retry_size_for_minimum_pixel_error(
                effective_size,
                exc,
            )
            if not retry_size:
                raise ValueError(
                    f"Image generation failed: {self._format_api_error(exc)}",
                ) from exc
            effective_size = retry_size
            payload["size"] = effective_size
            logger.info(
                "Retrying image generation after provider pixel-floor error: "
                "%s -> %s",
                requested_size,
                effective_size,
            )
            try:
                response = await client.images.generate(
                    **payload,
                    timeout=timeout,
                )
            except APIError as retry_exc:
                raise ValueError(
                    "Image generation failed after automatic size adjustment: "
                    f"{self._format_api_error(retry_exc)}",
                ) from retry_exc

        result = self._normalize_image_response(response)
        result["requested_size"] = requested_size
        result["effective_size"] = effective_size
        return result

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
        """Edit one or more local reference images through the Images API."""

        clean_prompt = (prompt or "").strip()
        if not clean_prompt:
            raise ValueError("Empty image prompt")
        paths = [Path(value).expanduser().resolve() for value in image_paths]
        if not paths:
            raise ValueError("Image editing requires at least one reference image")
        if any(not path.is_file() for path in paths):
            raise ValueError("Image editing references must be local files")

        quantity = max(1, min(4, int(n or 1)))
        effective_kwargs = self.get_effective_generate_kwargs(model_id)
        image_kwargs: dict[str, Any] = {}
        nested_kwargs = effective_kwargs.get("image_generation")
        if isinstance(nested_kwargs, dict):
            image_kwargs.update(nested_kwargs)
        payload: dict[str, Any] = dict(image_kwargs)
        payload.update(
            {
                "model": model_id,
                "prompt": clean_prompt,
                "n": quantity,
                "size": size or "1024x1024",
            },
        )
        if quality and quality != "auto":
            payload["quality"] = quality
        if response_format:
            payload["response_format"] = response_format

        requested_size = str(payload["size"])
        effective_size = requested_size
        client = self._client(timeout=timeout)

        async def request_edit() -> Any:
            with ExitStack() as stack:
                images = [stack.enter_context(path.open("rb")) for path in paths]
                return await client.images.edit(
                    image=images,
                    **payload,
                    timeout=timeout,
                )

        try:
            response = await request_edit()
        except APIError as exc:
            retry_size = self._retry_size_for_minimum_pixel_error(
                effective_size,
                exc,
            )
            if not retry_size:
                raise ValueError(
                    f"Image editing failed: {self._format_api_error(exc)}",
                ) from exc
            effective_size = retry_size
            payload["size"] = effective_size
            logger.info(
                "Retrying image editing after provider pixel-floor error: "
                "%s -> %s",
                requested_size,
                effective_size,
            )
            try:
                response = await request_edit()
            except APIError as retry_exc:
                raise ValueError(
                    "Image editing failed after automatic size adjustment: "
                    f"{self._format_api_error(retry_exc)}",
                ) from retry_exc

        result = self._normalize_image_response(response)
        result["requested_size"] = requested_size
        result["effective_size"] = effective_size
        return result

    @staticmethod
    def _normalize_image_response(response: Any) -> dict[str, Any]:
        data: list[dict[str, Any]] = []
        assets: list[dict[str, Any]] = []
        rows = getattr(response, "data", []) or []
        for row in rows:
            url = str(getattr(row, "url", "") or "").strip()
            b64_json = str(getattr(row, "b64_json", "") or "").strip()
            revised_prompt = str(
                getattr(row, "revised_prompt", "") or "",
            ).strip()
            item: dict[str, Any] = {}
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
            if revised_prompt:
                item["revised_prompt"] = revised_prompt
            if item:
                data.append(item)

        raw = response.model_dump() if hasattr(response, "model_dump") else {}
        return {
            "created": int(getattr(response, "created", 0) or time.time()),
            "assets": assets,
            "data": data,
            "raw": raw if isinstance(raw, dict) else {},
        }

    @staticmethod
    def _normalize_models_payload(payload: Any) -> List[ModelInfo]:
        models: List[ModelInfo] = []
        rows = getattr(payload, "data", [])
        for row in rows or []:
            model_id = str(getattr(row, "id", "") or "").strip()
            if not model_id:
                continue
            model_name = (
                str(getattr(row, "name", "") or model_id).strip() or model_id
            )
            models.append(ModelInfo(id=model_id, name=model_name))

        deduped: List[ModelInfo] = []
        seen: set[str] = set()
        for model in models:
            if model.id in seen:
                continue
            seen.add(model.id)
            deduped.append(model)
        return deduped

    async def check_connection(self, timeout: float = 5) -> tuple[bool, str]:
        """Check if OpenAI provider is reachable with current configuration."""
        if self._is_kimi_coding_endpoint():
            return await self.check_model_connection(
                KIMI_CODING_MODEL_ID,
                timeout=timeout,
            )
        client = self._client()
        try:
            await client.models.list(timeout=timeout)
            return True, ""
        except APIError as exc:
            return (
                False,
                f"API error when connecting to `{self.base_url}`: "
                f"{self._format_api_error(exc)}",
            )
        except Exception:
            return (
                False,
                f"Unknown exception when connecting to `{self.base_url}`",
            )

    async def fetch_models(self, timeout: float = 5) -> List[ModelInfo]:
        """Fetch available models."""
        if self._is_kimi_coding_endpoint():
            return list(self.models)
        try:
            client = self._client(timeout=timeout)
            payload = await client.models.list(timeout=timeout)
            models = self._normalize_models_payload(payload)
            return models
        except APIError:
            return []
        except Exception:
            return []

    async def check_model_connection(
        self,
        model_id: str,
        timeout: float = 5,
    ) -> tuple[bool, str]:
        """Check if a specific model is reachable/usable"""
        model_id = (model_id or "").strip()
        if not model_id:
            return False, "Empty model ID"

        try:
            client = self._client(timeout=timeout)
            # Some local servers reject OpenAI content arrays even when the
            # array only contains text blocks.
            if self._requires_plain_text_content():
                content = "ping"
            else:
                content = [{"type": "text", "text": "ping"}]
            res = await client.chat.completions.create(
                model=model_id,
                messages=[
                    {
                        "role": "user",
                        "content": content,
                    },
                ],
                timeout=timeout,
                max_tokens=1,
                stream=True,
            )
            # consume the stream to ensure the model is actually responsive
            async for _ in res:
                break
            return True, ""
        except APIError as exc:
            return (
                False,
                f"API error when connecting to model '{model_id}': "
                f"{self._format_api_error(exc)}",
            )
        except Exception:
            return (
                False,
                f"Unknown exception when connecting to model '{model_id}'",
            )

    def get_chat_model_instance(self, model_id: str) -> Any:
        from .openai_chat_model_compat import OpenAIChatModelCompat

        return OpenAIChatModelCompat(
            model_name=model_id,
            stream=True,
            api_key=self.api_key,
            stream_tool_parsing=False,
            normalize_content_to_text=self._requires_plain_text_content(),
            client_kwargs=self._chat_client_kwargs(),
            generate_kwargs=self.get_effective_generate_kwargs(model_id),
        )

    def resolve_model_endpoint(self, model_id: str) -> ProviderModelEndpoint:
        """Resolve this OpenAI-compatible provider for native model clients."""

        client_kwargs = self._chat_client_kwargs()
        return ProviderModelEndpoint(
            provider_id=self.id,
            model_id=model_id,
            family="openai",
            base_url=str(client_kwargs.get("base_url") or self.base_url),
            api_key=self.api_key,
            is_local=self.is_local,
            chat_model=self.chat_model,
            generate_kwargs=self.get_effective_generate_kwargs(model_id),
            default_headers=client_kwargs.get("default_headers"),
            supports_multimodal=self._model_supports_multimodal(model_id),
            native_kind="openai_compatible",
            normalize_content_to_text=self._requires_plain_text_content(),
        )

    async def probe_model_multimodal(
        self,
        model_id: str,
        timeout: float = 60,
        image_only: bool = False,
    ) -> ProbeResult:
        """Probe multimodal support via OpenAI-compatible API."""
        from .multimodal_prober import ProbeResult

        img_ok, img_msg = await self._probe_image_support(
            model_id,
            timeout,
        )
        # Skip video probe when image probe already failed: a model
        # that cannot perceive images will not perceive video either,
        # and some text-only models (e.g. qwen3-max) may randomly
        # guess the correct color keyword, causing false positives.
        if not img_ok:
            return ProbeResult(
                supports_image=False,
                supports_video=False,
                image_message=img_msg,
                video_message="Skipped: image probe failed",
            )
        if image_only:
            return ProbeResult(
                supports_image=img_ok,
                supports_video=False,
                image_message=img_msg,
                video_message="Skipped: image_only=True",
            )
        vid_ok, vid_msg = await self._probe_video_support(
            model_id,
            timeout,
        )
        return ProbeResult(
            supports_image=img_ok,
            supports_video=vid_ok,
            image_message=img_msg,
            video_message=vid_msg,
        )

    async def _probe_image_support(
        self,
        model_id: str,
        timeout: float = 15,
    ) -> tuple[bool, str]:
        """Probe image support by sending a solid-red 16x16 PNG.

        Uses a two-stage check:
        1. If the API rejects the request (400 / media-keyword error)
           → not supported.
        2. If accepted, verify the model can *actually perceive* the
           image content via a semantic check
           (see ``evaluate_image_probe_answer``).

        Why a semantic check is necessary:
            Some models (e.g. qwen3-max via OpenAI-compatible API) silently
            accept image payloads without returning an error, yet they do NOT
            actually process the image — they simply ignore it and respond to
            the text prompt only.  A pure "did the API error?" check would
            produce false positives for these models.  The semantic check
            (asking for the dominant color and verifying the answer) catches
            this class of silent failures.
        """
        from .multimodal_prober import (
            _PROBE_IMAGE_B64,
            _IMAGE_PROBE_PROMPT,
            _is_media_keyword_error,
            evaluate_image_probe_answer,
        )

        logger.info(
            "Image probe start: model=%s url=%s",
            model_id,
            self.base_url,
        )
        start_time = time.monotonic()
        client = self._client(timeout=timeout)
        try:
            res = await client.chat.completions.create(
                model=model_id,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": (
                                        "data:image/png;base64,"
                                        f"{_PROBE_IMAGE_B64}"
                                    ),
                                },
                            },
                            {
                                "type": "text",
                                "text": _IMAGE_PROBE_PROMPT,
                            },
                        ],
                    },
                ],
                max_tokens=200,
                timeout=timeout,
            )
            answer = (res.choices[0].message.content or "").lower().strip()
            reasoning = ""
            msg = res.choices[0].message
            if hasattr(msg, "reasoning_content") and msg.reasoning_content:
                reasoning = msg.reasoning_content.lower()
            return evaluate_image_probe_answer(
                answer,
                model_id,
                start_time,
                reasoning,
            )
        except APIError as e:
            elapsed = time.monotonic() - start_time
            logger.warning(
                "Image probe error: model=%s type=%s msg=%s %.2fs",
                model_id,
                type(e).__name__,
                e,
                elapsed,
            )
            # 400 or media-keyword error → definitive rejection.
            # Other API errors are inconclusive (could be transient).
            # Use getattr because APITimeoutError lacks status_code.
            status = getattr(e, "status_code", None)
            if status == 400 or _is_media_keyword_error(e):
                return False, f"Image not supported: {e}"
            return False, f"Probe inconclusive: {e}"
        except Exception as e:
            elapsed = time.monotonic() - start_time
            logger.warning(
                "Image probe error: model=%s type=%s msg=%s %.2fs",
                model_id,
                type(e).__name__,
                e,
                elapsed,
            )
            return False, f"Probe failed: {e}"

    async def _probe_video_support(
        self,
        model_id: str,
        timeout: float = 30,
    ) -> tuple[bool, str]:
        """Probe video support with automatic format fallback."""
        from .multimodal_prober import (
            _PROBE_VIDEO_B64,
            _PROBE_VIDEO_URL,
        )

        logger.info(
            "Video probe start: model=%s url=%s",
            model_id,
            self.base_url,
        )
        start_time = time.monotonic()
        video_urls = [
            f"data:video/mp4;base64,{_PROBE_VIDEO_B64}",
            _PROBE_VIDEO_URL,
        ]
        last_error_msg = ""
        for video_url in video_urls:
            result = await self._try_video_url(
                model_id,
                video_url,
                timeout,
                start_time=start_time,
            )
            if result is not None:
                return result
            last_error_msg = f"format rejected for {video_url}"
        elapsed = time.monotonic() - start_time
        logger.info(
            "Video probe done: model=%s result=False %.2fs",
            model_id,
            elapsed,
        )
        return False, f"Video not supported: {last_error_msg}"

    async def _try_video_url(
        self,
        model_id: str,
        video_url: str,
        timeout: float,
        *,
        start_time: float,
    ) -> tuple[bool, str] | None:
        """Try a single video URL format. Return None to try next."""
        from .multimodal_prober import (
            _PROBE_VIDEO_URL,
            _is_media_keyword_error,
        )

        is_http = video_url == _PROBE_VIDEO_URL
        req_timeout = timeout * 3 if is_http else timeout
        client = self._client(timeout=req_timeout)
        try:
            res = await client.chat.completions.create(
                model=model_id,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "video_url",
                                "video_url": {"url": video_url},
                            },
                            {
                                "type": "text",
                                "text": (
                                    "What is the single dominant "
                                    "color shown in this video? "
                                    "Reply with ONLY the color "
                                    "name, nothing else."
                                ),
                            },
                        ],
                    },
                ],
                max_tokens=200,
                timeout=req_timeout,
            )
            return self._evaluate_video_response(
                res,
                model_id,
                start_time,
                is_http,
            )
        except APIError as e:
            status = getattr(e, "status_code", None)
            # 400 means this specific video format was rejected, but the
            # model might accept a different format — return None to let
            # the caller try the next URL in the fallback list.
            if status == 400:
                logger.debug(
                    "Video probe format rejected (400): %s",
                    e,
                )
                return None
            elapsed = time.monotonic() - start_time
            # If the error message contains media-related keywords
            # (e.g. "video", "vision"), it's a definitive rejection.
            is_kw = _is_media_keyword_error(e)
            label = "not supported" if is_kw else "inconclusive"
            logger.warning(
                "Video probe error: model=%s type=%s msg=%s %.2fs",
                model_id,
                type(e).__name__,
                e,
                elapsed,
            )
            return False, f"Video {label}: {e}"
        except Exception as e:
            elapsed = time.monotonic() - start_time
            logger.warning(
                "Video probe error: model=%s type=%s msg=%s %.2fs",
                model_id,
                type(e).__name__,
                e,
                elapsed,
            )
            return False, f"Probe failed: {e}"

    @staticmethod
    def _evaluate_video_response(
        res,
        model_id: str,
        start_time: float,
        is_http: bool,
    ) -> tuple[bool, str]:
        """Evaluate video probe response.

        Detection criteria:
            The probe video is a solid-blue 64×64 H.264 MP4.  We ask
            "What is the single dominant color?" and check for "blue"
            or "蓝" in the reply or reasoning_content.

            Special case for HTTP URL probes: if the model returns any
            non-empty answer (even without "blue"), we accept it as
            supported.  The HTTP URL points to an external video whose
            content we do not control (not the blue probe video), so
            colour-matching is impossible.  This relaxed check is safe
            because ``probe_model_multimodal`` only reaches the video
            probe after the image probe has already passed, which
            filters out text-only models that silently accept media
            payloads (e.g. qwen3-max).
        """
        answer = (res.choices[0].message.content or "").lower().strip()
        # Primary check: answer contains a blue-family color keyword.
        # Models may describe the solid-blue video as "blue", "navy",
        # "azure", "cobalt", "cyan", "indigo", "蓝" etc.
        _BLUE_KW = ("blue", "navy", "azure", "cobalt", "cyan", "indigo", "蓝")
        if any(kw in answer for kw in _BLUE_KW):
            elapsed = time.monotonic() - start_time
            logger.info(
                "Video probe done: model=%s result=True %.2fs",
                model_id,
                elapsed,
            )
            return True, f"Video supported (answer={answer!r})"
        # Fallback: reasoning models may put analysis in reasoning_content.
        reasoning = ""
        msg = res.choices[0].message
        if hasattr(msg, "reasoning_content") and msg.reasoning_content:
            reasoning = msg.reasoning_content.lower()
        if reasoning and any(kw in reasoning for kw in _BLUE_KW):
            elapsed = time.monotonic() - start_time
            logger.info(
                "Video probe done: model=%s result=True %.2fs",
                model_id,
                elapsed,
            )
            return (
                True,
                f"Video supported (reasoning, answer={answer!r})",
            )
        # HTTP URL fallback: accept any non-empty response as evidence
        # of video support (see docstring for safety rationale).
        if is_http and answer:
            elapsed = time.monotonic() - start_time
            logger.info(
                "Video probe done: model=%s result=True (http) %.2fs",
                model_id,
                elapsed,
            )
            return True, f"Video supported (http, answer={answer!r})"
        elapsed = time.monotonic() - start_time
        logger.info(
            "Video probe done: model=%s result=False answer=%r %.2fs",
            model_id,
            answer,
            elapsed,
        )
        return (
            False,
            f"Model did not recognise video (answer={answer!r})",
        )
