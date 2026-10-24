"""Change-plan validation, preview, and risk classification."""
from __future__ import annotations

from copy import deepcopy

from .models import ChangePlan, ChangePreview, CustomizationProfile, RiskLevel
from .registry import CustomizationRegistry, RegistryError

_RISK_ORDER: dict[RiskLevel, int] = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "forbidden": 3,
}


def preview_plan(
    profile: CustomizationProfile,
    plan: ChangePlan,
    registry: CustomizationRegistry,
) -> ChangePreview:
    if plan.target_profile_id != profile.id:
        raise RegistryError("Change Plan targets a different Profile")
    before = deepcopy(profile.overrides)
    after = deepcopy(before)
    changed: dict[str, dict[str, object]] = {}
    ignored: dict[str, str] = {}
    highest: RiskLevel = "low"
    seen: set[str] = set()
    for operation in plan.operations:
        if operation.path in seen:
            raise RegistryError(
                f"Change Plan contains duplicate path: {operation.path}"
            )
        seen.add(operation.path)
        item = registry.get(operation.path)
        if item.merge_strategy == "immutable" or item.risk == "forbidden":
            ignored[operation.path] = "immutable"
            continue
        if plan.scope.kind not in item.scopes:
            raise RegistryError(
                f"Scope {plan.scope.kind!r} is not allowed for {operation.path}"
            )
        old_value = before.get(operation.path, item.factory_value)
        if operation.op == "unset":
            after.pop(operation.path, None)
            new_value = item.factory_value
        else:
            new_value = registry.validate_value(item, operation.value)
            after[operation.path] = new_value
        if old_value != new_value:
            changed[operation.path] = {"before": old_value, "after": new_value}
        if _RISK_ORDER[item.risk] > _RISK_ORDER[highest]:
            highest = item.risk
    return ChangePreview(
        plan=plan,
        before=before,
        after=after,
        changed=changed,
        ignored=ignored,
        risk=highest,
        requires_confirmation=highest in {"medium", "high"},
    )
