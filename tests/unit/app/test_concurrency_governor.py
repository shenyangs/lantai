from __future__ import annotations

import asyncio

import pytest

from lantai.app.concurrency import (
    ConcurrencyAdmissionError,
    RuntimeConcurrencyGovernor,
    get_runtime_concurrency_governor,
)


@pytest.mark.asyncio
async def test_default_governor_fits_host_plus_four_collaborators(
    monkeypatch,
) -> None:
    monkeypatch.delenv("LANTAI_RUNTIME_MAX_CONCURRENT", raising=False)
    monkeypatch.delenv("LANTAI_AGENT_MAX_CONCURRENT", raising=False)

    governor = get_runtime_concurrency_governor()
    snapshot = governor.snapshot()

    assert snapshot["max_active"] >= 5
    assert snapshot["category_limits"]["agent"] >= 5


@pytest.mark.asyncio
async def test_governor_caps_global_and_category_concurrency() -> None:
    governor = RuntimeConcurrencyGovernor(
        max_active=2,
        max_waiters=4,
        acquire_timeout_seconds=1,
        category_limits={"agent": 1, "coding": 1},
    )
    agent_entered = asyncio.Event()
    release_agent = asyncio.Event()
    second_agent_entered = asyncio.Event()

    async def first_agent() -> None:
        async with governor.slot(category="agent", owner_key="chat-a"):
            agent_entered.set()
            await release_agent.wait()

    async def second_agent() -> None:
        async with governor.slot(category="agent", owner_key="chat-b"):
            second_agent_entered.set()

    first = asyncio.create_task(first_agent())
    await agent_entered.wait()
    second = asyncio.create_task(second_agent())
    await asyncio.sleep(0)
    assert not second_agent_entered.is_set()

    async with governor.slot(category="coding", owner_key="workspace-a"):
        snapshot = governor.snapshot()
        assert snapshot["active"] == 2
        assert snapshot["active_by_category"] == {"agent": 1, "coding": 1}

    release_agent.set()
    await asyncio.gather(first, second)
    assert second_agent_entered.is_set()
    assert governor.snapshot()["active"] == 0


@pytest.mark.asyncio
async def test_governor_serializes_same_owner_and_releases_after_cancel() -> None:
    governor = RuntimeConcurrencyGovernor(
        max_active=2,
        max_waiters=4,
        acquire_timeout_seconds=1,
        category_limits={"agent": 2},
    )
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def first_run() -> None:
        async with governor.slot(category="agent", owner_key="same-chat"):
            first_entered.set()
            await release_first.wait()

    async def second_run() -> None:
        async with governor.slot(category="agent", owner_key="same-chat"):
            second_entered.set()

    first = asyncio.create_task(first_run())
    await first_entered.wait()
    second = asyncio.create_task(second_run())
    await asyncio.sleep(0)
    assert not second_entered.is_set()

    second.cancel()
    await asyncio.gather(second, return_exceptions=True)
    release_first.set()
    await first

    async with governor.slot(category="agent", owner_key="same-chat"):
        assert governor.snapshot()["active"] == 1
    assert governor.snapshot()["waiters"] == 0


@pytest.mark.asyncio
async def test_governor_applies_bounded_backpressure() -> None:
    governor = RuntimeConcurrencyGovernor(
        max_active=1,
        max_waiters=1,
        acquire_timeout_seconds=1,
        category_limits={"agent": 1},
    )
    release = asyncio.Event()
    entered = asyncio.Event()

    async def hold() -> None:
        async with governor.slot(category="agent", owner_key="chat-a"):
            entered.set()
            await release.wait()

    async def wait_for_slot() -> None:
        async with governor.slot(category="agent", owner_key="chat-b"):
            return

    holder = asyncio.create_task(hold())
    await entered.wait()
    waiter = asyncio.create_task(wait_for_slot())
    for _ in range(10):
        if governor.snapshot()["waiters"] == 1:
            break
        await asyncio.sleep(0)

    with pytest.raises(ConcurrencyAdmissionError) as exc_info:
        async with governor.slot(category="agent", owner_key="chat-c"):
            pass
    assert exc_info.value.code == "runtime_concurrency_queue_full"

    release.set()
    await asyncio.gather(holder, waiter)


@pytest.mark.asyncio
async def test_governor_reenters_same_task_but_not_child_task() -> None:
    governor = RuntimeConcurrencyGovernor(
        max_active=1,
        max_waiters=2,
        acquire_timeout_seconds=1,
        category_limits={"agent": 1},
    )
    child_entered = asyncio.Event()

    async def child() -> None:
        async with governor.slot(category="agent", owner_key="same-chat"):
            child_entered.set()

    async with governor.slot(category="agent", owner_key="same-chat"):
        async with governor.slot(category="agent", owner_key="same-chat"):
            assert governor.snapshot()["active"] == 1
        child_task = asyncio.create_task(child())
        await asyncio.sleep(0)
        assert not child_entered.is_set()

    await child_task
    assert child_entered.is_set()
    assert governor.snapshot()["active"] == 0


@pytest.mark.asyncio
async def test_nested_category_borrows_global_slot_without_deadlock() -> None:
    governor = RuntimeConcurrencyGovernor(
        max_active=1,
        max_waiters=2,
        acquire_timeout_seconds=1,
        category_limits={"pipeline": 1, "agent": 1},
    )

    async with governor.slot(category="pipeline", owner_key="pipeline-1"):
        assert governor.snapshot()["active"] == 1
        async with governor.slot(category="agent", owner_key="chat-1"):
            snapshot = governor.snapshot()
            assert snapshot["active"] == 1
            assert snapshot["active_by_category"] == {
                "agent": 1,
                "pipeline": 1,
            }

    assert governor.snapshot()["active"] == 0


@pytest.mark.asyncio
async def test_nested_same_category_with_distinct_owner_borrows_category_slot() -> None:
    """Layered adapters for one logical run must consume one category permit."""

    governor = RuntimeConcurrencyGovernor(
        max_active=1,
        max_waiters=3,
        acquire_timeout_seconds=1,
        category_limits={"agent": 1},
    )
    unrelated_entered = asyncio.Event()

    async def unrelated_run() -> None:
        async with governor.slot(category="agent", owner_key="other-run"):
            unrelated_entered.set()

    async with governor.slot(category="agent", owner_key="tracker-run"):
        async with governor.slot(category="agent", owner_key="runner-session"):
            assert governor.snapshot()["active_by_category"] == {"agent": 1}
            unrelated = asyncio.create_task(unrelated_run())
            await asyncio.sleep(0)
            assert not unrelated_entered.is_set()

    await unrelated
    assert unrelated_entered.is_set()
    assert governor.snapshot()["active"] == 0
