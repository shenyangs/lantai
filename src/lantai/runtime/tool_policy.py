# -*- coding: utf-8 -*-
"""Native Runtime-owned tool policy middleware."""

# pylint: disable=not-callable,too-many-branches,too-many-return-statements

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
import logging
from pathlib import Path
import re
from typing import Any

from .effects import RuntimeEffectRecoveryCapability
from .request_policy_receipt import (
    PolicyConsumptionObservation,
    PolicyEvaluationObservation,
    RequestPolicyReceiptObserver,
)
from .tools import (
    ToolCall,
    ToolPolicyContext,
    ToolPolicyOutcome,
    ToolSpec,
    runtime_blocked_tool_names,
)

logger = logging.getLogger(__name__)


_RUNTIME_OWNED_ARTIFACT_TOOLS = frozenset(
    {
        "declare_deliverable_contract",
        "resolve_pending_deliverable_choice",
        "finalize_deliverable",
    },
)
_ARTIFACT_DELIVERY_IDEMPOTENCY_PREFIX = "artifact-delivery:artifact:"


@dataclass(frozen=True, slots=True)
class _InternalAllowDecision:
    decision: str = "allow"
    reason: str = ""
    risk: str = "low"
    command_category: str = "internal_coordination"
    decision_source: str = "runtime_owned"


@dataclass(frozen=True, slots=True)
class _ChromeDevToolsPolicyDecision:
    decision: str
    reason: str
    risk: str
    profile: str
    matched_rule_id: str
    approval_copy: str | None = None
    permission_mode: str = "quiet"
    command_category: str = "chrome_devtools"
    decision_source: str = "official_chrome_devtools"


def _runtime_owned_artifact_tool(spec: ToolSpec) -> bool:
    """Recognize only the built-in artifact gates registered by Runner."""

    return (
        spec.origin == "builtin"
        and spec.name in _RUNTIME_OWNED_ARTIFACT_TOOLS
        and spec.metadata.get("runtime_owned") is True
    )


def _path_is_within_workspace(
    raw_path: Any,
    *,
    workspace_dir: str | None,
) -> bool:
    """Resolve the delivery target without weakening workspace confinement."""

    normalized_workspace = str(workspace_dir or "").strip()
    normalized_path = str(raw_path or "").strip()
    if not normalized_workspace or not normalized_path:
        return False
    try:
        workspace = Path(normalized_workspace).expanduser().resolve()
        candidate = Path(normalized_path).expanduser()
        if not candidate.is_absolute():
            candidate = workspace / candidate
        return candidate.resolve().is_relative_to(workspace)
    except (OSError, RuntimeError, ValueError):
        return False


_CHROME_PATHISH_KEY = re.compile(
    r"(?:^|_)(?:file|path)|(?:file|path)(?:$|_)",
    re.I,
)
_CHROME_URL_KEY = re.compile(r"(?:^|_)(?:url|uri)|(?:url|uri)(?:$|_)", re.I)


def _walk_chrome_argument_fields(
    value: Any,
    *,
    prefix: tuple[str, ...] = (),
):
    if isinstance(value, dict):
        for key, item in value.items():
            field_path = (*prefix, str(key))
            yield field_path, item
            yield from _walk_chrome_argument_fields(item, prefix=field_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_chrome_argument_fields(
                item,
                prefix=(*prefix, str(index)),
            )


def _chrome_path_candidate(
    raw_value: Any,
    *,
    root: Path,
) -> Path | None:
    if not isinstance(raw_value, str):
        return None
    value = raw_value.strip()
    if not value or "\x00" in value:
        return None
    lowered = value.casefold()
    if lowered.startswith("file:") or "://" in lowered:
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate


def _chrome_path_policy_error(
    tool_name: str,
    arguments: dict[str, Any],
    context: ToolPolicyContext,
) -> str:
    """Independently enforce roots before Connector Adapter validation."""

    from ..app.mcp.chrome_devtools import CHROME_DEVTOOLS_PATH_FIELDS

    allowed_fields = CHROME_DEVTOOLS_PATH_FIELDS.get(tool_name, {})
    for field_path, value in _walk_chrome_argument_fields(arguments):
        field_name = field_path[-1]
        if field_name.casefold() in {
            "file_grant_id",
            "grant_id",
            "workspace_grant_id",
        }:
            return "Tool arguments cannot grant Chrome DevTools file access."
        if _CHROME_URL_KEY.search(field_name) and isinstance(value, str):
            if value.strip().casefold().startswith("file:"):
                return "Chrome DevTools file URLs are forbidden."
        if not _CHROME_PATHISH_KEY.search(field_name):
            continue
        if len(field_path) != 1 or field_name not in allowed_fields:
            return "Chrome DevTools received an unreviewed path field."

    populated = [
        field_name
        for field_name in allowed_fields
        if arguments.get(field_name) not in (None, "", [])
    ]
    if not populated:
        return ""
    workspace_dir = str(context.workspace_dir or "").strip()
    if not workspace_dir:
        return "Chrome DevTools file access requires an active workspace."
    try:
        root = Path(workspace_dir).expanduser().resolve(strict=True)
    except OSError:
        return "Chrome DevTools workspace root is unavailable."

    for field_name in populated:
        mode = allowed_fields[field_name]
        raw_values = arguments.get(field_name)
        values = raw_values if mode == "upload_files" else [raw_values]
        if not isinstance(values, list) or not values:
            return f"Chrome DevTools {field_name} is invalid."
        if mode == "upload_files" and not str(
            context.workspace_grant_id or "",
        ).strip():
            return "Chrome DevTools uploads require a host-issued workspace grant."
        for value in values:
            candidate = _chrome_path_candidate(value, root=root)
            if candidate is None:
                return f"Chrome DevTools {field_name} is not a valid local path."
            resolved = candidate.resolve(strict=False)
            if resolved != root and root not in resolved.parents:
                return f"Chrome DevTools {field_name} escapes the workspace."
            if mode.startswith("write_"):
                if context.workspace_access_mode != "read_write":
                    return "Chrome DevTools cannot write in a read-only workspace."
                continue
            try:
                strict = candidate.resolve(strict=True)
            except OSError:
                return f"Chrome DevTools {field_name} does not exist."
            if strict != root and root not in strict.parents:
                return f"Chrome DevTools {field_name} resolves outside the workspace."
            if mode in {"read_file", "upload_files"} and not strict.is_file():
                return f"Chrome DevTools {field_name} must be a regular file."
            if mode == "read_dir" and not strict.is_dir():
                return f"Chrome DevTools {field_name} must be a directory."
    return ""


def _chrome_devtools_policy_decision(
    tool_call: ToolCall,
    spec: ToolSpec,
    context: ToolPolicyContext,
):
    """Return an exact policy only for host-authenticated Chrome metadata."""

    from ..app.browser_workbench.policy import browser_url_policy_violation
    from ..app.mcp.chrome_devtools import (
        chrome_devtools_tool_effect,
        is_official_chrome_devtools_metadata,
    )

    if spec.origin != "mcp" or not is_official_chrome_devtools_metadata(
        spec.metadata,
    ):
        return None, None
    if tool_call.name != spec.name:
        effect = chrome_devtools_tool_effect("")
        return effect, _ChromeDevToolsPolicyDecision(
            decision="forbidden",
            reason="Chrome DevTools tool identity mismatch.",
            risk="critical",
            profile=context.permission_profile,
            matched_rule_id="chrome_devtools_identity_mismatch",
        )

    effect = chrome_devtools_tool_effect(spec.name, tool_call.arguments)
    if effect.category == "unknown" or effect.approval == "forbidden":
        return effect, _ChromeDevToolsPolicyDecision(
            decision="forbidden",
            reason="Chrome DevTools tool is not in the reviewed 1.8.0 inventory.",
            risk="critical",
            profile=context.permission_profile,
            matched_rule_id="chrome_devtools_tool_unreviewed",
        )
    if not spec.enabled:
        return effect, _ChromeDevToolsPolicyDecision(
            decision="forbidden",
            reason="Chrome DevTools tool is disabled in this runtime.",
            risk="critical",
            profile=context.permission_profile,
            matched_rule_id="chrome_devtools_tool_disabled",
        )

    spec.read_only = effect.read_only
    spec.concurrency_safe = effect.read_only
    spec.destructive = effect.destructive
    spec.effect_recovery = RuntimeEffectRecoveryCapability(
        external_side_effect=effect.external_side_effect,
        idempotency=effect.idempotency,
        probe=effect.probe,
        compensation=effect.compensation,
        on_ambiguity=effect.on_ambiguity,
        notes=(
            "Per-call host Chrome DevTools effect contract; optional path "
            "arguments are reflected before EffectRecord intent."
        ),
    )
    spec.metadata["chrome_devtools_effect"] = {
        "category": effect.category,
        "approval": effect.approval,
        "populated_path_fields": list(effect.populated_path_fields),
    }

    path_error = _chrome_path_policy_error(
        spec.name,
        dict(tool_call.arguments),
        context,
    )
    if path_error:
        return effect, _ChromeDevToolsPolicyDecision(
            decision="forbidden",
            reason=path_error,
            risk="critical",
            profile=context.permission_profile,
            matched_rule_id="chrome_devtools_path_forbidden",
        )

    if effect.category == "navigation":
        url = str(tool_call.arguments.get("url") or "").strip()
        violation = browser_url_policy_violation(url)
        if violation is not None:
            if violation.matched_rule_id != "browser_private_network_url_blocked":
                return effect, _ChromeDevToolsPolicyDecision(
                    decision="forbidden",
                    reason=violation.reason,
                    risk="critical",
                    profile=context.permission_profile,
                    matched_rule_id=violation.matched_rule_id,
                )
            return effect, _ChromeDevToolsPolicyDecision(
                decision="prompt",
                reason=(
                    "Chrome DevTools is about to access a local or private-network "
                    "development target."
                ),
                risk="high",
                profile=context.permission_profile,
                matched_rule_id="chrome_devtools_private_network_prompt",
                approval_copy=f"允许 Chrome DevTools 访问 {url}？",
            )

    decision = "allow" if effect.approval == "auto" else "prompt"
    risk = "low" if effect.approval == "auto" else effect.approval
    return effect, _ChromeDevToolsPolicyDecision(
        decision=decision,
        reason=(
            "Chrome DevTools action matches the host-reviewed full-capability "
            f"{effect.category} policy."
        ),
        risk=risk,
        profile=context.permission_profile,
        matched_rule_id=f"chrome_devtools_{effect.category}",
        approval_copy=(
            f"允许 Chrome DevTools 执行 {spec.name}？"
            if decision == "prompt"
            else None
        ),
    )


def _effect_record_auto_delivery(
    tool_call: ToolCall,
    spec: ToolSpec,
    context: ToolPolicyContext,
) -> bool:
    """Recognize a Runtime-created delivery for one committed S6 artifact."""

    metadata = tool_call.metadata
    idempotency_key = str(metadata.get("idempotency_key") or "").strip()
    return (
        spec.origin == "builtin"
        and spec.name == "send_file_to_user"
        and tool_call.name == spec.name
        and metadata.get("auto_delivery") is True
        and idempotency_key.startswith(_ARTIFACT_DELIVERY_IDEMPOTENCY_PREFIX)
        and _path_is_within_workspace(
            tool_call.arguments.get("file_path"),
            workspace_dir=context.workspace_dir,
        )
    )


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_safe(to_dict())
    if hasattr(value, "model_dump"):
        try:
            return _json_safe(value.model_dump(mode="json"))
        except TypeError:
            return _json_safe(value.model_dump())
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))
    if isinstance(value, Enum):
        return _json_safe(value.value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _has_approval_route(context: ToolPolicyContext) -> bool:
    return bool(context.root_session_id or context.session_id)


def _full_access_enabled(
    context: ToolPolicyContext,
    policy_payload: dict[str, Any],
) -> bool:
    """Honor the turn preset even when a policy adapter omits echo fields."""

    profile = str(
        context.permission_profile or policy_payload.get("profile") or "",
    ).strip()
    permission_mode = str(
        context.permission_mode or policy_payload.get("permission_mode") or "",
    ).strip()
    distribution = str(
        context.distribution or policy_payload.get("distribution") or "",
    ).strip()
    return (
        profile == "full" or permission_mode == "full_auto"
    ) and distribution != "app_store"


def _outcome(
    action: str,
    *,
    reason: str = "",
    risk: str = "low",
    policy_payload: dict[str, Any] | None = None,
    guard_payload: dict[str, Any] | None = None,
    approval_copy: str | None = None,
    remembered_approval: dict[str, Any] | None = None,
    source: str,
) -> ToolPolicyOutcome:
    return ToolPolicyOutcome(
        action=action,  # type: ignore[arg-type]
        reason=reason,
        risk=risk,
        requires_approval=action == "needs_approval",
        approval_copy=approval_copy,
        remembered_approval=remembered_approval,
        policy_payload=policy_payload,
        guard_payload=guard_payload,
        metadata={"source": source},
    )


_SENSITIVE_TOOL_TOKENS = (
    "append",
    "auth",
    "browser",
    "code",
    "delete",
    "edit",
    "exec",
    "pay",
    "publish",
    "python",
    "send",
    "shell",
    "upload",
    "write",
)


def _is_sensitive_tool_name(tool_name: str) -> bool:
    lowered = str(tool_name or "").strip().lower()
    return any(token in lowered for token in _SENSITIVE_TOOL_TOKENS)


_COLLABORATION_READ_ONLY_EXACT = frozenset(
    {
        "desktop_screenshot",
        "get_current_time",
        "get_token_usage",
        "read_file",
        "read_skill",
        "grep_search",
        "glob_search",
        "view_image",
        "view_video",
        "list_agents",
        "check_agent_task",
        "wiki_search",
        "wiki_read",
        "wiki_graph_neighbors",
        "wiki_ingest_status",
    },
)


def collaboration_tool_is_read_only(tool_name: str) -> bool:
    """Compatibility path for the legacy toolkit, using an explicit catalog."""

    normalized = str(tool_name or "").strip().lower()
    return normalized in _COLLABORATION_READ_ONLY_EXACT


def _engine_error_payload(reason: str, detail: str = "") -> dict[str, Any]:
    return {
        "decision": "forbidden",
        "reason": reason,
        "matched_rule_id": "policy_engine_error",
        "matched_rule_ids": ["policy_engine_error"],
        "risk": "critical",
        "hard_denied": True,
        "error_detail": detail,
    }


def _engine_error_outcome(
    *,
    reason: str,
    detail: str = "",
    policy_payload: bool = False,
    guard_payload: bool = False,
) -> ToolPolicyOutcome:
    payload = _engine_error_payload(reason, detail)
    return _outcome(
        "deny",
        reason=reason,
        risk="critical",
        policy_payload=payload if policy_payload else None,
        guard_payload=payload if guard_payload else None,
        source="policy_engine_error",
    )


class RuntimeToolPolicyMiddleware:
    """Evaluate runtime policy, remembered approvals, and ToolGuard."""

    def __init__(self, approval_service: Any | None = None) -> None:
        self._approval_service = approval_service

    async def _context_with_live_session_permission(
        self,
        context: ToolPolicyContext,
    ) -> tuple[ToolPolicyContext, str]:
        """Overlay the latest composer choice onto a running turn."""

        service = self._approval_service
        get_override = getattr(
            service,
            "get_session_permission_override",
            None,
        )
        if not callable(get_override):
            return context, "not_available"
        root_session_id = str(
            context.root_session_id or context.session_id or "",
        ).strip()
        user_id = str(context.request_context.get("user_id") or "").strip()
        if not root_session_id or not user_id:
            return context, "missing_binding"
        try:
            override = await get_override(
                root_session_id=root_session_id,
                user_id=user_id,
                updated_after=context.request_context.get(
                    "permission_snapshot_at",
                ),
            )
        except Exception:
            logger.debug(
                "Failed to read live session permission override",
                exc_info=True,
            )
            return context, "read_failed"
        if not override:
            return context, "unchanged"

        permission_mode = str(
            override.get("permission_mode") or context.permission_mode or "",
        ).strip()
        permission_profile = str(
            override.get("permission_profile")
            or context.permission_profile
            or "workspace",
        ).strip()
        request_context = {
            **context.request_context,
            "permission_mode": permission_mode,
            "permission_profile": permission_profile,
        }
        return (
            context.model_copy(
                update={
                    "permission_mode": permission_mode or None,
                    "permission_profile": permission_profile,
                    "request_context": request_context,
                },
            ),
            "applied",
        )

    async def evaluate(
        self,
        tool_call: ToolCall,
        spec: ToolSpec,
        context: ToolPolicyContext,
        *,
        policy_observer: RequestPolicyReceiptObserver | None = None,
        policy_evaluation_observation: (
            PolicyEvaluationObservation | None
        ) = None,
        middleware_index: int = 0,
    ) -> ToolPolicyOutcome:
        """Evaluate one tool call without executing it."""

        consumption_observation = None
        if (
            type(policy_observer) is RequestPolicyReceiptObserver
            and type(policy_evaluation_observation)
            is PolicyEvaluationObservation
            and type(middleware_index) is int
        ):
            try:
                consumption_observation = (
                    policy_observer.start_policy_consumption(
                        policy_evaluation_observation,
                        middleware_index=middleware_index,
                        delivered_context=context,
                    )
                )
            except Exception:
                consumption_observation = None
        outcome = await self._evaluate_core(
            tool_call,
            spec,
            context,
            policy_observer=policy_observer,
            consumption_observation=consumption_observation,
        )
        if (
            type(policy_observer) is RequestPolicyReceiptObserver
            and type(consumption_observation) is PolicyConsumptionObservation
        ):
            try:
                policy_observer.record_live_permission_result(
                    consumption_observation,
                    mode="not_reached",
                    context=context,
                )
                policy_observer.complete_policy_consumption(
                    consumption_observation,
                    outcome=outcome,
                )
            except Exception:
                pass
        return outcome

    async def _evaluate_core(
        self,
        tool_call: ToolCall,
        spec: ToolSpec,
        context: ToolPolicyContext,
        *,
        policy_observer: RequestPolicyReceiptObserver | None,
        consumption_observation: PolicyConsumptionObservation | None,
    ) -> ToolPolicyOutcome:
        """Evaluate one tool call and optionally record context consumers."""

        def consume(
            stage: str, phase: str, consumed: ToolPolicyContext
        ) -> None:
            if type(policy_observer) is not RequestPolicyReceiptObserver:
                return
            try:
                policy_observer.record_policy_context_consumption(
                    consumption_observation,
                    stage=stage,
                    phase=phase,
                    context=consumed,
                )
            except Exception:
                pass

        tool_input = dict(tool_call.arguments or {})
        consume("runtime_phase", "initial", context)
        if spec.name in runtime_blocked_tool_names(context.request_context):
            return _outcome(
                "deny",
                reason=(
                    f"Tool '{spec.name}' is unavailable in this runtime phase."
                ),
                risk="high",
                source="runtime_phase_tool_block",
            )
        consume("official_chrome", "initial", context)
        (
            _chrome_effect,
            official_chrome_decision,
        ) = _chrome_devtools_policy_decision(tool_call, spec, context)
        consume("workspace_access", "initial", context)
        if (
            context.workspace_access_mode == "read_only"
            and not spec.read_only
            and not (
                official_chrome_decision is not None
                and official_chrome_decision.decision == "forbidden"
            )
        ):
            return _outcome(
                "deny",
                reason=(
                    "The selected workspace is read-only; this tool may "
                    "change files or external state."
                ),
                risk="high",
                source="workspace_read_only",
            )
        official_guanlan_decision = None
        if spec.metadata.get("official_integration") == "guanlan":
            consume("official_guanlan", "initial", context)
            from ..app.mcp.guanlan_native import (
                official_guanlan_policy_decision,
            )

            official_guanlan_decision = official_guanlan_policy_decision(
                spec.name,
                tool_input,
                profile=context.permission_profile,
                permission_mode=context.permission_mode,
                distribution=context.distribution,
            )
        consume("collaboration_read_only", "initial", context)
        if (
            context.request_context.get("collaboration_read_only") is True
            and official_guanlan_decision is None
            and not (spec.read_only and spec.concurrency_safe)
        ):
            return _outcome(
                "deny",
                reason=(
                    "Parallel collaboration members are consultation-only; "
                    f"tool '{spec.name}' may mutate shared or external state."
                ),
                risk="high",
                source="collaboration_read_only",
            )
        consume("delegation_tool_scope", "initial", context)
        if context.request_context.get("execution_team_mode") == "scoped_execute":
            required_identity = (
                "delegation_root_run_id",
                "delegation_parent_run_id",
                "delegation_parent_attempt_id",
                "delegation_parent_effect_id",
                "delegation_task_id",
            )
            missing_identity = [
                name
                for name in required_identity
                if not isinstance(context.request_context.get(name), str)
                or not str(context.request_context.get(name)).strip()
            ]
            raw_attempt = context.request_context.get("delegation_attempt")
            if (
                isinstance(raw_attempt, bool)
                or not isinstance(raw_attempt, int)
                or raw_attempt < 1
            ):
                missing_identity.append("delegation_attempt")
            if missing_identity:
                return _outcome(
                    "deny",
                    reason=(
                        "Execution-team identity is incomplete: "
                        + ", ".join(missing_identity)
                    ),
                    risk="high",
                    source="delegation_identity_scope",
                )
            nested_tools = {
                "chat_with_agent",
                "check_agent_task",
                "collaborate_with_agents",
                "list_agents",
                "submit_to_agent",
            }
            if (
                context.request_context.get("delegation_nested_allowed")
                is not True
                and spec.name in nested_tools
            ):
                return _outcome(
                    "deny",
                    reason=(
                        "Execution-team members cannot create or control "
                        "nested agent work."
                    ),
                    risk="high",
                    source="delegation_nested_denied",
                )
            raw_allowed = context.request_context.get(
                "delegation_allowed_tools",
            )
            allowed_tools = (
                {
                    item.strip()
                    for item in raw_allowed
                    if isinstance(item, str) and item.strip()
                }
                if isinstance(raw_allowed, (list, tuple))
                else set()
            )
            if spec.name not in allowed_tools:
                return _outcome(
                    "deny",
                    reason=(
                        f"Tool '{spec.name}' is outside this execution "
                        "member's delegated tool scope."
                    ),
                    risk="high",
                    source="delegation_tool_scope",
                )
        (
            context,
            live_override_mode,
        ) = await self._context_with_live_session_permission(context)
        if type(policy_observer) is RequestPolicyReceiptObserver:
            try:
                policy_observer.record_live_permission_result(
                    consumption_observation,
                    mode=live_override_mode,
                    context=context,
                )
            except Exception:
                pass
        policy_source = "runtime_policy"
        try:
            if _runtime_owned_artifact_tool(spec):
                consume("runtime_owned_artifact", "post_override", context)
                policy_decision = _InternalAllowDecision(
                    reason=(
                        "Runtime-owned artifact contract bookkeeping does not "
                        "require a second user approval."
                    ),
                )
                policy_source = "runtime_owned_internal"
            else:
                consume("artifact_delivery", "post_override", context)
                if _effect_record_auto_delivery(tool_call, spec, context):
                    policy_decision = _InternalAllowDecision(
                        reason=(
                            "The Runtime is projecting one committed artifact "
                            "EffectRecord into its idempotent file card."
                        ),
                        command_category="artifact_delivery",
                        decision_source="artifact_effect",
                    )
                    policy_source = "artifact_effect_auto_delivery"
                else:
                    if (
                        official_chrome_decision is None
                        and official_guanlan_decision is None
                    ):
                        consume("builtin_policy", "post_override", context)
                    policy_decision = (
                        official_chrome_decision
                        or official_guanlan_decision
                        or self._evaluate_builtin_policy(
                            spec.name,
                            tool_input,
                            context,
                        )
                    )
                    if official_guanlan_decision is not None:
                        policy_source = "official_guanlan_allowlist"
                    elif official_chrome_decision is not None:
                        policy_source = "official_chrome_devtools"
        except Exception as exc:
            logger.warning(
                "Runtime tool policy failed closed for %s",
                spec.name,
                exc_info=True,
            )
            return _engine_error_outcome(
                reason=(
                    "Tool policy evaluation failed closed before execution."
                ),
                detail=str(exc),
                policy_payload=True,
            )
        policy_payload = _json_safe(policy_decision)
        policy_action = str(getattr(policy_decision, "decision", "allow"))
        policy_reason = str(getattr(policy_decision, "reason", "") or "")
        policy_risk = str(getattr(policy_decision, "risk", "low") or "low")
        approval_copy = getattr(
            policy_decision,
            "approval_copy",
            None,
        ) or getattr(policy_decision, "suggested_approval_message", None)

        if policy_action == "forbidden":
            return _outcome(
                "deny",
                reason=policy_reason,
                risk=policy_risk,
                policy_payload=policy_payload,
                approval_copy=approval_copy,
                source="runtime_policy",
            )

        consume("full_access", "post_override", context)
        full_access = _full_access_enabled(context, policy_payload)
        remembered_policy = None
        if policy_action != "forbidden" and not full_access:
            consume("remembered_approval", "post_override", context)
            remembered_policy = await self._remembered_policy_approval(
                tool_name=spec.name,
                tool_input=tool_input,
                policy_decision=policy_decision,
                context=context,
            )
        if policy_action == "prompt" and not full_access:
            if remembered_policy is None and _has_approval_route(context):
                return _outcome(
                    "needs_approval",
                    reason=policy_reason,
                    risk=policy_risk,
                    policy_payload=policy_payload,
                    approval_copy=approval_copy,
                    source="runtime_policy",
                )
            if remembered_policy is None:
                return _outcome(
                    "deny",
                    reason=(
                        "Tool policy requires approval, but no approval route "
                        "is available."
                    ),
                    risk="critical" if policy_risk == "critical" else "high",
                    policy_payload=policy_payload,
                    approval_copy=approval_copy,
                    source="approval_unavailable",
                )

        consume("tool_guard", "post_override", context)
        guard_outcome = self._evaluate_tool_guard(
            spec.name,
            tool_input,
            context,
        )
        if guard_outcome is not None:
            guard_outcome.policy_payload = policy_payload
            if remembered_policy is not None:
                guard_outcome.remembered_approval = remembered_policy
            if full_access and guard_outcome.action == "needs_approval":
                return _outcome(
                    "allow",
                    reason=(
                        "Complete access suppresses repeated ToolGuard "
                        "approval prompts; hard-deny rules remain active."
                    ),
                    risk=policy_risk,
                    policy_payload=policy_payload,
                    guard_payload=guard_outcome.guard_payload,
                    source="full_access",
                )
            if (
                remembered_policy is not None
                and guard_outcome.action == "needs_approval"
            ):
                return _outcome(
                    "allow",
                    reason=(
                        "The current root session already approved tool "
                        "execution; hard-deny rules remain active."
                    ),
                    risk=policy_risk,
                    policy_payload=policy_payload,
                    guard_payload=guard_outcome.guard_payload,
                    remembered_approval=remembered_policy,
                    source="remembered_approval",
                )
            return guard_outcome

        return _outcome(
            "allow",
            reason=(
                "Matched remembered approval."
                if remembered_policy is not None
                else policy_reason or "Tool policy allowed this call."
            ),
            risk=policy_risk,
            policy_payload=policy_payload,
            remembered_approval=remembered_policy,
            source=(
                "remembered_approval"
                if remembered_policy is not None
                else policy_source
            ),
        )

    @staticmethod
    def _evaluate_builtin_policy(
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPolicyContext,
    ) -> Any:
        from lantai.security.tool_policy import (
            ToolExecutionContext,
            evaluate_tool_policy,
        )

        policy_context = ToolExecutionContext(
            agent_id=context.agent_id,
            chat_id=context.chat_id,
            turn_id=context.turn_id or context.session_id,
            workspace_dir=context.workspace_dir,
            permission_profile=context.permission_profile or "workspace",
            permission_mode=context.permission_mode,
            distribution=context.distribution,
        )
        return evaluate_tool_policy(
            tool_name,
            tool_input,
            context=policy_context,
        )

    async def _remembered_policy_approval(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
        policy_decision: Any,
        context: ToolPolicyContext,
    ) -> dict[str, Any] | None:
        workspace_dir = str(context.workspace_dir or "").strip()
        root_session_id = str(
            context.root_session_id or context.session_id or "",
        ).strip()

        try:
            if root_session_id and self._approval_service is not None:
                session_match = (
                    await self._approval_service.session_approval_matches(
                        root_session_id=root_session_id,
                        user_id=str(
                            context.request_context.get("user_id") or "",
                        ).strip(),
                        workspace_dir=workspace_dir,
                        tool_name=tool_name,
                        tool_input=tool_input,
                        policy_decision=policy_decision,
                        source_agent_id=str(context.agent_id or ""),
                    )
                )
                if session_match:
                    session_match = dict(session_match)
                    session_match["decision_scope"] = "session"
                    return session_match

            if not workspace_dir:
                return None

            from lantai.security.tool_permission_memory import (
                workspace_approval_matches,
            )

            workspace_match = workspace_approval_matches(
                workspace_dir=workspace_dir,
                tool_name=tool_name,
                tool_input=tool_input,
                policy_decision=policy_decision,
                source_agent_id=str(context.agent_id or ""),
                workspace_grant_id=str(context.workspace_grant_id or ""),
            )
            if workspace_match is None:
                return None
            payload = workspace_match.model_dump(mode="json")
            payload["decision_scope"] = "workspace"
            return payload
        except Exception:
            logger.debug(
                "Failed to evaluate remembered tool approval",
                exc_info=True,
            )
            return None

    @staticmethod
    def _evaluate_tool_guard(
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPolicyContext,
    ) -> ToolPolicyOutcome | None:
        try:
            from lantai.security.tool_guard.engine import get_guard_engine
            from lantai.security.tool_guard.execution_level import (
                ToolExecutionLevel,
            )
            from lantai.security.tool_guard.models import GuardSeverity
        except Exception:
            logger.warning(
                "Failed to import tool guard policy modules",
                exc_info=True,
            )
            if _is_sensitive_tool_name(tool_name):
                return _engine_error_outcome(
                    reason=(
                        "Tool guard policy engine failed closed before "
                        "execution."
                    ),
                    guard_payload=True,
                )
            return None

        try:
            engine = get_guard_engine()
            if not tool_name or not getattr(engine, "enabled", False):
                return None

            exec_level = ToolExecutionLevel.from_config(context.approval_level)
            if exec_level.is_disabled():
                return None

            if engine.is_denied(tool_name):
                guard_result = engine.guard(tool_name, tool_input)
                return _outcome(
                    "deny",
                    reason=f"Tool '{tool_name}' is denied by ToolGuard.",
                    risk="critical",
                    guard_payload=_json_safe(guard_result),
                    source="tool_guard",
                )

            if exec_level.requires_approval_for_all_tools():
                if not _has_approval_route(context):
                    return _outcome(
                        "deny",
                        reason=(
                            "Tool guard requires approval, but no approval "
                            "route is available."
                        ),
                        risk="high",
                        source="approval_unavailable",
                    )
                guard_result = engine.guard(
                    tool_name,
                    tool_input,
                    only_always_run=False,
                )
                if guard_result is not None and getattr(
                    guard_result,
                    "guardians_failed",
                    None,
                ):
                    return _engine_error_outcome(
                        reason=(
                            "Tool guard policy engine failed closed before "
                            "execution."
                        ),
                        detail=str(guard_result.guardians_failed),
                        guard_payload=True,
                    )
                if (
                    guard_result is not None
                    and engine.should_auto_deny_result(
                        guard_result,
                    )
                ):
                    return _outcome(
                        "deny",
                        reason=(
                            f"Tool '{tool_name}' matched auto-deny guard "
                            "rules."
                        ),
                        risk="critical",
                        guard_payload=_json_safe(guard_result),
                        source="tool_guard",
                    )
                return _outcome(
                    "needs_approval",
                    reason=(
                        f"Tool '{tool_name}' requires approval in STRICT mode."
                    ),
                    risk="medium",
                    guard_payload=_json_safe(guard_result),
                    approval_copy=f"Approve or deny tool call: {tool_name}",
                    source="tool_guard",
                )

            guarded = engine.is_guarded(tool_name)
            guard_result = engine.guard(
                tool_name,
                tool_input,
                only_always_run=not guarded,
            )
            if guard_result is not None and getattr(
                guard_result,
                "guardians_failed",
                None,
            ):
                return _engine_error_outcome(
                    reason=(
                        "Tool guard policy engine failed closed before "
                        "execution."
                    ),
                    detail=str(guard_result.guardians_failed),
                    guard_payload=True,
                )
            if guard_result is None or not getattr(
                guard_result,
                "findings",
                None,
            ):
                return None

            if engine.should_auto_deny_result(guard_result):
                return _outcome(
                    "deny",
                    reason=(
                        f"Tool '{tool_name}' matched auto-deny guard rules."
                    ),
                    risk="critical",
                    guard_payload=_json_safe(guard_result),
                    source="tool_guard",
                )

            if exec_level.is_smart_mode():
                max_severity = getattr(guard_result, "max_severity", None)
                if max_severity in (GuardSeverity.INFO, GuardSeverity.LOW):
                    return None

            if _has_approval_route(context):
                return _outcome(
                    "needs_approval",
                    reason=f"Tool '{tool_name}' requires approval.",
                    risk="high",
                    guard_payload=_json_safe(guard_result),
                    approval_copy=f"Approve or deny tool call: {tool_name}",
                    source="tool_guard",
                )

            return _outcome(
                "deny",
                reason=(
                    "Tool guard requires approval, but no approval route is "
                    "available."
                ),
                risk="high",
                guard_payload=_json_safe(guard_result),
                source="approval_unavailable",
            )
        except Exception as exc:
            logger.warning(
                "Runtime tool guard failed closed for %s",
                tool_name,
                exc_info=True,
            )
            if _is_sensitive_tool_name(tool_name):
                return _engine_error_outcome(
                    reason=(
                        "Tool guard policy engine failed closed before "
                        "execution."
                    ),
                    detail=str(exc),
                    guard_payload=True,
                )
            return None
