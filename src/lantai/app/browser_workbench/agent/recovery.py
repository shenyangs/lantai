# -*- coding: utf-8 -*-
"""Bounded recovery policy for Browser Agent failures."""

from __future__ import annotations

from ..models import BrowserAction
from .contracts import (
    BrowserAgentContext,
    BrowserRecoveryDecision,
    BrowserStepOutcome,
)


class BrowserRecoveryPolicy:
    """Choose the next bounded recovery without expanding authority."""

    def decide(
        self,
        *,
        context: BrowserAgentContext,
        action: BrowserAction,
        outcome: BrowserStepOutcome,
        visual_available: bool = False,
    ) -> BrowserRecoveryDecision:
        fingerprint = outcome.failure_fingerprint or "failure:unknown"
        count = max(1, int(context.failure_counts.get(fingerprint, 0)))
        if outcome.status == "blocked":
            requires_takeover = any(
                token in outcome.reason.casefold()
                for token in (
                    "login",
                    "captcha",
                    "payment",
                    "authorization",
                    "登录",
                    "验证码",
                    "支付",
                    "授权",
                    "manual takeover",
                )
            )
            return BrowserRecoveryDecision(
                kind="takeover" if requires_takeover else "stop",
                reason=outcome.reason or "Browser policy blocked the action.",
                failure_fingerprint=fingerprint,
            )
        if count >= context.budget.max_same_failure:
            return BrowserRecoveryDecision(
                kind="stop",
                reason="The same browser failure repeated beyond the safe limit.",
                failure_fingerprint=fingerprint,
            )
        if context.no_progress_count >= context.budget.max_no_progress:
            return BrowserRecoveryDecision(
                kind="stop",
                reason="The browser task made no observable progress.",
                failure_fingerprint=fingerprint,
            )
        if outcome.status == "indeterminate":
            return BrowserRecoveryDecision(
                kind="refresh_observation",
                reason="Refresh page evidence before deciding whether the action succeeded.",
                failure_fingerprint=fingerprint,
            )
        if outcome.safe_to_retry and count == 1:
            return BrowserRecoveryDecision(
                kind="retry",
                reason="The action is declared safe to retry once.",
                failure_fingerprint=fingerprint,
                retry_action=True,
            )
        if (action.selector or action.ref) and count == 1:
            return BrowserRecoveryDecision(
                kind="rediscover",
                reason="Refresh element references and rediscover the target.",
                failure_fingerprint=fingerprint,
            )
        if visual_available and context.screenshot_count < context.budget.max_screenshots:
            return BrowserRecoveryDecision(
                kind="visual_fallback",
                reason="DOM evidence is insufficient; collect bounded visual evidence.",
                failure_fingerprint=fingerprint,
            )
        if context.replan_count < context.budget.max_replans:
            return BrowserRecoveryDecision(
                kind="replan",
                reason="Replan from the refreshed page state and failure evidence.",
                failure_fingerprint=fingerprint,
            )
        return BrowserRecoveryDecision(
            kind="stop",
            reason="Browser recovery budget exhausted.",
            failure_fingerprint=fingerprint,
        )
