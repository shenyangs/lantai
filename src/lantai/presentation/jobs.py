# -*- coding: utf-8 -*-
"""Durable presentation jobs with cross-process single-writer leases."""

from __future__ import annotations

import json
import os
import re
import threading
from contextlib import contextmanager, ExitStack
from functools import wraps
from inspect import signature
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, Sequence
from uuid import uuid4

from pydantic import Field

from ..utils.file_lock import locked_file_descriptor
from ..utils.operation_control import (
    OperationControl,
    bind_operation,
    current_operation,
    controlled_capacity,
)

from .failure import normalize_persisted_failure_routes
from .models import (
    JobStage,
    JobStatus,
    PresentationFailureCategory,
    PresentationFailureRoute,
    PresentationJobEvent,
    PresentationJobRequest,
    PresentationRecoveryAction,
    PresentationStageMetric,
    SkillExecutionIdentity,
    StrictModel,
)


class PresentationWriterBusyError(RuntimeError):
    """Raised when another process owns the authoritative writer lease."""


class PresentationJobInterruptedError(RuntimeError):
    """Raised at a stage boundary after an explicit pause or cancellation."""


MAX_PRESENTATION_RECOVERY_ATTEMPTS = 3
MAX_PRESENTATION_EXTERNAL_RECOVERY_ATTEMPTS = 2
MAX_PRESENTATION_IMPLEMENTATION_RECOVERY_ATTEMPTS = 2
CURRENT_PRESENTATION_JOB_SCHEMA_VERSION = "2.0"
SUPPORTED_PRESENTATION_JOB_SCHEMA_VERSIONS = frozenset({"1.0", "2.0"})


class PresentationOperation(StrictModel):
    operation_id: str = Field(default_factory=lambda: f"edit-{uuid4().hex}")
    kind: str = "explicit_edit"
    deadline_at: datetime
    cost_budget_units: int = Field(ge=1)
    cost_used_units: int = Field(default=0, ge=0)
    status: JobStatus = JobStatus.RUNNING
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    completed_at: datetime | None = None


class PresentationJobRecord(StrictModel):
    schema_version: str = CURRENT_PRESENTATION_JOB_SCHEMA_VERSION
    job_id: str
    conversation_id: str
    request: PresentationJobRequest
    skill_identity: SkillExecutionIdentity | None = None
    stage: JobStage = JobStage.CREATED
    status: JobStatus = JobStatus.PENDING
    last_sequence: int = 0
    checkpoints: dict[str, str] = Field(default_factory=dict)
    events: list[PresentationJobEvent] = Field(default_factory=list)
    stage_metrics: list[PresentationStageMetric] = Field(default_factory=list)
    runtime_run_id: str = Field(default_factory=lambda: f"run-{uuid4().hex}")
    attempt_id: str | None = None
    attempt_count: int = Field(default=0, ge=0)
    recovery_attempt_count: int = Field(default=0, ge=0)
    external_recovery_attempt_count: int = Field(default=0, ge=0)
    implementation_recovery_attempt_count: int = Field(default=0, ge=0)
    implementation_recovery_fixes: list[str] = Field(default_factory=list)
    generation: int = Field(default=0, ge=0)
    base_pptx_sha256: str | None = None
    deadline_at: datetime | None = None
    timeout_seconds: int = Field(default=900, ge=30, le=7200)
    cost_budget_units: int = Field(default=100, ge=1)
    cost_used_units: int = Field(default=0, ge=0)
    idempotency_key: str | None = None
    pipeline_version: str = "lantai-presentation-v3"
    model_config_fingerprint: str | None = None
    input_manifest_hash: str | None = None
    control_epoch: int = Field(default=0, ge=0)
    active_operation: PresentationOperation | None = None
    operation_history: list[PresentationOperation] = Field(
        default_factory=list,
    )
    resource_settlement_pending: bool = False
    active_lease_id: str | None = None
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class PresentationWriterLease(StrictModel):
    """One recoverable mutation attempt for a presentation job."""

    lease_id: str = Field(default_factory=lambda: f"lease-{uuid4().hex}")
    job_id: str
    runtime_run_id: str
    attempt_id: str
    generation: int = Field(ge=1)
    base_pptx_sha256: str | None = None
    acquired_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    deadline_at: datetime
    pid: int = Field(default_factory=os.getpid)
    recovered_lease_id: str | None = None
    control_epoch: int = Field(default=0, ge=0)
    operation_id: str | None = None


class PresentationExecutionLimits:
    """Bound expensive work in-process; file leases cover all processes."""

    def __init__(
        self,
        synthesis: int = 1,
        rendering: int = 2,
        critics: int = 3,
    ) -> None:
        self._synthesis = threading.BoundedSemaphore(synthesis)
        self._rendering = threading.BoundedSemaphore(rendering)
        self._critics = threading.BoundedSemaphore(critics)

    @contextmanager
    def synthesis(self) -> Iterator[None]:
        with controlled_capacity(self._synthesis):
            yield

    @contextmanager
    def rendering(self) -> Iterator[None]:
        with controlled_capacity(self._rendering):
            yield

    @contextmanager
    def critic(
        self,
        *,
        ensure_active=None,
        poll_seconds=0.05,
    ) -> Iterator[None]:
        with controlled_capacity(self._critics, ensure_active, poll_seconds):
            yield


_SHARED_PRESENTATION_EXECUTION_LIMITS = PresentationExecutionLimits(
    synthesis=1,
    rendering=2,
    critics=3,
)


def shared_presentation_execution_limits() -> PresentationExecutionLimits:
    """Return the application-process capacity shared by all PPT jobs."""

    return _SHARED_PRESENTATION_EXECUTION_LIMITS


# One authority retains the existing record/lease API for its callers.
# Splitting the facade would change the ownership/type boundary.
class PresentationJobStore:  # pylint: disable=too-many-public-methods
    def __init__(
        self,
        root: str | Path,
        *,
        read_only: bool = False,
    ) -> None:
        self.root = Path(root)
        self.read_only = read_only
        if not self.read_only:
            self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._record_guards = threading.local()

    def _assert_writable(self) -> None:
        if self.read_only:
            raise RuntimeError(
                "read-only presentation job store cannot mutate state",
            )

    @contextmanager
    def _file_lock(
        self,
        path: Path,
        *,
        exclusive: bool = True,
        blocking: bool = True,
    ) -> Iterator[object]:
        self._assert_writable()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle, ExitStack() as locks:
            try:
                locks.enter_context(
                    locked_file_descriptor(
                        handle.fileno(),
                        exclusive=exclusive,
                        blocking=blocking,
                    ),
                )
            except BlockingIOError as exc:
                raise PresentationWriterBusyError(
                    "presentation job already has an authoritative writer",
                ) from exc
            yield handle

    @contextmanager
    def _record_guard(
        self,
        job_id: str,
        *,
        exclusive: bool = True,
    ) -> Iterator[None]:
        guards = getattr(self._record_guards, "held", None)
        if guards is None:
            guards = self._record_guards.held = {}
        if job_id in guards:
            if exclusive and not guards[job_id]:
                raise RuntimeError("cannot upgrade a presentation read lock")
            yield
            return
        with self._file_lock(
            self.job_dir(job_id) / ".manifest.lock",
            exclusive=exclusive,
        ):
            guards[job_id] = exclusive
            try:
                yield
            finally:
                del guards[job_id]

    def create(
        self,
        request: PresentationJobRequest,
        *,
        skill_identity: SkillExecutionIdentity | None = None,
        runtime_run_id: str | None = None,
    ) -> PresentationJobRecord:
        with self._lock, self._file_lock(self.root / ".create.lock"):
            if request.idempotency_key:
                for record in self.list_records(request.conversation_id):
                    if record.idempotency_key == request.idempotency_key:
                        if record.request != request:
                            raise ValueError(
                                (
                                    "idempotency key is already bound "
                                    "to another request"
                                ),
                            )
                        return record
            job_id = f"presentation-{uuid4().hex}"
            record = PresentationJobRecord(
                job_id=job_id,
                conversation_id=request.conversation_id,
                request=request,
                skill_identity=skill_identity,
                runtime_run_id=(runtime_run_id or f"run-{uuid4().hex}"),
                timeout_seconds=request.timeout_seconds,
                cost_budget_units=request.cost_budget_units,
                idempotency_key=request.idempotency_key,
                pipeline_version=request.pipeline_version,
                model_config_fingerprint=request.model_config_fingerprint,
                input_manifest_hash=request.input_manifest_hash,
            )
            record.deadline_at = record.created_at + timedelta(
                seconds=record.timeout_seconds,
            )
            self._write(record)
            return record

    def _read_unlocked(self, job_id: str) -> PresentationJobRecord:
        path = self._record_path(job_id)
        if not path.is_file():
            raise KeyError(f"unknown presentation job: {job_id}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("presentation job manifest must be a JSON object")
        schema_version = str(payload.get("schema_version", "1.0"))
        if schema_version not in SUPPORTED_PRESENTATION_JOB_SCHEMA_VERSIONS:
            raise ValueError(
                "unsupported presentation job schema: " f"{schema_version}",
            )
        if schema_version == "1.0":
            # v1 pre-dates Native Run ownership.  Give the historical job one
            # stable, non-Native owner in memory so repeated status reads do
            # not invent a different task owner.  A real resumed writer still
            # adopts the current Runtime Run explicitly through writer_lease.
            payload = dict(payload)
            payload["schema_version"] = CURRENT_PRESENTATION_JOB_SCHEMA_VERSION
            payload.setdefault(
                "runtime_run_id",
                f"run-legacy-{job_id.removeprefix('presentation-')}",
            )
        record = PresentationJobRecord.model_validate(payload)
        # Historical jobs receive the original acceptance deadline, never a
        # read-time renewal. Status reads remain side-effect free.
        if record.deadline_at is None:
            record.deadline_at = record.created_at + timedelta(
                seconds=record.timeout_seconds,
            )
        return record

    def get(self, job_id: str) -> PresentationJobRecord:
        if self.read_only:
            # Manifests are committed with os.replace, so a reader can observe
            # either complete version without creating a lock or job path.
            return self._read_unlocked(job_id)
        with self._record_guard(job_id, exclusive=False):
            return self._read_unlocked(job_id)

    def get_for_conversation(
        self,
        job_id: str,
        conversation_id: str,
    ) -> PresentationJobRecord:
        record = self.get(job_id)
        if record.conversation_id != conversation_id:
            raise PermissionError(
                "presentation job belongs to a different conversation",
            )
        return record

    def list_records(
        self,
        conversation_id: str | None = None,
    ) -> list[PresentationJobRecord]:
        records: list[PresentationJobRecord] = []
        for path in self.root.glob("presentation-*/manifest.json"):
            try:
                record = self.get(path.parent.name)
            except (OSError, ValueError, KeyError):
                continue
            if (
                conversation_id is None
                or record.conversation_id == conversation_id
            ):
                records.append(record)
        return sorted(records, key=lambda item: item.updated_at, reverse=True)

    def append_event(
        self,
        job_id: str,
        *,
        stage: JobStage,
        status: JobStatus,
        message: str,
        slide_id: str | None = None,
        checkpoint: str | None = None,
        recoverable: bool = False,
        failure_routes: Sequence[PresentationFailureRoute] = (),
        allow_paused_resume: bool = False,
        settlement: bool = False,
        expected_control_epoch: int | None = None,
    ) -> PresentationJobEvent:
        with self._lock, self._record_guard(job_id):
            record = self._read_unlocked(job_id)
            if (
                expected_control_epoch is not None
                and record.control_epoch != expected_control_epoch
            ):
                raise PresentationJobInterruptedError(
                    "retired presentation worker cannot publish a failure",
                )
            self._assert_producer(
                record,
                allow_terminal=(status == JobStatus.FAILED or settlement),
                settlement=settlement,
            )
            if settlement and record.active_operation is not None:
                if record.active_operation.status == JobStatus.RUNNING:
                    record.active_operation.status = JobStatus.FAILED
                # The original verified deck survives a stopped/failed edit.
                record.status = JobStatus.COMPLETED
            if record.status == JobStatus.CANCELLED:
                raise PresentationJobInterruptedError(
                    "cancelled presentation job cannot be revived",
                )
            if record.status == JobStatus.COMPLETED and status in {
                JobStatus.PAUSED,
                JobStatus.CANCELLED,
            }:
                raise ValueError(
                    "completed presentation job cannot pause or cancel",
                )
            if record.status == JobStatus.PAUSED and not (
                (allow_paused_resume and status == JobStatus.RUNNING)
                or status == JobStatus.CANCELLED
            ):
                raise PresentationJobInterruptedError(
                    "paused presentation job requires explicit resume",
                )
            if (
                record.status == JobStatus.FAILED
                and status
                in {
                    JobStatus.RUNNING,
                    JobStatus.COMPLETED,
                }
                and not (allow_paused_resume and status == JobStatus.RUNNING)
            ):
                raise PresentationJobInterruptedError(
                    "failed presentation job requires classified retry",
                )
            event = PresentationJobEvent(
                job_id=job_id,
                sequence=record.last_sequence + 1,
                stage=stage,
                status=status,
                slide_id=slide_id,
                message=message,
                checkpoint=checkpoint,
                recoverable=recoverable,
                failure_routes=list(failure_routes),
            )
            record.last_sequence = event.sequence
            record.stage = stage
            record.status = status
            record.updated_at = datetime.now(timezone.utc)
            record.events.append(event)
            if checkpoint:
                record.checkpoints[stage.value] = checkpoint
            self._write(record)
            return event

    def complete_after_verified_publication(
        self,
        job_id: str,
        *,
        message: str,
        checkpoint: str,
        publish: Callable[[], None],
        rollback: Callable[[], None] | None = None,
        slide_id: str | None = None,
    ) -> PresentationJobEvent:
        """Serialize the final file/version publication against explicit Stop.

        The callback owns rollback if its file or version write fails; an
        optional rollback also covers a final manifest-write failure. Holding
        the job manifest lock keeps cancel/pause from interleaving with a
        verified candidate's promotion and completed lifecycle event.
        """

        with self._lock, self._record_guard(job_id):
            record = self._read_unlocked(job_id)
            if record.status in {
                JobStatus.PAUSED,
                JobStatus.CANCELLED,
                JobStatus.FAILED,
            }:
                raise PresentationJobInterruptedError(
                    f"presentation job is {record.status.value}",
                )
            if record.status == JobStatus.COMPLETED:
                raise ValueError("completed presentation job cannot publish again")
            # This checks the bound writer and its effective absolute deadline:
            # an explicit edit owns a fresh operation, while generation/recovery
            # retains the original deadline. Do not recheck a historic generation
            # deadline after admitting the edit's own operation.
            self._assert_producer(record)
            try:
                publish()
                event = PresentationJobEvent(
                    job_id=job_id,
                    sequence=record.last_sequence + 1,
                    stage=JobStage.COMPLETED,
                    status=JobStatus.COMPLETED,
                    slide_id=slide_id,
                    message=message,
                    checkpoint=checkpoint,
                )
                record.last_sequence = event.sequence
                record.stage = event.stage
                record.status = event.status
                record.updated_at = datetime.now(timezone.utc)
                record.events.append(event)
                record.checkpoints[event.stage.value] = checkpoint
                self._write(record)
            except Exception:
                if rollback is not None:
                    rollback()
                raise
            return event

    @contextmanager
    def writer_lease(
        self,
        job_id: str,
        *,
        base_pptx_sha256: str | None = None,
        minimum_generation: int = 0,
        runtime_run_id: str | None = None,
    ) -> Iterator[PresentationWriterLease]:
        """Acquire the only cross-process mutation authority for this job."""

        lock_path = self.job_dir(job_id) / ".writer.lock"
        with self._file_lock(lock_path, blocking=False) as writer_handle:
            with self._record_guard(job_id):
                record = self._read_unlocked(job_id)
                now = datetime.now(timezone.utc)
                if record.resource_settlement_pending:
                    raise PresentationWriterBusyError(
                        "prior presentation resources have not settled",
                    )
                if record.status in {
                    JobStatus.PAUSED,
                    JobStatus.CANCELLED,
                    JobStatus.FAILED,
                }:
                    raise PresentationJobInterruptedError(
                        f"presentation job is {record.status.value}",
                    )
                if record.status == JobStatus.COMPLETED:
                    record.active_operation = PresentationOperation(
                        deadline_at=now
                        + timedelta(seconds=record.timeout_seconds),
                        cost_budget_units=record.cost_budget_units,
                    )
                elif (
                    record.deadline_at is not None
                    and now >= record.deadline_at
                ):
                    raise TimeoutError(
                        "presentation job exceeded its original deadline",
                    )
                recovered_lease_id = record.active_lease_id
                attempt_id = f"attempt-{uuid4().hex}"
                effective_runtime_run_id = (
                    runtime_run_id or record.runtime_run_id
                )
                lease = PresentationWriterLease(
                    job_id=job_id,
                    runtime_run_id=effective_runtime_run_id,
                    attempt_id=attempt_id,
                    generation=max(record.generation, minimum_generation) + 1,
                    base_pptx_sha256=base_pptx_sha256,
                    deadline_at=(
                        record.active_operation.deadline_at
                        if record.active_operation
                        else record.deadline_at
                    ),
                    control_epoch=record.control_epoch,
                    operation_id=(
                        record.active_operation.operation_id
                        if record.active_operation
                        else None
                    ),
                    recovered_lease_id=recovered_lease_id,
                )
                record.attempt_id = attempt_id
                record.attempt_count += 1
                record.runtime_run_id = effective_runtime_run_id
                record.generation = lease.generation
                record.base_pptx_sha256 = base_pptx_sha256
                record.active_lease_id = lease.lease_id
                record.updated_at = now
                self._write(record)
                self._atomic_text(
                    self.job_dir(job_id) / "lease.json",
                    lease.model_dump_json(indent=2),
                )
            control = None
            try:
                with self.operation_scope(
                    job_id,
                    lease,
                    writer_lock_fd=writer_handle.fileno(),
                ) as control:
                    yield lease
                    if control.unsettled.is_set():
                        raise RuntimeError(
                            "presentation resources have not settled",
                        )
            finally:
                with self._record_guard(job_id):
                    record = self._read_unlocked(job_id)
                    if record.active_lease_id == lease.lease_id:
                        if control is not None and control.unsettled.is_set():
                            # Keep the durable claim when resource settlement
                            # cannot be proved. A replacement may not
                            # recover it.
                            record.resource_settlement_pending = True
                            self._write(record)
                            raise RuntimeError(
                                (
                                    "presentation resource settlement "
                                    "is still pending"
                                ),
                            )
                        self._archive_settled_operation(record)
                        record.active_lease_id = None
                        record.updated_at = datetime.now(timezone.utc)
                        self._write(record)
                (self.job_dir(job_id) / "lease.json").unlink(missing_ok=True)

    @staticmethod
    def _archive_settled_operation(record: PresentationJobRecord) -> None:
        if record.active_operation is not None:
            operation = record.active_operation
            if operation.status == JobStatus.RUNNING:
                operation.status = (
                    JobStatus.COMPLETED
                    if record.status == JobStatus.COMPLETED
                    else JobStatus.FAILED
                )
            if operation.status == JobStatus.COMPLETED:
                # Only a committed edit joins the historic
                # cumulative meter. Interrupted edit usage is
                # still recorded on its own operation/metrics.
                record.cost_used_units += operation.cost_used_units
            operation.completed_at = datetime.now(timezone.utc)
            record.operation_history.append(operation)
            record.active_operation = None

    def _owner_key(self, job_id: str) -> tuple[str, str]:
        return (str(self.root.resolve()), job_id)

    @staticmethod
    def _lease_identity(lease: PresentationWriterLease) -> tuple:
        return (
            lease.lease_id,
            lease.runtime_run_id,
            lease.attempt_id,
            lease.generation,
            lease.control_epoch,
            lease.operation_id,
        )

    @staticmethod
    def _record_identity(record: PresentationJobRecord) -> tuple:
        return (
            record.active_lease_id,
            record.runtime_run_id,
            record.attempt_id,
            record.generation,
            record.control_epoch,
            record.active_operation.operation_id
            if record.active_operation
            else None,
        )

    def _assert_producer(
        self,
        record: PresentationJobRecord,
        *,
        allow_terminal=False,
        settlement=False,
    ) -> None:
        control = current_operation()
        if control is not None and control.owner_key == self._owner_key(
            record.job_id,
        ):
            identity = self._record_identity(record)
            # Rollback only belongs to the same edit and file writer. A Stop
            # epoch may retire its model work while cleanup retains ownership.
            same_cleanup = (
                settlement
                and record.active_operation is not None
                and control.identity[:4] == identity[:4]
                and control.identity[5] == identity[5]
            )
            if control.identity != identity and not same_cleanup:
                raise PresentationJobInterruptedError(
                    "presentation writer identity was retired "
                    f"({record.status.value})",
                )
        elif record.active_lease_id is not None:
            raise PresentationJobInterruptedError(
                "presentation mutation has no bound writer",
            )
        if allow_terminal:
            return
        if record.status in {
            JobStatus.PAUSED,
            JobStatus.CANCELLED,
            JobStatus.FAILED,
        }:
            raise PresentationJobInterruptedError(
                f"presentation job is {record.status.value}",
            )
        if (
            record.active_operation is not None
            and record.active_operation.status != JobStatus.RUNNING
        ):
            raise PresentationJobInterruptedError(
                f"presentation edit is {record.active_operation.status.value}",
            )
        deadline = (
            record.active_operation.deadline_at
            if record.active_operation
            else record.deadline_at
        )
        if deadline is not None and datetime.now(timezone.utc) >= deadline:
            raise TimeoutError(
                "presentation job exceeded its absolute deadline",
            )

    @contextmanager
    def operation_scope(
        self,
        job_id: str,
        lease: PresentationWriterLease | None = None,
        *,
        writer_lock_fd: int | None = None,
    ):
        existing = current_operation()
        if existing is not None and existing.owner_key == self._owner_key(
            job_id,
        ):
            if (
                lease is not None
                and existing.identity != self._lease_identity(lease)
            ):
                raise PresentationJobInterruptedError(
                    "presentation operation identity mismatch",
                )
            yield existing
            return
        if lease is None:
            record = self.get(job_id)
            if record.active_lease_id is None:
                raise PresentationJobInterruptedError(
                    "presentation stage has no active writer",
                )
            lease = PresentationWriterLease.model_validate_json(
                (self.job_dir(job_id) / "lease.json").read_text(),
            )
        identity = self._lease_identity(lease)

        def check() -> None:
            self._assert_producer(self.get(job_id))

        def remaining() -> float:
            return (
                lease.deadline_at - datetime.now(timezone.utc)
            ).total_seconds()

        control = OperationControl(
            self._owner_key(job_id),
            identity,
            check,
            remaining,
            publication_guard=lambda: self.publication_guard(job_id),
            writer_lock_fd=writer_lock_fd,
        )
        with bind_operation(control):
            check()
            yield control

    @contextmanager
    def publication_guard(self, job_id: str):
        with self._lock, self._record_guard(job_id):
            self._assert_producer(self._read_unlocked(job_id))
            yield

    def assert_execution_active(self, job_id: str) -> None:
        self._assert_producer(self.get(job_id))

    def assert_execution_budget(self, job_id: str) -> None:
        self.assert_execution_active(job_id)
        record = self.get(job_id)
        budget = record.active_operation or record
        if budget.cost_used_units >= budget.cost_budget_units:
            raise RuntimeError(
                "presentation operation exhausted its cost budget",
            )

    def record_stage_metric(
        self,
        job_id: str,
        metric: PresentationStageMetric,
    ) -> PresentationJobRecord:
        """Persist telemetry without changing task stage or lifecycle state."""

        with self._record_guard(job_id):
            record = self._read_unlocked(job_id)
            record.stage_metrics.append(metric)
            record.updated_at = datetime.now(timezone.utc)
            self._write(record)
            return record

    def consume_cost(self, job_id: str, units: int) -> PresentationJobRecord:
        if units < 0:
            raise ValueError("cost units must be non-negative")
        with self._record_guard(job_id):
            record = self._read_unlocked(job_id)
            self._assert_producer(record)
            budget = record.active_operation or record
            if budget.cost_used_units + units > budget.cost_budget_units:
                raise RuntimeError(
                    "presentation attempt would exceed its cost budget",
                )
            budget.cost_used_units += units
            record.updated_at = datetime.now(timezone.utc)
            self._write(record)
            return record

    def events_after(
        self,
        job_id: str,
        sequence: int,
        conversation_id: str,
    ) -> list[PresentationJobEvent]:
        record = self.get_for_conversation(job_id, conversation_id)
        return [event for event in record.events if event.sequence > sequence]

    def _change_control(
        self,
        job_id: str,
        conversation_id: str,
        target: JobStatus,
    ) -> PresentationJobEvent:
        with self._lock, self._record_guard(job_id):
            record = self._read_unlocked(job_id)
            if record.conversation_id != conversation_id:
                raise PermissionError(
                    "presentation job belongs to a different conversation",
                )
            editing = (
                record.status == JobStatus.COMPLETED
                and record.active_operation is not None
            )
            current_status = (
                record.active_operation.status if editing else record.status
            )
            if target == JobStatus.CANCELLED and current_status == target:
                # Repeated Stop observes the same cancellation. Cleanup still
                # owns the writer until its original resources have settled.
                return next(
                    event
                    for event in reversed(record.events)
                    if event.status == target
                )
            allowed = (
                {JobStatus.PAUSED}
                if target == JobStatus.RUNNING
                else {JobStatus.PENDING, JobStatus.RUNNING}
            )
            if target == JobStatus.CANCELLED:
                allowed |= {JobStatus.PAUSED, JobStatus.FAILED}
            if current_status not in allowed:
                if target == JobStatus.RUNNING:
                    raise ValueError(
                        "only paused presentation jobs can resume",
                    )
                raise ValueError(
                    "presentation control transition is not allowed",
                )
            record.control_epoch += 1
            record.last_sequence += 1
            event = PresentationJobEvent(
                job_id=job_id,
                sequence=record.last_sequence,
                stage=JobStage.CANCELLED
                if target == JobStatus.CANCELLED
                else record.stage,
                status=target,
                message={
                    JobStatus.RUNNING: "任务已从最近检查点恢复",
                    JobStatus.PAUSED: "任务已暂停",
                    JobStatus.CANCELLED: "任务已取消",
                }[target],
                checkpoint=record.checkpoints.get(record.stage.value),
                recoverable=target != JobStatus.CANCELLED,
            )
            if editing:
                record.active_operation.status = target
            else:
                record.stage, record.status = event.stage, target
            record.updated_at = datetime.now(timezone.utc)
            record.events.append(event)
            self._write(record)
            return event

    def retire_runtime_owner(self, job_id: str, runtime_run_id: str) -> None:
        """Fence shutdown work while keeping the original recovery budget."""
        with self._lock, self._record_guard(job_id):
            record = self._read_unlocked(job_id)
            if (
                record.runtime_run_id != runtime_run_id
                or record.active_lease_id is None
            ):
                return
            record.control_epoch += 1
            if record.active_operation is not None:
                record.active_operation.status = JobStatus.CANCELLED
            record.updated_at = datetime.now(timezone.utc)
            self._write(record)

    def settle_failed_recovery(
        self,
        job_id: str,
        *,
        expected_control_epoch: int,
        message: str,
        failure_routes: Sequence[PresentationFailureRoute],
    ) -> None:
        """Record an admission failure only after the previous writer
        settles."""
        with self._file_lock(
            self.job_dir(job_id) / ".writer.lock",
            blocking=False,
        ):
            with self._lock, self._record_guard(job_id):
                record = self._read_unlocked(job_id)
                if (
                    record.control_epoch != expected_control_epoch
                    or record.resource_settlement_pending
                    or record.status
                    not in {JobStatus.PENDING, JobStatus.RUNNING}
                ):
                    return
                record.active_lease_id = None
                event = PresentationJobEvent(
                    job_id=job_id,
                    sequence=record.last_sequence + 1,
                    stage=JobStage.FAILED,
                    status=JobStatus.FAILED,
                    message=message,
                    recoverable=True,
                    failure_routes=list(failure_routes),
                )
                record.last_sequence = event.sequence
                record.stage, record.status = event.stage, event.status
                record.events.append(event)
                record.updated_at = datetime.now(timezone.utc)
                self._write(record)
                (self.job_dir(job_id) / "lease.json").unlink(missing_ok=True)

    def pause(self, job_id: str, conversation_id: str) -> PresentationJobEvent:
        return self._change_control(job_id, conversation_id, JobStatus.PAUSED)

    def resume(
        self,
        job_id: str,
        conversation_id: str,
    ) -> PresentationJobEvent:
        return self._change_control(job_id, conversation_id, JobStatus.RUNNING)

    def _archive_checkpoints_invalidated_by_retry(
        self,
        record: PresentationJobRecord,
        *,
        retry_stage: JobStage,
        recovery_attempt: int,
        recovery_kind: str = "recovery",
    ) -> list[Path]:
        """Retire only authoritative aliases made stale by an upstream retry.

        Events and immutable recovery history remain append-only.  Moving the
        small authoritative JSON aliases forces the runtime to recompute the
        affected stages without deleting the previous plan or DeckIR.  Page
        scene checkpoints stay in place: their dependency hashes already
        decide whether each page can be reused after the new plan is written.
        """

        invalidated_files = {
            JobStage.BRIEF: (
                "brief.json",
                "presentation-design-context.json",
                "deck-plan.json",
                "deck-ir.json",
            ),
            # Research is folded into brief construction in the current
            # runtime, so a research retry must reread the source material.
            JobStage.RESEARCH: (
                "brief.json",
                "presentation-design-context.json",
                "deck-plan.json",
                "deck-ir.json",
            ),
            JobStage.PLAN: ("deck-plan.json", "deck-ir.json"),
            JobStage.DECK_IR: ("deck-ir.json",),
            # COMPOSE is the authoritative merge of page-scene checkpoints.
            # Keep per-page checkpoints so their dependency hashes can reuse
            # clean pages, but retire the merged DeckIR after an author or
            # composition contract fix.
            JobStage.COMPOSE: ("deck-ir.json",),
        }.get(retry_stage, ())
        history_dir = (
            self.job_dir(record.job_id)
            / "checkpoints"
            / "history"
            / (
                f"{recovery_kind}-{recovery_attempt:03d}-"
                f"from-{retry_stage.value}"
            )
        )
        archived: list[Path] = []
        for name in invalidated_files:
            source = self.checkpoint_path(record.job_id, name)
            destination = history_dir / name
            if not source.is_file():
                continue
            history_dir.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise RuntimeError(
                    "presentation retry history already contains this "
                    f"checkpoint: {destination}",
                )
            # Same-filesystem replace is atomic and preserves the exact bytes.
            # A retry interrupted between aliases remains resumable because
            # the attempt number is not committed until the manifest write.
            os.replace(source, destination)
            archived.append(destination)

        stage_order = {stage: index for index, stage in enumerate(JobStage)}
        retry_index = stage_order[retry_stage]
        for stage_name in list(record.checkpoints):
            try:
                checkpoint_stage = JobStage(stage_name)
            except ValueError:
                continue
            if stage_order[checkpoint_stage] >= retry_index:
                record.checkpoints.pop(stage_name, None)
        return archived

    def retry_failed(
        self,
        job_id: str,
        conversation_id: str,
        *,
        max_attempts: int = MAX_PRESENTATION_RECOVERY_ATTEMPTS,
    ) -> PresentationJobEvent:
        """Retry one classified transient failure on the original job.

        This is deliberately separate from pause/resume.  A failed job may
        run again only when its latest failure event contains complete,
        machine-readable routes and every route permits an automatic retry.
        The original budget, checkpoints, owner, and job identity remain
        authoritative.
        """

        if max_attempts < 1:
            raise ValueError(
                "presentation recovery attempt limit must be positive",
            )
        executable_stages = {
            JobStage.BRIEF,
            JobStage.RESEARCH,
            JobStage.PLAN,
            JobStage.DECK_IR,
            JobStage.COMPOSE,
            JobStage.RENDER,
            JobStage.QUALITY,
            JobStage.REPAIR,
        }
        stage_order = {stage: index for index, stage in enumerate(JobStage)}
        with self._lock, self._record_guard(job_id):
            record = self._read_unlocked(job_id)
            if record.conversation_id != conversation_id:
                raise PermissionError(
                    "presentation job belongs to a different conversation",
                )
            if record.status != JobStatus.FAILED:
                raise ValueError("only failed presentation jobs can retry")
            if (
                record.deadline_at is not None
                and datetime.now(timezone.utc) >= record.deadline_at
            ):
                raise TimeoutError(
                    (
                        "presentation recovery cannot extend its "
                        "original deadline"
                    ),
                )
            if record.recovery_attempt_count >= max_attempts:
                raise RuntimeError(
                    "presentation recovery attempt limit was exhausted",
                )
            if record.cost_used_units >= record.cost_budget_units:
                raise RuntimeError(
                    (
                        "presentation recovery cannot exceed the "
                        "original cost budget"
                    ),
                )
            failed_event = next(
                (
                    event
                    for event in reversed(record.events)
                    if event.status == JobStatus.FAILED
                ),
                None,
            )
            persisted_routes = (
                list(failed_event.failure_routes) if failed_event else []
            )
            routes = normalize_persisted_failure_routes(persisted_routes)
            normalized_legacy_route = routes != persisted_routes
            if not routes:
                raise ValueError(
                    "failed presentation job has no classified recovery route",
                )
            if any(
                not route.automatic_retry_allowed
                or route.requires_human
                or route.retry_stage not in executable_stages
                for route in routes
            ):
                raise ValueError(
                    (
                        "failed presentation job requires human review "
                        "or authored repair"
                    ),
                )
            retry_stages = [
                route.retry_stage
                for route in routes
                if route.retry_stage is not None
            ]
            retry_stage = min(
                retry_stages,
                key=lambda stage: stage_order[stage],
            )
            next_recovery_attempt = record.recovery_attempt_count + 1
            archived = self._archive_checkpoints_invalidated_by_retry(
                record,
                retry_stage=retry_stage,
                recovery_attempt=next_recovery_attempt,
            )
            event = PresentationJobEvent(
                job_id=job_id,
                sequence=record.last_sequence + 1,
                stage=retry_stage,
                status=JobStatus.RUNNING,
                message=(
                    "已按持久化故障证据从原作业检查点重试"
                    f"（{next_recovery_attempt}/{max_attempts}）"
                    + ("；已按稳定异常类型修正历史故障分类" if normalized_legacy_route else "")
                    + (f"；已归档 {len(archived)} 个过期下游检查点" if archived else "")
                ),
                recoverable=True,
            )
            record.last_sequence = event.sequence
            record.stage = retry_stage
            record.control_epoch += 1
            record.status = JobStatus.RUNNING
            record.recovery_attempt_count += 1
            record.updated_at = datetime.now(timezone.utc)
            record.events.append(event)
            self._write(record)
            return event

    def retry_failed_after_external_recovery(
        self,
        job_id: str,
        conversation_id: str,
        *,
        reviewed_routes: Sequence[PresentationFailureRoute],
        review_basis: str,
        max_attempts: int = MAX_PRESENTATION_EXTERNAL_RECOVERY_ATTEMPTS,
    ) -> PresentationJobEvent:
        """Resume after verified recovery of an external model service.

        This is a separate bounded lane, not an extension or reset of the
        general repair counter.  It exists for quota windows and equivalent
        provider-state outages that recover only after all ordinary repair
        attempts have already been consumed.  The caller must provide a
        non-sensitive evidence identifier and a complete model-service route;
        persisted authorization failures remain ineligible.
        """

        if max_attempts < 1:
            raise ValueError(
                (
                    "presentation external recovery attempt limit must "
                    "be positive"
                ),
            )
        normalized_basis = review_basis.strip()
        if not re.fullmatch(r"[a-z0-9_.:-]{3,120}", normalized_basis):
            raise ValueError(
                (
                    "review basis must be a short non-sensitive "
                    "evidence identifier"
                ),
            )
        routes = list(reviewed_routes)
        if not routes:
            raise ValueError(
                "external presentation recovery requires a reviewed route",
            )
        executable_stages = {
            JobStage.BRIEF,
            JobStage.RESEARCH,
            JobStage.PLAN,
            JobStage.DECK_IR,
            JobStage.COMPOSE,
            JobStage.RENDER,
            JobStage.QUALITY,
            JobStage.REPAIR,
        }
        if any(
            route.category != PresentationFailureCategory.MODEL_SERVICE
            or route.recovery_action
            != PresentationRecoveryAction.RETRY_MODEL_SERVICE
            or not route.automatic_retry_allowed
            or route.requires_human
            or route.retry_stage not in executable_stages
            or not route.issue_codes
            for route in routes
        ):
            raise ValueError(
                "external presentation recovery is limited to verified "
                "model-service failures",
            )
        stage_order = {stage: index for index, stage in enumerate(JobStage)}
        with self._lock, self._record_guard(job_id):
            record = self._read_unlocked(job_id)
            if record.conversation_id != conversation_id:
                raise PermissionError(
                    "presentation job belongs to a different conversation",
                )
            if record.status != JobStatus.FAILED:
                raise ValueError("only failed presentation jobs can retry")
            if (
                record.deadline_at is not None
                and datetime.now(timezone.utc) >= record.deadline_at
            ):
                raise TimeoutError(
                    (
                        "presentation recovery cannot extend its "
                        "original deadline"
                    ),
                )
            if record.external_recovery_attempt_count >= max_attempts:
                raise RuntimeError(
                    (
                        "presentation external recovery attempt limit "
                        "was exhausted"
                    ),
                )
            if record.cost_used_units >= record.cost_budget_units:
                raise RuntimeError(
                    (
                        "presentation recovery cannot exceed the "
                        "original cost budget"
                    ),
                )
            failed_event = next(
                (
                    event
                    for event in reversed(record.events)
                    if event.status == JobStatus.FAILED
                ),
                None,
            )
            if failed_event is None or not failed_event.failure_routes:
                raise ValueError(
                    "failed presentation job has no evidence to review",
                )
            if any(
                route.category == PresentationFailureCategory.AUTHORIZATION
                or route.recovery_action
                == PresentationRecoveryAction.REQUEST_AUTHORIZATION
                for route in failed_event.failure_routes
            ):
                raise ValueError(
                    "authorization failures cannot use external recovery",
                )
            retry_stages = [
                route.retry_stage
                for route in routes
                if route.retry_stage is not None
            ]
            retry_stage = min(
                retry_stages,
                key=lambda stage: stage_order[stage],
            )
            next_external_attempt = record.external_recovery_attempt_count + 1
            archived = self._archive_checkpoints_invalidated_by_retry(
                record,
                retry_stage=retry_stage,
                recovery_attempt=next_external_attempt,
                recovery_kind="external-recovery",
            )
            event = PresentationJobEvent(
                job_id=job_id,
                sequence=record.last_sequence + 1,
                stage=retry_stage,
                status=JobStatus.RUNNING,
                message=(
                    "已核验外部模型服务恢复，继续原作业"
                    f"（{next_external_attempt}/{max_attempts}）；"
                    f"复核依据={normalized_basis}"
                    + (f"；已归档 {len(archived)} 个过期下游检查点" if archived else "")
                ),
                recoverable=True,
                failure_routes=routes,
            )
            record.last_sequence = event.sequence
            record.stage = retry_stage
            record.control_epoch += 1
            record.status = JobStatus.RUNNING
            record.external_recovery_attempt_count += 1
            record.updated_at = datetime.now(timezone.utc)
            record.events.append(event)
            self._write(record)
            return event

    def retry_failed_after_review(
        self,
        job_id: str,
        conversation_id: str,
        *,
        reviewed_routes: Sequence[PresentationFailureRoute],
        review_basis: str,
        max_attempts: int = MAX_PRESENTATION_RECOVERY_ATTEMPTS,
    ) -> PresentationJobEvent:
        """Resume one ambiguous failure after explicit structured review.

        Historical failure evidence remains append-only.  The caller must
        supply the reviewed route rather than asking this layer to infer a
        cause from exception prose.  Authorization failures cannot use this
        and bounded recovery count remain authoritative.
        """

        if max_attempts < 1:
            raise ValueError(
                "presentation recovery attempt limit must be positive",
            )
        normalized_basis = review_basis.strip()
        if not re.fullmatch(r"[a-z0-9_.:-]{3,120}", normalized_basis):
            raise ValueError(
                (
                    "review basis must be a short non-sensitive "
                    "evidence identifier"
                ),
            )
        routes = list(reviewed_routes)
        if not routes:
            raise ValueError("reviewed presentation recovery requires a route")
        if any(
            route.category == PresentationFailureCategory.AUTHORIZATION
            or route.recovery_action
            == PresentationRecoveryAction.REQUEST_AUTHORIZATION
            for route in routes
        ):
            raise ValueError(
                "authorization failures cannot use reviewed recovery",
            )
        executable_stages = {
            JobStage.BRIEF,
            JobStage.RESEARCH,
            JobStage.PLAN,
            JobStage.DECK_IR,
            JobStage.COMPOSE,
            JobStage.RENDER,
            JobStage.QUALITY,
            JobStage.REPAIR,
        }
        if any(
            route.category
            in {
                PresentationFailureCategory.UNKNOWN,
            }
            or route.recovery_action
            in {
                PresentationRecoveryAction.INSPECT_FAILURE,
            }
            or route.requires_human
            or route.retry_stage not in executable_stages
            or not route.issue_codes
            for route in routes
        ):
            raise ValueError(
                "reviewed presentation recovery route is incomplete or unsafe",
            )
        stage_order = {stage: index for index, stage in enumerate(JobStage)}
        with self._lock, self._record_guard(job_id):
            record = self._read_unlocked(job_id)
            if record.conversation_id != conversation_id:
                raise PermissionError(
                    "presentation job belongs to a different conversation",
                )
            if record.status != JobStatus.FAILED:
                raise ValueError("only failed presentation jobs can retry")
            if (
                record.deadline_at is not None
                and datetime.now(timezone.utc) >= record.deadline_at
            ):
                raise TimeoutError(
                    (
                        "presentation recovery cannot extend its "
                        "original deadline"
                    ),
                )
            if record.recovery_attempt_count >= max_attempts:
                raise RuntimeError(
                    "presentation recovery attempt limit was exhausted",
                )
            if record.cost_used_units >= record.cost_budget_units:
                raise RuntimeError(
                    (
                        "presentation recovery cannot exceed the "
                        "original cost budget"
                    ),
                )
            failed_event = next(
                (
                    event
                    for event in reversed(record.events)
                    if event.status == JobStatus.FAILED
                ),
                None,
            )
            if failed_event is None or not failed_event.failure_routes:
                raise ValueError(
                    "failed presentation job has no evidence to review",
                )
            if any(
                route.category == PresentationFailureCategory.AUTHORIZATION
                or route.recovery_action
                == PresentationRecoveryAction.REQUEST_AUTHORIZATION
                for route in failed_event.failure_routes
            ):
                raise ValueError(
                    "authorization failures cannot use reviewed recovery",
                )
            retry_stages = [
                route.retry_stage
                for route in routes
                if route.retry_stage is not None
            ]
            retry_stage = min(
                retry_stages,
                key=lambda stage: stage_order[stage],
            )
            next_recovery_attempt = record.recovery_attempt_count + 1
            archived = self._archive_checkpoints_invalidated_by_retry(
                record,
                retry_stage=retry_stage,
                recovery_attempt=next_recovery_attempt,
            )
            event = PresentationJobEvent(
                job_id=job_id,
                sequence=record.last_sequence + 1,
                stage=retry_stage,
                status=JobStatus.RUNNING,
                message=(
                    "已按人工复核的结构化故障证据从原作业检查点恢复"
                    f"（{next_recovery_attempt}/{max_attempts}）；"
                    f"复核依据={normalized_basis}"
                    + (f"；已归档 {len(archived)} 个过期下游检查点" if archived else "")
                ),
                recoverable=True,
                failure_routes=routes,
            )
            record.last_sequence = event.sequence
            record.stage = retry_stage
            record.control_epoch += 1
            record.status = JobStatus.RUNNING
            record.recovery_attempt_count += 1
            record.updated_at = datetime.now(timezone.utc)
            record.events.append(event)
            self._write(record)
            return event

    def retry_failed_after_implementation_fix(
        self,
        job_id: str,
        conversation_id: str,
        *,
        reviewed_routes: Sequence[PresentationFailureRoute],
        review_basis: str,
        fix_identity: str,
        max_attempts: int = MAX_PRESENTATION_IMPLEMENTATION_RECOVERY_ATTEMPTS,
    ) -> PresentationJobEvent:
        """Resume the same work after a verified deterministic code fix.

        This bounded lane does not reset or extend ordinary model retries.  It
        records the exact non-sensitive fix identity, rejects replay of that
        fix, preserves page checkpoints for dependency-hash reuse, and keeps
        the original owner, history, authorization, and budget authoritative.
        """

        if max_attempts < 1:
            raise ValueError(
                (
                    "presentation implementation recovery attempt "
                    "limit must be positive"
                ),
            )
        normalized_basis = review_basis.strip()
        normalized_fix = fix_identity.strip()
        safe_identity = r"[a-z0-9_.:-]{3,120}"
        if not re.fullmatch(safe_identity, normalized_basis):
            raise ValueError(
                (
                    "review basis must be a short non-sensitive "
                    "evidence identifier"
                ),
            )
        if not re.fullmatch(safe_identity, normalized_fix):
            raise ValueError(
                "fix identity must be a short non-sensitive identifier",
            )
        routes = list(reviewed_routes)
        if not routes:
            raise ValueError(
                "implementation recovery requires a reviewed route",
            )
        executable_stages = {
            JobStage.BRIEF,
            JobStage.RESEARCH,
            JobStage.PLAN,
            JobStage.DECK_IR,
            JobStage.COMPOSE,
            JobStage.RENDER,
            JobStage.QUALITY,
            JobStage.REPAIR,
        }
        allowed_pairs = {
            (
                PresentationFailureCategory.CONTENT,
                PresentationRecoveryAction.REVISE_CONTENT,
            ),
            (
                PresentationFailureCategory.GEOMETRY,
                PresentationRecoveryAction.PATCH_GEOMETRY,
            ),
            (
                PresentationFailureCategory.AESTHETIC,
                PresentationRecoveryAction.REDESIGN_SLIDE,
            ),
            (
                PresentationFailureCategory.FILE,
                PresentationRecoveryAction.RETRY_FILE_STAGE,
            ),
        }
        if any(
            (route.category, route.recovery_action) not in allowed_pairs
            or route.requires_human
            or route.retry_stage not in executable_stages
            or not route.issue_codes
            for route in routes
        ):
            raise ValueError(
                "implementation recovery route is incomplete or unsafe",
            )
        stage_order = {stage: index for index, stage in enumerate(JobStage)}
        with self._lock, self._record_guard(job_id):
            record = self._read_unlocked(job_id)
            self._assert_implementation_recovery_eligible(
                record,
                conversation_id,
                normalized_fix,
                max_attempts,
            )
            retry_stages = [
                route.retry_stage
                for route in routes
                if route.retry_stage is not None
            ]
            retry_stage = min(
                retry_stages,
                key=lambda stage: stage_order[stage],
            )
            next_attempt = record.implementation_recovery_attempt_count + 1
            archived = self._archive_checkpoints_invalidated_by_retry(
                record,
                retry_stage=retry_stage,
                recovery_attempt=next_attempt,
                recovery_kind="implementation-recovery",
            )
            event = PresentationJobEvent(
                job_id=job_id,
                sequence=record.last_sequence + 1,
                stage=retry_stage,
                status=JobStatus.RUNNING,
                message=(
                    "已核验确定性实现修复，继续原作业"
                    f"（{next_attempt}/{max_attempts}）；"
                    f"修复标识={normalized_fix}；复核依据={normalized_basis}"
                    + (f"；已归档 {len(archived)} 个过期下游检查点" if archived else "")
                ),
                recoverable=True,
                failure_routes=routes,
            )
            record.last_sequence = event.sequence
            record.stage = retry_stage
            record.control_epoch += 1
            record.status = JobStatus.RUNNING
            record.implementation_recovery_attempt_count += 1
            record.implementation_recovery_fixes.append(normalized_fix)
            record.updated_at = datetime.now(timezone.utc)
            record.events.append(event)
            self._write(record)
            return event

    @staticmethod
    def _assert_implementation_recovery_eligible(
        record: PresentationJobRecord,
        conversation_id: str,
        normalized_fix: str,
        max_attempts: int,
    ) -> None:
        if record.conversation_id != conversation_id:
            raise PermissionError(
                "presentation job belongs to a different conversation",
            )
        if record.status != JobStatus.FAILED:
            raise ValueError("only failed presentation jobs can retry")
        if (
            record.deadline_at is not None
            and datetime.now(timezone.utc) >= record.deadline_at
        ):
            raise TimeoutError(
                (
                    "presentation recovery cannot extend its original "
                    "deadline"
                ),
            )
        if record.implementation_recovery_attempt_count >= max_attempts:
            raise RuntimeError(
                (
                    "presentation implementation recovery attempt "
                    "limit was exhausted"
                ),
            )
        if normalized_fix in record.implementation_recovery_fixes:
            raise ValueError(
                "the same presentation implementation fix was already used",
            )
        if record.cost_used_units >= record.cost_budget_units:
            raise RuntimeError(
                "presentation recovery cannot exceed the original cost budget",
            )
        failed_event = next(
            (
                event
                for event in reversed(record.events)
                if event.status == JobStatus.FAILED
            ),
            None,
        )
        if failed_event is None or not failed_event.failure_routes:
            raise ValueError(
                "failed presentation job has no evidence to review",
            )
        if any(
            route.category == PresentationFailureCategory.AUTHORIZATION
            or route.recovery_action
            == PresentationRecoveryAction.REQUEST_AUTHORIZATION
            for route in failed_event.failure_routes
        ):
            raise ValueError(
                "authorization failures cannot use implementation recovery",
            )

    def cancel(
        self,
        job_id: str,
        conversation_id: str,
    ) -> PresentationJobEvent:
        return self._change_control(
            job_id,
            conversation_id,
            JobStatus.CANCELLED,
        )

    def write_checkpoint(self, job_id: str, name: str, content: str) -> Path:
        self._assert_writable()
        path = self.checkpoint_path(job_id, name)
        with self._record_guard(job_id):
            self._assert_producer(self._read_unlocked(job_id))
            self._atomic_text(path, content)
        return path

    def checkpoint_path(self, job_id: str, name: str) -> Path:
        safe_name = Path(name).name
        return self.job_dir(job_id) / "checkpoints" / safe_name

    def write_quality_checkpoint(
        self,
        job_id: str,
        name: str,
        content: str,
    ) -> Path:
        self._assert_writable()
        path = self.quality_checkpoint_path(job_id, name)
        with self._record_guard(job_id):
            self._assert_producer(self._read_unlocked(job_id))
            self._atomic_text(path, content)
        return path

    def quality_checkpoint_path(self, job_id: str, name: str) -> Path:
        safe_name = Path(name).name
        return (
            self.job_dir(job_id) / "quality" / "visual-checkpoints" / safe_name
        )

    def job_dir(self, job_id: str) -> Path:
        if (
            not job_id.startswith("presentation-")
            or Path(job_id).name != job_id
        ):
            raise ValueError("invalid presentation job id")
        return self.root / job_id

    def _record_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "manifest.json"

    @staticmethod
    def _atomic_text(target: Path, content: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(
            f".{target.name}.{os.getpid()}.{uuid4().hex}.tmp",
        )
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, target)

    def _write(self, record: PresentationJobRecord) -> None:
        self._assert_writable()
        job_dir = self.job_dir(record.job_id)
        for subdir in (
            "assets",
            "renders",
            "quality",
            "checkpoints",
            "output",
            "candidates",
            "versions",
        ):
            (job_dir / subdir).mkdir(parents=True, exist_ok=True)
        payload = record.model_dump(mode="json")
        # Dual-read, single-write: once an old job changes, the same job and
        # owner are persisted using the current schema.  Read-only status
        # probes never call this method and therefore never mutate history.
        payload["schema_version"] = CURRENT_PRESENTATION_JOB_SCHEMA_VERSION
        self._atomic_text(
            self._record_path(record.job_id),
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
            ),
        )


def cancel_active_job_for_runtime_run(
    state_workspace_dir: str | Path,
    runtime_run_id: str,
    job_id: str,
    *,
    conversation_id: str | None = None,
) -> bool:
    """Cancel one exact active presentation stage owned by a Native Run.

    Pending-choice replacement carries a trusted job identifier. Resolve it
    against the recorded owner Run and, when available, the originating
    conversation before mutating anything so an unrelated deck can never be
    cancelled merely because it shares a workspace.
    """

    run_id = runtime_run_id.strip()
    normalized_job_id = job_id.strip()
    normalized_conversation_id = str(conversation_id or "").strip() or None
    root = Path(state_workspace_dir) / "presentation-jobs"
    if not run_id or not normalized_job_id or not root.is_dir():
        return False
    store = PresentationJobStore(root)
    try:
        record = store.get(normalized_job_id)
    except KeyError:
        return False
    if record.runtime_run_id != run_id:
        raise PermissionError(
            "presentation job does not belong to the pending Runtime Run",
        )
    if (
        normalized_conversation_id is not None
        and record.conversation_id != normalized_conversation_id
    ):
        raise PermissionError(
            "presentation job belongs to a different conversation",
        )
    if record.status not in {JobStatus.PENDING, JobStatus.RUNNING} and not (
        record.active_lease_id is not None
        and record.active_operation is not None
    ):
        return False
    try:
        store.cancel(record.job_id, record.conversation_id)
    except ValueError:
        # The worker may have completed before cancellation acquired the
        # manifest lock; preserve the completed historical result.
        return False
    return True


def cancel_active_jobs_for_runtime_run(
    state_workspace_dir: str | Path,
    runtime_run_id: str,
) -> list[str]:
    """Propagate an explicit Native Stop to its presentation stage records.

    A disconnected stream or expired foreground wait never calls this helper.
    Match the exact Run owner rather than cancelling every job in a chat.
    """

    run_id = runtime_run_id.strip()
    root = Path(state_workspace_dir) / "presentation-jobs"
    if not run_id or not root.is_dir():
        return []
    store = PresentationJobStore(root)
    cancelled: list[str] = []
    for record in store.list_records():
        if record.runtime_run_id != run_id or (
            record.status not in {JobStatus.PENDING, JobStatus.RUNNING}
            and not (
                record.active_lease_id is not None
                and record.active_operation is not None
            )
        ):
            continue
        if cancel_active_job_for_runtime_run(
            state_workspace_dir,
            run_id,
            record.job_id,
        ):
            cancelled.append(record.job_id)
    return cancelled


def job_operation(method):
    """Capture stage ownership once, including explicit cross-thread calls."""
    parameters = signature(method)

    @wraps(method)
    def bound(self, *args, **kwargs):
        arguments = parameters.bind(self, *args, **kwargs).arguments
        job_id = arguments.get("job_id")
        if job_id is None or (
            method.__name__ == "compose"
            and arguments.get("writer_lease") is None
        ):
            return method(self, *args, **kwargs)
        if (
            arguments.get("writer_lease") is None
            and self.store.get(job_id).active_lease_id is None
        ):
            # Direct verification has always been a supported service entry.
            # It receives the same exclusive owner and original job budget.
            with self.store.writer_lease(job_id):
                return method(self, *args, **kwargs)
        with self.store.operation_scope(job_id, arguments.get("writer_lease")):
            return method(self, *args, **kwargs)

    return bound
