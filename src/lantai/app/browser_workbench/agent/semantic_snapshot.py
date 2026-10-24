"""Bridge native semantic snapshots into Lantai's actionable page contract.

The design borrows browser-use's separation of compact page content and
executable element identities. This parser implements our runtime formats;
it does not translate a provider ref into a guessed CSS selector.
"""

from __future__ import annotations

import json
import re
from typing import Any

_QUOTED = r'"(?:[^"\\]|\\.)*"'
_VOM_LINE = re.compile(
    r"^\s*(?P<ref>@e\d+)\s+(?P<role>[\w-]+)(?:\s+(?P<name>"
    + _QUOTED
    + r"))?(?P<suffix>.*)$"
)
_ARIA_LINE = re.compile(
    r"^\s*-\s*(?P<role>[\w-]+)(?:\s+(?P<name>"
    + _QUOTED
    + r"))?\s+\[ref=(?P<ref>e\d+)\](?P<suffix>.*)$"
)
_INPUT_ROLES = {"textbox", "searchbox", "combobox", "spinbutton"}


def snapshot_controls(payload: dict[str, Any], runtime: str) -> list[dict[str, Any]]:
    """Only accept runtime-shaped entries, never references embedded in prose."""
    if runtime not in {"browser_skill", "playwright_fallback"}:
        return []
    raw = payload.get("snapshot") or payload.get("text") or ""
    if not isinstance(raw, str):
        return []
    pattern = _VOM_LINE if runtime == "browser_skill" else _ARIA_LINE
    allowed_refs = payload.get("refs")
    if runtime == "playwright_fallback" and not isinstance(allowed_refs, list):
        return []
    allowed = {item for item in (allowed_refs or []) if isinstance(item, str)}
    parsed: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    for line in raw[:100_000].splitlines()[:2000]:
        match = pattern.fullmatch(line)
        if match is None:
            continue
        ref = match["ref"]
        if runtime == "playwright_fallback" and ref not in allowed:
            continue
        if ref in parsed:
            duplicates.add(ref)
            continue
        try:
            name = json.loads(match["name"]) if match["name"] else ""
        except json.JSONDecodeError:
            continue
        suffix = match["suffix"]
        role = match["role"].lower()
        item: dict[str, Any] = {
            "ref": ref,
            "role": role,
            "name": name,
            "native_ref": True,
            "disabled": "[disabled]" in suffix,
            "visible": "[hidden]" not in suffix,
            "frame_selector": payload.get("frame_selector", ""),
        }
        # Do not promote arbitrary input values from a rendered snapshot.
        if role in _INPUT_ROLES:
            item["sensitive"] = True
        for state in ("checked", "selected"):
            if f"[{state}]" in suffix or f"[{state}=true]" in suffix:
                item[state] = True
            elif f"[{state}=false]" in suffix:
                item[state] = False
        parsed[ref] = item
    return [item for ref, item in parsed.items() if ref not in duplicates]


def progress_text(text: str, runtime: str) -> str:
    """Ignore reference renumbering only in known runtime entry syntax."""
    if runtime == "browser_skill":
        return re.sub(r"(?m)^(\s*)@e\d+(?=\s+[\w-]+)", r"\1@ref", text)
    if runtime == "playwright_fallback":
        return re.sub(r"\[ref=e\d+\]", "[ref]", text)
    return text
