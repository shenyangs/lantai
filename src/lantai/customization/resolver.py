"""Pure resolution rules for Factory and scoped Profile overlays."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable

from .models import (
    CustomizationProfile,
    EffectiveState,
    EffectiveValueSource,
    RegistryItem,
)
from .registry import CustomizationRegistry, RegistryError

_SCOPE_ORDER = {"global": 0, "project": 1, "agent": 2, "task": 3, "session": 4}


def _unique(values: Iterable[Any]) -> list[Any]:
    result: list[Any] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _merge(item: RegistryItem, current: Any, incoming: Any) -> Any:
    strategy = item.merge_strategy
    if strategy == "override":
        return deepcopy(incoming)
    if strategy == "union":
        return _unique([*(current or []), *(incoming or [])])
    if strategy == "intersection":
        if "*" in list(current or []):
            return _unique(list(incoming or []))
        if "*" in list(incoming or []):
            return _unique(list(current or []))
        allowed = list(incoming or [])
        return [value for value in list(current or []) if value in allowed]
    if strategy == "min":
        return min(current, incoming)
    if strategy == "max":
        return max(current, incoming)
    if strategy == "append_unique":
        return _unique([*(current or []), *(incoming or [])])
    return deepcopy(current)


def resolve_effective_state(
    registry: CustomizationRegistry,
    profiles: Iterable[CustomizationProfile],
) -> EffectiveState:
    values = registry.factory_values()
    sources = {
        item_id: EffectiveValueSource(layer="factory") for item_id in values
    }
    ignored: dict[str, str] = {}
    warnings: list[str] = []
    ordered = sorted(
        profiles,
        key=lambda profile: (_SCOPE_ORDER[profile.scope.kind], profile.updated_at),
    )
    for profile in ordered:
        for item_id, raw_value in profile.overrides.items():
            try:
                item = registry.get(item_id)
            except RegistryError:
                ignored[item_id] = "unknown_registry_item"
                continue
            if item.merge_strategy == "immutable":
                ignored[item_id] = "immutable"
                continue
            if profile.scope.kind not in item.scopes:
                ignored[item_id] = f"scope_not_allowed:{profile.scope.kind}"
                continue
            try:
                incoming = registry.validate_value(item, raw_value)
            except RegistryError as exc:
                ignored[item_id] = "invalid_value"
                warnings.append(str(exc))
                continue
            values[item_id] = _merge(item, values[item_id], incoming)
            sources[item_id] = EffectiveValueSource(
                layer="profile",
                profile_id=profile.id,
                revision=profile.revision,
            )
    return EffectiveState(
        values=values,
        sources=sources,
        ignored=ignored,
        warnings=warnings,
    )
