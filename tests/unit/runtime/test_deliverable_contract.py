# -*- coding: utf-8 -*-
"""Behavior locks for immutable primary-model deliverable declarations."""

from datetime import datetime, timezone

import pytest

from lantai.runtime.deliverable_contract import (
    DeliverableContractConflictError,
    PendingDeliverableResolutionConflictError,
    bind_deliverable_contract,
    resolve_pending_deliverable,
)
from lantai.runtime.runs import (
    RuntimeDeliverableContract,
    RuntimeRunContract,
    RuntimeRunOwner,
    build_chat_task_contract,
)
from lantai.runtime.store import RuntimeRunStore


async def _stored_run(tmp_path) -> RuntimeRunStore:
    store = RuntimeRunStore(tmp_path)
    now = datetime.now(timezone.utc)
    await store.save_contract(
        RuntimeRunContract(
            run_id="run-deck",
            attempt_id="run-deck:attempt-1",
            source="chat",
            owner=RuntimeRunOwner(
                user_id="user",
                agent_id="default",
                session_id="console:chat",
                workspace_id="workspace",
                channel="console",
            ),
            status="running",
            revision=0,
            created_at=now,
            started_at=now,
            heartbeat_at=now,
            task_contract=build_chat_task_contract(
                objective="根据附件做一份咖啡馆 PPT",
                client_turn_id="turn-deck",
                complexity="standard",
                work_method_id="standard",
                run_id="run-deck",
                attempt_id="run-deck:attempt-1",
            ),
            release_edition="official",
        ),
        expected_revision=None,
    )
    return store


@pytest.mark.asyncio
async def test_primary_model_contract_is_persisted_and_idempotent(tmp_path) -> None:
    store = await _stored_run(tmp_path)

    first = await bind_deliverable_contract(
        state_workspace_dir=tmp_path,
        run_id="run-deck",
        kind="presentation",
        accepted_extensions=["pptx"],
        visual_verification_required=True,
    )
    second = await bind_deliverable_contract(
        state_workspace_dir=tmp_path,
        run_id="run-deck",
        kind="presentation",
        accepted_extensions=[".pptx"],
        visual_verification_required=True,
    )

    stored = await store.load_contract("run-deck")
    assert first == second
    assert stored is not None and stored.task_contract is not None
    assert stored.task_contract.deliverable_contract == first
    assert stored.revision == 1


@pytest.mark.asyncio
async def test_bound_pptx_contract_rejects_html_route_drift(tmp_path) -> None:
    await _stored_run(tmp_path)
    await bind_deliverable_contract(
        state_workspace_dir=tmp_path,
        run_id="run-deck",
        kind="presentation",
        accepted_extensions=[".pptx"],
        visual_verification_required=True,
    )

    with pytest.raises(DeliverableContractConflictError, match="cannot change"):
        await bind_deliverable_contract(
            state_workspace_dir=tmp_path,
            run_id="run-deck",
            kind="presentation",
            accepted_extensions=[".html"],
            visual_verification_required=True,
        )


@pytest.mark.asyncio
async def test_continue_pending_deliverable_atomically_inherits_contract(
    tmp_path,
) -> None:
    store = await _stored_run(tmp_path)
    inherited = RuntimeDeliverableContract(
        kind="presentation",
        accepted_extensions=(".pptx",),
        visual_verification_required=True,
    )

    first = await resolve_pending_deliverable(
        state_workspace_dir=tmp_path,
        run_id="run-deck",
        source_run_id="run-ppt-original",
        decision="continue",
        inherited_contract=inherited,
    )
    second = await resolve_pending_deliverable(
        state_workspace_dir=tmp_path,
        run_id="run-deck",
        source_run_id="run-ppt-original",
        decision="continue",
        inherited_contract=inherited,
    )

    stored = await store.load_contract("run-deck")
    assert first == second
    assert stored is not None and stored.task_contract is not None
    assert stored.task_contract.pending_deliverable_resolution == first
    assert stored.task_contract.deliverable_contract == inherited
    assert stored.revision == 1


@pytest.mark.asyncio
async def test_pending_deliverable_resolution_binds_source_revision_and_job(
    tmp_path,
) -> None:
    store = await _stored_run(tmp_path)
    now = datetime.now(timezone.utc)
    inherited = RuntimeDeliverableContract(
        kind="presentation",
        accepted_extensions=(".pptx",),
        visual_verification_required=True,
    )
    source_task = build_chat_task_contract(
        objective="原始 PPT 任务",
        client_turn_id="turn-source",
        complexity="standard",
        work_method_id="standard",
        run_id="run-ppt-original",
        attempt_id="run-ppt-original:attempt-2",
    ).model_copy(update={"deliverable_contract": inherited})
    await store.save_contract(
        RuntimeRunContract(
            run_id="run-ppt-original",
            attempt_id="run-ppt-original:attempt-2",
            source="chat",
            owner=RuntimeRunOwner(
                user_id="user",
                agent_id="default",
                session_id="console:chat",
                workspace_id="workspace",
                channel="console",
            ),
            status="running",
            revision=4,
            created_at=now,
            started_at=now,
            heartbeat_at=now,
            task_contract=source_task,
            release_edition="official",
        ),
        expected_revision=None,
    )

    resolution = await resolve_pending_deliverable(
        state_workspace_dir=tmp_path,
        run_id="run-deck",
        source_run_id="run-ppt-original",
        source_contract_revision=4,
        source_task_revision=0,
        pending_job_id="presentation-source-job",
        decision="continue",
        inherited_contract=inherited,
    )

    assert resolution.source_contract_revision == 4
    assert resolution.source_task_revision == 0
    assert resolution.pending_job_id == "presentation-source-job"

    with pytest.raises(
        PendingDeliverableResolutionConflictError,
        match="source contract changed",
    ):
        await resolve_pending_deliverable(
            state_workspace_dir=tmp_path,
            run_id="run-deck",
            source_run_id="run-ppt-original",
            source_contract_revision=3,
            source_task_revision=0,
            pending_job_id="presentation-source-job",
            decision="continue",
            inherited_contract=inherited,
        )


@pytest.mark.asyncio
async def test_pending_deliverable_resolution_cannot_be_reinterpreted(
    tmp_path,
) -> None:
    await _stored_run(tmp_path)
    inherited = RuntimeDeliverableContract(
        kind="presentation",
        accepted_extensions=(".pptx",),
        visual_verification_required=True,
    )
    await resolve_pending_deliverable(
        state_workspace_dir=tmp_path,
        run_id="run-deck",
        source_run_id="run-ppt-original",
        decision="replace",
        inherited_contract=inherited,
    )

    with pytest.raises(
        PendingDeliverableResolutionConflictError,
        match="already resolved",
    ):
        await resolve_pending_deliverable(
            state_workspace_dir=tmp_path,
            run_id="run-deck",
            source_run_id="run-ppt-original",
            decision="continue",
            inherited_contract=inherited,
        )
