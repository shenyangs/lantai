# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from typing import TYPE_CHECKING, Any, Dict, Optional, Union

from apscheduler.events import EVENT_SCHEDULER_SHUTDOWN
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from lantai.exceptions import ConfigurationException

from ...config import get_heartbeat_config, get_dream_cron
from ...utils.async_settlement import await_settled

from ..concurrency import get_runtime_concurrency_governor
from ..console_push_store import append as push_store_append
from ..experience import (
    record_cron_job_settled,
    record_cron_job_started,
    record_heartbeat_run_settled,
    record_heartbeat_run_started,
    touch_cron_runtime_run_heartbeat,
)
from .executor import CronExecutor
from .heartbeat import (
    is_cron_expression,
    parse_heartbeat_cron,
    parse_heartbeat_every,
    run_heartbeat_once,
)
from .models import (
    CronJobSpec,
    CronJobState,
    CronOccurrence,
    CronOccurrenceWindow,
)
from .repo.base import BaseJobRepository

if TYPE_CHECKING:
    from ..runner.task_tracker import SessionRuntimeRegistry


HEARTBEAT_JOB_ID = "_heartbeat"
DREAM_JOB_ID = "_dream"
_CRON_RUNTIME_HEARTBEAT_INTERVAL_SECONDS = 10.0

logger = logging.getLogger(__name__)


@dataclass
class _Runtime:
    sem: asyncio.Semaphore


async def _cron_runtime_heartbeat_loop(
    *,
    workspace_dir: Path | str,
    run_id: str,
    interval_seconds: float,
) -> None:
    if interval_seconds <= 0:
        return
    while True:
        await asyncio.sleep(interval_seconds)
        touched = await touch_cron_runtime_run_heartbeat(
            workspace_dir=workspace_dir,
            run_id=run_id,
        )
        if touched is None:
            return


class CronManager:
    def __init__(
        self,
        *,
        repo: BaseJobRepository,
        runner: Any,
        channel_manager: Any,
        timezone: str = "UTC",  # pylint: disable=redefined-outer-name
        agent_id: Optional[str] = None,
        task_tracker: SessionRuntimeRegistry | None = None,
    ):
        self._repo = repo
        self._runner = runner
        self._channel_manager = channel_manager
        self._agent_id = agent_id
        self._task_tracker = task_tracker
        self._scheduler = AsyncIOScheduler(timezone=timezone)
        self._executor = CronExecutor(
            runner=runner,
            channel_manager=channel_manager,
            agent_id=agent_id,
        )

        self._lock = asyncio.Lock()
        self._states: Dict[str, CronJobState] = {}
        self._rt: Dict[str, _Runtime] = {}
        self._started = False
        # Manual invocations remain usable before the first scheduler start.
        self._accepting = True
        self._generation = 0
        self._owned_tasks: set[asyncio.Task] = set()
        self._cancel_requested_tasks: set[asyncio.Task] = set()
        self._startup_task: asyncio.Task | None = None
        self._shutdown_task: asyncio.Task | None = None
        self._active_runs: dict[str, tuple[str, asyncio.Task]] = {}
        self._stop_requested_runs: set[str] = set()

    def request_stop_run(self, run_id: str, *, expected_attempt_id: str) -> bool:
        """Cancel only this live invocation, once; settlement owns cleanup."""
        active = self._active_runs.get(run_id)
        if active is None or active[0] != expected_attempt_id or active[1].done():
            return False
        if run_id not in self._stop_requested_runs:
            self._stop_requested_runs.add(run_id)
            self._cancel_once(active[1])
        return True

    async def start(self) -> None:
        if self._shutdown_task is not None:
            await await_settled(self._shutdown_task)
        generation = self._generation
        async with self._lock:
            if generation != self._generation:
                raise RuntimeError("Cron manager stopped while waiting to start")
            if self._started:
                return
            self._startup_task = asyncio.current_task()
            self._accepting = False
            try:
                if os.environ.get("LANTAI_RECOVERY_SAFE_MODE") == "1":
                    logger.warning(
                        "Safe recovery mode: scheduled jobs and heartbeat are paused"
                    )
                    self._started = True
                    self._accepting = True
                    return
                jobs_file = await self._repo.load()

                self._scheduler.start(paused=True)
                for job in jobs_file.jobs:
                    try:
                        await self._register_or_update(job)
                    except Exception as e:  # pylint: disable=broad-except
                        logger.warning(
                            "Skipping invalid cron job during startup: "
                            "job_id=%s name=%s cron=%s error=%s",
                            job.id,
                            job.name,
                            job.schedule.cron,
                            repr(e),
                        )
                        if job.enabled:
                            disabled_job = job.model_copy(
                                update={"enabled": False},
                            )
                            await self._repo.upsert_job(disabled_job)
                            logger.warning(
                                "Auto-disabled invalid cron job: "
                                "job_id=%s name=%s",
                                job.id,
                                job.name,
                            )

                # Heartbeat: scheduled job when enabled in config
                hb = get_heartbeat_config(self._agent_id)
                if getattr(hb, "enabled", False):
                    trigger = self._build_heartbeat_trigger(hb.every)
                    self._scheduler.add_job(
                        self._heartbeat_callback,
                        trigger=trigger,
                        id=HEARTBEAT_JOB_ID,
                        replace_existing=True,
                    )
                    logger.info(
                        "Heartbeat job scheduled for agent %s: every=%s",
                        self._agent_id,
                        hb.every,
                    )

                # Dream-based memory optimization: cron job from config
                dream_cron = get_dream_cron(self._agent_id)
                if dream_cron:
                    try:
                        trigger = CronTrigger.from_crontab(
                            dream_cron,
                            timezone=self._scheduler.timezone,
                        )
                        self._scheduler.add_job(
                            self._dream_callback,
                            trigger=trigger,
                            id=DREAM_JOB_ID,
                            replace_existing=True,
                        )
                        logger.info(
                            f"Dream-based memory optimization job scheduled for "
                            f"agent {self._agent_id}: cron={dream_cron}",
                        )
                    except Exception as e:  # pylint: disable=broad-except
                        logger.error(
                            f"Failed to schedule dream-based memory optimization"
                            f"for  agent {self._agent_id}: error={repr(e)}",
                        )

                self._scheduler.resume()
                self._started = True
                self._accepting = True
            except BaseException:
                self._started = False
                self._accepting = False
                await await_settled(asyncio.create_task(
                    self._shutdown_scheduler(), name="cron-start-rollback",
                ))
                raise
            finally:
                if self._startup_task is not None:
                    self._cancel_requested_tasks.discard(self._startup_task)
                self._startup_task = None

    async def _shutdown_scheduler(self) -> None:
        if not self._scheduler.running:
            return
        closed = asyncio.Event()

        def on_shutdown(_event):
            closed.set()

        self._scheduler.add_listener(on_shutdown, EVENT_SCHEDULER_SHUTDOWN)
        try:
            self._scheduler.shutdown(wait=False)
            await closed.wait()
        finally:
            self._scheduler.remove_listener(on_shutdown)

    @asynccontextmanager
    async def _own_invocation(self) -> AsyncIterator[None]:
        task = asyncio.current_task()
        assert task is not None
        nested = task in self._owned_tasks
        if not nested:
            if not self._accepting:
                raise RuntimeError("Cron manager is stopping or stopped")
            self._owned_tasks.add(task)
        try:
            yield
        finally:
            if not nested:
                self._forget_task(task)

    async def _run_owned(
        self, operation: Callable[[], Awaitable[None]], *, name: str,
    ) -> None:
        async with self._own_invocation():
            task = asyncio.create_task(operation(), name=name)
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                self._cancel_once(task)
                try:
                    await await_settled(task)
                except (asyncio.CancelledError, Exception):
                    pass
                if not task.cancelled() and task.exception() is not None:
                    logger.error(
                        "cron callback cleanup failed: task=%s error_type=%s",
                        name, type(task.exception()).__name__,
                    )
                raise
            finally:
                self._cancel_requested_tasks.discard(task)

    def _cancel_once(self, task: asyncio.Task) -> None:
        if not task.done() and task not in self._cancel_requested_tasks:
            self._cancel_requested_tasks.add(task)
            task.cancel()

    def _forget_task(self, task: asyncio.Task) -> None:
        self._owned_tasks.discard(task)
        self._cancel_requested_tasks.discard(task)

    async def stop(self) -> None:
        # Close admission before the first await. Generation also rejects a
        # repository read that straddles stop followed by a fresh start.
        if self._shutdown_task is None or self._shutdown_task.done():
            self._accepting = False
            self._generation += 1
            if self._startup_task is not None and not self._startup_task.done():
                self._cancel_once(self._startup_task)
            self._shutdown_task = asyncio.create_task(
                self._stop_owned(), name="cron-manager-stop",
            )
        await await_settled(self._shutdown_task)

    async def _stop_owned(self) -> None:
        async with self._lock:
            self._accepting = False
            self._started = False
            if self._scheduler.running:
                self._scheduler.pause()
            tasks = tuple(self._owned_tasks)
            for task in tasks:
                self._cancel_once(task)
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    logger.error(
                        "cron shutdown cleanup failed: error_type=%s",
                        type(result).__name__,
                    )
        # APScheduler also cancels its tasks on shutdown. Drain ours first so
        # that this cannot issue a second cancellation inside their cleanup.
        await self._shutdown_scheduler()

    # ----- read/state -----

    async def list_jobs(self) -> list[CronJobSpec]:
        return await self._repo.list_jobs()

    async def list_job_occurrences(
        self,
        *,
        start_at: datetime,
        end_at: datetime,
        max_items: int = 2000,
    ) -> CronOccurrenceWindow:
        """Project enabled jobs with the scheduler's authoritative triggers.

        The total occurrence count is kept separate from the returned/rendered
        item count.  This prevents dense schedules from being represented as
        one decorative block per day while still bounding response size.
        """

        if (
            start_at.tzinfo is None
            or start_at.utcoffset() is None
            or end_at.tzinfo is None
            or end_at.utcoffset() is None
        ):
            raise ValueError("start_at and end_at must be timezone-aware")
        if end_at <= start_at:
            raise ValueError("end_at must be after start_at")
        if end_at - start_at > timedelta(days=31):
            raise ValueError("occurrence window must not exceed 31 days")
        if max_items < 1 or max_items > 5000:
            raise ValueError("max_items must be between 1 and 5000")

        jobs = [job for job in await self._repo.list_jobs() if job.enabled]
        projected: list[CronOccurrence] = []
        occurrence_count = 0
        complete = True
        unavailable_job_ids: list[str] = []
        evaluation_limit = 100_000

        for job in jobs:
            if occurrence_count >= evaluation_limit:
                complete = False
                break
            try:
                trigger = self._build_trigger(job)
            except Exception:  # pylint: disable=broad-except
                complete = False
                unavailable_job_ids.append(job.id or "")
                continue

            previous_fire_time: datetime | None = None
            cursor = start_at
            while True:
                next_fire_time = trigger.get_next_fire_time(
                    previous_fire_time,
                    cursor,
                )
                if next_fire_time is None or next_fire_time >= end_at:
                    break
                if next_fire_time < start_at:
                    complete = False
                    unavailable_job_ids.append(job.id or "")
                    break

                occurrence_count += 1
                projected.append(
                    CronOccurrence(
                        job_id=job.id or "",
                        scheduled_at=next_fire_time,
                        schedule_timezone=job.schedule.timezone,
                    ),
                )
                if occurrence_count >= evaluation_limit:
                    complete = False
                    break
                previous_fire_time = next_fire_time
                cursor = next_fire_time

        projected.sort(key=lambda item: item.scheduled_at)
        items = projected[:max_items]
        return CronOccurrenceWindow(
            start_at=start_at,
            end_at=end_at,
            rule_count=len(jobs),
            occurrence_count=occurrence_count,
            items=items,
            truncated=(len(items) < occurrence_count or not complete),
            complete=complete,
            unavailable_job_ids=unavailable_job_ids,
        )

    async def get_job(self, job_id: str) -> Optional[CronJobSpec]:
        return await self._repo.get_job(job_id)

    def get_state(self, job_id: str) -> CronJobState:
        return self._states.get(job_id, CronJobState())

    def get_dream_schedule_state(self) -> dict[str, Any]:
        """Expose the managed Dreaming job without leaking scheduler internals."""

        job = self._scheduler.get_job(DREAM_JOB_ID)
        next_run = getattr(job, "next_run_time", None) if job else None
        return {
            "scheduled": job is not None,
            "next_run_at": next_run.isoformat() if next_run is not None else "",
        }

    # ----- write/control -----

    async def create_or_replace_job(self, spec: CronJobSpec) -> None:
        async with self._lock:
            await self._repo.upsert_job(spec)
            if self._started:
                await self._register_or_update(spec)

    async def delete_job(self, job_id: str) -> bool:
        async with self._lock:
            if self._started and self._scheduler.get_job(job_id):
                self._scheduler.remove_job(job_id)
            self._states.pop(job_id, None)
            self._rt.pop(job_id, None)
            return await self._repo.delete_job(job_id)

    async def pause_job(self, job_id: str) -> None:
        async with self._lock:
            self._scheduler.pause_job(job_id)

    async def resume_job(self, job_id: str) -> None:
        async with self._lock:
            self._scheduler.resume_job(job_id)

    async def reschedule_heartbeat(self) -> None:
        """Reload heartbeat config and update or remove the heartbeat job.

        Note: CronManager should always be started during workspace
        initialization, so this method assumes self._started is True.
        """
        async with self._lock:
            if not self._started:
                logger.warning(
                    f"CronManager not started for agent {self._agent_id}, "
                    f"cannot reschedule heartbeat. This should not happen.",
                )
                return

            hb = get_heartbeat_config(self._agent_id)

            # Remove existing heartbeat job if present
            if self._scheduler.get_job(HEARTBEAT_JOB_ID):
                self._scheduler.remove_job(HEARTBEAT_JOB_ID)

            # Add heartbeat job if enabled
            if getattr(hb, "enabled", False):
                trigger = self._build_heartbeat_trigger(hb.every)
                self._scheduler.add_job(
                    self._heartbeat_callback,
                    trigger=trigger,
                    id=HEARTBEAT_JOB_ID,
                    replace_existing=True,
                )
                logger.info(
                    "heartbeat rescheduled: every=%s",
                    hb.every,
                )
            else:
                logger.info("heartbeat disabled, job removed")

    async def reschedule_dream(self) -> None:
        """Reschedule the dream-based memory optimization job based on
        configuration.

        Note: CronManager should always be started during workspace
        initialization, so this method assumes self._started is True.
        """
        async with self._lock:
            if not self._started:
                logger.warning(
                    f"CronManager not started for agent {self._agent_id}, "
                    "cannot reschedule dream-based memory optimization."
                    "This should not happen.",
                )
                return

            # Check if dream-based memory optimization is enabled in config
            dream_cron = get_dream_cron(self._agent_id)

            # Remove existing job if any
            if self._scheduler.get_job(DREAM_JOB_ID):
                self._scheduler.remove_job(DREAM_JOB_ID)
                logger.info(
                    "Dream-based memory optimization job removed for "
                    f"agent {self._agent_id}",
                )

            # Add new job if cron expression is valid
            if dream_cron:
                try:
                    trigger = CronTrigger.from_crontab(
                        dream_cron,
                        timezone=self._scheduler.timezone,
                    )
                    self._scheduler.add_job(
                        self._dream_callback,
                        trigger=trigger,
                        id=DREAM_JOB_ID,
                        replace_existing=True,
                    )
                    logger.info(
                        "Dream-based memory optimization job rescheduled"
                        f"for agent {self._agent_id}: cron={dream_cron}",
                    )
                except Exception as e:  # pylint: disable=broad-except
                    logger.error(
                        "Failed to reschedule dream-based memory  "
                        f"optimization for agent {self._agent_id}: "
                        f"error={repr(e)}",
                    )
            else:
                logger.info(
                    "dream-based memory optimization disabled, job removed",
                )

    async def run_job(self, job_id: str) -> None:
        """Trigger a job to run in the background (fire-and-forget).

        Raises KeyError if the job does not exist.
        The actual execution happens asynchronously; errors are logged
        and reflected in the job state but NOT propagated to the caller.
        """
        generation = self._generation
        if not self._accepting:
            raise RuntimeError("Cron manager is stopping or stopped")
        job = await self._repo.get_job(job_id)
        if not job:
            raise KeyError(f"Job not found: {job_id}")
        logger.info(
            "cron run_job (async): job_id=%s channel=%s task_type=%s "
            "target_user_id=%s target_session_id=%s",
            job_id,
            job.dispatch.channel,
            job.task_type,
            (job.dispatch.target.user_id or "")[:40],
            (job.dispatch.target.session_id or "")[:40],
        )
        async with self._lock:
            if not self._accepting or generation != self._generation:
                raise RuntimeError("Cron manager stopped while reading job")
            task = asyncio.create_task(
                self._execute_manual_once(job), name=f"cron-run-{job_id}",
            )
            self._owned_tasks.add(task)
            task.add_done_callback(self._forget_task)
            task.add_done_callback(lambda t: self._task_done_cb(t, job))

    # ----- callbacks -----

    async def _execute_manual_once(self, job: CronJobSpec) -> None:
        try:
            await self._execute_once(job)
        except Exception as exc:
            logger.error("cron manual job failed: job_id=%s error_type=%s", job.id, type(exc).__name__)
            session_id = job.dispatch.target.session_id
            if session_id:
                error_text = f"❌ Cron job [{job.name}] failed: {exc}"

                async def persist_receipt() -> None:
                    try:
                        await push_store_append(
                            session_id,
                            error_text,
                            sticky=True,
                            source_kind="cron_job",
                            source_id=job.id,
                            source_name=job.name,
                        )
                    except Exception as error:
                        logger.error("cron failure receipt failed: error_type=%s", type(error).__name__)
                        raise

                try:
                    await await_settled(asyncio.create_task(
                        persist_receipt(), name=f"cron-failure-receipt-{job.id}",
                    ))
                except Exception:
                    # Preserve the execution failure; the receipt failure has
                    # already been recorded without detaching its persistence.
                    pass
            raise

    def _task_done_cb(self, task: asyncio.Task, job: CronJobSpec) -> None:
        """Retrieve background failures; all async cleanup belongs to the task."""
        if not task.cancelled():
            task.exception()

    # ----- internal -----

    async def _register_or_update(self, spec: CronJobSpec) -> None:
        # Validate and build trigger first. If cron is invalid, fail fast
        # without mutating scheduler/runtime state.
        assert spec.id is not None, "Job must have an id"
        trigger = self._build_trigger(spec)

        # per-job concurrency semaphore
        self._rt[spec.id] = _Runtime(
            sem=asyncio.Semaphore(spec.runtime.max_concurrency),
        )

        # replace existing
        if self._scheduler.get_job(spec.id):
            self._scheduler.remove_job(spec.id)

        self._scheduler.add_job(
            self._scheduled_callback,
            trigger=trigger,
            id=spec.id,
            args=[spec.id],
            misfire_grace_time=spec.runtime.misfire_grace_seconds,
            replace_existing=True,
        )

        if not spec.enabled:
            self._scheduler.pause_job(spec.id)

        # update next_run
        aps_job = self._scheduler.get_job(spec.id)
        st = self._states.get(spec.id, CronJobState())
        st.next_run_at = aps_job.next_run_time if aps_job else None
        self._states[spec.id] = st

    def _build_trigger(self, spec: CronJobSpec) -> CronTrigger:
        # enforce 5 fields (no seconds)
        parts = [p for p in spec.schedule.cron.split() if p]
        if len(parts) != 5:
            raise ConfigurationException(
                message=(
                    f"cron must have 5 fields, "
                    f"got {len(parts)}: {spec.schedule.cron}"
                ),
            )

        minute, hour, day, month, day_of_week = parts
        return CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
            timezone=spec.schedule.timezone,
        )

    def _build_heartbeat_trigger(
        self,
        every: str,
    ) -> Union[CronTrigger, IntervalTrigger]:
        """Build a trigger from the heartbeat *every* value.

        Returns CronTrigger for cron expressions,
        IntervalTrigger for interval strings.
        """
        if is_cron_expression(every):
            minute, hour, day, month, day_of_week = parse_heartbeat_cron(every)
            return CronTrigger(
                minute=minute,
                hour=hour,
                day=day,
                month=month,
                day_of_week=day_of_week,
            )
        interval_seconds = parse_heartbeat_every(every)
        return IntervalTrigger(seconds=interval_seconds)

    async def _scheduled_callback(self, job_id: str) -> None:
        await self._run_owned(
            lambda: self._scheduled_callback_owned(job_id), name="cron_scheduled_callback",
        )

    async def _scheduled_callback_owned(self, job_id: str) -> None:
        job = await self._repo.get_job(job_id)
        if not job:
            return

        await self._execute_once(job)

        # refresh next_run
        aps_job = self._scheduler.get_job(job_id)
        st = self._states.get(job_id, CronJobState())
        st.next_run_at = aps_job.next_run_time if aps_job else None
        self._states[job_id] = st

    async def _heartbeat_callback(self) -> None:
        await self._run_owned(
            lambda: self._heartbeat_callback_owned(), name="cron_heartbeat_callback",
        )

    async def _persist_heartbeat_event(self, operation: Awaitable[object]) -> None:
        async def persist() -> None:
            try:
                await operation
            except Exception as exc:
                logger.error("heartbeat persistence failed error_type=%s", type(exc).__name__)
                raise

        await await_settled(asyncio.create_task(persist(), name="heartbeat_persistence"))

    async def _heartbeat_callback_owned(self) -> None:
        """Run one heartbeat and retain ownership through its terminal write."""
        workspace_dir = getattr(self._runner, "workspace_dir", None)
        agent_id = self._agent_id or "default"
        target = str(get_heartbeat_config(self._agent_id).target or "main") if workspace_dir else "main"
        query_path = str(Path(workspace_dir) / "HEARTBEAT.md") if workspace_dir else ""
        final_status = None
        result_reason = ""
        cancelled = False
        try:
            if workspace_dir:
                await self._persist_heartbeat_event(record_heartbeat_run_started(
                    workspace_dir=workspace_dir,
                    agent_id=agent_id,
                    target=target,
                    query_path=query_path,
                ))
            heartbeat_result = await run_heartbeat_once(
                runner=self._runner,
                channel_manager=self._channel_manager,
                agent_id=self._agent_id,
                workspace_dir=workspace_dir,
            )
            if heartbeat_result:
                target = str(heartbeat_result.get("target") or "main")
                query_path = str(heartbeat_result.get("query_path") or "")
                final_status = str(heartbeat_result.get("status") or "completed")
                result_reason = str(heartbeat_result.get("reason") or "")
        except asyncio.CancelledError:
            logger.info("heartbeat cancelled")
            cancelled = True
            final_status = result_reason = "cancelled"
            raise
        except Exception:  # pylint: disable=broad-except
            logger.exception("heartbeat run failed")
            final_status, result_reason = "error", "exception"
        finally:
            if workspace_dir and final_status:
                try:
                    await self._persist_heartbeat_event(record_heartbeat_run_settled(
                        workspace_dir=workspace_dir,
                        agent_id=agent_id,
                        target=target,
                        query_path=query_path,
                        final_status=final_status,
                        result_reason=result_reason,
                    ))
                except Exception:
                    if cancelled:
                        raise asyncio.CancelledError from None
                    raise

    async def _dream_callback(self) -> None:
        await self._run_owned(
            lambda: self._dream_callback_owned(), name="cron_dream_callback",
        )

    async def _dream_callback_owned(self) -> None:
        """Run one dream-based memory optimization task."""
        try:
            await self._runner.memory_manager.dream(trigger="scheduled")
            logger.debug("Dream task executed successfully")
        except asyncio.CancelledError:
            logger.info("Dream task was cancelled")
            raise
        except Exception as e:  # pylint: disable=broad-except
            logger.error(f"Failed to execute dream task: {e}", exc_info=True)

    async def _execute_once(self, job: CronJobSpec) -> None:
        await self._run_owned(
            lambda: self._execute_once_owned(job), name="cron_execute_once",
        )

    async def _execute_once_owned(self, job: CronJobSpec) -> None:
        async with get_runtime_concurrency_governor().slot(
            category="background",
            # The per-job semaphore below is the singleton authority.  Do not
            # serialize equal jobs here: a concurrent duplicate must reach the
            # semaphore immediately and be recorded as skipped, not wait and
            # run after the first invocation finishes.
            owner_key="",
        ):
            await self._execute_once_admitted(job)

    async def _execute_once_admitted(self, job: CronJobSpec) -> None:
        assert job.id is not None, "Job must have an id"
        rt = self._rt.get(job.id)
        if not rt:
            rt = _Runtime(sem=asyncio.Semaphore(job.runtime.max_concurrency))
            self._rt[job.id] = rt

        if rt.sem.locked():
            await self._record_skipped_invocation(
                job,
                reason="max_concurrency_reached",
            )
            return

        await rt.sem.acquire()
        cron_run_id: str | None = None
        registration_attempted = False
        try:
            st = self._states.get(job.id, CronJobState())
            st.last_status = "running"
            self._states[job.id] = st
            workspace_dir = getattr(self._runner, "workspace_dir", None)
            agent_id = self._agent_id or "default"
            heartbeat_task: asyncio.Task[None] | None = None
            execution_cancelled = False
            try:
                if workspace_dir:
                    # Allocate identity before the first write so even a
                    # partially persisted start settles this exact invocation.
                    cron_run_id = f"cron:{job.id}:{uuid4().hex}"

                    async def persist_start():
                        try:
                            return await record_cron_job_started(
                                workspace_dir=workspace_dir,
                                agent_id=agent_id,
                                job_id=job.id,
                                job_name=job.name,
                                task_type=job.task_type,
                                channel=job.dispatch.channel,
                                run_id=cron_run_id,
                            )
                        except Exception as exc:
                            logger.error(
                                "cron start failed: job_id=%s error_type=%s",
                                job.id, type(exc).__name__,
                            )
                            raise

                    started_event = await await_settled(asyncio.create_task(
                        persist_start(), name=f"cron-start-{job.id}",
                    ))
                    current_task = asyncio.current_task()
                    if current_task is not None:
                        self._active_runs[cron_run_id] = (
                            str(started_event.payload["attempt_id"]), current_task,
                        )
                    if self._task_tracker is not None:
                        # Reuse the durable invocation identity. Registration
                        # fences effects against workspace shutdown; even a
                        # cancellation during admission must finish the
                        # registration before its paired release below.
                        registration_attempted = True
                        await await_settled(asyncio.create_task(
                            self._task_tracker.register_external_task(
                                cron_run_id,
                                attempt_id=str(started_event.payload["attempt_id"]),
                                root_session_id=job.dispatch.target.session_id,
                                owner_task=current_task,
                                activity_kind="cron",
                                activity_name=job.name,
                            ),
                            name=f"cron-register-{job.id}",
                        ))
                    heartbeat_task = asyncio.create_task(
                        _cron_runtime_heartbeat_loop(
                            workspace_dir=workspace_dir,
                            run_id=cron_run_id,
                            interval_seconds=_CRON_RUNTIME_HEARTBEAT_INTERVAL_SECONDS,
                        ),
                        name=f"cron-heartbeat-{job.id}",
                    )

                execution_task = asyncio.create_task(
                    self._executor.execute(job), name=f"cron-execute-{job.id}",
                )
                try:
                    await asyncio.shield(execution_task)
                except asyncio.CancelledError:
                    # Forward one cancellation, then own the executor's
                    # cleanup even if this invocation is cancelled again.
                    if not execution_task.done():
                        execution_task.cancel()
                    try:
                        await await_settled(execution_task)
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        pass
                    # await_settled deliberately preserves repeated caller
                    # cancellation. Observe any simultaneous cleanup failure
                    # from the drained task without exposing its contents.
                    if not execution_task.cancelled():
                        cleanup_error = execution_task.exception()
                        if cleanup_error is not None:
                            logger.error(
                                "cron executor cleanup failed: job_id=%s error_type=%s",
                                job.id, type(cleanup_error).__name__,
                            )
                    raise
                st.last_status = "success"
                st.last_error = None
                logger.info(
                    "cron _execute_once: job_id=%s status=success",
                    job.id,
                )
            except asyncio.CancelledError:
                execution_cancelled = True
                st.last_status = "cancelled"
                st.last_error = "Job was cancelled"
                logger.info(
                    "cron _execute_once: job_id=%s status=cancelled",
                    job.id,
                )
                raise
            except Exception as e:  # pylint: disable=broad-except
                st.last_status = "error"
                st.last_error = repr(e)
                logger.warning(
                    "cron _execute_once: job_id=%s status=error error=%s",
                    job.id,
                    repr(e),
                )
                raise
            finally:
                # Execution is over: a first stop must not cancel settlement.
                if cron_run_id is not None:
                    self._active_runs.pop(cron_run_id, None)
                st.last_run_at = datetime.now(timezone.utc)
                self._states[job.id] = st
                # Capture this invocation's result before another invocation
                # can update the shared job display state.
                final_status = str(st.last_status or "unknown")
                error_text = st.last_error

                async def settle_invocation() -> None:
                    try:
                        if heartbeat_task is not None:
                            heartbeat_task.cancel()
                            with suppress(asyncio.CancelledError):
                                await heartbeat_task
                        if workspace_dir:
                            await record_cron_job_settled(
                                workspace_dir=workspace_dir,
                                agent_id=agent_id,
                                job_id=job.id,
                                job_name=job.name,
                                task_type=job.task_type,
                                channel=job.dispatch.channel,
                                final_status=final_status,
                                error_text=error_text,
                                run_id=cron_run_id,
                            )
                    except Exception as exc:
                        # await_settled preserves caller cancellation; retain
                        # observability if persistence also failed, without
                        # logging event contents or provider responses.
                        logger.error(
                            "cron settlement failed: job_id=%s error_type=%s",
                            job.id, type(exc).__name__,
                        )
                        raise

                try:
                    await await_settled(asyncio.create_task(
                        settle_invocation(), name=f"cron-settle-{job.id}",
                    ))
                except Exception:
                    if execution_cancelled:
                        raise asyncio.CancelledError from None
                    raise
        finally:
            if cron_run_id is not None:
                self._active_runs.pop(cron_run_id, None)
                self._stop_requested_runs.discard(cron_run_id)
            try:
                if registration_attempted and cron_run_id is not None:
                    # The executor and terminal persistence are already
                    # drained. Retain registry ownership until it acknowledges
                    # release, including repeated external cancellation.
                    await await_settled(asyncio.create_task(
                        self._task_tracker.unregister_external_task(cron_run_id),
                        name=f"cron-unregister-{job.id}",
                    ))
            finally:
                rt.sem.release()

    async def _record_skipped_invocation(
        self,
        job: CronJobSpec,
        *,
        reason: str,
    ) -> None:
        assert job.id is not None, "Job must have an id"
        st = self._states.get(job.id, CronJobState())
        st.last_status = "skipped"
        st.last_error = reason
        st.last_run_at = datetime.now(timezone.utc)
        self._states[job.id] = st

        workspace_dir = getattr(self._runner, "workspace_dir", None)
        if not workspace_dir:
            return
        agent_id = self._agent_id or "default"
        run_id = f"cron:{job.id}:{uuid4().hex}"
        final_status, error_text = "skipped", reason
        cancelled = False

        async def persist_start() -> None:
            try:
                await record_cron_job_started(
                    workspace_dir=workspace_dir,
                    agent_id=agent_id,
                    job_id=job.id,
                    job_name=job.name,
                    task_type=job.task_type,
                    channel=job.dispatch.channel,
                    run_id=run_id,
                )
            except Exception as exc:
                logger.error(
                    "cron skipped start failed: job_id=%s error_type=%s",
                    job.id, type(exc).__name__,
                )
                raise

        try:
            await await_settled(asyncio.create_task(
                persist_start(), name=f"cron-skip-start-{job.id}",
            ))
        except asyncio.CancelledError:
            cancelled = True
            final_status, error_text = "cancelled", "Job was cancelled"
            raise
        except Exception as exc:
            final_status, error_text = "error", type(exc).__name__
            raise
        finally:
            async def persist_settlement() -> None:
                try:
                    await record_cron_job_settled(
                        workspace_dir=workspace_dir,
                        agent_id=agent_id,
                        job_id=job.id,
                        job_name=job.name,
                        task_type=job.task_type,
                        channel=job.dispatch.channel,
                        final_status=final_status,
                        error_text=error_text,
                        run_id=run_id,
                    )
                except Exception as exc:
                    logger.error(
                        "cron skipped settlement failed: job_id=%s error_type=%s",
                        job.id, type(exc).__name__,
                    )
                    raise

            try:
                await await_settled(asyncio.create_task(
                    persist_settlement(), name=f"cron-skip-settle-{job.id}",
                ))
            except Exception:
                if cancelled:
                    raise asyncio.CancelledError from None
                raise
