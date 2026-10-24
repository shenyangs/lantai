# -*- coding: utf-8 -*-
"""Write-time sanitization for runtime state and event payloads."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


RUNTIME_PAYLOAD_REDACTION_VERSION = 1
MAX_RUNTIME_PAYLOAD_STRING_CHARS = 12_000
_SENSITIVE_EXACT_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "access_token",
    "auth_token",
    "refresh_token",
    "id_token",
    "token",
    "cookie",
    "password",
    "passwd",
    "secret",
    "credential",
    "bookmark_blob",
    "security_scoped_bookmark",
}
_SENSITIVE_SUFFIXES = (
    "_api_key",
    "_access_token",
    "_auth_token",
    "_refresh_token",
    "_id_token",
    "_token",
    "_cookie",
    "_password",
    "_passwd",
    "_secret",
    "_credential",
)
_USER_CONTENT_KEYS = {
    "prompt",
    "prompt_preview",
    "text",
    "text_preview",
    "stdout",
    "stderr",
    "output",
    "tool_output",
    "result_preview",
    "body",
    "html",
}


class RuntimePayloadSanitizationError(ValueError):
    """Raised when a runtime payload cannot be safely sanitized."""


@dataclass
class _SanitizerState:
    redactions: list[dict[str, object]] = field(default_factory=list)
    truncated: list[dict[str, object]] = field(default_factory=list)
    saw_user_content: bool = False


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_ref(value: Any) -> str:
    digest = hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    return lowered in _SENSITIVE_EXACT_KEYS or lowered.endswith(_SENSITIVE_SUFFIXES)


def _path_child(path: str, key: str | int) -> str:
    if isinstance(key, int):
        return f"{path}[{key}]"
    return f"{path}.{key}" if path else key


def _redacted_marker(value: Any) -> dict[str, object]:
    marker: dict[str, object] = {
        "redacted": True,
        "sha256": _sha256_ref(value),
    }
    if isinstance(value, str):
        marker["length"] = len(value)
    return marker


def _sanitize_value(value: Any, *, path: str, state: _SanitizerState) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= MAX_RUNTIME_PAYLOAD_STRING_CHARS:
            return value
        state.truncated.append(
            {
                "path": path,
                "sha256": _sha256_ref(value),
                "original_length": len(value),
                "kept_length": MAX_RUNTIME_PAYLOAD_STRING_CHARS,
            }
        )
        return value[:MAX_RUNTIME_PAYLOAD_STRING_CHARS]
    if isinstance(value, list):
        return [
            _sanitize_value(item, path=_path_child(path, index), state=state)
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                raise RuntimePayloadSanitizationError(
                    f"Runtime payload key is not a string at {path or '<root>'}",
                )
            child_path = _path_child(path, raw_key)
            if raw_key in _USER_CONTENT_KEYS:
                state.saw_user_content = True
            if _is_sensitive_key(raw_key):
                state.redactions.append(
                    {
                        "path": child_path,
                        "sha256": _sha256_ref(raw_value),
                    }
                )
                sanitized[raw_key] = _redacted_marker(raw_value)
                continue
            sanitized[raw_key] = _sanitize_value(
                raw_value,
                path=child_path,
                state=state,
            )
        return sanitized
    raise RuntimePayloadSanitizationError(
        f"Runtime payload value is not JSON-compatible at {path or '<root>'}",
    )


def sanitize_runtime_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Return a JSON-compatible payload safe to persist to runtime storage."""

    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise RuntimePayloadSanitizationError("Runtime payload must be an object")
    existing_classification = str(payload.get("data_classification") or "").strip()

    state = _SanitizerState()
    sanitized: dict[str, Any] = {}
    for raw_key, raw_value in payload.items():
        if not isinstance(raw_key, str):
            raise RuntimePayloadSanitizationError("Runtime payload keys must be strings")
        if raw_key in _USER_CONTENT_KEYS:
            state.saw_user_content = True
        if _is_sensitive_key(raw_key):
            state.redactions.append(
                {
                    "path": raw_key,
                    "sha256": _sha256_ref(raw_value),
                }
            )
            continue
        sanitized[raw_key] = _sanitize_value(raw_value, path=raw_key, state=state)

    if state.redactions:
        sanitized["redactions"] = state.redactions
    if state.truncated:
        sanitized["truncations"] = state.truncated
    sanitized["redaction_version"] = RUNTIME_PAYLOAD_REDACTION_VERSION
    if state.redactions or existing_classification == "sensitive":
        sanitized["data_classification"] = "sensitive"
    elif state.saw_user_content or existing_classification == "user_content":
        sanitized["data_classification"] = "user_content"
    else:
        sanitized["data_classification"] = "internal"
    return sanitized
