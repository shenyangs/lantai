"""File-lock setup belongs to bounded disk workers, including cancellation."""
import asyncio
import os
import threading
import time
from pathlib import Path

import pytest

from lantai.runtime import store as store_module


@pytest.mark.asyncio
@pytest.mark.parametrize("guarded", [False, True])
@pytest.mark.parametrize("stage", ["mkdir", "open", "chmod"])
async def test_slow_lock_setup_keeps_loop_responsive(
    tmp_path, monkeypatch, stage, guarded
):
    path = tmp_path / "locks" / "store.lock"
    finished = threading.Event()
    owner, name = (Path, "mkdir") if stage == "mkdir" else (os, stage)
    original = getattr(owner, name)

    def slow(*args, **kwargs):
        if args[0] in (path, path.parent):
            time.sleep(0.15)
            finished.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, slow)
    task = asyncio.create_task(run_locked(path, lambda: 42, guarded))
    try:
        await asyncio.sleep(0.02)
        responsive = not finished.is_set()
        assert await task == 42
        assert responsive, f"{stage} blocked the event loop"
    finally:
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("guarded", [False, True])
async def test_cancel_during_open_drains_and_closes_without_admitting_write(
    tmp_path,
    monkeypatch,
    guarded,
):
    path = tmp_path / "store.lock"
    started, release, finished = (threading.Event() for _ in range(3))
    original = os.open
    descriptors, writes = [], []

    def delayed_open(*args, **kwargs):
        if args[0] != path:
            return original(*args, **kwargs)
        started.set()
        # Bounded so the unfixed implementation fails rather than hangs.
        release.wait(0.5)
        fd = original(*args, **kwargs)
        descriptors.append(fd)
        finished.set()
        return fd

    monkeypatch.setattr(os, "open", delayed_open)
    task = asyncio.create_task(
        run_locked(path, lambda: writes.append("write"), guarded),
    )
    try:
        deadline = asyncio.get_running_loop().time() + 2
        while (
            not started.is_set()
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.005)
        assert started.is_set()
        assert not finished.is_set(), "lock setup prevented cancellation"
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done(), "cancel abandoned lock setup"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        assert writes == []
        assert len(descriptors) == 1
        with pytest.raises(OSError):
            os.fstat(descriptors[0])
        assert await store_module._owned_store_io(path, lambda: 7) == 7
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def run_locked(path, operation, guarded):
    if guarded:
        async with store_module._async_locked_file(path):
            return operation()
    return await store_module._owned_store_io(path, operation)


@pytest.mark.asyncio
async def test_guarded_body_unlock_and_return_do_not_yield(tmp_path):
    observed = []
    loop = asyncio.get_running_loop()
    async with store_module._async_locked_file(tmp_path / "store.lock"):
        loop.call_soon(observed.append, "control-change")
        observed.append("commit")
    observed.append("returned")
    assert observed == ["commit", "returned"]
    await asyncio.sleep(0)
    assert observed == ["commit", "returned", "control-change"]


@pytest.mark.asyncio
async def test_guarded_prepare_failure_closes_created_descriptor(
    tmp_path, monkeypatch
):
    path = tmp_path / "store.lock"
    original_open = os.open
    descriptors = []

    def capture_open(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        if args[0] == path:
            descriptors.append(fd)
        return fd

    def fail_after_open(target, mode):
        if target == path:
            raise RuntimeError("synthetic setup failure")

    monkeypatch.setattr(os, "open", capture_open)
    monkeypatch.setattr(store_module, "_chmod_best_effort", fail_after_open)
    with pytest.raises(RuntimeError, match="synthetic setup failure"):
        async with store_module._async_locked_file(path):
            pytest.fail("failed setup admitted body")
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    assert await store_module._owned_store_io(None, lambda: 7) == 7
