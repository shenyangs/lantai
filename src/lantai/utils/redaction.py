# -*- coding: utf-8 -*-
"""Local diagnostic redaction helpers.

These helpers sanitize data that remains on the user's machine or is shown in
the local debug UI.  They do not collect, queue, or upload any data.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any


_SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|authorization|bearer|cookie|credential|password|secret|"
    r"token|member[_-]?gateway|refresh[_-]?token|access[_-]?token)",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_QUERY_RE = re.compile(r"([?&])[^#\s'\"]+")
_USER_PATH_RE = re.compile(
    r"(?:(/Users|/home)/[^/\s:'\"]+|[A-Za-z]:\\Users\\[^\\\s:'\"]+)"
    r"(?:[/\\][^\s:'\"]*)?"
)
_LONG_SECRET_RE = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")
_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b")


def redact_sensitive_value(value: Any, *, depth: int = 0) -> Any:
    """Return a JSON-safe value with common secrets and identifiers removed."""

    if depth > 6:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, Path):
        return _redact_text(str(value))
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _SENSITIVE_KEY_RE.search(key_text):
                sanitized[key_text] = "[redacted]"
            else:
                sanitized[key_text] = redact_sensitive_value(
                    item,
                    depth=depth + 1,
                )
        return sanitized
    if isinstance(value, (list, tuple, set)):
        return [
            redact_sensitive_value(item, depth=depth + 1)
            for item in list(value)[:50]
        ]
    return _redact_text(str(value))


def _redact_text(value: str) -> str:
    text = _OPENAI_KEY_RE.sub("[redacted_key]", value)
    text = _EMAIL_RE.sub("[redacted_email]", text)
    text = _QUERY_RE.sub(r"\1[redacted]", text)
    text = _USER_PATH_RE.sub("[redacted_path]", text)
    text = _LONG_SECRET_RE.sub("[redacted_id]", text)
    if len(text) > 4000:
        text = text[:4000] + "...[truncated]"
    return text
