# -*- coding: utf-8 -*-
"""Provider-specific request profiles for OpenAI-compatible endpoints."""

from __future__ import annotations

import os

from .claude_code_version import (
    EMBEDDED_STABLE_VERSION,
    resolve_claude_cli_user_agent,
)

KIMI_CODING_BASE_URL = "https://api.kimi.com/coding/v1"
KIMI_CODING_MODEL_ID = "kimi-for-coding"
KIMI_CODING_USER_AGENT = (
    f"claude-cli/{EMBEDDED_STABLE_VERSION} (consumer, cli)"
)
KIMI_CODING_X_APP = "cli"


def _normalize_base_url(base_url: str) -> str:
    return (base_url or "").strip().rstrip("/")


def is_kimi_coding_base_url(base_url: str) -> bool:
    """Return whether a base URL points at Kimi Code Plan."""
    return _normalize_base_url(base_url) == KIMI_CODING_BASE_URL


def default_headers_for_base_url(base_url: str) -> dict[str, str] | None:
    """Return endpoint-specific default headers, if the endpoint needs them."""
    if not is_kimi_coding_base_url(base_url):
        return None
    configured_user_agent = os.environ.get(
        "LANTAI_KIMI_CODING_USER_AGENT",
        "",
    ).strip()
    return {
        "User-Agent": configured_user_agent or resolve_claude_cli_user_agent(),
        "x-app": os.environ.get(
            "LANTAI_KIMI_CODING_X_APP",
            KIMI_CODING_X_APP,
        ),
    }
