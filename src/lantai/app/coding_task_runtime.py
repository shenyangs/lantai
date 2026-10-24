# -*- coding: utf-8 -*-
"""Canonical RuntimeRun adapter for guarded terminal commands.

``RuntimeRunContract`` owns command control state, ``RuntimeEffectRecord``
owns the shell side effect, and ``CodeTaskRun`` is a bounded UI projection.
The projection lives beside the canonical run so backend restarts do not make
previous commands disappear.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..runtime.effects import (
    RuntimeEffectJournal,
    RuntimeEffectReducerAction,
    reduce_runtime_effect,
)
from ..runtime.runs import (
    ACTIVE_RUNTIME_RUN_STATUSES,
    RuntimeBudget,
    RuntimeEffectRecord,
    RuntimeRunContract,
    RuntimeRunOwner,
    RuntimeRunReason,
    RuntimeRunReducerAction,
    RuntimeStepRef,
    reduce_runtime_run,
)
from ..runtime.store import RuntimeRunStore

CODING_TASK_PROJECTION_SCHEMA_VERSION = 1
CODING_TASK_PROJECTION_WRITER_VERSION = "lantai-coding-task-projection/1"


class CodeTaskRun(BaseModel):
    """Bounded, non-authoritative UI projection for one terminal command."""

    task_id: str
    workspace_dir: str
    command: str
    status: str
    started_at: str
    ended_at: str | None = None
    returncode: int | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    output_seq: int = 0
    updated_at: str | None = None
    error: str | None = None
    timeout_seconds: int = Field(default=300, ge=1)
    runtime_attempt_id: str | None = None
    runtime_revision: int = Field(default=0, ge=0)


class _CodeTaskProjectionEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = CODING_TASK_PROJECTION_SCHEMA_VERSION
    writer_version: str = CODING_TASK_PROJECTION_WRITER_VERSION
    checksum: str
    payload: dict[str, object]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _utc_now()).isoformat()


def _stable_hash(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _projection_checksum(payload: dict[str, object]) -> str:
    return _stable_hash(payload)


def _projection_status(contract: RuntimeRunContract) -> str:
    if contract.status == "succeeded":
        return "completed"
    if contract.status == "stop_requested":
        return "stopping"
    if contract.status in {"claimed", "running"}:
        return "running"
    return contract.status


class CodingTaskRuntime:
    """Deep adapter joining terminal projections to run/effect fact sources."""

    def __init__(self, workspace_dir: Path | str, *, agent_id: str) -> None:
        self.workspace_dir = Path(workspace_dir).expanduser().resolve(strict=False)
        self.agent_id = agent_id or "default"
        self.store = RuntimeRunStore(self.workspace_dir)
        self.effects = RuntimeEffectJournal(self.workspace_dir)

    def projection_path(self, task_id: str) -> Path:
        return self.store.contract_path(task_id).with_name("coding-task.json")

    def _owner(self, task: CodeTaskRun) -> RuntimeRunOwner:
        return RuntimeRunOwner(
            user_id="default",
            agent_id=self.agent_id,
            session_id=task.task_id,
            workspace_id=str(self.workspace_dir),
            channel="terminal",
        )

    def _base_contract(self, task: CodeTaskRun) -> RuntimeRunContract:
        attempt_id = f"{task.task_id}:terminal-attempt-1"
        identity = {
            "source": "capability",
            "kind": "terminal_command",
            "run_id": task.task_id,
            "attempt_id": attempt_id,
            "workspace_dir": str(self.workspace_dir),
            "command_hash": _stable_hash(task.command),
        }
        return RuntimeRunContract(
            run_id=task.task_id,
            attempt_id=attempt_id,
            source="capability",
            owner=self._owner(task),
            status="queued",
            revision=0,
            created_at=datetime.fromisoformat(task.started_at.replace("Z", "+00:00")),
            budget=RuntimeBudget(max_seconds=float(task.timeout_seconds)),
            current_step=RuntimeStepRef(
                step_id=f"terminal:{task.task_id}:shell-step",
                index=0,
                kind="tool",
                title="Guarded terminal command",
            ),
            contract_hash=_stable_hash(identity),
            policy_snapshot_hash=_stable_hash(
                {
                    "guard": "coding_tool_guard",
                    "workspace_dir": str(self.workspace_dir),
                    "release_edition": "official",
                },
            ),
            release_edition="official",
        )

    def _base_effect(
        self,
        task: CodeTaskRun,
        contract: RuntimeRunContract,
    ) -> RuntimeEffectRecord:
        return RuntimeEffectRecord(
            run_id=contract.run_id,
            attempt_id=contract.attempt_id,
            step_id=str(contract.current_step.step_id),
            effect_id=f"terminal:{contract.run_id}:shell",
            source=contract.source,
            owner=contract.owner,
            status="intent",
            tool_name="terminal.shell",
            args_hash=_stable_hash(
                {"command": task.command, "cwd": task.workspace_dir},
            ),
            policy_snapshot_hash=contract.policy_snapshot_hash,
            idempotency_key=f"{contract.run_id}:{contract.attempt_id}:shell",
            release_edition=contract.release_edition,
        )

    def _apply_contract(
        self,
        task: CodeTaskRun,
        contract: RuntimeRunContract,
        **updates: object,
    ) -> CodeTaskRun:
        values: dict[str, object] = {
            "status": _projection_status(contract),
            "runtime_attempt_id": contract.attempt_id,
            "runtime_revision": contract.revision,
            "updated_at": _iso(contract.heartbeat_at),
            **updates,
        }
        if contract.settled_at is not None:
            values["ended_at"] = _iso(contract.settled_at)
        return task.model_copy(update=values)

    async def create(self, task: CodeTaskRun) -> CodeTaskRun:
        """Create the queued run, effect intent, and initial projection."""

        contract = self._base_contract(task)
        await self.store.save_contract(contract, expected_revision=None)
        self.effects.append_effect(self._base_effect(task, contract))
        projected = self._apply_contract(task, contract)
        self.save(projected)
        return projected

    async def mark_running(self, task: CodeTaskRun) -> CodeTaskRun:
        """Fence and publish the run/effect immediately before process spawn."""

        contract = await self._required_contract(task.task_id)
        effect = self._required_effect(contract)
        if effect.status == "intent":
            effect = reduce_runtime_effect(
                effect,
                RuntimeEffectReducerAction(type="start"),
            ).effect
            self.effects.append_effect(effect)

        lease_owner = f"terminal:{task.task_id}"
        current = contract
        if current.status == "queued":
            current = reduce_runtime_run(
                current,
                RuntimeRunReducerAction(
                    type="claim",
                    expected_revision=current.revision,
                    lease_owner=lease_owner,
                ),
            ).run
        if current.status == "claimed":
            current = reduce_runtime_run(
                current,
                RuntimeRunReducerAction(
                    type="start",
                    expected_attempt_id=current.attempt_id,
                    expected_revision=current.revision,
                    lease_owner=lease_owner,
                ),
            ).run
        await self.store.save_contract(
            current,
            expected_revision=contract.revision,
            expected_attempt_id=contract.attempt_id,
            lease_owner=contract.lease_owner,
        )
        projected = self._apply_contract(task, current)
        self.save(projected)
        return projected

    async def request_stop(self, task: CodeTaskRun) -> CodeTaskRun:
        contract = await self._required_contract(task.task_id)
        if contract.status not in ACTIVE_RUNTIME_RUN_STATUSES:
            return self._apply_contract(task, contract)
        if contract.status in {"queued", "claimed"}:
            return await self.settle(
                task,
                status="cancelled",
                error="Stop requested by user",
            )
        if contract.status != "stop_requested":
            stopped = reduce_runtime_run(
                contract,
                RuntimeRunReducerAction(
                    type="request_stop",
                    expected_attempt_id=contract.attempt_id,
                    expected_revision=contract.revision,
                    lease_owner=contract.lease_owner,
                    reason=RuntimeRunReason(
                        code="terminal_stop_requested",
                        message="Stop requested by user",
                        category="user",
                        retryable=True,
                    ),
                ),
            ).run
            await self.store.save_contract(
                stopped,
                expected_revision=contract.revision,
                expected_attempt_id=contract.attempt_id,
                lease_owner=contract.lease_owner,
            )
            contract = stopped
        projected = self._apply_contract(
            task,
            contract,
            error="Stop requested by user",
        )
        self.save(projected)
        return projected

    async def settle(
        self,
        task: CodeTaskRun,
        *,
        status: Literal["completed", "failed", "cancelled", "stalled"],
        returncode: int | None = None,
        error: str | None = None,
        reason_code: str | None = None,
    ) -> CodeTaskRun:
        contract = await self._required_contract(task.task_id)
        terminal_status = {
            "completed": "succeeded",
            "failed": "failed",
            "cancelled": "cancelled",
            "stalled": "stalled",
        }[status]
        effect = self._required_effect(contract)
        if effect.status in {"intent", "approved", "running", "ambiguous"}:
            effect_action = (
                "commit"
                if status == "completed"
                else "cancel"
                if status == "cancelled"
                else "mark_ambiguous"
                if status == "stalled" and effect.status == "running"
                else "fail"
            )
            effect = reduce_runtime_effect(
                effect,
                RuntimeEffectReducerAction(
                    type=effect_action,
                    result_hash=_stable_hash(
                        {"returncode": returncode, "status": status},
                    ),
                    ambiguity_reason=(
                        error if effect_action == "mark_ambiguous" else None
                    ),
                ),
            ).effect
            self.effects.append_effect(effect)

        if contract.status not in {"succeeded", "failed", "cancelled", "stalled"}:
            reason = None
            if terminal_status != "succeeded":
                reason = RuntimeRunReason(
                    code=reason_code or f"terminal_task_{status}",
                    message=error or f"Terminal task {status}",
                    category="user" if terminal_status == "cancelled" else "runtime",
                    retryable=terminal_status in {"cancelled", "stalled"},
                )
            settled = reduce_runtime_run(
                contract,
                RuntimeRunReducerAction(
                    type="settle",
                    status=terminal_status,
                    expected_attempt_id=contract.attempt_id,
                    expected_revision=contract.revision,
                    lease_owner=contract.lease_owner,
                    reason=reason,
                ),
            ).run
            await self.store.save_contract(
                settled,
                expected_revision=contract.revision,
                expected_attempt_id=contract.attempt_id,
                lease_owner=contract.lease_owner,
            )
            contract = settled
        projected = self._apply_contract(
            task,
            contract,
            returncode=returncode,
            error=error,
        )
        self.save(projected)
        return projected

    async def recover_interrupted(self) -> list[CodeTaskRun]:
        """Settle projections left active by a previous backend process."""

        recovered: list[CodeTaskRun] = []
        for task in self.load_all():
            contract = await self.store.load_contract(task.task_id)
            if contract is None or contract.status not in ACTIVE_RUNTIME_RUN_STATUSES:
                recovered.append(task)
                continue
            effect = self._required_effect(contract)
            if effect.status in {"committed", "probed_committed"}:
                recovered.append(
                    await self.settle(
                        task,
                        status="completed",
                        returncode=task.returncode,
                    ),
                )
                continue
            if effect.status == "failed":
                recovered.append(
                    await self.settle(
                        task,
                        status="failed",
                        returncode=task.returncode,
                        error=task.error or "Command failed before backend restart",
                        reason_code="terminal_task_failed_before_restart",
                    ),
                )
                continue
            if effect.status == "cancelled":
                recovered.append(
                    await self.settle(
                        task,
                        status="cancelled",
                        returncode=task.returncode,
                        error=task.error or "Command was cancelled",
                    ),
                )
                continue
            if contract.status == "queued":
                recovered.append(
                    await self.settle(
                        task,
                        status="cancelled",
                        error="Backend restarted before the command was launched",
                        reason_code="terminal_task_restart_before_launch",
                    ),
                )
            else:
                recovered.append(
                    await self.settle(
                        task,
                        status="stalled",
                        error="Backend restarted while the command was active",
                        reason_code="terminal_task_backend_restarted",
                    ),
                )
        return recovered

    def save(self, task: CodeTaskRun) -> None:
        payload = task.model_dump(mode="json")
        envelope = _CodeTaskProjectionEnvelope(
            checksum=_projection_checksum(payload),
            payload=payload,
        )
        path = self.projection_path(task.task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise ValueError("Coding task projection path is a symlink")
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                json.dump(
                    envelope.model_dump(mode="json"),
                    handle,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            if fd >= 0:
                os.close(fd)
            if temporary.exists():
                temporary.unlink()

    def load_all(self) -> list[CodeTaskRun]:
        runs_root = self.store.root / "runs"
        if not runs_root.exists():
            return []
        tasks: list[CodeTaskRun] = []
        for path in runs_root.glob("*/coding-task.json"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                envelope = _CodeTaskProjectionEnvelope.model_validate_json(
                    path.read_text(encoding="utf-8"),
                )
                if envelope.checksum != _projection_checksum(envelope.payload):
                    continue
                task = CodeTaskRun.model_validate(envelope.payload)
                task_workspace = Path(task.workspace_dir).expanduser().resolve(
                    strict=False,
                )
                if task_workspace != self.workspace_dir:
                    continue
                tasks.append(task)
            except (OSError, ValueError):
                continue
        return sorted(tasks, key=lambda task: task.started_at, reverse=True)

    async def _required_contract(self, task_id: str) -> RuntimeRunContract:
        contract = await self.store.load_contract(task_id)
        if contract is None:
            raise ValueError(f"RuntimeRun contract not found: {task_id}")
        return contract

    def _required_effect(
        self,
        contract: RuntimeRunContract,
    ) -> RuntimeEffectRecord:
        effect = self.effects.latest_effect(
            contract.run_id,
            contract.attempt_id,
            f"terminal:{contract.run_id}:shell",
        )
        if effect is None:
            raise ValueError(f"Terminal effect not found: {contract.run_id}")
        return effect
