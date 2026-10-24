# -*- coding: utf-8 -*-
"""Behavior locks for BrowserTask RuntimeRunStore adapter writes."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from lantai.app.browser_workbench import BrowserTaskRuntime
from lantai.app.browser_workbench.models import (
    BrowserAction,
    BrowserObservation,
    BrowserTaskRequest,
)
from lantai.runtime.store import RuntimeRunStore


class _FakeBrowserBroker:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok

    async def execute(self, action: BrowserAction) -> BrowserObservation:
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


class _SlowBrowserBroker:
    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def execute(self, action: BrowserAction) -> BrowserObservation:
        await self.release.wait()
        return BrowserObservation(
            ok=True,
            action=action.action,
            runtime="electron_bridge",
            profile_id=action.profile_id,
            page_id=action.page_id,
            url=action.url,
            message="ok",
        )


class _BlockingBrowserBroker:
    def evaluate(self, action: BrowserAction):
        return SimpleNamespace(
            decision="block",
            matched_rule_id="browser_profile_agent_control_disabled",
            reason="Browser profile has not enabled agent control.",
        )

    async def execute(self, action: BrowserAction) -> BrowserObservation:
        raise AssertionError("Blocked browser task should not execute actions")


async def _wait_for_terminal(runtime: BrowserTaskRuntime, run_id: str):
    deadline = asyncio.get_running_loop().time() + 0.5
    while asyncio.get_running_loop().time() < deadline:
        run = runtime.get_run(run_id)
        if run is not None and run.status in {"completed", "failed", "cancelled"}:
            return run
        await asyncio.sleep(0.01)
    return runtime.get_run(run_id)


@pytest.mark.asyncio
async def test_browser_task_writes_succeeded_runtime_run_contract(tmp_path):
    runtime = BrowserTaskRuntime(
        broker=_FakeBrowserBroker(ok=True),
        app_store_distribution=lambda: False,
    )

    run = await runtime.create(
        BrowserTaskRequest(
            run_id="browser-runtime-run-1",
            access="write",
            actions=[BrowserAction(action="click", selector="#save", access="write")],
            metadata={
                "workspace_dir": str(tmp_path),
                "agent_id": "browser-agent",
                "session_id": "browser-session",
                "user_id": "user-1",
            },
        )
    )
    completed = await _wait_for_terminal(runtime, run.run_id)
    contract = await RuntimeRunStore(tmp_path).load_contract(run.run_id)

    assert completed is not None
    assert completed.status == "completed"
    assert contract is not None
    assert contract.source == "browser"
    assert contract.status == "succeeded"
    assert contract.revision == 3
    assert contract.owner.agent_id == "browser-agent"
    assert contract.owner.session_id == "browser-session"
    assert contract.owner.workspace_id == str(tmp_path)
    assert contract.contract_hash.startswith("sha256:")


@pytest.mark.asyncio
async def test_browser_task_preflight_failure_settles_runtime_contract(tmp_path):
    runtime = BrowserTaskRuntime(
        broker=_BlockingBrowserBroker(),
        app_store_distribution=lambda: False,
    )

    run = await runtime.create(
        BrowserTaskRequest(
            run_id="browser-runtime-run-policy-block",
            access="read",
            actions=[BrowserAction(action="open", url="about:blank", access="read")],
            metadata={"workspace_dir": str(tmp_path)},
        )
    )
    contract = await RuntimeRunStore(tmp_path).load_contract(run.run_id)

    assert run.status == "failed"
    assert run.error_code == "browser_profile_agent_control_disabled"
    assert contract is not None
    assert contract.status == "failed"
    assert contract.revision == 3
    assert contract.error is not None
    assert contract.error.code == "browser_profile_agent_control_disabled"


@pytest.mark.asyncio
async def test_browser_task_writes_failed_runtime_run_contract(tmp_path):
    runtime = BrowserTaskRuntime(
        broker=_FakeBrowserBroker(ok=False),
        app_store_distribution=lambda: False,
    )

    run = await runtime.create(
        BrowserTaskRequest(
            run_id="browser-runtime-run-failed",
            access="write",
            actions=[BrowserAction(action="click", selector="#save", access="write")],
            metadata={"workspace_dir": str(tmp_path)},
        )
    )
    failed = await _wait_for_terminal(runtime, run.run_id)
    contract = await RuntimeRunStore(tmp_path).load_contract(run.run_id)

    assert failed is not None
    assert failed.status == "failed"
    assert contract is not None
    assert contract.status == "failed"
    assert contract.error is not None
    assert contract.error.code == "browser_task_action_failed"


@pytest.mark.asyncio
async def test_browser_task_runtime_refreshes_heartbeat_during_silent_action(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    from lantai.app.browser_workbench import tasks as browser_tasks

    broker = _SlowBrowserBroker()
    monkeypatch.setattr(browser_tasks, "_BROWSER_TASK_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    runtime = BrowserTaskRuntime(
        broker=broker,
        app_store_distribution=lambda: False,
    )

    run = await runtime.create(
        BrowserTaskRequest(
            run_id="browser-runtime-run-heartbeat",
            access="write",
            actions=[BrowserAction(action="click", selector="#save", access="write")],
            metadata={"workspace_dir": str(tmp_path)},
        )
    )
    store = RuntimeRunStore(tmp_path)

    before = await store.load_contract(run.run_id)
    assert before is not None
    assert before.status == "running"
    assert before.heartbeat_at is not None

    await asyncio.sleep(0.05)
    during = await store.load_contract(run.run_id)

    assert during is not None
    assert during.status == "running"
    assert during.revision > before.revision
    assert during.heartbeat_at is not None
    assert during.heartbeat_at > before.heartbeat_at

    broker.release.set()
    completed = await _wait_for_terminal(runtime, run.run_id)
    assert completed is not None
    assert completed.status == "completed"
