# -*- coding: utf-8 -*-
"""Tool and approval presentation policy for runtime surfaces."""

from __future__ import annotations

from typing import Any

from ...constant import BUILTIN_TEAM_AGENT_CONFIGS
from .schemas import ChatExperienceEvent, ToolPresentation


def normalize_tool_key(value: str) -> str:
    """Normalize a tool/agent name into a stable lookup key."""

    return "".join(
        char.lower() if char.isalnum() or "\u4e00" <= char <= "\u9fff" else "_"
        for char in value
    ).strip("_")

HIDDEN_INTERNAL_TOOL_NAMES = {
    "extract_memory_candidates",
    "summary_memory",
}
SKILL_IMPLEMENTATION_TOOL_NAMES = {
    "read_skill",
}
CORE_SUB_AGENT_TOOL_NAMES = {
    "list_agents",
    "chat_with_agent",
    "collaborate_with_agents",
    "submit_to_agent",
    "check_agent_task",
}
BUILTIN_TEAM_AGENT_IDS = frozenset(
    str(config["id"]) for config in BUILTIN_TEAM_AGENT_CONFIGS
)
BUILTIN_TEAM_AGENT_TOOL_NAMES = frozenset(
    normalize_tool_key(alias)
    for config in BUILTIN_TEAM_AGENT_CONFIGS
    for alias in (
        str(config["id"]),
        str(config["name"]),
        str(config["skill_name"]),
    )
)


def _skill_is_private_agent_implementation(payload: dict[str, Any]) -> bool:
    """Return whether this Skill read belongs to a sub-Agent's private runtime.

    Explicit user Skill invocation always wins.  Skill installation, browsing,
    management, and a user's direct Skill call therefore stay visible even when
    the same implementation tool is used underneath.
    """

    context_payload = dict(payload)
    for key in ("details", "metadata", "sub_run"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            for nested_key, value in nested.items():
                context_payload.setdefault(str(nested_key), value)

    exposure = str(context_payload.get("skill_exposure") or "").strip().lower()
    origin = str(
        context_payload.get("skill_invocation_origin") or ""
    ).strip().lower()
    if exposure == "explicit" or origin == "user_explicit":
        return False

    context = str(
        context_payload.get("presentation_context") or ""
    ).strip().lower()
    if context == "subagent_internal":
        return True

    agent_id = str(context_payload.get("agent_id") or "").strip()
    return bool(agent_id and agent_id in BUILTIN_TEAM_AGENT_IDS)


def tool_risk_level(payload: dict[str, Any]) -> str:
    """Infer user-facing risk level from policy and guard payloads."""

    policy = payload.get("policy_decision")
    if isinstance(policy, dict):
        decision = str(policy.get("decision") or "").lower()
        if decision in {"forbidden", "deny", "denied"}:
            return "high"
        if decision in {"prompt", "approval", "ask"}:
            return "medium"
    guard = payload.get("guard")
    if isinstance(guard, dict):
        severity = str(guard.get("max_severity") or "").lower()
        if severity in {"critical", "high"}:
            return "high"
        if severity in {"medium", "warning"}:
            return "medium"
    return "low"


def tool_presentation_for_tool_name(
    tool_name: str,
    *,
    payload: dict[str, Any] | None = None,
    summary: str = "",
    is_approval: bool = False,
) -> ToolPresentation:
    """Return a stable renderer hint from a tool name alone."""

    payload = payload or {}
    normalized = normalize_tool_key(tool_name)
    display_name = tool_name.strip() or ("审批" if is_approval else "工具")

    if is_approval:
        return ToolPresentation(
            visibility="visible",
            surface="approval",
            risk_level=tool_risk_level(payload),  # type: ignore[arg-type]
            display_name=display_name,
            summary=summary or display_name,
        )

    if tool_name.startswith("memory_") or tool_name in HIDDEN_INTERNAL_TOOL_NAMES:
        return ToolPresentation(
            visibility="hidden",
            surface="debug",
            risk_level="low",
            display_name=display_name,
            summary=summary or display_name,
        )

    if (
        normalized in SKILL_IMPLEMENTATION_TOOL_NAMES
        and _skill_is_private_agent_implementation(payload)
    ):
        return ToolPresentation(
            visibility="hidden",
            surface="debug",
            risk_level="low",
            display_name="内部专业方法",
            summary="内部专业方法已就绪",
        )

    if normalized.startswith("wiki_"):
        return ToolPresentation(
            visibility="quiet",
            surface="service",
            risk_level="low",
            display_name=display_name,
            summary=summary or display_name,
        )

    if (
        tool_name in CORE_SUB_AGENT_TOOL_NAMES
        or normalized in BUILTIN_TEAM_AGENT_TOOL_NAMES
    ):
        return ToolPresentation(
            visibility="prominent",
            surface="subagent",
            risk_level=tool_risk_level(payload),  # type: ignore[arg-type]
            display_name=display_name,
            summary=summary or display_name,
        )

    return ToolPresentation(
        visibility="visible",
        surface="tool",
        risk_level=tool_risk_level(payload),  # type: ignore[arg-type]
        display_name=display_name,
        summary=summary or display_name,
    )


def tool_presentation_for_event(
    event: ChatExperienceEvent,
    payload: dict[str, Any],
) -> ToolPresentation | None:
    """Return the product-layer renderer hint for one runtime event."""

    kind = event.kind
    presentation_payload = dict(payload)
    presentation_payload.setdefault("agent_id", event.agent_id)
    if kind.startswith("chat.approval."):
        tool_name = str(payload.get("tool_name") or "approval").strip()
        return tool_presentation_for_tool_name(
            tool_name,
            payload=presentation_payload,
            summary=event.summary,
            is_approval=True,
        )

    if not kind.startswith("chat.tool"):
        return None

    tool_name = str(payload.get("tool_name") or "").strip()
    return tool_presentation_for_tool_name(
        tool_name,
        payload=presentation_payload,
        summary=event.summary,
    )
