"""Use-case orchestration for customization APIs and Agent tools."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .impact import preview_plan
from .migration import migrate_legacy_settings
from .models import ChangePlan, NaturalLanguageDraft, ProfileScope, utc_now
from .registry import CustomizationRegistry
from .store import CustomizationStore, CustomizationStoreError


class CustomizationService:
    def __init__(self, root: Path, *, settings_path: Path | None = None) -> None:
        self.registry = CustomizationRegistry.load_bundled()
        self.store = CustomizationStore(root)
        if settings_path is not None:
            migrate_legacy_settings(self.store, self.registry, settings_path)
        requested_mode = os.environ.get("LANTAI_CUSTOMIZATION_MODE", "").strip()
        boot_id = os.environ.get("LANTAI_CUSTOMIZATION_BOOT_ID", "").strip()
        dismissed_path = self.store.root / "recovery-override-dismissed.json"
        dismissed_boot_id = ""
        try:
            dismissed_boot_id = str(
                json.loads(dismissed_path.read_text("utf-8")).get("boot_id") or ""
            )
        except (OSError, json.JSONDecodeError, TypeError):
            pass
        if (
            requested_mode in {"factory", "safe"}
            and (not boot_id or dismissed_boot_id != boot_id)
        ):
            current = self.store.get_state()
            if current.mode != requested_mode:
                self.store.set_mode(requested_mode)
        self._recover_lkg_for_boot_once(boot_id)

    def _recover_lkg_for_boot_once(self, boot_id: str) -> None:
        if os.environ.get("LANTAI_CUSTOMIZATION_RECOVER_LKG") != "1" or not boot_id:
            return
        marker = self.store.root / "lkg-boot-recovery.json"
        try:
            if json.loads(marker.read_text("utf-8")).get("boot_id") == boot_id:
                return
        except (OSError, json.JSONDecodeError, TypeError):
            pass
        state = self.store.get_state()
        profile_id = state.active_profile_id
        result: dict[str, Any] = {"boot_id": boot_id, "profile_id": profile_id}
        if profile_id:
            try:
                transaction = self.store.restore_last_known_good(
                    profile_id,
                    self.registry,
                )
                result["transaction_id"] = transaction.id
                result["status"] = transaction.status
            except CustomizationStoreError as exc:
                result["status"] = "unavailable"
                result["error"] = str(exc)
        from .store import _atomic_write

        _atomic_write(marker, result)

    def dismiss_boot_recovery_override(self) -> None:
        boot_id = os.environ.get("LANTAI_CUSTOMIZATION_BOOT_ID", "").strip()
        if not boot_id:
            return
        from .store import _atomic_write

        _atomic_write(
            self.store.root / "recovery-override-dismissed.json",
            {"boot_id": boot_id, "dismissed_at": utc_now().isoformat()},
        )

    def health(self) -> dict[str, Any]:
        state = self.store.get_state()
        effective = self.store.get_effective_state(self.registry)
        return {
            **self._runtime_health(effective),
            "mode": state.mode,
            "active_profile_id": state.active_profile_id,
            "registry_schema_version": self.registry.schema_version,
            "warnings": effective.warnings,
            "ignored": effective.ignored,
        }

    @staticmethod
    def _runtime_health(effective: Any) -> dict[str, Any]:
        checks = ["schema", "resolver", "core_routes"]
        errors: list[str] = []
        for item_id in (
            "core.runtime_facts",
            "core.recovery_control",
            "core.memory_provenance",
            "navigation.sidebar.chat.visible",
            "navigation.sidebar.works.visible",
            "navigation.sidebar.settings.visible",
        ):
            if effective.values.get(item_id) is not True:
                errors.append(f"required_core_value:{item_id}")
        work_method = effective.values.get("work_methods.default", "standard")
        checks.append("work_method_reference")
        if not isinstance(work_method, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,100}", work_method
        ):
            errors.append("invalid_work_method_reference")
        model_policy = effective.values.get("models.routing.policy", {})
        checks.append("model_route_shape")
        if not isinstance(model_policy, dict) or len(model_policy) > 30:
            errors.append("invalid_model_route_shape")
        return {
            "ok": not errors and not any(
                reason == "invalid_value" for reason in effective.ignored.values()
            ),
            "checks": checks,
            "errors": errors,
            "warnings": effective.warnings,
        }

    def create_profile(self, body: dict[str, Any]):
        scope = ProfileScope.model_validate(body.get("scope") or {})
        overrides = body.get("overrides") or {}
        if not isinstance(overrides, dict):
            raise ValueError("overrides must be an object")
        profile = self.store.create_profile(
            name=str(body.get("name") or "我的澜台"),
            description=str(body.get("description") or ""),
            icon=str(body.get("icon") or "sparkles"),
            color=str(body.get("color") or "#4b6581"),
            scope=scope,
        )
        if overrides:
            from .models import ChangeOperation

            operations = [
                ChangeOperation(op="set", path=path, value=value)
                for path, value in overrides.items()
            ]
            self.store.apply_plan(
                ChangePlan(
                    target_profile_id=profile.id,
                    base_revision=0,
                    scope=scope,
                    intent="创建配置档",
                    operations=operations,
                ),
                self.registry,
                actor="user",
                source="profile_create",
            )
            profile = self.store.get_profile(profile.id)
        return profile

    def preview(self, plan: ChangePlan):
        preview = preview_plan(
            self.store.get_profile(plan.target_profile_id),
            plan,
            self.registry,
        )
        if not preview.changed:
            raise CustomizationStoreError("Change Plan contains no effective change")
        self.store.save_plan(plan, preview)
        return preview

    def apply(
        self,
        plan: ChangePlan,
        *,
        confirmed: bool,
        actor: str = "user",
        source: str = "api",
    ):
        return self.store.apply_plan(
            plan,
            self.registry,
            confirmed=confirmed,
            actor=actor,
            source=source,
            health_check=self._runtime_health,
        )

    def confirm(self, plan_id: str, *, actor: str = "user") -> dict[str, Any]:
        return self.store.confirm_plan(plan_id, actor=actor)

    def apply_saved(
        self,
        plan_id: str,
        *,
        actor: str = "user",
        source: str = "api",
        confirmation_token: str | None = None,
    ):
        return self.store.apply_saved_plan(
            plan_id,
            self.registry,
            actor=actor,
            source=source,
            confirmation_token=confirmation_token,
            health_check=self._runtime_health,
        )

    async def plan_from_intent(
        self,
        *,
        intent: str,
        target_profile_id: str,
        agent_id: str = "default",
    ) -> dict[str, Any]:
        from ..app.structured_utility import run_structured_utility_task

        profile = self.store.get_profile(target_profile_id)
        catalog = [
            {
                "id": item.id,
                "title": item.title,
                "description": item.description,
                "type": item.value_type,
                "enum": item.enum,
                "minimum": item.minimum,
                "maximum": item.maximum,
                "risk": item.risk,
                "scopes": item.scopes,
                "factory_value": item.factory_value,
                "current_value": self.store.get_effective_state(self.registry).values.get(
                    item.id
                ),
            }
            for item in self.registry.items
            if item.risk != "forbidden" and "global" in item.scopes
        ]
        system_prompt = """
你是澜台的个性化改造规划器。把用户的自然语言要求转换为结构化配置操作。
只能使用目录中明确提供的稳定 ID 和合法值，不得发明字段，不得修改源码，不得扩大权限，不得加入解释性伪操作。
用户没有要求的字段不要改。无法确定时保持最小改动；不要通过关闭安全、恢复、审计或运行事实来满足要求。
summary 用简洁中文说明准备做什么。operations 只使用 set 或 unset。
"""
        result = await run_structured_utility_task(
            agent_id=agent_id,
            system_prompt=system_prompt,
            user_prompt=(
                f"用户需求：\n{intent.strip()}\n\n"
                f"当前配置档：{profile.name}，revision {profile.revision}\n"
                "可用改造目录：\n"
                + json.dumps(catalog, ensure_ascii=False)
            ),
            purpose="customization_intent_planning",
            result_model=NaturalLanguageDraft,
            timeout_seconds=60,
        )
        if result is None:
            raise CustomizationStoreError("当前没有可用的改造规划模型")
        plan = ChangePlan(
            target_profile_id=profile.id,
            base_revision=profile.revision,
            scope=profile.scope,
            intent=intent.strip(),
            operations=result.value.operations,
        )
        preview = self.preview(plan)
        if not preview.changed:
            raise CustomizationStoreError("这项要求没有产生可应用的改造")
        return {
            "summary": result.value.summary,
            "plan": plan.model_dump(mode="json"),
            "preview": preview.model_dump(mode="json"),
            "model_label": result.model_label,
            "repaired": result.repaired,
        }
