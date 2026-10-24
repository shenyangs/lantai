# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from lantai.app.experience.events import collect_runtime_runs
from lantai.app.reminders.models import (
    ReminderCreateRequest,
    WatchTopicCreateRequest,
    WatchTopicUpdateRequest,
    utc_now,
)
from lantai.app.reminders.repo import ReminderRepository
from lantai.app.reminders.delivery import ReminderDeliveryWorker
from lantai.app.reminders.lifecycle import ReminderDeliveryWorkspaceService
from lantai.app.reminders.service import ReminderService
from lantai.app.workspace.workspace import Workspace
from lantai.runtime.store import RuntimeRunStore


@pytest.mark.asyncio
async def test_reminder_repo_loads_empty_file(tmp_path):
    repo = ReminderRepository(tmp_path)

    data = await repo.load()

    assert data.version == 1
    assert data.reminders == []
    assert data.watch_topics == []
    assert data.settings.quiet_mode is True


@pytest.mark.asyncio
async def test_reminder_repositories_serialize_concurrent_workspace_writes(tmp_path):
    """The API and delivery worker use distinct repositories for one workspace."""

    services = [ReminderService(ReminderRepository(tmp_path)) for _ in range(24)]
    start = asyncio.Event()

    async def create(index: int):
        await start.wait()
        return await services[index].create_reminder(
            ReminderCreateRequest(title=f"并发提醒 {index}")
        )

    tasks = [asyncio.create_task(create(index)) for index in range(24)]
    start.set()
    created = await asyncio.gather(*tasks)
    stored = await ReminderService(ReminderRepository(tmp_path)).list_reminders()

    assert {item.id for item in stored} == {item.id for item in created}


@pytest.mark.asyncio
async def test_reminder_status_flow_and_snooze(tmp_path):
    service = ReminderService(ReminderRepository(tmp_path))
    item = await service.create_reminder(
        ReminderCreateRequest(
            title="准备周会",
            summary="整理三个待确认问题",
            priority="high",
        )
    )

    read_item = await service.set_status(item.id, "read")
    snoozed_until = utc_now() + timedelta(hours=4)
    snoozed_item = await service.snooze_reminder(item.id, snoozed_until)
    done_item = await service.set_status(item.id, "done")

    assert read_item is not None
    assert read_item.status == "read"
    assert snoozed_item is not None
    assert snoozed_item.status == "snoozed"
    assert snoozed_item.snooze_until == snoozed_until
    assert done_item is not None
    assert done_item.status == "done"


@pytest.mark.asyncio
async def test_today_brief_includes_due_and_high_value_items(tmp_path):
    service = ReminderService(ReminderRepository(tmp_path))
    high = await service.create_reminder(
        ReminderCreateRequest(title="高价值线索", priority="high")
    )
    due = await service.create_reminder(
        ReminderCreateRequest(
            title="今天要看",
            due_at=utc_now() - timedelta(minutes=1),
        )
    )
    later = await service.create_reminder(
        ReminderCreateRequest(
            title="以后再说",
            due_at=utc_now() + timedelta(days=3),
        )
    )
    dismissed = await service.create_reminder(
        ReminderCreateRequest(title="忽略项", priority="high")
    )
    await service.set_status(dismissed.id, "dismissed")

    brief = await service.today_brief()
    brief_ids = {item.id for item in brief.items}

    assert high.id in brief_ids
    assert due.id in brief_ids
    assert later.id not in brief_ids
    assert dismissed.id not in brief_ids
    assert brief.high_value_count == 1
    assert brief.due_count == 1


@pytest.mark.asyncio
async def test_reminder_crud_does_not_create_runtime_run(tmp_path):
    service = ReminderService(ReminderRepository(tmp_path))
    item = await service.create_reminder(
        ReminderCreateRequest(
            title="只是一条提醒",
            due_at=utc_now() - timedelta(minutes=5),
        )
    )

    await service.set_status(item.id, "read")
    await service.snooze_reminder(item.id, utc_now() + timedelta(hours=1))
    await service.set_status(item.id, "done")

    assert RuntimeRunStore(tmp_path).list_contracts() == []


@pytest.mark.asyncio
async def test_due_reminder_claim_creates_runtime_invocation_and_dedupes(tmp_path):
    now = datetime(2026, 7, 17, 9, 30, tzinfo=timezone.utc)
    service = ReminderService(ReminderRepository(tmp_path))
    item = await service.create_reminder(
        ReminderCreateRequest(
            title="跟进运行迁移",
            priority="high",
            due_at=now - timedelta(minutes=1),
        )
    )

    first = await service.claim_due_reminders(
        agent_id="agent-default",
        now=now,
        dedupe_window=timedelta(hours=2),
    )
    assert len(first) == 1
    assert first[0].status == "claimed"
    assert first[0].run_id is not None

    contract = await RuntimeRunStore(tmp_path).load_contract(first[0].run_id)
    stored = (await service.list_reminders())[0]
    assert contract is not None
    assert contract.source == "reminder"
    assert contract.status == "running"
    assert contract.revision == 2
    assert contract.owner.agent_id == "agent-default"
    assert contract.owner.session_id == f"reminder:{item.id}"
    assert contract.current_step is not None
    assert contract.current_step.kind == "delivery"
    assert stored.metadata["runtime"]["last_invocation_id"] == first[0].run_id
    response = collect_runtime_runs([tmp_path], source="reminder")
    assert response.total == 1
    assert response.items[0].id == first[0].run_id
    assert response.items[0].projection_source == "runtime_store"
    assert response.items[0].source == "reminder"
    assert response.items[0].current_step is not None
    assert response.items[0].current_step["kind"] == "delivery"

    second = await service.claim_due_reminders(
        agent_id="agent-default",
        now=now + timedelta(minutes=30),
        dedupe_window=timedelta(hours=2),
    )
    assert len(second) == 1
    assert second[0].status == "deduped"
    assert second[0].run_id == first[0].run_id
    assert [
        contract.run_id
        for contract in RuntimeRunStore(tmp_path).list_contracts()
        if contract.source == "reminder"
    ] == [first[0].run_id]


@pytest.mark.asyncio
async def test_reminder_delivery_worker_delivers_settles_and_restart_dedupes(tmp_path):
    now = datetime(2026, 7, 17, 9, 30, tzinfo=timezone.utc)
    service = ReminderService(ReminderRepository(tmp_path))
    item = await service.create_reminder(
        ReminderCreateRequest(
            title="提醒 runner 到点投递",
            priority="high",
            due_at=now - timedelta(minutes=1),
        )
    )
    deliveries: list[tuple[str, str | None]] = []

    async def notifier(reminder, trigger) -> None:
        deliveries.append((reminder.id, trigger.run_id))

    first_worker = ReminderDeliveryWorker(
        service,
        agent_id="agent-default",
        notifier=notifier,
        injected_test_mode=True,
        dedupe_window=timedelta(hours=2),
    )
    first_tick = await first_worker.run_once(now=now)

    assert first_tick.claimed_count == 1
    assert first_tick.delivered_count == 1
    assert first_tick.deduped_count == 0
    assert first_tick.failed_count == 0
    assert len(first_tick.results) == 1
    assert first_tick.results[0].status == "delivered"
    assert deliveries == [(item.id, first_tick.results[0].run_id)]

    run_id = first_tick.results[0].run_id
    assert run_id is not None
    contract = await RuntimeRunStore(tmp_path).load_contract(run_id)
    assert contract is not None
    assert contract.source == "reminder"
    assert contract.status == "succeeded"
    assert contract.settled_at is not None

    restarted_service = ReminderService(ReminderRepository(tmp_path))
    restarted_worker = ReminderDeliveryWorker(
        restarted_service,
        agent_id="agent-default",
        notifier=notifier,
        injected_test_mode=True,
        dedupe_window=timedelta(hours=2),
    )
    restart_tick = await restarted_worker.run_once(now=now + timedelta(minutes=30))

    assert restart_tick.claimed_count == 0
    assert restart_tick.delivered_count == 0
    assert restart_tick.deduped_count == 1
    assert restart_tick.failed_count == 0
    assert restart_tick.results[0].run_id == run_id
    assert deliveries == [(item.id, run_id)]
    assert [
        contract.run_id
        for contract in RuntimeRunStore(tmp_path).list_contracts()
        if contract.source == "reminder"
    ] == [run_id]


@pytest.mark.asyncio
async def test_reminder_delivery_worker_settles_failed_notification(tmp_path):
    now = datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc)
    service = ReminderService(ReminderRepository(tmp_path))
    await service.create_reminder(
        ReminderCreateRequest(
            title="失败投递也必须收敛",
            due_at=now - timedelta(minutes=1),
        )
    )

    async def failing_notifier(reminder, trigger) -> None:
        _ = (reminder, trigger)
        raise RuntimeError("native notification unavailable")

    worker = ReminderDeliveryWorker(
        service,
        agent_id="agent-default",
        notifier=failing_notifier,
        dedupe_window=timedelta(hours=2),
    )
    tick = await worker.run_once(now=now)

    assert tick.claimed_count == 1
    assert tick.delivered_count == 0
    assert tick.failed_count == 1
    assert tick.results[0].status == "failed"
    assert tick.results[0].reason == "notification_delivery_failed"
    run_id = tick.results[0].run_id
    assert run_id is not None

    contract = await RuntimeRunStore(tmp_path).load_contract(run_id)
    assert contract is not None
    assert contract.status == "failed"
    assert contract.error is not None
    assert contract.error.code == "reminder_invocation_failed"
    assert "native notification unavailable" in contract.error.message


@pytest.mark.asyncio
async def test_reminder_delivery_lifecycle_stays_off_without_explicit_env(tmp_path):
    lifecycle = ReminderDeliveryWorkspaceService(
        workspace_dir=tmp_path,
        agent_id="agent-default",
        env={},
    )

    await lifecycle.start()

    assert lifecycle.enabled is False
    assert lifecycle.running is False


@pytest.mark.asyncio
async def test_reminder_delivery_cannot_settle_without_a_notification_adapter(tmp_path):
    now = datetime(2026, 7, 17, 11, 0, tzinfo=timezone.utc)
    service = ReminderService(ReminderRepository(tmp_path))
    await service.create_reminder(
        ReminderCreateRequest(title="必须真实通知", due_at=now - timedelta(minutes=1))
    )
    lifecycle = ReminderDeliveryWorkspaceService(
        workspace_dir=tmp_path,
        agent_id="agent-default",
        env={"LANTAI_REMINDER_DELIVERY_WORKER": "1"},
    )

    await lifecycle.start()
    assert lifecycle.enabled is False
    assert lifecycle.running is False
    with pytest.raises(RuntimeError, match="notification adapter is not configured"):
        await lifecycle.run_once(now=now)

    assert RuntimeRunStore(tmp_path).list_contracts() == []


@pytest.mark.asyncio
async def test_reminder_smoke_file_alone_cannot_enable_fake_delivery(tmp_path):
    lifecycle = ReminderDeliveryWorkspaceService(
        workspace_dir=tmp_path,
        agent_id="agent-default",
        env={
            "LANTAI_REMINDER_DELIVERY_WORKER": "1",
            "LANTAI_REMINDER_DELIVERY_SMOKE_FILE": str(tmp_path / "delivery.jsonl"),
        },
    )

    await lifecycle.start()
    assert lifecycle.enabled is False
    assert lifecycle.running is False


@pytest.mark.asyncio
async def test_reminder_delivery_lifecycle_runs_and_dedupes_after_restart(tmp_path):
    now = datetime(2026, 7, 17, 11, 0, tzinfo=timezone.utc)
    smoke_file = tmp_path / "delivery.jsonl"
    env = {
        "LANTAI_REMINDER_DELIVERY_WORKER": "1",
        "LANTAI_REMINDER_DELIVERY_INTERVAL_SECONDS": "0.05",
        "LANTAI_REMINDER_DELIVERY_DEDUPE_SECONDS": "7200",
        "LANTAI_REMINDER_DELIVERY_SMOKE_FILE": str(smoke_file),
        "LANTAI_REMINDER_DELIVERY_TEST_MODE": "1",
    }
    service = ReminderService(ReminderRepository(tmp_path))
    await service.create_reminder(
        ReminderCreateRequest(
            title="生命周期 runner 投递",
            due_at=now - timedelta(minutes=1),
        )
    )

    lifecycle = ReminderDeliveryWorkspaceService(
        workspace_dir=tmp_path,
        agent_id="agent-default",
        env=env,
    )
    tick = await lifecycle.run_once(now=now)

    assert tick.delivered_count == 1
    records = [
        json.loads(line)
        for line in smoke_file.read_text(encoding="utf-8").splitlines()
    ]
    delivery_records = [
        record for record in records if record["kind"] == "reminder_delivery"
    ]
    tick_records = [
        record for record in records if record["kind"] == "reminder_delivery_tick"
    ]
    assert len(delivery_records) == 1
    assert len(tick_records) == 1
    run_id = tick.results[0].run_id
    assert run_id is not None
    contract = await RuntimeRunStore(tmp_path).load_contract(run_id)
    assert contract is not None
    assert contract.status == "succeeded"

    restarted = ReminderDeliveryWorkspaceService(
        workspace_dir=tmp_path,
        agent_id="agent-default",
        env=env,
    )
    restarted_tick = await restarted.run_once(now=now + timedelta(minutes=30))

    assert restarted_tick.deduped_count == 1
    assert restarted_tick.delivered_count == 0
    restarted_records = [
        json.loads(line)
        for line in smoke_file.read_text(encoding="utf-8").splitlines()
    ]
    assert [
        record
        for record in restarted_records
        if record["kind"] == "reminder_delivery"
    ] == delivery_records
    assert len(
        [
            record
            for record in restarted_records
            if record["kind"] == "reminder_delivery_tick"
        ]
    ) == 2


def test_workspace_registers_reminder_delivery_lifecycle_service(tmp_path):
    workspace = Workspace(agent_id="default", workspace_dir=str(tmp_path))

    descriptor = workspace._service_manager.descriptors["reminder_delivery"]

    assert descriptor.service_class is ReminderDeliveryWorkspaceService
    assert descriptor.priority == 45
    assert descriptor.start_method == "start"
    assert descriptor.stop_method == "stop"


@pytest.mark.asyncio
async def test_watch_topic_crud(tmp_path):
    service = ReminderService(ReminderRepository(tmp_path))
    topic = await service.create_watch_topic(
        WatchTopicCreateRequest(
            title="澜台提醒系统",
            description="跟进提醒系统下一步",
        )
    )

    updated = await service.update_watch_topic(
        topic.id,
        WatchTopicUpdateRequest(enabled=False, frequency="weekly"),
    )
    deleted = await service.delete_watch_topic(topic.id)

    assert updated is not None
    assert updated.enabled is False
    assert updated.frequency == "weekly"
    assert deleted is True
    assert await service.list_watch_topics() == []
