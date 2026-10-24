"""Secret-free Profile template import and export."""
from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any

from .models import CustomizationProfile, ProfileScope
from .registry import CustomizationRegistry, RegistryError
from .store import CustomizationStore

_MACHINE_PATH = re.compile(r"(?:^|[\s\"'])(?:/Users/|/home/|[A-Za-z]:\\)")
_SECRET = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{12,}|\bBearer\s+[A-Za-z0-9._-]{12,}|"
    r"(?:api[_-]?key|access[_-]?token|secret)\s*[:=]\s*[^\s,]{8,})",
    re.IGNORECASE,
)


def _unsafe_portable_value(value: Any) -> str | None:
    text = json.dumps(value, ensure_ascii=False)
    if _SECRET.search(text):
        return "suspected_secret"
    if _MACHINE_PATH.search(text):
        return "machine_path"
    if re.search(r"https?://", text, re.IGNORECASE):
        return "external_url"
    return None


def export_template(
    profile: CustomizationProfile,
    registry: CustomizationRegistry,
) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    requirements: list[dict[str, Any]] = []
    omitted: list[str] = []
    for item_id, value in profile.overrides.items():
        try:
            item = registry.get(item_id)
        except RegistryError:
            omitted.append(item_id)
            continue
        if item.export_policy == "private":
            omitted.append(item_id)
            continue
        if item.export_policy in {"local_reference", "redact_machine_paths"}:
            requirements.append(
                {
                    "item_id": item_id,
                    "title": item.title,
                    "policy": item.export_policy,
                }
            )
            continue
        unsafe_reason = _unsafe_portable_value(value)
        if unsafe_reason:
            omitted.append(item_id)
            continue
        overrides[item_id] = deepcopy(value)
    return {
        "format": "lantai-profile",
        "format_version": 1,
        "registry_schema_version": registry.schema_version,
        "profile": {
            "name": profile.name,
            "description": profile.description,
            "icon": profile.icon,
            "color": profile.color,
            "overrides": overrides,
        },
        "requirements": requirements,
        "omitted": omitted,
    }


def import_template(
    payload: dict[str, Any],
    *,
    store: CustomizationStore,
    registry: CustomizationRegistry,
) -> tuple[CustomizationProfile, dict[str, Any]]:
    if len(json.dumps(payload, ensure_ascii=False)) > 1_000_000:
        raise ValueError("Customization template exceeds the 1 MB limit")
    if payload.get("format") != "lantai-profile":
        raise ValueError("Unsupported customization template format")
    if int(payload.get("format_version", 0)) != 1:
        raise ValueError("Unsupported customization template version")
    raw_profile = payload.get("profile")
    if not isinstance(raw_profile, dict):
        raise ValueError("Template profile must be an object")
    raw_overrides = raw_profile.get("overrides") or {}
    if not isinstance(raw_overrides, dict):
        raise ValueError("Template overrides must be an object")
    if len(raw_overrides) > 200:
        raise ValueError("Template contains too many overrides")
    accepted: dict[str, Any] = {}
    ignored: dict[str, str] = {}
    for item_id, value in raw_overrides.items():
        try:
            item = registry.get(str(item_id))
        except RegistryError:
            ignored[str(item_id)] = "unknown_registry_item"
            continue
        if item.merge_strategy == "immutable" or item.risk == "forbidden":
            ignored[item.id] = "immutable"
            continue
        if item.export_policy != "shareable":
            ignored[item.id] = "local_or_private"
            continue
        unsafe_reason = _unsafe_portable_value(value)
        if unsafe_reason:
            ignored[item.id] = unsafe_reason
            continue
        try:
            accepted[item.id] = registry.validate_value(item, value)
        except RegistryError:
            ignored[item.id] = "invalid_value"
    profile = store.create_profile(
        name=str(raw_profile.get("name") or "导入的澜台")[:80],
        description=str(raw_profile.get("description") or "")[:500],
        icon=str(raw_profile.get("icon") or "import")[:40],
        color=str(raw_profile.get("color") or "#4b6581")[:40],
        scope=ProfileScope(kind="global"),
        overrides=accepted,
    )
    return profile, {
        "accepted": sorted(accepted),
        "ignored": ignored,
        "requirements": payload.get("requirements") or [],
        "activated": False,
    }
