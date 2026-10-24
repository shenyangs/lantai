# -*- coding: utf-8 -*-
"""Behavior locks for the protected Runtime Run durable store."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from lantai.runtime import (
    LoopContract,
    LoopDeliverable,
    LoopDeterministicGate,
    LoopDeterministicGateResult,
    LoopRubric,
    LoopRubricDimension,
    LoopRubricDimensionScore,
    evaluate_loop_contract,
)
from lantai.runtime.runs import (
    RuntimeRunContract,
    RuntimeRunOwner,
    start_runtime_run_attempt,
)
from lantai.runtime.store import (
    RUNTIME_RUN_STORE_SCHEMA_VERSION,
    RuntimeRunStore,
    RuntimeRunStoreConflictError,
    RuntimeRunStoreCorruptionError,
    RuntimeRunStoreError,
    RuntimeRunStoreLeaseError,
    _checksum_payload,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def _contract(
    *,
    revision: int = 0,
    lease_owner: str | None = None,
    status: str = "running",
) -> RuntimeRunContract:
    return RuntimeRunContract(
        run_id="run-1",
        attempt_id="attempt-1",
        source="chat",
        owner=RuntimeRunOwner(
            user_id="user-1",
            agent_id="agent-default",
            session_id="console:chat-1",
            workspace_id="workspace-1",
            channel="console",
        ),
        status=status,  # type: ignore[arg-type]
        revision=revision,
        lease_owner=lease_owner,
        lease_expires_at=NOW + timedelta(minutes=5) if lease_owner else None,
        created_at=NOW,
        started_at=NOW,
        heartbeat_at=NOW,
        release_edition="official",
    )


def _loop_contract() -> LoopContract:
    return LoopContract(
        contract_id="loop-store-1",
        objective="Persist loop evaluator evidence",
        scope=["Runtime loop evaluation"],
        deliverables=[
            LoopDeliverable(
                deliverable_id="ledger",
                title="Evidence ledger",
                acceptance=["Evaluation history is durable"],
            ),
        ],
        acceptance=["Evaluation reports can be replayed after restart"],
        deterministic_gates=[
            LoopDeterministicGate(
                gate_id="unit-tests",
                name="Unit tests",
                kind="test",
                command="uv run pytest tests/unit/runtime -q",
                required=True,
            ),
        ],
        rubric=LoopRubric(
            rubric_id="loop-quality",
            passing_score=4.0,
            dimensions=[
                LoopRubricDimension(
                    dimension_id="correctness",
                    name="Correctness",
                    weight=1,
                    target_score=4,
                ),
            ],
        ),
    )


def _loop_report(*, report_id: str, score: float) -> object:
    return evaluate_loop_contract(
        _loop_contract(),
        gate_results=[
            LoopDeterministicGateResult(
                gate_id="unit-tests",
                status="passed",
                evidence_hash=f"sha256:{report_id}:gate",
            ),
        ],
        rubric_scores=[
            LoopRubricDimensionScore(
                dimension_id="correctness",
                score=score,
                rationale_summary=f"{report_id} score",
                evidence_hash=f"sha256:{report_id}:rubric",
            ),
        ],
        report_id=report_id,
    )


@pytest.mark.asyncio
async def test_runtime_run_store_writes_versioned_checksum_envelope(tmp_path):
    store = RuntimeRunStore(tmp_path)

    saved = await store.save_contract(_contract(revision=1), expected_revision=None)
    loaded = await store.load_contract("run-1")
    raw = json.loads(store.contract_path("run-1").read_text(encoding="utf-8"))

    assert saved.revision == 1
    assert loaded == saved
    assert raw["schema_version"] == RUNTIME_RUN_STORE_SCHEMA_VERSION
    assert raw["writer_version"].startswith("lantai-runtime-store/")
    assert raw["checksum"].startswith("sha256:")
    assert raw["payload"]["run_id"] == "run-1"
    assert store.contract_path("run-1").stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_runtime_run_store_preserves_attempt_contract_payload(tmp_path):
    store = RuntimeRunStore(tmp_path)
    original = _contract(revision=4, status="failed")
    await store.save_contract(original, expected_revision=None)

    retry = start_runtime_run_attempt(
        original,
        attempt_id="attempt-2",
        attempt_reason="retry",
        now=NOW + timedelta(seconds=1),
    )
    await store.save_contract(
        retry,
        expected_revision=4,
        expected_attempt_id="attempt-1",
    )

    loaded = await store.load_contract("run-1")

    assert loaded is not None
    assert loaded.attempt_id == "attempt-2"
    assert loaded.revision == 0
    assert loaded.status == "queued"
    assert getattr(loaded, "attempt_reason") == "retry"
    assert getattr(loaded, "previous_attempt_id") == "attempt-1"


@pytest.mark.asyncio
async def test_runtime_run_store_lists_readable_contracts(tmp_path):
    store = RuntimeRunStore(tmp_path)
    first = _contract(revision=1)
    second = _contract(revision=2).model_copy(
        update={
            "run_id": "run-2",
            "attempt_id": "attempt-2",
        }
    )

    await store.save_contract(first, expected_revision=None)
    await store.save_contract(second, expected_revision=None)

    contracts = store.list_contracts()

    assert {contract.run_id for contract in contracts} == {"run-1", "run-2"}
    assert {contract.attempt_id for contract in contracts} == {
        "attempt-1",
        "attempt-2",
    }


@pytest.mark.asyncio
async def test_runtime_run_store_list_isolates_contract_in_wrong_directory(tmp_path):
    store = RuntimeRunStore(tmp_path)
    await store.save_contract(_contract(revision=1), expected_revision=None)
    wrong_path = store.contract_path("wrong-run")
    wrong_path.parent.mkdir(parents=True, exist_ok=True)
    wrong_path.write_text(
        store.contract_path("run-1").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    contracts = store.list_contracts()

    assert {contract.run_id for contract in contracts} == {"run-1"}
    assert not wrong_path.exists()
    assert list((store.root / "corrupt").glob("*.bad"))


@pytest.mark.asyncio
async def test_runtime_run_store_archives_contract_without_removing_active_copy(
    tmp_path,
):
    store = RuntimeRunStore(tmp_path)
    await store.save_contract(
        _contract(revision=3, status="succeeded"),
        expected_revision=None,
    )

    archived = await store.archive_contract(
        "run-1",
        expected_attempt_id="attempt-1",
        expected_revision=3,
    )
    repeated = await store.archive_contract(
        "run-1",
        expected_attempt_id="attempt-1",
        expected_revision=3,
    )
    active = await store.load_contract("run-1")
    archive_payload = json.loads(archived.archive_path.read_text(encoding="utf-8"))

    assert archived.kind == "runtime_run_archive"
    assert archived.written is True
    assert archived.active_contract_retained is True
    assert archived.archive_path.exists()
    assert archived.archive_path.stat().st_mode & 0o777 == 0o600
    assert archived.source_contract_path == store.contract_path("run-1")
    assert archived.source_contract_sha256
    assert archived.contract_checksum.startswith("sha256:")
    assert archive_payload["kind"] == "runtime_run_archive"
    assert archive_payload["envelope"]["payload"]["run_id"] == "run-1"
    assert archive_payload["envelope"]["payload"]["revision"] == 3
    assert repeated.archive_path == archived.archive_path
    assert repeated.written is False
    assert active is not None
    assert active.status == "succeeded"
    assert active.revision == 3


@pytest.mark.asyncio
async def test_runtime_run_store_archive_rejects_stale_revision(tmp_path):
    store = RuntimeRunStore(tmp_path)
    await store.save_contract(_contract(revision=3), expected_revision=None)

    with pytest.raises(RuntimeRunStoreConflictError, match="revision"):
        await store.archive_contract(
            "run-1",
            expected_attempt_id="attempt-1",
            expected_revision=2,
        )


@pytest.mark.asyncio
async def test_runtime_run_store_deletes_terminal_contract_after_archiving(
    tmp_path,
):
    store = RuntimeRunStore(tmp_path)
    await store.save_contract(
        _contract(revision=4, status="succeeded"),
        expected_revision=None,
    )

    report = await store.delete_contract(
        "run-1",
        expected_attempt_id="attempt-1",
        expected_revision=4,
    )
    tombstone = json.loads(report.tombstone_path.read_text(encoding="utf-8"))

    assert report.kind == "runtime_run_delete"
    assert report.active_contract_deleted is True
    assert report.artifacts_deleted is False
    assert report.archive_path.exists()
    assert report.tombstone_path.exists()
    assert report.tombstone_path.stat().st_mode & 0o777 == 0o600
    assert report.tombstone_sha256
    assert tombstone["kind"] == "runtime_run_delete"
    assert tombstone["run_id"] == "run-1"
    assert tombstone["revision"] == 4
    assert tombstone["active_contract_deleted"] is True
    assert tombstone["artifacts_deleted"] is False
    assert tombstone["archive_path"] == str(report.archive_path)
    assert await store.load_contract("run-1") is None


@pytest.mark.asyncio
async def test_runtime_run_store_delete_rejects_active_contract_by_default(tmp_path):
    store = RuntimeRunStore(tmp_path)
    await store.save_contract(
        _contract(revision=4, status="running"),
        expected_revision=None,
    )

    with pytest.raises(RuntimeRunStoreError, match="terminal"):
        await store.delete_contract(
            "run-1",
            expected_attempt_id="attempt-1",
            expected_revision=4,
        )

    assert await store.load_contract("run-1") is not None


@pytest.mark.asyncio
async def test_runtime_run_store_delete_rejects_stale_revision(tmp_path):
    store = RuntimeRunStore(tmp_path)
    await store.save_contract(
        _contract(revision=4, status="succeeded"),
        expected_revision=None,
    )

    with pytest.raises(RuntimeRunStoreConflictError, match="revision"):
        await store.delete_contract(
            "run-1",
            expected_attempt_id="attempt-1",
            expected_revision=3,
        )


@pytest.mark.asyncio
async def test_runtime_run_store_rejects_revision_and_lease_conflicts(tmp_path):
    store = RuntimeRunStore(tmp_path)
    await store.save_contract(
        _contract(revision=1, lease_owner="worker-a"),
        expected_revision=None,
    )

    with pytest.raises(RuntimeRunStoreConflictError, match="revision"):
        await store.save_contract(
            _contract(revision=2, lease_owner="worker-a"),
            expected_revision=0,
        )

    with pytest.raises(RuntimeRunStoreLeaseError, match="lease"):
        await store.save_contract(
            _contract(revision=2, lease_owner="worker-b"),
            expected_revision=1,
            lease_owner="worker-b",
        )

    loaded = await store.load_contract("run-1")
    assert loaded is not None
    assert loaded.revision == 1
    assert loaded.lease_owner == "worker-a"


@pytest.mark.asyncio
async def test_runtime_run_store_rejects_final_symlink(tmp_path):
    store = RuntimeRunStore(tmp_path)
    path = store.contract_path("run-1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(tmp_path / "outside.json")

    with pytest.raises(ValueError, match="symlink"):
        await store.save_contract(_contract(revision=1), expected_revision=None)


@pytest.mark.asyncio
async def test_runtime_run_store_isolates_corrupt_contract(tmp_path):
    store = RuntimeRunStore(tmp_path)
    await store.save_contract(_contract(revision=1), expected_revision=None)
    store.contract_path("run-1").write_text("{broken", encoding="utf-8")

    with pytest.raises(RuntimeRunStoreCorruptionError) as exc_info:
        await store.load_contract("run-1")

    report = exc_info.value.report
    assert report.run_id == "run-1"
    assert report.isolated_path is not None
    assert report.isolated_path.exists()
    assert report.report_path is not None
    assert report.report_path.exists()
    assert not store.contract_path("run-1").exists()


@pytest.mark.asyncio
async def test_runtime_run_store_isolates_contract_run_id_mismatch(tmp_path):
    store = RuntimeRunStore(tmp_path)
    await store.save_contract(_contract(revision=1), expected_revision=None)
    path = store.contract_path("run-1")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["payload"]["run_id"] = "different-run"
    raw["checksum"] = _checksum_payload(raw["payload"])
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(RuntimeRunStoreCorruptionError) as exc_info:
        await store.load_contract("run-1")

    report = exc_info.value.report
    assert "run_id mismatch" in report.message
    assert report.isolated_path is not None
    assert report.isolated_path.exists()
    assert not path.exists()


@pytest.mark.asyncio
async def test_runtime_run_store_migration_dry_run_and_backup_are_idempotent(tmp_path):
    store = RuntimeRunStore(tmp_path)
    legacy_payload = {
        "schema_version": 0,
        "id": "legacy-run",
        "source": "chat",
        "agent_id": "agent-default",
        "session_id": "console:chat-1",
        "status": "completed",
        "started_at": NOW.isoformat(),
        "ended_at": NOW.isoformat(),
        "release_edition": "official",
    }

    dry_run = await store.migrate_legacy_contract(legacy_payload, dry_run=True)
    assert dry_run.dry_run is True
    assert dry_run.would_write is True
    assert await store.load_contract("legacy-run") is None

    migrated = await store.migrate_legacy_contract(legacy_payload, dry_run=False)
    repeated = await store.migrate_legacy_contract(legacy_payload, dry_run=False)

    assert migrated.dry_run is False
    assert migrated.written is True
    assert migrated.backup_path is not None
    assert migrated.backup_path.exists()
    assert repeated.written is False
    assert repeated.backup_path == migrated.backup_path


@pytest.mark.asyncio
async def test_runtime_run_store_persists_loop_evaluation_history(tmp_path):
    store = RuntimeRunStore(tmp_path)
    first = _loop_report(report_id="report-first", score=4)
    second = _loop_report(report_id="report-second", score=5)

    first_snapshot = await store.save_loop_evaluation_report(
        "run-1",
        attempt_id="attempt-1",
        report=first,
        saved_at=NOW,
    )
    await store.save_loop_evaluation_report(
        "run-1",
        attempt_id="attempt-1",
        report=second,
        saved_at=NOW + timedelta(seconds=1),
    )

    reports = await store.list_loop_evaluation_reports("run-1")
    latest = await store.load_latest_loop_evaluation_report(
        "run-1",
        expected_attempt_id="attempt-1",
    )
    raw = json.loads(
        store.loop_evaluation_path("run-1", first.report_id).read_text(
            encoding="utf-8",
        ),
    )

    assert first_snapshot.run_id == "run-1"
    assert first_snapshot.attempt_id == "attempt-1"
    assert first_snapshot.report.report_id == "report-first"
    assert [item.report.report_id for item in reports] == [
        "report-second",
        "report-first",
    ]
    assert latest is not None
    assert latest.report.report_id == "report-second"
    assert raw["schema_version"] == 1
    assert raw["writer_version"].startswith("lantai-runtime-store/")
    assert raw["checksum"].startswith("sha256:")
    assert raw["payload"]["report"]["report_id"] == "report-first"
    assert (
        store.loop_evaluation_path("run-1", first.report_id).stat().st_mode & 0o777
        == 0o600
    )


@pytest.mark.asyncio
async def test_runtime_run_store_persists_loop_contract_sidecar(tmp_path):
    store = RuntimeRunStore(tmp_path)
    contract = _loop_contract()

    snapshot = await store.save_loop_contract(
        "run-1",
        attempt_id="attempt-1",
        contract=contract,
        saved_at=NOW,
    )
    loaded = await store.load_loop_contract_snapshot(
        "run-1",
        expected_attempt_id="attempt-1",
    )
    mismatched = await store.load_loop_contract_snapshot(
        "run-1",
        expected_attempt_id="attempt-2",
    )
    raw = json.loads(store.loop_contract_path("run-1").read_text(encoding="utf-8"))

    assert snapshot.run_id == "run-1"
    assert snapshot.attempt_id == "attempt-1"
    assert snapshot.contract.contract_id == contract.contract_id
    assert loaded is not None
    assert loaded.contract.contract_id == contract.contract_id
    assert mismatched is None
    assert raw["schema_version"] == 1
    assert raw["writer_version"].startswith("lantai-runtime-store/")
    assert raw["checksum"].startswith("sha256:")
    assert raw["payload"]["contract"]["contract_id"] == contract.contract_id
    assert store.loop_contract_path("run-1").stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_runtime_run_store_filters_loop_evaluations_by_attempt(tmp_path):
    store = RuntimeRunStore(tmp_path)
    await store.save_loop_evaluation_report(
        "run-1",
        attempt_id="attempt-1",
        report=_loop_report(report_id="report-attempt-1", score=4),
        saved_at=NOW,
    )
    await store.save_loop_evaluation_report(
        "run-1",
        attempt_id="attempt-2",
        report=_loop_report(report_id="report-attempt-2", score=5),
        saved_at=NOW + timedelta(seconds=1),
    )

    attempt_one_reports = await store.list_loop_evaluation_reports(
        "run-1",
        expected_attempt_id="attempt-1",
    )
    latest_attempt_one = await store.load_latest_loop_evaluation_report(
        "run-1",
        expected_attempt_id="attempt-1",
    )
    missing_attempt = await store.load_latest_loop_evaluation_report(
        "run-1",
        expected_attempt_id="attempt-3",
    )

    assert [item.report.report_id for item in attempt_one_reports] == [
        "report-attempt-1",
    ]
    assert latest_attempt_one is not None
    assert latest_attempt_one.report.report_id == "report-attempt-1"
    assert missing_attempt is None


@pytest.mark.asyncio
async def test_runtime_run_store_list_isolates_loop_evaluation_report_id_mismatch(
    tmp_path,
):
    store = RuntimeRunStore(tmp_path)
    report = _loop_report(report_id="report-original", score=4)
    await store.save_loop_evaluation_report(
        "run-1",
        attempt_id="attempt-1",
        report=report,
        saved_at=NOW,
    )
    path = store.loop_evaluation_path("run-1", report.report_id)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["payload"]["report"]["report_id"] = "report-forged"
    raw["checksum"] = _checksum_payload(raw["payload"])
    path.write_text(json.dumps(raw), encoding="utf-8")

    reports = await store.list_loop_evaluation_reports("run-1")
    latest = await store.load_latest_loop_evaluation_report("run-1")

    assert reports == []
    assert latest is None
    assert not path.exists()


@pytest.mark.asyncio
async def test_runtime_run_store_isolates_corrupt_loop_evaluation(tmp_path):
    store = RuntimeRunStore(tmp_path)
    report = _loop_report(report_id="report-corrupt", score=4)
    await store.save_loop_evaluation_report(
        "run-1",
        attempt_id="attempt-1",
        report=report,
        saved_at=NOW,
    )
    path = store.loop_evaluation_path("run-1", report.report_id)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["checksum"] = "sha256:bad"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(RuntimeRunStoreCorruptionError) as exc_info:
        await store.load_loop_evaluation_report("run-1", report.report_id)

    corruption_report = exc_info.value.report
    assert corruption_report.run_id == "run-1"
    assert corruption_report.isolated_path is not None
    assert corruption_report.isolated_path.exists()
    assert corruption_report.report_path is not None
    assert corruption_report.report_path.exists()
    assert not path.exists()
