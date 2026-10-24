# -*- coding: utf-8 -*-
"""Durable, foreground-first coordination for background semantic work."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, ClassVar, TypeVar


T = TypeVar("T")


class PrimaryModelPreempted(RuntimeError):
    """Raised when a visible turn takes the primary model from a side task."""


@dataclass
class _ForegroundState:
    foreground_count: int = 0
    idle: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    primary_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    preempted_tasks: set[asyncio.Task[Any]] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.idle.set()


_FOREGROUND_STATES: dict[str, _ForegroundState] = {}


def _state(agent_id: str) -> _ForegroundState:
    key = str(agent_id or "default")
    state = _FOREGROUND_STATES.get(key)
    if state is None:
        state = _ForegroundState()
        _FOREGROUND_STATES[key] = state
    return state


@asynccontextmanager
async def foreground_model_turn(agent_id: str) -> AsyncIterator[None]:
    """Give a visible turn priority over primary-model background work."""

    state = _state(agent_id)
    async with state.lock:
        state.foreground_count += 1
        state.idle.clear()
        background_tasks = tuple(state.primary_tasks)
    for task in background_tasks:
        state.preempted_tasks.add(task)
        task.cancel()
    try:
        yield
    finally:
        async with state.lock:
            state.foreground_count = max(0, state.foreground_count - 1)
            if state.foreground_count == 0:
                state.idle.set()


async def run_primary_when_idle(
    agent_id: str,
    operation: Callable[[], Awaitable[T]],
) -> T:
    """Run isolated primary-model work only while no visible turn is active."""

    state = _state(agent_id)
    while True:
        await state.idle.wait()
        async with state.lock:
            if state.foreground_count:
                continue
            task = asyncio.create_task(operation())
            state.primary_tasks.add(task)
        try:
            return await task
        except asyncio.CancelledError as exc:
            if task in state.preempted_tasks:
                raise PrimaryModelPreempted(
                    "primary background work yielded to a foreground turn",
                ) from exc
            task.cancel()
            raise
        finally:
            state.primary_tasks.discard(task)
            state.preempted_tasks.discard(task)


@dataclass(frozen=True)
class BackgroundSemanticJob:
    job_id: str
    kind: str
    payload: dict[str, Any]
    result: Any
    idempotency_key: str
    status: str
    attempts: int
    last_error: str
    created_at: float
    updated_at: float


class BackgroundSemanticJobStore:
    """Small SQLite ledger for accepted semantic jobs.

    Opening the ledger turns interrupted ``running`` rows back into
    ``pending``. A deterministic idempotency key prevents repeated terminal
    events or reconnects from performing the same semantic write twice.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(Path(path).expanduser())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS background_semantic_jobs (
                    job_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    result_json TEXT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """,
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_background_semantic_status "
                "ON background_semantic_jobs(status, created_at)",
            )
            columns = {
                str(row[1])
                for row in self._connection.execute(
                    "PRAGMA table_info(background_semantic_jobs)",
                ).fetchall()
            }
            if "result_json" not in columns:
                self._connection.execute(
                    "ALTER TABLE background_semantic_jobs "
                    "ADD COLUMN result_json TEXT",
                )
            self._connection.execute(
                "UPDATE background_semantic_jobs SET status = 'pending', "
                "last_error = 'recovered after interrupted execution', "
                "updated_at = ? WHERE status = 'running'",
                (time.time(),),
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> BackgroundSemanticJob:
        result = None
        if row["result_json"]:
            try:
                result = json.loads(row["result_json"])
            except json.JSONDecodeError:
                result = None
        return BackgroundSemanticJob(
            job_id=str(row["job_id"]),
            kind=str(row["kind"]),
            payload=dict(json.loads(row["payload_json"])),
            result=result,
            idempotency_key=str(row["idempotency_key"]),
            status=str(row["status"]),
            attempts=int(row["attempts"]),
            last_error=str(row["last_error"] or ""),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def enqueue(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> BackgroundSemanticJob:
        now = time.time()
        key = str(idempotency_key).strip()
        if not key:
            raise ValueError("background semantic idempotency key is required")
        job_id = "semantic_" + hashlib.sha256(key.encode("utf-8")).hexdigest()
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO background_semantic_jobs (
                    job_id, kind, payload_json, idempotency_key, status,
                    attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (job_id, str(kind), encoded, key, now, now),
            )
            row = self._connection.execute(
                "SELECT * FROM background_semantic_jobs "
                "WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            raise RuntimeError("failed to persist background semantic job")
        return self._row(row)

    def claim_next(self) -> BackgroundSemanticJob | None:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM background_semantic_jobs "
                "WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1",
            ).fetchone()
            if row is None:
                return None
            now = time.time()
            self._connection.execute(
                "UPDATE background_semantic_jobs SET status = 'running', "
                "updated_at = ? WHERE job_id = ? AND status = 'pending'",
                (now, row["job_id"]),
            )
            claimed = self._connection.execute(
                "SELECT * FROM background_semantic_jobs WHERE job_id = ?",
                (row["job_id"],),
            ).fetchone()
        return self._row(claimed) if claimed is not None else None

    def list_pending(self) -> list[BackgroundSemanticJob]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM background_semantic_jobs "
                "WHERE status = 'pending' ORDER BY created_at ASC",
            ).fetchall()
        return [self._row(row) for row in rows]

    def get(self, job_id: str) -> BackgroundSemanticJob | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM background_semantic_jobs WHERE job_id = ?",
                (str(job_id),),
            ).fetchone()
        return self._row(row) if row is not None else None

    def mark_completed(self, job_id: str, result: Any = None) -> None:
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE background_semantic_jobs SET status = 'completed', "
                "result_json = ?, last_error = '', updated_at = ? "
                "WHERE job_id = ?",
                (encoded, time.time(), str(job_id)),
            )

    def release(
        self,
        job_id: str,
        *,
        error: str,
        count_attempt: bool,
        max_attempts: int = 3,
    ) -> str:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT attempts FROM background_semantic_jobs "
                "WHERE job_id = ?",
                (str(job_id),),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            attempts = int(row["attempts"]) + (1 if count_attempt else 0)
            status = (
                "failed"
                if count_attempt and attempts >= max(1, int(max_attempts))
                else "pending"
            )
            self._connection.execute(
                "UPDATE background_semantic_jobs SET status = ?, "
                "attempts = ?, last_error = ?, updated_at = ? "
                "WHERE job_id = ?",
                (
                    status,
                    attempts,
                    str(error)[:2000],
                    time.time(),
                    str(job_id),
                ),
            )
        return status

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT status, COUNT(*) AS total FROM background_semantic_jobs "
                "GROUP BY status",
            ).fetchall()
        counts = {
            "pending": 0,
            "running": 0,
            "completed": 0,
            "failed": 0,
        }
        counts.update({str(row["status"]): int(row["total"]) for row in rows})
        return counts

    def health(self) -> dict[str, Any]:
        with self._lock:
            failed = self._connection.execute(
                "SELECT last_error, updated_at FROM background_semantic_jobs "
                "WHERE status = 'failed' ORDER BY updated_at DESC LIMIT 1",
            ).fetchone()
            completed = self._connection.execute(
                "SELECT updated_at FROM background_semantic_jobs "
                "WHERE status = 'completed' ORDER BY updated_at DESC LIMIT 1",
            ).fetchone()
        return {
            "last_error": str(failed["last_error"] or "") if failed else "",
            "last_failed_at": float(failed["updated_at"]) if failed else None,
            "last_completed_at": (
                float(completed["updated_at"]) if completed else None
            ),
        }

    def requeue_failed(self) -> int:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE background_semantic_jobs SET status = 'pending', "
                "attempts = 0, last_error = '', updated_at = ? "
                "WHERE status = 'failed'",
                (time.time(),),
            )
        return max(0, int(cursor.rowcount))

    def close(self) -> None:
        with self._lock:
            self._connection.close()


SemanticHandler = Callable[[dict[str, Any]], Awaitable[Any]]


class BackgroundSemanticService:
    """One serial durable worker per Agent workspace."""

    _instances: ClassVar[dict[str, "BackgroundSemanticService"]] = {}

    def __init__(self, workspace_dir: str | Path, *, agent_id: str) -> None:
        self.workspace_dir = Path(workspace_dir).expanduser()
        self.agent_id = str(agent_id or "default")
        safe_agent = re.sub(r"[^a-zA-Z0-9_.-]+", "-", self.agent_id)
        self.store = BackgroundSemanticJobStore(
            self.workspace_dir
            / ".lantai"
            / "background-semantics"
            / safe_agent
            / "jobs.sqlite3",
        )
        self._handlers: dict[str, SemanticHandler] = {}
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._stopping = False

    @classmethod
    def for_agent(cls, agent_id: str) -> "BackgroundSemanticService" | None:
        return cls._instances.get(str(agent_id or "default"))

    def register_handler(self, kind: str, handler: SemanticHandler) -> None:
        self._handlers[str(kind)] = handler

    async def start(self) -> None:
        if self._worker is not None and not self._worker.done():
            return
        self._stopping = False
        self._instances[self.agent_id] = self
        self._wake.set()
        self._worker = asyncio.create_task(
            self._run(),
            name=f"background-semantics:{self.agent_id}",
        )

    def submit(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> BackgroundSemanticJob:
        job = self.store.enqueue(
            kind=kind,
            payload=payload,
            idempotency_key=idempotency_key,
        )
        self._wake.set()
        return job

    async def wait_for_job(
        self,
        job_id: str,
        *,
        poll_interval: float = 0.01,
    ) -> BackgroundSemanticJob:
        while True:
            job = self.store.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job.status in {"completed", "failed"}:
                return job
            await asyncio.sleep(max(0.001, float(poll_interval)))

    async def _run(self) -> None:
        try:
            while not self._stopping:
                job = self.store.claim_next()
                if job is None:
                    self._wake.clear()
                    if self.store.list_pending():
                        self._wake.set()
                        continue
                    await self._wake.wait()
                    continue
                handler = self._handlers.get(job.kind)
                if handler is None:
                    status = self.store.release(
                        job.job_id,
                        error=f"no handler registered for {job.kind}",
                        count_attempt=True,
                    )
                    if status == "pending":
                        await asyncio.sleep(min(2.0, 0.25 * (2**job.attempts)))
                    continue
                try:
                    result = await handler(job.payload)
                except PrimaryModelPreempted as exc:
                    self.store.release(
                        job.job_id,
                        error=str(exc),
                        count_attempt=False,
                    )
                    await asyncio.sleep(0)
                except asyncio.CancelledError:
                    self.store.release(
                        job.job_id,
                        error="worker stopped during execution",
                        count_attempt=False,
                    )
                    raise
                except Exception as exc:  # pylint: disable=broad-except
                    status = self.store.release(
                        job.job_id,
                        error=f"{type(exc).__name__}: {exc}",
                        count_attempt=True,
                    )
                    if status == "pending":
                        await asyncio.sleep(min(2.0, 0.25 * (2**job.attempts)))
                else:
                    self.store.mark_completed(job.job_id, result)
        except asyncio.CancelledError:
            raise

    def status(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "worker_running": bool(self._worker and not self._worker.done()),
            **self.store.counts(),
            **self.store.health(),
        }

    def retry_failed(self) -> int:
        count = self.store.requeue_failed()
        if count:
            self._wake.set()
        return count

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        worker = self._worker
        self._worker = None
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        if self._instances.get(self.agent_id) is self:
            self._instances.pop(self.agent_id, None)
        self.store.close()


__all__ = [
    "BackgroundSemanticJob",
    "BackgroundSemanticJobStore",
    "BackgroundSemanticService",
    "PrimaryModelPreempted",
    "foreground_model_turn",
    "run_primary_when_idle",
]
