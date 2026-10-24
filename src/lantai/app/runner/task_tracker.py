# -*- coding: utf-8 -*-
"""Task tracker for background runs: streaming, reconnect, multi-subscriber.

For internal streaming runs, ``run_key`` is typically ``ChatSpec.id``
(chat_id). For externally-managed tasks (registered via
:meth:`TaskTracker.register_external_task`), ``run_key`` is an opaque
identifier chosen by the caller (e.g. a UUID prefixed with ``"ext-"``).
Per run: task, queues, event buffer. Reconnects get buffer replay + new
events. Cleanup when task completes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import (
    Any, AsyncGenerator, Awaitable, Callable, Coroutine, Literal, Optional,
)

from lantai.utils.async_iterator import DelegatedAsyncIterator
from lantai.utils.async_settlement import await_settled
from lantai.app.concurrency import get_runtime_concurrency_governor
from .replay_transport import ReplayCursor, ReplayTransport

logger = logging.getLogger(__name__)

_SENTINEL = None
_STREAM_KEEPALIVE_SECONDS = 15.0
_STOP_SETTLEMENT_WAIT_SECONDS = 10.0


class ActiveRunRequestConflict(RuntimeError):
    """A new logical request tried to attach to an unrelated active run.

    Historically :meth:`attach_or_start` treated every concurrent request as
    a reconnect. That is safe only for legacy clients that do not provide a
    request identity. Modern clients must never have a new payload silently
    discarded while receiving the previous run's buffered events.
    """

    def __init__(
        self,
        *,
        run_key: str,
        active_request_id: str | None,
        incoming_request_id: str,
    ) -> None:
        super().__init__(
            f"active run {run_key!r} belongs to a different logical request",
        )
        self.run_key = run_key
        self.active_request_id = active_request_id
        self.incoming_request_id = incoming_request_id


def _failed_response_sse(message: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "id": f"response_error_{int(now.timestamp() * 1000)}",
        "object": "response",
        "created_at": int(now.timestamp()),
        "status": "failed",
        "type": "response.failed",
        "output": [],
        "error": {"message": message},
        "message": message,
        "usage": None,
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _progress_sse(run_key: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "id": f"runtime_progress_{run_key}_{int(now.timestamp())}",
        "object": "runtime_progress",
        "created_at": int(now.timestamp()),
        "status": "running",
        "type": "runtime.progress",
        "summary": "Runtime is active.",
        "metadata": {
            "run_key": run_key,
            "last_update_at": now.isoformat(),
            "structured_keepalive": True,
        },
    }
    return ": heartbeat\n" + f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _terminal_response_status(sse: str) -> str | None:
    """Return the terminal response status carried by an SSE frame.

    A response terminal is the public ownership boundary for a run.  Cleanup
    may still be unwinding after the frame is queued, but the run must no
    longer appear stoppable or accept reconnect subscribers at that point.
    Item-level completion events are deliberately ignored.
    """

    for line in str(sse or "").splitlines():
        if not line.startswith("data:"):
            continue
        raw = line.removeprefix("data:").strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("object") != "response":
            continue
        status = str(payload.get("status") or "").strip().lower()
        event_type = str(payload.get("type") or "").strip().lower()
        if status in {
            "completed",
            "failed",
            "interrupted",
            "cancelled",
            "canceled",
        }:
            return status
        if event_type in {"response.completed", "response.failed"}:
            return event_type.removeprefix("response.")
    return None


@dataclass
class _RunState:
    """Per-run state (task, queues, buffer), guarded by tracker lock."""

    task: asyncio.Future
    queues: list[asyncio.Queue | ReplayCursor] = field(default_factory=list)
    buffer: list[str] = field(default_factory=list)
    replay: ReplayTransport | None = None
    start_time: Optional[datetime] = None
    finish_time: Optional[datetime] = None
    generation: int = 0
    attempt_id: str = ""
    root_session_id: str | None = None
    request_id: str | None = None
    cleanups: list[Callable[[], Awaitable[None] | None]] = field(
        default_factory=list,
    )
    cleanup_started: bool = False
    cleanup_task: asyncio.Task[bool] | None = None
    cleanup_complete: bool = False
    cleanup_failed: bool = False
    stop_requested: bool = False
    terminal_status: str | None = None
    external_owner: asyncio.Task | None = None
    activity_kind: Literal["cron"] | None = None
    activity_name: str | None = None
    first_event_or_exit: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def active(self) -> bool:
        """Whether callers may still attach to or stop this attempt."""

        if self.cleanup_failed:
            return True
        if self.stop_requested:
            return not self.cleanup_complete or not self.task.done()
        return not self.task.done() and self.terminal_status is None

    @property
    def occupied(self) -> bool:
        """A terminal frame does not prove that attempt resources settled."""
        return not self.task.done() or not self.cleanup_complete

    @property
    def status(self) -> str:
        if self.cleanup_failed:
            return "cleanup_failed"
        return "stopping" if self.stop_requested else "running"


class SessionRuntimeRegistry:
    """Per-workspace tracker: run_key -> RunState.

    All mutations to _runs use _lock. Frame publication is atomic on the loop.
    Internal subscribers share bounded-memory replay and own only cursors.
    External registrations retain the legacy queue adapter.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._runs: dict[str, _RunState] = {}
        self._generations: dict[str, int] = {}
        self._global_last_run_at: Optional[datetime] = None
        self._global_last_finish_at: Optional[datetime] = None
        self._closing = False

    @staticmethod
    def _attempt_id(
        run_key: str,
        generation: int,
        requested: str | None,
    ) -> str:
        normalized = str(requested or "").strip()
        return normalized or f"{run_key}:attempt-{generation}"

    @staticmethod
    async def _run_cleanups(state: _RunState) -> bool:
        if state.cleanup_task is None:
            state.cleanup_started = True

            async def settle() -> bool:
                for cleanup in state.cleanups:
                    try:
                        result = cleanup()
                        if result is not None:
                            await result
                    except asyncio.CancelledError:
                        state.cleanup_failed = True
                        logger.warning(
                            "Session runtime cleanup cancelled attempt_id=%s",
                            state.attempt_id,
                        )
                        raise
                    except Exception as exc:
                        state.cleanup_failed = True
                        logger.warning(
                            "Session runtime cleanup failed attempt_id=%s type=%s",
                            state.attempt_id,
                            type(exc).__name__,
                        )
                state.cleanup_complete = not state.cleanup_failed
                return state.cleanup_complete

            state.cleanup_task = asyncio.create_task(settle())
        # Concurrent stop/unregister callers await the same attempt-owned work.
        # Cancelling a caller must not cancel cleanup and release its resources.
        return await asyncio.shield(state.cleanup_task)

    async def _finish_run(
        self,
        run_key: str,
        state: _RunState,
        *,
        producer_exited: bool = False,
    ) -> None:
        async with self._lock:
            if self._runs.get(run_key) is not state:
                return
        if not await self._run_cleanups(state):
            return
        async with self._lock:
            if self._runs.get(run_key) is not state:
                return
            if isinstance(state.task, asyncio.Task) and not (
                producer_exited or state.task.done()
            ):
                return
            if not state.task.done() and not isinstance(state.task, asyncio.Task):
                state.task.set_result(None)
            self._runs.pop(run_key, None)
            state.finish_time = datetime.now(timezone.utc)
            self._global_last_finish_at = state.finish_time
            for queue in state.queues:
                if not isinstance(queue, ReplayCursor):
                    queue.put_nowait(_SENTINEL)
            if state.replay is not None:
                state.replay.seal()
        if state.replay is not None:
            await state.replay.close_if_unused()

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    async def get_status(self, run_key: str) -> str:
        """Return public activity, including unsettled stop/cleanup status."""
        async with self._lock:
            state = self._runs.get(run_key)
        if state is None or not state.active:
            return "idle"
        return state.status

    async def wait_for_first_event(
        self, run_key: str, attempt_id: str, *, timeout: float = 10.0,
    ) -> bool:
        """Wait for this producer's startup frame without owning its task.

        Console persists its canonical owner before the first frame. This
        barrier only orders admission; callers must still validate that owner.
        A timeout or cancelled waiter must not stop the producer.
        """
        async with self._lock:
            state = self._runs.get(run_key)
            if state is None or state.attempt_id != attempt_id:
                return False
            if state.replay is None:
                return state.active and state.status == "running"
        await asyncio.wait_for(state.first_event_or_exit.wait(), timeout=timeout)
        async with self._lock:
            return (
                self._runs.get(run_key) is state
                and state.active
                and state.status == "running"
            )

    async def get_control_snapshot(self, run_key: str) -> dict[str, Any] | None:
        """Read active identity/status without copying the replay backlog."""
        return await self.get_snapshot(run_key, include_events=False)

    async def get_snapshot(
        self, run_key: str, *, include_events: bool = True,
    ) -> dict[str, Any] | None:
        """Read active state, preserving full replay for existing callers."""
        async with self._lock:
            state = self._runs.get(run_key)
            if state is None or not state.active:
                return None
            snapshot = {
                "run_key": run_key,
                "status": state.status,
                "generation": state.generation,
                "attempt_id": state.attempt_id,
                "root_session_id": state.root_session_id,
                "request_id": state.request_id,
                "started_at": (
                    state.start_time.isoformat()
                    if state.start_time is not None
                    else None
                ),
                "event_count": (
                    state.replay.count if state.replay is not None
                    else len(state.buffer)
                ),
            }
            if state.activity_kind is not None:
                snapshot["activity_kind"] = state.activity_kind
                snapshot["activity_name"] = state.activity_name
            reader = None
            if include_events:
                if state.replay is None:
                    snapshot["events"] = list(state.buffer)
                else:
                    reader = state.replay.subscribe(
                        through_count=state.replay.count,
                    )
        if reader is not None:
            snapshot["events"] = []
            try:
                while (event := await reader.get()) is not None:
                    snapshot["events"].append(event)
            finally:
                await reader.close()
        return snapshot

    async def get_global_status(self) -> dict:
        """Get global agent status summary.

        Returns:
            dict with keys:
                - status: 'idle' | 'running'
                - running_task_count: int
                - last_run_at: Optional[datetime]
                - last_finish_at: Optional[datetime]
        """
        async with self._lock:
            running_count = sum(
                1 for state in self._runs.values() if state.active
            )
            status = "running" if running_count > 0 else "idle"

            return {
                "status": status,
                "running_task_count": running_count,
                "last_run_at": self._global_last_run_at,
                "last_finish_at": self._global_last_finish_at,
            }

    async def has_active_tasks(self) -> bool:
        """Check if any tasks are currently running.

        Returns:
            bool: True if any tasks are active, False otherwise
        """
        async with self._lock:
            for state in self._runs.values():
                if state.active:
                    return True
            return False

    async def list_active_tasks(self) -> list[str]:
        """List all currently running task keys.

        Returns:
            list[str]: List of active run_keys
        """
        async with self._lock:
            return [
                run_key
                for run_key, state in self._runs.items()
                if state.active
            ]

    async def has_unsettled_tasks(self) -> bool:
        """Whether producers or attempt-owned cleanup still hold resources."""
        async with self._lock:
            return any(state.occupied for state in self._runs.values())

    async def get_settlement_snapshot(self, run_key: str) -> dict[str, Any] | None:
        """Expose resource ownership independently from a public terminal."""
        async with self._lock:
            state = self._runs.get(run_key)
            if state is None or not state.occupied:
                return None
            status = state.status
            if state.terminal_status and not state.stop_requested and not state.cleanup_failed:
                status = "cleaning_up"
            return {
                "attempt_id": state.attempt_id,
                "generation": state.generation,
                "status": status,
                "settlement_pending": True,
            }

    async def wait_all_done(self, timeout: float = 300.0) -> bool:
        """Wait for all active tasks to complete.

        Args:
            timeout: Maximum time to wait in seconds (default: 300s = 5min)

        Returns:
            bool: True if all tasks completed, False if timeout occurred
        """

        async def _wait_loop() -> None:
            while await self.has_unsettled_tasks():
                await asyncio.sleep(0.5)

        try:
            await asyncio.wait_for(_wait_loop(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return not await self.has_unsettled_tasks()

    async def shutdown(self, timeout: float = _STOP_SETTLEMENT_WAIT_SECONDS) -> None:
        """Fence admission and retain every attempt until its resources settle."""
        async with self._lock:
            self._closing = True
            owned = list(self._runs.items())
            for _run_key, state in owned:
                if state.occupied and state.terminal_status is None and not state.stop_requested:
                    state.stop_requested = True
                    # External registrations contain a placeholder Future.
                    # Cancelling it would invent an acknowledgement from its
                    # real owner; only unregister may release that ownership.
                    if isinstance(state.task, asyncio.Task) and not state.cleanup_started:
                        state.task.cancel()
                    elif state.external_owner is not None:
                        state.external_owner.cancel()

        async def finish_completed(run_key, state):
            if isinstance(state.task, asyncio.Task):
                await asyncio.wait({state.task}, timeout=timeout)
                if state.task.done():
                    await self._finish_run(run_key, state)

        await asyncio.gather(*(
            finish_completed(run_key, state) for run_key, state in owned
        ))
        if not await self.wait_all_done(timeout=timeout):
            raise RuntimeError("Workspace runtime resources remain unsettled")

    async def register_external_task(
        self,
        run_key: str,
        *,
        attempt_id: str | None = None,
        root_session_id: str | None = None,
        cleanup: Callable[[], Awaitable[None] | None] | None = None,
        owner_task: asyncio.Task | None = None,
        activity_kind: Literal["cron"] | None = None,
        activity_name: str | None = None,
    ) -> int:
        """Register an externally-managed task so it is visible to
        :meth:`has_active_tasks` and :meth:`wait_all_done`.

        This is used for tasks managed outside of 澜台's own streaming
        pipeline (e.g. background tasks dispatched through
        the legacy runtime app wrapper).  The caller **must** call
        :meth:`unregister_external_task` when the task completes.

        Args:
            run_key: Unique identifier for the external task.
        """
        start_time = datetime.now(timezone.utc)
        async with self._lock:
            if self._closing:
                raise RuntimeError("Workspace runtime is shutting down")
            if run_key in self._runs and self._runs[run_key].occupied:
                logger.debug(
                    "External task already registered: %s",
                    run_key,
                )
                return self._runs[run_key].generation
            # Use an unresolved Future as the "task" — it stays not-done
            # until unregister_external_task resolves it.
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            generation = self._generations.get(run_key, 0) + 1
            self._generations[run_key] = generation
            self._runs[run_key] = _RunState(
                task=future,
                queues=[],
                buffer=[],
                start_time=start_time,
                generation=generation,
                attempt_id=self._attempt_id(run_key, generation, attempt_id),
                root_session_id=str(root_session_id or "").strip() or None,
                cleanups=[cleanup] if cleanup is not None else [],
                external_owner=owner_task,
                activity_kind=activity_kind,
                activity_name=str(activity_name or "").strip() or None,
            )
            self._global_last_run_at = start_time
            logger.debug("Registered external task: %s", run_key)
            return generation

    async def unregister_external_task(self, run_key: str) -> None:
        """Mark an externally-managed task as done and remove it.

        Sends the sentinel value to any subscriber queues so their
        :meth:`stream_from_queue` consumers terminate cleanly instead of
        hanging. Idempotent — safe to call even if *run_key* was never
        registered or was already unregistered.

        Args:
            run_key: Unique identifier previously passed to
                :meth:`register_external_task`.
        """
        async with self._lock:
            state = self._runs.get(run_key)
            if state is None:
                return
        # Keep the attempt registered while cleanup runs so a replacement
        # cannot start and then be affected by the old attempt's cleanup.
        await asyncio.shield(asyncio.create_task(self._finish_run(run_key, state)))

    async def attach(self, run_key: str) -> asyncio.Queue | ReplayCursor | None:
        """Attach to an existing run.

        Returns a reader replaying all buffered events, or ``None``
        if no run is active for *run_key*.
        """
        async with self._lock:
            state = self._runs.get(run_key)
            if state is None or not state.active:
                return None
            q = self._subscribe(state)
            state.queues.append(q)
            return q

    async def detach_subscriber(
        self,
        run_key: str,
        queue: asyncio.Queue | ReplayCursor,
    ) -> None:
        """Remove *queue* from *run_key*'s subscriber list.

        Idempotent if the run ended or *queue* was already removed.
        """
        async with self._lock:
            state = self._runs.get(run_key)
            if state is not None:
                try:
                    state.queues.remove(queue)
                except ValueError:
                    pass
        # A completed run has already left the registry, but its consumers
        # still own their replay cursors until drain or disconnect.
        if isinstance(queue, ReplayCursor):
            await queue.close()

    @staticmethod
    def _subscribe(state: _RunState):
        if state.replay is not None:
            return state.replay.subscribe()
        queue = asyncio.Queue()
        for sse in state.buffer:
            queue.put_nowait(sse)
        return queue

    async def request_stop_if_generation(self, run_key: str, generation: int) -> bool:
        """Cancel *run_key* only when the active generation still matches."""
        return await self._request_stop(
            run_key,
            expected_generation=generation,
            expected_attempt_id=None,
        )

    async def request_stop_if_attempt(
        self,
        run_key: str,
        *,
        attempt_id: str,
        generation: int | None = None,
    ) -> bool:
        """Cancel only the exact active attempt observed by the caller."""
        return await self._request_stop(
            run_key,
            expected_generation=generation,
            expected_attempt_id=attempt_id,
        )

    async def request_stop(self, run_key: str) -> bool:
        """Cancel the run. Returns ``True`` if it was running."""
        return await self._request_stop(
            run_key,
            expected_generation=None,
            expected_attempt_id=None,
        )

    async def _request_stop(
        self,
        run_key: str,
        *,
        expected_generation: int | None,
        expected_attempt_id: str | None,
    ) -> bool:
        logger.debug("[STOP] request_stop called for run_key=%s", run_key)
        async with self._lock:
            state = self._runs.get(run_key)
            logger.debug(
                "[STOP] run_key=%s state=%s done=%s",
                run_key,
                "found" if state else "not_found",
                state.task.done() if state else "N/A",
            )
            if state is None or not state.active:
                logger.debug(
                    "[STOP] Cannot stop run_key=%s (not running)",
                    run_key,
                )
                return False
            if (
                expected_generation is not None
                and state.generation != expected_generation
            ):
                logger.debug(
                    "[STOP] Cannot stop run_key=%s generation=%s "
                    "(expected %s)",
                    run_key,
                    state.generation,
                    expected_generation,
                )
                return False
            if (
                expected_attempt_id is not None
                and state.attempt_id != expected_attempt_id
            ):
                logger.debug(
                    "[STOP] Cannot stop run_key=%s attempt_id=%s "
                    "(expected %s)",
                    run_key,
                    state.attempt_id,
                    expected_attempt_id,
                )
                return False
        # Enter cancellation immediately; approval/tool cleanup remains fenced
        # by the occupied slot while it resolves. This keeps stop latency
        # bounded even when an external approval channel is slow.
        async with self._lock:
            if self._runs.get(run_key) is not state or not state.active:
                return False
            if not state.stop_requested:
                state.stop_requested = True
                self._generations[run_key] = state.generation + 1
                if isinstance(state.task, asyncio.Task) and not state.cleanup_started:
                    logger.debug("[STOP] Calling task.cancel() for run_key=%s", run_key)
                    state.task.cancel()
                elif state.external_owner is not None and not state.cleanup_started:
                    state.external_owner.cancel()

        # Only unregister acknowledges completion of an external producer.
        # Its placeholder must remain occupied while the real owner unwinds.
        if not isinstance(state.task, asyncio.Task):
            if state.external_owner is not None:
                # Give the cancelled owner time to acknowledge its cleanup in
                # finally. Owner exit by itself cannot settle this placeholder.
                # asyncio.wait leaves it intact on timeout or caller cancellation.
                await asyncio.wait(
                    {state.task}, timeout=_STOP_SETTLEMENT_WAIT_SECONDS,
                )
            return True

        # Internal stream producers own the model generator's cancellation
        # lifecycle.  Let that lifecycle unwind before reporting a successful
        # stop so it can persist the durable interrupted terminal event first.
        # A bounded acknowledgement never proves that resources settled.
        if isinstance(state.task, asyncio.Task):
            done, _pending = await asyncio.wait(
                {state.task},
                timeout=_STOP_SETTLEMENT_WAIT_SECONDS,
            )
            if not done:
                logger.warning(
                    "[STOP] producer settlement timed out for run_key=%s; "
                    "attempt remains occupied",
                    run_key,
                )
                return True

        # A task cancelled before its coroutine first runs never enters the
        # producer's ``finally`` block. Verify that the attempt actually
        # relinquished its slot before returning; otherwise perform the same
        # attempt-owned fallback used for a stalled producer.
        async with self._lock:
            if self._runs.get(run_key) is not state:
                return True
        finish = asyncio.create_task(self._finish_run(run_key, state))
        await asyncio.wait({finish}, timeout=_STOP_SETTLEMENT_WAIT_SECONDS)
        return True

    async def attach_or_start(
        self,
        run_key: str,
        payload: Any,
        stream_fn: Callable[..., Coroutine],
        *,
        attempt_id: str | None = None,
        root_session_id: str | None = None,
        request_id: str | None = None,
        reject_conflicting_request: bool = False,
        cleanup: Callable[[], Awaitable[None] | None] | None = None,
    ) -> tuple[asyncio.Queue | ReplayCursor, bool]:
        """Attach to an existing run or start a new one.

        Returns ``(queue, is_new_run)``.
        """
        async with self._lock:
            if self._closing:
                raise RuntimeError("Workspace runtime is shutting down")
            state = self._runs.get(run_key)
            # Keep an emitted-terminal attempt occupying its serialization
            # slot until attempt-owned cleanup completes. Reconnect callers
            # may replay that terminal frame, while a different request id is
            # rejected instead of overwriting the finishing run state.
            if state is not None and state.occupied:
                normalized_request_id = str(request_id or "").strip() or None
                if (
                    reject_conflicting_request
                    and normalized_request_id is not None
                    and state.request_id != normalized_request_id
                ):
                    raise ActiveRunRequestConflict(
                        run_key=run_key,
                        active_request_id=state.request_id,
                        incoming_request_id=normalized_request_id,
                    )
                q = self._subscribe(state)
                state.queues.append(q)
                return q, False

            replay = ReplayTransport()
            my_queue = replay.subscribe()
            generation = self._generations.get(run_key, 0) + 1
            self._generations[run_key] = generation
            run = _RunState(
                task=asyncio.Future(),  # placeholder, replaced below
                queues=[my_queue],
                buffer=[],
                replay=replay,
                generation=generation,
                attempt_id=self._attempt_id(run_key, generation, attempt_id),
                root_session_id=str(root_session_id or "").strip() or None,
                request_id=str(request_id or "").strip() or None,
                cleanups=[cleanup] if cleanup is not None else [],
            )
            self._runs[run_key] = run

            tracker_ref = weakref.ref(self)

            async def _drive_source() -> None:
                start_time = datetime.now(timezone.utc)

                try:
                    tracker = tracker_ref()
                    if tracker is not None:
                        async with tracker.lock:
                            run.start_time = start_time
                            # pylint: disable=protected-access
                            tracker._global_last_run_at = start_time

                    # The run key is the serialization boundary.  A root
                    # session is only an approval/control-routing identity:
                    # collaboration children intentionally share it with the
                    # host, while each child owns a distinct chat/run.  Using
                    # the root session as the governor owner silently
                    # serialized the fan-out and made later members hit the
                    # admission timeout whenever an earlier model call took
                    # longer than the timeout.
                    owner_key = run_key
                    async with get_runtime_concurrency_governor().slot(
                        category="agent",
                        owner_key=owner_key,
                    ):
                        stream = stream_fn(payload)
                        async def publish(sse: str) -> None:
                            tracker = tracker_ref()
                            if tracker is None:
                                return
                            terminal_status = _terminal_response_status(sse)

                            def accept() -> bool:
                                return (
                                    tracker._runs.get(run_key) is run
                                    and (not run.stop_requested or terminal_status in {
                                        "interrupted", "cancelled", "canceled", "failed",
                                    })
                                )

                            # A terminal has already settled in the source.
                            # Disk transport must not make it stoppable again.
                            if terminal_status is not None and accept():
                                run.terminal_status = terminal_status
                            await replay.append(
                                sse, accept=accept, published=run.first_event_or_exit.set,
                            )

                        async with DelegatedAsyncIterator(stream) as events:
                            async for sse in events:
                                try:
                                    await publish(sse)
                                except asyncio.CancelledError as exc:
                                    events.request_cancel(exc)
                except asyncio.CancelledError:
                    logger.debug("run cancelled run_key=%s", run_key)
                except Exception:
                    logger.exception("run error run_key=%s", run_key)
                    err_sse = _failed_response_sse("internal server error")
                    tracker = tracker_ref()
                    if tracker is not None:
                        async with tracker.lock:
                            if tracker._runs.get(run_key) is not run:
                                return
                            replay.fail(err_sse)
                            run.terminal_status = "failed"
                finally:
                    tracker = tracker_ref()
                    if tracker is not None:
                        await tracker._finish_run(
                            run_key, run, producer_exited=True,
                        )

            async def _producer() -> None:
                # The source and all its ContextVar tokens live in this one
                # owned task. Repeated caller cancellation cannot interrupt
                # its cancellation/cleanup path a second time.
                source_task = asyncio.create_task(_drive_source())
                try:
                    await asyncio.shield(source_task)
                except asyncio.CancelledError:
                    source_task.cancel()
                    try:
                        await await_settled(source_task)
                    except asyncio.CancelledError:
                        pass

            run.task = asyncio.create_task(_producer())
            # Cancellation before the coroutine starts bypasses its finally.
            # The callback also completes cleanup interrupted by caller cancel.
            run.task.add_done_callback(lambda _task: run.first_event_or_exit.set())
            run.task.add_done_callback(
                lambda _task: asyncio.create_task(self._finish_run(run_key, run))
            )
            return my_queue, True

    async def stream_from_queue(
        self,
        queue: asyncio.Queue | ReplayCursor,
        run_key: str,
    ) -> AsyncGenerator[str, None]:
        """Yield SSE strings from *queue* until the sentinel ``None``.

        Always detaches *queue* from *run_key* when this stream ends or is
        closed (including client disconnect), so reconnects do not leak queues.
        """
        try:
            while True:
                try:
                    event = await asyncio.wait_for(
                        queue.get(),
                        timeout=_STREAM_KEEPALIVE_SECONDS,
                    )
                    if event is _SENTINEL:
                        break
                    yield event
                except asyncio.TimeoutError:
                    yield _progress_sse(run_key)
                except asyncio.CancelledError:
                    break
        finally:
            await self.detach_subscriber(run_key, queue)


# Compatibility facade for existing imports while ownership converges on the
# explicit SessionRuntimeRegistry name.
TaskTracker = SessionRuntimeRegistry


async def read_control_snapshot(tracker: Any, run_key: str) -> dict[str, Any] | None:
    """Use bounded metadata reads while accepting legacy tracker adapters."""
    reader = getattr(tracker, "get_control_snapshot", None)
    if callable(reader):
        return await reader(run_key)
    return await tracker.get_snapshot(run_key)
