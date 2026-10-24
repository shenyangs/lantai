"""WP-05 durable remote task recovery contracts."""

from __future__ import annotations

import pytest

from lantai.runtime.effects import (
    RuntimeEffectJournal,
    RuntimeEffectReducerAction,
    reduce_runtime_effect,
)
from lantai.runtime.remote_tasks import (
    RemoteArtifactDownload,
    RemoteTaskJournal,
    RemoteTaskState,
    RuntimeRemoteTaskCoordinator,
    TokenDanceRemoteSubmissionObserver,
    prepare_remote_artifact_download,
    validate_remote_artifact_url,
)
from lantai.runtime.effect_context import RuntimeEffectContext
from lantai.providers.tokendance_authorization import TokenDanceAuthorization
from lantai.runtime.runs import RuntimeEffectRecord, RuntimeRunOwner


def _journal(tmp_path) -> RemoteTaskJournal:
    return RemoteTaskJournal(tmp_path)


def _running_effect(tmp_path) -> tuple[RuntimeEffectJournal, RuntimeEffectRecord]:
    journal = RuntimeEffectJournal(tmp_path)
    intent = RuntimeEffectRecord(
        run_id="run-1",
        attempt_id="attempt-1",
        step_id="step-1",
        effect_id="effect-1",
        source="chat",
        owner=RuntimeRunOwner(user_id="user-1", agent_id="default"),
        tool_name="tokendance.video.submit",
        args_hash="sha256:args",
        policy_snapshot_hash="sha256:policy",
        idempotency_key="remote:op-1",
        release_edition="official",
    )
    journal.append_effect(intent)
    running = reduce_runtime_effect(
        intent,
        RuntimeEffectReducerAction(type="start"),
    ).effect
    journal.append_effect(running)
    return journal, running


def _coordinator(tmp_path) -> tuple[RuntimeRemoteTaskCoordinator, RuntimeEffectJournal]:
    effects, _ = _running_effect(tmp_path)
    return (
        RuntimeRemoteTaskCoordinator(
            effect_journal=effects,
            remote_journal=RemoteTaskJournal(tmp_path),
        ),
        effects,
    )


def test_t059_remote_submission_requires_an_existing_running_runtime_effect(
    tmp_path,
) -> None:
    coordinator, effects = _coordinator(tmp_path)
    record = coordinator.prepare_submission(
        operation_id="op-1",
        run_id="run-1",
        attempt_id="attempt-1",
        effect_id="effect-1",
        owner_binding_id="binding-1",
        binding_revision=2,
        credential_ref="cred-1",
        model_snapshot_hash="model-1",
    )

    assert record.state is RemoteTaskState.PREPARED
    assert effects.latest_effect("run-1", "attempt-1", "effect-1").status == "running"


def test_t060_begin_submission_closes_the_post_before_task_id_crash_window(
    tmp_path,
) -> None:
    coordinator, effects = _coordinator(tmp_path)
    prepared = coordinator.prepare_submission(
        operation_id="op-1",
        run_id="run-1",
        attempt_id="attempt-1",
        effect_id="effect-1",
        owner_binding_id="binding-1",
        binding_revision=2,
        credential_ref="cred-1",
        model_snapshot_hash="model-1",
    )

    # The caller must persist this fence before issuing the network request.
    begun = coordinator.begin_submission(
        "op-1", expected_revision=prepared.revision
    )

    assert begun.state is RemoteTaskState.SUBMISSION_UNKNOWN
    assert effects.latest_effect("run-1", "attempt-1", "effect-1").status == "running"
    with pytest.raises(ValueError, match="cannot transition"):
        coordinator.begin_submission(
            "op-1", expected_revision=begun.revision
        )

    # A response received in the same process may still persist the task id.
    submitted = coordinator.record_submitted(
        operation_id="op-1",
        expected_revision=begun.revision,
        remote_task_id="task-1",
    )
    assert submitted.state is RemoteTaskState.SUBMITTED
    assert submitted.remote_task_id == "task-1"


def test_tokendance_observer_binds_effect_authorization_and_model_snapshot(
    tmp_path,
) -> None:
    coordinator, effects = _coordinator(tmp_path)
    authorization = TokenDanceAuthorization(
        owner_subject_id="watcha-user-1",
        account_epoch=3,
        credential_ref="cred-1",
        payment_binding_id="payment-1",
        binding_revision=2,
        use_kind="model",
        api_key="secret",
    )
    verified = []
    observer = TokenDanceRemoteSubmissionObserver.create(
        coordinator=coordinator,
        context=RuntimeEffectContext(
            run_id="run-1",
            attempt_id="attempt-1",
            effect_id="effect-1",
            state_workspace_dir=str(tmp_path),
        ),
        authorization=authorization,
        model_id="seedance-2.5",
        protocol="seedance:generations",
        prompt="private prompt",
        request_options={"duration": 5, "aspect_ratio": "16:9"},
        assert_authorization_current=lambda: verified.append(True),
    )

    observer.before_submit()
    observer.response_received()
    observer.submitted("remote-task-1")
    observer.response_received()
    observer.succeeded("sha256:provider-result")

    rows = coordinator.remote_journal.records_for(observer.operation_id)
    assert rows[-1].state is RemoteTaskState.SUCCEEDED
    assert rows[-1].remote_task_id == "remote-task-1"
    assert rows[-1].owner_binding_id == authorization.context_id
    assert rows[-1].binding_revision == 2
    assert rows[-1].credential_ref == "cred-1"
    assert "private prompt" not in coordinator.remote_journal.path.read_text(
        encoding="utf-8"
    )
    assert verified == [True, True, True]
    assert effects.latest_effect("run-1", "attempt-1", "effect-1").status == "running"


def test_tokendance_observer_leaves_uncertain_submission_for_reconciliation(
    tmp_path,
) -> None:
    coordinator, _effects = _coordinator(tmp_path)
    observer = TokenDanceRemoteSubmissionObserver.create(
        coordinator=coordinator,
        context=RuntimeEffectContext(
            run_id="run-1",
            attempt_id="attempt-1",
            effect_id="effect-1",
            state_workspace_dir=str(tmp_path),
        ),
        authorization=TokenDanceAuthorization(
            owner_subject_id="watcha-user-1",
            account_epoch=3,
            credential_ref="cred-1",
            payment_binding_id="payment-1",
            binding_revision=2,
            use_kind="model",
            api_key="secret",
        ),
        model_id="seedance-2.5",
        protocol="seedance:generations",
        prompt="private prompt",
        request_options={"duration": 5},
        assert_authorization_current=lambda: None,
    )

    observer.before_submit()
    observer.submission_unknown()

    record = coordinator.remote_journal.records_for(observer.operation_id)[-1]
    assert record.state is RemoteTaskState.SUBMISSION_UNKNOWN
    with pytest.raises(ValueError, match="cannot transition"):
        observer.before_submit()


def test_t061_ambiguous_remote_submit_marks_runtime_effect_and_never_replays(
    tmp_path,
) -> None:
    coordinator, effects = _coordinator(tmp_path)
    record = coordinator.prepare_submission(
        operation_id="op-1",
        run_id="run-1",
        attempt_id="attempt-1",
        effect_id="effect-1",
        owner_binding_id="binding-1",
        binding_revision=2,
        credential_ref="cred-1",
        model_snapshot_hash="model-1",
    )

    coordinator.mark_submission_unknown(
        "op-1",
        expected_revision=record.revision,
        reason="upstream accepted request but response was lost",
    )

    assert effects.latest_effect("run-1", "attempt-1", "effect-1").status == (
        "ambiguous"
    )
    with pytest.raises(ValueError, match="must not be resubmitted"):
        coordinator.begin_submission(
            "op-1", expected_revision=record.revision + 1
        )


def test_t063_t070_remote_success_commits_the_same_effect_once(tmp_path) -> None:
    coordinator, effects = _coordinator(tmp_path)
    prepared = coordinator.prepare_submission(
        operation_id="op-1",
        run_id="run-1",
        attempt_id="attempt-1",
        effect_id="effect-1",
        owner_binding_id="binding-1",
        binding_revision=2,
        credential_ref="cred-1",
        model_snapshot_hash="model-1",
    )
    begun = coordinator.begin_submission(
        "op-1", expected_revision=prepared.revision
    )
    submitted = coordinator.record_submitted(
        operation_id="op-1",
        expected_revision=begun.revision,
        remote_task_id="task-1",
    )
    succeeded = coordinator.record_succeeded(
        operation_id="op-1",
        expected_revision=submitted.revision,
        result_hash="sha256:video",
    )

    assert succeeded.state is RemoteTaskState.SUCCEEDED
    effect = effects.latest_effect("run-1", "attempt-1", "effect-1")
    assert effect.status == "committed"
    assert effect.result_hash == "sha256:video"

    repeated = coordinator.record_succeeded(
        operation_id="op-1",
        expected_revision=succeeded.revision,
        result_hash="sha256:video",
    )
    assert repeated == succeeded
    assert len(effects.list_effects("run-1", "attempt-1")) == 3


def test_t061_t063_late_task_id_and_success_reconcile_ambiguous_effect(
    tmp_path,
) -> None:
    coordinator, effects = _coordinator(tmp_path)
    prepared = coordinator.prepare_submission(
        operation_id="op-1",
        run_id="run-1",
        attempt_id="attempt-1",
        effect_id="effect-1",
        owner_binding_id="binding-1",
        binding_revision=2,
        credential_ref="cred-1",
        model_snapshot_hash="model-1",
    )
    begun = coordinator.begin_submission(
        "op-1", expected_revision=prepared.revision
    )
    coordinator.mark_submission_unknown(
        "op-1",
        expected_revision=begun.revision,
        reason="response was lost after POST",
    )

    submitted = coordinator.record_submitted(
        operation_id="op-1",
        expected_revision=begun.revision,
        remote_task_id="task-late",
    )
    succeeded = coordinator.record_succeeded(
        operation_id="op-1",
        expected_revision=submitted.revision,
        result_hash="sha256:verified-video",
    )

    assert succeeded.state is RemoteTaskState.SUCCEEDED
    effect = effects.latest_effect("run-1", "attempt-1", "effect-1")
    assert effect.status == "probed_committed"
    assert effect.result_hash == "sha256:verified-video"


def test_t065_verified_remote_failure_reconciles_ambiguous_submission(
    tmp_path,
) -> None:
    coordinator, effects = _coordinator(tmp_path)
    prepared = coordinator.prepare_submission(
        operation_id="op-1",
        run_id="run-1",
        attempt_id="attempt-1",
        effect_id="effect-1",
        owner_binding_id="binding-1",
        binding_revision=2,
        credential_ref="cred-1",
        model_snapshot_hash="model-1",
    )
    begun = coordinator.begin_submission(
        "op-1", expected_revision=prepared.revision
    )
    coordinator.mark_submission_unknown(
        "op-1",
        expected_revision=begun.revision,
        reason="response was lost after POST",
    )
    submitted = coordinator.record_submitted(
        operation_id="op-1",
        expected_revision=begun.revision,
        remote_task_id="task-late",
    )

    failed = coordinator.record_failed(
        operation_id="op-1",
        expected_revision=submitted.revision,
        failure_hash="sha256:verified-provider-failure",
    )

    assert failed.state is RemoteTaskState.FAILED
    assert failed.result_hash == "sha256:verified-provider-failure"
    effect = effects.latest_effect("run-1", "attempt-1", "effect-1")
    assert effect.status == "probed_committed"
    assert effect.result_hash == "sha256:verified-provider-failure"


def test_t059_submission_and_polling_reuse_one_remote_task(tmp_path) -> None:
    journal = _journal(tmp_path)
    record = journal.prepare_submission(
        operation_id="op-1",
        run_id="run-1",
        attempt_id="attempt-1",
        effect_id="effect-1",
        owner_binding_id="binding-1",
        binding_revision=2,
        credential_ref="cred-1",
        model_snapshot_hash="model-1",
    )
    record = journal.begin_submission(
        "op-1", expected_revision=record.revision
    )
    record = journal.record_submitted(
        operation_id="op-1",
        expected_revision=record.revision,
        remote_task_id="task-1",
    )

    assert journal.poll_reference("op-1").remote_task_id == "task-1"
    assert journal.poll_reference("op-1").remote_task_id == "task-1"
    assert journal.records_for("op-1")[-1].remote_task_id == "task-1"


def test_t060_t061_crash_or_ambiguous_submit_never_resubmits(tmp_path) -> None:
    journal = _journal(tmp_path)
    initial = journal.prepare_submission(
        operation_id="op-1",
        run_id="run-1",
        attempt_id="attempt-1",
        effect_id="effect-1",
        owner_binding_id="binding-1",
        binding_revision=2,
        credential_ref="cred-1",
        model_snapshot_hash="model-1",
    )
    ambiguous = journal.begin_submission(
        "op-1", expected_revision=initial.revision
    )

    assert ambiguous.state is RemoteTaskState.SUBMISSION_UNKNOWN
    with pytest.raises(ValueError, match="cannot transition"):
        journal.begin_submission(
            "op-1", expected_revision=ambiguous.revision
        )


def test_t062_task_id_received_but_not_persisted_stays_unknown_after_restart(
    tmp_path,
) -> None:
    journal = _journal(tmp_path)
    initial = journal.prepare_submission(
        operation_id="op-1",
        run_id="run-1",
        attempt_id="attempt-1",
        effect_id="effect-1",
        owner_binding_id="binding-1",
        binding_revision=2,
        credential_ref="cred-1",
        model_snapshot_hash="model-1",
    )
    begun = journal.begin_submission(
        "op-1", expected_revision=initial.revision
    )

    # Simulate process death after the provider returned a task id but before
    # record_submitted could durably append it.  A fresh process sees only the
    # no-replay fence and must reconcile upstream rather than infer a retry.
    restarted = _journal(tmp_path)
    recovered = restarted.records_for("op-1")[-1]

    assert recovered == begun
    assert recovered.state is RemoteTaskState.SUBMISSION_UNKNOWN
    assert recovered.remote_task_id is None
    with pytest.raises(ValueError, match="not known"):
        restarted.poll_reference("op-1")
    with pytest.raises(ValueError, match="cannot transition"):
        restarted.begin_submission(
            "op-1", expected_revision=recovered.revision
        )

    # If an authoritative probe later supplies the original id, it is bound
    # to this operation and becomes the sole polling reference.
    reconciled = restarted.record_submitted(
        operation_id="op-1",
        expected_revision=recovered.revision,
        remote_task_id="task-returned-before-kill",
    )
    assert restarted.poll_reference("op-1") == reconciled


def test_t063_t066_recovery_checks_original_task_and_auth_fence(tmp_path) -> None:
    journal = _journal(tmp_path)
    initial = journal.prepare_submission(
        operation_id="op-1",
        run_id="run-1",
        attempt_id="attempt-1",
        effect_id="effect-1",
        owner_binding_id="binding-1",
        binding_revision=2,
        credential_ref="cred-1",
        model_snapshot_hash="model-1",
    )
    begun = journal.begin_submission(
        "op-1", expected_revision=initial.revision
    )
    journal.record_submitted(
        operation_id="op-1",
        expected_revision=begun.revision,
        remote_task_id="task-1",
    )

    with pytest.raises(ValueError, match="authorization changed"):
        journal.claim_recovery(
            "op-1",
            worker_id="worker-1",
            owner_binding_id="binding-2",
            binding_revision=3,
            credential_ref="cred-2",
        )

    assert journal.poll_reference("op-1").remote_task_id == "task-1"


def test_t064_only_one_recovery_worker_can_claim(tmp_path) -> None:
    journal = _journal(tmp_path)
    initial = journal.prepare_submission(
        operation_id="op-1",
        run_id="run-1",
        attempt_id="attempt-1",
        effect_id="effect-1",
        owner_binding_id="binding-1",
        binding_revision=2,
        credential_ref="cred-1",
        model_snapshot_hash="model-1",
    )
    begun = journal.begin_submission(
        "op-1", expected_revision=initial.revision
    )
    journal.record_submitted(
        operation_id="op-1",
        expected_revision=begun.revision,
        remote_task_id="task-1",
    )

    journal.claim_recovery(
        "op-1",
        worker_id="worker-1",
        owner_binding_id="binding-1",
        binding_revision=2,
        credential_ref="cred-1",
    )
    with pytest.raises(ValueError, match="already claimed"):
        journal.claim_recovery(
            "op-1",
            worker_id="worker-2",
            owner_binding_id="binding-1",
            binding_revision=2,
            credential_ref="cred-1",
        )


def test_t067_local_cancel_does_not_invent_remote_cancel(tmp_path) -> None:
    journal = _journal(tmp_path)
    initial = journal.prepare_submission(
        operation_id="op-1",
        run_id="run-1",
        attempt_id="attempt-1",
        effect_id="effect-1",
        owner_binding_id="binding-1",
        binding_revision=2,
        credential_ref="cred-1",
        model_snapshot_hash="model-1",
    )
    begun = journal.begin_submission(
        "op-1", expected_revision=initial.revision
    )
    submitted = journal.record_submitted(
        operation_id="op-1",
        expected_revision=begun.revision,
        remote_task_id="task-1",
    )
    cancelled = journal.record_local_cancel(
        "op-1", expected_revision=submitted.revision
    )

    assert cancelled.local_cancel_requested is True
    assert cancelled.state is RemoteTaskState.SUBMITTED


def test_t068_successful_remote_generation_survives_download_failure(
    tmp_path,
) -> None:
    journal = _journal(tmp_path)
    initial = journal.prepare_submission(
        operation_id="op-1",
        run_id="run-1",
        attempt_id="attempt-1",
        effect_id="effect-1",
        owner_binding_id="binding-1",
        binding_revision=2,
        credential_ref="cred-1",
        model_snapshot_hash="model-1",
    )
    begun = journal.begin_submission(
        "op-1", expected_revision=initial.revision
    )
    submitted = journal.record_submitted(
        operation_id="op-1",
        expected_revision=begun.revision,
        remote_task_id="task-1",
    )
    succeeded = journal.record_succeeded(
        operation_id="op-1",
        expected_revision=submitted.revision,
        result_hash="sha256:provider-result",
    )

    # An expired/malicious download URL fails admission, but the provider
    # success and original task id remain durable for a later URL refresh.
    with pytest.raises(ValueError, match="host is not allowed"):
        prepare_remote_artifact_download(
            "https://expired.example/result.mp4",
            allowed_hosts={"cdn.tokendance.space"},
            allowed_content_types={"video/mp4"},
            content_type="video/mp4",
            content_length=1_024,
            max_bytes=2_048,
            destination_root=tmp_path / "artifacts",
            relative_path="run-1/result.mp4",
        )

    restarted = _journal(tmp_path)
    assert restarted.poll_reference("op-1") == succeeded
    with pytest.raises(ValueError, match="cannot transition"):
        restarted.begin_submission(
            "op-1", expected_revision=succeeded.revision
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.tokendance.space/a.mp4",
        "https://127.0.0.1/a.mp4",
        "https://example.com/a.mp4",
        "file:///tmp/a.mp4",
        "https://user:secret@cdn.tokendance.space/a.mp4",
    ],
)
def test_t071_remote_artifact_url_rejects_ssrf_and_credentials(url) -> None:
    with pytest.raises(ValueError):
        validate_remote_artifact_url(
            url,
            allowed_hosts={"cdn.tokendance.space"},
        )


def test_t071_remote_artifact_url_accepts_exact_https_host() -> None:
    assert (
        validate_remote_artifact_url(
            "https://cdn.tokendance.space/a.mp4",
            allowed_hosts={"cdn.tokendance.space"},
        )
        == "https://cdn.tokendance.space/a.mp4"
    )


def test_t071_remote_artifact_download_has_type_size_path_and_key_fences(
    tmp_path,
) -> None:
    request = prepare_remote_artifact_download(
        "https://cdn.tokendance.space/result.mp4",
        allowed_hosts={"cdn.tokendance.space"},
        allowed_content_types={"video/mp4"},
        content_type="video/mp4",
        content_length=1_024,
        max_bytes=2_048,
        destination_root=tmp_path / "artifacts",
        relative_path="run-1/result.mp4",
    )

    assert isinstance(request, RemoteArtifactDownload)
    assert request.destination.is_relative_to((tmp_path / "artifacts").resolve())
    assert request.forward_headers == {}


@pytest.mark.parametrize(
    ("content_type", "content_length", "relative_path"),
    [
        ("application/x-executable", 1_024, "run-1/result.mp4"),
        ("video/mp4", 2_049, "run-1/result.mp4"),
        ("video/mp4", 1_024, "../outside.mp4"),
    ],
)
def test_t071_remote_artifact_download_rejects_malicious_metadata(
    tmp_path,
    content_type: str,
    content_length: int,
    relative_path: str,
) -> None:
    with pytest.raises(ValueError):
        prepare_remote_artifact_download(
            "https://cdn.tokendance.space/result.mp4",
            allowed_hosts={"cdn.tokendance.space"},
            allowed_content_types={"video/mp4"},
            content_type=content_type,
            content_length=content_length,
            max_bytes=2_048,
            destination_root=tmp_path / "artifacts",
            relative_path=relative_path,
        )
