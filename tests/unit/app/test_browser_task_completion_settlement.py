# -*- coding: utf-8 -*-
"""A terminal browser task must have attempted owned tab cleanup."""
from __future__ import annotations

import asyncio

import pytest

from lantai.app.browser_workbench import (
    BrowserActionBroker,
    BrowserProfileService,
    BrowserTaskRuntime,
)
from lantai.app.browser_workbench.models import (
    BrowserAction,
    BrowserProfile,
    BrowserTaskRequest,
)
from lantai.app.browser_workbench.replay import BrowserReplayStore
from lantai.app.browser_workbench import tasks as tasks_module
from lantai.runtime.store import RuntimeRunStore
from lantai.runtime.store import RuntimeRunStoreError
from lantai.runtime import store as store_module


class DelayedCleanupBridge:
    available = True

    def __init__(self):
        self.actions: list[BrowserAction] = []
        self.cleanup_entered = asyncio.Event()
        self.allow_cleanup = asyncio.Event()

    async def execute(self, action: BrowserAction):
        self.actions.append(action)
        if action.action == "close_task_group":
            self.cleanup_entered.set()
            await self.allow_cleanup.wait()
        return {
            "ok": True,
            "runtime": "electron_bridge",
            "profile_id": action.profile_id,
            "url": action.url or "about:blank",
            "closed_tabs": 1,
        }


@pytest.mark.asyncio
async def test_completion_waits_for_owned_tab_cleanup(tmp_path):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="research",
            name="Research",
            allowed_domains=["example.com"],
            agent_control_enabled=True,
        ),
    )
    bridge = DelayedCleanupBridge()
    runtime = BrowserTaskRuntime(
        broker=BrowserActionBroker(
            profiles=profiles,
            replay=BrowserReplayStore(tmp_path),
            bridge=bridge,
        ),
        app_store_distribution=lambda: False,
    )
    run = await runtime.create(
        BrowserTaskRequest(
            run_id="completion-cleanup-boundary",
            profile_id="research",
            target_domains=["example.com"],
            access="read",
            actions=[
                BrowserAction(
                    action="press_key",
                    profile_id="research",
                    url="https://example.com/search",
                    access="read",
                    metadata={"key": "PageDown"},
                ),
            ],
        ),
    )
    handle = getattr(runtime, "_handles")[run.run_id]
    try:
        await asyncio.wait_for(bridge.cleanup_entered.wait(), timeout=2)
        assert run.status == "running_background"
        assert "terminal_tab_cleanup" not in run.metadata
        bridge.allow_cleanup.set()
        await asyncio.wait_for(asyncio.shield(handle), timeout=2)
        assert run.status == "completed"
        assert run.metadata["terminal_tab_cleanup"] == {
            "ok": True,
            "closed_tabs": 1,
            "error_code": "",
        }
        assert [action.action for action in bridge.actions] == [
            "press_key",
            "close_task_group",
        ]
        assert bridge.actions[-1].metadata["task_group_id"] == (
            f"task-{run.run_id}"
        )
    finally:
        bridge.allow_cleanup.set()
        await asyncio.wait_for(asyncio.shield(handle), timeout=2)


def _runtime_and_request(tmp_path, bridge, run_id):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="research",
            name="Research",
            allowed_domains=["example.com"],
            agent_control_enabled=True,
        ),
    )
    runtime = BrowserTaskRuntime(
        broker=BrowserActionBroker(
            profiles=profiles,
            replay=BrowserReplayStore(tmp_path),
            bridge=bridge,
        ),
        app_store_distribution=lambda: False,
    )
    request = BrowserTaskRequest(
        run_id=run_id,
        profile_id="research",
        target_domains=["example.com"],
        access="read",
        actions=[
            BrowserAction(
                action="press_key",
                profile_id="research",
                url="https://example.com/search",
                access="read",
                metadata={"key": "PageDown"},
            ),
        ],
        metadata={"workspace_dir": str(tmp_path)},
    )
    return runtime, request


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["cancel", "cancel_canonical", "pause"])
async def test_control_during_cleanup_preserves_requested_state(
    tmp_path,
    control,
):
    bridge = DelayedCleanupBridge()
    runtime, request = _runtime_and_request(tmp_path, bridge, control)
    run = await runtime.create(request)
    handle = getattr(runtime, "_handles")[run.run_id]
    expected = "paused" if control == "pause" else "cancelled"
    try:
        await asyncio.wait_for(bridge.cleanup_entered.wait(), timeout=2)
        if control == "cancel_canonical":
            await runtime.cancel_canonical(run.run_id)
        else:
            getattr(runtime, control)(run.run_id)
            getattr(runtime, control)(run.run_id)
        assert run.status == expected
        bridge.allow_cleanup.set()
        await asyncio.wait_for(asyncio.shield(handle), timeout=2)
        assert run.status == expected
        assert not any(
            event.type == "task_completed"
            for event in runtime.list_events(run.run_id)
        )
        contract = await RuntimeRunStore(tmp_path).load_contract(run.run_id)
        assert contract is not None
        assert contract.status == expected
        assert [a.action for a in bridge.actions].count(
            "close_task_group",
        ) == 1
    finally:
        bridge.allow_cleanup.set()
        await asyncio.wait_for(asyncio.shield(handle), timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("control", [None, "cancel_canonical", "pause"])
async def test_completion_publishes_only_after_settlement_and_control_check(
    tmp_path,
    monkeypatch,
    control,
):
    bridge = DelayedCleanupBridge()
    bridge.allow_cleanup.set()
    runtime, request = _runtime_and_request(
        tmp_path,
        bridge,
        f"settlement-{control}",
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    original = tasks_module.record_browser_task_runtime_run_settled

    async def delayed_settle(staged, *, app_store_distribution, **kwargs):
        if staged.status == "completed":
            entered.set()
            await release.wait()
        return await original(
            staged,
            app_store_distribution=app_store_distribution,
            **kwargs,
        )

    monkeypatch.setattr(
        tasks_module,
        "record_browser_task_runtime_run_settled",
        delayed_settle,
    )
    run = await runtime.create(request)
    handle = getattr(runtime, "_handles")[run.run_id]
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert run.status == "running_background"
        contract = await RuntimeRunStore(tmp_path).load_contract(run.run_id)
        assert contract is not None
        assert contract.status == "running"
        assert not any(
            event.type == "task_completed"
            for event in runtime.list_events(run.run_id)
        )
        if control == "cancel_canonical":
            await runtime.cancel_canonical(run.run_id)
        elif control == "pause":
            await runtime.pause_canonical(run.run_id)
        release.set()
        await asyncio.wait_for(asyncio.shield(handle), timeout=2)
        expected = {
            None: "completed",
            "cancel_canonical": "cancelled",
            "pause": "paused",
        }[control]
        assert run.status == expected
        contract = await RuntimeRunStore(tmp_path).load_contract(run.run_id)
        assert contract is not None
        assert contract.status == (
            "succeeded" if expected == "completed" else expected
        )
    finally:
        release.set()
        await asyncio.wait_for(asyncio.shield(handle), timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["cancel", "pause"])
async def test_control_wins_while_success_waits_for_store_lock(
    tmp_path,
    monkeypatch,
    control,
):
    bridge = DelayedCleanupBridge()
    bridge.allow_cleanup.set()
    runtime, request = _runtime_and_request(
        tmp_path,
        bridge,
        f"store-lock-{control}",
    )
    entered, release = asyncio.Event(), asyncio.Event()
    original = RuntimeRunStore.save_contract

    async def queued_save(store, contract, *, expected_revision, **kwargs):
        if contract.status != "succeeded":
            return await original(
                store,
                contract,
                expected_revision=expected_revision,
                **kwargs,
            )
        lock = await getattr(store_module, "_lock_for_path")(
            store.contract_path(contract.run_id),
        )
        async with lock:
            commit = asyncio.create_task(
                original(
                    store,
                    contract,
                    expected_revision=expected_revision,
                    **kwargs,
                ),
            )
            await asyncio.sleep(0)
            entered.set()
            await release.wait()
        return await commit

    monkeypatch.setattr(RuntimeRunStore, "save_contract", queued_save)
    run = await runtime.create(request)
    handle = getattr(runtime, "_handles")[run.run_id]
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        getattr(runtime, control)(run.run_id)
        release.set()
        await asyncio.wait_for(asyncio.shield(handle), timeout=2)
        expected = "paused" if control == "pause" else "cancelled"
        assert run.status == expected
        contract = await RuntimeRunStore(tmp_path).load_contract(run.run_id)
        assert contract is not None
        assert contract.status == expected
        assert not any(
            event.type == "task_completed"
            for event in runtime.list_events(run.run_id)
        )
    finally:
        release.set()
        await asyncio.wait_for(asyncio.shield(handle), timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unsettled", "store_error"])
async def test_missing_durable_success_cannot_publish_completed(
    tmp_path,
    monkeypatch,
    failure,
):
    bridge = DelayedCleanupBridge()
    bridge.allow_cleanup.set()
    runtime, request = _runtime_and_request(
        tmp_path,
        bridge,
        f"persist-failure-{failure}",
    )
    original = tasks_module.record_browser_task_runtime_run_settled

    async def unavailable_settle(staged, *, app_store_distribution, **kwargs):
        if staged.status == "completed":
            if failure == "store_error":
                raise RuntimeRunStoreError("synthetic write failure")
            return await RuntimeRunStore(tmp_path).load_contract(staged.run_id)
        return await original(
            staged,
            app_store_distribution=app_store_distribution,
            **kwargs,
        )

    monkeypatch.setattr(
        tasks_module,
        "record_browser_task_runtime_run_settled",
        unavailable_settle,
    )
    run = await runtime.create(request)
    await asyncio.wait_for(
        asyncio.shield(getattr(runtime, "_handles")[run.run_id]),
        timeout=2,
    )
    assert run.status == "failed"
    assert run.error_code == "browser_task_runtime_settlement_failed"
    contract = await RuntimeRunStore(tmp_path).load_contract(run.run_id)
    assert contract is not None
    assert contract.status == "failed"
    assert not any(
        event.type == "task_completed"
        for event in runtime.list_events(run.run_id)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["cancel_canonical", "pause"])
async def test_control_is_checked_at_durable_commit(
    tmp_path,
    monkeypatch,
    control,
):
    bridge = DelayedCleanupBridge()
    bridge.allow_cleanup.set()
    runtime, request = _runtime_and_request(
        tmp_path,
        bridge,
        f"commit-{control}",
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    original = RuntimeRunStore.save_contract

    async def delayed_save(store, contract, *, expected_revision, **kwargs):
        if contract.status == "succeeded":
            entered.set()
            await release.wait()
        return await original(
            store,
            contract,
            expected_revision=expected_revision,
            **kwargs,
        )

    monkeypatch.setattr(RuntimeRunStore, "save_contract", delayed_save)
    run = await runtime.create(request)
    handle = getattr(runtime, "_handles")[run.run_id]
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert run.status == "running_background"
        if control == "pause":
            await runtime.pause_canonical(run.run_id)
        else:
            await runtime.cancel_canonical(run.run_id)
        release.set()
        await asyncio.wait_for(asyncio.shield(handle), timeout=2)
        expected = "paused" if control == "pause" else "cancelled"
        assert run.status == expected
        contract = await RuntimeRunStore(tmp_path).load_contract(run.run_id)
        assert contract is not None
        assert contract.status == expected
        assert not any(
            event.type == "task_completed"
            for event in runtime.list_events(run.run_id)
        )
    finally:
        release.set()
        await asyncio.wait_for(asyncio.shield(handle), timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "control",
    [None, "cancel_canonical", "pause_canonical"],
)
async def test_delayed_store_error_preserves_control_state(
    tmp_path,
    monkeypatch,
    control,
):
    bridge = DelayedCleanupBridge()
    bridge.allow_cleanup.set()
    runtime, request = _runtime_and_request(
        tmp_path,
        bridge,
        f"delayed-error-{control}",
    )
    entered, release = asyncio.Event(), asyncio.Event()
    original = tasks_module.record_browser_task_runtime_run_settled

    async def delayed_error(staged, *, app_store_distribution, **kwargs):
        if staged.status == "completed":
            entered.set()
            await release.wait()
            raise RuntimeRunStoreError("synthetic delayed write failure")
        return await original(
            staged,
            app_store_distribution=app_store_distribution,
            **kwargs,
        )

    monkeypatch.setattr(
        tasks_module,
        "record_browser_task_runtime_run_settled",
        delayed_error,
    )
    run = await runtime.create(request)
    handle = getattr(runtime, "_handles")[run.run_id]
    expected = {
        None: "failed",
        "cancel_canonical": "cancelled",
        "pause_canonical": "paused",
    }[control]
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        if control is not None:
            await getattr(runtime, control)(run.run_id)
            assert run.status == expected
            contract = await RuntimeRunStore(tmp_path).load_contract(
                run.run_id,
            )
            assert contract is not None
            assert contract.status == expected
        release.set()
        await asyncio.wait_for(asyncio.shield(handle), timeout=2)
        assert run.status == expected
        contract = await RuntimeRunStore(tmp_path).load_contract(run.run_id)
        assert contract is not None
        assert contract.status == expected
        if control is None:
            assert run.error_code == "browser_task_runtime_settlement_failed"
        assert not any(
            event.type == "task_completed"
            for event in runtime.list_events(run.run_id)
        )
        assert [a.action for a in bridge.actions].count(
            "close_task_group",
        ) == 1
    finally:
        release.set()
        await asyncio.wait_for(asyncio.shield(handle), timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["pause", "cancel"])
async def test_control_write_is_owned_by_handle_and_canonical_caller(
    tmp_path,
    monkeypatch,
    control,
):
    bridge = DelayedCleanupBridge()
    runtime, request = _runtime_and_request(
        tmp_path, bridge, f"owned-{control}"
    )
    run = await runtime.create(request)
    handle = runtime._handles[run.run_id]
    entered = asyncio.Event()
    release = asyncio.Event()
    name = (
        "record_browser_task_runtime_run_control"
        if control == "pause"
        else "record_browser_task_runtime_run_settled"
    )
    original = getattr(tasks_module, name)
    calls = 0

    async def delayed(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return await original(*args, **kwargs)

    canonical = None
    try:
        await asyncio.wait_for(bridge.cleanup_entered.wait(), timeout=2)
        monkeypatch.setattr(tasks_module, name, delayed)
        getattr(runtime, control)(run.run_id)
        await asyncio.wait_for(entered.wait(), timeout=2)
        canonical = asyncio.create_task(
            getattr(runtime, f"{control}_canonical")(run.run_id),
        )
        bridge.allow_cleanup.set()
        await asyncio.sleep(0.02)
        assert not handle.done()
        assert not canonical.done()
        canonical.cancel()
        await asyncio.sleep(0)
        canonical.cancel()
        await asyncio.sleep(0.02)
        assert not canonical.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await canonical
        await asyncio.wait_for(asyncio.shield(handle), timeout=2)
        persisted = await RuntimeRunStore(tmp_path).load_contract(run.run_id)
        assert persisted.status == (
            "paused" if control == "pause" else "cancelled"
        )
        assert run.run_id not in runtime._runtime_controls
    finally:
        release.set()
        bridge.allow_cleanup.set()
        if canonical is not None:
            await asyncio.gather(canonical, return_exceptions=True)
        await asyncio.wait_for(asyncio.shield(handle), timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_first", [False, True])
async def test_control_drain_owns_new_tail_through_failure_and_cancellation(
    tmp_path,
    monkeypatch,
    fail_first,
):
    from lantai.app.browser_workbench.models import BrowserTaskRun

    runtime, _ = _runtime_and_request(tmp_path, DelayedCleanupBridge(), "tail")
    run = BrowserTaskRun(run_id="tail")
    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    first_release = asyncio.Event()
    second_release = asyncio.Event()
    calls = []

    async def persist(_run, *, action):
        calls.append(action)
        if action == "pause":
            first_entered.set()
            await first_release.wait()
            if fail_first:
                raise OSError("synthetic disk failure")
        else:
            second_entered.set()
            await second_release.wait()

    monkeypatch.setattr(
        tasks_module, "record_browser_task_runtime_run_control", persist
    )
    runtime._schedule_runtime_control(run, "pause")
    await asyncio.wait_for(first_entered.wait(), 2)
    drainer = asyncio.create_task(runtime._drain_runtime_controls(run.run_id))
    await asyncio.sleep(0)
    runtime._schedule_runtime_control(run, "resume")
    drainer.cancel()
    first_release.set()
    try:
        await asyncio.wait_for(second_entered.wait(), 2)
        drainer.cancel()
        await asyncio.sleep(0.01)
        assert not drainer.done()
        assert calls == ["pause", "resume"]
    finally:
        first_release.set()
        second_release.set()
        await asyncio.gather(drainer, return_exceptions=True)
    assert drainer.cancelled()
    assert not runtime._runtime_controls


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_fence", [None, "attempt_id", "lease_owner"])
async def test_browser_settlement_retries_only_same_attempt_revision_races(
    tmp_path,
    monkeypatch,
    changed_fence,
):
    from lantai.app.browser_workbench.models import BrowserTaskRun
    from lantai.app.browser_workbench.runtime_runs import (
        record_browser_task_runtime_run_started,
        record_browser_task_runtime_run_settled,
    )
    from lantai.runtime.store import RuntimeRunStoreConflictError

    run = BrowserTaskRun(
        run_id="revision-race", metadata={"workspace_dir": str(tmp_path)}
    )
    await record_browser_task_runtime_run_started(
        run, app_store_distribution=False
    )
    original = RuntimeRunStore.save_contract
    races = 0

    async def competing_save(store, contract, **kwargs):
        nonlocal races
        if contract.status == "succeeded":
            races += 1
            if races == 1:
                current = await store.load_contract(run.run_id)
                update = {"revision": current.revision + 1}
                if changed_fence:
                    update[changed_fence] = "replacement-owner"
                await original(
                    store,
                    current.model_copy(update=update),
                    expected_revision=current.revision,
                )
        return await original(store, contract, **kwargs)

    monkeypatch.setattr(RuntimeRunStore, "save_contract", competing_save)
    run.status = "completed"
    if changed_fence:
        with pytest.raises(RuntimeRunStoreConflictError):
            await record_browser_task_runtime_run_settled(
                run, app_store_distribution=False
            )
        persisted = await RuntimeRunStore(tmp_path).load_contract(run.run_id)
        assert persisted.status == "running"
        assert races == 1
    else:
        result = await record_browser_task_runtime_run_settled(
            run, app_store_distribution=False
        )
        assert result.status == "succeeded"
        assert races == 2


@pytest.mark.asyncio
async def test_failed_heartbeat_does_not_skip_owned_terminal_cleanup(
    tmp_path, monkeypatch
):
    from lantai.app.browser_workbench.models import BrowserTaskRun

    runtime, _ = _runtime_and_request(
        tmp_path, DelayedCleanupBridge(), "failed-heartbeat"
    )
    run = BrowserTaskRun(run_id="failed-heartbeat", status="cancelled")
    calls = []

    async def failed_heartbeat():
        raise OSError("synthetic heartbeat failure")

    async def drain(_run_id):
        calls.append("drain")

    async def settle(*args, **kwargs):
        calls.append("settle")

    async def cleanup(_run):
        calls.append("cleanup")

    monkeypatch.setattr(runtime, "_drain_runtime_controls", drain)
    monkeypatch.setattr(
        tasks_module, "record_browser_task_runtime_run_settled", settle
    )
    monkeypatch.setattr(runtime, "_cleanup_terminal_task_tabs", cleanup)
    monkeypatch.setattr(
        runtime, "_release_write_lock", lambda _run: calls.append("release")
    )
    heartbeat = asyncio.create_task(failed_heartbeat())
    await asyncio.sleep(0)
    with pytest.raises(OSError, match="heartbeat failure"):
        await runtime._settle_and_cleanup(run, heartbeat)
    assert calls == ["drain", "settle", "cleanup", "release"]
