from __future__ import annotations

import json
from pathlib import Path

import pytest

from lantai.presentation.jobs import PresentationJobStore
from lantai.presentation.models import JobStage, JobStatus, PresentationJobRequest


_V1_FIELDS = {
    "schema_version",
    "job_id",
    "conversation_id",
    "request",
    "skill_identity",
    "stage",
    "status",
    "last_sequence",
    "checkpoints",
    "events",
    "created_at",
    "updated_at",
}


def _write_v1_manifest(store: PresentationJobStore) -> tuple[str, Path]:
    record = store.create(
        PresentationJobRequest(
            conversation_id="legacy-conversation",
            user_request="continue the original deck",
            slide_count=1,
        ),
    )
    manifest_path = store.job_dir(record.job_id) / "manifest.json"
    current = json.loads(manifest_path.read_text(encoding="utf-8"))
    legacy = {key: value for key, value in current.items() if key in _V1_FIELDS}
    legacy["schema_version"] = "1.0"
    manifest_path.write_text(
        json.dumps(legacy, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return record.job_id, manifest_path


def test_v1_job_read_has_stable_owner_without_rewriting_history(tmp_path) -> None:
    store = PresentationJobStore(tmp_path)
    job_id, manifest_path = _write_v1_manifest(store)
    original_bytes = manifest_path.read_bytes()

    first = store.get(job_id)
    second = PresentationJobStore(tmp_path).get(job_id)

    assert first.job_id == second.job_id == job_id
    assert first.runtime_run_id == second.runtime_run_id
    assert first.runtime_run_id == f"run-legacy-{job_id.removeprefix('presentation-')}"
    assert first.schema_version == second.schema_version == "2.0"
    assert manifest_path.read_bytes() == original_bytes


def test_first_v1_job_mutation_writes_v2_without_creating_second_owner(
    tmp_path,
) -> None:
    store = PresentationJobStore(tmp_path)
    job_id, manifest_path = _write_v1_manifest(store)
    legacy_owner = store.get(job_id).runtime_run_id

    store.append_event(
        job_id,
        stage=JobStage.BRIEF,
        status=JobStatus.RUNNING,
        message="resume the original job",
        checkpoint="brief.json",
    )

    written = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert written["schema_version"] == "2.0"
    assert written["job_id"] == job_id
    assert written["conversation_id"] == "legacy-conversation"
    assert written["runtime_run_id"] == legacy_owner
    assert written["checkpoints"] == {"brief": "brief.json"}
    assert len(list(tmp_path.glob("presentation-*/manifest.json"))) == 1


def test_current_runtime_adopts_v1_job_through_same_writer_lease(tmp_path) -> None:
    store = PresentationJobStore(tmp_path)
    job_id, manifest_path = _write_v1_manifest(store)

    with store.writer_lease(job_id, runtime_run_id="run-current-native") as lease:
        assert lease.job_id == job_id
        assert lease.runtime_run_id == "run-current-native"

    written = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert written["schema_version"] == "2.0"
    assert written["job_id"] == job_id
    assert written["runtime_run_id"] == "run-current-native"
    assert written["active_lease_id"] is None
    assert len(list(tmp_path.glob("presentation-*/manifest.json"))) == 1


def test_unknown_job_schema_is_not_silently_downgraded(tmp_path) -> None:
    store = PresentationJobStore(tmp_path)
    job_id, manifest_path = _write_v1_manifest(store)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["schema_version"] = "99.0"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported presentation job schema: 99.0"):
        store.get(job_id)


def test_read_only_store_rejects_mutation_without_materializing_root(
    tmp_path,
) -> None:
    root = tmp_path / "presentation-jobs"
    store = PresentationJobStore(root, read_only=True)

    with pytest.raises(RuntimeError, match="read-only presentation job store"):
        store.write_checkpoint(
            "presentation-read-only",
            "deck-ir.json",
            "{}",
        )
    with pytest.raises(RuntimeError, match="read-only presentation job store"):
        store.create(
            PresentationJobRequest(
                conversation_id="conversation-read-only",
                user_request="must not materialize",
                slide_count=1,
            )
        )

    assert not root.exists()
