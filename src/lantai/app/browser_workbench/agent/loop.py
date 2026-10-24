# -*- coding: utf-8 -*-
"""The bounded Observe → Decide → Act → Verify → Recover browser loop."""

from __future__ import annotations

import inspect
import time
from collections.abc import Awaitable, Callable
from typing import Literal

from ..broker import BrowserActionBroker
from ..models import BrowserAccess, BrowserAction, BrowserObservation
from ..policy import (
    browser_action_is_task_read_interaction,
    canonical_action_access,
)
from .contracts import (
    BrowserAgentContext,
    BrowserAgentHistoryItem,
    BrowserAgentPhase,
    BrowserAgentResult,
    BrowserAgentUsage,
    BrowserNextDecision,
)
from .observation import normalize_browser_observation
from .planning import BrowserPlanner, BrowserPlannerError
from .progress import browser_progress_facts
from .recovery import BrowserRecoveryPolicy
from .safety import (
    page_signals_block_write,
    url_within_domains,
)
from .verification import (
    BrowserGoalVerifier,
    BrowserOutcomeVerifier,
    DeterministicGoalVerifier,
)


BrowserLoopControl = Literal["continue", "pause", "cancel"]
BrowserActionExecutor = Callable[[BrowserAction, int], Awaitable[BrowserObservation]]
BrowserPhaseObserver = Callable[
    [BrowserAgentPhase, dict[str, object]],
    Awaitable[None] | None,
]
BrowserControlProbe = Callable[[], BrowserLoopControl]


def _access_rank(value: BrowserAccess | None) -> int:
    return {None: 0, "read": 0, "write": 1, "sensitive": 2}[value]


class BrowserAgentLoop:
    """One deep, bounded Browser Agent module.

    Runtime ownership and Effect journaling stay with the caller.  The loop
    asks the supplied executor to perform each real browser action, so the
    BrowserTask facade can record Effect intent before delegating to Broker.
    """

    def __init__(
        self,
        *,
        broker: BrowserActionBroker,
        planner: BrowserPlanner,
        verifier: BrowserOutcomeVerifier | None = None,
        goal_verifier: BrowserGoalVerifier | None = None,
        recovery: BrowserRecoveryPolicy | None = None,
        execute_action: BrowserActionExecutor | None = None,
        on_phase: BrowserPhaseObserver | None = None,
        control_probe: BrowserControlProbe | None = None,
        visual_available: bool = False,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.broker = broker
        self.planner = planner
        self.verifier = verifier or BrowserOutcomeVerifier()
        self.goal_verifier = (
            goal_verifier
            or getattr(planner, "goal_verifier", None)
            or DeterministicGoalVerifier()
        )
        self.recovery = recovery or BrowserRecoveryPolicy()
        self.execute_action = execute_action or self._broker_execute
        self.on_phase = on_phase
        self.control_probe = control_probe or (lambda: "continue")
        self.visual_available = visual_available
        self.monotonic = monotonic

    async def _broker_execute(
        self,
        action: BrowserAction,
        _index: int,
    ) -> BrowserObservation:
        return await self.broker.execute(action)

    async def _phase(
        self,
        phase: BrowserAgentPhase,
        **details: object,
    ) -> None:
        if self.on_phase is None:
            return
        result = self.on_phase(phase, details)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _result(
        context: BrowserAgentContext,
        *,
        ok: bool,
        stop_reason,
        message: str,
        pending_action: BrowserAction | None = None,
        evidence_refs: tuple[str, ...] = (),
    ) -> BrowserAgentResult:
        return BrowserAgentResult(
            ok=ok,
            stop_reason=stop_reason,
            message=message,
            final_state=context.page_state,
            action_count=context.action_count,
            model_decision_count=context.model_decision_count,
            replan_count=context.replan_count,
            usage=context.usage,
            pending_action=pending_action,
            evidence_refs=evidence_refs,
        )

    def _control_result(
        self,
        context: BrowserAgentContext,
    ) -> BrowserAgentResult | None:
        control = self.control_probe()
        if control == "pause":
            return self._result(
                context,
                ok=False,
                stop_reason="paused",
                message="Browser Agent paused by the user.",
            )
        if control == "cancel":
            return self._result(
                context,
                ok=False,
                stop_reason="cancelled",
                message="Browser Agent cancelled by the user.",
            )
        return None

    def _budget_result(
        self,
        context: BrowserAgentContext,
        *,
        started_at: float,
    ) -> BrowserAgentResult | None:
        if context.action_count >= context.budget.max_actions:
            return self._result(
                context,
                ok=False,
                stop_reason="action_budget",
                message="Browser Agent action budget exhausted.",
            )
        if context.model_decision_count >= context.budget.max_model_decisions:
            return self._result(
                context,
                ok=False,
                stop_reason="decision_budget",
                message="Browser Agent model decision budget exhausted.",
            )
        if context.usage.total_tokens >= context.budget.max_model_tokens:
            return self._result(
                context,
                ok=False,
                stop_reason="token_budget",
                message="Browser Agent model token budget exhausted.",
            )
        if context.usage.cost_cny >= context.budget.max_cost_cny:
            return self._result(
                context,
                ok=False,
                stop_reason="cost_budget",
                message="Browser Agent model cost budget exhausted.",
            )
        if self.monotonic() - started_at >= context.budget.max_duration_seconds:
            return self._result(
                context,
                ok=False,
                stop_reason="duration_budget",
                message="Browser Agent duration budget exhausted.",
            )
        return None

    @staticmethod
    def _drain_usage(
        context: BrowserAgentContext,
        component: object,
    ) -> BrowserAgentContext:
        drain = getattr(component, "drain_usage", None)
        usage = drain() if callable(drain) else BrowserAgentUsage()
        if not isinstance(usage, BrowserAgentUsage):
            usage = BrowserAgentUsage.model_validate(usage)
        combined = BrowserAgentUsage(
            model_calls=context.usage.model_calls + usage.model_calls,
            input_tokens=context.usage.input_tokens + usage.input_tokens,
            output_tokens=context.usage.output_tokens + usage.output_tokens,
            total_tokens=context.usage.total_tokens + usage.total_tokens,
            cost_cny=context.usage.cost_cny + usage.cost_cny,
        )
        return context.model_copy(
            update={
                "usage": combined,
                "model_decision_count": combined.model_calls,
            }
        )

    def _post_model_budget_result(
        self,
        context: BrowserAgentContext,
    ) -> BrowserAgentResult | None:
        if context.model_decision_count > context.budget.max_model_decisions:
            return self._result(
                context,
                ok=False,
                stop_reason="decision_budget",
                message="Browser Agent model decision budget was exceeded.",
            )
        if context.usage.total_tokens > context.budget.max_model_tokens:
            return self._result(
                context,
                ok=False,
                stop_reason="token_budget",
                message="Browser Agent model token budget was exceeded.",
            )
        if context.usage.cost_cny > context.budget.max_cost_cny:
            return self._result(
                context,
                ok=False,
                stop_reason="cost_budget",
                message="Browser Agent model cost budget was exceeded.",
            )
        return None

    def _authority_failure(
        self,
        context: BrowserAgentContext,
        action: BrowserAction,
    ) -> str:
        if action.confirmed:
            return "Planner actions cannot pre-approve browser confirmation."
        effective_access = self._task_action_access(context, action)
        if _access_rank(effective_access) > _access_rank(context.access):
            return "Planner action exceeds the task access level."
        if action.url and not url_within_domains(action.url, context.target_domains):
            return "Planner action leaves the task target domains."
        if _access_rank(effective_access) > 0 and page_signals_block_write(
            context.page_state.security_signals
        ):
            return "Untrusted page instructions block automatic write actions."
        return ""

    @staticmethod
    def _task_action_access(
        context: BrowserAgentContext,
        action: BrowserAction,
    ) -> BrowserAccess:
        if browser_action_is_task_read_interaction(
            action=action.action,
            task_access=context.access,
            selector=action.selector,
            text=action.text,
            reason=action.reason,
            metadata=action.metadata,
        ):
            return "read"
        return canonical_action_access(action.action, action.access)

    async def _observe(
        self,
        context: BrowserAgentContext,
        *,
        action_index: int,
    ) -> tuple[BrowserAgentContext, BrowserObservation]:
        await self._phase(
            "observing",
            action_index=action_index,
            page_revision=context.page_state.page_revision,
        )
        action = BrowserAction(
            action="snapshot",
            profile_id=context.profile_id,
            page_id=context.page_state.page_id,
            url=context.page_state.url,
            access="read",
            run_id=context.run_id,
            reason="Observe the current page for Browser Agent verification.",
            metadata={
                "browser_agent_internal_observation": True,
                "attempt_id": context.attempt_id,
                "expected_page_revision": context.page_state.page_revision,
            },
        )
        observation = await self.execute_action(action, action_index)
        state = normalize_browser_observation(observation, budget=context.budget)
        return (
            context.model_copy(
                update={
                    "page_state": state,
                    "action_count": context.action_count + 1,
                }
            ),
            observation,
        )

    async def _visual_observe(
        self,
        context: BrowserAgentContext,
        *,
        action_index: int,
    ) -> BrowserAgentContext:
        await self._phase("observing", visual_fallback=True, action_index=action_index)
        action = BrowserAction(
            action="screenshot",
            profile_id=context.profile_id,
            page_id=context.page_state.page_id,
            url=context.page_state.url,
            access="read",
            run_id=context.run_id,
            reason="Collect bounded visual evidence after DOM recovery failed.",
            metadata={
                "browser_agent_visual_fallback": True,
                "attempt_id": context.attempt_id,
            },
        )
        observation = await self.execute_action(action, action_index)
        state = normalize_browser_observation(observation, budget=context.budget)
        # Screenshot observations may not carry DOM; preserve the last DOM state.
        merged = context.page_state.model_copy(
            update={
                "screenshot_artifact_ref": state.screenshot_artifact_ref,
                "screenshot_sha256": state.screenshot_sha256,
                "observed_at": state.observed_at,
                "raw_observation_hash": state.raw_observation_hash,
            }
        )
        return context.model_copy(
            update={
                "page_state": merged,
                "action_count": context.action_count + 1,
                "screenshot_count": context.screenshot_count + 1,
            }
        )

    async def run(self, context: BrowserAgentContext) -> BrowserAgentResult:
        started_at = self.monotonic()
        retry_decision: BrowserNextDecision | None = None

        while True:
            controlled = self._control_result(context)
            if controlled is not None:
                return controlled
            exhausted = self._budget_result(context, started_at=started_at)
            if exhausted is not None:
                return exhausted

            await self._phase(
                "planning",
                action_count=context.action_count,
                model_decision_count=context.model_decision_count,
                replan_count=context.replan_count,
                page_revision=context.page_state.page_revision,
                domain=context.page_state.domain,
                security_signals=[
                    item.model_dump(mode="json")
                    for item in context.page_state.security_signals
                ],
                budget_remaining={
                    "actions": context.budget.max_actions - context.action_count,
                    "model_decisions": context.budget.max_model_decisions
                    - context.model_decision_count,
                    "replans": context.budget.max_replans - context.replan_count,
                    "model_tokens": context.budget.max_model_tokens
                    - context.usage.total_tokens,
                    "cost_cny": context.budget.max_cost_cny
                    - context.usage.cost_cny,
                },
            )
            if retry_decision is not None:
                decision = retry_decision
                retry_decision = None
            else:
                try:
                    decision = await self.planner.decide(context)
                except BrowserPlannerError as exc:
                    context = self._drain_usage(context, self.planner)
                    return self._result(
                        context,
                        ok=False,
                        stop_reason="planner_invalid",
                        message=str(exc),
                    )
                context = self._drain_usage(context, self.planner)
                post_model_budget = self._post_model_budget_result(context)
                if post_model_budget is not None:
                    return post_model_budget

            if decision.kind == "complete":
                await self._phase(
                    "verifying",
                    final_goal=True,
                    page_revision=context.page_state.page_revision,
                    planner_reason=decision.reason,
                )
                goal_outcome = await self.goal_verifier.verify(
                    context=context,
                    decision=decision,
                )
                context = self._drain_usage(context, self.goal_verifier)
                post_model_budget = self._post_model_budget_result(context)
                if post_model_budget is not None:
                    return post_model_budget
                goal_item = BrowserAgentHistoryItem(
                    index=len(context.history),
                    decision=decision,
                    before_revision=context.page_state.page_revision,
                    after_revision=context.page_state.page_revision,
                    outcome=goal_outcome,
                )
                context = context.model_copy(
                    update={"history": (*context.history, goal_item)}
                )
                if goal_outcome.status == "verified":
                    return self._result(
                        context,
                        ok=True,
                        stop_reason="goal_complete",
                        message=decision.reason or goal_outcome.reason,
                        evidence_refs=goal_outcome.evidence_refs,
                    )
                if context.replan_count >= context.budget.max_replans:
                    return self._result(
                        context,
                        ok=False,
                        stop_reason="replan_budget",
                        message=goal_outcome.reason
                        or "Final goal could not be independently verified.",
                        evidence_refs=goal_outcome.evidence_refs,
                    )
                context = context.model_copy(
                    update={
                        "replan_count": context.replan_count + 1,
                        "no_progress_count": context.no_progress_count + 1,
                    }
                )
                if context.no_progress_count >= context.budget.max_no_progress:
                    return self._result(
                        context,
                        ok=False,
                        stop_reason="no_progress",
                        message=goal_outcome.reason
                        or "Final goal remained unverified after repeated checks.",
                        evidence_refs=goal_outcome.evidence_refs,
                    )
                continue
            if decision.kind == "ask_user":
                return self._result(
                    context,
                    ok=False,
                    stop_reason="waiting_confirmation",
                    message=decision.reason or "Browser Agent needs user input.",
                    evidence_refs=decision.evidence_refs,
                )
            if decision.kind == "takeover":
                return self._result(
                    context,
                    ok=False,
                    stop_reason="manual_takeover",
                    message=decision.reason or "Browser Agent requires manual takeover.",
                    evidence_refs=decision.evidence_refs,
                )
            if decision.kind == "fail" or decision.action is None:
                return self._result(
                    context,
                    ok=False,
                    stop_reason="failed",
                    message=decision.reason or "Browser Agent could not continue.",
                    evidence_refs=decision.evidence_refs,
                )

            action = decision.action.to_action(
                profile_id=context.profile_id,
                run_id=context.run_id,
                default_access=context.access,
                current_page_id=context.page_state.page_id,
            )
            action = action.model_copy(
                update={
                    "access": self._task_action_access(context, action),
                    "metadata": {
                        **action.metadata,
                        "browser_agent_expected_outcomes": [
                            item.model_dump(mode="json")
                            for item in decision.expected_outcomes
                        ],
                        "browser_agent_page_revision": context.page_state.page_revision,
                    }
                }
            )
            authority_failure = self._authority_failure(context, action)
            if authority_failure:
                return self._result(
                    context,
                    ok=False,
                    stop_reason="policy_blocked",
                    message=authority_failure,
                    pending_action=action,
                )

            await self._phase(
                "checking_policy",
                action=action.action,
                reason=decision.reason,
                expected_outcomes=[
                    item.model_dump(mode="json") for item in decision.expected_outcomes
                ],
            )
            policy = self.broker.evaluate(action)
            if policy.decision == "prompt":
                return self._result(
                    context,
                    ok=False,
                    stop_reason="waiting_confirmation",
                    message=policy.reason,
                    pending_action=action,
                )
            if policy.decision == "block":
                reason = policy.reason or "Browser policy blocked the action."
                takeover = policy.matched_rule_id == "browser_manual_takeover_required"
                return self._result(
                    context,
                    ok=False,
                    stop_reason="manual_takeover" if takeover else "policy_blocked",
                    message=reason,
                    pending_action=action,
                )

            before = context.page_state
            await self._phase(
                "executing",
                action=action.action,
                reason=decision.reason,
                page_revision=before.page_revision,
            )
            action_observation = await self.execute_action(action, context.action_count)
            context = context.model_copy(
                update={"action_count": context.action_count + 1}
            )
            if (
                action_observation.decision is not None
                and action_observation.decision.decision == "prompt"
            ):
                return self._result(
                    context,
                    ok=False,
                    stop_reason="waiting_confirmation",
                    message=action_observation.error
                    or action_observation.decision.reason,
                    pending_action=action,
                    evidence_refs=(action_observation.replay_event_id,),
                )

            if action.action in {"snapshot", "snapshot_ax", "snapshot_dom"}:
                after = normalize_browser_observation(
                    action_observation,
                    budget=context.budget,
                )
                context = context.model_copy(update={"page_state": after})
            else:
                if context.action_count >= context.budget.max_actions:
                    return self._result(
                        context,
                        ok=False,
                        stop_reason="action_budget",
                        message="Browser Agent cannot verify the action within budget.",
                        evidence_refs=(action_observation.replay_event_id,),
                    )
                context, _verification_observation = await self._observe(
                    context,
                    action_index=context.action_count,
                )
                after = context.page_state

            if after.requires_manual_takeover:
                return self._result(
                    context,
                    ok=False,
                    stop_reason="manual_takeover",
                    message="The page requires manual takeover before Browser Agent can continue.",
                    pending_action=action,
                    evidence_refs=tuple(
                        ref
                        for ref in (
                            action_observation.replay_event_id,
                            after.screenshot_artifact_ref,
                        )
                        if ref
                    ),
                )

            await self._phase(
                "verifying",
                action=action.action,
                before_revision=before.page_revision,
                after_revision=after.page_revision,
            )
            outcome = self.verifier.verify(
                action=action,
                expectations=decision.expected_outcomes,
                before=before,
                after=after,
                observation=action_observation,
            )
            item = BrowserAgentHistoryItem(
                index=len(context.history),
                decision=decision,
                before_revision=before.page_revision,
                after_revision=after.page_revision,
                before_progress_hash=before.progress_hash,
                after_progress_hash=after.progress_hash,
                outcome=outcome,
            )
            context = context.model_copy(
                update={"history": (*context.history, item)}
            )
            if outcome.status == "verified":
                changed = (before.progress_hash or before.content_hash) != (
                    after.progress_hash or after.content_hash
                )
                stagnant = 0 if changed else context.no_progress_count + 1
                context = context.model_copy(
                    update={
                        "no_progress_count": stagnant,
                        "verified_evidence_refs": tuple(
                            dict.fromkeys(
                                (
                                    *context.verified_evidence_refs,
                                    *outcome.evidence_refs,
                                )
                            )
                        ),
                    }
                )
                if browser_progress_facts(context)["unchanged_action_repeat_count"] >= context.budget.max_no_progress:
                    return self._result(
                        context, ok=False, stop_reason="no_progress",
                        message="Browser actions succeeded but page evidence did not change; stop before repeating further.",
                        evidence_refs=outcome.evidence_refs,
                    )
                continue

            failure_counts = dict(context.failure_counts)
            fingerprint = outcome.failure_fingerprint or "failure:unknown"
            failure_counts[fingerprint] = failure_counts.get(fingerprint, 0) + 1
            context = context.model_copy(
                update={
                    "failure_counts": failure_counts,
                    "no_progress_count": context.no_progress_count
                    + (1 if (before.progress_hash or before.content_hash) == (after.progress_hash or after.content_hash) else 0),
                }
            )
            await self._phase(
                "recovering",
                action=action.action,
                outcome=outcome.model_dump(mode="json"),
            )
            recovery = self.recovery.decide(
                context=context,
                action=action,
                outcome=outcome,
                visual_available=self.visual_available,
            )
            if recovery.kind == "retry":
                retry_decision = decision
                continue
            if recovery.kind in {"refresh_observation", "rediscover", "replan"}:
                if context.action_count >= context.budget.max_actions:
                    return self._result(
                        context,
                        ok=False,
                        stop_reason="action_budget",
                        message="Browser Agent recovery action budget exhausted.",
                    )
                context, _ = await self._observe(
                    context,
                    action_index=context.action_count,
                )
                if recovery.kind in {"rediscover", "replan"}:
                    context = context.model_copy(
                        update={"replan_count": context.replan_count + 1}
                    )
                continue
            if recovery.kind == "visual_fallback":
                context = await self._visual_observe(
                    context,
                    action_index=context.action_count,
                )
                context = context.model_copy(
                    update={"replan_count": context.replan_count + 1}
                )
                continue
            if recovery.kind == "ask_user":
                return self._result(
                    context,
                    ok=False,
                    stop_reason="waiting_confirmation",
                    message=recovery.reason,
                    pending_action=action,
                )
            if recovery.kind == "takeover":
                return self._result(
                    context,
                    ok=False,
                    stop_reason="manual_takeover",
                    message=recovery.reason,
                    pending_action=action,
                )
            stop_reason = (
                "repeated_failure"
                if failure_counts.get(fingerprint, 0) >= context.budget.max_same_failure
                else "no_progress"
                if context.no_progress_count >= context.budget.max_no_progress
                else "failed"
            )
            return self._result(
                context,
                ok=False,
                stop_reason=stop_reason,
                message=recovery.reason,
                evidence_refs=outcome.evidence_refs,
            )


__all__ = ["BrowserAgentLoop"]
