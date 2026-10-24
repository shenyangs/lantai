# -*- coding: utf-8 -*-
"""Durably append user steering commands to the canonical Runtime Run."""

from __future__ import annotations

from .runs import (
    RuntimeRunContract,
    RuntimeRunReducerAction,
    RuntimeRunTransitionError,
    reduce_runtime_run,
    revise_chat_task_contract,
)
from .store import (
    RuntimeRunStore,
    RuntimeRunStoreConflictError,
    RuntimeRunStoreError,
)


async def persist_runtime_task_command(
    *,
    workspace_dir: str,
    run_id: str,
    attempt_id: str,
    client_turn_id: str,
    command: str,
    max_retries: int = 4,
) -> RuntimeRunContract:
    """Persist one append-only steer before the Runtime acts on it.

    Concurrent heartbeat writes are retried against the newest Run revision.
    Replaying the same client turn is idempotent; reusing its identity for
    different text, crossing an attempt fence, or revising a terminal Run
    remains a hard failure.
    """

    store = RuntimeRunStore(workspace_dir)
    attempts = max(1, int(max_retries))
    last_conflict: RuntimeRunStoreConflictError | None = None
    for _ in range(attempts):
        current = await store.load_contract(run_id)
        if current is None:
            raise RuntimeRunStoreError(f"RuntimeRun contract not found: {run_id}")
        if current.attempt_id != attempt_id:
            raise RuntimeRunStoreConflictError(
                "RuntimeRun attempt conflict while revising task: "
                f"{attempt_id} != {current.attempt_id}"
            )
        if current.task_contract is None:
            raise RuntimeRunTransitionError(
                "RuntimeRun task revision requires an existing task contract."
            )
        revised_task = revise_chat_task_contract(
            current.task_contract,
            client_turn_id=client_turn_id,
            command=command,
        )
        if revised_task == current.task_contract:
            return current
        revised_run = reduce_runtime_run(
            current,
            RuntimeRunReducerAction(
                type="revise_task",
                task_contract=revised_task,
                expected_attempt_id=current.attempt_id,
                expected_revision=current.revision,
                lease_owner=current.lease_owner,
            ),
        ).run
        try:
            return await store.save_contract(
                revised_run,
                expected_revision=current.revision,
                expected_attempt_id=current.attempt_id,
                lease_owner=current.lease_owner,
            )
        except RuntimeRunStoreConflictError as exc:
            last_conflict = exc
    raise RuntimeRunStoreConflictError(
        "RuntimeRun task revision remained contested after retries."
    ) from last_conflict


__all__ = ["persist_runtime_task_command"]
