"""Local fake notification adapters; no notification is sent to a user."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from lantai.app.reminders.delivery import ReminderDeliveryWorker
from lantai.app.reminders.lifecycle import ReminderDeliveryWorkspaceService
from lantai.app.reminders.models import ReminderCreateRequest
from lantai.app.reminders.repo import ReminderRepository
from lantai.app.reminders.service import ReminderService
from lantai.runtime.store import RuntimeRunStore

NOW = datetime(2026, 10, 2, 13, 0, tzinfo=timezone.utc)


async def _due_service(path):
    service = ReminderService(ReminderRepository(path))
    await service.create_reminder(ReminderCreateRequest(title="fixture only", due_at=NOW - timedelta(minutes=1)))
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", [None, False, {"ok": False, "error": "permission_denied"}, {"ok": True, "shown": False, "reason": "unsupported"}])
async def test_no_delivery_confirmation_cannot_settle_as_delivered(tmp_path, receipt):
    service = await _due_service(tmp_path)
    worker = ReminderDeliveryWorker(service, agent_id="fixture-agent", notifier=lambda reminder, trigger: receipt)
    tick = await worker.run_once(now=NOW)
    contract = await RuntimeRunStore(tmp_path).load_contract(tick.results[0].run_id)
    assert (tick.delivered_count, tick.results[0].status, contract.status) != (1, "delivered", "succeeded"), "returning without a positive delivery receipt was recorded as successful delivery"


@pytest.mark.asyncio
async def test_cancelled_inflight_notification_records_a_settled_non_success_outcome(tmp_path):
    service = await _due_service(tmp_path)
    entered = asyncio.Event()

    async def notifier(reminder, trigger):
        entered.set()
        await asyncio.Event().wait()

    worker = ReminderDeliveryWorker(service, agent_id="fixture-agent", notifier=notifier)
    pending = asyncio.create_task(worker.run_once(now=NOW))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        contracts = [run for run in RuntimeRunStore(tmp_path).list_contracts() if run.source == "reminder"]
        assert len(contracts) == 1
        assert contracts[0].status not in {"queued", "running", "succeeded"} and contracts[0].settled_at is not None, "cancelled delivery left a durable running claim without an unknown/cancelled receipt"
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_missing_reminder_after_claim_settles_the_claimed_runtime_run(tmp_path, monkeypatch):
    service = await _due_service(tmp_path)

    async def missing_reminders():
        return []

    monkeypatch.setattr(service, "list_reminders", missing_reminders)
    worker = ReminderDeliveryWorker(service, agent_id="fixture-agent", notifier=lambda reminder, trigger: None)
    tick = await worker.run_once(now=NOW)
    contract = await RuntimeRunStore(tmp_path).load_contract(tick.results[0].run_id)
    assert tick.failed_count == 1
    assert contract.status not in {"queued", "running", "succeeded"} and contract.settled_at is not None, "reported failure did not settle its claimed RuntimeRun"


@pytest.mark.asyncio
async def test_unconfigured_production_entry_never_claims_due_reminders(tmp_path):
    await _due_service(tmp_path)
    lifecycle = ReminderDeliveryWorkspaceService(workspace_dir=tmp_path, agent_id="fixture-agent", env={"LANTAI_REMINDER_DELIVERY_WORKER": "1"})
    await lifecycle.start()
    try:
        assert not lifecycle.enabled and not lifecycle.running
        assert RuntimeRunStore(tmp_path).list_contracts() == []
    finally:
        await lifecycle.stop()


@pytest.mark.asyncio
async def test_explicit_adapter_exception_records_failed_delivery(tmp_path):
    service = await _due_service(tmp_path)

    def notifier(reminder, trigger):
        raise RuntimeError("fixture notification denied")

    tick = await ReminderDeliveryWorker(service, agent_id="fixture-agent", notifier=notifier).run_once(now=NOW)
    contract = await RuntimeRunStore(tmp_path).load_contract(tick.results[0].run_id)
    assert tick.delivered_count == 0 and tick.failed_count == 1 and contract.status == "failed"
