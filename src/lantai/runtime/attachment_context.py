# -*- coding: utf-8 -*-
"""Turn uploaded document files into bounded, model-readable context."""

from __future__ import annotations

import base64
import binascii
import io
import zipfile
from functools import lru_cache
from pathlib import Path
from typing import Any

from .model_normalizer import file_payload_metadata, file_url_to_path

MAX_ATTACHMENT_BYTES = 150 * 1024 * 1024
MAX_ATTACHMENT_CONTEXT_CHARS = 120_000
MAX_TEXT_PREVIEW_BYTES = MAX_ATTACHMENT_CONTEXT_CHARS * 4 + 4
MAX_ZIP_ENTRIES = 200
MAX_ZIP_MEMBER_PREVIEW_BYTES = 256 * 1024
MAX_ZIP_PREVIEW_BYTES = 1024 * 1024

_TEXT_EXTENSIONS = {
    ".c",
    ".cfg",
    ".conf",
    ".cpp",
    ".css",
    ".csv",
    ".go",
    ".h",
    ".hpp",
    ".htm",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".less",
    ".log",
    ".markdown",
    ".md",
    ".mjs",
    ".py",
    ".rb",
    ".rs",
    ".scss",
    ".sh",
    ".sql",
    ".svg",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}


def _source_details(
    data: dict[str, Any],
) -> tuple[bytes | None, Path | None, str]:
    source = data.get("source")
    if isinstance(source, dict):
        source_type = str(source.get("type") or "")
        if source_type == "base64":
            encoded = str(source.get("data") or "")
            if not encoded:
                return None, None, ""
            # Reject oversized inline payloads before allocating a second,
            # decoded copy in memory.
            max_encoded_chars = ((MAX_ATTACHMENT_BYTES + 2) // 3) * 4
            if len(encoded) > max_encoded_chars:
                return None, None, ""
            try:
                payload = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                return None, None, ""
            if len(payload) > MAX_ATTACHMENT_BYTES:
                return None, None, ""
            return payload, None, ""
        raw_url = source.get("url")
        if isinstance(raw_url, str) and raw_url:
            if raw_url.startswith(("http://", "https://")):
                return None, None, raw_url
            raw_path = (
                file_url_to_path(raw_url)
                if raw_url.startswith("file://")
                else raw_url
            )
            path = Path(raw_path).expanduser()
            if path.exists() and path.is_file():
                return None, path, str(path.resolve())
            return None, None, raw_url

    for key in ("path", "url"):
        value = data.get(key)
        if not isinstance(value, str) or not value:
            continue
        if value.startswith(("http://", "https://")):
            return None, None, value
        raw_path = file_url_to_path(value) if value.startswith("file://") else value
        path = Path(raw_path).expanduser()
        if path.exists() and path.is_file():
            return None, path, str(path.resolve())
        return None, None, value
    return None, None, ""


def _decode_text(payload: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace")


def _zip_context(payload: bytes | Path) -> str:
    source: bytes | Path | io.BytesIO = (
        io.BytesIO(payload) if isinstance(payload, bytes) else payload
    )
    if not zipfile.is_zipfile(source):
        return ""
    lines = ["Archive contents:"]
    preview_parts: list[str] = []
    preview_bytes = 0
    with zipfile.ZipFile(source) as archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        for member in members[:MAX_ZIP_ENTRIES]:
            lines.append(f"- {member.filename} ({member.file_size} bytes)")
            suffix = Path(member.filename).suffix.lower()
            if (
                suffix not in _TEXT_EXTENSIONS
                or member.file_size > MAX_ZIP_MEMBER_PREVIEW_BYTES
                or preview_bytes >= MAX_ZIP_PREVIEW_BYTES
            ):
                continue
            allowed = min(
                member.file_size,
                MAX_ZIP_PREVIEW_BYTES - preview_bytes,
            )
            with archive.open(member) as stream:
                member_payload = stream.read(allowed + 1)
            if len(member_payload) > allowed:
                member_payload = member_payload[:allowed]
            preview_bytes += len(member_payload)
            preview_parts.append(
                f"\n--- {member.filename} ---\n{_decode_text(member_payload)}",
            )
        if len(members) > MAX_ZIP_ENTRIES:
            lines.append(
                f"- … {len(members) - MAX_ZIP_ENTRIES} more entries omitted",
            )
    return "\n".join(lines + preview_parts)


def _convert_payload(payload: bytes, suffix: str) -> str:
    if suffix == ".zip":
        return _zip_context(payload)
    if suffix in _TEXT_EXTENSIONS:
        return _decode_text(payload)

    try:
        from markitdown import MarkItDown

        result = MarkItDown().convert_stream(
            io.BytesIO(payload),
            file_extension=suffix or None,
        )
        return str(result.text_content or "")
    except Exception:
        return ""


def _read_bounded_text_file(path: Path) -> bytes:
    """Read only enough bytes to fill the bounded public attachment context."""

    with path.open("rb") as stream:
        return stream.read(MAX_TEXT_PREVIEW_BYTES)


@lru_cache(maxsize=64)
def _convert_local_file(
    path_value: str,
    modified_ns: int,
    file_size: int,
) -> str:
    del modified_ns
    if file_size > MAX_ATTACHMENT_BYTES:
        return ""
    path = Path(path_value)
    if path.suffix.lower() == ".zip":
        return _zip_context(path)
    if path.suffix.lower() in _TEXT_EXTENSIONS:
        return _decode_text(_read_bounded_text_file(path))
    try:
        from markitdown import MarkItDown

        result = MarkItDown().convert(path)
        return str(result.text_content or "")
    except Exception:
        return ""


def attachment_context_text(data: dict[str, Any]) -> str:
    """Return a bounded prompt block for one uploaded file.

    Images remain native multimodal blocks elsewhere. Generic files are
    converted to text because chat-completions APIs do not share a portable
    ``file_url`` content type. The local path is retained so coding agents can
    still inspect formats that cannot be converted automatically.
    """

    filename, media_type = file_payload_metadata(data)
    payload, path, location = _source_details(data)
    suffix = Path(filename).suffix.lower()
    extracted = ""

    if path is not None:
        stat = path.stat()
        extracted = _convert_local_file(
            str(path.resolve()),
            stat.st_mtime_ns,
            stat.st_size,
        )
    elif payload is not None:
        extracted = _convert_payload(payload, suffix)

    extracted = extracted.strip()
    truncated = len(extracted) > MAX_ATTACHMENT_CONTEXT_CHARS
    if truncated:
        extracted = extracted[:MAX_ATTACHMENT_CONTEXT_CHARS].rstrip()

    header = (
        f"<attachment name={filename!r} mime_type={media_type!r}"
        + (f" location={location!r}" if location else "")
        + ">"
    )
    body = (
        extracted
        if extracted
        else (
            "No readable text was extracted automatically. "
            "Use the local file tools when a local location is available."
        )
    )
    if truncated:
        body += "\n[Attachment text truncated by Lantai.]"
    return f"{header}\n{body}\n</attachment>"


__all__ = [
    "MAX_ATTACHMENT_BYTES",
    "MAX_ATTACHMENT_CONTEXT_CHARS",
    "MAX_TEXT_PREVIEW_BYTES",
    "attachment_context_text",
]
