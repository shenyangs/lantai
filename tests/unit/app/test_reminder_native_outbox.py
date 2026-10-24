"""Bound desktop receipts and durable terminal-write recovery."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from lantai.app.reminders.delivery import ReminderDeliveryWorker
from lantai.app.reminders.models import ReminderCreateRequest, ReminderSettings
from lantai.app.reminders.notification_outbox import NativeReminderNotifier, ReminderNotificationOutbox
from lantai.app.reminders.repo import ReminderRepository
from lantai.app.reminders.service import ReminderService
from lantai.runtime.store import RuntimeRunStore

NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)


async def setup_delivery(path):
    service = ReminderService(ReminderRepository(path))
    await service.set_settings(ReminderSettings(native_notification_policy="explicit_only"))
    item = await service.create_reminder(ReminderCreateRequest(title="Synthetic", due_at=NOW))
    outbox = ReminderNotificationOutbox(service.repository, "a")
    await outbox.claim_native("desktop-a")
    worker = ReminderDeliveryWorker(service, agent_id="a", native_only=True,
        notifier=NativeReminderNotifier(outbox, timeout_seconds=1))
    task = asyncio.create_task(worker.run_once(now=NOW))
    payload = None
    for _ in range(100):
        payload = await outbox.claim_native("desktop-a")
        if payload:
            break
        await asyncio.sleep(.01)
    assert payload is not None
    return service, outbox, worker, task, payload, item


def shown_receipt(payload):
    return {key: value for key, value in payload.items() if key not in {"title", "body"}} | {
        "id": payload["run_id"], "ok": True, "shown": True, "status": "shown",
        "reason": "", "error": ""}


@pytest.mark.asyncio
async def test_bound_show_receipt_and_single_claim(tmp_path):
    service, outbox, worker, task, payload, _ = await setup_delivery(tmp_path)
    assert await outbox.claim_native("desktop-b") is None
    receipt = shown_receipt(payload)
    await outbox.resolve_native(receipt)
    tick = await task
    assert tick.delivered_count == 1
    run = await RuntimeRunStore(tmp_path).load_contract(payload["run_id"])
    assert run.status == "succeeded" and run.settled_at
    # A replay of the exact acknowledged receipt is safe after settlement.
    await outbox.resolve_native(receipt)
    second = await worker.run_once(now=NOW + timedelta(seconds=1))
    assert second.claimed_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["agent_id", "caller_id", "attempt_id", "run_id", "reminder_id", "receipt_token", "id"])
async def test_foreign_identity_cannot_complete_the_original_run(tmp_path, key):
    _, outbox, _, task, payload, _ = await setup_delivery(tmp_path)
    receipt = shown_receipt(payload)
    receipt[key] = "foreign"
    try:
        with pytest.raises(ValueError):
            await outbox.resolve_native(receipt)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_native_worker_does_not_consume_a_due_reminder_without_an_available_desktop(tmp_path):
    service = ReminderService(ReminderRepository(tmp_path))
    await service.set_settings(ReminderSettings(native_notification_policy="explicit_only"))
    await service.create_reminder(ReminderCreateRequest(title="Synthetic", due_at=NOW))
    outbox = ReminderNotificationOutbox(service.repository, "a")
    worker = ReminderDeliveryWorker(service, agent_id="a", native_only=True,
        notifier=NativeReminderNotifier(outbox, timeout_seconds=.02))
    tick = await worker.run_once(now=NOW)
    assert tick.claimed_count == 0
    assert not RuntimeRunStore(tmp_path).list_contracts()


@pytest.mark.asyncio
async def test_native_deadline_before_ipc_is_known_not_submitted(tmp_path):
    service = ReminderService(ReminderRepository(tmp_path))
    await service.set_settings(ReminderSettings(native_notification_policy="explicit_only"))
    await service.create_reminder(ReminderCreateRequest(title="Synthetic", due_at=NOW))
    outbox = ReminderNotificationOutbox(service.repository, "a")
    await outbox.claim_native("desktop-ready")
    worker = ReminderDeliveryWorker(service, agent_id="a", native_only=True,
        notifier=NativeReminderNotifier(outbox, timeout_seconds=.02))
    assert (await worker.run_once(now=NOW)).claimed_count == 1
    claim = next(iter((await service.repository.load()).delivery_claims.values()))
    assert claim.settled and claim.status == "failed" and not claim.caller_id
    assert (await worker.run_once(now=NOW + timedelta(seconds=1))).claimed_count == 1
    claims = (await service.repository.load()).delivery_claims.values()
    assert len(claims) == 2 and all(c.settled and c.status == "failed" for c in claims)


@pytest.mark.asyncio
async def test_expired_or_foreign_desktop_presence_does_not_consume_due_reminders(tmp_path):
    service = ReminderService(ReminderRepository(tmp_path))
    await service.set_settings(ReminderSettings(native_notification_policy="explicit_only"))
    await service.create_reminder(ReminderCreateRequest(title="Synthetic", due_at=NOW))
    outbox = ReminderNotificationOutbox(service.repository, "foreign")
    await outbox.claim_native("foreign-desktop")
    worker = ReminderDeliveryWorker(service, agent_id="a", native_only=True,
        notifier=NativeReminderNotifier(outbox, timeout_seconds=.02))
    assert (await worker.run_once(now=NOW)).claimed_count == 0
    outbox = ReminderNotificationOutbox(service.repository, "a")
    await outbox.claim_native("original-desktop")
    await service.repository.update(lambda data: data.model_copy(update={
        "native_adapter_ready_until": datetime.now(timezone.utc) - timedelta(seconds=1),
    }))
    assert (await worker.run_once(now=NOW)).claimed_count == 0
    assert not RuntimeRunStore(tmp_path).list_contracts()


@pytest.mark.asyncio
async def test_unknown_receipt_stays_visible_and_manual_snooze_is_a_new_intent(tmp_path):
    service, outbox, worker, task, payload, item = await setup_delivery(tmp_path)
    receipt = shown_receipt(payload) | {"ok": False, "shown": False, "status": "unknown"}
    await outbox.resolve_native(receipt)
    await task
    original = await RuntimeRunStore(tmp_path).load_contract(payload["run_id"])
    assert original.status == "stalled"
    visible = (await service.list_reminders())[0]
    assert visible.metadata["runtime"]["delivery_status"] == "unknown"
    assert (await worker.run_once(now=NOW + timedelta(days=1))).claimed_count == 0
    await service.snooze_reminder(item.id, NOW + timedelta(days=2))
    await outbox.claim_native("desktop-a")
    worker._notifier = lambda reminder, trigger: {"ok": False, "shown": False, "status": "failed"}
    assert (await worker.run_once(now=NOW + timedelta(days=2))).claimed_count == 1
    assert (await RuntimeRunStore(tmp_path).load_contract(payload["run_id"])).status == "stalled"


@pytest.mark.asyncio
async def test_terminal_write_failure_retains_owner_until_stop_retry(tmp_path, monkeypatch):
    service = ReminderService(ReminderRepository(tmp_path))
    await service.create_reminder(ReminderCreateRequest(title="Synthetic", due_at=NOW))
    calls = []
    worker = ReminderDeliveryWorker(service, agent_id="a", notifier=lambda item, trigger: calls.append(trigger.run_id))
    from lantai.app.reminders import notification_outbox as module
    original = module.record_reminder_runtime_run_settled

    async def fail(**kwargs):
        raise OSError("synthetic terminal persistence unavailable")

    monkeypatch.setattr(module, "record_reminder_runtime_run_settled", fail)
    with pytest.raises(OSError):
        await worker.run_once(now=NOW)
    assert worker._pending_owner and len(calls) == 1
    monkeypatch.setattr(module, "record_reminder_runtime_run_settled", original)
    await worker.stop()
    assert worker._pending_owner is None
    claim = (await service.repository.load()).delivery_claims[calls[0]]
    assert claim.settled and not claim.status == "submitted"


@pytest.mark.asyncio
async def test_rescheduled_claim_cannot_emit_the_obsolete_notification(tmp_path):
    service, outbox, _, task, payload, item = await setup_delivery(tmp_path)
    # Return this first claimed effect to the pre-IPC state for this boundary.
    claim = await outbox.get(payload["run_id"])
    await outbox.mutate(claim.run_id, claim.owner_id, caller_id="", status="attempting")
    await service.snooze_reminder(item.id, NOW + timedelta(hours=4))
    try:
        assert await outbox.claim_native("late-desktop") is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_terminal_authority_cannot_be_rewritten_as_success(tmp_path):
    from lantai.app.reminders.runtime_runs import record_reminder_runtime_run_settled
    service = ReminderService(ReminderRepository(tmp_path))
    item = await service.create_reminder(ReminderCreateRequest(title="Synthetic", due_at=NOW))
    await record_reminder_runtime_run_settled(workspace_dir=tmp_path, agent_id="a",
        reminder=item, run_id="synthetic-terminal", final_status="failed")
    with pytest.raises(ValueError):
        await record_reminder_runtime_run_settled(workspace_dir=tmp_path, agent_id="a",
            reminder=item, run_id="synthetic-terminal", final_status="succeeded")
    with pytest.raises(ValueError):
        await record_reminder_runtime_run_settled(workspace_dir=tmp_path, agent_id="a",
            reminder=item, run_id="synthetic-terminal", final_status="running")


@pytest.mark.asyncio
async def test_http_receipts_are_strict_and_bound_to_authenticated_agent(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import httpx
    from fastapi import FastAPI
    from lantai.app.reminders import api
    _, outbox, _, task, payload, _ = await setup_delivery(tmp_path)
    authenticated_agent = "a"

    async def get_workspace(request):
        return SimpleNamespace(workspace_dir=tmp_path, agent_id=authenticated_agent)

    monkeypatch.setattr(api, "get_agent_for_request", get_workspace)
    app = FastAPI()
    app.include_router(api.router)
    receipt = shown_receipt(payload)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            settings = (await client.get("/reminders/settings")).json()
            assert settings["native_notification_policy"] == "explicit_only"
            assert (await client.post("/reminders/native/resolve", json=receipt | {"ok": "true"})).status_code == 422
            authenticated_agent = "foreign"
            assert (await client.post("/reminders/native/resolve", json=receipt)).status_code == 409
            authenticated_agent = "a"
            assert (await client.post("/reminders/native/resolve", json=receipt)).status_code == 200
        assert (await task).delivered_count == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
