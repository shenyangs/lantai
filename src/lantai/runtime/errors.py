# -*- coding: utf-8 -*-
"""Runtime-facing exception formatting helpers."""

from __future__ import annotations

import asyncio


def runtime_exception_message(exc: BaseException, *, fallback: str) -> str:
    """Return a non-empty, user-facing summary for runtime failures."""

    message = str(exc).strip()
    if message:
        return message
    if isinstance(exc, asyncio.TimeoutError):
        return f"{fallback} timed out."
    return f"{fallback}: {type(exc).__name__}"


__all__ = ["runtime_exception_message"]
