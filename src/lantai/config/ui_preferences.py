# -*- coding: utf-8 -*-
"""Read small UI preferences that also affect backend response behavior."""
from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

from ..constant import WORKING_DIR

QUQU_TURN_BUCKETS = 2
PERSONA_MODES = frozenset({"full", "balanced", "quiet", "off"})


def _display_settings(settings_file: Path | None = None) -> dict[str, Any]:
    path = settings_file or (WORKING_DIR / "settings.json")
    try:
        payload: Any = json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    appearance = payload.get("appearance")
    if not isinstance(appearance, dict):
        return {}
    display_settings = appearance.get("display_settings")
    return display_settings if isinstance(display_settings, dict) else {}


def ququ_turn_selected() -> bool:
    """Return whether this conversation turn is in the one-half ququ bucket."""
    return secrets.randbelow(QUQU_TURN_BUCKETS) == 0


def ququ_enabled(
    *,
    settings_file: Path | None = None,
    default: bool = True,
) -> bool:
    """Return the single global preference for generating and showing ququ."""
    value = _display_settings(settings_file).get("showInnerVoice")
    return value if isinstance(value, bool) else default


def set_ququ_enabled(
    enabled: bool,
    *,
    settings_file: Path | None = None,
) -> None:
    """Write the same global preference consumed by generation and display."""

    path = settings_file or (WORKING_DIR / "settings.json")
    try:
        payload: Any = json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    appearance = payload.get("appearance")
    if not isinstance(appearance, dict):
        appearance = {}
    display_settings = appearance.get("display_settings")
    if not isinstance(display_settings, dict):
        display_settings = {}
    display_settings["showInnerVoice"] = bool(enabled)
    appearance["display_settings"] = display_settings
    payload["appearance"] = appearance
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def persona_mode(
    *,
    settings_file: Path | None = None,
    default: str = "full",
) -> str:
    """Return the canonical user-controlled perceptible-persona mode."""

    fallback = default if default in PERSONA_MODES else "full"
    value = _display_settings(settings_file).get("personaMode")
    return str(value) if value in PERSONA_MODES else fallback
