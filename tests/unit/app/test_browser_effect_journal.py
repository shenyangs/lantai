# -*- coding: utf-8 -*-
"""BrowserTaskRuntime effect journal integration locks."""

from __future__ import annotations

import asyncio

import pytest

from lantai.app.browser_workbench import BrowserTaskRuntime
from lantai.app.browser_workbench.models import (
    BrowserAction,
    BrowserObservation,
    BrowserTaskRequest,
)
from lantai.runtime.effects import RuntimeEffectJournal


class FakeBrowserBroker:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.executed: list[BrowserAction] = []

    async def execute(self, action: BrowserAction) -> BrowserObservation:
        self.executed.append(action)
        return BrowserObservation(
            ok=self.ok,
            action=action.action,
            runtime="electron_bridge",
            profile_id=action.profile_id,
            page_id=action.page_id,
            url=action.url,
            message="ok" if self.ok else "",
            error=None if self.ok else "click failed",
        )


async def _wait_for_status(runtime: BrowserTaskRuntime, run_id: str, statuses: set[str]):
    deadline = asyncio.get_running_loop().time() + 0.5
    while asyncio.get_running_loop().time() < deadline:
        run = runtime.get_run(run_id)
        if run is not None and run.status in statuses:
            return run
        await asyncio.sleep(0.01)
    return runtime.get_run(run_id)


@pytest.mark.asyncio
async def test_browser_task_writes_committed_effect_records(tmp_path):
    journal = RuntimeEffectJournal(tmp_path)
    runtime = BrowserTaskRuntime(
        broker=FakeBrowserBroker(ok=True),
        app_store_distribution=lambda: False,
        effect_journal=journal,
    )

    run = await runtime.create(
        BrowserTaskRequest(
            run_id="browser-run-1",
            access="write",
            actions=[
                BrowserAction(
                    action="click",
                    selector="#save",
                    access="write",
                    metadata={"effect_id": "effect-save-click"},
                )
            ],
            metadata={
                "attempt_id": "attempt-browser-1",
                "contract_hash": "sha256:contract",
                "policy_snapshot_hash": "sha256:policy",
            },
        )
    )
    completed = await _wait_for_status(runtime, run.run_id, {"completed", "failed"})

    records = journal.list_effects("browser-run-1", "attempt-browser-1")

    assert completed is not None
    assert completed.status == "completed"
    assert [record.status for record in records] == ["intent", "running", "committed"]
    assert records[0].effect_id == "effect-save-click"
    assert records[0].tool_name == "browser.click"
    assert records[0].args_hash.startswith("sha256:")
    assert records[0].policy_snapshot_hash == "sha256:policy"
    assert records[-1].result_hash is not None


@pytest.mark.asyncio
async def test_browser_task_writes_failed_effect_record_on_action_failure(tmp_path):
    journal = RuntimeEffectJournal(tmp_path)
    runtime = BrowserTaskRuntime(
        broker=FakeBrowserBroker(ok=False),
        app_store_distribution=lambda: False,
        effect_journal=journal,
    )

    run = await runtime.create(
        BrowserTaskRequest(
            run_id="browser-run-fail",
            access="write",
            actions=[BrowserAction(action="click", selector="#save", access="write")],
            metadata={"attempt_id": "attempt-browser-fail"},
        )
    )
    failed = await _wait_for_status(runtime, run.run_id, {"completed", "failed"})

    records = journal.list_effects("browser-run-fail", "attempt-browser-fail")

    assert failed is not None
    assert failed.status == "failed"
    assert [record.status for record in records] == ["intent", "running", "failed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_effect_id", [False, True])
async def test_durable_static_confirmation_has_a_fresh_effect(
    tmp_path,
    explicit_effect_id,
):
    from lantai.app.browser_workbench.broker import BrowserActionBroker
    from lantai.app.browser_workbench.models import (
        BrowserProfile,
        BrowserTaskConfirmation,
    )
    from lantai.app.browser_workbench.profiles import BrowserProfileService
    from lantai.app.browser_workbench.replay import BrowserReplayStore
    from lantai.app.browser_workbench.tasks import BrowserTaskStore
    from lantai.runtime.store import RuntimeRunStore

    class RecordingBridge:
        available = True

        def __init__(self):
            self.actions = []

        async def execute(self, action):
            self.actions.append(action)
            return {
                "ok": True,
                "runtime": "electron_bridge",
                "profile_id": action.profile_id,
                "page_id": action.page_id,
                "url": action.url,
            }

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
    bridge = RecordingBridge()
    journal = RuntimeEffectJournal(tmp_path)
    runtime = BrowserTaskRuntime(
        broker=BrowserActionBroker(
            profiles=profiles,
            replay=BrowserReplayStore(tmp_path),
            bridge=bridge,
        ),
        app_store_distribution=lambda: False,
        agent_loop_settings=lambda: {"agent_loop_enabled": False},
        store=BrowserTaskStore(tmp_path),
        effect_journal=journal,
    )
    action_metadata = (
        {"effect_id": "save-draft", "idempotency_key": "draft-save-once"}
        if explicit_effect_id
        else {}
    )
    run = await runtime.create(
        BrowserTaskRequest(
            run_id="static-confirmation",
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
                    metadata=action_metadata,
                )
            ],
        )
    )
    await runtime._handles[run.run_id]
    assert run.status == "waiting_for_user_confirmation"
    assert not bridge.actions
    attempt_id = runtime._attempt_id_for_run(run)
    before = journal.list_effects(run.run_id, attempt_id)
    assert [record.status for record in before] == [
        "intent",
        "running",
        "failed",
    ]

    confirmation = BrowserTaskConfirmation(
        approved=True, reason="Save this draft once"
    )
    await runtime.confirm(run.run_id, confirmation)
    await runtime._handles[run.run_id]
    assert run.status == "completed", (run.status, run.error_code)
    await runtime.confirm(run.run_id, confirmation)
    assert [action.action for action in bridge.actions].count("click") == 1
    assert [action.action for action in bridge.actions].count(
        "close_task_group"
    ) == 1
    assert run.completed_action_count == run.planned_action_count == 1
    contract = await RuntimeRunStore(tmp_path).load_contract(run.run_id)
    assert contract is not None and contract.status == "succeeded"
    after = journal.list_effects(run.run_id, attempt_id)
    assert [record.status for record in after] == [
        "intent",
        "running",
        "failed",
        "intent",
        "running",
        "committed",
    ]
    assert after[:3] == before
    assert after[0].effect_id != after[3].effect_id
    assert len({record.effect_id for record in after[3:]}) == 1
    if explicit_effect_id:
        assert after[0].effect_id == "save-draft"
        assert after[3].idempotency_key == "draft-save-once"
