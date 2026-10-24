# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json

import pytest

from lantai.app.browser_workbench.broker import BrowserActionBroker
from lantai.app.browser_workbench.models import (
    BrowserAction,
    BrowserProfile,
    BrowserTaskConfirmation,
    BrowserTaskRequest,
)
from lantai.app.browser_workbench.profiles import BrowserProfileService
from lantai.app.browser_workbench.replay import BrowserReplayStore
from lantai.app.browser_workbench.tasks import BrowserTaskRuntime
from lantai.app.browser_workbench.tasks import BrowserTaskStore
from lantai.runtime.effects import RuntimeEffectJournal
from lantai.runtime.store import RuntimeRunStore
from lantai.runtime.models import ModelMessage, ModelResponse, ModelUsage


async def wait_for_status(runtime, run_id, statuses, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        run = runtime.get_run(run_id)
        if run is not None and run.status in statuses:
            return run
        await asyncio.sleep(0.01)
    return runtime.get_run(run_id)


class StatefulBridge:
    available = True

    def __init__(self):
        self.url = "about:blank"
        self.text = ""
        self.actions = []
        self.manual_takeover = False

    async def execute(self, action):
        self.actions.append(action)
        if action.action in {"open", "navigate"}:
            self.url = action.url
            self.text = "Quarterly report revenue grew 20 percent."
        if action.action == "click":
            self.text = "Draft saved"
        if action.action in {"snapshot", "snapshot_ax", "snapshot_dom"}:
            return {
                "ok": True,
                "runtime": "electron_bridge",
                "page_id": "tab-agent",
                "profile_id": action.profile_id,
                "url": self.url,
                "data": {
                    "url": self.url,
                    "title": "Agent fixture",
                    "text": self.text,
                    "controls": [
                        {
                            "ref": "save",
                            "selector": "#save",
                            "tag": "button",
                            "role": "button",
                            "name": "Save draft",
                        }
                    ],
                    "page_context": {
                        "kind": "login" if self.manual_takeover else "normal",
                        "requires_manual_takeover": self.manual_takeover,
                    },
                },
            }
        return {
            "ok": True,
            "runtime": "electron_bridge",
            "page_id": "tab-agent",
            "profile_id": action.profile_id,
            "url": self.url,
        }


@pytest.mark.asyncio
async def test_terminal_agent_run_releases_only_its_task_group(tmp_path):
    model = FakeModelClient(
        [
            json.dumps(
                {
                    "kind": "complete",
                    "reason": "Requested evidence is visible.",
                    "evidence_refs": ["page:verified"],
                    "confidence": 0.9,
                }
            ),
            json.dumps(
                {
                    "status": "verified",
                    "reason": "The requested result is present.",
                    "evidence_refs": ["page:verified"],
                }
            ),
        ]
    )
    runtime, bridge = runtime_fixture(
        tmp_path,
        settings={
            "agent_loop_enabled": True,
            "agent_loop_mode": "read_canary",
            "agent_loop_budget_profile": "standard",
        },
        model_client=model,
    )

    run = await runtime.create(
        BrowserTaskRequest(
            run_id="terminal-tab-cleanup",
            profile_id="research",
            target_domains=["example.com"],
            access="read",
            url="https://example.com/report",
            instruction="Read the report.",
        )
    )
    completed = await wait_for_status(runtime, run.run_id, {"completed", "failed"})

    assert completed is not None
    assert completed.status == "completed"
    cleanup = bridge.actions[-1]
    assert cleanup.action == "close_task_group"
    assert cleanup.metadata["task_group_id"] == "task-terminal-tab-cleanup"
    assert cleanup.metadata["browser_task_terminal_cleanup"] is True


@pytest.mark.asyncio
async def test_agent_run_does_not_publish_terminal_status_before_tab_cleanup(
    tmp_path,
):
    model = FakeModelClient(
        [
            json.dumps(
                {
                    "kind": "complete",
                    "reason": "Requested evidence is visible.",
                    "evidence_refs": ["page:verified"],
                    "confidence": 0.9,
                }
            ),
            json.dumps(
                {
                    "status": "verified",
                    "reason": "The requested result is present.",
                    "evidence_refs": ["page:verified"],
                }
            ),
        ]
    )
    runtime, bridge = runtime_fixture(
        tmp_path,
        settings={
            "agent_loop_enabled": True,
            "agent_loop_mode": "read_canary",
            "agent_loop_budget_profile": "standard",
        },
        model_client=model,
    )
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    execute = bridge.execute

    async def execute_with_delayed_cleanup(action):
        if action.action == "close_task_group":
            cleanup_started.set()
            await release_cleanup.wait()
        return await execute(action)

    bridge.execute = execute_with_delayed_cleanup
    run = await runtime.create(
        BrowserTaskRequest(
            run_id="terminal-cleanup-order",
            profile_id="research",
            target_domains=["example.com"],
            access="read",
            url="https://example.com/report",
            instruction="Read the report.",
        )
    )

    await asyncio.wait_for(cleanup_started.wait(), timeout=2.0)
    assert runtime.get_run(run.run_id).status == "running_background"

    release_cleanup.set()
    completed = await wait_for_status(runtime, run.run_id, {"completed", "failed"})
    assert completed is not None and completed.status == "completed"


class FakeModelClient:
    provider_id = "fake"
    model_name = "fake-browser"

    def __init__(self, outputs, *, usages=None):
        self.outputs = list(outputs)
        self.usages = list(usages or [None] * len(self.outputs))
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return ModelResponse(
            message=ModelMessage.text("assistant", self.outputs.pop(0)),
            usage=self.usages.pop(0),
        )


def runtime_fixture(tmp_path, *, settings, model_client=None, durable=False):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="research",
            name="Research",
            allowed_domains=["example.com"],
            agent_control_enabled=True,
            risk_policy="write",
        )
    )
    bridge = StatefulBridge()
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
        bridge=bridge,
    )
    runtime = BrowserTaskRuntime(
        broker=broker,
        app_store_distribution=lambda: False,
        agent_loop_settings=lambda: settings,
        model_client_factory=(lambda: model_client) if model_client else None,
        store=BrowserTaskStore(tmp_path) if durable else None,
        effect_journal=RuntimeEffectJournal(tmp_path) if durable else None,
    )
    return runtime, bridge


@pytest.mark.asyncio
async def test_agent_loop_writes_canonical_run_turn_step_and_effects(tmp_path):
    model = FakeModelClient(
        [
            json.dumps(
                {
                    "kind": "complete",
                    "reason": "Revenue evidence found; password=leaked-secret.",
                    "evidence_refs": [],
                    "confidence": 0.9,
                }
            ),
            json.dumps(
                {
                    "status": "verified",
                    "reason": "The page proves the requested result.",
                    "evidence_refs": [],
                }
            ),
        ],
        usages=[
            ModelUsage(
                input_tokens=120,
                output_tokens=30,
                total_tokens=150,
                cost_cny=0.02,
            ),
            ModelUsage(
                input_tokens=80,
                output_tokens=20,
                total_tokens=100,
                cost_cny=0.01,
            ),
        ],
    )
    runtime, _bridge = runtime_fixture(
        tmp_path,
        settings={
            "agent_loop_enabled": True,
            "agent_loop_mode": "read_canary",
            "agent_loop_budget_profile": "standard",
        },
        model_client=model,
        durable=True,
    )
    run = await runtime.create(
        BrowserTaskRequest(
            run_id="agent-runtime-contract",
            profile_id="research",
            target_domains=["example.com"],
            access="read",
            url="https://example.com/report",
            instruction="Read the report revenue result.",
        )
    )
    completed = await wait_for_status(runtime, run.run_id, {"completed", "failed"})

    assert completed is not None and completed.status == "completed"
    serialized = json.dumps(
        {
            "run": completed.model_dump(mode="json"),
            "events": [
                event.model_dump(mode="json")
                for event in runtime.list_events(run.run_id)
            ],
        }
    )
    assert "leaked-secret" not in serialized
    store = RuntimeRunStore(tmp_path)
    contract = await store.load_contract(run.run_id)
    assert contract is not None and contract.status == "succeeded"
    assert contract.current_step is not None
    steps = await store.list_steps(
        run.run_id,
        expected_attempt_id=contract.attempt_id,
    )
    assert any(step.kind == "turn" for step in steps)
    assert any(step.kind == "plan" for step in steps)
    assert any(step.kind == "evaluation" for step in steps)
    effects = RuntimeEffectJournal(tmp_path).list_effects(
        run.run_id,
        contract.attempt_id,
    )
    latest = {}
    for effect in effects:
        latest[effect.effect_id] = effect
    assert len(latest) == 3
    assert {effect.status for effect in latest.values()} == {"committed"}


@pytest.mark.asyncio
async def test_flag_off_preserves_static_browser_task_runtime(tmp_path):
    runtime, bridge = runtime_fixture(
        tmp_path,
        settings={
            "agent_loop_enabled": False,
            "agent_loop_mode": "off",
            "agent_loop_budget_profile": "standard",
        },
    )
    run = await runtime.create(
        BrowserTaskRequest(
            run_id="static-read",
            profile_id="research",
            target_domains=["example.com"],
            access="read",
            url="https://example.com/report",
        )
    )
    completed = await wait_for_status(runtime, run.run_id, {"completed", "failed"})

    assert completed is not None and completed.status == "completed"
    assert completed.metadata["agent_loop"]["enabled"] is False
    assert [action.action for action in bridge.actions] == [
        "open",
        "snapshot",
        "screenshot",
        "close_task_group",
    ]


@pytest.mark.asyncio
async def test_browser_task_redacts_secrets_from_in_memory_goal_metadata(tmp_path):
    runtime, _bridge = runtime_fixture(
        tmp_path,
        settings={
            "agent_loop_enabled": False,
            "agent_loop_mode": "off",
            "agent_loop_budget_profile": "standard",
        },
    )
    run = await runtime.create(
        BrowserTaskRequest(
            run_id="secret-goal",
            profile_id="research",
            target_domains=["example.com"],
            access="read",
            url="https://example.com/?token=url-secret-token",
            instruction="Read with password=hunter2 and token=goal-secret-token",
        )
    )

    assert "hunter2" not in run.instruction
    assert "goal-secret-token" not in run.instruction
    assert "url-secret-token" not in str(run.metadata)
    assert "[REDACTED]" in run.instruction


@pytest.mark.asyncio
async def test_read_canary_uses_model_loop_and_verified_bootstrap(tmp_path):
    model = FakeModelClient(
        [
            json.dumps(
                {
                    "kind": "complete",
                    "reason": "Revenue grew 20 percent.",
                    "evidence_refs": ["page:report"],
                    "confidence": 0.9,
                }
            ),
            json.dumps(
                {
                    "status": "verified",
                    "reason": "The current page proves the requested revenue result.",
                    "evidence_refs": [],
                }
            ),
        ],
        usages=[
            ModelUsage(
                input_tokens=120,
                output_tokens=30,
                total_tokens=150,
                cost_cny=0.02,
            ),
            ModelUsage(
                input_tokens=80,
                output_tokens=20,
                total_tokens=100,
                cost_cny=0.01,
            ),
        ],
    )
    runtime, bridge = runtime_fixture(
        tmp_path,
        settings={
            "agent_loop_enabled": True,
            "agent_loop_mode": "read_canary",
            "agent_loop_budget_profile": "standard",
        },
        model_client=model,
    )
    run = await runtime.create(
        BrowserTaskRequest(
            run_id="agent-read",
            profile_id="research",
            target_domains=["example.com"],
            access="read",
            url="https://example.com/report",
            instruction="Read the quarterly report and summarize revenue growth.",
        )
    )
    completed = await wait_for_status(runtime, run.run_id, {"completed", "failed"})

    assert completed is not None and completed.status == "completed"
    assert completed.result["agent_loop"] is True
    assert completed.result["stop_reason"] == "goal_complete"
    assert "Revenue grew" in completed.result["message"]
    assert [action.action for action in bridge.actions] == [
        "snapshot",
        "open",
        "snapshot",
        "close_task_group",
    ]
    assert len(model.requests) == 2
    assert model.requests[1].metadata["purpose"] == "browser_agent_goal_verifier"
    assert completed.metadata["agent_usage"] == {
        "model_calls": 2,
        "input_tokens": 200,
        "output_tokens": 50,
        "total_tokens": 250,
        "cost_cny": 0.03,
        "cost_points": 0.03,
    }
    event_types = [event.type for event in runtime.list_events(run.run_id)]
    assert "agent_planning" in event_types
    assert "agent_verifying" in event_types


@pytest.mark.asyncio
async def test_agent_write_waits_for_confirmation_then_executes_once(tmp_path):
    runtime, bridge = runtime_fixture(
        tmp_path,
        settings={
            "agent_loop_enabled": True,
            "agent_loop_mode": "interactive",
            "agent_loop_budget_profile": "draft_write",
        },
        durable=True,
    )
    run = await runtime.create(
        BrowserTaskRequest(
            run_id="agent-write",
            profile_id="research",
            target_domains=["example.com"],
            access="write",
            instruction="Save the prepared draft without publishing.",
            actions=[
                BrowserAction(
                    action="click",
                    profile_id="research",
                    url="https://example.com/editor",
                    selector="#save",
                    access="write",
                    reason="Save draft",
                )
            ],
        )
    )
    waiting = await wait_for_status(
        runtime,
        run.run_id,
        {"waiting_for_user_confirmation", "failed"},
    )

    assert waiting is not None
    assert waiting.status == "waiting_for_user_confirmation"
    assert [action.action for action in bridge.actions].count("click") == 0
    waiting_contract = await RuntimeRunStore(tmp_path).load_contract(run.run_id)
    assert waiting_contract is not None
    assert waiting_contract.status == "waiting_approval"

    await runtime.confirm(
        run.run_id,
        BrowserTaskConfirmation(approved=True, reason="Save this draft once"),
    )
    completed = await wait_for_status(runtime, run.run_id, {"completed", "failed"})

    assert completed is not None and completed.status == "completed"
    assert [action.action for action in bridge.actions].count("click") == 1
    assert completed.metadata["agent_static_plan_index"] == 1
    settled_contract = await RuntimeRunStore(tmp_path).load_contract(run.run_id)
    assert settled_contract is not None
    assert settled_contract.status == "succeeded"


@pytest.mark.asyncio
async def test_model_usage_budget_survives_confirmation_resume(tmp_path):
    model = FakeModelClient(
        [
            json.dumps(
                {
                    "kind": "action",
                    "action": {
                        "action": "click",
                        "ref": "save",
                        "access": "write",
                    },
                    "expected_outcomes": [{"kind": "action_ok"}],
                    "reason": "Save the draft.",
                }
            ),
            json.dumps(
                {
                    "kind": "complete",
                    "reason": "The draft is saved.",
                    "confidence": 0.9,
                }
            ),
            json.dumps(
                {
                    "status": "verified",
                    "reason": "The page confirms the draft was saved.",
                    "evidence_refs": [],
                }
            ),
        ],
        usages=[
            ModelUsage(input_tokens=80, output_tokens=20, total_tokens=100),
            ModelUsage(input_tokens=160, output_tokens=40, total_tokens=200),
            ModelUsage(input_tokens=240, output_tokens=60, total_tokens=300),
        ],
    )
    runtime, bridge = runtime_fixture(
        tmp_path,
        settings={
            "agent_loop_enabled": True,
            "agent_loop_mode": "interactive",
            "agent_loop_budget_profile": "draft_write",
        },
        model_client=model,
        durable=True,
    )
    run = await runtime.create(
        BrowserTaskRequest(
            run_id="agent-model-usage-resume",
            profile_id="research",
            target_domains=["example.com"],
            access="write",
            url="https://example.com/editor",
            instruction="Save the prepared draft without publishing.",
        )
    )
    waiting = await wait_for_status(
        runtime,
        run.run_id,
        {"waiting_for_user_confirmation", "failed"},
    )

    assert waiting is not None
    assert waiting.status == "waiting_for_user_confirmation"
    assert waiting.metadata["agent_usage"]["total_tokens"] == 100

    await runtime.confirm(
        run.run_id,
        BrowserTaskConfirmation(approved=True, reason="Save this draft once"),
    )
    completed = await wait_for_status(runtime, run.run_id, {"completed", "failed"})

    assert completed is not None and completed.status == "completed"
    assert [action.action for action in bridge.actions].count("click") == 1
    assert completed.metadata["agent_usage"]["model_calls"] == 3
    assert completed.metadata["agent_usage"]["total_tokens"] == 600
    assert completed.result["model_decision_count"] == 3


@pytest.mark.asyncio
async def test_read_canary_does_not_enable_agent_loop_for_write(tmp_path):
    runtime, _bridge = runtime_fixture(
        tmp_path,
        settings={
            "agent_loop_enabled": True,
            "agent_loop_mode": "read_canary",
            "agent_loop_budget_profile": "standard",
        },
    )
    run = await runtime.create(
        BrowserTaskRequest(
            run_id="write-not-canary",
            profile_id="research",
            target_domains=["example.com"],
            access="write",
            actions=[
                BrowserAction(
                    action="click",
                    profile_id="research",
                    url="https://example.com/editor",
                    selector="#save",
                    access="write",
                )
            ],
        )
    )

    assert run.metadata["agent_loop"]["enabled"] is False


@pytest.mark.asyncio
async def test_manual_takeover_returns_with_new_attempt_and_observes_first(tmp_path):
    runtime, bridge = runtime_fixture(
        tmp_path,
        settings={
            "agent_loop_enabled": True,
            "agent_loop_mode": "interactive",
            "agent_loop_budget_profile": "standard",
        },
        durable=True,
    )
    bridge.manual_takeover = True
    run = await runtime.create(
        BrowserTaskRequest(
            run_id="agent-takeover",
            profile_id="research",
            target_domains=["example.com"],
            access="read",
            instruction="Read after login.",
            actions=[
                BrowserAction(
                    action="snapshot",
                    profile_id="research",
                    access="read",
                )
            ],
        )
    )
    takeover = await wait_for_status(
        runtime,
        run.run_id,
        {"manual_takeover_required", "failed"},
    )
    assert takeover is not None
    assert takeover.status == "manual_takeover_required"
    previous = await RuntimeRunStore(tmp_path).load_contract(run.run_id)
    assert previous is not None and previous.status == "stalled"

    bridge.manual_takeover = False
    returned = await runtime.resume_canonical(run.run_id)
    assert returned.status == "queued"
    completed = await wait_for_status(runtime, run.run_id, {"completed", "failed"})

    assert completed is not None and completed.status == "completed"
    current = await RuntimeRunStore(tmp_path).load_contract(run.run_id)
    assert current is not None and current.status == "succeeded"
    assert current.attempt_id != previous.attempt_id
    assert getattr(current, "attempt_reason", None) == "takeover"
    previous_steps = await RuntimeRunStore(tmp_path).list_steps(
        run.run_id,
        expected_attempt_id=previous.attempt_id,
    )
    current_steps = await RuntimeRunStore(tmp_path).list_steps(
        run.run_id,
        expected_attempt_id=current.attempt_id,
    )
    assert previous_steps
    assert current_steps
    assert {step.step_id for step in previous_steps}.isdisjoint(
        step.step_id for step in current_steps
    )
    returned_events = runtime.list_events(run.run_id)
    returned_index = next(
        index
        for index, event in enumerate(returned_events)
        if event.type == "task_takeover_returned"
    )
    assert returned_events[returned_index + 1].type == "agent_loop_started"
    assert bridge.actions[2].action == "snapshot"


@pytest.mark.asyncio
async def test_manual_takeover_return_does_not_steal_another_write_lock(tmp_path):
    runtime, bridge = runtime_fixture(
        tmp_path,
        settings={
            "agent_loop_enabled": True,
            "agent_loop_mode": "interactive",
            "agent_loop_budget_profile": "draft_write",
        },
        durable=True,
    )
    bridge.manual_takeover = True
    run = await runtime.create(
        BrowserTaskRequest(
            run_id="agent-takeover-lock",
            profile_id="research",
            target_domains=["example.com"],
            access="write",
            instruction="Save after login.",
            actions=[
                BrowserAction(
                    action="click",
                    profile_id="research",
                    url="https://example.com/editor",
                    selector="#save",
                    access="write",
                )
            ],
        )
    )
    takeover = await wait_for_status(
        runtime,
        run.run_id,
        {"manual_takeover_required", "failed"},
    )
    assert takeover is not None and takeover.status == "manual_takeover_required"
    previous = await RuntimeRunStore(tmp_path).load_contract(run.run_id)
    assert previous is not None and previous.status == "stalled"

    runtime._write_locks[("research", "example.com")] = "other-run"
    blocked = await runtime.resume_canonical(run.run_id)

    assert blocked.status == "manual_takeover_required"
    assert blocked.error_code == "browser_task_write_lock_conflict"
    assert runtime._write_locks[("research", "example.com")] == "other-run"
    current = await RuntimeRunStore(tmp_path).load_contract(run.run_id)
    assert current is not None and current.attempt_id == previous.attempt_id
