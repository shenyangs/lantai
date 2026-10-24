# -*- coding: utf-8 -*-
"""Protected durable store for RuntimeRunContract snapshots."""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from collections.abc import Callable, Mapping
from contextlib import ExitStack, asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from lantai.utils.file_lock import locked_file_descriptor
from lantai.security.storage_keys import (
    contained_storage_path,
    read_text_no_follow,
    safe_storage_key,
    write_text_atomic_no_follow,
)

from .loops import LoopContract, LoopEvaluationReport, LoopProgressWindow
from .runs import (
    TERMINAL_RUNTIME_RUN_STATUSES,
    RuntimeAttemptContract,
    RuntimeRunContract,
    RuntimeStepContract,
    migrate_runtime_run_contract,
)


RUNTIME_STORE_LOCK_TIMEOUT_SECONDS = 5.0
RUNTIME_STORE_LOCK_POLL_SECONDS = 0.01

# Bound admission before submitting: ThreadPoolExecutor's own queue is unbounded.
_STORE_IO_WORKERS = 4
_STORE_IO_SLOTS = threading.BoundedSemaphore(_STORE_IO_WORKERS)
_STORE_IO_EXECUTOR = ThreadPoolExecutor(
    max_workers=_STORE_IO_WORKERS, thread_name_prefix="runtime-store-io",
)
_IO_RESULT = TypeVar("_IO_RESULT")


async def _owned_store_io(
    lock_path: Path | None,
    operation: Callable[[], _IO_RESULT],
) -> _IO_RESULT:
    """Bound and drain setup, disk operations, and file-lock release.

    Capacity is reserved before submission. The worker owns every file-lock
    operation, so synchronous readers cannot prevent its unlock. Cancellation
    prevents admission while acquiring the lock; admitted operations drain.
    User callbacks and commit guards remain on the event loop.
    """
    while not _STORE_IO_SLOTS.acquire(blocking=False):
        await asyncio.sleep(RUNTIME_STORE_LOCK_POLL_SECONDS)
    cancelled = threading.Event()
    try:
        def operate_and_unlock():
            with _owned_store_lock(lock_path, cancelled):
                if cancelled.is_set():
                    raise asyncio.CancelledError()
                return operation()

        context = contextvars.copy_context()
        future = asyncio.get_running_loop().run_in_executor(
            _STORE_IO_EXECUTOR, context.run, operate_and_unlock,
        )
        cancellation = None
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError as exc:
                cancellation = exc
                cancelled.set()
            except BaseException:
                if cancellation is None:
                    raise
                break
        if cancellation is not None:
            # Retrieve failures too; no orphaned error, late write, or late quarantine.
            try:
                future.result()
            except BaseException:
                pass
            raise cancellation
        return future.result()
    finally:
        _STORE_IO_SLOTS.release()


@contextmanager
def _owned_store_lock(path: Path | None, cancelled: threading.Event):
    if path is None:
        # Missing-data preflight must not create storage or require write access.
        yield
        return
    if cancelled.is_set():
        raise asyncio.CancelledError()
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_best_effort(path.parent, 0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _chmod_best_effort(path, 0o600)
        deadline = time.monotonic() + RUNTIME_STORE_LOCK_TIMEOUT_SECONDS
        with ExitStack() as held:
            while True:
                if cancelled.is_set():
                    raise asyncio.CancelledError()
                try:
                    held.enter_context(locked_file_descriptor(fd, blocking=False))
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RuntimeRunStoreError(
                            "Runtime store lock is busy; no operation was admitted.",
                        ) from None
                    cancelled.wait(min(RUNTIME_STORE_LOCK_POLL_SECONDS, remaining))
                else:
                    break
            yield
    finally:
        os.close(fd)


async def _owned_store_exists(path: Path) -> bool:
    return await _owned_store_io(None, path.exists)


RUNTIME_RUN_STORE_SCHEMA_VERSION = 1
RUNTIME_RUN_STORE_WRITER_VERSION = "lantai-runtime-store/1"
RUNTIME_RUN_LOOP_PROGRESS_SCHEMA_VERSION = 1
RUNTIME_RUN_LOOP_EVALUATION_SCHEMA_VERSION = 1
RUNTIME_RUN_LOOP_CONTRACT_SCHEMA_VERSION = 1
RUNTIME_RUN_STEP_SCHEMA_VERSION = 1

_LOCKS_GUARD = asyncio.Lock()
_PROCESS_LOCKS: dict[str, asyncio.Lock] = {}


class RuntimeRunStoreError(ValueError):
    """Base error raised by RuntimeRunStore."""


class RuntimeRunStoreConflictError(RuntimeRunStoreError):
    """Raised when optimistic revision fencing rejects a write."""


class RuntimeRunStoreLeaseError(RuntimeRunStoreConflictError):
    """Raised when lease-owner fencing rejects a write."""


class RuntimeRunStoreCorruptionError(RuntimeRunStoreError):
    """Raised when a stored contract is unreadable or checksum-invalid."""

    def __init__(self, report: "RuntimeRunStoreRecoveryReport") -> None:
        self.report = report
        super().__init__(report.message)


class RuntimeRunStoreEnvelope(BaseModel):
    """Versioned on-disk envelope for one RuntimeRunContract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = RUNTIME_RUN_STORE_SCHEMA_VERSION
    writer_version: str = RUNTIME_RUN_STORE_WRITER_VERSION
    checksum: str
    payload: dict[str, object]


class RuntimeRunLoopProgressEnvelope(BaseModel):
    """Versioned on-disk envelope for one Loop progress sidecar."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = RUNTIME_RUN_LOOP_PROGRESS_SCHEMA_VERSION
    writer_version: str = RUNTIME_RUN_STORE_WRITER_VERSION
    checksum: str
    payload: dict[str, object]


class RuntimeRunLoopEvaluationEnvelope(BaseModel):
    """Versioned on-disk envelope for one Loop evaluation report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = RUNTIME_RUN_LOOP_EVALUATION_SCHEMA_VERSION
    writer_version: str = RUNTIME_RUN_STORE_WRITER_VERSION
    checksum: str
    payload: dict[str, object]


class RuntimeRunLoopContractEnvelope(BaseModel):
    """Versioned on-disk envelope for one Loop contract sidecar."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = RUNTIME_RUN_LOOP_CONTRACT_SCHEMA_VERSION
    writer_version: str = RUNTIME_RUN_STORE_WRITER_VERSION
    checksum: str
    payload: dict[str, object]


class RuntimeRunStepEnvelope(BaseModel):
    """Checksum-protected envelope for one Runtime Step snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = RUNTIME_RUN_STEP_SCHEMA_VERSION
    writer_version: str = RUNTIME_RUN_STORE_WRITER_VERSION
    checksum: str
    payload: dict[str, object]


class RuntimeRunLoopProgressSnapshot(BaseModel):
    """Durable Loop progress window for one RuntimeRun attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = RUNTIME_RUN_LOOP_PROGRESS_SCHEMA_VERSION
    run_id: str
    attempt_id: str
    updated_at: datetime
    progress: LoopProgressWindow


class RuntimeRunLoopEvaluationSnapshot(BaseModel):
    """Durable Loop evaluation report for one RuntimeRun attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = RUNTIME_RUN_LOOP_EVALUATION_SCHEMA_VERSION
    run_id: str
    attempt_id: str
    saved_at: datetime
    report: LoopEvaluationReport


class RuntimeRunLoopContractSnapshot(BaseModel):
    """Durable Loop contract bound to one RuntimeRun attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = RUNTIME_RUN_LOOP_CONTRACT_SCHEMA_VERSION
    run_id: str
    attempt_id: str
    saved_at: datetime
    contract: LoopContract


class RuntimeRunStoreRecoveryReport(BaseModel):
    """Report emitted when a corrupt contract is isolated."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    message: str
    isolated_path: Path | None = None
    report_path: Path | None = None
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class RuntimeRunStoreMigrationReport(BaseModel):
    """Report for copy-on-write legacy contract migration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    dry_run: bool
    would_write: bool
    written: bool
    backup_path: Path | None = None


class RuntimeRunStoreArchiveReport(BaseModel):
    """Report for a protected archive copy of a RuntimeRun contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["runtime_run_archive"] = "runtime_run_archive"
    run_id: str
    attempt_id: str
    status: str
    revision: int
    archived_at: datetime
    archive_path: Path
    source_contract_path: Path
    source_contract_sha256: str
    contract_checksum: str
    written: bool
    active_contract_retained: bool = True


class RuntimeRunStoreDeleteReport(BaseModel):
    """Report for deleting the active RuntimeRun contract after archival."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["runtime_run_delete"] = "runtime_run_delete"
    run_id: str
    attempt_id: str
    status: str
    revision: int
    deleted_at: datetime
    source_contract_path: Path
    source_contract_sha256: str
    contract_checksum: str
    archive_path: Path
    tombstone_path: Path
    tombstone_sha256: str
    active_contract_deleted: bool
    artifacts_deleted: bool = False


async def _lock_for_path(path: Path) -> asyncio.Lock:
    key = str(path.expanduser().resolve(strict=False))
    async with _LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _PROCESS_LOCKS[key] = lock
        return lock


def _json_dumps(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _checksum_payload(payload: Mapping[str, object]) -> str:
    digest = hashlib.sha256(_json_dumps(payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _chmod_best_effort(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


@contextmanager
def _locked_file(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_best_effort(path.parent, 0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    _chmod_best_effort(path, 0o600)
    try:
        with locked_file_descriptor(fd):
            yield
    finally:
        os.close(fd)


@asynccontextmanager
async def _async_locked_file(path: Path):
    """Wait cooperatively; cancellation before admission cannot write later.

    Guarded writes remain synchronous so commit_guard and durable writes
    cannot interleave. Offloaded operations drain before this lock exits.
    """
    # The worker never acquires flock: ownership transfers before loop-side
    # admission. A holder retains the descriptor even if cancellation discards
    # the worker result; _owned_store_io drains before cleanup can inspect it.
    prepared: list[int] = []

    def prepare() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _chmod_best_effort(path.parent, 0o700)
        prepared.append(os.open(path, os.O_RDWR | os.O_CREAT, 0o600))
        _chmod_best_effort(path, 0o600)

    try:
        await _owned_store_io(None, prepare)
        fd = prepared.pop()
    finally:
        for abandoned_fd in prepared:
            os.close(abandoned_fd)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + RUNTIME_STORE_LOCK_TIMEOUT_SECONDS
    try:
        with ExitStack() as held:
            while True:
                try:
                    held.enter_context(locked_file_descriptor(fd, blocking=False))
                except BlockingIOError:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise RuntimeRunStoreError(
                            "Runtime store lock is busy; no operation was admitted.",
                        ) from None
                    await asyncio.sleep(min(RUNTIME_STORE_LOCK_POLL_SECONDS, remaining))
                else:
                    break
            yield held
    finally:
        os.close(fd)


# pylint: disable=R0904
class RuntimeRunStore:
    """Durable v1 store rooted at ``<workspace>/.lantai/runtime/v1``."""

    def __init__(self, workspace_dir: Path | str) -> None:
        self.workspace_dir = Path(workspace_dir).expanduser()
        self.root = contained_storage_path(
            self.workspace_dir,
            ".lantai",
            "runtime",
            "v1",
        )

    def contract_path(self, run_id: str) -> Path:
        key = safe_storage_key("runtime-run", run_id, suffix="")
        return contained_storage_path(self.root, "runs", key, "contract.json")

    def loop_progress_path(self, run_id: str) -> Path:
        key = safe_storage_key("runtime-run", run_id, suffix="")
        return contained_storage_path(
            self.root,
            "runs",
            key,
            "loop_progress.json",
        )

    def loop_contract_path(self, run_id: str) -> Path:
        key = safe_storage_key("runtime-run", run_id, suffix="")
        return contained_storage_path(
            self.root,
            "runs",
            key,
            "loop_contract.json",
        )

    def loop_evaluation_dir(self, run_id: str) -> Path:
        key = safe_storage_key("runtime-run", run_id, suffix="")
        return contained_storage_path(self.root, "runs", key, "evaluations")

    def loop_evaluation_path(self, run_id: str, report_id: str) -> Path:
        filename = safe_storage_key(
            "runtime-run-loop-evaluation",
            report_id,
            suffix=".json",
        )
        return contained_storage_path(
            self.loop_evaluation_dir(run_id),
            filename,
        )

    def step_dir(self, run_id: str) -> Path:
        key = safe_storage_key("runtime-run", run_id, suffix="")
        return contained_storage_path(self.root, "runs", key, "steps")

    def step_path(self, run_id: str, step_id: str) -> Path:
        filename = safe_storage_key("runtime-step", step_id, suffix=".json")
        return contained_storage_path(self.step_dir(run_id), filename)

    def _backup_path(self, run_id: str, raw: Mapping[str, object]) -> Path:
        key = safe_storage_key("runtime-run", run_id, suffix="")
        checksum = hashlib.sha256(
            _json_dumps(raw).encode("utf-8"),
        ).hexdigest()[:16]
        return contained_storage_path(
            self.root,
            "backups",
            f"{key}.contract.{checksum}.bak",
        )

    def _archive_path(
        self,
        run_id: str,
        *,
        revision: int,
        source_contract_sha256: str,
    ) -> Path:
        key = safe_storage_key("runtime-run", run_id, suffix="")
        return contained_storage_path(
            self.root,
            "archives",
            "runs",
            key,
            (
                "contract.rev"
                f"{revision}"
                "."
                f"{source_contract_sha256[:16]}"
                ".archive.json"
            ),
        )

    def archive_dir(self, run_id: str) -> Path:
        key = safe_storage_key("runtime-run", run_id, suffix="")
        return contained_storage_path(self.root, "archives", "runs", key)

    def _delete_tombstone_path(
        self,
        run_id: str,
        *,
        revision: int,
        source_contract_sha256: str,
    ) -> Path:
        key = safe_storage_key("runtime-run", run_id, suffix="")
        return contained_storage_path(
            self.root,
            "deleted",
            "runs",
            key,
            (
                "contract.rev"
                f"{revision}"
                "."
                f"{source_contract_sha256[:16]}"
                ".deleted.json"
            ),
        )

    def _lock_path(self) -> Path:
        return contained_storage_path(self.root, ".runtime-store.lock")

    async def load_contract(self, run_id: str) -> RuntimeRunContract | None:
        path = self.contract_path(run_id)
        if not await _owned_store_exists(path):
            return None
        lock = await _lock_for_path(path)
        async with lock:
            return await _owned_store_io(
                self._lock_path(),
                lambda: (
                    self._load_contract_unlocked(run_id, path)
                    if path.exists() else None
                ),
            )

    async def list_contract_history(
        self,
        run_id: str,
    ) -> tuple[RuntimeRunContract, ...]:
        (
            "Return checksum-verified active and archived snapshots f"
            "or one run."
        )

        path = self.contract_path(run_id)
        lock = await _lock_for_path(path)
        async with lock:
            def read_unlocked() -> tuple[RuntimeRunContract, ...]:
                snapshots: list[RuntimeRunContract] = []
                if path.exists():
                    snapshots.append(
                        self._load_contract_unlocked(run_id, path),
                    )
                archive_dir = self.archive_dir(run_id)
                if archive_dir.exists():
                    if archive_dir.is_symlink() or not archive_dir.is_dir():
                        raise RuntimeRunStoreError(
                            (
                                "RuntimeRun archive directory is invalid: "
                                f"{run_id}"
                            ),
                        )
                    for archive_path in sorted(
                        archive_dir.glob("contract.rev*.archive.json"),
                    ):
                        snapshots.append(
                            self._load_archived_contract_unlocked(
                                run_id,
                                archive_path,
                            ),
                        )
                unique: dict[str, RuntimeRunContract] = {}
                for contract in snapshots:
                    identity = _json_dumps(contract.model_dump(mode="json"))
                    unique.setdefault(identity, contract)
                return tuple(unique.values())

            return await _owned_store_io(self._lock_path(), read_unlocked)

    async def load_loop_progress_snapshot(
        self,
        run_id: str,
    ) -> RuntimeRunLoopProgressSnapshot | None:
        path = self.loop_progress_path(run_id)
        if not await _owned_store_exists(path):
            return None
        lock = await _lock_for_path(path)
        async with lock:
            def read_unlocked() -> RuntimeRunLoopProgressSnapshot | None:
                if not path.exists():
                    return None
                return self._load_loop_progress_snapshot_unlocked(run_id, path)

            return await _owned_store_io(self._lock_path(), read_unlocked)

    async def load_loop_progress_window(
        self,
        run_id: str,
        *,
        expected_attempt_id: str | None = None,
    ) -> LoopProgressWindow | None:
        snapshot = await self.load_loop_progress_snapshot(run_id)
        if snapshot is None:
            return None
        if (
            expected_attempt_id is not None
            and snapshot.attempt_id != expected_attempt_id
        ):
            return None
        return snapshot.progress

    async def load_loop_contract_snapshot(
        self,
        run_id: str,
        *,
        expected_attempt_id: str | None = None,
    ) -> RuntimeRunLoopContractSnapshot | None:
        path = self.loop_contract_path(run_id)
        if not await _owned_store_exists(path):
            return None
        lock = await _lock_for_path(path)
        async with lock:
            def read_unlocked() -> RuntimeRunLoopContractSnapshot | None:
                if not path.exists():
                    return None
                return self._load_loop_contract_snapshot_unlocked(
                    run_id,
                    path,
                )

            snapshot = await _owned_store_io(self._lock_path(), read_unlocked)
        if snapshot is None:
            return None
        if (
            expected_attempt_id is not None
            and snapshot.attempt_id != expected_attempt_id
        ):
            return None
        return snapshot

    async def load_loop_contract(
        self,
        run_id: str,
        *,
        expected_attempt_id: str | None = None,
    ) -> LoopContract | None:
        snapshot = await self.load_loop_contract_snapshot(
            run_id,
            expected_attempt_id=expected_attempt_id,
        )
        return snapshot.contract if snapshot is not None else None

    async def load_loop_evaluation_report(
        self,
        run_id: str,
        report_id: str,
    ) -> RuntimeRunLoopEvaluationSnapshot | None:
        path = self.loop_evaluation_path(run_id, report_id)
        if not await _owned_store_exists(path):
            return None
        lock = await _lock_for_path(path)
        async with lock:
            def read_unlocked() -> RuntimeRunLoopEvaluationSnapshot | None:
                if not path.exists():
                    return None
                return self._load_loop_evaluation_snapshot_unlocked(
                    run_id,
                    report_id,
                    path,
                )

            return await _owned_store_io(self._lock_path(), read_unlocked)

    async def list_loop_evaluation_reports(
        self,
        run_id: str,
        *,
        expected_attempt_id: str | None = None,
    ) -> list[RuntimeRunLoopEvaluationSnapshot]:
        evaluations_dir = self.loop_evaluation_dir(run_id)

        if not await _owned_store_exists(evaluations_dir):
            return []
        lock = await _lock_for_path(evaluations_dir)
        async with lock:
            def read_unlocked() -> list[RuntimeRunLoopEvaluationSnapshot]:
                if not evaluations_dir.exists():
                    return []
                snapshots: list[RuntimeRunLoopEvaluationSnapshot] = []
                for path in sorted(evaluations_dir.iterdir()):
                    if path.is_symlink() or not path.is_file():
                        continue
                    try:
                        snapshot = (
                            self._load_loop_evaluation_snapshot_unlocked(
                                run_id,
                                None,
                                path,
                            )
                        )
                    except RuntimeRunStoreCorruptionError:
                        continue
                    if (
                        expected_attempt_id is not None
                        and snapshot.attempt_id != expected_attempt_id
                    ):
                        continue
                    snapshots.append(snapshot)
                return sorted(
                    snapshots,
                    key=lambda item: (item.saved_at, item.report.report_id),
                    reverse=True,
                )

            return await _owned_store_io(self._lock_path(), read_unlocked)

    async def load_latest_loop_evaluation_report(
        self,
        run_id: str,
        *,
        expected_attempt_id: str | None = None,
    ) -> RuntimeRunLoopEvaluationSnapshot | None:
        snapshots = await self.list_loop_evaluation_reports(
            run_id,
            expected_attempt_id=expected_attempt_id,
        )
        return snapshots[0] if snapshots else None

    async def save_loop_progress_window(
        self,
        run_id: str,
        *,
        attempt_id: str,
        progress: LoopProgressWindow,
        updated_at: datetime | None = None,
    ) -> RuntimeRunLoopProgressSnapshot:
        snapshot = RuntimeRunLoopProgressSnapshot(
            run_id=run_id,
            attempt_id=attempt_id,
            updated_at=updated_at or datetime.now(timezone.utc),
            progress=progress,
        )
        path = self.loop_progress_path(run_id)
        lock = await _lock_for_path(path)
        async with lock:
            await _owned_store_io(
                self._lock_path(),
                lambda: self._write_loop_progress_snapshot_unlocked(path, snapshot),
            )
        return snapshot

    async def save_loop_contract(
        self,
        run_id: str,
        *,
        attempt_id: str,
        contract: LoopContract,
        saved_at: datetime | None = None,
    ) -> RuntimeRunLoopContractSnapshot:
        snapshot = RuntimeRunLoopContractSnapshot(
            run_id=run_id,
            attempt_id=attempt_id,
            saved_at=saved_at or datetime.now(timezone.utc),
            contract=contract,
        )
        path = self.loop_contract_path(run_id)
        lock = await _lock_for_path(path)
        async with lock:
            await _owned_store_io(
                self._lock_path(),
                lambda: self._write_loop_contract_snapshot_unlocked(path, snapshot),
            )
        return snapshot

    async def save_loop_evaluation_report(
        self,
        run_id: str,
        *,
        attempt_id: str,
        report: LoopEvaluationReport,
        saved_at: datetime | None = None,
    ) -> RuntimeRunLoopEvaluationSnapshot:
        snapshot = RuntimeRunLoopEvaluationSnapshot(
            run_id=run_id,
            attempt_id=attempt_id,
            saved_at=saved_at or datetime.now(timezone.utc),
            report=report,
        )
        path = self.loop_evaluation_path(run_id, report.report_id)
        lock = await _lock_for_path(path)
        async with lock:
            await _owned_store_io(
                self._lock_path(),
                lambda: self._write_loop_evaluation_snapshot_unlocked(path, snapshot),
            )
        return snapshot

    async def save_step(
        self,
        step: RuntimeStepContract,
    ) -> RuntimeStepContract:
        """Persist the latest fenced snapshot for one Runtime Step."""

        path = self.step_path(step.run_id, step.step_id)
        lock = await _lock_for_path(path)
        async with lock:
            def write_unlocked() -> None:
                if path.exists():
                    existing = self._load_step_unlocked(step.run_id, path)
                    if existing.attempt_id != step.attempt_id:
                        raise RuntimeRunStoreConflictError(
                            "RuntimeStep attempt conflict: "
                            f"{existing.attempt_id} != {step.attempt_id}",
                        )
                    if step.revision < existing.revision:
                        raise RuntimeRunStoreConflictError(
                            "RuntimeStep revision moved backwards: "
                            f"{step.revision} < {existing.revision}",
                        )
                self._write_step_unlocked(path, step)

            await _owned_store_io(self._lock_path(), write_unlocked)
        return step

    async def load_step(
        self,
        run_id: str,
        step_id: str,
    ) -> RuntimeStepContract | None:
        path = self.step_path(run_id, step_id)
        if not await _owned_store_exists(path):
            return None
        lock = await _lock_for_path(path)
        async with lock:
            def read_unlocked() -> RuntimeStepContract | None:
                if not path.exists():
                    return None
                return self._load_step_unlocked(run_id, path)

            return await _owned_store_io(self._lock_path(), read_unlocked)

    async def list_steps(
        self,
        run_id: str,
        *,
        expected_attempt_id: str | None = None,
    ) -> list[RuntimeStepContract]:
        root = self.step_dir(run_id)
        if not await _owned_store_exists(root):
            return []
        lock = await _lock_for_path(root)
        async with lock:
            def read_unlocked() -> list[RuntimeStepContract]:
                if not root.exists():
                    return []
                steps: list[RuntimeStepContract] = []
                for path in sorted(root.iterdir()):
                    if path.is_symlink() or not path.is_file():
                        continue
                    try:
                        step = self._load_step_unlocked(run_id, path)
                    except RuntimeRunStoreCorruptionError:
                        continue
                    if (
                        expected_attempt_id is not None
                        and step.attempt_id != expected_attempt_id
                    ):
                        continue
                    steps.append(step)
                return sorted(steps, key=lambda item: (item.index, item.step_id))

            return await _owned_store_io(self._lock_path(), read_unlocked)

    def list_contracts(self) -> list[RuntimeRunContract]:
        """Synchronous compatibility API; async callers use list_contracts_async."""
        if not contained_storage_path(self.root, "runs").exists():
            return []
        with _locked_file(self._lock_path()):
            return self._list_contracts_unlocked()

    async def list_contracts_async(self) -> list[RuntimeRunContract]:
        """List contracts with cancellable, bounded file-lock admission."""
        runs_root = contained_storage_path(self.root, "runs")
        if not await _owned_store_exists(runs_root):
            return []
        return await _owned_store_io(
            self._lock_path(), self._list_contracts_unlocked,
        )

    def _list_contracts_unlocked(self) -> list[RuntimeRunContract]:
        runs_root = contained_storage_path(self.root, "runs")
        if not runs_root.exists():
            return []
        contracts: list[RuntimeRunContract] = []
        for run_dir in sorted(runs_root.iterdir()):
            if run_dir.is_symlink() or not run_dir.is_dir():
                continue
            path = run_dir / "contract.json"
            if not path.exists():
                continue
            try:
                contracts.append(self._load_contract_unlocked(None, path))
            except RuntimeRunStoreCorruptionError:
                continue
        return contracts

    async def save_contract(
        self,
        contract: RuntimeRunContract,
        *,
        expected_revision: int | None,
        expected_attempt_id: str | None = None,
        lease_owner: str | None = None,
        archive_existing: bool = False,
        commit_guard: Callable[[], bool] | None = None,
    ) -> RuntimeRunContract:
        path = self.contract_path(contract.run_id)
        lock = await _lock_for_path(path)
        async with lock:

            def write_unlocked() -> RuntimeRunContract:
                existing = (
                    self._load_contract_unlocked(contract.run_id, path)
                    if path.exists()
                    else None
                )
                self._check_fencing(
                    existing,
                    expected_revision=expected_revision,
                    expected_attempt_id=expected_attempt_id,
                    lease_owner=lease_owner,
                )
                if commit_guard is not None and not commit_guard():
                    raise RuntimeRunStoreConflictError(
                        "RuntimeRun control changed before durable commit.",
                    )
                preserve_previous_attempt = existing is not None and (
                    archive_existing
                    or existing.attempt_id != contract.attempt_id
                )
                if preserve_previous_attempt and existing is not None:
                    raw_text = read_text_no_follow(path)
                    raw = json.loads(raw_text)
                    envelope = RuntimeRunStoreEnvelope.model_validate(raw)
                    self._archive_contract_unlocked(
                        existing.run_id,
                        path,
                        existing,
                        raw_text,
                        raw,
                        envelope,
                    )
                self._write_contract_unlocked(path, contract)
                return contract

            if commit_guard is not None:
                # The live control predicate and commit must not interleave.
                async with _async_locked_file(self._lock_path()):
                    return write_unlocked()

            return await _owned_store_io(self._lock_path(), write_unlocked)

    async def archive_contract(
        self,
        run_id: str,
        *,
        expected_revision: int | None,
        expected_attempt_id: str | None = None,
    ) -> RuntimeRunStoreArchiveReport:
        path = self.contract_path(run_id)
        if not await _owned_store_exists(path):
            raise RuntimeRunStoreError(
                f"RuntimeRun contract not found: {run_id}",
            )
        lock = await _lock_for_path(path)
        async with lock:

            def write_unlocked() -> RuntimeRunStoreArchiveReport:
                if not path.exists():
                    raise RuntimeRunStoreError(
                        f"RuntimeRun contract not found: {run_id}",
                    )
                contract = self._load_contract_unlocked(run_id, path)
                self._check_fencing(
                    contract,
                    expected_revision=expected_revision,
                    expected_attempt_id=expected_attempt_id,
                    lease_owner=None,
                )
                raw_text = read_text_no_follow(path)
                raw = json.loads(raw_text)
                envelope = RuntimeRunStoreEnvelope.model_validate(raw)
                return self._archive_contract_unlocked(
                    run_id,
                    path,
                    contract,
                    raw_text,
                    raw,
                    envelope,
                )

            return await _owned_store_io(self._lock_path(), write_unlocked)

    async def delete_contract(
        self,
        run_id: str,
        *,
        expected_revision: int | None,
        expected_attempt_id: str | None = None,
        require_terminal: bool = True,
    ) -> RuntimeRunStoreDeleteReport:
        path = self.contract_path(run_id)
        if not await _owned_store_exists(path):
            raise RuntimeRunStoreError(
                f"RuntimeRun contract not found: {run_id}",
            )
        lock = await _lock_for_path(path)
        async with lock:

            def write_unlocked() -> RuntimeRunStoreDeleteReport:
                if not path.exists():
                    raise RuntimeRunStoreError(
                        f"RuntimeRun contract not found: {run_id}",
                    )
                contract = self._load_contract_unlocked(run_id, path)
                self._check_fencing(
                    contract,
                    expected_revision=expected_revision,
                    expected_attempt_id=expected_attempt_id,
                    lease_owner=None,
                )
                if (
                    require_terminal
                    and contract.status not in TERMINAL_RUNTIME_RUN_STATUSES
                ):
                    raise RuntimeRunStoreError(
                        (
                            "RuntimeRun contract must be terminal before "
                            "deletion."
                        ),
                    )
                raw_text = read_text_no_follow(path)
                raw = json.loads(raw_text)
                envelope = RuntimeRunStoreEnvelope.model_validate(raw)
                archive_report = self._archive_contract_unlocked(
                    run_id,
                    path,
                    contract,
                    raw_text,
                    raw,
                    envelope,
                )
                source_sha256 = archive_report.source_contract_sha256
                tombstone_path = self._delete_tombstone_path(
                    run_id,
                    revision=contract.revision,
                    source_contract_sha256=source_sha256,
                )
                if path.is_symlink():
                    raise ValueError("Storage path is a symlink")
                latest_text = read_text_no_follow(path)
                latest_sha256 = hashlib.sha256(
                    latest_text.encode("utf-8"),
                ).hexdigest()
                if latest_sha256 != source_sha256:
                    raise RuntimeRunStoreConflictError(
                        "RuntimeRun contract changed before deletion.",
                    )
                deleted_at = datetime.now(timezone.utc)
                tombstone_payload = {
                    "schema_version": RUNTIME_RUN_STORE_SCHEMA_VERSION,
                    "kind": "runtime_run_delete",
                    "run_id": run_id,
                    "attempt_id": contract.attempt_id,
                    "status": contract.status,
                    "revision": contract.revision,
                    "deleted_at": deleted_at.isoformat().replace(
                        "+00:00",
                        "Z",
                    ),
                    "source_contract_path": str(path),
                    "source_contract_sha256": source_sha256,
                    "contract_checksum": envelope.checksum,
                    "archive_path": str(archive_report.archive_path),
                    "active_contract_deleted": True,
                    "artifacts_deleted": False,
                }
                tombstone_text = _json_dumps(tombstone_payload)
                self._write_text_protected(tombstone_path, tombstone_text)
                path.unlink()
                _fsync_dir(path.parent)
                return RuntimeRunStoreDeleteReport(
                    run_id=run_id,
                    attempt_id=contract.attempt_id,
                    status=contract.status,
                    revision=contract.revision,
                    deleted_at=deleted_at,
                    source_contract_path=path,
                    source_contract_sha256=source_sha256,
                    contract_checksum=envelope.checksum,
                    archive_path=archive_report.archive_path,
                    tombstone_path=tombstone_path,
                    tombstone_sha256=hashlib.sha256(
                        tombstone_text.encode("utf-8"),
                    ).hexdigest(),
                    active_contract_deleted=True,
                )

            return await _owned_store_io(self._lock_path(), write_unlocked)

    async def migrate_legacy_contract(
        self,
        raw: Mapping[str, object],
        *,
        dry_run: bool,
    ) -> RuntimeRunStoreMigrationReport:
        contract = migrate_runtime_run_contract(raw)
        path = self.contract_path(contract.run_id)
        backup_path = self._backup_path(contract.run_id, raw)
        existing = await self.load_contract(contract.run_id)
        would_write = existing != contract
        if dry_run:
            return RuntimeRunStoreMigrationReport(
                run_id=contract.run_id,
                dry_run=True,
                would_write=would_write,
                written=False,
                backup_path=backup_path,
            )
        lock = await _lock_for_path(path)
        async with lock:

            def write_unlocked() -> RuntimeRunStoreMigrationReport:
                existing = (
                    self._load_contract_unlocked(contract.run_id, path)
                    if path.exists()
                    else None
                )
                if existing == contract:
                    return RuntimeRunStoreMigrationReport(
                        run_id=contract.run_id,
                        dry_run=False,
                        would_write=False,
                        written=False,
                        backup_path=backup_path,
                    )
                if not backup_path.exists():
                    self._write_text_protected(backup_path, _json_dumps(raw))
                self._write_contract_unlocked(path, contract)
                return RuntimeRunStoreMigrationReport(
                    run_id=contract.run_id,
                    dry_run=False,
                    would_write=True,
                    written=True,
                    backup_path=backup_path,
                )

            return await _owned_store_io(self._lock_path(), write_unlocked)

    def _check_fencing(
        self,
        existing: RuntimeRunContract | None,
        *,
        expected_revision: int | None,
        expected_attempt_id: str | None,
        lease_owner: str | None,
    ) -> None:
        if existing is None:
            return
        if (
            expected_revision is not None
            and existing.revision != expected_revision
        ):
            raise RuntimeRunStoreConflictError(
                (
                    "RuntimeRun revision conflict: "
                    f"{expected_revision}"
                    " != "
                    f"{existing.revision}"
                ),
            )
        if expected_attempt_id and existing.attempt_id != expected_attempt_id:
            raise RuntimeRunStoreConflictError(
                (
                    "RuntimeRun attempt conflict: "
                    f"{expected_attempt_id}"
                    " != "
                    f"{existing.attempt_id}"
                ),
            )
        if (
            lease_owner
            and existing.lease_owner
            and existing.lease_owner != lease_owner
        ):
            raise RuntimeRunStoreLeaseError(
                (
                    "RuntimeRun lease conflict: "
                    f"{lease_owner}"
                    " != "
                    f"{existing.lease_owner}"
                ),
            )

    def _archive_contract_unlocked(
        self,
        run_id: str,
        path: Path,
        contract: RuntimeRunContract,
        raw_text: str,
        raw: Mapping[str, object],
        envelope: RuntimeRunStoreEnvelope,
    ) -> RuntimeRunStoreArchiveReport:
        source_sha256 = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        archive_path = self._archive_path(
            run_id,
            revision=contract.revision,
            source_contract_sha256=source_sha256,
        )
        if archive_path.exists():
            existing = json.loads(read_text_no_follow(archive_path))
            archived_at = datetime.fromisoformat(
                str(existing["archived_at"]).replace("Z", "+00:00"),
            )
            return RuntimeRunStoreArchiveReport(
                run_id=run_id,
                attempt_id=contract.attempt_id,
                status=contract.status,
                revision=contract.revision,
                archived_at=archived_at,
                archive_path=archive_path,
                source_contract_path=path,
                source_contract_sha256=source_sha256,
                contract_checksum=envelope.checksum,
                written=False,
            )

        archived_at = datetime.now(timezone.utc)
        payload = {
            "schema_version": RUNTIME_RUN_STORE_SCHEMA_VERSION,
            "kind": "runtime_run_archive",
            "run_id": run_id,
            "attempt_id": contract.attempt_id,
            "status": contract.status,
            "revision": contract.revision,
            "archived_at": archived_at.isoformat().replace("+00:00", "Z"),
            "source_contract_path": str(path),
            "source_contract_sha256": source_sha256,
            "contract_checksum": envelope.checksum,
            "active_contract_retained": True,
            "envelope": raw,
        }
        self._write_text_protected(archive_path, _json_dumps(payload))
        return RuntimeRunStoreArchiveReport(
            run_id=run_id,
            attempt_id=contract.attempt_id,
            status=contract.status,
            revision=contract.revision,
            archived_at=archived_at,
            archive_path=archive_path,
            source_contract_path=path,
            source_contract_sha256=source_sha256,
            contract_checksum=envelope.checksum,
            written=True,
        )

    def _load_loop_progress_snapshot_unlocked(
        self,
        run_id: str,
        path: Path,
    ) -> RuntimeRunLoopProgressSnapshot:
        try:
            raw_text = read_text_no_follow(path)
            raw = json.loads(raw_text)
            envelope = RuntimeRunLoopProgressEnvelope.model_validate(raw)
            if envelope.checksum != _checksum_payload(envelope.payload):
                raise ValueError("checksum mismatch")
            snapshot = RuntimeRunLoopProgressSnapshot.model_validate(
                envelope.payload,
            )
            if snapshot.run_id != run_id:
                raise ValueError(
                    (
                        "loop progress run_id mismatch: "
                        f"{snapshot.run_id}"
                        " != "
                        f"{run_id}"
                    ),
                )
            return snapshot
        except Exception as exc:
            raise RuntimeRunStoreCorruptionError(
                self._isolate_corrupt_contract(run_id, path, exc),
            ) from exc

    def _load_step_unlocked(
        self,
        run_id: str,
        path: Path,
    ) -> RuntimeStepContract:
        try:
            raw = json.loads(read_text_no_follow(path))
            envelope = RuntimeRunStepEnvelope.model_validate(raw)
            if envelope.checksum != _checksum_payload(envelope.payload):
                raise ValueError("checksum mismatch")
            step = RuntimeStepContract.model_validate(envelope.payload)
            if step.run_id != run_id:
                raise ValueError(
                    f"runtime step run_id mismatch: {step.run_id} != {run_id}",
                )
            return step
        except Exception as exc:
            raise RuntimeRunStoreCorruptionError(
                self._isolate_corrupt_contract(run_id, path, exc),
            ) from exc

    def _write_step_unlocked(
        self,
        path: Path,
        step: RuntimeStepContract,
    ) -> None:
        payload = step.model_dump(mode="json")
        envelope = RuntimeRunStepEnvelope(
            checksum=_checksum_payload(payload),
            payload=payload,
        )
        self._write_text_protected(
            path,
            _json_dumps(envelope.model_dump(mode="json")),
        )

    def _load_loop_contract_snapshot_unlocked(
        self,
        run_id: str,
        path: Path,
    ) -> RuntimeRunLoopContractSnapshot:
        try:
            raw_text = read_text_no_follow(path)
            raw = json.loads(raw_text)
            envelope = RuntimeRunLoopContractEnvelope.model_validate(raw)
            if envelope.checksum != _checksum_payload(envelope.payload):
                raise ValueError("checksum mismatch")
            snapshot = RuntimeRunLoopContractSnapshot.model_validate(
                envelope.payload,
            )
            if snapshot.run_id != run_id:
                raise ValueError(
                    (
                        "loop contract run_id mismatch: "
                        f"{snapshot.run_id}"
                        " != "
                        f"{run_id}"
                    ),
                )
            return snapshot
        except Exception as exc:
            raise RuntimeRunStoreCorruptionError(
                self._isolate_corrupt_contract(run_id, path, exc),
            ) from exc

    def _write_loop_contract_snapshot_unlocked(
        self,
        path: Path,
        snapshot: RuntimeRunLoopContractSnapshot,
    ) -> None:
        payload = snapshot.model_dump(mode="json")
        envelope = RuntimeRunLoopContractEnvelope(
            checksum=_checksum_payload(payload),
            payload=payload,
        )
        self._write_text_protected(
            path,
            _json_dumps(envelope.model_dump(mode="json")),
        )

    def _write_loop_progress_snapshot_unlocked(
        self,
        path: Path,
        snapshot: RuntimeRunLoopProgressSnapshot,
    ) -> None:
        payload = snapshot.model_dump(mode="json")
        envelope = RuntimeRunLoopProgressEnvelope(
            checksum=_checksum_payload(payload),
            payload=payload,
        )
        self._write_text_protected(
            path,
            _json_dumps(envelope.model_dump(mode="json")),
        )

    def _load_loop_evaluation_snapshot_unlocked(
        self,
        run_id: str,
        report_id: str | None,
        path: Path,
    ) -> RuntimeRunLoopEvaluationSnapshot:
        try:
            raw_text = read_text_no_follow(path)
            raw = json.loads(raw_text)
            envelope = RuntimeRunLoopEvaluationEnvelope.model_validate(raw)
            if envelope.checksum != _checksum_payload(envelope.payload):
                raise ValueError("checksum mismatch")
            snapshot = RuntimeRunLoopEvaluationSnapshot.model_validate(
                envelope.payload,
            )
            if snapshot.run_id != run_id:
                raise ValueError(
                    (
                        "loop evaluation run_id mismatch: "
                        f"{snapshot.run_id}"
                        " != "
                        f"{run_id}"
                    ),
                )
            canonical_path = self.loop_evaluation_path(
                run_id,
                snapshot.report.report_id,
            )
            if path.expanduser().resolve(
                strict=False,
            ) != canonical_path.expanduser().resolve(strict=False):
                raise ValueError(
                    "loop evaluation canonical path mismatch: "
                    f"{path} != {canonical_path}",
                )
            if (
                report_id is not None
                and snapshot.report.report_id != report_id
            ):
                raise ValueError(
                    "loop evaluation report_id mismatch: "
                    f"{snapshot.report.report_id} != {report_id}",
                )
            return snapshot
        except Exception as exc:
            raise RuntimeRunStoreCorruptionError(
                self._isolate_corrupt_contract(run_id, path, exc),
            ) from exc

    def _write_loop_evaluation_snapshot_unlocked(
        self,
        path: Path,
        snapshot: RuntimeRunLoopEvaluationSnapshot,
    ) -> None:
        payload = snapshot.model_dump(mode="json")
        envelope = RuntimeRunLoopEvaluationEnvelope(
            checksum=_checksum_payload(payload),
            payload=payload,
        )
        self._write_text_protected(
            path,
            _json_dumps(envelope.model_dump(mode="json")),
        )

    def _load_contract_unlocked(
        self,
        run_id: str | None,
        path: Path,
    ) -> RuntimeRunContract:
        report_run_id = run_id or path.parent.name
        try:
            raw_text = read_text_no_follow(path)
            raw = json.loads(raw_text)
            envelope = RuntimeRunStoreEnvelope.model_validate(raw)
            if envelope.checksum != _checksum_payload(envelope.payload):
                raise ValueError("checksum mismatch")
            payload_run_id = str(envelope.payload.get("run_id") or "")
            if run_id is None and payload_run_id:
                report_run_id = payload_run_id
            if "attempt_reason" in envelope.payload:
                contract = RuntimeAttemptContract.model_validate(
                    envelope.payload,
                )
            else:
                contract = RuntimeRunContract.model_validate(envelope.payload)
            if run_id is not None and contract.run_id != run_id:
                raise ValueError(
                    f"contract run_id mismatch: {contract.run_id} != {run_id}",
                )
            if run_id is None:
                expected_path = self.contract_path(contract.run_id)
                if path.resolve(strict=False) != expected_path.resolve(
                    strict=False,
                ):
                    raise ValueError(
                        "contract storage path mismatch: "
                        f"{path} != {expected_path}",
                    )
            return contract
        except Exception as exc:
            raise RuntimeRunStoreCorruptionError(
                self._isolate_corrupt_contract(report_run_id, path, exc),
            ) from exc

    # pylint: disable=R0912
    def _load_archived_contract_unlocked(
        self,
        run_id: str,
        path: Path,
    ) -> RuntimeRunContract:
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError("archive path is not a regular file")
            raw = json.loads(read_text_no_follow(path))
            if raw.get("schema_version") != RUNTIME_RUN_STORE_SCHEMA_VERSION:
                raise ValueError("archive schema version mismatch")
            if raw.get("kind") != "runtime_run_archive":
                raise ValueError("archive kind mismatch")
            if raw.get("run_id") != run_id:
                raise ValueError("archive run_id mismatch")
            envelope_raw = raw.get("envelope")
            envelope = RuntimeRunStoreEnvelope.model_validate(envelope_raw)
            if envelope.checksum != _checksum_payload(envelope.payload):
                raise ValueError("archive contract checksum mismatch")
            if raw.get("contract_checksum") != envelope.checksum:
                raise ValueError("archive metadata checksum mismatch")
            source_sha256 = hashlib.sha256(
                _json_dumps(envelope_raw).encode("utf-8"),
            ).hexdigest()
            if raw.get("source_contract_sha256") != source_sha256:
                raise ValueError("archive source checksum mismatch")
            if "attempt_reason" in envelope.payload:
                contract = RuntimeAttemptContract.model_validate(
                    envelope.payload,
                )
            else:
                contract = RuntimeRunContract.model_validate(envelope.payload)
            if contract.run_id != run_id:
                raise ValueError("archived contract run_id mismatch")
            if raw.get("attempt_id") != contract.attempt_id:
                raise ValueError("archived contract attempt_id mismatch")
            if raw.get("revision") != contract.revision:
                raise ValueError("archived contract revision mismatch")
            if raw.get("status") != contract.status:
                raise ValueError("archived contract status mismatch")
            expected_path = self._archive_path(
                run_id,
                revision=contract.revision,
                source_contract_sha256=source_sha256,
            )
            if path.resolve(strict=False) != expected_path.resolve(
                strict=False,
            ):
                raise ValueError("archive storage path mismatch")
            return contract
        except Exception as exc:
            raise RuntimeRunStoreError(
                f"RuntimeRun archive is corrupt: {path}: {exc}",
            ) from exc

    # pylint: enable=R0912

    def _write_contract_unlocked(
        self,
        path: Path,
        contract: RuntimeRunContract,
    ) -> None:
        payload = contract.model_dump(mode="json")
        envelope = RuntimeRunStoreEnvelope(
            checksum=_checksum_payload(payload),
            payload=payload,
        )
        self._write_text_protected(
            path,
            _json_dumps(envelope.model_dump(mode="json")),
        )

    def _write_text_protected(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _chmod_best_effort(self.root, 0o700)
        _chmod_best_effort(path.parent, 0o700)
        write_text_atomic_no_follow(path, text)
        _chmod_best_effort(path, 0o600)
        _fsync_dir(path.parent)

    def _isolate_corrupt_contract(
        self,
        run_id: str,
        path: Path,
        exc: BaseException,
    ) -> RuntimeRunStoreRecoveryReport:
        corrupt_dir = contained_storage_path(self.root, "corrupt")
        corrupt_dir.mkdir(parents=True, exist_ok=True)
        _chmod_best_effort(corrupt_dir, 0o700)
        suffix = uuid4().hex
        isolated_path = (
            corrupt_dir / f"{path.parent.name}.contract.{suffix}.bad"
        )
        report_path = (
            corrupt_dir / f"{path.parent.name}.contract.{suffix}.report.json"
        )
        if path.exists():
            os.replace(path, isolated_path)
        report = RuntimeRunStoreRecoveryReport(
            run_id=run_id,
            message=f"RuntimeRun contract is corrupt: {exc}",
            isolated_path=isolated_path,
            report_path=report_path,
        )
        write_text_atomic_no_follow(
            report_path,
            _json_dumps(report.model_dump(mode="json")),
        )
        _chmod_best_effort(report_path, 0o600)
        _fsync_dir(corrupt_dir)
        return report


# pylint: enable=R0904
