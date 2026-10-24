# -*- coding: utf-8 -*-
"""Workspace lifecycle service for reminder delivery."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Mapping

from .delivery import ReminderDeliveryWorker, ReminderNotifier
from .models import ReminderItem, ReminderTriggerResult, utc_now
from .notification_outbox import NativeReminderNotifier, ReminderNotificationOutbox
from .repo import ReminderRepository
from .service import DEFAULT_REMINDER_DEDUPE_WINDOW, ReminderService

REMINDER_DELIVERY_WORKER_ENV = "LANTAI_REMINDER_DELIVERY_WORKER"
REMINDER_DELIVERY_INTERVAL_ENV = "LANTAI_REMINDER_DELIVERY_INTERVAL_SECONDS"
REMINDER_DELIVERY_LIMIT_ENV = "LANTAI_REMINDER_DELIVERY_LIMIT"
REMINDER_DELIVERY_DEDUPE_ENV = "LANTAI_REMINDER_DELIVERY_DEDUPE_SECONDS"
REMINDER_DELIVERY_SMOKE_FILE_ENV = "LANTAI_REMINDER_DELIVERY_SMOKE_FILE"
REMINDER_DELIVERY_TEST_MODE_ENV = "LANTAI_REMINDER_DELIVERY_TEST_MODE"

logger = logging.getLogger(__name__)


def _env_flag(
    env: Mapping[str, str],
    name: str,
    *,
    default: bool = False,
) -> bool:
    raw = str(env.get(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_float(
    env: Mapping[str, str],
    name: str,
    *,
    default: float,
    minimum: float,
) -> float:
    try:
        value = float(str(env.get(name, "")).strip())
    except ValueError:
        return default
    return value if value >= minimum else default


def _env_int(
    env: Mapping[str, str],
    name: str,
    *,
    default: int,
    minimum: int,
) -> int:
    try:
        value = int(str(env.get(name, "")).strip())
    except ValueError:
        return default
    return value if value >= minimum else default


def reminder_delivery_worker_enabled(env: Mapping[str, str] | None = None) -> bool:
    return _env_flag(os.environ if env is None else env, REMINDER_DELIVERY_WORKER_ENV)


class ReminderDeliveryWorkspaceService:
    """Optional per-workspace background worker for due reminder delivery."""

    def __init__(
        self,
        *,
        workspace_dir: str | Path,
        agent_id: str,
        env: Mapping[str, str] | None = None,
        notifier: ReminderNotifier | None = None,
        native_notifications: bool = False,
    ) -> None:
        self.workspace_dir = Path(workspace_dir).expanduser()
        self.agent_id = agent_id
        self._env = os.environ if env is None else env
        self._notifier = notifier
        self._native_notifications = native_notifications
        self._service = ReminderService(ReminderRepository(self.workspace_dir))
        self._worker: ReminderDeliveryWorker | None = None

    @property
    def enabled(self) -> bool:
        return (reminder_delivery_worker_enabled(self._env) or self._native_notifications) and (
            self._notifier is not None or self._smoke_mode_enabled or self._native_notifications
        )

    @property
    def _smoke_mode_enabled(self) -> bool:
        return _env_flag(self._env, REMINDER_DELIVERY_TEST_MODE_ENV) and bool(
            str(self._env.get(REMINDER_DELIVERY_SMOKE_FILE_ENV, "")).strip()
        )

    @property
    def running(self) -> bool:
        return self._worker is not None and self._worker.running

    async def start(self) -> None:
        if self.running:
            return
        if not self.enabled:
            logger.error(
                "Reminder delivery worker is disabled: notification adapter is not configured"
            )
            return
        if self._worker is None:
            self._worker = self._build_worker()
        self._worker.start()

    async def stop(self) -> None:
        worker = self._worker
        if worker is None:
            return
        await worker.stop()
        self._worker = None

    async def run_once(
        self,
        *,
        now: datetime | None = None,
    ):
        if self._worker is None:
            self._worker = self._build_worker()
        return await self._worker.run_once(now=now)

    def _build_worker(self) -> ReminderDeliveryWorker:
        notifier = self._notifier
        if notifier is None and self._smoke_mode_enabled:
            notifier = self._record_delivery_for_smoke
        native_only = notifier is None and self._native_notifications
        if native_only:
            notifier = NativeReminderNotifier(ReminderNotificationOutbox(self._service.repository, self.agent_id))
        if notifier is None:
            raise RuntimeError("notification adapter is not configured")
        dedupe_seconds = _env_float(
            self._env,
            REMINDER_DELIVERY_DEDUPE_ENV,
            default=DEFAULT_REMINDER_DEDUPE_WINDOW.total_seconds(),
            minimum=1.0,
        )
        interval_seconds = _env_float(
            self._env,
            REMINDER_DELIVERY_INTERVAL_ENV,
            default=1.0 if native_only else 60.0,
            minimum=0.05,
        )
        limit = _env_int(
            self._env,
            REMINDER_DELIVERY_LIMIT_ENV,
            default=50,
            minimum=1,
        )
        return ReminderDeliveryWorker(
            self._service,
            agent_id=self.agent_id,
            notifier=notifier,
            tick_observer=self._record_tick_for_smoke,
            dedupe_window=timedelta(seconds=dedupe_seconds),
            limit=limit,
            interval_seconds=interval_seconds,
            now_provider=utc_now,
            native_only=native_only,
            injected_test_mode=self._notifier is None and self._smoke_mode_enabled,
        )

    async def _record_delivery_for_smoke(
        self,
        reminder: ReminderItem,
        trigger: ReminderTriggerResult,
    ) -> None:
        smoke_file = str(self._env.get(REMINDER_DELIVERY_SMOKE_FILE_ENV, "")).strip()
        if not smoke_file:
            return
        path = Path(smoke_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "kind": "reminder_delivery",
            "agent_id": self.agent_id,
            "workspace_dir": str(self.workspace_dir),
            "reminder_id": reminder.id,
            "run_id": trigger.run_id,
            "triggered_at": trigger.triggered_at.isoformat(),
            "recorded_at": utc_now().isoformat(),
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            handle.write("\n")

    async def _record_tick_for_smoke(self, tick) -> None:
        smoke_file = str(self._env.get(REMINDER_DELIVERY_SMOKE_FILE_ENV, "")).strip()
        if not smoke_file:
            return
        path = Path(smoke_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "kind": "reminder_delivery_tick",
            "agent_id": self.agent_id,
            "workspace_dir": str(self.workspace_dir),
            "checked_at": tick.checked_at.isoformat(),
            "claimed_count": tick.claimed_count,
            "delivered_count": tick.delivered_count,
            "deduped_count": tick.deduped_count,
            "failed_count": tick.failed_count,
            "recorded_at": utc_now().isoformat(),
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
