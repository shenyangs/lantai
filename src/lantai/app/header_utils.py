# -*- coding: utf-8 -*-
"""Helpers for request header values shared by app routers."""

from __future__ import annotations

from urllib.parse import unquote


def decode_header_value(value: str | None) -> str | None:
    """Decode values encoded by the web client for HTTP-header safety."""
    if value is None:
        return None
    try:
        return unquote(value)
    except Exception:
        return value
