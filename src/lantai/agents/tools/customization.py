# -*- coding: utf-8 -*-
"""Safe proposal-only tools for customizing Lantai from the main chat."""
from __future__ import annotations

import json
from typing import Any

from ...constant import WORKING_DIR
from ...customization.models import ChangeOperation, ChangePlan
from ...customization.service import CustomizationService
from ...runtime.tool_response import TextBlock, ToolResponse


def _service() -> CustomizationService:
    return CustomizationService(
        WORKING_DIR / "customization",
        settings_path=WORKING_DIR / "settings.json",
    )


def _response(payload: dict[str, Any]) -> ToolResponse:
    return ToolResponse(
        content=[
            TextBlock(
                type="text",
                text=json.dumps(payload, ensure_ascii=False),
            )
        ]
    )


async def inspect_lantai_customization(query: str = "") -> ToolResponse:
    """Inspect Lantai's supported customization catalogue and current state.

    Call this before preparing a customization when you need stable field IDs,
    allowed values, risk, factory defaults, or the active Profile. It is
    read-only. Search is matched against IDs, titles, descriptions, synonyms,
    and examples. An empty query returns the complete public handbook.

    Args:
        query: Optional phrase such as "左侧栏", "颜色", "模型", or "memory".
    """
    service = _service()
    needle = (query or "").strip().casefold()
    effective = service.store.get_effective_state(service.registry)
    items: list[dict[str, Any]] = []
    for item in service.registry.items:
        if item.risk == "forbidden" or item.export_policy == "private":
            continue
        searchable = " ".join(
            [
                item.id,
                item.title,
                item.description,
                *item.model.synonyms,
                *item.model.examples,
            ]
        ).casefold()
        if needle and needle not in searchable:
            continue
        items.append(
            {
                "id": item.id,
                "category": item.category,
                "title": item.title,
                "description": item.description,
                "type": item.value_type,
                "enum": item.enum,
                "minimum": item.minimum,
                "maximum": item.maximum,
                "risk": item.risk,
                "scopes": item.scopes,
                "factory_value": item.factory_value,
                "current_value": effective.values.get(item.id),
            }
        )
    state = service.store.get_state()
    return _response(
        {
            "type": "lantai_customization_catalog",
            "schema_version": service.registry.schema_version,
            "mode": state.mode,
            "active_profile_id": state.active_profile_id,
            "items": items,
            "rule": "Only IDs and values in this catalogue may be proposed.",
        }
    )


async def prepare_lantai_customization(
    intent: str,
    operations: list[dict[str, Any]],
    target_profile_id: str = "",
) -> ToolResponse:
    """Prepare a validated Lantai customization draft for user confirmation.

    Use this after `inspect_lantai_customization` when the user asks to change
    Lantai itself: its appearance, navigation, conversation density, persona,
    model routing, agent defaults, work methods, memory behavior, permissions,
    automation defaults, or delivery preferences. Use only catalogue IDs and
    legal values. Keep changes minimal and never invent a field.

    This tool NEVER applies or activates customization. It persists a versioned
    Change Plan and returns a special preview card. Only an explicit click by
    the user can obtain the server confirmation receipt and commit the plan.

    Args:
        intent: Short plain-language summary of the user's requested change.
        operations: List of {"op":"set"|"unset","path":stable_id,"value":...}.
        target_profile_id: Optional Profile ID; defaults to the active Profile.
    """
    service = _service()
    state = service.store.get_state()
    profile_id = (target_profile_id or state.active_profile_id or "").strip()
    created_profile = False
    if profile_id:
        profile = service.store.get_profile(profile_id)
    else:
        profile = service.store.create_profile(
            name="主对话改造",
            description="由主对话生成、等待用户确认的澜台配置档",
            icon="message-circle",
            color="#4b6581",
        )
        created_profile = True
        first_empty = next(
            (slot for slot in service.store.list_slots() if slot.profile_id is None),
            None,
        )
        if first_empty is not None:
            service.store.set_slot(
                first_empty.index,
                profile_id=profile.id,
                label=profile.name,
                icon=profile.icon,
                color=profile.color,
            )
    parsed_operations = [ChangeOperation.model_validate(value) for value in operations]
    plan = ChangePlan(
        target_profile_id=profile.id,
        base_revision=profile.revision,
        scope=profile.scope,
        intent=(intent or "从主对话改造澜台").strip(),
        operations=parsed_operations,
    )
    preview = service.preview(plan)
    if not preview.changed:
        raise ValueError("The request produced no applicable customization change")
    return _response(
        {
            "type": "lantai_customization_draft",
            "summary": plan.intent,
            "profile": {
                "id": profile.id,
                "name": profile.name,
                "created_for_draft": created_profile,
            },
            "plan": plan.model_dump(mode="json"),
            "preview": preview.model_dump(mode="json"),
            "notice": "No customization has been applied. User confirmation is required.",
        }
    )
