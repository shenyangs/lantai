# -*- coding: utf-8 -*-
"""Compact, deterministic search over Browser Workbench snapshots."""

from __future__ import annotations

import re
from typing import Any


class SnapshotSearchRegexError(ValueError):
    """Raised when a browser snapshot regex cannot be compiled."""


def _compile_matcher(
    *,
    text: str,
    regex: str,
) -> tuple[re.Pattern[str], str, str]:
    plain = str(text or "").strip()
    pattern = str(regex or "").strip()
    if bool(plain) == bool(pattern):
        raise ValueError("Provide exactly one of text or regex.")
    if plain:
        return re.compile(re.escape(plain), re.IGNORECASE), plain, "text"

    flags = 0
    source = pattern
    wrapped = re.fullmatch(r"/(.*)/([a-zA-Z]*)", pattern, re.DOTALL)
    if wrapped:
        source = wrapped.group(1)
        flag_text = wrapped.group(2)
        unknown = set(flag_text) - {"i", "m", "s"}
        if unknown:
            raise SnapshotSearchRegexError(
                "Unsupported regex flag(s): " + ", ".join(sorted(unknown))
            )
        if "i" in flag_text:
            flags |= re.IGNORECASE
        if "m" in flag_text:
            flags |= re.MULTILINE
        if "s" in flag_text:
            flags |= re.DOTALL
    try:
        compiled = re.compile(source, flags)
    except re.error as exc:
        raise SnapshotSearchRegexError(str(exc)) from exc
    return compiled, pattern, "regex"


def validate_snapshot_search_query(*, text: str = "", regex: str = "") -> None:
    """Validate mutually exclusive snapshot search input without browser I/O."""

    _compile_matcher(text=text, regex=regex)


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _snapshot_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def _snippet(value: str, match: re.Match[str], context_chars: int) -> str:
    start = max(0, match.start() - context_chars)
    end = min(len(value), match.end() + context_chars)
    prefix = "…" if start else ""
    suffix = "…" if end < len(value) else ""
    return f"{prefix}{value[start:end].strip()}{suffix}"


def search_snapshot_payload(
    payload: dict[str, Any],
    *,
    text: str = "",
    regex: str = "",
    max_results: int = 20,
    context_chars: int = 96,
) -> dict[str, Any]:
    """Return compact matches without echoing the complete page snapshot."""

    pattern, query, query_kind = _compile_matcher(text=text, regex=regex)
    limit = max(1, min(int(max_results or 20), 50))
    context = max(0, min(int(context_chars or 0), 500))
    data = _snapshot_data(payload)
    matches: list[dict[str, Any]] = []
    total = 0

    def add(candidate: dict[str, Any]) -> None:
        nonlocal total
        total += 1
        if len(matches) < limit:
            matches.append(candidate)

    controls = _dict_list(data.get("controls"))
    blocked_selectors = {
        str(control.get("selector") or "").strip()
        for control in controls
        if control.get("sensitive") or control.get("visible") is False
    }
    blocked_selectors.discard("")

    for control in controls:
        if control.get("sensitive") or control.get("visible") is False:
            continue
        role = str(control.get("role") or control.get("tag") or "control")
        name = str(control.get("name") or "").strip()
        selector = str(control.get("selector") or "").strip()
        value = " ".join(part for part in (role, name, selector) if part)
        match = pattern.search(value)
        if not match:
            continue
        add(
            {
                "kind": "control",
                "role": role,
                "name": name,
                "selector": selector,
                "frame_selector": str(control.get("frame_selector") or ""),
                "shadow_host_selector": str(
                    control.get("shadow_host_selector") or ""
                ),
            }
        )

    for link in _dict_list(data.get("links")):
        label = str(link.get("text") or "").strip()
        href = str(link.get("href") or "").strip()
        selector = str(link.get("selector") or "").strip()
        if (
            link.get("sensitive")
            or link.get("visible") is False
            or selector in blocked_selectors
        ):
            continue
        value = " ".join(part for part in (label, href, selector) if part)
        if not pattern.search(value):
            continue
        add(
            {
                "kind": "link",
                "name": label,
                "href": href,
                "selector": selector,
            }
        )

    snapshot = payload.get("snapshot") or data.get("snapshot")
    if isinstance(snapshot, str):
        for line_number, line in enumerate(snapshot.splitlines(), start=1):
            match = pattern.search(line)
            if match:
                add(
                    {
                        "kind": "snapshot_node",
                        "line": line_number,
                        "excerpt": _snippet(line, match, context),
                    }
                )

    page_text = data.get("text") if isinstance(data.get("text"), str) else None
    if page_text is None and isinstance(payload.get("text"), str):
        page_text = payload["text"]
    if page_text:
        for match in pattern.finditer(page_text):
            add(
                {
                    "kind": "page_text",
                    "excerpt": _snippet(page_text, match, context),
                }
            )

    return {
        "ok": bool(payload.get("ok", True)),
        "action": "find",
        "runtime": str(payload.get("runtime") or ""),
        "page_id": str(
            payload.get("page_id") or payload.get("tab_id") or "default"
        ),
        "profile_id": str(payload.get("profile_id") or "default"),
        "url": str(payload.get("url") or data.get("url") or ""),
        "title": str(data.get("title") or payload.get("title") or ""),
        "query": query,
        "query_kind": query_kind,
        "match_count": total,
        "returned_match_count": len(matches),
        "truncated": total > len(matches),
        "matches": matches,
        "message": (
            f"Found {total} matching snapshot item(s)."
            if total
            else "No matching snapshot items found."
        ),
    }


__all__ = [
    "SnapshotSearchRegexError",
    "search_snapshot_payload",
    "validate_snapshot_search_query",
]
