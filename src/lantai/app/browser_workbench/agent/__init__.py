# -*- coding: utf-8 -*-
"""Deep Browser Agent loop module.

Only the loop and stable contracts are public.  BrowserTask remains a
compatibility facade; RuntimeRunContract remains the control authority.
"""

from .contracts import (
    BROWSER_AGENT_BUDGETS,
    BrowserActionDraft,
    BrowserAgentBudget,
    BrowserAgentContext,
    BrowserAgentResult,
    BrowserAgentUsage,
    BrowserExpectedOutcome,
    BrowserInteractiveElement,
    BrowserNextDecision,
    BrowserPageState,
    BrowserRecoveryDecision,
    BrowserSecuritySignal,
    BrowserStepOutcome,
    browser_agent_budget,
)
from .loop import BrowserAgentLoop
from .observation import browser_page_state_diff, normalize_browser_observation
from .planning import (
    BootstrapPlannerAdapter,
    BrowserPlanner,
    BrowserPlannerError,
    DeterministicPlannerAdapter,
    ModelPlannerAdapter,
)
from .verification import (
    BrowserGoalAssessment,
    BrowserGoalVerifier,
    DeterministicGoalVerifier,
    ModelGoalVerifier,
)

__all__ = [
    "BROWSER_AGENT_BUDGETS",
    "BrowserAgentLoop",
    "BrowserActionDraft",
    "BrowserAgentBudget",
    "BrowserAgentContext",
    "BrowserAgentResult",
    "BrowserAgentUsage",
    "BrowserExpectedOutcome",
    "BrowserInteractiveElement",
    "BrowserNextDecision",
    "BrowserPageState",
    "BrowserRecoveryDecision",
    "BrowserSecuritySignal",
    "BrowserStepOutcome",
    "browser_agent_budget",
    "browser_page_state_diff",
    "normalize_browser_observation",
    "BrowserPlanner",
    "BrowserPlannerError",
    "DeterministicPlannerAdapter",
    "BootstrapPlannerAdapter",
    "ModelPlannerAdapter",
    "BrowserGoalAssessment",
    "BrowserGoalVerifier",
    "DeterministicGoalVerifier",
    "ModelGoalVerifier",
]
