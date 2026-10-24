# -*- coding: utf-8 -*-
"""Bind a primary-model deliverable declaration to the durable Run contract."""

from __future__ import annotations

from pathlib import Path

from .runs import (
    RuntimeDeliverableContract,
    RuntimePendingDeliverableResolution,
)
from .store import RuntimeRunStore, RuntimeRunStoreConflictError


class DeliverableContractConflictError(ValueError):
    """Raised when a turn attempts to change an already-bound deliverable."""


class PendingDeliverableResolutionConflictError(ValueError):
    """Raised when one turn tries to reinterpret a resolved pending choice."""


async def bind_deliverable_contract(
    *,
    state_workspace_dir: str | Path,
    run_id: str,
    kind: str,
    accepted_extensions: list[str] | tuple[str, ...],
    visual_verification_required: bool,
) -> RuntimeDeliverableContract:
    """Bind once, return idempotently, and reject output-type drift."""

    desired = RuntimeDeliverableContract(
        kind=kind,
        accepted_extensions=tuple(accepted_extensions),
        visual_verification_required=visual_verification_required,
    )
    store = RuntimeRunStore(state_workspace_dir)
    for _attempt in range(3):
        run = await store.load_contract(run_id)
        if run is None or run.task_contract is None:
            raise ValueError("Runtime task contract is missing.")
        current = run.task_contract.deliverable_contract
        if current is not None:
            if current == desired:
                return current
            raise DeliverableContractConflictError(
                "Deliverable contract is already bound to "
                f"{', '.join(current.accepted_extensions)} and cannot change to "
                f"{', '.join(desired.accepted_extensions)} in the same task."
            )
        updated_task = run.task_contract.model_copy(
            update={"deliverable_contract": desired},
        )
        updated_run = run.model_copy(
            update={
                "revision": run.revision + 1,
                "task_contract": updated_task,
            },
        )
        try:
            await store.save_contract(
                updated_run,
                expected_revision=run.revision,
                expected_attempt_id=run.attempt_id,
                lease_owner=run.lease_owner,
            )
        except RuntimeRunStoreConflictError:
            continue
        return desired
    raise RuntimeRunStoreConflictError(
        "Deliverable contract could not be bound after concurrent updates."
    )


async def resolve_pending_deliverable(
    *,
    state_workspace_dir: str | Path,
    run_id: str,
    source_run_id: str,
    source_contract_revision: int | None = None,
    source_task_revision: int | None = None,
    pending_job_id: str | None = None,
    decision: str,
    inherited_contract: RuntimeDeliverableContract,
) -> RuntimePendingDeliverableResolution:
    """Persist one semantic choice and atomically inherit its contract.

    The caller supplies ``inherited_contract`` from trusted runtime state, not
    from model arguments.  The primary model owns only the semantic decision.
    """

    desired = RuntimePendingDeliverableResolution(
        source_run_id=source_run_id,
        source_contract_revision=source_contract_revision or 0,
        source_task_revision=source_task_revision or 0,
        pending_job_id=pending_job_id,
        decision=decision,
    )
    store = RuntimeRunStore(state_workspace_dir)
    for _attempt in range(3):
        if source_contract_revision is not None:
            source_run = await store.load_contract(source_run_id)
            source_task = (
                source_run.task_contract if source_run is not None else None
            )
            if source_run is None or source_task is None:
                raise PendingDeliverableResolutionConflictError(
                    "Pending deliverable source task no longer exists."
                )
            if source_run.revision != source_contract_revision:
                raise PendingDeliverableResolutionConflictError(
                    "Pending deliverable source contract changed before the "
                    "choice was resolved."
                )
            if (
                source_task_revision is not None
                and source_task.revision != source_task_revision
            ):
                raise PendingDeliverableResolutionConflictError(
                    "Pending deliverable source task revision changed before "
                    "the choice was resolved."
                )
            if source_task.deliverable_contract != inherited_contract:
                raise PendingDeliverableResolutionConflictError(
                    "Pending deliverable contract no longer matches its "
                    "trusted source snapshot."
                )
        run = await store.load_contract(run_id)
        if run is None or run.task_contract is None:
            raise ValueError("Runtime task contract is missing.")
        task = run.task_contract
        current_resolution = task.pending_deliverable_resolution
        if current_resolution is not None:
            if current_resolution == desired:
                return current_resolution
            raise PendingDeliverableResolutionConflictError(
                "Pending deliverable choice is already resolved as "
                f"{current_resolution.decision!r} for "
                f"{current_resolution.source_run_id!r}."
            )
        next_contract = task.deliverable_contract
        if desired.decision == "continue":
            if next_contract is not None and next_contract != inherited_contract:
                raise DeliverableContractConflictError(
                    "Current deliverable contract conflicts with the inherited "
                    "unfinished task."
                )
            next_contract = inherited_contract
        updated_task = task.model_copy(
            update={
                "deliverable_contract": next_contract,
                "pending_deliverable_resolution": desired,
            },
        )
        updated_run = run.model_copy(
            update={
                "revision": run.revision + 1,
                "task_contract": updated_task,
            },
        )
        try:
            await store.save_contract(
                updated_run,
                expected_revision=run.revision,
                expected_attempt_id=run.attempt_id,
                lease_owner=run.lease_owner,
            )
        except RuntimeRunStoreConflictError:
            continue
        return desired
    raise RuntimeRunStoreConflictError(
        "Pending deliverable choice could not be saved after concurrent updates."
    )


__all__ = [
    "DeliverableContractConflictError",
    "PendingDeliverableResolutionConflictError",
    "bind_deliverable_contract",
    "resolve_pending_deliverable",
]
