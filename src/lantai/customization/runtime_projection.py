"""Fail-safe projections from the customization control plane to runtime."""
from __future__ import annotations

from typing import Any

from ..constant import WORKING_DIR
from .service import CustomizationService


def effective_customization_value(item_id: str, fallback: Any) -> Any:
    try:
        service = CustomizationService(
            WORKING_DIR / "customization",
            settings_path=WORKING_DIR / "settings.json",
        )
        effective = service.store.get_effective_state(service.registry)
        return effective.values.get(item_id, fallback)
    except Exception:
        return fallback


def active_profile_value(item_id: str, fallback: Any) -> Any:
    """Return a value only when an active Profile, not Factory, supplied it."""

    try:
        service = CustomizationService(
            WORKING_DIR / "customization",
            settings_path=WORKING_DIR / "settings.json",
        )
        effective = service.store.get_effective_state(service.registry)
        source = effective.sources.get(item_id)
        if source is None or source.layer != "profile":
            return fallback
        return effective.values.get(item_id, fallback)
    except Exception:
        return fallback


def memory_capture_enabled() -> bool:
    return bool(active_profile_value("memory.capture.enabled", True))


def clamp_memory_recall_limit(requested: int) -> int:
    configured = active_profile_value(
        "memory.recall.max_results",
        requested,
    )
    try:
        return max(0, min(int(requested), int(configured)))
    except (TypeError, ValueError):
        return max(0, int(requested))


def agent_behavior_prompt() -> str:
    presence = str(effective_customization_value("persona.presence", "full"))
    address = str(effective_customization_value("persona.user_address", "")).strip()
    proactivity = effective_customization_value("persona.proactivity", 0.5)
    delivery = str(effective_customization_value("delivery.default_format", "auto"))
    lines = [
        "## 用户选择的澜台行为配置",
        f"- 人格存在感：{presence}",
        f"- 主动程度：{proactivity}",
        f"- 默认交付格式：{delivery}",
    ]
    if address:
        lines.append(f"- 用户希望被称为：{address}")
    lines.append("这些偏好不能覆盖事实、权限、安全、审计或用户当前明确指令。")
    return "\n".join(lines)
