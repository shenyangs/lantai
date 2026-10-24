"""Generate images inside chat with ordered model failover."""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import mimetypes
import re
import shutil
import time
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import unquote, urlparse
from uuid import uuid4

from ...config import load_config
from ...config.context import get_current_workspace_dir
from ...constant import DEFAULT_MEDIA_DIR, WORKING_DIR
from ...exceptions import (
    TokenDanceRecoveryException,
    get_tokendance_recovery_action,
)
from ...providers.provider import ModelInfo, Provider
from ...providers.provider_manager import ProviderManager
from ...runtime.tool_response import ImageBlock, TextBlock, ToolResponse
from ...security.cloud_account_store import is_watcha_account_connected
from ..utils.file_handling import download_file_from_url

logger = logging.getLogger(__name__)
_MAX_REFERENCE_IMAGES = 4
_MAX_REFERENCE_BYTES = 20 * 1024 * 1024
_DEFAULT_IMAGE_SIZE = "1024x1024"
_ASPECT_RATIO_SIZES = {
    "1:1": "1024x1024",
    "16:9": "1920x1080",
    "9:16": "1080x1920",
    "4:3": "1536x1152",
    "3:4": "1152x1536",
    "3:2": "1536x1024",
    "2:3": "1024x1536",
}


class ImageSource(NamedTuple):
    """A normalized generated image source."""

    kind: str
    value: str
    mime_type: str = "image/png"


def _coerce_quantity(value: int | str | None) -> int:
    try:
        quantity = int(value or 1)
    except (TypeError, ValueError) as exc:
        raise ValueError("n must be an integer between 1 and 4") from exc
    if quantity < 1 or quantity > 4:
        raise ValueError("n must be an integer between 1 and 4")
    return quantity


def _normalize_requested_size(value: str | None) -> str:
    text = str(value or "").strip().lower()
    compact = text.replace("×", "x").replace("*", "x").replace(" ", "")
    if compact in _ASPECT_RATIO_SIZES:
        return _ASPECT_RATIO_SIZES[compact]
    if compact in {"2k", "3k", "4k"}:
        return compact
    match = re.fullmatch(r"(\d{2,5})x(\d{2,5})", compact)
    if match and int(match.group(1)) > 0 and int(match.group(2)) > 0:
        return f"{int(match.group(1))}x{int(match.group(2))}"
    return _DEFAULT_IMAGE_SIZE


def _parse_data_uri(value: str) -> tuple[str, str] | None:
    if not value.startswith("data:image/") or ";base64," not in value:
        return None
    header, data = value.split(";base64,", 1)
    return header.removeprefix("data:"), data


def _decode_base64_image(value: str) -> bytes:
    parsed = _parse_data_uri(value)
    if parsed:
        _, value = parsed
    compact = "".join(value.split())
    compact += "=" * (-len(compact) % 4)
    try:
        return base64.b64decode(compact, validate=False)
    except binascii.Error as exc:
        raise ValueError("Invalid base64 image payload") from exc


def _extension_for_mime_type(mime_type: str) -> str:
    if mime_type == "image/jpeg":
        return ".jpg"
    return mimetypes.guess_extension(mime_type or "image/png") or ".png"


def _save_base64_image(
    source: ImageSource,
    *,
    media_dir: Path,
    index: int,
) -> Path:
    image_bytes = _decode_base64_image(source.value)
    media_dir.mkdir(parents=True, exist_ok=True)
    image_path = media_dir / (
        f"generated_image_{int(time.time() * 1000)}_"
        f"{index}_{uuid4().hex[:8]}"
        f"{_extension_for_mime_type(source.mime_type)}"
    )
    image_path.write_bytes(image_bytes)
    return image_path


def _extract_generated_sources(payload: Any) -> list[ImageSource]:
    record = payload if isinstance(payload, dict) else {}
    sources: list[ImageSource] = []
    seen: set[tuple[str, str]] = set()

    def append_source(source: ImageSource) -> None:
        key = (source.kind, source.value)
        if not source.value or key in seen:
            return
        seen.add(key)
        sources.append(source)

    assets = record.get("assets")
    if isinstance(assets, list):
        for item in assets:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            mime_type = str(
                item.get("mimeType") or item.get("mime_type") or "image/png",
            )
            parsed = _parse_data_uri(url)
            if parsed:
                append_source(ImageSource("base64", parsed[1], parsed[0]))
            elif url:
                append_source(ImageSource("url", url, mime_type))

    data = record.get("data")
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            b64_json = str(item.get("b64_json") or "").strip()
            if url:
                append_source(ImageSource("url", url))
            if b64_json:
                append_source(ImageSource("base64", b64_json))

    return sources


def _find_model(provider: Provider, model_id: str) -> ModelInfo | None:
    return next(
        (
            model
            for model in [*provider.models, *provider.extra_models]
            if model.id == model_id
        ),
        None,
    )


def _generated_image_filename(source: ImageSource, *, index: int) -> str:
    remote_name = Path(urlparse(source.value).path).name
    suffix = Path(remote_name).suffix or _extension_for_mime_type(source.mime_type)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", remote_name) or f"image{suffix}"
    if not Path(safe_name).suffix:
        safe_name = f"{safe_name}{suffix}"
    return (
        f"generated_image_{int(time.time() * 1000)}_"
        f"{index}_{uuid4().hex[:8]}_{safe_name}"
    )


class _UnsafeWorkspaceStagingPath(ValueError):
    """Raised when workspace image staging could escape its managed root."""


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _stage_workspace_image(
    candidate: Path,
) -> tuple[str | None, dict[str, str] | None]:
    workspace_dir = get_current_workspace_dir()
    if workspace_dir is None:
        return None, None

    try:
        project_root = Path(workspace_dir).expanduser().resolve(strict=True)
        working_root = Path(WORKING_DIR).expanduser().resolve(strict=True)
    except OSError as exc:
        logger.warning("Unable to resolve image staging roots", exc_info=True)
        return None, {
            "status": "failed",
            "reason": "workspace_unavailable",
            "warning": f"任务工作区副本未写入：{exc}",
        }

    if not _is_within(project_root, working_root):
        return None, {
            "status": "skipped",
            "reason": "workspace_outside_managed_root",
        }

    try:
        lantai_dir = project_root / ".lantai"
        if lantai_dir.is_symlink():
            raise _UnsafeWorkspaceStagingPath(".lantai is a symbolic link")
        lantai_dir.mkdir(parents=True, exist_ok=True)

        staging_dir = lantai_dir / "generated_images"
        if staging_dir.is_symlink():
            raise _UnsafeWorkspaceStagingPath(
                "generated_images is a symbolic link",
            )
        staging_dir.mkdir(parents=True, exist_ok=True)
        resolved_staging = staging_dir.resolve(strict=True)
        if not _is_within(resolved_staging, project_root) or not _is_within(
            resolved_staging,
            working_root,
        ):
            raise _UnsafeWorkspaceStagingPath(
                "generated image directory escaped its managed roots",
            )

        staged = staging_dir / candidate.name
        if staged.is_symlink():
            raise _UnsafeWorkspaceStagingPath(
                "generated image target is a symbolic link",
            )
        resolved_target = staged.resolve(strict=False)
        if not _is_within(resolved_target, resolved_staging) or not _is_within(
            resolved_target,
            project_root,
        ):
            raise _UnsafeWorkspaceStagingPath(
                "generated image target escaped its staging directory",
            )
        if resolved_target != candidate:
            shutil.copy2(candidate, resolved_target)
        return str(resolved_target), {"status": "completed"}
    except _UnsafeWorkspaceStagingPath as exc:
        logger.warning("Refused unsafe generated-image staging path: %s", exc)
        return None, {
            "status": "failed",
            "reason": "unsafe_staging_path",
            "warning": "任务工作区副本未写入：目标路径不安全。",
        }
    except OSError as exc:
        logger.warning("Unable to stage generated image", exc_info=True)
        return None, {
            "status": "failed",
            "reason": "copy_failed",
            "warning": f"任务工作区副本未写入：{exc}",
        }


async def _image_block(
    source: ImageSource,
    *,
    media_dir: Path,
    index: int,
    metadata: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str, bool, str | None, str | None]:
    durable = True
    if source.kind == "base64":
        path = _save_base64_image(source, media_dir=media_dir, index=index)
        url = str(path)
    else:
        try:
            url = await download_file_from_url(
                source.value,
                filename=_generated_image_filename(source, index=index),
                download_dir=str(media_dir),
            )
        except Exception:  # pylint: disable=broad-except
            logger.warning(
                "Generated image could not be cached locally; using provider URL",
                exc_info=True,
            )
            url = source.value
            durable = False
    managed_path: str | None = None
    workspace_path: str | None = None
    workspace_staging: dict[str, str] | None = None
    if durable:
        parsed = urlparse(url)
        if parsed.scheme in {"", "file"}:
            raw_path = unquote(parsed.path) if parsed.scheme == "file" else url
            candidate = Path(raw_path).expanduser().resolve()
            if candidate.is_file():
                managed_path = str(candidate)
                workspace_path, workspace_staging = _stage_workspace_image(candidate)
    block_metadata = {**(metadata or {}), "durable": durable}
    if managed_path:
        block_metadata["managed_path"] = managed_path
    if workspace_path:
        block_metadata["workspace_path"] = workspace_path
    if workspace_staging:
        block_metadata["workspace_staging"] = workspace_staging
    return (
        ImageBlock(
            source={"type": "url", "url": url},
            image_url=url,
            mime_type=source.mime_type,
            metadata=block_metadata,
        ),
        url,
        durable,
        managed_path,
        workspace_path,
    )


def _normalize_reference_images(values: list[str] | None) -> list[str]:
    if not values:
        return []
    if not isinstance(values, list) or len(values) > _MAX_REFERENCE_IMAGES:
        raise ValueError("reference_images must contain 1 to 4 local image paths")
    paths: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("reference_images must contain local image paths")
        parsed = urlparse(value.strip())
        if parsed.scheme and parsed.scheme != "file":
            raise ValueError("reference_images only accepts local image files")
        raw_path = parsed.path if parsed.scheme == "file" else value.strip()
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"Reference image does not exist: {path}")
        mime_type = mimetypes.guess_type(path.name)[0] or ""
        if not mime_type.startswith("image/"):
            raise ValueError(f"Reference file is not an image: {path}")
        if path.stat().st_size > _MAX_REFERENCE_BYTES:
            raise ValueError(f"Reference image exceeds 20MB: {path}")
        normalized = str(path)
        if normalized not in paths:
            paths.append(normalized)
    return paths


async def generate_image(
    prompt: str,
    size: str = "1024x1024",
    quality: str = "auto",
    n: int = 1,
    reference_images: list[str] | None = None,
    parent_artifact_id: str | None = None,
) -> ToolResponse:
    """Generate or edit images when the user asks for a visual.

    Use this tool only for explicit image-generation requests. The configured
    image models are tried in order and automatically fail over. Pass local
    ``reference_images`` to edit an existing visual while preserving lineage.
    """

    clean_prompt = (prompt or "").strip()
    if not clean_prompt:
        raise ValueError("Please provide a non-empty image prompt.")
    quantity = _coerce_quantity(n)
    requested_size_input = str(size or _DEFAULT_IMAGE_SIZE)
    normalized_size = _normalize_requested_size(requested_size_input)
    normalized_references = _normalize_reference_images(reference_images)
    operation = "edit" if normalized_references else "generate"
    clean_parent_artifact_id = str(parent_artifact_id or "").strip()[:200] or None
    generation_id = f"image-gen-{uuid4().hex}"
    prompt_sha256 = "sha256:" + hashlib.sha256(clean_prompt.encode("utf-8")).hexdigest()

    config = load_config().image_generation
    if not config.enabled or not config.models:
        raise RuntimeError(
            "In-chat image generation is not configured. Select at least one "
            "image model in Settings > Models."
        )

    manager = ProviderManager.get_instance()
    failures: list[str] = []
    for slot in config.models:
        if slot.provider_id == "tokendance" and not is_watcha_account_connected():
            failures.append(
                "tokendance: 请先登录观猹账号，再使用 TokenDance 生图",
            )
            continue
        provider = manager.get_provider(slot.provider_id)
        model = _find_model(provider, slot.model) if provider else None
        generator_name = "edit_image" if normalized_references else "generate_image"
        generator = getattr(provider, generator_name, None) if provider else None
        if (
            provider is None
            or model is None
            or not manager.is_model_slot_available(slot)
            or not callable(generator)
        ):
            failures.append(f"{slot.provider_id}/{slot.model}: unavailable")
            continue

        try:
            call_kwargs: dict[str, Any] = {
                "model_id": model.id,
                "prompt": clean_prompt,
                "n": quantity,
                "size": normalized_size,
                "quality": quality or "auto",
                "response_format": None,
                "timeout": float(config.timeout_seconds),
            }
            if normalized_references:
                call_kwargs["image_paths"] = normalized_references
            payload = await generator(
                **call_kwargs,
            )
            sources = _extract_generated_sources(payload)
            if not sources:
                raise RuntimeError("provider returned no displayable image")
            requested_size = str(
                payload.get("requested_size") or normalized_size,
            )
            effective_size = str(
                payload.get("effective_size") or requested_size,
            )

            media_dir = DEFAULT_MEDIA_DIR / "generated_images"
            content: list[dict[str, Any]] = []
            rendered_urls: list[str] = []
            durable_assets: list[bool] = []
            managed_paths: list[str | None] = []
            workspace_paths: list[str | None] = []
            workspace_staging_results: list[dict[str, str] | None] = []
            for index, source in enumerate(sources, start=1):
                artifact_id = f"{generation_id}:{index}"
                block, url, durable, managed_path, workspace_path = await _image_block(
                    source,
                    media_dir=media_dir,
                    index=index,
                    metadata={
                        "artifact_id": artifact_id,
                        "generation_id": generation_id,
                        "parent_artifact_id": clean_parent_artifact_id,
                        "operation": operation,
                    },
                )
                content.append(block)
                rendered_urls.append(url)
                durable_assets.append(durable)
                managed_paths.append(managed_path)
                workspace_paths.append(workspace_path)
                workspace_staging_results.append(block.get("metadata", {}).get("workspace_staging"))

            summary = [
                (
                    f"已基于 {len(normalized_references)} 张参考图编辑"
                    f" {len(rendered_urls)} 张图片。"
                    if normalized_references
                    else f"已生成 {len(rendered_urls)} 张图片。"
                ),
                f"模型：{provider.name} / {model.name}",
                (
                    f"尺寸：{effective_size}（由 {requested_size} 自动适配）"
                    if effective_size != requested_size
                    else f"尺寸：{effective_size}"
                ),
            ]
            local_paths = [
                url
                for url in rendered_urls
                if not urlparse(url).scheme or urlparse(url).scheme == "file"
            ]
            if local_paths:
                summary.append("图片已保存到本地媒体目录。")
            staged_paths = [path for path in workspace_paths if path]
            if staged_paths:
                summary.append("任务工作区图片：" + "、".join(staged_paths))
            if any(
                result and result.get("status") == "failed"
                for result in workspace_staging_results
            ):
                summary.append("工作区副本未写入；已保留澜台托管副本。")
            content.append(TextBlock(text="\n".join(summary)))
            return ToolResponse(
                content=content,
                metadata={
                    "provider_id": provider.id,
                    "model": model.id,
                    "attempts": len(failures) + 1,
                    "operation": operation,
                    "generation_id": generation_id,
                    "parent_artifact_id": clean_parent_artifact_id,
                    "reference_images": normalized_references,
                    "prompt_sha256": prompt_sha256,
                    "requested_size_input": requested_size_input,
                    "requested_size": requested_size,
                    "effective_size": effective_size,
                    "artifacts": [
                        {
                            "artifact_id": f"{generation_id}:{index}",
                            "url": url,
                            "parent_artifact_id": clean_parent_artifact_id,
                            "durable": durable_assets[index - 1],
                            "managed_path": managed_paths[index - 1],
                            "workspace_path": workspace_paths[index - 1],
                            "workspace_staging": workspace_staging_results[index - 1],
                        }
                        for index, url in enumerate(rendered_urls, start=1)
                    ],
                },
            )
        except Exception as exc:  # pylint: disable=broad-except
            recovery_action = get_tokendance_recovery_action(exc)
            if recovery_action:
                raise TokenDanceRecoveryException(
                    recovery_action,
                    details={
                        "provider_id": slot.provider_id,
                        "model_id": slot.model,
                        "recovery_action": recovery_action,
                    },
                ) from exc
            logger.warning(
                "Image generation attempt failed for %s/%s: %s",
                slot.provider_id,
                slot.model,
                exc,
                exc_info=True,
            )
            failures.append(
                f"{slot.provider_id}/{slot.model}: "
                f"{str(exc).strip() or type(exc).__name__}"
            )

    detail = "; ".join(failures[:4]) or "no configured model was available"
    raise RuntimeError(f"All configured image models failed: {detail}")
