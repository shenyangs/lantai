# -*- coding: utf-8 -*-
"""Normalize raw BrowserObservation payloads into stable model-facing state."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..models import BrowserObservation
from .contracts import (
    BrowserAgentBudget,
    BrowserInteractiveElement,
    BrowserPageState,
    BrowserTabState,
    BrowserTextBlock,
)
from .safety import (
    detect_page_security_signals,
    redact_untrusted_text,
    redact_untrusted_value,
)
from .semantic_snapshot import progress_text, snapshot_controls


_SENSITIVE_INPUT_TYPES = frozenset(
    {"password", "hidden", "otp", "captcha", "credit-card", "cc-number"}
)


def _stable_hash(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _payload_data(observation: BrowserObservation) -> dict[str, Any]:
    raw = redact_untrusted_value(observation.data)
    payload = raw if isinstance(raw, dict) else {}
    nested = payload.get("data")
    payload = nested if isinstance(nested, dict) else payload
    source = observation.data.get("data", observation.data)
    # Generic redaction flattens whitespace. Native snapshots have one
    # executable handle per line; preserve that grammar while redacting each
    # line before it reaches the parser or model.
    if observation.runtime in {"browser_skill", "playwright_fallback"} and isinstance(source, dict):
        for key in ("snapshot", "text"):
            value = source.get(key)
            if isinstance(value, str) and isinstance(payload.get(key), str):
                payload[key] = "\n".join(
                    redact_untrusted_text(line, limit=20_000)
                    for line in value[:20_000].splitlines()
                )
    return payload


def _text(value: object, *, limit: int) -> str:
    return redact_untrusted_text(value, limit=limit)


def _bool(value: object, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _selector_for_control(item: dict[str, Any], index: int) -> str:
    selector = _text(item.get("selector"), limit=500)
    if selector:
        return selector
    return ""


def _element_ref(
    item: dict[str, Any],
    *,
    index: int,
    selector: str,
) -> str:
    explicit = _text(item.get("ref"), limit=240)
    if explicit:
        return explicit
    identity = {
        "selector": selector,
        "role": _text(item.get("role"), limit=120),
        "name": _text(
            item.get("name") or item.get("text") or item.get("aria_label"),
            limit=200,
        ),
        "tag": _text(item.get("tag"), limit=80),
        "frame": _text(item.get("frame_selector"), limit=300),
        "shadow": _text(item.get("shadow_host_selector"), limit=300),
        "index": index,
    }
    return "el:" + _stable_hash(identity).removeprefix("sha256:")[:16]


def _value_state(item: dict[str, Any]) -> tuple[str, str]:
    input_type = _text(item.get("type") or item.get("input_type"), limit=80).lower()
    if input_type in _SENSITIVE_INPUT_TYPES or _bool(item.get("sensitive")):
        return "redacted", ""
    if "value" not in item and "value_length" not in item:
        return "unknown", ""
    value = _text(item.get("value"), limit=2000)
    value_length = item.get("value_length")
    present = bool(value) or (isinstance(value_length, int) and value_length > 0)
    if not present:
        return "empty", ""
    return "present", _stable_hash(value or f"length:{value_length}")


def _interactive_elements(
    payload: dict[str, Any],
    *,
    budget: BrowserAgentBudget,
    runtime: str = "electron_bridge",
) -> tuple[BrowserInteractiveElement, ...]:
    raw_controls = payload.get("controls") or payload.get("interactive_elements") or []
    native_controls = not bool(raw_controls)
    if native_controls:
        raw_controls = snapshot_controls(payload, runtime)
    if not isinstance(raw_controls, list):
        return ()
    out: list[BrowserInteractiveElement] = []
    # Preserve source indices for ref identity; filter before applying the cap.
    for index, raw_item in enumerate(raw_controls):
        if not isinstance(raw_item, dict):
            continue
        if raw_item.get("visible") is False:
            continue
        if len(out) >= budget.max_interactive_elements:
            break
        item = raw_item
        selector = _selector_for_control(item, index)
        state, value_hash = _value_state(item)
        out.append(
            BrowserInteractiveElement(
                ref=_element_ref(item, index=index, selector=selector),
                target_kind="runtime_ref" if native_controls else "selector",
                selector=selector,
                role=_text(item.get("role"), limit=80),
                name=_text(
                    item.get("name") or item.get("text") or item.get("aria_label"),
                    limit=240,
                ),
                tag=_text(item.get("tag"), limit=80),
                input_type=_text(item.get("type") or item.get("input_type"), limit=80),
                value_state=state,  # type: ignore[arg-type]
                value_hash=value_hash,
                enabled=not _bool(item.get("disabled"), False),
                visible=not (item.get("visible") is False),
                selected=item.get("selected") if isinstance(item.get("selected"), bool) else None,
                checked=item.get("checked") if isinstance(item.get("checked"), bool) else None,
                frame_selector=_text(item.get("frame_selector"), limit=300),
                shadow_host_selector=_text(item.get("shadow_host_selector"), limit=300),
            )
        )
    return tuple(out)


def _tabs(payload: dict[str, Any], page_id: str) -> tuple[BrowserTabState, ...]:
    raw_tabs = payload.get("tabs") or []
    if not isinstance(raw_tabs, list):
        return ()
    active_id = _text(payload.get("active_tab_id") or page_id, limit=240)
    out: list[BrowserTabState] = []
    for raw_item in raw_tabs[:50]:
        if not isinstance(raw_item, dict):
            continue
        tab_id = _text(raw_item.get("id") or raw_item.get("page_id") or raw_item.get("tab_id"), limit=240)
        if not tab_id:
            continue
        out.append(
            BrowserTabState(
                id=tab_id,
                title=_text(raw_item.get("title"), limit=240),
                url=_text(raw_item.get("url"), limit=2048),
                active=tab_id == active_id,
                loading=_bool(raw_item.get("loading"), False),
            )
        )
    return tuple(out)


def _error_summaries(payload: dict[str, Any], key: str) -> tuple[str, ...]:
    raw = payload.get(key) or []
    if not isinstance(raw, list):
        return ()
    out: list[str] = []
    for item in raw[-20:]:
        if isinstance(item, dict):
            text = item.get("error") or item.get("message") or item.get("url") or ""
        else:
            text = item
        cleaned = _text(text, limit=500)
        if cleaned:
            out.append(cleaned)
    return tuple(out)


def _screenshot_identity(observation: BrowserObservation) -> tuple[str, str]:
    if not observation.screenshot_path:
        return "", ""
    path = Path(observation.screenshot_path)
    digest = ""
    try:
        if path.is_file() and not path.is_symlink():
            hasher = hashlib.sha256()
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    hasher.update(chunk)
            digest = f"sha256:{hasher.hexdigest()}"
    except OSError:
        digest = ""
    return f"replay:{observation.replay_event_id}:screenshot:{path.name}", digest


def normalize_browser_observation(
    observation: BrowserObservation,
    *,
    budget: BrowserAgentBudget,
) -> BrowserPageState:
    """Return a bounded, redacted and deterministic Page State."""

    payload = _payload_data(observation)
    url = _text(payload.get("url") or observation.url, limit=4096)
    title = _text(payload.get("title"), limit=500)
    text_limit = budget.max_observation_chars
    visible_text = _text(payload.get("text") or payload.get("visible_text") or payload.get("snapshot"), limit=text_limit)
    blocks: list[BrowserTextBlock] = []
    if visible_text:
        blocks.append(BrowserTextBlock(source="document", text=visible_text))
    links = payload.get("links") or []
    if isinstance(links, list):
        link_lines: list[str] = []
        for item in links[:80]:
            if not isinstance(item, dict):
                continue
            name = _text(item.get("text") or item.get("name"), limit=160)
            href = _text(item.get("href") or item.get("url"), limit=2048)
            if name or href:
                link_lines.append(f"{name} → {href}".strip())
        if link_lines:
            blocks.append(
                BrowserTextBlock(
                    source="element",
                    text="\n".join(link_lines)[: min(6000, text_limit)],
                )
            )
    elements = _interactive_elements(payload, budget=budget, runtime=observation.runtime)
    page_context = payload.get("page_context")
    if not isinstance(page_context, dict):
        page_context = {}
    security_texts = [title, visible_text, *(element.name for element in elements)]
    signals = detect_page_security_signals(security_texts)
    tabs = _tabs(payload, observation.page_id)
    screenshot_ref, screenshot_sha = _screenshot_identity(observation)
    downloads_raw = payload.get("downloads") or payload.get("matches") or []
    downloads: tuple[str, ...] = ()
    if isinstance(downloads_raw, list):
        download_items: list[str] = []
        for item in downloads_raw[:20]:
            if not item:
                continue
            raw_value = (
                item.get("filename") or item.get("url") or ""
                if isinstance(item, dict)
                else item
            )
            cleaned = _text(raw_value, limit=500)
            if cleaned:
                download_items.append(cleaned)
        downloads = tuple(download_items)
    raw_hash = _stable_hash(redact_untrusted_value(observation.model_dump(mode="json")))
    content_identity = {
        "url": url,
        "title": title,
        "text": visible_text,
        "elements": [
            {
                "ref": item.ref,
                "name": item.name,
                "enabled": item.enabled,
                "selected": item.selected,
                "checked": item.checked,
                "value_hash": item.value_hash,
            }
            for item in elements
        ],
        "tabs": [item.model_dump(mode="json") for item in tabs],
    }
    content_hash = _stable_hash(content_identity)
    progress_identity = {
        **content_identity,
        "text": _text(progress_text(str(payload.get("text") or payload.get("visible_text") or payload.get("snapshot") or ""), observation.runtime), limit=text_limit),
        "elements": [
            {key: value for key, value in item.model_dump(mode="json").items() if key != "ref"}
            for item in elements
        ],
        "downloads": downloads,
    }
    raw_controls = payload.get("controls") or payload.get("interactive_elements") or []
    controls_truncated = (
        isinstance(raw_controls, list)
        and len(raw_controls) > budget.max_interactive_elements
    )
    text_truncated = len(str(payload.get("text") or payload.get("visible_text") or payload.get("snapshot") or "")) > text_limit
    return BrowserPageState(
        page_revision="page:" + content_hash.removeprefix("sha256:")[:20],
        content_hash=content_hash,
        progress_hash=_stable_hash(progress_identity),
        runtime=observation.runtime,
        profile_id=observation.profile_id,
        page_id=observation.page_id,
        url=url,
        title=title,
        domain=(urlparse(url).hostname or "").lower(),
        loading=_bool(payload.get("loading"), False),
        tabs=tabs,
        text_blocks=tuple(blocks),
        interactive_elements=elements,
        screenshot_artifact_ref=screenshot_ref,
        screenshot_sha256=screenshot_sha,
        downloads=downloads,
        network_errors=_error_summaries(payload, "network"),
        console_errors=_error_summaries(payload, "console"),
        security_signals=signals,
        page_context_kind=_text(page_context.get("kind") or "normal", limit=80),
        requires_manual_takeover=_bool(
            page_context.get("requires_manual_takeover"), False
        ),
        truncated=text_truncated or controls_truncated or payload.get("truncated") is True,
        raw_observation_hash=raw_hash,
    )


def browser_page_state_diff(
    before: BrowserPageState,
    after: BrowserPageState,
) -> dict[str, object]:
    before_refs = {item.ref: item for item in before.interactive_elements}
    after_refs = {item.ref: item for item in after.interactive_elements}
    return {
        "page_changed": before.content_hash != after.content_hash,
        "url_changed": before.url != after.url,
        "title_changed": before.title != after.title,
        "added_refs": sorted(set(after_refs) - set(before_refs)),
        "removed_refs": sorted(set(before_refs) - set(after_refs)),
        "changed_refs": sorted(
            ref
            for ref in set(before_refs).intersection(after_refs)
            if before_refs[ref].model_dump(mode="json")
            != after_refs[ref].model_dump(mode="json")
        ),
        "tab_count_delta": len(after.tabs) - len(before.tabs),
        "download_count_delta": len(after.downloads) - len(before.downloads),
    }
