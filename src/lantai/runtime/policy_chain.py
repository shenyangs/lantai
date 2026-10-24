# -*- coding: utf-8 -*-
"""Policy middleware chain evaluation owned outside the Native turn loop."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .coherence import AuthorityProvenance
from .coherence.integrity import canonical_sha256
from .request_policy_receipt import (
    EffectivePolicyObservationInvocation,
    PolicyEvaluationObservation,
    RequestPolicyReceiptObserver,
)
from .tool_policy import RuntimeToolPolicyMiddleware
from .tools import (
    LantaiToolRegistry,
    ToolCall,
    ToolPolicyContext,
    ToolPolicyOutcome,
    ToolSpec,
)


AwaitWithBudget = Callable[..., Awaitable[Any]]


async def evaluate_policy_chain(
    *,
    registry: LantaiToolRegistry,
    await_with_budget: AwaitWithBudget,
    deadline_exception_type: type[BaseException],
    tool_call: ToolCall,
    spec: ToolSpec,
    policy_context: ToolPolicyContext,
    budget: Any,
    policy_observer: RequestPolicyReceiptObserver | None = None,
    policy_observation_invocation: (
        EffectivePolicyObservationInvocation | None
    ) = None,
) -> ToolPolicyOutcome:
    """Evaluate ordered middleware without owning tool execution."""

    policy_chain: list[dict[str, Any]] = []
    provenance_rows: list[dict[str, Any]] = []
    selected = ToolPolicyOutcome(action="allow")
    decision_rank = {"allow": 0, "needs_approval": 1, "deny": 2}
    evaluation_observation = None
    if (
        type(policy_observer) is RequestPolicyReceiptObserver
        and type(policy_observation_invocation)
        is EffectivePolicyObservationInvocation
    ):
        try:
            evaluation_observation = policy_observer.start_policy_evaluation(
                policy_observation_invocation,
                tool_call=tool_call,
                policy_context=policy_context,
            )
        except Exception:
            evaluation_observation = None
    for index, middleware in enumerate(registry.policy_middlewares()):
        try:
            middleware_context = policy_context.model_copy(deep=True)
            if type(policy_observer) is RequestPolicyReceiptObserver:
                try:
                    policy_observer.record_policy_middleware_dispatch(
                        evaluation_observation,
                        middleware_index=index,
                        middleware_name=type(middleware).__name__,
                        delivered_context=middleware_context,
                    )
                except Exception:
                    pass
            if (
                type(middleware) is RuntimeToolPolicyMiddleware
                and type(policy_observer) is RequestPolicyReceiptObserver
                and type(evaluation_observation) is PolicyEvaluationObservation
            ):
                evaluation = middleware.evaluate(
                    tool_call,
                    spec,
                    middleware_context,
                    policy_observer=policy_observer,
                    policy_evaluation_observation=evaluation_observation,
                    middleware_index=index,
                )
            else:
                evaluation = middleware.evaluate(
                    tool_call,
                    spec,
                    middleware_context,
                )
            outcome = await await_with_budget(evaluation, budget=budget)
        except BaseException as exc:
            if isinstance(exc, deadline_exception_type) or not isinstance(
                exc,
                Exception,
            ):
                raise
            reason = "Tool policy evaluation failed closed before execution."
            outcome = ToolPolicyOutcome(
                action="deny",
                reason=reason,
                risk="critical",
                policy_payload={
                    "decision": "forbidden",
                    "reason": reason,
                    "matched_rule_id": "policy_engine_error",
                    "matched_rule_ids": ["policy_engine_error"],
                    "risk": "critical",
                    "hard_denied": True,
                    "error_detail": str(exc),
                    "middleware": type(middleware).__name__,
                },
                metadata={"source": "policy_engine_error"},
            )
        source_ref = str(
            policy_context.metadata.get("policy_snapshot_hash")
            or policy_context.request_context.get("policy_snapshot_hash")
            or f"runtime-policy:{type(middleware).__name__}"
        ).strip()
        authority = AuthorityProvenance(
            authority_id=(
                "policy:"
                + canonical_sha256(
                    {
                        "index": index,
                        "middleware": type(middleware).__name__,
                        "source_ref": source_ref,
                        "action": outcome.action,
                        "tool_name": tool_call.name,
                        "turn_id": policy_context.turn_id,
                    },
                )[:24]
            ),
            source_kind="runtime_policy",
            actor_kind="policy",
            actor_id=type(middleware).__name__,
            source_ref=source_ref,
            scopes=tuple(
                value
                for value in (
                    (
                        f"turn:{policy_context.turn_id}"
                        if policy_context.turn_id
                        else ""
                    ),
                    f"tool:{tool_call.name}",
                )
                if value
            ),
            delegable=False,
        )
        policy_chain.append(
            {
                "index": index,
                "middleware": type(middleware).__name__,
                "action": outcome.action,
                "reason": outcome.reason,
                "authority_id": authority.authority_id,
            },
        )
        provenance_rows.append(authority.model_dump(mode="json"))
        if decision_rank[outcome.action] > decision_rank[selected.action]:
            selected = outcome
        if selected.action == "deny":
            break
    if type(policy_observer) is RequestPolicyReceiptObserver:
        try:
            policy_observer.complete_policy_evaluation(evaluation_observation)
        except Exception:
            pass
    return selected.model_copy(
        update={
            "metadata": {
                **selected.metadata,
                "policy_chain": policy_chain,
                "authority_provenance": provenance_rows,
                "permission_lattice": "allow<needs_approval<deny",
            },
        },
    )


__all__ = ["evaluate_policy_chain"]
