# -*- coding: utf-8 -*-
"""Provider-neutral policy for reasoning state across tool continuations.

Reasoning capability answers whether a model can think.  This module answers
the different question that matters inside an agent loop: which opaque state
must survive a tool transaction, and whether thinking may be switched for the
next request without invalidating that transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .reasoning import ReasoningCapability, get_reasoning_capability
from .request_profiles import is_kimi_coding_base_url

ReasoningContinuationProtocol = Literal[
    "none",
    "openai_reasoning_content",
    "anthropic_thinking_blocks",
    "gemini_thought_signature",
    "openai_reasoning_items",
]


@dataclass(frozen=True)
class ReasoningContinuationPolicy:
    """Stable wire-continuation contract for one provider/model endpoint."""

    protocol: ReasoningContinuationProtocol = "none"
    requires_exact_replay_with_tools: bool = False
    opaque_state: bool = False
    switchable: bool = False
    required_tool_choice_incompatible: bool = False
    bounded_blank_repair: bool = False


@dataclass(frozen=True)
class ReasoningTurnPlan:
    """The requested and effective reasoning state for one provider call."""

    policy: ReasoningContinuationPolicy
    requested_enabled: bool
    effective_enabled: bool
    forced_disabled_reason: str = ""
    effective_tool_choice: str | None = None
    tool_choice_adjustment_reason: str = ""

    def status_metadata(self, *, returned: bool) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "requested": self.requested_enabled,
            "effective_enabled": self.effective_enabled,
            "returned": returned,
            "protocol": self.policy.protocol,
            "forced_disabled_reason": self.forced_disabled_reason,
        }
        if self.tool_choice_adjustment_reason:
            metadata.update(
                effective_tool_choice=self.effective_tool_choice,
                tool_choice_adjustment_reason=self.tool_choice_adjustment_reason,
            )
        return metadata


def _normalized(value: str) -> str:
    return str(value or "").strip().lower()


def resolve_reasoning_continuation_policy(
    provider_id: str,
    model_id: str,
    *,
    base_url: str = "",
    chat_model: str = "",
) -> ReasoningContinuationPolicy:
    """Resolve the provider's reasoning-history wire contract.

    The result is intentionally independent from UI reasoning visibility and
    from request effort.  It describes only state that affects a later tool
    continuation.
    """

    provider = _normalized(provider_id)
    model = _normalized(model_id)
    base = _normalized(base_url)
    chat_model_name = _normalized(chat_model)
    capability = get_reasoning_capability(
        provider_id,
        model_id,
        base_url=base_url,
        chat_model=chat_model,
    )

    if provider == "gemini" or model.startswith("gemini-"):
        return ReasoningContinuationPolicy(
            protocol="gemini_thought_signature",
            requires_exact_replay_with_tools=True,
            opaque_state=True,
            switchable=not capability.fixed,
        )

    if (
        provider == "anthropic"
        or "anthropic" in chat_model_name
        or model.startswith("claude-")
    ):
        return ReasoningContinuationPolicy(
            protocol="anthropic_thinking_blocks",
            requires_exact_replay_with_tools=True,
            opaque_state=True,
            switchable=not capability.fixed,
            required_tool_choice_incompatible=not capability.fixed,
        )

    if (
        "deepseek" in model
        or "deepseek" in provider
        or "kimi" in model
        or "kimi" in provider
        or "api.kimi.com" in base
        or is_kimi_coding_base_url(base_url)
    ):
        return ReasoningContinuationPolicy(
            protocol="openai_reasoning_content",
            requires_exact_replay_with_tools=True,
            opaque_state=False,
            switchable=not capability.fixed,
            required_tool_choice_incompatible=(
                not capability.fixed
                or "kimi-k2.7-code" in model
                or "kimi-k2-7-code" in model
            ),
            bounded_blank_repair=True,
        )

    # Reasoning items are an OpenAI Responses API continuation primitive. The
    # current OpenAI-compatible client uses Chat Completions, which does not
    # expose those opaque items for replay. Do not claim a wire guarantee that
    # the active transport cannot uphold; a future Responses adapter can opt in
    # explicitly through its chat-model identity.
    if (
        provider in {"openai", "azure-openai"}
        and capability.supported
        and "responses" in chat_model_name
    ):
        return ReasoningContinuationPolicy(
            protocol="openai_reasoning_items",
            requires_exact_replay_with_tools=True,
            opaque_state=True,
            switchable=not capability.fixed,
        )

    if capability.mode in {
        "openai_extra_thinking",
        "deepseek_thinking",
        "minimax_adaptive_thinking",
    }:
        return ReasoningContinuationPolicy(
            protocol="openai_reasoning_content",
            requires_exact_replay_with_tools=True,
            switchable=not capability.fixed,
            required_tool_choice_incompatible=not capability.fixed,
            bounded_blank_repair=True,
        )

    return ReasoningContinuationPolicy()


def _message_has_tool_call(message: Any) -> bool:
    if _normalized(getattr(message, "role", "")) != "assistant":
        return False
    for block in getattr(message, "content", None) or ():
        if _normalized(getattr(block, "type", "")) == "tool_call":
            return True
    return False


def _disabled_reasoning_tool_turn_in_history(messages: list[Any]) -> bool:
    """Return whether the latest assistant tool turn ran with thinking off."""

    for message in reversed(messages):
        if not _message_has_tool_call(message):
            continue
        metadata = getattr(message, "metadata", None)
        if not isinstance(metadata, dict):
            return False
        status = metadata.get("reasoning_status")
        if not isinstance(status, dict):
            return False
        return status.get("effective_enabled") is False
    return False


def plan_reasoning_turn(
    *,
    capability: ReasoningCapability,
    policy: ReasoningContinuationPolicy,
    requested_enabled: bool,
    tool_choice: str | None,
    has_tools: bool,
    messages: list[Any],
) -> ReasoningTurnPlan:
    """Plan one call without switching state inside an open tool chain."""

    requested = bool(requested_enabled and capability.supported)
    effective = bool(requested or (capability.fixed and capability.supported))
    reason = ""
    if (
        effective
        and policy.switchable
        and policy.required_tool_choice_incompatible
        and tool_choice == "required"
    ):
        effective = False
        reason = "required_tool_choice"
    elif (
        effective
        and policy.switchable
        and policy.requires_exact_replay_with_tools
        and has_tools
        and _disabled_reasoning_tool_turn_in_history(messages)
    ):
        effective = False
        reason = "continue_disabled_tool_chain"
    effective_tool_choice = compatible_fixed_reasoning_tool_choice(
        capability=capability,
        policy=policy,
        tool_choice=tool_choice,
        has_tools=has_tools,
    )
    return ReasoningTurnPlan(
        policy=policy,
        requested_enabled=requested,
        effective_enabled=effective,
        forced_disabled_reason=reason,
        effective_tool_choice=effective_tool_choice,
        tool_choice_adjustment_reason=(
            "fixed_reasoning_required_tool_choice"
            if effective_tool_choice != tool_choice
            else ""
        ),
    )


def compatible_fixed_reasoning_tool_choice(
    *,
    capability: ReasoningCapability,
    policy: ReasoningContinuationPolicy,
    tool_choice: str | None,
    has_tools: bool,
) -> str | None:
    """Adapt only the wire choice; the Runtime's required-tool contract stays intact.

    Kimi K2.7 Code rejects forced tool choices in its fixed thinking mode. It
    cannot use the switchable-model workaround of disabling thinking. Callers
    keep the available tool subset and enforce completion in their agent loop.
    """

    if (
        has_tools
        and capability.supported
        and capability.fixed
        and policy.required_tool_choice_incompatible
        and tool_choice == "required"
    ):
        return "auto"
    return tool_choice


def is_missing_reasoning_content_error(exc: Exception) -> bool:
    """Recognize the strict OpenAI-compatible continuation 400."""

    return bool(
        getattr(exc, "status_code", None) == 400
        and "reasoning_content" in str(exc).lower()
    )


def repair_missing_reasoning_content(messages: Any) -> bool:
    """Apply one compatibility-only blank replay to formatted messages.

    This is not the primary continuity path.  It exists for old histories and
    strict gateways that return the explicit missing-field error.  Callers
    must bound it to one retry before any stream event is exposed.
    """

    if not isinstance(messages, list):
        return False
    modified = False
    for message in messages:
        if (
            isinstance(message, dict)
            and message.get("role") == "assistant"
            and "reasoning_content" not in message
        ):
            message["reasoning_content"] = " "
            modified = True
    return modified


def project_disabled_tool_turn_reasoning_content(
    source_messages: list[Any],
    formatted_messages: list[dict[str, Any]],
) -> bool:
    """Represent an explicitly non-thinking tool turn on a thinking request.

    Strict DeepSeek-compatible endpoints require ``reasoning_content`` on
    historical assistant tool calls whenever the current request uses
    thinking mode. A tool turn that intentionally ran with thinking disabled
    has no hidden reasoning to replay, so one blank wire value is the complete
    representation of that state. This proactive projection is allowed only
    when canonical metadata proves thinking was disabled. Old history without
    that evidence remains covered by the bounded 400 repair.
    """

    disabled_call_ids: set[str] = set()
    for message in source_messages:
        if not _message_has_tool_call(message):
            continue
        metadata = getattr(message, "metadata", None)
        status = (
            metadata.get("reasoning_status")
            if isinstance(metadata, dict)
            else None
        )
        if (
            not isinstance(status, dict)
            or status.get("effective_enabled") is not False
        ):
            continue
        for block in getattr(message, "content", None) or ():
            if _normalized(getattr(block, "type", "")) != "tool_call":
                continue
            data = getattr(block, "data", None)
            if not isinstance(data, dict):
                continue
            call_id = str(data.get("id") or "").strip()
            if call_id:
                disabled_call_ids.add(call_id)

    modified = False
    for message in formatted_messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        call_ids = {
            str(tool_call.get("id") or "").strip()
            for tool_call in tool_calls
            if isinstance(tool_call, dict)
        }
        if (
            disabled_call_ids.intersection(call_ids)
            and "reasoning_content" not in message
        ):
            message["reasoning_content"] = " "
            modified = True
    return modified


def repair_missing_reasoning_content_call(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> bool:
    """Legacy call-shape adapter for :func:`repair_missing_reasoning_content`."""

    messages = kwargs.get("messages")
    if messages is None and args and isinstance(args[0], list):
        messages = args[0]
    return repair_missing_reasoning_content(messages)


__all__ = [
    "ReasoningContinuationPolicy",
    "ReasoningTurnPlan",
    "is_missing_reasoning_content_error",
    "plan_reasoning_turn",
    "project_disabled_tool_turn_reasoning_content",
    "repair_missing_reasoning_content",
    "repair_missing_reasoning_content_call",
    "resolve_reasoning_continuation_policy",
]
