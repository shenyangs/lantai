"""Actual reminder/store/controller boundaries; synthetic temporary data only."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import shutil
import threading
from datetime import datetime, timedelta, timezone

import pytest

from lantai.app.reminders.delivery import ReminderDeliveryWorker
from lantai.app.reminders.models import ReminderCreateRequest
from lantai.app.reminders.repo import ReminderRepository
from lantai.app.reminders.service import ReminderService
from lantai.runtime.runs import TERMINAL_RUNTIME_RUN_STATUSES
from lantai.runtime.store import RuntimeRunStore

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


async def due_service(path, count=3):
    service = ReminderService(ReminderRepository(path))
    for index in range(count):
        await service.create_reminder(ReminderCreateRequest(
            title=f"Synthetic only {index}", due_at=NOW - timedelta(minutes=1)))
    return service


def assert_terminal_non_success(path, count):
    runs = [run for run in RuntimeRunStore(path).list_contracts() if run.source == "reminder"]
    assert len(runs) == count
    states = [(run.status, run.settled_at is not None) for run in runs]
    assert all(run.status in TERMINAL_RUNTIME_RUN_STATUSES and run.status != "succeeded"
               and run.settled_at is not None for run in runs), states


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", [
    {"ok": True, "shown": False, "reason": "timeout"},
    {"ok": True, "status": "unknown"},
    {"ok": True, "shown": False, "reason": "failed"},
])
async def test_timeout_failed_unknown_receipt_is_not_delivered(tmp_path, receipt):
    service = await due_service(tmp_path, 1)
    worker = ReminderDeliveryWorker(service, agent_id="synthetic-agent",
                                    notifier=lambda reminder, trigger: receipt)
    tick = await worker.run_once(now=NOW)
    run = await RuntimeRunStore(tmp_path).load_contract(tick.results[0].run_id)
    assert tick.delivered_count == 0 and run.status != "succeeded", (
        tick.model_dump(mode="json"), run.status)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["no_event", "failed"])
async def test_actual_native_unconfirmed_result_is_not_a_delivery_receipt(tmp_path, mode):
    # The existing console endpoint is not wired to reminders.  This feeds its
    # actual result into the callback boundary to test the proposed adapter.
    completed = subprocess.run([os.environ.get("LANTAI_NODE_BIN") or shutil.which("node") or "node", str(Path(__file__).with_name(
        "reminder_native_notification_probe.cjs")), mode], capture_output=True, text=True,
        check=True, timeout=5, env=os.environ.copy())
    probe = json.loads(completed.stdout)
    assert "show" not in probe["events"]
    service = await due_service(tmp_path, 1)
    tick = await ReminderDeliveryWorker(service, agent_id="synthetic-agent",
        notifier=lambda reminder, trigger: probe["result"]).run_once(now=NOW)
    run = await RuntimeRunStore(tmp_path).load_contract(tick.results[0].run_id)
    assert tick.delivered_count == 0 and run.status != "succeeded", (probe, run.status)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_cancellation_settles_unattempted_batch_claims(tmp_path, cancel_count):
    service = await due_service(tmp_path)
    entered = asyncio.Event()
    calls = []

    async def notifier(reminder, trigger):
        calls.append(trigger.run_id)
        entered.set()
        await asyncio.Event().wait()

    worker = ReminderDeliveryWorker(service, agent_id="synthetic-agent", notifier=notifier)
    task = asyncio.create_task(worker.run_once(now=NOW))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert len(RuntimeRunStore(tmp_path).list_contracts()) == 3
        for _ in range(cancel_count):
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert len(calls) == 1, "stopping must not attempt the other claimed notifications"
        assert_terminal_non_success(tmp_path, 3)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_read_failure_after_claim_settles_the_whole_batch(tmp_path, monkeypatch):
    service = await due_service(tmp_path)
    calls = []

    async def failed_read():
        raise OSError("synthetic post-claim read failure")

    monkeypatch.setattr(service, "list_reminders", failed_read)
    worker = ReminderDeliveryWorker(service, agent_id="synthetic-agent",
        notifier=lambda reminder, trigger: calls.append(trigger.run_id))
    try:
        await worker.run_once(now=NOW)
    except OSError as error:
        assert str(error) == "synthetic post-claim read failure"
    assert calls == []
    assert_terminal_non_success(tmp_path, 3)


@pytest.mark.asyncio
async def test_cancelled_background_claim_write_cannot_leave_a_dedupe_only_claim(tmp_path, monkeypatch):
    service = await due_service(tmp_path, 1)
    write_entered = threading.Event()
    release_write = threading.Event()
    write_finished = threading.Event()
    original = ReminderRepository._write_model

    def gated_write(path, data):
        is_claim = any(item.metadata.get("runtime", {}).get("last_invocation_id")
                       for item in data.reminders)
        if is_claim:
            write_entered.set()
            if not release_write.wait(3):
                raise TimeoutError("synthetic claim barrier was not released")
        try:
            return original(path, data)
        finally:
            if is_claim:
                write_finished.set()

    monkeypatch.setattr(ReminderRepository, "_write_model", staticmethod(gated_write))
    task = asyncio.create_task(ReminderDeliveryWorker(service, agent_id="synthetic-agent",
        notifier=lambda reminder, trigger: None).run_once(now=NOW))
    try:
        assert await asyncio.to_thread(write_entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        release_write.set()
        await asyncio.gather(task, return_exceptions=True)
        assert await asyncio.to_thread(write_finished.wait, 2)
        monkeypatch.setattr(ReminderRepository, "_write_model", staticmethod(original))
        restarted = ReminderService(ReminderRepository(tmp_path))
        reminders = await restarted.list_reminders()
        run_id = reminders[0].metadata["runtime"]["last_invocation_id"]
        await ReminderDeliveryWorker(restarted, agent_id="synthetic-agent",
            notifier=lambda reminder, trigger: None).run_once(now=NOW + timedelta(seconds=1))
        run = await RuntimeRunStore(tmp_path).load_contract(run_id)
        assert run is not None and run.status in TERMINAL_RUNTIME_RUN_STATUSES and run.settled_at, (
            "background claim became durable after cancellation, but restart only deduped it", run)
    finally:
        release_write.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_process_exit_leaves_claims_for_restart_settlement(tmp_path):
    marker = tmp_path / "entered.json"
    child_source = """
import asyncio, json, os, sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from lantai.app.reminders.delivery import ReminderDeliveryWorker
from lantai.app.reminders.models import ReminderCreateRequest
from lantai.app.reminders.repo import ReminderRepository
from lantai.app.reminders.service import ReminderService
async def main():
    root = Path(sys.argv[1]); now = datetime(2026,10,4,12,tzinfo=timezone.utc)
    service = ReminderService(ReminderRepository(root))
    for i in range(3):
        await service.create_reminder(ReminderCreateRequest(title=f'Synthetic {i}', due_at=now-timedelta(minutes=1)))
    async def notifier(reminder, trigger):
        (root/'entered.json').write_text(json.dumps({'run_id':trigger.run_id}))
        os._exit(73)
    await ReminderDeliveryWorker(service,agent_id='synthetic-agent',notifier=notifier).run_once(now=now)
asyncio.run(main())
"""
    exited = subprocess.run([sys.executable, "-c", child_source,
        str(tmp_path)], env=os.environ.copy(), capture_output=True, text=True, timeout=8)
    assert exited.returncode == 73 and marker.exists(), exited.stderr
    assert len(RuntimeRunStore(tmp_path).list_contracts()) == 3

    async def restart():
        service = ReminderService(ReminderRepository(tmp_path))
        calls = []
        await ReminderDeliveryWorker(service, agent_id="synthetic-agent",
            notifier=lambda reminder, trigger: calls.append(trigger.run_id)).run_once(
                now=NOW + timedelta(seconds=1))
        assert calls == [], "an uncertain native attempt must not be blindly duplicated on restart"
        assert_terminal_non_success(tmp_path, 3)

    asyncio.run(restart())


@pytest.mark.asyncio
async def test_explicit_notification_exception_settles_all_attempted_claims(tmp_path):
    service = await due_service(tmp_path)
    calls = []

    def rejected(reminder, trigger):
        calls.append(trigger.run_id)
        raise RuntimeError("synthetic notification rejected")

    tick = await ReminderDeliveryWorker(service, agent_id="synthetic-agent",
        notifier=rejected).run_once(now=NOW)
    assert tick.failed_count == 3 and tick.delivered_count == 0 and len(calls) == 3
    assert_terminal_non_success(tmp_path, 3)
