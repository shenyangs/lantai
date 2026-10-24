# -*- coding: utf-8 -*-
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ..capabilities.policy import is_app_store_distribution
from .models import (
    ComputerAction,
    ComputerObservation,
    ComputerPolicyDecision,
)


_READ_ACTIONS = {"screenshot_screen", "list_windows"}
_WRITE_ACTIONS = {"focus_window", "click_at", "type_text", "press_key", "drag"}
_PERMISSION_BY_ACTION = {
    "screenshot_screen": "screen_recording",
    "list_windows": "accessibility",
    "focus_window": "accessibility",
    "click_at": "accessibility",
    "type_text": "accessibility",
    "press_key": "accessibility",
    "drag": "accessibility",
}


class ComputerActionBroker:
    """Policy boundary for future system-level computer automation."""

    def __init__(
        self,
        *,
        app_store_distribution: Callable[[], bool] = is_app_store_distribution,
        permission_status: Callable[[], dict[str, bool]] | None = None,
        native_executor: Callable[[ComputerAction], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self.app_store_distribution = app_store_distribution
        self.permission_status = permission_status or (lambda: {})
        self.native_executor = native_executor

    def evaluate(self, action: ComputerAction) -> ComputerPolicyDecision:
        if self.app_store_distribution():
            return ComputerPolicyDecision(
                decision="block",
                reason="App Store distribution disables system-level computer automation.",
                risk="critical",
                matched_rule_id="app_store_computer_automation_blocked",
            )
        permission = _PERMISSION_BY_ACTION.get(action.action)
        permissions = self.permission_status()
        if permission and permissions and not permissions.get(permission, False):
            return ComputerPolicyDecision(
                decision="block",
                reason=f"Computer action requires macOS permission: {permission}.",
                risk="high",
                matched_rule_id="computer_permission_missing",
            )
        if action.action in _WRITE_ACTIONS and not self._has_target(action):
            return ComputerPolicyDecision(
                decision="block",
                reason="Computer write actions require an app allowlist or target window.",
                risk="high",
                matched_rule_id="computer_target_required",
            )
        if action.action in _WRITE_ACTIONS and not action.confirmed:
            return ComputerPolicyDecision(
                decision="prompt",
                reason="Computer write actions require explicit user confirmation.",
                risk="high",
                requires_confirmation=True,
                matched_rule_id="computer_write_confirmation_required",
            )
        return ComputerPolicyDecision(
            decision="allow",
            reason="Computer action allowed.",
            risk="low" if action.action in _READ_ACTIONS else "medium",
        )

    async def execute(self, action: ComputerAction) -> ComputerObservation:
        decision = self.evaluate(action)
        if decision.decision != "allow":
            return ComputerObservation(
                ok=False,
                action=action.action,
                run_id=action.run_id,
                error=decision.reason,
                data={
                    "error_code": decision.matched_rule_id
                    or "computer_action_blocked",
                    "text_length": len(action.text or ""),
                },
                decision=decision,
            )
        if self.native_executor is None:
            return ComputerObservation(
                ok=False,
                action=action.action,
                run_id=action.run_id,
                error="Computer action was allowed but no native adapter is available.",
                data={
                    **self._safe_result(action),
                    "error_code": "computer_native_adapter_unavailable",
                    "policy_status": "allowed",
                    "dispatch_status": "not_sent",
                    "effect_status": "failed",
                },
                decision=decision,
            )
        result = await self.native_executor(action)
        return ComputerObservation(
            ok=bool(result.get("ok")),
            action=action.action,
            run_id=action.run_id,
            message=str(result.get("message") or ""),
            error=str(result.get("error") or "") or None,
            data=result,
            decision=decision,
        )

    @staticmethod
    def _has_target(action: ComputerAction) -> bool:
        return bool(
            action.app_allowlist
            or action.app_bundle_id
            or action.window_id
            or action.window_title
        )

    @staticmethod
    def _safe_result(action: ComputerAction) -> dict[str, Any]:
        if action.action == "list_windows":
            return {
                "windows": [],
                "native_adapter": "not_implemented",
                "message": "Window enumeration policy boundary is ready; native adapter is pending.",
            }
        if action.action == "screenshot_screen":
            return {
                "screenshot_path": "",
                "native_adapter": "not_implemented",
                "message": "Screen capture policy boundary is ready; native adapter is pending.",
            }
        return {
            "accepted": True,
            "native_adapter": "not_implemented",
            "target": {
                "app_bundle_id": action.app_bundle_id,
                "window_id": action.window_id,
                "window_title": action.window_title,
                "app_allowlist": action.app_allowlist,
            },
            "text_length": len(action.text or ""),
        }
