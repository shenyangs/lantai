# -*- coding: utf-8 -*-
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from secrets import token_urlsafe
from typing import Any
from urllib.parse import urlparse

from ...config import load_config
from ..capabilities import policy as capability_policy
from .bridge import ElectronBrowserBridgeClient
from .browser_skill import BrowserSkillClient, get_browser_skill_client
from .file_grants import BrowserFileGrantService
from .models import (
    BrowserAccess,
    BrowserAction,
    BrowserObservation,
    BrowserPolicyDecision,
    BrowserReplayEvent,
)
from .policy import (
    browser_action_is_task_read_interaction,
    browser_url_policy_violation,
    canonical_action_access,
    canonical_action_name,
    infer_action_access,
)
from .profiles import BrowserProfileService
from .replay import BrowserReplayStore
from .safe_scripts import (
    safe_evaluate_script_id_from_metadata,
    safe_evaluate_script_params,
    safe_evaluate_script_source,
)
from .snapshot_search import search_snapshot_payload

_BROWSER_USE_KWARGS: frozenset[str] = frozenset(
    {
        "accept",
        "browser_args",
        "button",
        "cdp_port",
        "cdp_url",
        "code",
        "double_click",
        "element",
        "end_element",
        "end_ref",
        "end_selector",
        "executable_path",
        "fields_json",
        "filename",
        "frame_selector",
        "full_page",
        "headed",
        "height",
        "include_static",
        "index",
        "key",
        "level",
        "modifiers_json",
        "page_id",
        "path",
        "paths_json",
        "port",
        "port_max",
        "port_min",
        "private_mode",
        "profile_id",
        "prompt_text",
        "ref",
        "screenshot_type",
        "selector",
        "shadow_host_selector",
        "slowly",
        "snapshot_filename",
        "start_element",
        "start_ref",
        "start_selector",
        "submit",
        "tab_action",
        "text",
        "text_gone",
        "url",
        "values_json",
        "wait",
        "wait_time",
        "width",
    }
)

_CONFIRMATION_TOKEN_METADATA_KEY = "browser_confirmation_token"
_READ_TASK_TOKEN_METADATA_KEY = "browser_read_task_token"
_CONFIRMATION_INTERNAL_METADATA_KEYS = frozenset(
    {
        _CONFIRMATION_TOKEN_METADATA_KEY,
        _READ_TASK_TOKEN_METADATA_KEY,
        "confirmation_result",
        "confirmation_reason",
        "browser_task_action_timeout_seconds",
    }
)


class BrowserActionBroker:
    """Policy and replay boundary for all agentic browser actions."""

    def __init__(
        self,
        *,
        profiles: BrowserProfileService | None = None,
        replay: BrowserReplayStore | None = None,
        bridge: ElectronBrowserBridgeClient | None = None,
        browser_skill: BrowserSkillClient | None = None,
        file_grants: BrowserFileGrantService | None = None,
    ) -> None:
        self.profiles = profiles or BrowserProfileService()
        self.replay = replay or BrowserReplayStore()
        self.bridge = bridge or ElectronBrowserBridgeClient()
        self.browser_skill = browser_skill or get_browser_skill_client()
        self.file_grants = file_grants or BrowserFileGrantService()
        self._confirmation_tokens: dict[str, str] = {}
        self._read_task_tokens: dict[str, str] = {}

    def evaluate(self, action: BrowserAction) -> BrowserPolicyDecision:
        profile = self.profiles.get_profile(action.profile_id)
        access = self._canonical_access(action)
        confirmed = self._has_action_bound_confirmation(action)
        matrix = capability_policy.build_capability_matrix()
        browser_matrix_entry = matrix.entry(
            "browser_read_navigation" if access == "read" else "browser_write",
        )
        background_entry = matrix.entry("background_scheduled_execution")
        if matrix.distribution == "app_store" and (
            browser_matrix_entry.mode != "enabled" or background_entry.mode != "enabled"
        ):
            return BrowserPolicyDecision(
                decision="block",
                reason=(
                    browser_matrix_entry.description
                    or background_entry.description
                    or "App Store distribution disables agent browser automation."
                ),
                risk="high",
                access=access,
                matched_rule_id="app_store_browser_automation_blocked",
            )
        if profile is None:
            return BrowserPolicyDecision(
                decision="block",
                reason=f"Browser profile not found: {action.profile_id}",
                risk="medium",
                access=access,
                matched_rule_id="browser_profile_missing",
            )
        if not profile.agent_control_enabled:
            return BrowserPolicyDecision(
                decision="block",
                reason=(
                    f"Browser profile '{profile.id}' has not enabled agent control."
                ),
                risk="medium",
                access=access,
                matched_rule_id="browser_profile_agent_control_disabled",
            )
        url_violation = browser_url_policy_violation(action.url)
        if url_violation is not None:
            return BrowserPolicyDecision(
                decision="block",
                reason=url_violation.reason,
                risk="critical",
                access=access,
                matched_rule_id=url_violation.matched_rule_id,
            )
        domain = self._domain_for_action(action)
        if domain and profile.allowed_domains:
            if not self._domain_allowed(domain, profile.allowed_domains):
                return BrowserPolicyDecision(
                    decision="block",
                    reason=f"Domain is not allowed for this profile: {domain}",
                    risk="high",
                    access=access,
                    matched_rule_id="browser_domain_not_allowed",
                )
        canonical = canonical_action_name(action.action)
        if canonical in {"cookies_get", "cookies_set", "cookies_clear"}:
            return BrowserPolicyDecision(
                decision="block",
                reason="Raw cookie access is not exposed through Browser Action Broker.",
                risk="critical",
                access=access,
                matched_rule_id="browser_raw_cookie_blocked",
            )
        if canonical == "evaluate" and not self._safe_evaluate_code(action):
            return BrowserPolicyDecision(
                decision="block",
                reason=(
                    "Browser evaluate requires a registered read-only script id."
                ),
                risk="critical",
                access=access,
                matched_rule_id="browser_evaluate_script_registry_required",
            )
        if canonical == "upload_file":
            grant_id = self._file_grant_id(action)
            if not grant_id:
                return BrowserPolicyDecision(
                    decision="block",
                    reason="Browser file upload requires an explicit file grant.",
                    risk="critical",
                    access=access,
                    matched_rule_id="browser_upload_file_grant_required",
                )
            grant_error = self.file_grants.validate(grant_id, action)
            if grant_error is not None:
                return BrowserPolicyDecision(
                    decision="block",
                    reason=grant_error.reason,
                    risk="critical",
                    access=access,
                    matched_rule_id=grant_error.code,
                )
        if self._is_scheduled_browser_write(action, access):
            return BrowserPolicyDecision(
                decision="block",
                reason="Scheduled browser write actions are disabled in v1.",
                risk="critical",
                access=access,
                matched_rule_id="browser_scheduled_write_blocked",
            )
        manual_reason = self._manual_takeover_reason(action)
        if manual_reason:
            return BrowserPolicyDecision(
                decision="block",
                reason=manual_reason,
                risk="critical",
                access=access,
                matched_rule_id="browser_manual_takeover_required",
            )
        if access == "sensitive":
            return BrowserPolicyDecision(
                decision="allow" if confirmed else "prompt",
                reason="Sensitive browser actions require explicit user confirmation.",
                risk="critical",
                access=access,
                requires_confirmation=not confirmed,
                matched_rule_id="browser_sensitive_confirmation_required",
            )
        if access == "write" and profile.risk_policy in {"write", "always"}:
            return BrowserPolicyDecision(
                decision="allow" if confirmed else "prompt",
                reason="Browser write actions require user confirmation.",
                risk="medium",
                access=access,
                requires_confirmation=not confirmed,
                matched_rule_id="browser_write_confirmation_required",
            )
        if profile.risk_policy == "always" and not confirmed:
            return BrowserPolicyDecision(
                decision="prompt",
                reason="This profile requires confirmation before browser actions.",
                risk="medium",
                access=access,
                requires_confirmation=True,
                matched_rule_id="browser_profile_always_confirm",
            )
        return BrowserPolicyDecision(
            decision="allow",
            reason="Browser action allowed.",
            risk="low" if access == "read" else "medium",
            access=access,
        )

    async def execute(self, action: BrowserAction) -> BrowserObservation:
        decision = self.evaluate(action)
        if decision.decision != "allow":
            observation = BrowserObservation(
                ok=False,
                action=action.action,
                runtime=action.runtime or "playwright_fallback",
                profile_id=action.profile_id,
                page_id=action.page_id,
                url=action.url,
                error=decision.reason,
                decision=decision,
                data={
                    "requires_confirmation": decision.requires_confirmation,
                    "error_code": decision.matched_rule_id or "browser_policy_blocked",
                },
            )
            self._record(action, observation, decision)
            return observation
        confirmation_rule = decision.matched_rule_id in {
            "browser_sensitive_confirmation_required",
            "browser_write_confirmation_required",
            "browser_profile_always_confirm",
        }
        if confirmation_rule and not self._consume_action_bound_confirmation(action):
            observation = BrowserObservation(
                ok=False,
                action=action.action,
                runtime=action.runtime or "playwright_fallback",
                profile_id=action.profile_id,
                page_id=action.page_id,
                url=action.url,
                error="Browser confirmation token is invalid or already used.",
                decision=BrowserPolicyDecision(
                    decision="block",
                    reason="Browser confirmation token is invalid or already used.",
                    risk="critical" if decision.access == "sensitive" else "high",
                    access=decision.access,
                    matched_rule_id="browser_confirmation_token_invalid",
                ),
                data={
                    "requires_confirmation": True,
                    "error_code": "browser_confirmation_token_invalid",
                },
            )
            self._record(action, observation, observation.decision)
            return observation
        if (
            action.metadata.get(_READ_TASK_TOKEN_METADATA_KEY)
            and not self._consume_read_task_token(action)
        ):
            observation = BrowserObservation(
                ok=False,
                action=action.action,
                runtime=action.runtime or "playwright_fallback",
                profile_id=action.profile_id,
                page_id=action.page_id,
                url=action.url,
                error="Browser read-task authorization token is invalid or already used.",
                decision=BrowserPolicyDecision(
                    decision="block",
                    reason=(
                        "Browser read-task authorization token is invalid or "
                        "already used."
                    ),
                    risk="high",
                    access=decision.access,
                    matched_rule_id="browser_read_task_token_invalid",
                ),
                data={"error_code": "browser_read_task_token_invalid"},
            )
            self._record(action, observation, observation.decision)
            return observation

        runtime_action = self._with_safe_evaluate_code(action)
        grant_observation = self._consume_file_grant(runtime_action, decision)
        if grant_observation is not None:
            self._record(runtime_action, grant_observation, grant_observation.decision)
            return grant_observation
        runtime = runtime_action.runtime or self._configured_runtime()
        try:
            payload = await self._execute_runtime(runtime_action, runtime)
            ok = bool(payload.get("ok", True))
            resolved_runtime = payload.get("runtime") or runtime
            loaded_url = str(
                payload.get("final_url")
                or payload.get("url")
                or runtime_action.url
            )
            observation = BrowserObservation(
                ok=ok,
                action=runtime_action.action,
                runtime=resolved_runtime,
                profile_id=runtime_action.profile_id,
                page_id=str(payload.get("page_id") or payload.get("tab_id") or runtime_action.page_id),
                url=loaded_url,
                message=str(payload.get("message") or ""),
                error=str(payload.get("error")) if payload.get("error") else None,
                screenshot_path=str(
                    payload.get("screenshot_path") or payload.get("path") or ""
                )
                if runtime_action.action in {"screenshot", "take_screenshot"}
                else None,
                data=payload,
                decision=decision,
            )
            final_url_block = self._final_url_policy_observation(
                runtime_action,
                observation,
                decision,
            )
            if final_url_block is not None:
                observation = final_url_block
        except Exception as exc:  # pragma: no cover - defensive boundary
            observation = BrowserObservation(
                ok=False,
                action=runtime_action.action,
                runtime=runtime,
                profile_id=runtime_action.profile_id,
                page_id=runtime_action.page_id,
                url=runtime_action.url,
                error=str(exc),
                data={"error_code": "browser_runtime_exception"},
                decision=decision,
            )
        self.profiles.touch_profile(runtime_action.profile_id)
        self._record(runtime_action, observation, observation.decision or decision)
        return observation

    def confirm_action(
        self,
        action: BrowserAction,
        *,
        reason: str = "",
    ) -> BrowserAction:
        token = token_urlsafe(32)
        metadata = {
            **action.metadata,
            _CONFIRMATION_TOKEN_METADATA_KEY: token,
            "confirmation_result": "approved",
            "confirmation_reason": reason,
        }
        confirmed_action = action.model_copy(
            update={
                "confirmed": True,
                "metadata": metadata,
            }
        )
        self._confirmation_tokens[token] = self._confirmation_fingerprint(
            confirmed_action
        )
        return confirmed_action

    def authorize_read_task_interaction(
        self,
        action: BrowserAction,
        *,
        task_access: BrowserAccess,
    ) -> BrowserAction:
        """Bind one safe UI gesture to BrowserTaskRuntime's read authority."""

        clean_metadata = dict(action.metadata)
        clean_metadata.pop(_READ_TASK_TOKEN_METADATA_KEY, None)
        clean_action = action.model_copy(update={"metadata": clean_metadata})
        if not browser_action_is_task_read_interaction(
            action=clean_action.action,
            task_access=task_access,
            selector=clean_action.selector,
            text=clean_action.text,
            reason=clean_action.reason,
            metadata=clean_action.metadata,
        ):
            return clean_action
        token = token_urlsafe(32)
        authorized = clean_action.model_copy(
            update={
                "access": "read",
                "metadata": {
                    **clean_action.metadata,
                    _READ_TASK_TOKEN_METADATA_KEY: token,
                },
            }
        )
        self._read_task_tokens[token] = self._read_task_fingerprint(authorized)
        return authorized

    def grant_upload_file(
        self,
        *,
        path: str | Path,
        profile_id: str,
        run_id: str = "",
        page_id: str = "",
        url: str = "",
        selector: str = "",
        ttl_seconds: int | None = None,
        security_scoped_bookmark: bool = False,
        security_scope_ref: str = "",
        security_scope_bookmark_sha256: str = "",
    ):
        return self.file_grants.issue(
            path=path,
            profile_id=profile_id,
            run_id=run_id,
            page_id=page_id,
            url=url,
            selector=selector,
            ttl_seconds=ttl_seconds,
            security_scoped_bookmark=security_scoped_bookmark,
            security_scope_ref=security_scope_ref,
            security_scope_bookmark_sha256=security_scope_bookmark_sha256,
        )

    def file_grant_lifecycle_report(
        self,
        *,
        operation: str = "archive",
        dry_run: bool = True,
        run_id: str = "",
        profile_id: str = "",
    ) -> dict[str, Any]:
        return self.file_grants.lifecycle_report(
            operation=operation,  # type: ignore[arg-type]
            dry_run=dry_run,
            run_id=run_id,
            profile_id=profile_id,
        )

    def deny_confirmation(
        self,
        action: BrowserAction,
        *,
        reason: str = "",
    ) -> BrowserObservation:
        access = self._canonical_access(action)
        decision = BrowserPolicyDecision(
            decision="block",
            reason=reason or "User denied browser action confirmation.",
            risk="critical" if access == "sensitive" else "high",
            access=access,
            matched_rule_id="browser_user_denied_confirmation",
        )
        observation = BrowserObservation(
            ok=False,
            action=action.action,
            runtime=action.runtime or self._configured_runtime(),
            profile_id=action.profile_id,
            page_id=action.page_id,
            url=action.url,
            error=decision.reason,
            data={
                "confirmation_result": "denied",
                "error_code": decision.matched_rule_id,
            },
            decision=decision,
        )
        self._record(action, observation, decision)
        return observation

    async def _execute_runtime(
        self,
        action: BrowserAction,
        runtime: str,
    ) -> dict[str, Any]:
        if canonical_action_name(action.action) == "find":
            snapshot_action = action.model_copy(update={"action": "snapshot"})
            payload = await self._execute_runtime(snapshot_action, runtime)
            if not bool(payload.get("ok", True)):
                return {**payload, "action": "find"}
            return search_snapshot_payload(
                payload,
                text=action.text,
                regex=str(action.metadata.get("regex") or ""),
                max_results=int(action.metadata.get("max_results") or 20),
                context_chars=int(action.metadata.get("context_chars") or 96),
            )
        if runtime == "electron_bridge":
            if self.bridge.available:
                interrupted = await self._electron_interruption()
                if interrupted is not None:
                    return interrupted
                payload = await self.bridge.execute(action)
                payload.setdefault("runtime", "electron_bridge")
                return payload
            browser_skill_status = await self.browser_skill.status()
            browser_skill_requested = bool(
                action.metadata.get("browser_skill_browser_id")
            )
            if browser_skill_status.get("connected") and (
                browser_skill_status.get(
                    "ready",
                    browser_skill_status.get("connected"),
                )
                or browser_skill_requested
            ):
                browser_skill_payload = await self.browser_skill.execute(action)
                browser_skill_payload.setdefault("runtime", "browser_skill")
                browser_skill_payload.setdefault(
                    "fallback_reason",
                    "Electron Browser Bridge is unavailable.",
                )
                return browser_skill_payload
            fallback_payload = await self._execute_playwright_fallback(action)
            fallback_payload.setdefault("runtime", "playwright_fallback")
            fallback_payload.setdefault(
                "fallback_reason",
                (
                    "Electron Browser Bridge is unavailable and BrowserSkill "
                    "is not ready."
                ),
            )
            return fallback_payload
        if runtime == "browser_skill":
            payload = await self.browser_skill.execute(action)
            payload.setdefault("runtime", "browser_skill")
            return payload
        payload = await self._execute_playwright_fallback(action)
        payload.setdefault("runtime", "playwright_fallback")
        return payload

    async def _electron_interruption(self) -> dict[str, Any] | None:
        get_state = getattr(self.bridge, "get_state", None)
        if not callable(get_state):
            return None
        state = await get_state()
        if not isinstance(state, dict) or state.get("ok") is False:
            return None
        status = str(state.get("agent_status") or "").strip().lower()
        if status in {"已暂停", "paused", "agent paused"}:
            return {
                "ok": False,
                "runtime": "electron_bridge",
                "error": "Browser agent control is paused by the user.",
                "interrupted": True,
                "interruption": "paused",
            }
        if status in {"已终止", "stopped", "terminated", "agent stopped"}:
            return {
                "ok": False,
                "runtime": "electron_bridge",
                "error": "Browser agent control was stopped by the user.",
                "interrupted": True,
                "interruption": "stopped",
            }
        return None

    async def _execute_playwright_fallback(
        self,
        action: BrowserAction,
    ) -> dict[str, Any]:
        from ...agents.tools.browser_control import (
            _allow_internal_browser_runtime,
            browser_use,
        )

        fallback_action = self._fallback_action_name(action.action)
        kwargs: dict[str, Any] = {
            "action": fallback_action,
            "url": action.url,
            "page_id": action.page_id,
            "selector": action.selector,
            "ref": action.ref,
            "text": action.text,
            "code": action.code or self._fallback_code(action.action),
            "path": action.path,
            "profile_id": action.profile_id,
            "legacy_runtime": True,
        }
        kwargs.update(
            {
                key: value
                for key, value in action.metadata.items()
                if key in _BROWSER_USE_KWARGS
            }
        )
        with _allow_internal_browser_runtime():
            response = await browser_use(**kwargs)
        return self._tool_response_json(response)

    def _final_url_policy_observation(
        self,
        action: BrowserAction,
        observation: BrowserObservation,
        decision: BrowserPolicyDecision,
    ) -> BrowserObservation | None:
        if not observation.ok:
            return None
        final_url = str(observation.url or "").strip()
        if not final_url or final_url == "about:blank":
            return None

        violation = browser_url_policy_violation(final_url)
        if violation is not None:
            block_decision = BrowserPolicyDecision(
                decision="block",
                reason=violation.reason,
                risk="critical",
                access=decision.access,
                matched_rule_id=violation.matched_rule_id,
            )
            return self._blocked_final_url_observation(
                action,
                observation,
                block_decision,
            )

        profile = self.profiles.get_profile(action.profile_id)
        domain = self._domain_for_url(final_url)
        if (
            profile is not None
            and domain
            and profile.allowed_domains
            and not self._domain_allowed(domain, profile.allowed_domains)
        ):
            block_decision = BrowserPolicyDecision(
                decision="block",
                reason="Final browser URL is not allowed for this profile.",
                risk="high",
                access=decision.access,
                matched_rule_id="browser_final_domain_not_allowed",
            )
            return self._blocked_final_url_observation(
                action,
                observation,
                block_decision,
            )
        return None

    @staticmethod
    def _blocked_final_url_observation(
        action: BrowserAction,
        observation: BrowserObservation,
        decision: BrowserPolicyDecision,
    ) -> BrowserObservation:
        final_url = str(observation.url or "").strip()
        return observation.model_copy(
            update={
                "ok": False,
                "error": decision.reason,
                "decision": decision,
                "data": {
                    **observation.data,
                    "error_code": decision.matched_rule_id
                    or "browser_final_url_policy_blocked",
                    "initial_url": action.url,
                    "final_url": final_url,
                    "blocked_after_load": True,
                },
            },
        )

    @staticmethod
    def _fallback_action_name(action: str) -> str:
        aliases = {
            "new_tab": "open",
            "select_tab": "tabs",
            "close_tab": "tabs",
            "snapshot_ax": "snapshot",
            "snapshot_dom": "snapshot",
            "console_list": "console_messages",
            "network_list": "network_requests",
            "upload_file": "file_upload",
            "select": "select_option",
            "extract_table": "evaluate",
            "extract_tables": "evaluate",
            "extract_list": "evaluate",
            "extract_items": "evaluate",
        }
        return aliases.get(action, action)

    @staticmethod
    def _fallback_code(action: str) -> str:
        if action in {"extract_table", "extract_tables"}:
            return (
                "() => Array.from(document.querySelectorAll('table')).slice(0, 5)"
                ".map((table, index) => ({ index, text: table.innerText, "
                "rows: Array.from(table.querySelectorAll('tr')).slice(0, 80)"
                ".map((row) => Array.from(row.querySelectorAll('th,td'))"
                ".map((cell) => cell.innerText.trim())) }))"
            )
        if action in {"extract_list", "extract_items"}:
            return (
                "() => Array.from(document.querySelectorAll('article, "
                "[role=listitem], li, .item, .card')).slice(0, 80)"
                ".map((el) => ({ text: el.innerText, href: "
                "(el.matches('a') ? el : el.querySelector('a[href]'))?.href || '' }))"
                ".filter((item) => item.text)"
            )
        return ""

    @staticmethod
    def _tool_response_json(response: Any) -> dict[str, Any]:
        dumped = response.model_dump(mode="json") if hasattr(response, "model_dump") else {}
        blocks = dumped.get("content") or getattr(response, "content", None) or []
        text = "\n".join(
            str(
                (
                    block.get("text")
                    if isinstance(block, dict)
                    else getattr(block, "text", "")
                )
                or ""
            )
            for block in blocks
            if (
                block.get("text")
                if isinstance(block, dict)
                else getattr(block, "text", None)
            )
        )
        if not text:
            return {"ok": True}
        try:
            payload = json.loads(text)
            return payload if isinstance(payload, dict) else {"ok": True, "data": payload}
        except json.JSONDecodeError:
            return {"ok": True, "text": text}

    def _record(
        self,
        action: BrowserAction,
        observation: BrowserObservation,
        decision: BrowserPolicyDecision,
    ) -> None:
        metadata: dict[str, Any] = {
            "selector": bool(action.selector),
            "selector_value": self._safe_selector(action.selector),
            "ref": bool(action.ref),
            "ref_value": self._safe_selector(action.ref),
            "path": self._safe_path(action.path),
            "text_length": len(action.text or ""),
            **{
                key: value
                for key, value in action.metadata.items()
                if key
                not in {
                    "fields_json",
                    "paths_json",
                    "values_json",
                    "regex",
                    _CONFIRMATION_TOKEN_METADATA_KEY,
                    _READ_TASK_TOKEN_METADATA_KEY,
                }
            },
        }
        summary = self._observation_summary(observation)
        if summary:
            metadata["observation_summary"] = summary
        error_code = observation.data.get("error_code")
        if error_code:
            metadata["error_code"] = str(error_code)[:120]
        event = BrowserReplayEvent(
            id=observation.replay_event_id,
            run_id=action.run_id,
            profile_id=action.profile_id,
            action=action.action,
            runtime=observation.runtime,
            access=decision.access,
            risk=decision.risk,
            decision=decision.decision,
            reason=action.reason or decision.reason,
            ok=observation.ok,
            url=observation.url or action.url,
            page_id=observation.page_id,
            error=observation.error,
            screenshot_path=observation.screenshot_path,
            metadata=metadata,
        )
        self.replay.append(event)

    @staticmethod
    def _infer_access(action: str) -> BrowserAccess:
        return infer_action_access(action)

    def _canonical_access(self, action: BrowserAction) -> BrowserAccess:
        if self._has_read_task_token(action):
            return "read"
        if action.access != "sensitive" and canonical_action_name(
            action.action
        ) in {"hover", "press_key"} and browser_action_is_task_read_interaction(
            action=action.action,
            task_access="read",
            selector=action.selector,
            text=action.text,
            reason=action.reason,
            metadata=action.metadata,
        ):
            return "read"
        if canonical_action_name(action.action) == "evaluate" and self._safe_evaluate_code(action):
            if action.access in {"write", "sensitive"}:
                return action.access
            return "read"
        return canonical_action_access(action.action, action.access)

    @staticmethod
    def _safe_evaluate_code(action: BrowserAction) -> str:
        if canonical_action_name(action.action) != "evaluate":
            return ""
        script_id = safe_evaluate_script_id_from_metadata(action.metadata)
        if not script_id:
            return ""
        try:
            return safe_evaluate_script_source(
                script_id,
                safe_evaluate_script_params(action.metadata),
            )
        except ValueError:
            return ""

    @classmethod
    def _with_safe_evaluate_code(cls, action: BrowserAction) -> BrowserAction:
        code = cls._safe_evaluate_code(action)
        if not code or action.code == code:
            return action
        return action.model_copy(update={"code": code})

    @staticmethod
    def _file_grant_id(action: BrowserAction) -> str:
        return str(
            action.metadata.get("file_grant_id")
            or action.metadata.get("browser_file_grant_id")
            or ""
        ).strip()

    def _consume_file_grant(
        self,
        action: BrowserAction,
        allow_decision: BrowserPolicyDecision,
    ) -> BrowserObservation | None:
        if canonical_action_name(action.action) != "upload_file":
            return None
        grant_id = self._file_grant_id(action)
        error = self.file_grants.consume(grant_id, action)
        if error is None:
            return None
        decision = BrowserPolicyDecision(
            decision="block",
            reason=error.reason,
            risk="critical",
            access=allow_decision.access,
            matched_rule_id=error.code,
        )
        return BrowserObservation(
            ok=False,
            action=action.action,
            runtime=action.runtime or self._configured_runtime(),
            profile_id=action.profile_id,
            page_id=action.page_id,
            url=action.url,
            error=error.reason,
            data={"error_code": error.code},
            decision=decision,
        )

    def _has_action_bound_confirmation(self, action: BrowserAction) -> bool:
        if not action.confirmed:
            return False
        token = str(action.metadata.get(_CONFIRMATION_TOKEN_METADATA_KEY) or "")
        if not token:
            return False
        expected = self._confirmation_tokens.get(token)
        return expected == self._confirmation_fingerprint(action)

    def _consume_action_bound_confirmation(self, action: BrowserAction) -> bool:
        if not action.confirmed:
            return True
        token = str(action.metadata.get(_CONFIRMATION_TOKEN_METADATA_KEY) or "")
        if not token:
            return False
        expected = self._confirmation_tokens.pop(token, None)
        return expected == self._confirmation_fingerprint(action)

    def _confirmation_fingerprint(self, action: BrowserAction) -> str:
        metadata = {
            key: value
            for key, value in action.metadata.items()
            if key not in _CONFIRMATION_INTERNAL_METADATA_KEYS
        }
        payload = {
            "action": action.action,
            "profile_id": action.profile_id,
            "url": action.url,
            "page_id": action.page_id,
            "selector": action.selector,
            "ref": action.ref,
            "text": action.text,
            "code": action.code,
            "path": action.path,
            "access": self._canonical_access(action),
            "run_id": action.run_id,
            "runtime": action.runtime,
            "metadata": metadata,
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return sha256(encoded.encode("utf-8")).hexdigest()

    def _has_read_task_token(self, action: BrowserAction) -> bool:
        token = str(action.metadata.get(_READ_TASK_TOKEN_METADATA_KEY) or "")
        if not token:
            return False
        return self._read_task_tokens.get(token) == self._read_task_fingerprint(action)

    def _consume_read_task_token(self, action: BrowserAction) -> bool:
        token = str(action.metadata.get(_READ_TASK_TOKEN_METADATA_KEY) or "")
        if not token:
            return True
        expected = self._read_task_tokens.pop(token, None)
        return expected == self._read_task_fingerprint(action)

    @staticmethod
    def _read_task_fingerprint(action: BrowserAction) -> str:
        metadata = {
            key: value
            for key, value in action.metadata.items()
            if key != _READ_TASK_TOKEN_METADATA_KEY
        }
        payload = {
            "action": action.action,
            "profile_id": action.profile_id,
            "url": action.url,
            "page_id": action.page_id,
            "selector": action.selector,
            "ref": action.ref,
            "text": action.text,
            "code": action.code,
            "path": action.path,
            "access": action.access,
            "run_id": action.run_id,
            "runtime": action.runtime,
            "metadata": metadata,
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _domain_for_action(action: BrowserAction) -> str:
        domain = BrowserActionBroker._domain_for_url(action.url)
        if domain:
            return domain
        return str(action.metadata.get("domain") or "").strip().lower()

    @staticmethod
    def _domain_for_url(url: str) -> str:
        if url:
            parsed = urlparse(url)
            if parsed.hostname:
                return parsed.hostname.lower()
        return ""

    @staticmethod
    def _domain_allowed(domain: str, allowed: list[str]) -> bool:
        domain = domain.lower().strip(".")
        for item in allowed:
            rule = item.lower().strip(".")
            if not rule:
                continue
            if domain == rule or domain.endswith(f".{rule}"):
                return True
        return False

    @staticmethod
    def _safe_path(path: str) -> str:
        if not path:
            return ""
        try:
            return str(Path(path).name if Path(path).is_absolute() else path)
        except Exception:
            return ""

    @staticmethod
    def _safe_selector(selector: str) -> str:
        value = str(selector or "").strip()
        if not value:
            return ""
        lowered = value.lower()
        if any(
            marker in lowered
            for marker in ("password", "passwd", "cookie", "token", "secret", "auth")
        ):
            return ""
        return value[:240]

    @staticmethod
    def _safe_scope(scope: Any) -> dict[str, str]:
        if not isinstance(scope, dict):
            return {}
        out: dict[str, str] = {}
        frame_selector = BrowserActionBroker._safe_selector(
            str(scope.get("frame_selector") or "")
        )
        shadow_host_selector = BrowserActionBroker._safe_selector(
            str(scope.get("shadow_host_selector") or "")
        )
        if frame_selector:
            out["frame_selector"] = frame_selector
        if shadow_host_selector:
            out["shadow_host_selector"] = shadow_host_selector
        return out

    @staticmethod
    def _observation_summary(observation: BrowserObservation) -> dict[str, Any]:
        payload = observation.data.get("data")
        if not isinstance(payload, dict):
            return {}
        out: dict[str, Any] = {}
        if payload.get("title"):
            out["title"] = str(payload.get("title"))[:180]
        if payload.get("url"):
            out["url"] = str(payload.get("url"))[:400]
        links = payload.get("links")
        controls = payload.get("controls")
        text = payload.get("text")
        if isinstance(links, list):
            out["links_count"] = len(links)
        if isinstance(controls, list):
            out["controls_count"] = len(controls)
        if isinstance(text, str):
            out["text_length"] = len(text)
            out["text_excerpt"] = text[:240]
        page_context = payload.get("page_context")
        if isinstance(page_context, dict):
            out["page_context"] = {
                "kind": str(page_context.get("kind") or "normal")[:80],
                "requires_manual_takeover": bool(
                    page_context.get("requires_manual_takeover")
                ),
            }
        scope = BrowserActionBroker._safe_scope(payload.get("scope"))
        if scope:
            out["scope"] = scope
        details = payload.get("details")
        if isinstance(details, list):
            field_scopes: list[dict[str, str]] = []
            for detail in details[:20]:
                if not isinstance(detail, dict):
                    continue
                detail_scope = BrowserActionBroker._safe_scope(detail.get("scope"))
                if not detail_scope:
                    continue
                selector = BrowserActionBroker._safe_selector(
                    str(detail.get("selector") or "")
                )
                item = {"selector": selector, **detail_scope}
                field_scopes.append(
                    {key: value for key, value in item.items() if value}
                )
            if field_scopes:
                out["field_scopes"] = field_scopes
                out["scoped_field_count"] = len(field_scopes)
        value = payload.get("value")
        if isinstance(value, list):
            out["items_count"] = len(value)
        elif value is not None:
            out["value_type"] = type(value).__name__
        return out

    @staticmethod
    def _configured_runtime() -> str:
        try:
            return load_config().browser.primary_runtime
        except Exception:
            return "playwright_fallback"

    @staticmethod
    def _is_scheduled_browser_write(
        action: BrowserAction,
        access: BrowserAccess,
    ) -> bool:
        if access == "read":
            return False
        scheduled = bool(
            action.metadata.get("scheduled")
            or action.metadata.get("cron")
            or action.metadata.get("background_task"),
        )
        if not scheduled:
            return False
        try:
            return not bool(load_config().browser.enable_scheduled_browser_tasks)
        except Exception:
            return True

    @staticmethod
    def _manual_takeover_reason(action: BrowserAction) -> str:
        if not action.metadata.get("requires_manual_takeover"):
            return ""
        page_kind = str(action.metadata.get("page_kind") or "sensitive").strip()
        reasons = {
            "login": "Login pages require manual takeover.",
            "captcha": "Captcha pages require manual takeover.",
            "payment": "Payment pages require manual takeover.",
            "publish": "Publishing pages require manual takeover or explicit product confirmation.",
            "delete": "Deletion pages require manual takeover or explicit product confirmation.",
            "authorize": "Authorization pages require manual takeover.",
            "send_message": "Message sending pages require manual takeover or explicit product confirmation.",
            "profile_update": "Profile update pages require manual takeover or explicit product confirmation.",
        }
        return reasons.get(page_kind, "This browser step requires manual takeover.")
