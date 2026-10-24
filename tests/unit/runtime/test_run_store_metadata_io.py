"""Filesystem metadata must not stall async RuntimeRunStore readers."""
import asyncio
import threading
import time
from pathlib import Path

import pytest
from lantai.runtime.store import RuntimeRunStore, RuntimeRunStoreError


OPERATIONS = [
    "contract",
    "progress",
    "loop_contract",
    "evaluation",
    "step",
    "evaluations",
    "steps",
    "contracts",
    "archive",
    "delete",
]


def invocation(store, name):
    if name == "contract":
        return (
            store.contract_path("missing"),
            store.load_contract("missing"),
            None,
        )
    if name == "progress":
        return (
            store.loop_progress_path("missing"),
            store.load_loop_progress_snapshot("missing"),
            None,
        )
    if name == "loop_contract":
        return (
            store.loop_contract_path("missing"),
            store.load_loop_contract_snapshot(
                "missing", expected_attempt_id="attempt"
            ),
            None,
        )
    if name == "evaluation":
        return (
            store.loop_evaluation_path("missing", "report"),
            store.load_loop_evaluation_report("missing", "report"),
            None,
        )
    if name == "step":
        return (
            store.step_path("missing", "step"),
            store.load_step("missing", "step"),
            None,
        )
    if name == "evaluations":
        return (
            store.loop_evaluation_dir("missing"),
            store.list_loop_evaluation_reports("missing"),
            [],
        )
    if name == "steps":
        return store.step_dir("missing"), store.list_steps("missing"), []
    if name == "contracts":
        return store.root / "runs", store.list_contracts_async(), []
    return (
        store.contract_path("missing"),
        getattr(store, name + "_contract")("missing", expected_revision=None),
        RuntimeRunStoreError,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("name", OPERATIONS)
async def test_slow_missing_metadata_keeps_heartbeat_and_missing_semantics(
    tmp_path, monkeypatch, name
):
    store = RuntimeRunStore(tmp_path)
    target, operation, expected = invocation(store, name)
    original = Path.exists
    finished = threading.Event()

    def slow(path):
        if path == target:
            time.sleep(0.15)
            finished.set()
        return original(path)

    monkeypatch.setattr(Path, "exists", slow)
    task = asyncio.create_task(operation)
    try:
        await asyncio.sleep(0.02)
        responsive = not finished.is_set()
        if expected is RuntimeRunStoreError:
            with pytest.raises(
                RuntimeRunStoreError, match="contract not found"
            ):
                await task
        else:
            assert await task == expected
        assert (
            not store.root.exists()
        ), "missing lookup created runtime storage"
        assert finished.is_set()
        assert (
            responsive
        ), "metadata existence check blocked event-loop heartbeat"
    finally:
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_missing_preflight_cancellation_drains_worker(
    tmp_path, monkeypatch
):
    store = RuntimeRunStore(tmp_path)
    target = store.contract_path("missing")
    original = Path.exists
    started, release, finished = (threading.Event() for _ in range(3))

    def slow(path):
        if path == target:
            started.set()
            assert release.wait(5)
            finished.set()
        return original(path)

    monkeypatch.setattr(Path, "exists", slow)
    task = asyncio.create_task(store.load_contract("missing"))
    try:
        for _ in range(500):
            if started.is_set():
                break
            await asyncio.sleep(0.002)
        assert started.is_set()
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        assert not store.root.exists()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
