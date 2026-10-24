# -*- coding: utf-8 -*-
"""Conservative, inspectable context accounting for a model invocation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Literal, Protocol

from .model_context_profile import ContextLimit, ModelContextProfile


class Tokenizer(Protocol):
    """Optional provider/model tokenizer adapter."""

    def count_tokens(self, text: str) -> int: ...


@dataclass(frozen=True)
class TokenEstimate:
    estimated: int
    upper_bound: int
    method: Literal["exact", "estimated"]
    uncertainty: int = 0


class ModelAwareTokenCounter:
    """Exact when possible; conservative upper bound otherwise.

    ``observe_usage`` calibrates a multiplicative error factor from real
    provider prompt usage.  The factor is monotonic, preventing a noisy low
    observation from making the next request less safe.
    """

    def __init__(
        self,
        tokenizer: Tokenizer | None = None,
        estimate_divisor: float = 4.0,
        initial_error_ratio: float = 0.20,
    ) -> None:
        if estimate_divisor <= 0:
            raise ValueError("estimate_divisor must be positive")
        self.tokenizer = tokenizer
        self.estimate_divisor = estimate_divisor
        self.initial_error_ratio = max(0.0, initial_error_ratio)
        self._error_ratio = self.initial_error_ratio

    def count(self, value: Any) -> TokenEstimate:
        text = self._serialize(value)
        if not text:
            return TokenEstimate(0, 0, "exact", 0)
        if self.tokenizer is not None:
            try:
                exact = max(0, int(self.tokenizer.count_tokens(text)))
                return TokenEstimate(exact, exact, "exact", 0)
            except Exception:
                # Provider tokenizers are optional adapters; planner safety
                # must not depend on them being alive.
                pass
        estimated = max(1, int(len(text.encode("utf-8")) / self.estimate_divisor + 0.5))
        upper = max(estimated, int(estimated * (1 + self._error_ratio) + 0.999))
        return TokenEstimate(estimated, upper, "estimated", upper - estimated)

    def observe_usage(self, *, serialized_payload: Any, actual_prompt_tokens: int) -> None:
        """Increase the safety factor from provider-reported prompt usage."""

        actual = max(0, int(actual_prompt_tokens))
        estimate = self.count(serialized_payload).estimated
        if estimate:
            self._error_ratio = max(
                self._error_ratio,
                max(0.0, actual / estimate - 1),
            )

    @staticmethod
    def _serialize(value: Any) -> str:
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return str(value)


ContextCategory = Literal[
    "system",
    "tool_schema",
    "request_envelope",
    "current_message",
    "attachment",
    "history",
    "summary",
    "stable_memory",
    "dynamic_memory",
    "wiki_evidence",
    "tool_result",
]


@dataclass(frozen=True)
class ContextCandidate:
    category: ContextCategory
    content: Any
    required: bool = False
    relevance: float = 0.0
    trust: float = 1.0
    recency: float = 0.0
    diversity_key: str = ""
    citation: str = ""
    identifier: str = ""


@dataclass
class ContextManifest:
    requested_window: int | None
    effective_window: int | None
    actual_window: int | None
    context_mode: str
    usable_input_budget: int
    target_input_budget: int
    output_reserve: int
    safety_margin: int
    context_profile_status: str
    used_unknown_fallback: bool
    categories: dict[str, TokenEstimate] = field(default_factory=dict)
    selected: list[str] = field(default_factory=list)
    discarded: dict[str, str] = field(default_factory=dict)
    payload_hash: str = ""

    @property
    def input_upper_bound(self) -> int:
        return sum(item.upper_bound for item in self.categories.values())

    @property
    def total_upper_bound(self) -> int:
        return self.input_upper_bound + self.output_reserve + self.safety_margin

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["input_upper_bound"] = self.input_upper_bound
        result["total_upper_bound"] = self.total_upper_bound
        return result


class ContextBudgetPlanner:
    """Select optional context under a model-specific token budget."""

    def __init__(self, counter: ModelAwareTokenCounter) -> None:
        self.counter = counter

    def plan(
        self,
        *,
        profile: ModelContextProfile,
        requested_output_tokens: int,
        required: Iterable[ContextCandidate],
        optional: Iterable[ContextCandidate] = (),
        actual_window: int | None = None,
        unknown_serialization_tokens: int | None = None,
        next_turn_reserve_tokens: int | None = None,
        context_mode: str = "smart",
    ) -> tuple[list[ContextCandidate], ContextManifest]:
        """Return selected blocks and a manifest that proves budget safety."""

        required_blocks = list(required)
        optional_blocks = list(optional)
        decision = profile.policy(context_mode)  # type: ignore[arg-type]
        effective = decision.hard_window
        explicitly_requested_output = profile.output_reserve(
            requested_output_tokens,
        )
        output = min(
            max(0, effective // 2),
            max(decision.output_reserve, explicitly_requested_output),
        )
        if (
            unknown_serialization_tokens is None
            and next_turn_reserve_tokens is None
        ):
            margin = decision.safety_margin
        else:
            margin = max(0, int(unknown_serialization_tokens or 0)) + max(
                0,
                int(next_turn_reserve_tokens or 0),
            )
        usable_input_budget = max(0, effective - output - margin)
        target_input_budget = min(
            usable_input_budget,
            decision.target_input_tokens,
        )
        categories: dict[str, TokenEstimate] = {}
        selected = list(required_blocks)
        for block in required_blocks:
            estimate = self.counter.count(block.content)
            previous = categories.get(block.category)
            categories[block.category] = self._sum_estimates(previous, estimate)

        mandatory = sum(item.upper_bound for item in categories.values())
        if effective is not None and mandatory + output + margin > effective:
            raise ContextBudgetError(
                "required context exceeds effective window",
                effective_window=effective,
                required_tokens=mandatory,
                output_reserve=output,
                safety_margin=margin,
            )

        available = max(0, target_input_budget - mandatory)
        selected_keys = {block.diversity_key for block in required_blocks if block.diversity_key}
        discarded: dict[str, str] = {}
        ranked = sorted(
            optional_blocks,
            key=lambda block: (
                block.relevance * 0.50
                + block.trust * 0.25
                + block.recency * 0.15
                + (0.10 if block.citation else 0.0)
            ),
            reverse=True,
        )
        for block in ranked:
            key = block.identifier or block.citation or str(id(block))
            if block.diversity_key and block.diversity_key in selected_keys:
                discarded[key] = "redundant"
                continue
            estimate = self.counter.count(block.content)
            if estimate.upper_bound > available:
                discarded[key] = "budget"
                continue
            selected.append(block)
            categories[block.category] = self._sum_estimates(
                categories.get(block.category), estimate,
            )
            available -= estimate.upper_bound
            if block.diversity_key:
                selected_keys.add(block.diversity_key)

        payload = [
            {"category": block.category, "content": block.content, "citation": block.citation}
            for block in selected
        ]
        payload_hash = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"),
        ).hexdigest()
        manifest = ContextManifest(
            requested_window=profile.user_window.tokens,
            effective_window=effective,
            actual_window=actual_window,
            context_mode=decision.mode,
            usable_input_budget=usable_input_budget,
            target_input_budget=target_input_budget,
            output_reserve=output,
            safety_margin=margin,
            context_profile_status=profile.status,
            used_unknown_fallback=decision.used_unknown_fallback,
            categories=categories,
            selected=[block.identifier or block.category for block in selected],
            discarded=discarded,
            payload_hash=payload_hash,
        )
        return selected, manifest

    @staticmethod
    def _sum_estimates(
        current: TokenEstimate | None,
        new: TokenEstimate,
    ) -> TokenEstimate:
        if current is None:
            return new
        method: Literal["exact", "estimated"] = (
            "exact" if current.method == new.method == "exact" else "estimated"
        )
        return TokenEstimate(
            current.estimated + new.estimated,
            current.upper_bound + new.upper_bound,
            method,
            current.uncertainty + new.uncertainty,
        )


class ContextBudgetError(ValueError):
    """Mandatory material cannot fit, so retrying blindly would be unsafe."""

    def __init__(
        self,
        message: str,
        *,
        effective_window: int,
        required_tokens: int,
        output_reserve: int,
        safety_margin: int,
    ) -> None:
        super().__init__(message)
        self.effective_window = effective_window
        self.required_tokens = required_tokens
        self.output_reserve = output_reserve
        self.safety_margin = safety_margin


class ContextTooLongRecovery:
    """One evidence-backed capacity downgrade; never a blind retry loop."""

    _LIMIT_RE = re.compile(
        r"(?:context(?: window| length)?|maximum context|最多上下文)\D{0,24}(\d[\d,_]*)",
        re.IGNORECASE,
    )

    def __init__(self) -> None:
        self.attempted = False

    def downgrade(
        self,
        profile: ModelContextProfile,
        provider_error: str,
    ) -> ModelContextProfile | None:
        """Return a lower verified profile exactly once when error names a cap."""

        if self.attempted:
            return None
        match = self._LIMIT_RE.search(provider_error or "")
        if match is None:
            return None
        reported = int(match.group(1).replace(",", "").replace("_", ""))
        if reported <= 0:
            return None
        policy = profile.policy()
        if policy.used_unknown_fallback:
            user_ceiling = profile.user_window.tokens
            if user_ceiling is not None and reported >= int(user_ceiling):
                return None
        elif (
            profile.effective_window is not None
            and reported >= profile.effective_window
        ):
            return None
        self.attempted = True
        return ModelContextProfile(
            **{
                **profile.__dict__,
                "provider_window": ContextLimit(
                    reported,
                    "provider-error:context-too-long",
                    "verified",
                ),
            },
        )
