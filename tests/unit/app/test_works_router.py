# -*- coding: utf-8 -*-
# Router tests intentionally verify the validation and digest seams directly.
# pylint: disable=protected-access

import asyncio
import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from fastapi import HTTPException

from lantai.app.routers import works as works_router
from lantai.app.runner.models import ChatSpec
from lantai.app import works as works_store
from lantai.app.works import add_work, works_index_path
from lantai.runtime.artifact_effects import (
    RuntimeArtifactEffectJournal,
    RuntimeArtifactEffectRecord,
)
from lantai.runtime.runs import (
    RuntimeCompletionEvidence,
    RuntimeRunContract,
    RuntimeRunOwner,
    RuntimeValidationMetadata,
)
from lantai.runtime.store import RuntimeRunStore


def _write_ooxml_stub(path: Path, *, payload: bytes = b"<document />") -> None:
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", b"<Types />")
        archive.writestr("word/document.xml", payload)


def _project_validation_fixture(
    tmp_path: Path,
    *,
    rows: tuple[dict[str, object], ...],
    required_scope: tuple[str, ...],
    observed_scope: tuple[str, ...],
):
    source = tmp_path / "candidate.txt"
    source.write_text("candidate", encoding="utf-8")
    work = add_work(
        tmp_path,
        source_path=source,
        allowed_roots=[tmp_path],
        source_run_id="run-validation-outcomes",
    )
    now = datetime.now(timezone.utc)
    contract = RuntimeRunContract(
        run_id="run-validation-outcomes",
        attempt_id="attempt-1",
        source="chat",
        owner=RuntimeRunOwner(
            agent_id="default",
            session_id="console:chat-1",
            workspace_id=str(tmp_path),
        ),
        status="failed",
        delivery_status="partial",
        completion_evidence=RuntimeCompletionEvidence(
            acceptance_results=rows,
            validation_work_item_ids=(work.id,),
            validation=RuntimeValidationMetadata(
                validator_id="runtime-completion-gate",
                validator_version="1",
                ruleset_id="delivery-readiness",
                ruleset_version="1",
                assessed_at=now,
                required_scope=required_scope,
                observed_scope=observed_scope,
            ),
        ),
        release_edition="official",
        created_at=now,
    )
    return works_router._project_validation(work, contract)


@pytest.mark.parametrize(
    (
        "rows",
        "required_scope",
        "observed_scope",
        "expected_status",
        "outcomes",
    ),
    (
        (
            (
                {
                    "id": "validator",
                    "passed": False,
                    "outcome": "error",
                    "required": True,
                    "detail": "validator unavailable",
                },
            ),
            ("validator",),
            (),
            "error",
            ["error"],
        ),
        (
            (
                {
                    "id": "unsupported",
                    "passed": False,
                    "outcome": "not_run",
                    "required": True,
                    "detail": "unsupported format",
                },
            ),
            ("unsupported",),
            (),
            "not_run",
            ["not_run"],
        ),
        (
            (
                {
                    "id": "opened",
                    "passed": True,
                    "outcome": "passed",
                    "required": True,
                    "detail": "opened",
                },
                {
                    "id": "visual",
                    "passed": False,
                    "outcome": "not_run",
                    "required": True,
                    "detail": "not run",
                },
            ),
            ("opened", "visual"),
            ("opened",),
            "partial",
            ["passed", "not_run"],
        ),
        (
            (
                {
                    "id": "approved-exception",
                    "passed": True,
                    "outcome": "waived",
                    "required": True,
                    "detail": "explicit approval",
                    "evidence_refs": ["approval:user:2026-09-21"],
                },
            ),
            ("approved-exception",),
            ("approved-exception",),
            "passed",
            ["waived"],
        ),
    ),
)
def test_validation_projection_preserves_non_boolean_outcomes(
    tmp_path: Path,
    rows: tuple[dict[str, object], ...],
    required_scope: tuple[str, ...],
    observed_scope: tuple[str, ...],
    expected_status: str,
    outcomes: list[str],
) -> None:
    projection = _project_validation_fixture(
        tmp_path,
        rows=rows,
        required_scope=required_scope,
        observed_scope=observed_scope,
    )

    assert projection is not None
    assert projection.status == expected_status
    assert [check.outcome for check in projection.checks] == outcomes


def test_validation_projection_fails_closed_for_unknown_explicit_outcome(
    tmp_path: Path,
) -> None:
    projection = _project_validation_fixture(
        tmp_path,
        rows=(
            {
                "id": "visual-review",
                "passed": True,
                "outcome": "skipped",
                "required": True,
                "detail": "legacy producer emitted an unknown state",
            },
        ),
        required_scope=("visual-review",),
        observed_scope=("visual-review",),
    )

    assert projection is not None
    assert projection.status == "error"
    assert projection.checks[0].outcome == "error"
    assert "unknown outcome 'skipped'" in projection.checks[0].detail


@pytest.mark.asyncio
async def test_list_route_can_be_scoped_to_one_canonical_chat(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    for name, chat_id in (("one.txt", "chat-1"), ("two.txt", "chat-2")):
        source = workspace_dir / name
        source.write_text(name, encoding="utf-8")
        add_work(
            workspace_dir,
            source_path=source,
            allowed_roots=[workspace_dir],
            source_chat_id=chat_id,
        )
    source = workspace_dir / "unowned.txt"
    source.write_text("unowned", encoding="utf-8")
    add_work(
        workspace_dir,
        source_path=source,
        allowed_roots=[workspace_dir],
    )
    workspace = SimpleNamespace(workspace_dir=str(workspace_dir))

    async def _agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        works_router,
        "get_agent_for_request",
        _agent_for_request,
    )

    scoped = await works_router.get_works(SimpleNamespace(), chat_id="chat-1")
    unscoped = await works_router.get_works(SimpleNamespace())

    assert [item.source_chat_id for item in scoped] == ["chat-1"]
    assert scoped[0].artifact_role is None
    assert scoped[0].completion_bound is False
    assert scoped[0].delivery_status is None
    assert len(unscoped) == 3
    assert (workspace_dir / "one.txt").read_text(encoding="utf-8") == "one.txt"
    assert (workspace_dir / "two.txt").read_text(encoding="utf-8") == "two.txt"
    assert (workspace_dir / "unowned.txt").read_text(
        encoding="utf-8",
    ) == "unowned"


@pytest.mark.asyncio
async def test_scoped_list_filters_index_before_hashing_unrelated_works(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    for index in range(12):
        source = workspace_dir / f"work-{index}.txt"
        source.write_text(f"payload-{index}", encoding="utf-8")
        add_work(
            workspace_dir,
            source_path=source,
            allowed_roots=[workspace_dir],
            source_chat_id="selected" if index in {2, 9} else "other",
        )
    workspace = SimpleNamespace(workspace_dir=str(workspace_dir))

    async def _agent_for_request(_request):
        return workspace

    hashed: list[str] = []
    original_digest = works_store._source_digest

    def _counted_digest(path: Path) -> str:
        hashed.append(path.name)
        return original_digest(path)

    monkeypatch.setattr(
        works_router,
        "get_agent_for_request",
        _agent_for_request,
    )
    monkeypatch.setattr(works_store, "_source_digest", _counted_digest)

    scoped = await works_router.get_works(
        SimpleNamespace(),
        chat_id="selected",
    )

    assert len(scoped) == 2
    assert len(hashed) == 2
    assert {item.source_chat_id for item in scoped} == {"selected"}


@pytest.mark.asyncio
async def test_list_route_keeps_event_loop_responsive_during_index_io(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = SimpleNamespace(workspace_dir=str(tmp_path))
    started = threading.Event()
    release = threading.Event()
    worker_threads: list[int] = []

    async def _agent_for_request(_request):
        return workspace

    def _blocking_list_works(*_args, **_kwargs):
        worker_threads.append(threading.get_ident())
        started.set()
        assert release.wait(timeout=2)
        return []

    monkeypatch.setattr(
        works_router,
        "get_agent_for_request",
        _agent_for_request,
    )
    monkeypatch.setattr(works_router, "list_works", _blocking_list_works)

    timer = threading.Timer(0.1, release.set)
    timer.start()
    try:
        request_task = asyncio.create_task(
            works_router.get_works(SimpleNamespace()),
        )
        await asyncio.sleep(0)
        assert await asyncio.to_thread(started.wait, 1)
        assert request_task.done() is False
        assert worker_threads != [threading.get_ident()]
        release.set()
        assert await request_task == []
    finally:
        release.set()
        timer.cancel()


@pytest.mark.asyncio
async def test_index_failure_has_structured_diagnostics_and_confirmed_recovery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    for name in ("one.txt", "two.txt"):
        source = workspace_dir / name
        source.write_text(name, encoding="utf-8")
        add_work(
            workspace_dir,
            source_path=source,
            allowed_roots=[workspace_dir],
        )
    workspace = SimpleNamespace(workspace_dir=str(workspace_dir))

    async def _agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        works_router,
        "get_agent_for_request",
        _agent_for_request,
    )
    works_index_path(workspace_dir).write_text(
        '{"schema_version":',
        encoding="utf-8",
    )

    with pytest.raises(HTTPException) as blocked:
        await works_router.get_works(SimpleNamespace())
    assert blocked.value.status_code == 409
    assert blocked.value.detail["code"] == "works_index_invalid_json"
    assert blocked.value.detail["diagnostic_id"].startswith("works-index-")

    inspection = await works_router.get_works_index_diagnostics(
        SimpleNamespace(),
    )
    assert inspection.status == "recovery_required"
    assert inspection.current_digest == blocked.value.detail["current_digest"]
    assert inspection.backups

    preview = await works_router.get_works_index_recovery_preview(
        inspection.backups[0].digest,
        SimpleNamespace(),
    )
    assert preview.current_digest == inspection.current_digest
    assert preview.confirmation_required is True
    assert len(preview.snapshot_works) == 1

    restored = await works_router.recover_works_index(
        works_router.WorksIndexRestoreRequest(
            expected_current_digest=preview.current_digest,
            snapshot_digest=preview.snapshot_digest,
            confirm=True,
        ),
        SimpleNamespace(),
    )
    assert restored.status == "restored"
    assert restored.quarantine_digest == preview.current_digest
    assert (
        len(
            json.loads(
                works_index_path(workspace_dir).read_text(encoding="utf-8"),
            )["works"],
        )
        == 1
    )


@pytest.mark.asyncio
async def test_list_projects_role_and_delivery_from_runtime_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    source = workspace_dir / "answer.pptx"
    source.write_bytes(b"immutable ppt candidate")
    work = add_work(
        workspace_dir,
        source_path=source,
        allowed_roots=[workspace_dir],
        source_chat_id="chat-1",
        source_run_id="run-1",
    )
    now = datetime.now(timezone.utc)
    contract = RuntimeRunContract(
        run_id="run-1",
        attempt_id="attempt-1",
        source="chat",
        owner=RuntimeRunOwner(
            agent_id="default",
            session_id="console:chat-1",
            workspace_id=str(workspace_dir),
        ),
        status="succeeded",
        delivery_status="verified",
        completion_evidence=RuntimeCompletionEvidence(
            acceptance_results=(
                {
                    "id": "deliverable_openable",
                    "passed": True,
                    "required": True,
                    "detail": "The package opens.",
                },
            ),
            primary_artifact_ids=("artifact-primary",),
            works_item_ids=(work.id,),
            validation_work_item_ids=(work.id,),
            validation=RuntimeValidationMetadata(
                validator_id="runtime-completion-gate",
                validator_version="1",
                ruleset_id="delivery-readiness",
                ruleset_version="1",
                assessed_at=now,
                required_scope=("deliverable_openable",),
                observed_scope=("deliverable_openable",),
            ),
        ),
        release_edition="official",
        created_at=now,
        started_at=now,
        settled_at=now,
    )
    await RuntimeRunStore(workspace_dir).save_contract(
        contract,
        expected_revision=None,
    )
    RuntimeArtifactEffectJournal(workspace_dir).append(
        RuntimeArtifactEffectRecord(
            run_id="run-1",
            attempt_id="attempt-1",
            source_effect_id="effect-finalize",
            tool_call_id="call-finalize",
            tool_name="finalize_deliverable",
            artifact_effect_id="artifact-effect-primary",
            artifact_id="artifact-primary",
            artifact_role="primary",
            status="committed",
            revision=1,
            relative_path="answer.pptx",
            delivery_idempotency_key="delivery:answer",
            created_at=now,
            settled_at=now,
            size_bytes=source.stat().st_size,
            sha256=f"sha256:{hashlib.sha256(source.read_bytes()).hexdigest()}",
            media_type=(
                "application/vnd.openxmlformats-officedocument."
                "presentationml.presentation"
            ),
        ),
    )
    workspace = SimpleNamespace(workspace_dir=str(workspace_dir))

    async def _agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        works_router,
        "get_agent_for_request",
        _agent_for_request,
    )

    projected = await works_router.get_works(
        SimpleNamespace(),
        chat_id="chat-1",
    )

    assert len(projected) == 1
    assert projected[0].artifact_role == "primary"
    assert projected[0].completion_bound is True
    assert projected[0].delivery_status == "verified"
    assert projected[0].validation is not None
    assert projected[0].validation.status == "passed"
    assert projected[0].validation.artifact_digest == work.source_digest
    assert projected[0].validation.validator_version == "1"
    assert projected[0].validation.ruleset_version == "1"
    assert projected[0].validation.required_scope == ["deliverable_openable"]


@pytest.mark.asyncio
async def test_failed_validation_candidate_is_not_completion_bound(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    source = workspace_dir / "candidate.html"
    source.write_text("<html><body>candidate</body></html>", encoding="utf-8")
    work = add_work(
        workspace_dir,
        source_path=source,
        allowed_roots=[workspace_dir],
        source_chat_id="chat-1",
        source_run_id="run-validation-only",
    )
    now = datetime.now(timezone.utc)
    contract = RuntimeRunContract(
        run_id="run-validation-only",
        attempt_id="attempt-1",
        source="chat",
        owner=RuntimeRunOwner(
            agent_id="default",
            session_id="console:chat-1",
            workspace_id=str(workspace_dir),
        ),
        status="failed",
        delivery_status="partial",
        completion_evidence=RuntimeCompletionEvidence(
            acceptance_results=(
                {
                    "id": "deliverable_openable",
                    "passed": False,
                    "required": True,
                    "detail": "The candidate is invalid.",
                },
            ),
            validation_work_item_ids=(work.id,),
            validation=RuntimeValidationMetadata(
                validator_id="runtime-completion-gate",
                validator_version="1",
                ruleset_id="delivery-readiness",
                ruleset_version="1",
                assessed_at=now,
                required_scope=("deliverable_openable",),
                observed_scope=("deliverable_openable",),
            ),
        ),
        release_edition="official",
        created_at=now,
        started_at=now,
        settled_at=now,
    )
    await RuntimeRunStore(workspace_dir).save_contract(
        contract,
        expected_revision=None,
    )
    workspace = SimpleNamespace(workspace_dir=str(workspace_dir))

    async def _agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        works_router,
        "get_agent_for_request",
        _agent_for_request,
    )

    projected = await works_router.get_works(
        SimpleNamespace(),
        chat_id="chat-1",
    )

    assert len(projected) == 1
    assert projected[0].completion_bound is False
    assert projected[0].delivery_status is None
    assert projected[0].validation is not None
    assert projected[0].validation.status == "failed"


@pytest.mark.asyncio
async def test_list_route_selects_latest_exact_validation_attempt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    source = workspace_dir / "candidate.html"
    source.write_text("<html><body>candidate</body></html>", encoding="utf-8")
    work = add_work(
        workspace_dir,
        source_path=source,
        allowed_roots=[workspace_dir],
        source_chat_id="chat-1",
        source_run_id="run-revalidated",
    )
    first_checked_at = datetime(2026, 9, 21, 1, 0, tzinfo=timezone.utc)
    second_checked_at = datetime(2026, 9, 21, 2, 0, tzinfo=timezone.utc)
    owner = RuntimeRunOwner(
        agent_id="default",
        session_id="console:chat-1",
        workspace_id=str(workspace_dir),
    )

    def _contract(
        *,
        attempt_id: str,
        checked_at: datetime,
        passed: bool,
        revision: int,
    ) -> RuntimeRunContract:
        return RuntimeRunContract(
            run_id="run-revalidated",
            attempt_id=attempt_id,
            source="chat",
            owner=owner,
            status="succeeded" if passed else "failed",
            delivery_status="verified" if passed else "partial",
            completion_evidence=RuntimeCompletionEvidence(
                acceptance_results=(
                    {
                        "id": "deliverable_openable",
                        "passed": passed,
                        "required": True,
                        "detail": "latest result"
                        if passed
                        else "earlier result",
                    },
                ),
                validation_work_item_ids=(work.id,),
                validation=RuntimeValidationMetadata(
                    validator_id="runtime-completion-gate",
                    validator_version="1",
                    ruleset_id="delivery-readiness",
                    ruleset_version=str(revision),
                    assessed_at=checked_at,
                    required_scope=("deliverable_openable",),
                    observed_scope=("deliverable_openable",),
                ),
            ),
            release_edition="official",
            created_at=checked_at,
            started_at=checked_at,
            settled_at=checked_at,
            revision=revision,
        )

    store = RuntimeRunStore(workspace_dir)
    await store.save_contract(
        _contract(
            attempt_id="attempt-1",
            checked_at=first_checked_at,
            passed=False,
            revision=1,
        ),
        expected_revision=None,
    )
    await store.archive_contract(
        "run-revalidated",
        expected_revision=1,
        expected_attempt_id="attempt-1",
    )
    await store.save_contract(
        _contract(
            attempt_id="attempt-2",
            checked_at=second_checked_at,
            passed=True,
            revision=2,
        ),
        expected_revision=1,
        expected_attempt_id="attempt-1",
    )
    workspace = SimpleNamespace(workspace_dir=str(workspace_dir))

    async def _agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        works_router,
        "get_agent_for_request",
        _agent_for_request,
    )

    projected = await works_router.get_works(
        SimpleNamespace(),
        chat_id="chat-1",
    )

    assert len(projected) == 1
    assert projected[0].completion_bound is False
    assert projected[0].delivery_status is None
    assert projected[0].validation is not None
    assert projected[0].validation.source_attempt_id == "attempt-2"
    assert projected[0].validation.status == "passed"
    assert projected[0].validation.ruleset_version == "2"


@pytest.mark.asyncio
async def test_list_keeps_delivery_after_same_run_starts_new_attempt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    source = workspace_dir / "answer.pptx"
    source.write_bytes(b"immutable historical delivery")
    work = add_work(
        workspace_dir,
        source_path=source,
        allowed_roots=[workspace_dir],
        source_chat_id="chat-1",
        source_run_id="run-1",
    )
    now = datetime.now(timezone.utc)
    delivered = RuntimeRunContract(
        run_id="run-1",
        attempt_id="attempt-1",
        source="chat",
        owner=RuntimeRunOwner(
            agent_id="default",
            session_id="console:chat-1",
            workspace_id=str(workspace_dir),
        ),
        status="succeeded",
        delivery_status="verified",
        completion_evidence=RuntimeCompletionEvidence(
            primary_artifact_ids=("artifact-primary",),
            works_item_ids=(work.id,),
        ),
        release_edition="official",
        created_at=now,
        started_at=now,
        settled_at=now,
        revision=4,
    )
    store = RuntimeRunStore(workspace_dir)
    await store.save_contract(delivered, expected_revision=None)
    await store.archive_contract(
        "run-1",
        expected_revision=4,
        expected_attempt_id="attempt-1",
    )
    await store.save_contract(
        RuntimeRunContract(
            run_id="run-1",
            attempt_id="attempt-2",
            source="chat",
            owner=delivered.owner,
            status="running",
            release_edition="official",
            created_at=now,
        ),
        expected_revision=4,
        expected_attempt_id="attempt-1",
    )
    RuntimeArtifactEffectJournal(workspace_dir).append(
        RuntimeArtifactEffectRecord(
            run_id="run-1",
            attempt_id="attempt-1",
            source_effect_id="effect-finalize",
            tool_call_id="call-finalize",
            tool_name="finalize_deliverable",
            artifact_effect_id="artifact-effect-primary",
            artifact_id="artifact-primary",
            artifact_role="primary",
            status="committed",
            revision=1,
            relative_path="answer.pptx",
            delivery_idempotency_key="delivery:answer",
            created_at=now,
            settled_at=now,
            size_bytes=source.stat().st_size,
            sha256=f"sha256:{hashlib.sha256(source.read_bytes()).hexdigest()}",
            media_type=(
                "application/vnd.openxmlformats-officedocument."
                "presentationml.presentation"
            ),
        ),
    )
    workspace = SimpleNamespace(workspace_dir=str(workspace_dir))

    async def _agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        works_router,
        "get_agent_for_request",
        _agent_for_request,
    )

    projected = await works_router.get_works(
        SimpleNamespace(),
        chat_id="chat-1",
    )

    assert len(projected) == 1
    assert projected[0].artifact_role == "primary"
    assert projected[0].completion_bound is True
    assert projected[0].delivery_status == "verified"


@pytest.mark.asyncio
async def test_list_does_not_cross_bind_name_and_digest_across_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace_dir = tmp_path / "workspace"
    primary_dir = workspace_dir / "primary"
    reference_dir = workspace_dir / "reference"
    primary_dir.mkdir(parents=True)
    reference_dir.mkdir(parents=True)
    payload = b"same bytes, distinct runtime artifacts"
    primary_source = primary_dir / "answer.pptx"
    candidate_source = reference_dir / "answer.pptx"
    primary_source.write_bytes(payload)
    candidate_source.write_bytes(payload)
    work = add_work(
        workspace_dir,
        source_path=candidate_source,
        allowed_roots=[workspace_dir],
        source_chat_id="chat-1",
        source_run_id="run-1",
    )
    now = datetime.now(timezone.utc)
    contract = RuntimeRunContract(
        run_id="run-1",
        attempt_id="attempt-1",
        source="chat",
        owner=RuntimeRunOwner(
            agent_id="default",
            session_id="console:chat-1",
            workspace_id=str(workspace_dir),
        ),
        status="succeeded",
        delivery_status="verified",
        completion_evidence=RuntimeCompletionEvidence(
            primary_artifact_ids=("artifact-primary",),
            works_item_ids=(work.id,),
        ),
        release_edition="official",
        created_at=now,
        started_at=now,
        settled_at=now,
    )
    await RuntimeRunStore(workspace_dir).save_contract(
        contract,
        expected_revision=None,
    )
    journal = RuntimeArtifactEffectJournal(workspace_dir)
    digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    for effect_id, artifact_id, role, relative_path in (
        (
            "artifact-effect-primary",
            "artifact-primary",
            "primary",
            "primary/answer.pptx",
        ),
        (
            "artifact-effect-candidate",
            "artifact-candidate",
            "candidate",
            "reference/answer.pptx",
        ),
    ):
        journal.append(
            RuntimeArtifactEffectRecord(
                run_id="run-1",
                attempt_id="attempt-1",
                source_effect_id=f"effect-{role}",
                tool_call_id=f"call-{role}",
                tool_name="finalize_deliverable",
                artifact_effect_id=effect_id,
                artifact_id=artifact_id,
                artifact_role=role,
                status="committed",
                revision=1,
                relative_path=relative_path,
                delivery_idempotency_key=f"delivery:{role}",
                created_at=now,
                settled_at=now,
                size_bytes=len(payload),
                sha256=digest,
                media_type=(
                    "application/vnd.openxmlformats-officedocument."
                    "presentationml.presentation"
                ),
            ),
        )
    workspace = SimpleNamespace(workspace_dir=str(workspace_dir))

    async def _agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        works_router,
        "get_agent_for_request",
        _agent_for_request,
    )

    projected = await works_router.get_works(
        SimpleNamespace(),
        chat_id="chat-1",
    )

    assert len(projected) == 1
    assert projected[0].artifact_role == "candidate"


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["chat-2", None])
async def test_content_route_rejects_cross_chat_or_unowned_work_when_scoped(
    tmp_path: Path,
    monkeypatch,
    owner: str | None,
) -> None:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    source = workspace_dir / "report.txt"
    source.write_text("report", encoding="utf-8")
    work = add_work(
        workspace_dir,
        source_path=source,
        allowed_roots=[workspace_dir],
        source_chat_id=owner,
    )
    workspace = SimpleNamespace(workspace_dir=str(workspace_dir))

    async def _agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        works_router,
        "get_agent_for_request",
        _agent_for_request,
    )

    with pytest.raises(HTTPException) as raised:
        await works_router.get_work_content(
            work.id,
            SimpleNamespace(),
            chat_id="chat-1",
        )

    assert raised.value.status_code == 409
    assert "not an outcome" in raised.value.detail


@pytest.mark.asyncio
async def test_preview_routes_reject_cross_chat_work_before_rendering(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    source = workspace_dir / "deck.pdf"
    source.write_bytes(b"%PDF-1.7 fixture")
    work = add_work(
        workspace_dir,
        source_path=source,
        allowed_roots=[workspace_dir],
        source_chat_id="chat-2",
    )
    workspace = SimpleNamespace(workspace_dir=str(workspace_dir))

    async def _agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        works_router,
        "get_agent_for_request",
        _agent_for_request,
    )

    with pytest.raises(HTTPException) as preview_error:
        await works_router.get_work_preview(
            work.id,
            SimpleNamespace(),
            chat_id="chat-1",
        )
    with pytest.raises(HTTPException) as page_error:
        await works_router.get_work_preview_image(
            work.id,
            1,
            work.source_digest or "",
            SimpleNamespace(),
            chat_id="chat-1",
        )
    with pytest.raises(HTTPException) as text_error:
        await works_router.get_work_text_preview(
            work.id,
            SimpleNamespace(),
            chat_id="chat-1",
        )

    assert preview_error.value.status_code == 409
    assert page_error.value.status_code == 409
    assert text_error.value.status_code == 409


@pytest.mark.asyncio
async def test_preview_rechecks_permission_after_slow_render(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    source = workspace_dir / "deck.pdf"
    source.write_bytes(b"%PDF-1.7 fixture")
    work = add_work(
        workspace_dir,
        source_path=source,
        allowed_roots=[workspace_dir],
        source_chat_id="chat-1",
    )
    workspace = SimpleNamespace(workspace_dir=str(workspace_dir))
    render_started = threading.Event()
    release_render = threading.Event()

    async def _agent_for_request(_request):
        return workspace

    def _slow_preview(_workspace_dir, _work_id):
        render_started.set()
        assert release_render.wait(
            timeout=2,
        ), "preview render was not released"
        return SimpleNamespace(
            work_id=work.id,
            source_digest=work.source_digest,
            kind="pdf",
            renderer="fixture",
            renderer_version="fixture-v1",
            page_count=1,
            pages=(SimpleNamespace(number=1, width=1600, height=900),),
        )

    monkeypatch.setattr(
        works_router,
        "get_agent_for_request",
        _agent_for_request,
    )
    monkeypatch.setattr(
        works_router,
        "get_or_create_work_preview",
        _slow_preview,
    )

    managed_path = Path(work.path)
    task = asyncio.create_task(
        works_router.get_work_preview(
            work.id,
            SimpleNamespace(),
            chat_id="chat-1",
        ),
    )
    try:
        assert await asyncio.to_thread(render_started.wait, 2)
        managed_path.chmod(0)
        release_render.set()
        with pytest.raises(HTTPException) as raised:
            await task
        assert raised.value.status_code == 403
        assert raised.value.detail == "saved work copy cannot be read"
    finally:
        release_render.set()
        managed_path.chmod(0o600)


@pytest.mark.asyncio
async def test_text_preview_reads_only_the_managed_office_revision(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    source = workspace_dir / "report.docx"
    _write_ooxml_stub(source)
    work = add_work(
        workspace_dir,
        source_path=source,
        allowed_roots=[workspace_dir],
        source_chat_id="chat-1",
    )
    calls: list[tuple[str, str]] = []

    def _preview_office_document(file_path: str, *, mode: str):
        calls.append((file_path, mode))
        return SimpleNamespace(
            available=True,
            method="ooxml-fallback",
            mode=mode,
            content_type="text/markdown",
            content="# 周报\n\n这是受管版本正文。",
            message="",
        )

    workspace = SimpleNamespace(workspace_dir=str(workspace_dir))

    async def _agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        works_router,
        "get_agent_for_request",
        _agent_for_request,
    )
    monkeypatch.setattr(
        works_router,
        "preview_office_document",
        _preview_office_document,
        raising=False,
    )

    result = await works_router.get_work_text_preview(
        work.id,
        SimpleNamespace(),
        chat_id="chat-1",
    )

    assert result.work_id == work.id
    assert result.source_digest == work.source_digest
    assert result.kind == "document"
    assert result.content == "# 周报\n\n这是受管版本正文。"
    assert result.method == "ooxml-fallback"
    assert calls == [(work.path, "text")]


@pytest.mark.asyncio
async def test_text_preview_rejects_a_damaged_ooxml_package_before_parsing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    source = workspace_dir / "damaged.docx"
    source.write_bytes(b"this is not an OOXML package")
    work = add_work(
        workspace_dir,
        source_path=source,
        allowed_roots=[workspace_dir],
        source_chat_id="chat-1",
    )
    workspace = SimpleNamespace(workspace_dir=str(workspace_dir))

    async def _agent_for_request(_request):
        return workspace

    def _must_not_parse(*_args, **_kwargs):
        raise AssertionError("damaged Office content reached the parser")

    monkeypatch.setattr(
        works_router,
        "get_agent_for_request",
        _agent_for_request,
    )
    monkeypatch.setattr(
        works_router,
        "preview_office_document",
        _must_not_parse,
    )

    with pytest.raises(HTTPException) as error:
        await works_router.get_work_text_preview(
            work.id,
            SimpleNamespace(),
            chat_id="chat-1",
        )

    assert error.value.status_code == 415
    assert "damaged" in str(error.value.detail).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limit_name", "limit_value", "expected_detail"),
    [
        ("_MAX_OFFICE_PREVIEW_SOURCE_BYTES", 8, "source exceeds"),
        (
            "_MAX_OFFICE_PREVIEW_EXPANDED_BYTES",
            128,
            "expanded content exceeds",
        ),
        ("_MAX_OFFICE_PREVIEW_ARCHIVE_MEMBERS", 1, "too many entries"),
    ],
)
async def test_text_preview_rejects_ooxml_resource_limits_before_parsing(
    tmp_path: Path,
    monkeypatch,
    limit_name: str,
    limit_value: int,
    expected_detail: str,
) -> None:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    source = workspace_dir / "large.docx"
    _write_ooxml_stub(source, payload=b"x" * 1024)
    work = add_work(
        workspace_dir,
        source_path=source,
        allowed_roots=[workspace_dir],
        source_chat_id="chat-1",
    )
    workspace = SimpleNamespace(workspace_dir=str(workspace_dir))

    async def _agent_for_request(_request):
        return workspace

    def _must_not_parse(*_args, **_kwargs):
        raise AssertionError("oversized Office content reached the parser")

    monkeypatch.setattr(
        works_router,
        "get_agent_for_request",
        _agent_for_request,
    )
    monkeypatch.setattr(works_router, limit_name, limit_value, raising=False)
    monkeypatch.setattr(
        works_router,
        "preview_office_document",
        _must_not_parse,
    )

    with pytest.raises(HTTPException) as error:
        await works_router.get_work_text_preview(
            work.id,
            SimpleNamespace(),
            chat_id="chat-1",
        )

    assert error.value.status_code == 413
    assert expected_detail in str(error.value.detail).lower()


@pytest.mark.asyncio
async def test_create_work_edit_session_binds_an_independent_version_branch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    source = workspace_dir / "source" / "index.html"
    source.parent.mkdir()
    source.write_text("<title>Game</title>", encoding="utf-8")
    (source.parent / "game.js").write_text("play()", encoding="utf-8")
    work = add_work(
        workspace_dir,
        source_path=source,
        allowed_roots=[workspace_dir],
        title="迷你马里奥",
        source_chat_id="chat-source",
        source_run_id="run-source",
    )
    calls = {}

    class _ChatManager:
        async def get_chat(self, chat_id: str):
            assert chat_id == "chat-source"
            return SimpleNamespace(name="做一个小游戏")

        async def get_or_create_chat(
            self,
            session_id: str,
            user_id: str,
            channel: str,
            *,
            name: str,
        ) -> ChatSpec:
            calls["session_id"] = session_id
            return ChatSpec(
                name=name,
                session_id=session_id,
                user_id=user_id,
                channel=channel,
            )

        async def merge_chat_meta(self, _chat_id: str, updates: dict):
            calls["updates"] = updates
            return calls["chat"].model_copy(update={"meta": updates})

    manager = _ChatManager()
    workspace = SimpleNamespace(
        workspace_dir=str(workspace_dir),
        agent_id="default",
        chat_manager=manager,
    )

    async def _agent_for_request(_request):
        return workspace

    async def _ensure_topic(
        _workspace,
        _manager,
        chat,
        *,
        manual_workspace_dir,
        coding_mode,
        agent_id,
    ):
        calls["chat"] = chat
        calls["manual_workspace_dir"] = manual_workspace_dir
        calls["coding_mode"] = coding_mode
        calls["agent_id"] = agent_id
        return SimpleNamespace(chat=chat)

    monkeypatch.setattr(
        works_router,
        "get_agent_for_request",
        _agent_for_request,
    )
    monkeypatch.setattr(
        works_router,
        "ensure_chat_topic_workspace",
        _ensure_topic,
    )

    chat = await works_router.create_work_edit_session(
        work.id,
        SimpleNamespace(),
    )

    assert chat.name == "修改：迷你马里奥"
    assert calls["session_id"].startswith(f"console:work-edit:{work.id}:")
    edit_root = Path(calls["manual_workspace_dir"])
    assert edit_root != Path(work.path).parent
    assert (edit_root / "index.html").read_text(
        encoding="utf-8",
    ) == "<title>Game</title>"
    assert (edit_root / "game.js").read_text(encoding="utf-8") == "play()"
    assert Path(work.path).read_text(encoding="utf-8") == "<title>Game</title>"
    assert calls["coding_mode"] is True
    assert calls["updates"]["work_edit_id"] == work.id
    assert (
        str(edit_root / "index.html") in calls["updates"]["work_edit_context"]
    )
    assert calls["updates"]["work_edit_parent_version"] == 1
    assert calls["updates"]["work_edit_lineage_root_id"] == work.id
    assert calls["updates"]["work_edit_session"] == {
        "schema_version": 1,
        "work_id": work.id,
        "title": "迷你马里奥",
        "kind": "html",
        "source_version": 1,
    }


@pytest.mark.asyncio
async def test_content_route_distinguishes_a_modified_saved_version(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    source = workspace_dir / "report.txt"
    source.write_text("original", encoding="utf-8")
    work = add_work(
        workspace_dir,
        source_path=source,
        allowed_roots=[workspace_dir],
        title="报告",
    )
    Path(work.path).write_text("changed", encoding="utf-8")
    workspace = SimpleNamespace(workspace_dir=str(workspace_dir))

    async def _agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        works_router,
        "get_agent_for_request",
        _agent_for_request,
    )

    with pytest.raises(HTTPException) as raised:
        await works_router.get_work_content(work.id, SimpleNamespace())

    assert raised.value.status_code == 409
    assert raised.value.detail == "saved work version was modified"


@pytest.mark.asyncio
async def test_content_route_preserves_index_diagnostic_on_second_read_race(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    source = workspace_dir / "report.txt"
    source.write_text("original", encoding="utf-8")
    work = add_work(
        workspace_dir,
        source_path=source,
        allowed_roots=[workspace_dir],
    )
    workspace = SimpleNamespace(workspace_dir=str(workspace_dir))

    async def _agent_for_request(_request):
        return workspace

    def _changed_index(*_args, **_kwargs):
        raise works_store.WorksIndexUnavailable(
            "works_index_invalid_json",
            "injected second-read index failure",
        )

    monkeypatch.setattr(
        works_router,
        "get_agent_for_request",
        _agent_for_request,
    )
    monkeypatch.setattr(works_router, "get_managed_work_file", _changed_index)

    with pytest.raises(HTTPException) as raised:
        await works_router.get_work_content(work.id, SimpleNamespace())

    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "works_index_invalid_json"


@pytest.mark.asyncio
async def test_canonical_edit_copy_is_bound_to_source_chat_and_revision(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    source = workspace_dir / "deck.pptx"
    source.write_bytes(b"pptx fixture")
    work = add_work(
        workspace_dir,
        source_path=source,
        allowed_roots=[workspace_dir],
        source_chat_id="chat-1",
        source_run_id="run-1",
    )

    class _ChatManager:
        async def get_chat(self, chat_id: str):
            return SimpleNamespace(id=chat_id) if chat_id == "chat-1" else None

    workspace = SimpleNamespace(
        workspace_dir=str(workspace_dir),
        chat_manager=_ChatManager(),
    )

    async def _agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        works_router,
        "get_agent_for_request",
        _agent_for_request,
    )

    result = await works_router.create_canonical_work_edit_copy(
        work.id,
        works_router.WorkEditCopyRequest(chat_id="chat-1"),
        SimpleNamespace(),
    )

    assert result.work_id == work.id
    assert result.asset_id == work.artifact_id
    assert result.base_version == 1
    assert result.base_digest == work.source_digest
    assert Path(result.edit_path).read_bytes() == b"pptx fixture"
    assert Path(result.edit_path) != Path(work.path)


@pytest.mark.asyncio
async def test_canonical_edit_copy_rejects_a_different_chat(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    source = workspace_dir / "deck.pptx"
    source.write_bytes(b"pptx fixture")
    work = add_work(
        workspace_dir,
        source_path=source,
        allowed_roots=[workspace_dir],
        source_chat_id="chat-1",
    )

    class _ChatManager:
        async def get_chat(self, chat_id: str):
            return SimpleNamespace(id=chat_id)

    workspace = SimpleNamespace(
        workspace_dir=str(workspace_dir),
        chat_manager=_ChatManager(),
    )

    async def _agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        works_router,
        "get_agent_for_request",
        _agent_for_request,
    )

    with pytest.raises(HTTPException) as raised:
        await works_router.create_canonical_work_edit_copy(
            work.id,
            works_router.WorkEditCopyRequest(chat_id="chat-2"),
            SimpleNamespace(),
        )

    assert raised.value.status_code == 409


@pytest.mark.asyncio
async def test_canonical_edit_copy_rejects_work_without_chat_ownership(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    source = workspace_dir / "deck.pptx"
    source.write_bytes(b"pptx fixture")
    work = add_work(
        workspace_dir,
        source_path=source,
        allowed_roots=[workspace_dir],
    )

    class _ChatManager:
        async def get_chat(self, chat_id: str):
            return SimpleNamespace(id=chat_id)

    workspace = SimpleNamespace(
        workspace_dir=str(workspace_dir),
        chat_manager=_ChatManager(),
    )

    async def _agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        works_router,
        "get_agent_for_request",
        _agent_for_request,
    )

    with pytest.raises(HTTPException) as raised:
        await works_router.create_canonical_work_edit_copy(
            work.id,
            works_router.WorkEditCopyRequest(chat_id="chat-1"),
            SimpleNamespace(),
        )

    assert raised.value.status_code == 409
    assert "not an outcome" in raised.value.detail


@pytest.mark.asyncio
async def test_pdf_derivation_route_rejects_a_different_chat_before_conversion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    source = workspace_dir / "deck.pptx"
    source.write_bytes(b"pptx fixture")
    work = add_work(
        workspace_dir,
        source_path=source,
        allowed_roots=[workspace_dir],
        source_chat_id="chat-1",
    )
    workspace = SimpleNamespace(workspace_dir=str(workspace_dir))

    async def _agent_for_request(_request):
        return workspace

    def _unexpected_conversion(*_args, **_kwargs):
        raise AssertionError("conversion must not start for another chat")

    monkeypatch.setattr(
        works_router,
        "get_agent_for_request",
        _agent_for_request,
    )
    monkeypatch.setattr(
        works_router,
        "derive_work_to_pdf",
        _unexpected_conversion,
    )

    with pytest.raises(HTTPException) as raised:
        await works_router.create_work_derivation(
            work.id,
            works_router.WorkDerivationRequest(chat_id="chat-2"),
            SimpleNamespace(),
        )

    assert raised.value.status_code == 409


@pytest.mark.asyncio
async def test_pdf_derivation_route_reports_changed_output_as_conflict(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    source = workspace_dir / "deck.pptx"
    source.write_bytes(b"pptx fixture")
    work = add_work(
        workspace_dir,
        source_path=source,
        allowed_roots=[workspace_dir],
        source_chat_id="chat-1",
    )
    workspace = SimpleNamespace(workspace_dir=str(workspace_dir))

    async def _agent_for_request(_request):
        return workspace

    def _changed_output(*_args, **_kwargs):
        raise works_store.WorkDerivationConflict(
            "derived Work output changed during registration"
        )

    monkeypatch.setattr(
        works_router,
        "get_agent_for_request",
        _agent_for_request,
    )
    monkeypatch.setattr(
        works_router,
        "derive_work_to_pdf",
        _changed_output,
    )

    with pytest.raises(HTTPException) as raised:
        await works_router.create_work_derivation(
            work.id,
            works_router.WorkDerivationRequest(chat_id="chat-1"),
            SimpleNamespace(),
        )

    assert raised.value.status_code == 409
    assert "changed during registration" in raised.value.detail
