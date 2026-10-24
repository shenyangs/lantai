"""One-way, non-destructive migration from legacy UI preferences."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .registry import CustomizationRegistry
from .store import CustomizationStore, _atomic_write

_SAFE_TOKEN = re.compile(
    r"(--lt-[a-z0-9-]+)\s*:\s*([^;{}]+)\s*;",
    flags=re.IGNORECASE,
)

_DISPLAY_MAPPING = {
    "showInnerVoice": "conversation.show_inner_voice",
    "showThinking": "conversation.show_thinking",
    "showToolCalls": "conversation.show_tool_calls",
    "showTokenUsage": "conversation.show_token_usage",
    "composerSize": "appearance.composer_size",
    "sidebarMaterial": "appearance.sidebar_material",
    "personaMode": "persona.presence",
}


def extract_safe_design_tokens(css: str) -> dict[str, str]:
    """Extract inert Lantai variables; never preserve selectors or imports."""

    tokens: dict[str, str] = {}
    for name, raw_value in _SAFE_TOKEN.findall(css or ""):
        value = raw_value.strip()
        lowered = value.lower()
        if not value or "url(" in lowered or "@import" in lowered:
            continue
        if len(value) > 256:
            continue
        tokens[name.lower()] = value
    return tokens


def _load_settings(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def migrate_legacy_settings(
    store: CustomizationStore,
    registry: CustomizationRegistry,
    settings_path: Path,
) -> str | None:
    """Migrate once, retaining legacy source and unsupported CSS for recovery."""

    marker = store.root / "legacy-migration.json"
    if marker.exists():
        payload = store._read_json(marker, {})
        return payload.get("profile_id") if isinstance(payload, dict) else None

    settings = _load_settings(settings_path)
    appearance = settings.get("appearance")
    overrides: dict[str, Any] = {}
    legacy_css = ""
    if isinstance(appearance, dict):
        direct = {
            "theme_mode": "appearance.theme_mode",
            "appearance_palette": "appearance.palette",
            "density_mode": "appearance.composer_size",
        }
        for old_key, new_key in direct.items():
            value = appearance.get(old_key)
            if value is None:
                continue
            try:
                item = registry.get(new_key)
                validated = registry.validate_value(item, value)
            except ValueError:
                continue
            if validated != item.factory_value:
                overrides[new_key] = validated
        display = appearance.get("display_settings")
        if isinstance(display, dict):
            for old_key, new_key in _DISPLAY_MAPPING.items():
                if old_key not in display:
                    continue
                try:
                    item = registry.get(new_key)
                    validated = registry.validate_value(item, display[old_key])
                except ValueError:
                    continue
                if validated != item.factory_value:
                    overrides[new_key] = validated
        raw_css = appearance.get("custom_theme_css")
        if isinstance(raw_css, str) and raw_css.strip():
            legacy_css = raw_css
            tokens = extract_safe_design_tokens(raw_css)
            if tokens:
                overrides["appearance.design_tokens"] = tokens

    profile_id: str | None = None
    if overrides:
        profile = store.create_profile(
            name="迁移自旧版",
            description="由升级前的外观与显示设置自动转换，可随时回到原生澜台。",
            icon="history",
            overrides=overrides,
        )
        profile_id = profile.id
        store.set_slot(1, profile_id=profile.id, label=profile.name, icon=profile.icon)
        store.activate_profile(profile.id)

    archive_path: str | None = None
    if legacy_css:
        archive = store.root / "imports" / "quarantined" / "legacy_custom_css.css.txt"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_text(legacy_css, encoding="utf-8")
        archive_path = str(archive)
    _atomic_write(
        marker,
        {
            "completed": True,
            "profile_id": profile_id,
            "settings_source": str(settings_path),
            "legacy_css_archive": archive_path,
        },
    )
    return profile_id
