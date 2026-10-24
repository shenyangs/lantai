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
