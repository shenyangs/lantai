"""Bounded, expiring extracted-text snapshots. No credentials or disk persistence."""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from secrets import token_urlsafe
from threading import RLock
from time import monotonic

_lock = RLock()
_entries: OrderedDict[str, tuple[float, str, str, dict]] = OrderedDict()
_MAX_CHARS = 8_000_000
_TTL = 900
_generation = 0


def cache_generation() -> int:
    with _lock:
        return _generation


def clear_read_cache() -> None:
    global _generation
    with _lock:
        _generation += 1
        _entries.clear()


def store_read(scope: str, file_id: str, result: dict, generation: int) -> str:
    with _lock:
        if generation != _generation:
            return ""
        now = monotonic()
        for key, (created, *_rest) in list(_entries.items()):
            if now - created > _TTL:
                del _entries[key]
        size = len(result["content"])
        if size > _MAX_CHARS:
            return ""
        while _entries and (len(_entries) >= 16 or sum(len(v[3]["content"]) for v in _entries.values()) + size > _MAX_CHARS):
            _entries.popitem(last=False)
        key = token_urlsafe(24)
        _entries[key] = (now, scope, file_id, deepcopy(result))
        return key


def read_page(scope: str, file_id: str, cursor: str, limit: int) -> dict | None:
    with _lock:
        key, _, offset_text = cursor.partition(":")
        entry = _entries.get(key)
        if not entry or entry[1:3] != (scope, file_id) or monotonic() - entry[0] > _TTL:
            return None
        if not offset_text.isdigit() or len(offset_text) > 8:
            return None
        offset = int(offset_text)
        result = deepcopy(entry[3])
        text = result["content"]
        if not 0 <= offset < len(text):
            return None
        end = min(len(text), offset + limit)
        result["content"] = text[offset:end]
        result["continuation"] = f"{key}:{end}" if end < len(text) else ""
        result["truncated"] = end < len(text) or result["truncated"]
        if end < len(text):
            result["coverage"]["body"] = "partial"
        result["snapshot_id"] = key
        return result
