# -*- coding: utf-8 -*-
"""Resolve a recent Claude Code version without delaying model requests."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from urllib.request import Request, urlopen

from ..constant import WORKING_DIR

PACKAGE_NAME = "@anthropic-ai/claude-code"
NPM_DIST_TAGS_URL = (
    "https://registry.npmjs.org/-/package/"
    "%40anthropic-ai%2Fclaude-code/dist-tags"
)
EMBEDDED_STABLE_VERSION = "2.1.212"
REFRESH_INTERVAL_SECONDS = 7 * 24 * 60 * 60
FAILURE_RETRY_SECONDS = 6 * 60 * 60
QUERY_TIMEOUT_SECONDS = 5.0

_CACHE_SCHEMA_VERSION = 1
_CACHE_FILENAME = "claude-code-dist-tags.json"
_VERSION_PATTERN = re.compile(
    r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
_refresh_lock = threading.Lock()
_refresh_inflight = False


def _cache_path() -> Path:
    override = os.environ.get("LANTAI_CLAUDE_CODE_VERSION_CACHE_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return WORKING_DIR / "provider_cache" / _CACHE_FILENAME


def _valid_version(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not _VERSION_PATTERN.fullmatch(normalized):
        return None
    return normalized


def _read_cache(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            json.dump(
                payload,
                temporary_file,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
            temporary_path = Path(temporary_file.name)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _refresh_due(payload: dict[str, Any], *, now: float) -> bool:
    checked_at = payload.get("checked_at")
    if _valid_version(payload.get("stable_version")) and isinstance(
        checked_at,
        (int, float),
    ):
        if now - float(checked_at) < REFRESH_INTERVAL_SECONDS:
            return False

    last_attempt_at = payload.get("last_attempt_at")
    if payload.get("last_status") == "failed" and isinstance(
        last_attempt_at,
        (int, float),
    ):
        if now - float(last_attempt_at) < FAILURE_RETRY_SECONDS:
            return False
    return True


def _query_dist_tags() -> dict[str, Any]:
    request = Request(
        NPM_DIST_TAGS_URL,
        headers={
            "Accept": "application/json",
            "User-Agent": "Lantai/Claude-Code-Version-Resolver",
        },
    )
    with urlopen(request, timeout=QUERY_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("npm registry dist-tags response is not an object")
    return payload


def refresh_claude_code_version(
    *,
    cache_path: Path | None = None,
    now: float | None = None,
) -> str | None:
    """Refresh the cached stable version while keeping failures invisible."""
    resolved_path = cache_path or _cache_path()
    resolved_now = time.time() if now is None else now
    previous = _read_cache(resolved_path)

    try:
        stable_version = _valid_version(_query_dist_tags().get("stable"))
        if stable_version is None:
            raise ValueError("npm registry returned an invalid stable version")
        _write_cache(
            resolved_path,
            {
                "schema_version": _CACHE_SCHEMA_VERSION,
                "package": PACKAGE_NAME,
                "stable_version": stable_version,
                "checked_at": resolved_now,
                "last_attempt_at": resolved_now,
                "source": "npm_registry",
            },
        )
        return stable_version
    except Exception:  # noqa: BLE001 - refresh must never affect model requests
        failure_payload: dict[str, Any] = {
            "schema_version": _CACHE_SCHEMA_VERSION,
            "package": PACKAGE_NAME,
            "last_attempt_at": resolved_now,
            "last_status": "failed",
        }
        previous_version = _valid_version(previous.get("stable_version"))
        if previous_version is not None:
            failure_payload["stable_version"] = previous_version
        for key in ("checked_at", "source"):
            if key in previous:
                failure_payload[key] = previous[key]
        try:
            _write_cache(resolved_path, failure_payload)
        except Exception:  # noqa: BLE001 - a cache failure is also non-fatal
            pass
        return None


def _background_refresh(cache_path: Path, now: float | None) -> None:
    global _refresh_inflight
    try:
        refresh_claude_code_version(cache_path=cache_path, now=now)
    finally:
        with _refresh_lock:
            _refresh_inflight = False


def schedule_claude_code_version_refresh(
    *,
    cache_path: Path | None = None,
    now: float | None = None,
) -> bool:
    """Start at most one non-blocking refresh process-wide."""
    global _refresh_inflight
    resolved_path = cache_path or _cache_path()

    with _refresh_lock:
        if _refresh_inflight:
            return False
        _refresh_inflight = True

    try:
        worker = threading.Thread(
            target=_background_refresh,
            args=(resolved_path, now),
            name="claude-code-version-refresh",
            daemon=True,
        )
        worker.start()
        return True
    except Exception:  # noqa: BLE001 - thread startup must not affect requests
        with _refresh_lock:
            _refresh_inflight = False
        return False


def resolve_claude_cli_user_agent(*, now: float | None = None) -> str:
    """Return immediately from cache and refresh an expired value in background."""
    resolved_now = time.time() if now is None else now
    cache_path = _cache_path()
    payload = _read_cache(cache_path)
    cached_version = _valid_version(payload.get("stable_version"))
    stable_version = cached_version or EMBEDDED_STABLE_VERSION

    refresh_disabled = (
        os.environ.get("LANTAI_DISABLE_CLAUDE_CODE_VERSION_REFRESH", "").strip()
        == "1"
    )
    if not refresh_disabled and _refresh_due(payload, now=resolved_now):
        schedule_claude_code_version_refresh(
            cache_path=cache_path,
            now=resolved_now,
        )

    return f"claude-cli/{stable_version} (consumer, cli)"
