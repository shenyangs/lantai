# -*- coding: utf-8 -*-
"""Provider-neutral model request normalization helpers."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from ..constant import MEDIA_UNSUPPORTED_PLACEHOLDER
from ..exceptions import ModelFormatterError
from .models import ModelContentBlock, ModelMessage

_MEDIA_BLOCK_TYPES = {"image", "audio", "video"}
_PROVIDER_ONLY_TOOL_CALL_FIELDS = frozenset({"extra_content", "raw_input"})
_GEMINI_NATIVE_FIELDS = frozenset({"extra_content"})
_MIME_FIXES = {"image/jpg": "image/jpeg"}
MAX_MEDIA_BLOCKS_PER_REQUEST = 16
MAX_LOCAL_MEDIA_BYTES = 150 * 1024 * 1024
MAX_LOCAL_MEDIA_TOTAL_BYTES = 150 * 1024 * 1024

_IMAGE_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
_VIDEO_MIME_BY_EXT = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mpeg": "video/mpeg",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
}
_FILE_MIME_BY_EXT = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".json": "application/json",
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".zip": "application/zip",
}


def file_url_to_path(url: str) -> str:
    """Strip ``file://`` and percent-decode a local file URL."""

    value = url.removeprefix("file://")
    if len(value) >= 3 and value.startswith("/") and value[1].isalpha() and value[2] == ":":
        value = value[1:]
    return unquote(value)


def _fixed_mime(media_type: str) -> str:
    return _MIME_FIXES.get(media_type, media_type)


def file_payload_metadata(data: dict[str, Any]) -> tuple[str, str]:
    """Return a stable display name and MIME type for a file content block.

    Console uploads retain the original filename separately from the temporary
    stored path.  Some model adapters later turn that path into a base64 data
    URL, where relying on the source media type alone degrades common documents
    to ``application/octet-stream``.  Prefer an explicit useful media type, then
    infer it from the original filename or source path.
    """

    source = data.get("source")
    source_url = ""
    declared_media_type = ""
    if isinstance(source, dict):
        if source.get("type") == "base64":
            declared_media_type = str(source.get("media_type") or "")
        elif isinstance(source.get("url"), str):
            source_url = str(source["url"])
    if not source_url:
        for key in ("url", "path"):
            value = data.get(key)
            if isinstance(value, str):
                source_url = value
                break

    filename = ""
    for key in ("filename", "file_name", "name"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            filename = value.strip()
            break
    if not filename and source_url and not source_url.startswith("data:"):
        parsed = urlparse(source_url)
        candidate_path = parsed.path if parsed.scheme else source_url
        filename = Path(unquote(candidate_path)).name
    filename = filename or "attachment"

    data_url_media_type = ""
    if source_url.startswith("data:") and ";" in source_url:
        data_url_media_type = source_url[5:].split(";", 1)[0]
    declared_media_type = _fixed_mime(
        declared_media_type or data_url_media_type,
    )

    guessed_media_type = (
        _FILE_MIME_BY_EXT.get(Path(filename).suffix.lower())
        or mimetypes.guess_type(filename)[0]
        or ""
    )
    if (
        not declared_media_type
        or declared_media_type == "application/octet-stream"
    ) and guessed_media_type:
        media_type = guessed_media_type
    else:
        media_type = declared_media_type

    return filename, _fixed_mime(media_type or "application/octet-stream")


def _source_url(data: dict[str, Any]) -> str:
    source = data.get("source")
    if isinstance(source, dict):
        if source.get("type") == "base64":
            media_type = _fixed_mime(str(source.get("media_type") or ""))
            if not media_type and data.get("filename"):
                _filename, media_type = file_payload_metadata(data)
            payload = str(source.get("data") or "")
            return f"data:{media_type};base64,{payload}" if media_type else ""
        url = source.get("url")
        if isinstance(url, str):
            return url
    for key in ("url", "path"):
        value = data.get(key)
        if isinstance(value, str):
            return value
    return ""


def _set_source_url(data: dict[str, Any], url: str) -> None:
    source = data.get("source")
    if isinstance(source, dict) and isinstance(source.get("url"), str):
        source["url"] = url
    elif "url" in data:
        data["url"] = url
    elif "path" in data:
        data["path"] = url


def _is_local_path(url: str) -> bool:
    if not url or url.startswith(("http://", "https://", "data:")):
        return False
    parsed = urlparse(url)
    return parsed.scheme in ("", "file")


def _base64_payload_size(payload: str) -> int:
    compact = "".join(payload.split())
    padding = len(compact) - len(compact.rstrip("="))
    return max(0, (len(compact) * 3 // 4) - padding)


def _data_url_payload_size(url: str) -> int | None:
    if not url.startswith("data:") or "," not in url:
        return None
    header, payload = url.split(",", 1)
    if ";base64" not in header:
        return len(unquote(payload).encode("utf-8"))
    return _base64_payload_size(payload)


def _local_media_path(url: str) -> Path | None:
    if not _is_local_path(url):
        return None
    local = file_url_to_path(url) if url.startswith("file://") else url
    return Path(local).expanduser()


def _media_payload_size(data: dict[str, Any]) -> int | None:
    source = data.get("source")
    if isinstance(source, dict) and source.get("type") == "base64":
        return _base64_payload_size(str(source.get("data") or ""))

    url = _source_url(data)
    data_url_size = _data_url_payload_size(url)
    if data_url_size is not None:
        return data_url_size

    path = _local_media_path(url)
    if path is None or not path.exists() or not path.is_file():
        return None
    return path.stat().st_size


class MediaPayloadBoundary:
    """Track per-request provider media limits before encoding payloads."""

    def __init__(self) -> None:
        self.count = 0
        self.total_bytes = 0

    def add(self, block_type: str, data: dict[str, Any]) -> None:
        if block_type not in _MEDIA_BLOCK_TYPES:
            return
        self.count += 1
        if self.count > MAX_MEDIA_BLOCKS_PER_REQUEST:
            raise ModelFormatterError(
                message=(
                    "Too many media blocks for one model request "
                    f"(max {MAX_MEDIA_BLOCKS_PER_REQUEST})."
                ),
            )

        size = _media_payload_size(data)
        if size is None:
            return
        if size > MAX_LOCAL_MEDIA_BYTES:
            raise ModelFormatterError(
                message=(
                    f"{block_type.title()} media exceeds per-file limit "
                    f"({size} > {MAX_LOCAL_MEDIA_BYTES} bytes)."
                ),
            )
        self.total_bytes += size
        if self.total_bytes > MAX_LOCAL_MEDIA_TOTAL_BYTES:
            raise ModelFormatterError(
                message=(
                    "Local media payload exceeds per-request limit "
                    f"({self.total_bytes} > {MAX_LOCAL_MEDIA_TOTAL_BYTES} bytes)."
                ),
            )


def validate_media_payload_boundary(
    block_type: str,
    data: dict[str, Any],
    boundary: MediaPayloadBoundary | None = None,
) -> None:
    (boundary or MediaPayloadBoundary()).add(block_type, data)


def _normalise_media_data(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    normalized = dict(data)
    source = normalized.get("source")
    if isinstance(source, dict):
        normalized["source"] = dict(source)
        media_type = normalized["source"].get("media_type")
        if isinstance(media_type, str):
            normalized["source"]["media_type"] = _fixed_mime(media_type)

    url = _source_url(normalized)
    if url.startswith("file://"):
        url = file_url_to_path(url)
        _set_source_url(normalized, url)

    if _is_local_path(url) and not os.path.exists(url):
        return normalized, False

    return normalized, True


def _media_unavailable_block(block_type: str) -> ModelContentBlock:
    return ModelContentBlock(
        type="text",
        text=f"[{block_type.title()} unavailable - file deleted from disk]",
    )


def _strip_media_items(items: list[Any]) -> list[Any]:
    kept: list[Any] = []
    for item in items:
        if not isinstance(item, dict):
            kept.append(item)
            continue
        item_type = item.get("type")
        if item_type in _MEDIA_BLOCK_TYPES:
            continue
        if item_type == "tool_result" and isinstance(item.get("output"), list):
            item = dict(item)
            item["output"] = _strip_media_items(item["output"])
            if not item["output"]:
                item["output"] = MEDIA_UNSUPPORTED_PLACEHOLDER
        kept.append(item)
    return kept


def _normalise_media_items(
    items: list[Any],
    boundary: MediaPayloadBoundary,
) -> list[Any]:
    result: list[Any] = []
    for item in items:
        if not isinstance(item, dict):
            result.append(item)
            continue
        item_type = item.get("type")
        if item_type in _MEDIA_BLOCK_TYPES:
            validate_media_payload_boundary(str(item_type), item, boundary)
            data, exists = _normalise_media_data(item)
            if exists:
                result.append(data)
            else:
                result.append(
                    {
                        "type": "text",
                        "text": f"[{str(item_type).title()} unavailable - file deleted from disk]",
                    },
                )
            continue
        if item_type == "tool_result" and isinstance(item.get("output"), list):
            item = dict(item)
            item["output"] = _normalise_media_items(item["output"], boundary)
        result.append(item)
    return result


def _repair_tool_call_data(data: dict[str, Any]) -> None:
    arguments = data.get("arguments")
    raw_input = data.get("raw_input")
    if arguments or not raw_input or raw_input == "{}":
        return
    try:
        parsed = json.loads(raw_input)
    except (TypeError, ValueError):
        return
    if isinstance(parsed, dict) and parsed:
        data["arguments"] = parsed


def _clean_provider_fields(data: dict[str, Any], target_family: str) -> None:
    preserve = _GEMINI_NATIVE_FIELDS if target_family == "gemini" else frozenset()
    for field in _PROVIDER_ONLY_TOOL_CALL_FIELDS - preserve:
        data.pop(field, None)


def _tool_call_ids(message: ModelMessage) -> list[str]:
    return [
        str(block.data.get("id") or "").strip()
        for block in message.content
        if block.type == "tool_call" and str(block.data.get("id") or "").strip()
    ]


def _tool_result_ids(message: ModelMessage) -> list[str]:
    result: list[str] = []
    for block in message.content:
        if block.type != "tool_result":
            continue
        call_id = str(
            block.data.get("id")
            or message.tool_call_id
            or message.metadata.get("tool_call_id")
            or message.id
            or ""
        ).strip()
        if call_id:
            result.append(call_id)
    if not result and message.role == "tool":
        call_id = str(
            message.tool_call_id
            or message.metadata.get("tool_call_id")
            or message.id
            or ""
        ).strip()
        if call_id:
            result.append(call_id)
    return result


def repair_incomplete_tool_transactions(
    messages: list[ModelMessage],
) -> list[ModelMessage]:
    """Drop only protocol-incomplete historical tool transactions.

    OpenAI-compatible providers reject an assistant ``tool_calls`` row unless
    it is immediately followed by a complete batch of matching tool results.
    Live Runtime rounds already preserve that invariant, but older persisted
    histories can contain the call without its result.  Never invent a result:
    retain any ordinary assistant content and discard only the broken protocol
    rows before they reach the provider.
    """

    repaired: list[ModelMessage] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        call_ids = _tool_call_ids(message)
        if message.role == "assistant" and call_ids:
            result_end = index + 1
            result_ids: list[str] = []
            while (
                result_end < len(messages)
                and messages[result_end].role == "tool"
            ):
                result_ids.extend(_tool_result_ids(messages[result_end]))
                result_end += 1

            if (
                len(call_ids) == len(set(call_ids))
                and len(result_ids) == len(set(result_ids))
                and set(call_ids) == set(result_ids)
            ):
                repaired.extend(messages[index:result_end])
            else:
                assistant = message.model_copy(deep=True)
                assistant.content = [
                    block
                    for block in assistant.content
                    if block.type != "tool_call"
                ]
                if assistant.content:
                    repaired.append(assistant)
            index = result_end
            continue

        if message.role == "tool" or message.tool_call_id:
            # A tool row outside an immediately preceding complete assistant
            # transaction is equally invalid and cannot be replayed safely.
            index += 1
            continue

        repaired.append(message)
        index += 1
    return repaired


def normalize_model_messages_for_provider(
    messages: list[ModelMessage],
    *,
    supports_multimodal: bool | None,
    target_family: str = "openai",
) -> list[ModelMessage]:
    """Return a normalized copy suitable for provider payload formatting."""

    allow_media = True if supports_multimodal is None else bool(supports_multimodal)
    boundary = MediaPayloadBoundary()
    normalized: list[ModelMessage] = []
    for message in messages:
        msg = message.model_copy(deep=True)
        new_blocks: list[ModelContentBlock] = []
        stripped_media = False

        for block in msg.content:
            if (
                block.type == "thinking"
                and target_family != "anthropic"
                and any(
                    key in block.data
                    for key in ("signature", "redacted_thinking", "data")
                )
            ):
                # Signed/opaque Anthropic state is valid only for the source
                # wire protocol. Canonical history keeps the original block;
                # this provider-specific copy omits it rather than disguising
                # it as another provider's reasoning text.
                continue
            if block.type == "tool_call":
                block.data = dict(block.data)
                _repair_tool_call_data(block.data)
                _clean_provider_fields(block.data, target_family)
                new_blocks.append(block)
                continue

            if block.type == "tool_result":
                block.data = dict(block.data)
                output = block.data.get("output")
                if isinstance(output, list):
                    block.data["output"] = (
                        _normalise_media_items(output, boundary)
                        if allow_media
                        else _strip_media_items(output)
                    )
                    if not block.data["output"]:
                        block.data["output"] = MEDIA_UNSUPPORTED_PLACEHOLDER
                new_blocks.append(block)
                continue

            if block.type in _MEDIA_BLOCK_TYPES:
                if not allow_media:
                    stripped_media = True
                    continue
                validate_media_payload_boundary(block.type, block.data, boundary)
                data, exists = _normalise_media_data(block.data)
                if exists:
                    block.data = data
                    new_blocks.append(block)
                else:
                    new_blocks.append(_media_unavailable_block(block.type))
                continue

            new_blocks.append(block)

        if not new_blocks and stripped_media:
            new_blocks.append(
                ModelContentBlock(type="text", text=MEDIA_UNSUPPORTED_PLACEHOLDER),
            )
        msg.content = new_blocks
        normalized.append(msg)

    return normalized


def media_payload_url(block_type: str, data: dict[str, Any]) -> str:
    """Return a provider payload URL, encoding local media as data URLs."""

    validate_media_payload_boundary(block_type, data)
    if block_type == "file":
        source = data.get("source")
        if isinstance(source, dict) and source.get("type") == "base64":
            _filename, media_type = file_payload_metadata(data)
            payload = str(source.get("data") or "")
            return f"data:{media_type};base64,{payload}" if payload else ""

    url = _source_url(data)
    if not url:
        return ""
    if url.startswith("data:"):
        if block_type == "file" and "," in url:
            _filename, media_type = file_payload_metadata(data)
            header, payload = url.split(",", 1)
            if ";base64" in header:
                return f"data:{media_type};base64,{payload}"
        for wrong, right in _MIME_FIXES.items():
            if url.startswith(f"data:{wrong};"):
                return url.replace(f"data:{wrong};", f"data:{right};", 1)
        return url
    if url.startswith("file://"):
        url = file_url_to_path(url)
    if url.startswith(("http://", "https://")):
        return url
    path = Path(url).expanduser()
    if not path.exists() or not path.is_file():
        return url
    source = data.get("source")
    media_type = ""
    if isinstance(source, dict):
        media_type = str(source.get("media_type") or "")
    if not media_type:
        ext = path.suffix.lower()
        if block_type == "image":
            media_type = _IMAGE_MIME_BY_EXT.get(ext, "")
        elif block_type == "video":
            media_type = _VIDEO_MIME_BY_EXT.get(ext, "")
        if not media_type:
            media_type = mimetypes.guess_type(str(path))[0] or ""
    media_type = _fixed_mime(media_type or "application/octet-stream")
    encoded = base64.b64encode(path.read_bytes()).decode("utf-8")
    return f"data:{media_type};base64,{encoded}"


__all__ = [
    "MediaPayloadBoundary",
    "file_payload_metadata",
    "file_url_to_path",
    "media_payload_url",
    "normalize_model_messages_for_provider",
    "repair_incomplete_tool_transactions",
    "validate_media_payload_boundary",
]
