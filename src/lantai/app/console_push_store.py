# -*- coding: utf-8 -*-
"""In-memory store for console channel push messages (e.g. cron text).

Ordinary messages expire after a short delivery window.  Sticky messages are
kept until the user acknowledges them, while the global count limit still
bounds memory use.  Message receipts retain their source and occurrence time
so the console can explain where a notification came from.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Dict, List

# Single list: each item has id, text, ts, session_id and optional metadata.
# Bounded by count and age.
_list: List[Dict[str, Any]] = []
_lock = asyncio.Lock()
_MAX_AGE_SECONDS = 60
_MAX_MESSAGES = 500


async def append(
    session_id: str,
    text: str,
    *,
    sticky: bool = False,
    source_kind: str | None = None,
    source_id: str | None = None,
    source_name: str | None = None,
) -> str | None:
    """Append a message (bounded: oldest dropped if over _MAX_MESSAGES)."""
    if not session_id or not text:
        return None
    message_id = str(uuid.uuid4())
    async with _lock:
        _list.append(
            {
                "id": message_id,
                "text": text,
                "sticky": sticky,
                "ts": time.time(),
                "session_id": session_id,
                "source_kind": source_kind,
                "source_id": source_id,
                "source_name": source_name,
            },
        )
        if len(_list) > _MAX_MESSAGES:
            _list.sort(key=lambda m: m["ts"])
            del _list[: len(_list) - _MAX_MESSAGES]
    return message_id


async def acknowledge(message_id: str) -> bool:
    """Remove one message after an explicit user acknowledgement."""
    if not message_id:
        return False
    async with _lock:
        for index, message in enumerate(_list):
            if message.get("id") == message_id:
                del _list[index]
                return True
    return False


async def take(session_id: str) -> List[Dict[str, Any]]:
    """Return and remove all messages for the session."""
    if not session_id:
        return []
    async with _lock:
        _prune_expired_locked(_MAX_AGE_SECONDS)
        out = []
        remaining = []
        for msg in _list:
            if msg.get("session_id") == session_id:
                out.append(msg)
            else:
                remaining.append(msg)
        _list[:] = remaining
        return _to_receipts(out)


async def take_all() -> List[Dict[str, Any]]:
    """Return and remove all non-expired messages from the store."""
    async with _lock:
        _prune_expired_locked(_MAX_AGE_SECONDS)
        out = list(_list)
        _list.clear()
        return _to_receipts(out)


def _to_receipts(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "id": m["id"],
            "text": m["text"],
            "sticky": bool(m.get("sticky", False)),
            "occurred_at": m["ts"],
            "session_id": m.get("session_id"),
            "source_kind": m.get("source_kind"),
            "source_id": m.get("source_id"),
            "source_name": m.get("source_name"),
        }
        for m in msgs
    ]


def _prune_expired_locked(max_age_seconds: int) -> None:
    """Drop expired ordinary messages in-place. Caller must hold _lock."""
    cutoff = time.time() - max_age_seconds
    _list[:] = [
        m
        for m in _list
        if bool(m.get("sticky", False)) or m["ts"] >= cutoff
    ]


async def get_recent(
    max_age_seconds: int = _MAX_AGE_SECONDS,
) -> List[Dict[str, Any]]:
    """
    Return recent messages (not consumed). Drop older than max_age_seconds
    from store to bound memory.
    """
    if max_age_seconds < 0:
        raise ValueError("max_age_seconds must be non-negative")

    async with _lock:
        _prune_expired_locked(max_age_seconds)
        return _to_receipts(_list)
