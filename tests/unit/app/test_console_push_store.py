from __future__ import annotations

import pytest

from lantai.app import console_push_store as store


async def _clear_store() -> None:
    async with store._lock:  # pylint: disable=protected-access
        store._list.clear()  # pylint: disable=protected-access


@pytest.mark.asyncio
async def test_sticky_message_keeps_source_until_acknowledged(monkeypatch):
    await _clear_store()
    clock = {"now": 1000.0}
    monkeypatch.setattr(store.time, "time", lambda: clock["now"])

    ordinary_id = await store.append("session-1", "ordinary")
    sticky_id = await store.append(
        "session-1",
        "needs attention",
        sticky=True,
        source_kind="cron_job",
        source_id="job-1",
        source_name="Daily review",
    )
    clock["now"] = 1061.0

    receipts = await store.get_recent()

    assert ordinary_id is not None
    assert [item["id"] for item in receipts] == [sticky_id]
    assert receipts[0] == {
        "id": sticky_id,
        "text": "needs attention",
        "sticky": True,
        "occurred_at": 1000.0,
        "session_id": "session-1",
        "source_kind": "cron_job",
        "source_id": "job-1",
        "source_name": "Daily review",
    }

    assert await store.acknowledge(sticky_id or "") is True
    assert await store.get_recent() == []
    assert await store.acknowledge(sticky_id or "") is False
    await _clear_store()


@pytest.mark.asyncio
async def test_take_preserves_receipt_metadata(monkeypatch):
    await _clear_store()
    monkeypatch.setattr(store.time, "time", lambda: 1234.5)
    message_id = await store.append(
        "session-2",
        "done",
        source_kind="cron_job",
        source_id="job-2",
    )

    receipts = await store.take("session-2")

    assert receipts[0]["id"] == message_id
    assert receipts[0]["occurred_at"] == 1234.5
    assert receipts[0]["session_id"] == "session-2"
    assert receipts[0]["source_kind"] == "cron_job"
    assert receipts[0]["source_id"] == "job-2"
    assert await store.get_recent() == []
    await _clear_store()
