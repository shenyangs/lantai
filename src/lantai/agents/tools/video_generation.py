# -*- coding: utf-8 -*-
"""Generate durable videos from conversation tasks through configured models."""

from __future__ import annotations

import hashlib
import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import aiofiles
import httpx

from ...config import load_config
from ...constant import DEFAULT_MEDIA_DIR
from ...exceptions import (
    TokenDanceRecoveryException,
    VideoGenerationAttemptException,
    get_tokendance_recovery_action,
)
from ...providers.provider_manager import ProviderManager
from ...providers.tokendance_authorization import (
    assert_tokendance_authorization_current,
    resolve_tokendance_authorization,
)
from ...providers.tokendance_provider import TokenDanceProvider
from ...runtime.effect_context import current_runtime_effect_context
from ...runtime.effects import RuntimeEffectJournal
from ...runtime.remote_tasks import (
    RemoteTaskJournal,
    RuntimeRemoteTaskCoordinator,
    TokenDanceRemoteSubmissionObserver,
)
from ...runtime.tool_response import TextBlock, ToolResponse, VideoBlock
from ...security.cloud_account_store import is_watcha_account_connected
from ...video_generation_routing import (
    effective_video_generation_config,
    find_video_model,
)
logger = logging.getLogger(__name__)

_LOGGED_URL_PATTERN = re.compile(r"https?://[^\s\"']+", re.IGNORECASE)


def _redact_logged_url(match: re.Match[str]) -> str:
    """Keep only the origin/path of a URL found in dependency logs."""

    try:
        parsed = urlsplit(match.group(0))
        hostname = parsed.hostname or ""
        if not hostname:
            return "[redacted-url]"
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return urlunsplit(
            (parsed.scheme, f"{hostname}{port}", parsed.path, "", ""),
        )
    except Exception:
        return "[redacted-url]"


class _DependencyUrlRedactionFilter(logging.Filter):
    """Remove URL credentials and signatures from HTTP dependency logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            record.msg = _LOGGED_URL_PATTERN.sub(_redact_logged_url, message)
            record.args = ()
        except Exception:
            record.msg = "HTTP dependency event (URL redacted)."
            record.args = ()
        if record.exc_info:
            record.exc_info = None
            record.exc_text = None
        return True


_DEPENDENCY_URL_REDACTION_FILTER = _DependencyUrlRedactionFilter()


def _tokendance_remote_submission_observer(
    *,
    manager: ProviderManager,
    provider: TokenDanceProvider,
    model_id: str,
    prompt: str,
    duration: int,
    aspect_ratio: str,
    resolution: str,
) -> TokenDanceRemoteSubmissionObserver:
    context = current_runtime_effect_context()
    if context is None:
        raise RuntimeError(
            "TokenDance 远端任务必须由可恢复的 Runtime Effect 发起。"
        )
    authorization = resolve_tokendance_authorization(manager, use_kind="model")
    protocol = provider._video_protocol_for(model_id)  # noqa: SLF001
    if not protocol:
        raise ValueError("TokenDance video route has no verified protocol")
    coordinator = RuntimeRemoteTaskCoordinator(
        effect_journal=RuntimeEffectJournal(context.state_workspace_dir),
        remote_journal=RemoteTaskJournal(context.state_workspace_dir),
    )
    return TokenDanceRemoteSubmissionObserver.create(
        coordinator=coordinator,
        context=context,
        authorization=authorization,
        model_id=model_id,
        protocol=protocol,
        prompt=prompt,
        request_options={
            "duration": duration,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
        },
        assert_authorization_current=lambda: assert_tokendance_authorization_current(
            manager,
            authorization,
        ),
    )


def _install_dependency_url_redaction() -> None:
    """Permanently protect HTTPX/HTTPCore request logs in this process."""

    for logger_name in (
        "httpx",
        "httpcore",
        "httpcore.connection",
        "httpcore.connection_pool",
        "httpcore.http11",
        "httpcore.http2",
        "httpcore.proxy",
    ):
        dependency_logger = logging.getLogger(logger_name)
        if _DEPENDENCY_URL_REDACTION_FILTER not in dependency_logger.filters:
            dependency_logger.addFilter(_DEPENDENCY_URL_REDACTION_FILTER)


_install_dependency_url_redaction()


class VideoDownloadError(RuntimeError):
    """A safe, URL-free failure while persisting a generated video."""


def is_video_generation_available() -> bool:
    """Whether at least one configured text-to-video route is runnable."""

    manager = ProviderManager.get_instance()
    config = effective_video_generation_config(load_config(), manager)
    if not config.enabled:
        return False
    for slot in config.models:
        provider = manager.get_provider(slot.provider_id)
        if provider is None or not callable(
            getattr(provider, "generate_video", None),
        ):
            continue
        if find_video_model(provider, slot.model) is None:
            continue
        if slot.provider_id == "tokendance" and not is_watcha_account_connected():
            continue
        if getattr(provider, "require_api_key", True) and not str(
            getattr(provider, "api_key", "") or "",
        ).strip():
            continue
        return True
    return False


def _video_filename(url: str, suffix: str = ".mp4") -> str:
    parsed = urlsplit(url)
    identity = f"{parsed.hostname or ''}{parsed.path}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    safe_suffix = suffix if suffix in {".mp4", ".webm"} else ".mp4"
    return (
        f"generated_video_{int(time.time() * 1000)}_"
        f"{uuid4().hex[:8]}_{digest}{safe_suffix}"
    )


def _detect_video_container(header: bytes) -> tuple[str, str] | None:
    if len(header) >= 8 and header[4:8] == b"ftyp":
        return ".mp4", "video/mp4"
    if header.startswith(b"\x1a\x45\xdf\xa3"):
        return ".webm", "video/webm"
    return None


async def _download_video_to_cache(
    video_url: str,
    download_dir: Path,
) -> tuple[str, str]:
    """Persist a verified MP4/WebM response via a temporary file and rename."""

    directory = Path(download_dir)
    temporary = directory / f".video-{uuid4().hex}.part"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            async with client.stream("GET", video_url) as response:
                if not 200 <= int(response.status_code) < 300:
                    raise VideoDownloadError(
                        f"Video download returned HTTP {response.status_code}."
                    )
                content_type = str(response.headers.get("content-type", ""))
                content_type = content_type.split(";", 1)[0].strip().lower()
                header = bytearray()
                written = 0
                async with aiofiles.open(temporary, "wb") as handle:
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        if not chunk:
                            continue
                        if len(header) < 16:
                            header.extend(chunk[: 16 - len(header)])
                        await handle.write(chunk)
                        written += len(chunk)
        detected = _detect_video_container(bytes(header))
        if written == 0 or detected is None:
            raise VideoDownloadError("Video download did not contain a video file.")
        suffix, mime_type = detected
        if content_type not in {
            "",
            "application/octet-stream",
            "binary/octet-stream",
            mime_type,
        }:
            raise VideoDownloadError(
                "Video download content type did not match its container."
            )
        destination = directory / _video_filename(video_url, suffix)
        temporary.replace(destination)
        return str(destination), mime_type
    except VideoDownloadError:
        temporary.unlink(missing_ok=True)
        raise
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        raise VideoDownloadError(
            f"Video cache download failed ({type(exc).__name__})."
        ) from None


async def generate_video(
    prompt: str,
    duration: int = 5,
    aspect_ratio: str = "16:9",
    resolution: str = "720p",
) -> ToolResponse:
    """Generate a video only when the user explicitly requests a video task.

    The configured media routes are independent from the chat model selector.
    One upstream task is submitted and polled per attempt; ambiguous submissions
    or long-running tasks are never automatically duplicated.
    """

    clean_prompt = (prompt or "").strip()
    if not clean_prompt:
        raise ValueError("Please provide a non-empty video prompt.")
    manager = ProviderManager.get_instance()
    config = effective_video_generation_config(load_config(), manager)
    if not config.enabled or not config.models:
        raise RuntimeError(
            "生视频尚未配置。请在设置 > 模型 > 对话内生视频中配置并启用。",
        )

    attempts: list[dict[str, str]] = []
    provider = None
    model = None
    payload: dict[str, Any] = {}
    video_url = ""
    for slot in config.models:
        provider = manager.get_provider(slot.provider_id)
        model = find_video_model(provider, slot.model)
        if provider is None or model is None or not callable(
            getattr(provider, "generate_video", None),
        ):
            attempts.append(
                {
                    "provider_id": slot.provider_id,
                    "model": slot.model,
                    "error": "configured route is no longer available",
                },
            )
            continue
        if slot.provider_id == "tokendance" and not is_watcha_account_connected():
            raise RuntimeError("请先登录观猹账号，再使用 TokenDance 生视频。")
        if getattr(provider, "require_api_key", True) and not str(
            getattr(provider, "api_key", "") or "",
        ).strip():
            attempts.append(
                {
                    "provider_id": slot.provider_id,
                    "model": slot.model,
                    "error": "provider is not authorized",
                },
            )
            continue
        try:
            generate_options: dict[str, Any] = {
                "model_id": model.id,
                "prompt": clean_prompt,
                "duration": duration,
                "aspect_ratio": aspect_ratio,
                "resolution": resolution,
                "timeout": float(config.timeout_seconds),
            }
            if isinstance(provider, TokenDanceProvider):
                generate_options["submission_observer"] = (
                    _tokendance_remote_submission_observer(
                        manager=manager,
                        provider=provider,
                        model_id=model.id,
                        prompt=clean_prompt,
                        duration=duration,
                        aspect_ratio=aspect_ratio,
                        resolution=resolution,
                    )
                )
            candidate = await provider.generate_video(
                **generate_options,
            )
            candidate_assets = (
                candidate.get("assets") if isinstance(candidate, dict) else None
            )
            if isinstance(candidate_assets, list):
                for asset in candidate_assets:
                    if isinstance(asset, dict) and asset.get("type") == "video":
                        video_url = str(asset.get("url") or "").strip()
                        if video_url:
                            break
            if not video_url:
                raise VideoGenerationAttemptException(
                    "Video task completed without a video asset.",
                    task_id=str(candidate.get("task_id") or "")
                    if isinstance(candidate, dict)
                    else "",
                )
            payload = candidate
            break
        except Exception as exc:  # provider errors have several SDK subclasses
            has_submitted_task = (
                isinstance(exc, VideoGenerationAttemptException)
                and bool(exc.task_id)
            )
            recovery_action = (
                get_tokendance_recovery_action(exc)
                if slot.provider_id == "tokendance" and not has_submitted_task
                else ""
            )
            if recovery_action:
                raise TokenDanceRecoveryException(
                    recovery_action,
                    details={
                        "provider_id": slot.provider_id,
                        "model_id": slot.model,
                        "recovery_action": recovery_action,
                    },
                ) from exc
            if not isinstance(exc, VideoGenerationAttemptException):
                raise
            if not exc.may_fail_over:
                raise
            attempts.append(
                {
                    "provider_id": slot.provider_id,
                    "model": slot.model,
                    "error": exc.message,
                    "task_id": exc.task_id,
                },
            )
            video_url = ""
    if not video_url or provider is None or model is None:
        detail = "; ".join(
            (
                f"{item['provider_id']}/{item['model']}: "
                f"{item['error']}"
            )
            for item in attempts
        )
        raise RuntimeError(
            "所有已配置的生视频模型均失败。"
            + (f" {detail}" if detail else ""),
        )

    durable = True
    media_dir = DEFAULT_MEDIA_DIR / "generated_videos"
    try:
        local_url, mime_type = await _download_video_to_cache(
            video_url,
            media_dir,
        )
    except Exception:  # signed result URL remains usable for a bounded period
        logger.warning("Generated video could not be cached locally")
        local_url = video_url
        mime_type = (
            "video/webm"
            if Path(urlsplit(video_url).path).suffix.lower() == ".webm"
            else "video/mp4"
        )
        durable = False

    task_id = str(payload.get("task_id") or "")
    protocol = str(payload.get("protocol") or "")
    return ToolResponse(
        content=[
            VideoBlock(
                source={"type": "url", "url": local_url},
                video_url=local_url,
                mime_type=mime_type,
                metadata={"durable": durable, "task_id": task_id},
            ),
            TextBlock(
                text=(
                    f"已生成视频。模型：{provider.name} / {model.name}；"
                    f"协议：{protocol or 'provider catalog'}。"
                    + (
                        "本地缓存失败，当前为临时链接，请尽快下载保存。"
                        if not durable
                        else ""
                    )
                ),
            ),
        ],
        metadata={
            "provider_id": provider.id,
            "model": model.id,
            "task_id": task_id,
            "protocol": protocol,
            "durable": durable,
            "attempts": attempts,
            "prompt_sha256": (
                "sha256:" + hashlib.sha256(clean_prompt.encode("utf-8")).hexdigest()
            ),
        },
    )


__all__ = [
    "VideoDownloadError",
    "generate_video",
    "is_video_generation_available",
]
