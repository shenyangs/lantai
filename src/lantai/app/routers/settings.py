# -*- coding: utf-8 -*-
"""Global UI settings (language, theme, etc.).

Persisted in ``WORKING_DIR/settings.json``, independent of
per-agent configuration.  All endpoints are public (no auth required).
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Body, HTTPException

from ...agents.skills_manager import set_builtin_skill_language_preference
from ...constant import WORKING_DIR
from ...storage_retention import (
    clean_storage_retention,
    get_storage_retention_policy,
    update_storage_retention_policy,
)

router = APIRouter(prefix="/settings", tags=["settings"])

_SETTINGS_FILE = WORKING_DIR / "settings.json"

_VALID_LANGUAGES = {"en", "zh", "ja", "ru", "pt-BR"}
_DEFAULT_LANGUAGE = "zh"
_VALID_THEME_MODES = {"light", "dark", "system"}
_VALID_DENSITY_MODES = {"compact", "comfortable", "spacious"}
_VALID_SIDEBAR_MATERIALS = {
    "solid",
    "light-glass",
    "mist-glass",
    "deep-glass",
}
_VALID_PALETTES = {
    "lantai",
    "mist",
    "pine",
    "blue",
    "warm",
    "violet",
    "ink",
    "custom",
}
_DEFAULT_DISPLAY_SETTINGS = {
    "showInnerVoice": True,
    "personaMode": "full",
    "showThinking": True,
    "showToolCalls": True,
    "showTokenUsage": True,
    "dynamicWelcome": True,
    "aiWelcome": True,
    "welcomeCulturalCalendar": "none",
    "welcomeTone": "natural",
    "composerSize": "comfortable",
    "sidebarMaterial": "solid",
}
_DEFAULT_APPEARANCE_SETTINGS = {
    "theme_mode": "system",
    "density_mode": "comfortable",
    "appearance_palette": "mist",
    "display_settings": _DEFAULT_DISPLAY_SETTINGS,
    "custom_theme_css": "",
}
_VALID_TURN_COMPLETION_NOTIFICATION_MODES = {
    "never",
    "when_unfocused",
    "always",
}
_DEFAULT_SYSTEM_BEHAVIOR_SETTINGS = {
    "launch_at_login": False,
    "keep_awake_while_running": False,
    "turn_completion_notification": "when_unfocused",
    "permission_notification": True,
}


def _load() -> dict:
    if _SETTINGS_FILE.is_file():
        try:
            return json.loads(_SETTINGS_FILE.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save(data: dict) -> None:
    _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        "utf-8",
    )


def _normalize_display_settings(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return dict(_DEFAULT_DISPLAY_SETTINGS)

    normalized = dict(_DEFAULT_DISPLAY_SETTINGS)
    for key in (
        "showInnerVoice",
        "showThinking",
        "showToolCalls",
        "showTokenUsage",
        "dynamicWelcome",
        "aiWelcome",
    ):
        if key in value:
            normalized[key] = bool(value[key])

    persona_mode = value.get("personaMode")
    if persona_mode in {"full", "balanced", "quiet", "off"}:
        normalized["personaMode"] = persona_mode

    cultural_calendar = value.get("welcomeCulturalCalendar")
    if cultural_calendar in {"none", "china", "global", "all"}:
        normalized["welcomeCulturalCalendar"] = cultural_calendar

    welcome_tone = value.get("welcomeTone")
    if welcome_tone in {"natural", "concise", "restrained"}:
        normalized["welcomeTone"] = welcome_tone

    composer_size = value.get("composerSize")
    if composer_size in _VALID_DENSITY_MODES:
        normalized["composerSize"] = composer_size

    sidebar_material = value.get("sidebarMaterial")
    if sidebar_material in _VALID_SIDEBAR_MATERIALS:
        normalized["sidebarMaterial"] = sidebar_material
    return normalized


def _normalize_appearance_settings(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        value = {}

    normalized = dict(_DEFAULT_APPEARANCE_SETTINGS)
    display_settings = _normalize_display_settings(value.get("display_settings"))
    normalized["display_settings"] = display_settings

    theme_mode = value.get("theme_mode")
    if theme_mode in _VALID_THEME_MODES:
        normalized["theme_mode"] = theme_mode

    density_mode = value.get("density_mode")
    if density_mode in _VALID_DENSITY_MODES:
        normalized["density_mode"] = density_mode
    else:
        normalized["density_mode"] = display_settings["composerSize"]

    appearance_palette = value.get("appearance_palette")
    if appearance_palette in _VALID_PALETTES:
        normalized["appearance_palette"] = appearance_palette

    custom_theme_css = value.get("custom_theme_css")
    if isinstance(custom_theme_css, str):
        normalized["custom_theme_css"] = custom_theme_css

    return normalized


def _normalize_system_behavior_settings(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        value = {}

    normalized = dict(_DEFAULT_SYSTEM_BEHAVIOR_SETTINGS)
    for key in (
        "launch_at_login",
        "keep_awake_while_running",
        "permission_notification",
    ):
        if isinstance(value.get(key), bool):
            normalized[key] = value[key]

    notification_mode = value.get("turn_completion_notification")
    if notification_mode in _VALID_TURN_COMPLETION_NOTIFICATION_MODES:
        normalized["turn_completion_notification"] = notification_mode
    return normalized


@router.get("/storage-retention", summary="Get local storage retention policy")
async def get_storage_retention() -> dict[str, int]:
    return get_storage_retention_policy()


@router.put("/storage-retention", summary="Update local storage retention policy")
async def put_storage_retention(body: dict = Body(...)) -> dict[str, int]:
    allowed = {
        "logs_cache_days",
        "logs_cache_max_bytes",
        "runtime_days",
        "runtime_max_bytes",
        "generated_images_days",
    }
    patch = {key: value for key, value in body.items() if key in allowed}
    if not patch:
        raise HTTPException(status_code=400, detail="No storage retention settings supplied")
    if any(
        not isinstance(value, int) or isinstance(value, bool)
        for value in patch.values()
    ):
        raise HTTPException(
            status_code=400,
            detail="Storage retention settings must be integers",
        )
    if (
        "generated_images_days" in patch
        and patch["generated_images_days"] not in {0, 3, 7, 15, 30}
    ):
        raise HTTPException(
            status_code=400,
            detail="Generated image retention must be 0, 3, 7, 15, or 30 days",
        )
    return update_storage_retention_policy(patch)


@router.post("/storage-retention/preview", summary="Preview local data cleanup")
async def preview_storage_retention() -> dict[str, Any]:
    return clean_storage_retention(dry_run=True)


@router.post("/storage-retention/cleanup", summary="Clean disposable local data")
async def cleanup_storage_retention() -> dict[str, Any]:
    return clean_storage_retention(dry_run=False)


@router.get("/language", summary="Get UI language")
async def get_language() -> dict:
    return {"language": _load().get("language", _DEFAULT_LANGUAGE)}


@router.put("/language", summary="Update UI language")
async def put_language(
    body: dict = Body(..., description='e.g. {"language": "zh"}'),
) -> dict:
    language = body.get("language", "").strip()
    if language not in _VALID_LANGUAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid language, must be one of "
            f"{sorted(_VALID_LANGUAGES)}",
        )
    data = _load()
    data["language"] = language
    _save(data)
    # Update cached builtin preference since it falls back to UI language.
    if not data.get("builtin_skill_language"):
        set_builtin_skill_language_preference(
            "zh" if language.startswith("zh") else "en",
        )
    return {"language": language}


@router.get("/appearance", summary="Get UI appearance settings")
async def get_appearance() -> dict[str, Any]:
    data = _load()
    return _normalize_appearance_settings(data.get("appearance"))


@router.put("/appearance", summary="Update UI appearance settings")
async def put_appearance(
    body: dict = Body(..., description="UI appearance preference patch"),
) -> dict[str, Any]:
    data = _load()
    current = _normalize_appearance_settings(data.get("appearance"))
    patch: dict[str, Any] = {}

    if "theme_mode" in body:
        theme_mode = body.get("theme_mode")
        if theme_mode not in _VALID_THEME_MODES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid theme_mode, must be one of "
                f"{sorted(_VALID_THEME_MODES)}",
            )
        patch["theme_mode"] = theme_mode

    if "density_mode" in body:
        density_mode = body.get("density_mode")
        if density_mode not in _VALID_DENSITY_MODES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid density_mode, must be one of "
                f"{sorted(_VALID_DENSITY_MODES)}",
            )
        patch["density_mode"] = density_mode

    if "appearance_palette" in body:
        appearance_palette = body.get("appearance_palette")
        if appearance_palette not in _VALID_PALETTES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid appearance_palette, must be one of "
                f"{sorted(_VALID_PALETTES)}",
            )
        patch["appearance_palette"] = appearance_palette

    if "display_settings" in body:
        if not isinstance(body.get("display_settings"), dict):
            raise HTTPException(
                status_code=400,
                detail="display_settings must be an object",
            )
        patch["display_settings"] = _normalize_display_settings(
            {
                **current.get("display_settings", {}),
                **body["display_settings"],
            },
        )

    if "custom_theme_css" in body:
        custom_theme_css = body.get("custom_theme_css")
        if not isinstance(custom_theme_css, str):
            raise HTTPException(
                status_code=400,
                detail="custom_theme_css must be a string",
            )
        patch["custom_theme_css"] = custom_theme_css

    updated = _normalize_appearance_settings({**current, **patch})
    data["appearance"] = updated
    _save(data)
    return updated


@router.get("/system-behavior", summary="Get desktop system behavior settings")
async def get_system_behavior() -> dict[str, Any]:
    data = _load()
    return _normalize_system_behavior_settings(data.get("system_behavior"))


@router.put("/system-behavior", summary="Update desktop system behavior settings")
async def put_system_behavior(
    body: dict = Body(..., description="Desktop system behavior preference patch"),
) -> dict[str, Any]:
    data = _load()
    current = _normalize_system_behavior_settings(data.get("system_behavior"))
    patch: dict[str, Any] = {}

    for key in (
        "launch_at_login",
        "keep_awake_while_running",
        "permission_notification",
    ):
        if key not in body:
            continue
        if not isinstance(body.get(key), bool):
            raise HTTPException(
                status_code=400,
                detail=f"{key} must be a boolean",
            )
        patch[key] = body[key]

    if "turn_completion_notification" in body:
        notification_mode = body.get("turn_completion_notification")
        if notification_mode not in _VALID_TURN_COMPLETION_NOTIFICATION_MODES:
            raise HTTPException(
                status_code=400,
                detail="Invalid turn_completion_notification, must be one of "
                f"{sorted(_VALID_TURN_COMPLETION_NOTIFICATION_MODES)}",
            )
        patch["turn_completion_notification"] = notification_mode

    updated = _normalize_system_behavior_settings({**current, **patch})
    data["system_behavior"] = updated
    _save(data)
    return updated
