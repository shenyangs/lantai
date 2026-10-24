"""Runtime store contention must not freeze cancellation or other requests."""
from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone

import pytest

from lantai.runtime import store as store_module
from lantai.runtime.runs import RuntimeRunContract, RuntimeRunOwner
from lantai.runtime.store import RuntimeRunStore, RuntimeRunStoreError


def contract():
    return RuntimeRunContract(
        run_id="contended-run", attempt_id="attempt-1", source="chat",
        owner=RuntimeRunOwner(user_id="user", agent_id="default", session_id="s", workspace_id="w", channel="console"),
        status="running", created_at=datetime.now(timezone.utc),
        release_edition="official",
    )


def hold_lock(store, duration=0.3):
    acquired = threading.Event()
    released = threading.Event()
    def hold():
        with store_module._locked_file(store._lock_path()):
            acquired.set()
            time.sleep(duration)
        released.set()
    thread = threading.Thread(target=hold)
    thread.start()
    assert acquired.wait(2)
    return thread, released


@pytest.mark.asyncio
async def test_contended_store_keeps_event_loop_responsive(tmp_path):
    store = RuntimeRunStore(tmp_path)
    thread, released = hold_lock(store)
    write = asyncio.create_task(store.save_contract(contract(), expected_revision=None))
    try:
        await asyncio.sleep(0.025)
        responsive_while_locked = not released.is_set()
        await write
        assert responsive_while_locked, "file lock froze unrelated asyncio work"
        assert await store.load_contract("contended-run") is not None
    finally:
        await asyncio.gather(write, return_exceptions=True)
        thread.join()


@pytest.mark.asyncio
async def test_cancel_while_waiting_for_lock_does_not_write_late(tmp_path):
    store = RuntimeRunStore(tmp_path)
    thread, _ = hold_lock(store)
    write = asyncio.create_task(store.save_contract(contract(), expected_revision=None))
    try:
        await asyncio.sleep(0.025)
        write.cancel()
        with pytest.raises(asyncio.CancelledError):
            await write
    finally:
        await asyncio.gather(write, return_exceptions=True)
        thread.join()
    assert not store.contract_path("contended-run").exists()


@pytest.mark.asyncio
async def test_lock_deadline_fails_without_writing(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "RUNTIME_STORE_LOCK_TIMEOUT_SECONDS", 0.04, raising=False)
    store = RuntimeRunStore(tmp_path)
    thread, _ = hold_lock(store)
    try:
        with pytest.raises(RuntimeRunStoreError, match="lock"):
            await store.save_contract(contract(), expected_revision=None)
    finally:
        thread.join()
    assert not store.contract_path("contended-run").exists()


@pytest.mark.asyncio
async def test_commit_guard_rechecked_after_contention(tmp_path):
    store = RuntimeRunStore(tmp_path)
    thread, _ = hold_lock(store)
    allowed = True
    write = asyncio.create_task(store.save_contract(contract(), expected_revision=None, commit_guard=lambda: allowed))
    try:
        await asyncio.sleep(0.025)
        allowed = False
        with pytest.raises(RuntimeRunStoreError, match="control changed"):
            await write
    finally:
        await asyncio.gather(write, return_exceptions=True)
        thread.join()
    assert not store.contract_path("contended-run").exists()


@pytest.mark.asyncio
async def test_contended_writers_keep_revision_fencing(tmp_path):
    store = RuntimeRunStore(tmp_path)
    initial = contract()
    await store.save_contract(initial, expected_revision=None)
    thread, _ = hold_lock(store)
    try:
        results = await asyncio.gather(
            store.save_contract(initial.model_copy(update={"revision": 1}), expected_revision=0),
            RuntimeRunStore(tmp_path).save_contract(initial.model_copy(update={"revision": 2}), expected_revision=0),
            return_exceptions=True,
        )
    finally:
        thread.join()
    assert sum(isinstance(value, RuntimeRunContract) for value in results) == 1
    assert sum(isinstance(value, store_module.RuntimeRunStoreConflictError) for value in results) == 1
    assert (await store.load_contract(initial.run_id)).revision in (1, 2)


@pytest.mark.asyncio
async def test_deadline_preserves_existing_bytes_and_lock_can_be_reused(tmp_path, monkeypatch):
    store = RuntimeRunStore(tmp_path)
    initial = contract()
    await store.save_contract(initial, expected_revision=None)
    path = store.contract_path(initial.run_id)
    original = path.read_bytes()
    monkeypatch.setattr(store_module, "RUNTIME_STORE_LOCK_TIMEOUT_SECONDS", 0.04)
    thread, _ = hold_lock(store)
    try:
        with pytest.raises(RuntimeRunStoreError, match="lock"):
            await store.save_contract(initial.model_copy(update={"revision": 1}), expected_revision=0)
    finally:
        thread.join()
    assert path.read_bytes() == original
    assert await store.load_contract(initial.run_id) == initial
    assert (await store.save_contract(initial.model_copy(update={"revision": 1}), expected_revision=0)).revision == 1


@pytest.mark.asyncio
async def test_watchdog_listing_does_not_block_event_loop(tmp_path):
    from lantai.app.runner.stall_watchdog import refresh_stalled_runtime_runs

    store = RuntimeRunStore(tmp_path)
    await store.save_contract(contract().model_copy(update={"status": "succeeded"}), expected_revision=None)
    thread, released = hold_lock(store)
    scan = asyncio.create_task(refresh_stalled_runtime_runs([tmp_path]))
    try:
        await asyncio.sleep(0.025)
        responsive_while_locked = not released.is_set()
        assert await scan == []
        assert responsive_while_locked, "watchdog listing blocked unrelated asyncio work"
    finally:
        await asyncio.gather(scan, return_exceptions=True)
        thread.join()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "deadline"])
async def test_async_listing_cannot_quarantine_after_failed_admission(tmp_path, monkeypatch, action):
    store = RuntimeRunStore(tmp_path)
    path = store.contract_path("broken")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"broken JSON must remain unchanged")
    original = path.read_bytes()
    if action == "deadline":
        monkeypatch.setattr(store_module, "RUNTIME_STORE_LOCK_TIMEOUT_SECONDS", 0.04)
    thread, _ = hold_lock(store)
    scan = None
    try:
        scan = asyncio.create_task(store.list_contracts_async())
        if action == "cancel":
            await asyncio.sleep(0.025)
            scan.cancel()
            with pytest.raises(asyncio.CancelledError):
                await scan
        else:
            with pytest.raises(RuntimeRunStoreError, match="lock"):
                await scan
    finally:
        if scan is not None:
            await asyncio.gather(scan, return_exceptions=True)
        thread.join()
    assert path.read_bytes() == original
    assert not list((store.root / "corrupt").glob("*.bad"))
    assert await store.list_contracts_async() == []
    assert not path.exists()
    assert list((store.root / "corrupt").glob("*.bad"))
