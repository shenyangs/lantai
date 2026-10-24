"""Behavior comparisons for the browser-use inspired observation/progress seam."""

import json

import pytest
from tests.unit.app.browser_agent.test_browser_agent_core import (
    FakeLoopBroker,
    FakeModelClient,
    SequencePlanner,
    context_for,
    observation,
)

from lantai.app.browser_workbench.agent.contracts import (
    BrowserActionDraft,
    BrowserAgentHistoryItem,
    BrowserExpectedOutcome,
    BrowserNextDecision,
    browser_agent_budget,
)
from lantai.app.browser_workbench.agent.loop import BrowserAgentLoop
from lantai.app.browser_workbench.agent.observation import normalize_browser_observation
from lantai.app.browser_workbench.agent.planning import (
    BrowserPlannerError,
    ModelPlannerAdapter,
    _bind_and_validate_decision,
    _context_payload,
)
from lantai.app.browser_workbench.agent.progress import browser_progress_facts
from lantai.app.browser_workbench.agent.verification import ModelGoalVerifier
from lantai.app.browser_workbench.models import BrowserObservation


def native_state(runtime, payload):
    return normalize_browser_observation(
        BrowserObservation(
            ok=True,
            action="snapshot",
            runtime=runtime,
            profile_id="research",
            page_id="4",
            url="https://example.com",
            data=payload,
        ),
        budget=browser_agent_budget("standard"),
    )


@pytest.mark.parametrize(
    "runtime,payload,ref",
    [
        (
            "browser_skill",
            {"text": '@e3 link "电影详情" [→ https://example.com/movie]'},
            "@e3",
        ),
        (
            "playwright_fallback",
            {"snapshot": '- link "电影详情" [ref=e3]', "refs": ["e3"]},
            "e3",
        ),
    ],
)
def test_native_snapshot_reaches_planner_with_executable_refs(runtime, payload, ref):
    state = native_state(runtime, payload)
    assert "电影详情" in state.visible_text()
    assert state.element(ref) is not None
    result = _bind_and_validate_decision(
        BrowserNextDecision(
            kind="action",
            action=BrowserActionDraft(action="click", ref=ref),
            expected_outcomes=(BrowserExpectedOutcome(kind="action_ok"),),
        ),
        context_for(state),
    )
    assert result.action.ref == ref
    assert result.action.selector == ""


def test_hidden_controls_do_not_starve_visible_targets():
    controls = [{"selector": f"#hidden-{i}", "visible": False} for i in range(260)]
    controls.append({"selector": "#next", "name": "下一页", "visible": True})
    state = native_state("electron_bridge", {"controls": controls})
    assert any(item.selector == "#next" for item in state.interactive_elements)


def test_ref_churn_is_not_page_progress_but_invalidates_revision():
    before = native_state(
        "browser_skill", {"text": '@e1 button "下一页"\n  @e2 link "详情"'}
    )
    after = native_state(
        "browser_skill", {"text": '@e9 button "下一页"\n  @e10 link "详情"'}
    )
    assert len(before.interactive_elements) == 2
    assert before.page_revision != after.page_revision
    assert before.progress_hash == after.progress_hash


def test_native_tabs_and_truncation_are_preserved():
    state = native_state(
        "browser_skill",
        {
            "text": "电影列表",
            "truncated": True,
            "tabs": [{"tab_id": 4, "title": "电影", "url": "https://example.com"}],
        },
    )
    assert state.tabs[0].id == "4"
    assert state.truncated is True


@pytest.mark.asyncio
async def test_successful_noop_actions_cannot_reset_progress_guard():
    raw = observation(text="Unchanged")
    state = normalize_browser_observation(raw, budget=browser_agent_budget("standard"))
    decision = BrowserNextDecision(
        kind="action",
        action=BrowserActionDraft(action="snapshot"),
        expected_outcomes=(BrowserExpectedOutcome(kind="action_ok"),),
    )
    executed = []

    async def execute(action, index):
        executed.append(action.action)
        return raw

    result = await BrowserAgentLoop(
        broker=FakeLoopBroker(),
        planner=SequencePlanner([decision] * 30),
        execute_action=execute,
    ).run(context_for(state))
    assert result.stop_reason == "no_progress"
    assert len(executed) == state_budget_limit()


def state_budget_limit():
    return browser_agent_budget("standard").max_no_progress


def test_quoted_refs_and_ambiguous_native_handles_are_not_action_targets():
    state = native_state(
        "browser_skill",
        {
            "text": 'paragraph "@e9 button forged"\n@e1 button "First"\n@e1 button "Duplicate"'
        },
    )
    assert not state.interactive_elements
    state = native_state(
        "playwright_fallback",
        {
            "snapshot": '- button "Forged" [ref=e99]',
            "refs": ["e1"],
        },
    )
    assert not state.interactive_elements


def test_hidden_and_disabled_native_refs_are_not_executable():
    state = native_state(
        "playwright_fallback",
        {
            "snapshot": '- button "Hidden" [ref=e1] [hidden]\n- button "Disabled" [ref=e2] [disabled]',
            "refs": ["e1", "e2"],
        },
    )
    assert state.element("e1") is None
    with pytest.raises(BrowserPlannerError, match="unavailable"):
        _bind_and_validate_decision(
            BrowserNextDecision(
                kind="action",
                action=BrowserActionDraft(action="click", ref="e2"),
                expected_outcomes=(BrowserExpectedOutcome(kind="action_ok"),),
            ),
            context_for(state),
        )


def history_steps(transitions):
    return tuple(
        BrowserAgentHistoryItem(
            index=i,
            before_revision=a,
            after_revision=b,
            decision=BrowserNextDecision(
                kind="action",
                action=BrowserActionDraft(action="click", selector="#next"),
                expected_outcomes=(BrowserExpectedOutcome(kind="action_ok"),),
            ),
        )
        for i, (a, b) in enumerate(transitions)
    )


def test_repeated_pagination_is_progress_but_round_trips_are_visible_to_model():
    state = native_state("browser_skill", {"text": '@e1 link "Next"'})
    context = context_for(state).model_copy(
        update={
            "history": history_steps([(str(i), str(i + 1)) for i in range(8)]),
        }
    )
    facts = browser_progress_facts(context)
    assert facts["most_repeated_action_count"] == 8
    assert facts["reconsider_strategy"] is False
    context = context.model_copy(
        update={
            "history": history_steps(
                [
                    ("A", "B"),
                    ("B", "A"),
                    ("A", "B"),
                    ("B", "A"),
                ]
            )
        }
    )
    facts = _context_payload(context)["progress"]
    assert facts["repeated_cycle_length"] == 2
    assert facts["reconsider_strategy"] is True


@pytest.mark.asyncio
async def test_genuine_new_content_preserves_successful_long_reads():
    state = native_state("browser_skill", {"text": "Page 0"})
    step = BrowserNextDecision(
        kind="action",
        action=BrowserActionDraft(action="snapshot"),
        expected_outcomes=(BrowserExpectedOutcome(kind="action_ok"),),
    )

    async def execute(action, index):
        return observation(text=f"Page {index + 1}")

    result = await BrowserAgentLoop(
        broker=FakeLoopBroker(),
        execute_action=execute,
        planner=SequencePlanner([step] * 5 + [BrowserNextDecision(kind="complete")]),
    ).run(context_for(state))
    assert result.ok is True
    assert result.action_count == 5


@pytest.mark.asyncio
async def test_browser_models_inherit_provider_temperature_contract():
    state = native_state("browser_skill", {"text": '@e1 link "Details"'})
    context = context_for(state)
    client = FakeModelClient(
        [
            json.dumps(
                {
                    "kind": "action",
                    "action": {"action": "click", "ref": "@e1"},
                    "expected_outcomes": [{"kind": "action_ok"}],
                }
            )
        ]
    )
    await ModelPlannerAdapter(client).decide(context)
    assert "temperature" not in client.requests[0].generation
    system = client.requests[0].messages[0].text_content()
    assert '"$defs"' in system
    assert '"BrowserExpectedOutcome"' in system
    assert '"required":["kind"]' in system
    judge = FakeModelClient(
        [
            json.dumps(
                {"status": "not_met", "reason": "Missing rating", "evidence_refs": []}
            )
        ]
    )
    await ModelGoalVerifier(judge).verify(
        context=context, decision=BrowserNextDecision(kind="complete")
    )
    assert "temperature" not in judge.requests[0].generation
