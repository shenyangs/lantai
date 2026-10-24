# -*- coding: utf-8 -*-
"""Model context capabilities and their conservative effective window.

The legacy ``running.max_input_length`` is a user request, not proof that a
provider, gateway, or local runtime accepts that many tokens.  This module is
dependency-free so providers, local runtimes, the planner, and the UI can all
use the same contract without a circular import.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


Confidence = Literal["verified", "reported", "configured", "unverified"]
ContextMode = Literal["smart", "max", "economy"]

UNKNOWN_MODEL_SAFE_WINDOW = 256 * 1024


@dataclass(frozen=True)
class ContextPolicyDecision:
    """One inspectable input/output budget for an exact model route."""

    mode: ContextMode
    hard_window: int
    usable_input_tokens: int
    target_input_tokens: int
    output_reserve: int
    safety_margin: int
    used_unknown_fallback: bool = False


@dataclass(frozen=True)
class ContextLimit:
    """One independently observed context limit."""

    tokens: int | None
    source: str
    confidence: Confidence = "unverified"
    hard_limit: bool = True

    @property
    def known(self) -> bool:
        return isinstance(self.tokens, int) and self.tokens > 0


@dataclass(frozen=True)
class ModelContextProfile:
    """Capabilities for the exact provider/model/route/runtime in use."""

    model_id: str = ""
    provider_id: str = ""
    route_id: str = ""
    user_window: ContextLimit = field(
        default_factory=lambda: ContextLimit(None, "agent-config", "configured"),
    )
    provider_window: ContextLimit = field(
        default_factory=lambda: ContextLimit(None, "provider", "unverified"),
    )
    route_window: ContextLimit = field(
        default_factory=lambda: ContextLimit(None, "route", "unverified"),
    )
    local_runtime_window: ContextLimit = field(
        default_factory=lambda: ContextLimit(None, "local-runtime", "unverified"),
    )
    provider_max_output: ContextLimit = field(
        default_factory=lambda: ContextLimit(None, "provider", "unverified"),
    )
    recommended_output_reserve: ContextLimit = field(
        default_factory=lambda: ContextLimit(None, "policy", "unverified"),
    )
    tokenizer: str | None = None
    tokenizer_source: str = "unverified"

    @property
    def known_hard_windows(self) -> tuple[ContextLimit, ...]:
        return tuple(
            limit
            for limit in (
                self.user_window,
                self.provider_window,
                self.route_window,
                self.local_runtime_window,
            )
            if limit.hard_limit and limit.known
        )

    @property
    def effective_window(self) -> int | None:
        """Return the safest usable hard cap for the exact model route.

        The legacy Agent value is only a user ceiling.  When no provider,
        route, or local runtime capability is known, a deliberately small
        product fallback prevents an arbitrary user ceiling from being
        advertised or sent as if it were a verified model limit.
        """

        capability_limits = tuple(
            limit
            for limit in (
                self.provider_window,
                self.route_window,
                self.local_runtime_window,
            )
            if limit.hard_limit and limit.known
        )
        user_ceiling = (
            int(self.user_window.tokens)
            if self.user_window.known
            else None
        )
        if capability_limits:
            limits = [int(limit.tokens) for limit in capability_limits]
            if user_ceiling is not None:
                limits.append(user_ceiling)
            return min(limits)
        if user_ceiling is not None:
            return min(user_ceiling, UNKNOWN_MODEL_SAFE_WINDOW)
        return UNKNOWN_MODEL_SAFE_WINDOW

    @property
    def is_verified(self) -> bool:
        """Whether an authoritative exact-route capability bound is known."""

        capabilities = tuple(
            limit
            for limit in (
                self.provider_window,
                self.route_window,
                self.local_runtime_window,
            )
            if limit.known
        )
        if not capabilities:
            return False
        tightest_capability = min(
            int(limit.tokens) for limit in capabilities
        )
        return any(
            int(limit.tokens) == tightest_capability
            and limit.confidence == "verified"
            for limit in capabilities
        )

    @property
    def status(self) -> str:
        return "verified" if self.is_verified else "unverified"

    def output_reserve(self, requested: int) -> int:
        """Clamp output to a verified/reported provider maximum when known."""

        requested = max(0, int(requested))
        if self.provider_max_output.known:
            return min(requested, int(self.provider_max_output.tokens))
        return requested

    def policy(self, mode: ContextMode = "smart") -> ContextPolicyDecision:
        """Resolve a safe default policy without treating maximum as optimal."""

        hard_window = int(self.effective_window or UNKNOWN_MODEL_SAFE_WINDOW)
        used_unknown_fallback = not any(
            limit.known
            for limit in (
                self.provider_window,
                self.route_window,
                self.local_runtime_window,
            )
        )
        if self.recommended_output_reserve.known:
            requested_output_reserve = int(
                self.recommended_output_reserve.tokens,
            )
        elif self.provider_max_output.known:
            requested_output_reserve = int(self.provider_max_output.tokens)
        else:
            requested_output_reserve = (
                max(8 * 1024, min(32 * 1024, int(hard_window * 0.15)))
                if hard_window >= 32 * 1024
                else max(32, int(hard_window * 0.15))
            )
        output_reserve = min(hard_window // 2, requested_output_reserve)
        safety_margin = (
            max(2048, int(hard_window * 0.03))
            if hard_window >= 8192
            else max(16, int(hard_window * 0.03))
        )
        usable = max(0, hard_window - output_reserve - safety_margin)
        target_ratio = {
            "smart": 0.80,
            "max": 1.0,
            "economy": 0.55,
        }[mode]
        return ContextPolicyDecision(
            mode=mode,
            hard_window=hard_window,
            usable_input_tokens=usable,
            target_input_tokens=max(0, int(usable * target_ratio)),
            output_reserve=output_reserve,
            safety_margin=safety_margin,
            used_unknown_fallback=used_unknown_fallback,
        )

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["effective_window"] = self.effective_window
        payload["status"] = self.status
        return payload

    @classmethod
    def from_mapping(
        cls,
        *,
        model_id: str = "",
        provider_id: str = "",
        route_id: str = "",
        user_window: int | None = None,
        user_window_source: str = "agent-config",
        provider_metadata: dict[str, Any] | None = None,
        route_metadata: dict[str, Any] | None = None,
        local_runtime_window: int | None = None,
        local_runtime_verified: bool = False,
    ) -> "ModelContextProfile":
        """Build a profile from legacy provider metadata without guessing.

        Recognises common gateway spellings but marks all metadata as reported
        until a provider response or explicit probe verifies it.
        """

        provider_metadata = provider_metadata or {}
        route_metadata = route_metadata or {}

        def limit_from(
            metadata: dict[str, Any],
            source: str,
            *keys: str,
        ) -> ContextLimit:
            for key in keys:
                value = metadata.get(key)
                try:
                    tokens = int(value)
                except (TypeError, ValueError):
                    continue
                if tokens > 0:
                    explicit_source = str(
                        metadata.get(f"_{source}_source")
                        or metadata.get("_context_source")
                        or metadata.get("capability_source")
                        or f"{source}:{key}"
                    )
                    explicit_confidence = str(
                        metadata.get(f"_{source}_confidence")
                        or metadata.get("_context_confidence")
                        or (
                            "configured"
                            if metadata.get("capability_source")
                            == "user-configured"
                            else ""
                        )
                        or "reported"
                    )
                    if explicit_confidence not in {
                        "verified",
                        "reported",
                        "configured",
                        "unverified",
                    }:
                        explicit_confidence = "reported"
                    return ContextLimit(
                        tokens,
                        explicit_source,
                        explicit_confidence,  # type: ignore[arg-type]
                    )
            return ContextLimit(None, source, "unverified")

        return cls(
            model_id=model_id,
            provider_id=provider_id,
            route_id=route_id,
            user_window=ContextLimit(
                user_window,
                user_window_source,
                "configured",
            ),
            provider_window=limit_from(
                provider_metadata,
                "provider",
                "context_window",
                "context_length",
                "max_context_length",
            ),
            route_window=limit_from(
                route_metadata,
                "route",
                "context_window",
                "context_length",
                "max_context_length",
            ),
            local_runtime_window=ContextLimit(
                local_runtime_window,
                "local-runtime",
                "verified" if local_runtime_verified else "configured",
            ),
            provider_max_output=limit_from(
                provider_metadata,
                "provider",
                "max_output_tokens",
                "max_completion_tokens",
                "max_tokens",
            ),
            recommended_output_reserve=limit_from(
                provider_metadata,
                "policy",
                "recommended_output_reserve",
            ),
            tokenizer=str(provider_metadata.get("tokenizer") or "") or None,
            tokenizer_source=str(provider_metadata.get("tokenizer_source") or "unverified"),
        )

    @classmethod
    def from_model_info(
        cls,
        model_info: Any,
        *,
        user_window: int | None,
        provider_id: str = "",
        route_metadata: dict[str, Any] | None = None,
        local_runtime_window: int | None = None,
    ) -> "ModelContextProfile":
        """Compatibility facade for provider ``ModelInfo`` objects/mappings."""

        if hasattr(model_info, "model_dump"):
            metadata = model_info.model_dump()
        elif isinstance(model_info, dict):
            metadata = model_info
        else:
            metadata = {
                key: getattr(model_info, key, None)
                for key in (
                    "id",
                    "context_window",
                    "user_context_window",
                    "max_output_tokens",
                    "tokenizer",
                )
            }
        model_user_window = metadata.pop("user_context_window", None)
        try:
            model_user_window = int(model_user_window)
        except (TypeError, ValueError):
            model_user_window = None
        if model_user_window is not None and model_user_window < 1000:
            model_user_window = None
        return cls.from_mapping(
            model_id=str(metadata.get("id") or ""), provider_id=provider_id,
            user_window=(
                model_user_window
                if model_user_window is not None
                else user_window
            ),
            user_window_source=(
                "model-user-override"
                if model_user_window is not None
                else "agent-config"
            ),
            provider_metadata=metadata,
            route_metadata=route_metadata,
            local_runtime_window=local_runtime_window,
        )

    @classmethod
    def from_provider_selection(
        cls,
        *,
        provider_manager: Any,
        provider_id: str,
        model_id: str,
        user_window: int | None,
        route_metadata: dict[str, Any] | None = None,
        local_runtime_window: int | None = None,
    ) -> "ModelContextProfile":
        """Resolve the current ProviderManager selection without guessing.

        Provider-level metadata supplies a fallback for custom gateways while
        a matching ``ModelInfo`` wins for model-specific limits.  Callers use
        this at request time, so a provider/model switch naturally creates a
        new profile rather than retaining stale capability data.
        """

        provider = None
        try:
            provider = provider_manager.get_provider(provider_id)
        except Exception:
            provider = None

        metadata: dict[str, Any] = {}
        try:
            from .providers.model_capability_registry import (
                resolve_model_capability,
            )

            registered = resolve_model_capability(provider_id, model_id)
            if registered is not None:
                metadata.update(registered.as_metadata())
        except Exception:
            # The registry is a product hint, never a runtime dependency.
            pass
        raw_meta = getattr(provider, "meta", None)
        if isinstance(raw_meta, dict):
            metadata.update(raw_meta)
        matched = None
        for candidate in [
            *(getattr(provider, "models", ()) or ()),
            *(getattr(provider, "extra_models", ()) or ()),
        ]:
            if str(getattr(candidate, "id", "") or "") == model_id:
                matched = candidate
                break
        if matched is not None:
            if hasattr(matched, "model_dump"):
                model_metadata = matched.model_dump()
            elif isinstance(matched, dict):
                model_metadata = matched
            else:
                model_metadata = {
                    key: getattr(matched, key, None)
                    for key in (
                        "id",
                        "context_window",
                        "user_context_window",
                        "max_output_tokens",
                        "tokenizer",
                    )
                }
            # ``None`` is the ModelInfo default and must not erase a useful
            # explicit provider/gateway fallback.
            metadata.update(
                {key: value for key, value in model_metadata.items() if value is not None},
            )

        model_user_window = metadata.pop("user_context_window", None)
        try:
            model_user_window = int(model_user_window)
        except (TypeError, ValueError):
            model_user_window = None
        if model_user_window is not None and model_user_window < 1000:
            model_user_window = None

        return cls.from_mapping(
            model_id=model_id,
            provider_id=provider_id,
            user_window=(
                model_user_window
                if model_user_window is not None
                else user_window
            ),
            user_window_source=(
                "model-user-override"
                if model_user_window is not None
                else "agent-config"
            ),
            provider_metadata=metadata,
            route_metadata=route_metadata,
            local_runtime_window=local_runtime_window,
            local_runtime_verified=local_runtime_window is not None,
        )
