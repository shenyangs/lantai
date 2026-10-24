# -*- coding: utf-8 -*-
"""Lifecycle for isolated daily-brief research cycles.

The worker is deliberately separate from the chat and cron runtimes.  It only
consumes canonical cycles that already passed the brief grant/topic fences and
never targets or shares an active chat session.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

from .models import StrictModel, utc_now
from .repo import DailyBriefRepository
from .research import (
    BriefResearchAdapter,
    DailyBriefResearchRunner,
    DisabledBriefResearchAdapter,
)
from .scheduler import DailyBriefScheduler
from .service import DailyBriefService, BriefConflict

logger = logging.getLogger(__name__)

DAILY_BRIEF_WORKER_ENV = "LANTAI_DAILY_BRIEF_WORKER"
DAILY_BRIEF_INTERVAL_ENV = "LANTAI_DAILY_BRIEF_INTERVAL_SECONDS"
DAILY_BRIEF_LIMIT_ENV = "LANTAI_DAILY_BRIEF_LIMIT"
DAILY_BRIEF_TIMEOUT_ENV = "LANTAI_DAILY_BRIEF_TIMEOUT_SECONDS"


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


def daily_brief_worker_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Run the in-process scheduler unless an operator explicitly opts out."""

    return _env_flag(
        env if env is not None else os.environ,
        DAILY_BRIEF_WORKER_ENV,
        default=True,
    )


class BriefCycleRunResult(StrictModel):
    cycle_id: str
    status: Literal["published", "failed"]
    edition_id: str | None = None
    error: str = ""


class DailyBriefTickResult(StrictModel):
    checked_at: datetime
    claimed_preview_count: int = 0
    claimed_subscription_count: int = 0
    published_count: int = 0
    failed_count: int = 0
    results: list[BriefCycleRunResult]


class DailyBriefWorker:
    """Run bounded canonical cycles with one isolated adapter."""

    def __init__(
        self,
        workspace_dir: str | Path,
        *,
        adapter: BriefResearchAdapter | DisabledBriefResearchAdapter | None = None,
        limit: int = 4,
        interval_seconds: float = 60.0,
        timeout_seconds: float = 120.0,
        now_provider: Callable[[], datetime] = utc_now,
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be at least one")
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be greater than zero")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self._repo = DailyBriefRepository(workspace_dir)
        self._service = DailyBriefService(workspace_dir)
        self._scheduler = DailyBriefScheduler(workspace_dir)
        self._runner = DailyBriefResearchRunner(workspace_dir, adapter=adapter)
        self._limit = limit
        self._interval_seconds = interval_seconds
        self._timeout_seconds = timeout_seconds
        self._now_provider = now_provider
        self._run_lock = asyncio.Lock()
        self._wake_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.last_tick_at: datetime | None = None

    @property
    def running(self) -> bool:
        task = self._task
        return task is not None and not task.done()

    async def run_once(self, *, now: datetime | None = None) -> DailyBriefTickResult:
        checked_at = now or self._now_provider()
        if checked_at.tzinfo is None:
            raise ValueError("worker time must be timezone-aware")
        checked_at = checked_at.astimezone(timezone.utc)
        self.last_tick_at = checked_at

        async with self._run_lock:
            await self._service.recover_interrupted_cycles()
            store = await self._repo.load()
            if not store.feature_flags.research:
                return DailyBriefTickResult(checked_at=checked_at, results=[])

            queued_previews = sorted(
                (
                    cycle
                    for cycle in store.cycles
                    if cycle.status == "queued"
                ),
                key=lambda cycle: cycle.requested_at,
            )[: self._limit]
            results: list[BriefCycleRunResult] = []
            for cycle in queued_previews:
                results.append(await self._run_cycle(cycle.id, preview=True))

            remaining = self._limit - len(queued_previews)
            claimed_subscriptions = []
            # A sequential worker claims just before execution. A waiting
            # subscription must not spend its lease while another is running.
            for _ in range(remaining):
                claimed = await self._scheduler.tick(now=checked_at, limit=1)
                if not claimed:
                    break
                cycle = claimed[0]
                claimed_subscriptions.append(cycle)
                results.append(await self._run_cycle(cycle.id, preview=False))

            return DailyBriefTickResult(
                checked_at=checked_at,
                claimed_preview_count=len(queued_previews),
                claimed_subscription_count=len(claimed_subscriptions),
                published_count=sum(item.status == "published" for item in results),
                failed_count=sum(item.status == "failed" for item in results),
                results=results,
            )

    async def _run_cycle(
        self,
        cycle_id: str,
        *,
        preview: bool,
    ) -> BriefCycleRunResult:
        try:
            edition = await asyncio.wait_for(
                (
                    self._run_queued(cycle_id)
                    if preview
                    else self._runner.run_claimed_cycle(cycle_id)
                ),
                timeout=self._timeout_seconds,
            )

            return BriefCycleRunResult(
                cycle_id=cycle_id,
                status="published",
                edition_id=edition.id,
            )
        except Exception as exc:  # The persisted cycle remains the authority.
            reason = f"{type(exc).__name__}: {exc}"
            if not isinstance(exc, BriefConflict):
                await self._service.fail_cycle(cycle_id, reason)
            return BriefCycleRunResult(
                cycle_id=cycle_id,
                status="failed",
                error=reason[:500],
            )

    async def _run_queued(self, cycle_id: str):
        await self._service.claim_queued_cycle(cycle_id)
        return await self._runner.run_claimed_cycle(cycle_id)

    def start(self) -> None:
        if self.running:
            return
        self._wake_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._run_loop(),
            name="daily-brief-worker",
        )

    def wake(self) -> None:
        """Wake an already authorized worker after an explicit queue action."""

        if self.running:
            self._wake_event.set()

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self._task = None

    async def _run_loop(self) -> None:
        while True:
            self._wake_event.clear()
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # Keep the failure visible without killing future ticks.
                logger.exception("Daily brief worker tick failed")
            try:
                await asyncio.wait_for(
                    self._wake_event.wait(),
                    timeout=self._interval_seconds,
                )
            except asyncio.TimeoutError:
                continue


class DailyBriefWorkspaceService:
    """Consent-gated per-workspace lifecycle for isolated brief cycles."""

    def __init__(
        self,
        *,
        workspace_dir: str | Path,
        agent_id: str,
        env: Mapping[str, str] | None = None,
        adapter: BriefResearchAdapter | DisabledBriefResearchAdapter | None = None,
        mcp_manager: Any | None = None,
    ) -> None:
        self.workspace_dir = Path(workspace_dir).expanduser()
        self.agent_id = agent_id
        self._env = env if env is not None else os.environ
        self._adapter = adapter
        self._mcp_manager = mcp_manager
        self._worker: DailyBriefWorker | None = None

    @property
    def enabled(self) -> bool:
        return daily_brief_worker_enabled(self._env)

    @property
    def running(self) -> bool:
        return self._worker is not None and self._worker.running

    @property
    def last_tick_at(self) -> datetime | None:
        return self._worker.last_tick_at if self._worker is not None else None

    async def start(self) -> None:
        if not self.enabled or self.running:
            return
        self._worker = self._build_worker()
        self._worker.start()

    async def stop(self) -> None:
        worker = self._worker
        if worker is None:
            return
        await worker.stop()
        self._worker = None

    async def run_once(self, *, now: datetime | None = None) -> DailyBriefTickResult:
        worker = self._worker or self._build_worker()
        return await worker.run_once(now=now)

    def wake(self) -> None:
        if self.enabled and self._worker is not None:
            self._worker.wake()

    def _build_worker(self) -> DailyBriefWorker:
        if self._adapter is None:
            from .production_research import (
                GuanlanFirstBriefSearch,
                ProductionBriefResearchAdapter,
            )

            adapter: BriefResearchAdapter | DisabledBriefResearchAdapter = (
                ProductionBriefResearchAdapter(
                    agent_id=self.agent_id,
                    search=GuanlanFirstBriefSearch(
                        mcp_manager=self._mcp_manager,
                    ),
                )
            )
        else:
            adapter = self._adapter
        return DailyBriefWorker(
            self.workspace_dir,
            adapter=adapter,
            limit=_env_int(
                self._env,
                DAILY_BRIEF_LIMIT_ENV,
                default=4,
                minimum=1,
            ),
            interval_seconds=_env_float(
                self._env,
                DAILY_BRIEF_INTERVAL_ENV,
                default=60.0,
                minimum=0.05,
            ),
            timeout_seconds=_env_float(
                self._env,
                DAILY_BRIEF_TIMEOUT_ENV,
                default=120.0,
                minimum=0.001,
            ),
        )
