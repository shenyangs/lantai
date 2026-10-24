#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke evidence for restart-safe reminder delivery."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Literal

from lantai.app.reminders.delivery import ReminderDeliveryWorker
from lantai.app.reminders.models import ReminderCreateRequest
from lantai.app.reminders.repo import ReminderRepository
from lantai.app.reminders.service import ReminderService
from lantai.runtime.store import RuntimeRunStore

SCHEMA = "com.lantai.reminder-delivery-smoke.v1"
REQUIRED_CHECKS = {
    "due_reminder_delivered",
    "runtime_run_settled",
    "restart_deduped",
    "no_duplicate_notification",
}
NOW = datetime(2026, 7, 17, 9, 30, tzinfo=timezone.utc)
RuntimeReleaseEditionText = Literal["official", "mas"]


@contextmanager
def _release_environment(
    release_edition: RuntimeReleaseEditionText,
) -> Iterator[None]:
    original_app_store = os.environ.get("LANTAI_APP_STORE")
    original_channel = os.environ.get("LANTAI_DISTRIBUTION_CHANNEL")
    try:
        if release_edition == "mas":
            os.environ["LANTAI_APP_STORE"] = "1"
            os.environ["LANTAI_DISTRIBUTION_CHANNEL"] = "mas"
        else:
            os.environ.pop("LANTAI_APP_STORE", None)
            os.environ["LANTAI_DISTRIBUTION_CHANNEL"] = "official"
        yield
    finally:
        if original_app_store is None:
            os.environ.pop("LANTAI_APP_STORE", None)
        else:
            os.environ["LANTAI_APP_STORE"] = original_app_store
        if original_channel is None:
            os.environ.pop("LANTAI_DISTRIBUTION_CHANNEL", None)
        else:
            os.environ["LANTAI_DISTRIBUTION_CHANNEL"] = original_channel


async def _run_smoke(
    workspace: Path,
    *,
    agent_id: str,
) -> dict[str, object]:
    service = ReminderService(ReminderRepository(workspace))
    reminder = await service.create_reminder(
        ReminderCreateRequest(
            title="Runtime migration reminder smoke",
            summary=(
                "Prove due reminder delivery survives backend restart ded"
                "upe."
            ),
            priority="high",
            due_at=NOW - timedelta(minutes=1),
        ),
    )
    deliveries: list[dict[str, object]] = []

    async def notifier(reminder_item, trigger) -> None:
        deliveries.append(
            {
                "reminder_id": reminder_item.id,
                "run_id": trigger.run_id,
                "triggered_at": trigger.triggered_at.isoformat(),
            },
        )

    first_worker = ReminderDeliveryWorker(
        service,
        agent_id=agent_id,
        notifier=notifier,
        injected_test_mode=True,
        dedupe_window=timedelta(hours=2),
    )
    first_tick = await first_worker.run_once(now=NOW)

    run_id = first_tick.results[0].run_id if first_tick.results else None
    first_contract = (
        await RuntimeRunStore(workspace).load_contract(run_id)
        if run_id is not None
        else None
    )

    restarted_service = ReminderService(ReminderRepository(workspace))
    restarted_worker = ReminderDeliveryWorker(
        restarted_service,
        agent_id=agent_id,
        notifier=notifier,
        injected_test_mode=True,
        dedupe_window=timedelta(hours=2),
    )
    restart_tick = await restarted_worker.run_once(
        now=NOW + timedelta(minutes=30),
    )
    contracts = [
        contract
        for contract in RuntimeRunStore(workspace).list_contracts()
        if contract.source == "reminder"
    ]

    checks = {
        "due_reminder_delivered": (
            first_tick.claimed_count == 1
            and first_tick.delivered_count == 1
            and first_tick.failed_count == 0
            and len(deliveries) == 1
        ),
        "runtime_run_settled": (
            first_contract is not None
            and first_contract.status == "succeeded"
            and first_contract.settled_at is not None
        ),
        "restart_deduped": (
            restart_tick.claimed_count == 0
            and restart_tick.delivered_count == 0
            and restart_tick.deduped_count == 1
            and bool(restart_tick.results)
            and restart_tick.results[0].run_id == run_id
        ),
        "no_duplicate_notification": (
            len(deliveries) == 1
            and len(contracts) == 1
            and contracts[0].run_id == run_id
        ),
    }

    return {
        "schema": SCHEMA,
        "adapter_kind": "injected-test",
        "passed": all(checks.values()),
        "checks": checks,
        "workspace_dir": str(workspace),
        "agent_id": agent_id,
        "reminder_id": reminder.id,
        "delivery_count": len(deliveries),
        "deliveries": deliveries,
        "first_tick": first_tick.model_dump(mode="json"),
        "restart_tick": restart_tick.model_dump(mode="json"),
        "runtime_runs": [
            {
                "run_id": contract.run_id,
                "status": contract.status,
                "revision": contract.revision,
                "release_edition": contract.release_edition,
                "settled": contract.settled_at is not None,
            }
            for contract in contracts
        ],
    }


def run_reminder_delivery_smoke(
    *,
    output: Path,
    workspace: Path | None = None,
    agent_id: str = "agent-default",
    release_edition: RuntimeReleaseEditionText = "official",
) -> Path:
    output = output.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)

    with _release_environment(release_edition):
        if workspace is None:
            with tempfile.TemporaryDirectory(
                prefix="lantai-reminder-delivery-smoke-",
            ) as tmp_dir:
                payload = asyncio.run(
                    _run_smoke(Path(tmp_dir) / "workspace", agent_id=agent_id),
                )
        else:
            payload = asyncio.run(
                _run_smoke(workspace.expanduser(), agent_id=agent_id),
            )

    payload["release_edition"] = release_edition
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--agent-id", default="agent-default")
    parser.add_argument(
        "--release-edition",
        choices=["official", "mas"],
        default="official",
    )
    args = parser.parse_args(argv)

    output = run_reminder_delivery_smoke(
        output=args.output,
        workspace=args.workspace,
        agent_id=args.agent_id,
        release_edition=args.release_edition,
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
