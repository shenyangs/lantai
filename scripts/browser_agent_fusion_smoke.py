"""Real configured model against synthetic browser pages; no website writes.

This proves the planner/normalizer/executor/verifier protocol, not real-site
navigation or comparative browser-use success rates. Explicit --live-model
is required because the configured model provider can incur token charges.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lantai.agents.model_factory import create_model_client
from lantai.app.browser_workbench.agent.contracts import (
    BrowserAgentContext,
    browser_agent_budget,
)
from lantai.app.browser_workbench.agent.loop import BrowserAgentLoop
from lantai.app.browser_workbench.agent.observation import normalize_browser_observation
from lantai.app.browser_workbench.agent.planning import ModelPlannerAdapter
from lantai.app.browser_workbench.models import (
    BrowserObservation,
    BrowserPolicyDecision,
)


class SyntheticBrowser:
    def __init__(self):
        self.opened_detail = False
        self.actions: list[str] = []

    def snapshot(self):
        text = (
            'heading "Northern Lights"\nRating: 8.4/10 from 1200 reviews.\nReview summary: strong photography, slow pacing.'
            if self.opened_detail
            else 'heading "Movie catalogue"\n@e1 link "Northern Lights details"\n@e2 link "Help"'
        )
        return BrowserObservation(
            ok=True,
            action="snapshot",
            runtime="browser_skill",
            profile_id="fusion-smoke",
            page_id="4",
            url="https://example.com/movies/1"
            if self.opened_detail
            else "https://example.com/movies",
            data={"text": text},
        )

    def evaluate(self, action):
        allowed = action.action == "snapshot" or (
            action.action == "click" and action.ref == "@e1"
        )
        return BrowserPolicyDecision(
            decision="allow" if allowed else "block",
            access="read",
            reason="Synthetic fixture permits only its details link and observation.",
        )

    async def execute(self, action, _index):
        if self.evaluate(action).decision != "allow":
            raise AssertionError("Fixture action outside test scope")
        self.actions.append(action.action)
        if action.action == "click":
            self.opened_detail = True
        return self.snapshot().model_copy(update={"action": action.action})


async def run():
    browser = SyntheticBrowser()
    budget = browser_agent_budget("quick")
    context = BrowserAgentContext(
        run_id="browser-fusion-synthetic-model-smoke",
        attempt_id="one",
        instruction="Open Northern Lights details using the visible link, read its rating and review summary. Finish with the rating and summary grounded in the detail page.",
        profile_id="fusion-smoke",
        target_domains=("example.com",),
        access="read",
        page_state=normalize_browser_observation(browser.snapshot(), budget=budget),
        budget=budget,
    )
    try:
        result = await asyncio.wait_for(
            BrowserAgentLoop(
                broker=browser,
                planner=ModelPlannerAdapter(create_model_client()),
                execute_action=browser.execute,
            ).run(context),
            timeout=90,
        )
        passed = result.ok and browser.opened_detail and "8.4" in result.message
        print(
            json.dumps(
                {
                    "passed": passed,
                    "evidence_mode": "real_model_synthetic_browser",
                    "real_site_verified": False,
                    "stop_reason": result.stop_reason,
                    "actions": browser.actions,
                    "message": result.message,
                    "usage": result.usage.model_dump(),
                },
                ensure_ascii=False,
            )
        )
        return 0 if passed else 1
    except Exception as exc:  # noqa: BLE001 -- report failures without exposing provider credentials
        print(json.dumps({"passed": False, "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-model", action="store_true", required=True)
    parser.parse_args()
    raise SystemExit(asyncio.run(run()))
