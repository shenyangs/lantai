# -*- coding: utf-8 -*-
"""Behavior contracts for persistent Runtime Inbox admission."""

import hashlib
from pathlib import Path

import pytest

import lantai.runtime.inbox_admission as inbox_admission_module
from lantai.runtime.coherence.contracts import ExecutionFence
from lantai.runtime.continuation import continuation_input_refs
from lantai.runtime.inbox_admission import RuntimeInboxAdmission
from lantai.runtime.messages import ContentBlock, RuntimeMessage
from lantai.runtime.turn_control import (
    RunInboxMessageMetadata,
    RuntimeTurnControlStore,
    control_input_grant_ref,
)


def test_claimed_input_settles_only_after_model_surface_is_observed(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    fence = ExecutionFence(
        attempt_id="attempt-1",
        generation=2,
        expected_revision=7,
        lease_owner="worker-1",
    )
    store.append_steer(
        client_turn_id="client-2",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        text="please adjust",
        fence=fence,
    )
    admission = RuntimeInboxAdmission(
        store=store,
        run_id="run-1",
        runtime_metadata={
            "attempt_id": "attempt-1",
            "runtime_generation": 2,
            "runtime_revision": 7,
            "runtime_lease_owner": "worker-1",
        },
    )

    outcome = admission.claim_next(after_seq=0)

    assert outcome.conflict is None
    assert outcome.next_after_seq == 1
    assert outcome.claim is not None
    assert outcome.claim.message.text_content() == "please adjust"
    assert store.current_dispositions("run-1")[1].status == "claimed"

    admission.settle_consumed(outcome.claim)

    assert store.current_dispositions("run-1")[1].status == "consumed"
    assert not admission.has_pending(after_seq=0)


def test_foreign_attempt_is_rejected_and_cursor_advances(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    store.append_steer(
        client_turn_id="client-old",
        run_id="run-1",
        attempt_id="attempt-old",
        chat_id="chat-1",
        text="stale correction",
        fence=ExecutionFence(attempt_id="attempt-old", generation=1),
    )
    admission = RuntimeInboxAdmission(
        store=store,
        run_id="run-1",
        runtime_metadata={
            "attempt_id": "attempt-new",
            "runtime_generation": 2,
        },
    )

    outcome = admission.claim_next(after_seq=0)

    assert outcome.claim is None
    assert outcome.conflict is not None
    assert outcome.conflict.client_turn_id == "client-old"
    assert outcome.next_after_seq == 1
    assert not store.current_dispositions("run-1")


def test_claimed_message_carries_exact_artifact_target_to_model_boundary(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    store.append_steer(
        client_turn_id="client-target",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        text="修改第三页",
        message_metadata=RunInboxMessageMetadata.model_validate(
            {
                "artifact_edit_target": {
                    "schema_version": 1,
                    "chat_id": "chat-1",
                    "asset_id": "asset-1",
                    "work_id": "work-1",
                    "edit_path": "/managed/edit/deck.pptx",
                    "base_revision": {
                        "version": 2,
                        "digest": "sha256:base",
                    },
                    "selection": {"kind": "page", "page": 3},
                },
                "work_edit_context": "Use the managed edit branch.",
            }
        ),
    )
    admission = RuntimeInboxAdmission(
        store=store,
        run_id="run-1",
        runtime_metadata={"attempt_id": "attempt-1"},
    )

    outcome = admission.claim_next(after_seq=0)

    assert outcome.claim is not None
    metadata = outcome.claim.message.metadata
    assert metadata["artifact_edit_target"]["selection"] == {
        "kind": "page",
        "page": 3,
    }
    assert metadata["artifact_edit_target"]["base_revision"] == {
        "version": 2,
        "digest": "sha256:base",
    }
    assert metadata["work_edit_context"] == "Use the managed edit branch."


def test_claimed_structured_input_preserves_image_without_text_placeholder(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    store.append_input(
        command_id="image-1",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        intent="supplement",
        message=RuntimeMessage(
            role="user",
            content=[
                ContentBlock(
                    type="image",
                    data={
                        "attachment_id": "upload-1",
                        "version": 1,
                        "digest": "sha256:image-v1",
                        "grant_ref": "grant-1",
                    },
                ),
            ],
        ),
    )
    admission = RuntimeInboxAdmission(
        store=store,
        run_id="run-1",
        runtime_metadata={"attempt_id": "attempt-1"},
    )

    outcome = admission.claim_next(after_seq=0)

    assert outcome.claim is not None
    assert outcome.claim.message.text_content() == ""
    assert [block.type for block in outcome.claim.message.content] == ["image"]
    assert outcome.claim.message.content[0].data["attachment_id"] == "upload-1"


def test_changed_attachment_snapshot_is_rejected_before_model_binding(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    attachment = tmp_path / "guidance.png"
    original = b"original-image-bytes"
    attachment.write_bytes(original)
    digest = f"sha256:{hashlib.sha256(original).hexdigest()}"
    store.append_input(
        command_id="image-conflict",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        intent="supplement",
        message=RuntimeMessage(
            role="user",
            content=[
                ContentBlock(
                    type="image",
                    data={
                        "source": {"type": "url", "url": str(attachment)},
                        "version": 1,
                        "digest": digest,
                        "grant_ref": control_input_grant_ref(
                            chat_id="chat-1",
                            run_id="run-1",
                            canonical_path=str(attachment.resolve()),
                            digest=digest,
                        ),
                    },
                ),
            ],
        ),
    )
    attachment.write_bytes(b"replacement-image-bytes")
    admission = RuntimeInboxAdmission(
        store=store,
        run_id="run-1",
        runtime_metadata={"attempt_id": "attempt-1"},
    )

    outcome = admission.claim_next(after_seq=0)

    assert outcome.claim is None
    assert outcome.conflict is not None
    assert outcome.conflict.kind == "run_inbox_attachment_conflict"
    assert "digest" in outcome.conflict.reason
    disposition = store.current_dispositions("run-1")[1]
    assert disposition.status == "rejected"
    assert "digest" in disposition.reason


def test_foreign_attachment_grant_is_rejected_before_model_binding(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    attachment = tmp_path / "guidance.png"
    content = b"immutable-image-bytes"
    attachment.write_bytes(content)
    store.append_input(
        command_id="image-foreign-grant",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        intent="supplement",
        message=RuntimeMessage(
            role="user",
            content=[
                ContentBlock(
                    type="image",
                    data={
                        "source": {"type": "url", "url": str(attachment)},
                        "version": 1,
                        "digest": (
                            "sha256:" f"{hashlib.sha256(content).hexdigest()}"
                        ),
                        "grant_ref": ("file-grant:sha256:" f"{'0' * 64}"),
                    },
                ),
            ],
        ),
    )
    admission = RuntimeInboxAdmission(
        store=store,
        run_id="run-1",
        runtime_metadata={"attempt_id": "attempt-1"},
    )

    outcome = admission.claim_next(after_seq=0)

    assert outcome.claim is None
    assert outcome.conflict is not None
    assert outcome.conflict.kind == "run_inbox_attachment_conflict"
    assert "grant" in outcome.conflict.reason
    disposition = store.current_dispositions("run-1")[1]
    assert disposition.status == "rejected"
    assert "grant" in disposition.reason


def test_attachment_version_is_revalidated_before_model_binding(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    attachment = (tmp_path / "guidance.png").resolve()
    content = b"immutable-image-bytes"
    attachment.write_bytes(content)
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    store.append_input(
        command_id="image-invalid-version",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        intent="supplement",
        message=RuntimeMessage(
            role="user",
            content=[
                ContentBlock(
                    type="image",
                    data={
                        "source": {"type": "url", "url": str(attachment)},
                        "version": 0,
                        "digest": digest,
                        "grant_ref": control_input_grant_ref(
                            chat_id="chat-1",
                            run_id="run-1",
                            canonical_path=str(attachment),
                            digest=digest,
                        ),
                    },
                ),
            ],
        ),
    )
    admission = RuntimeInboxAdmission(
        store=store,
        run_id="run-1",
        runtime_metadata={"attempt_id": "attempt-1"},
    )

    outcome = admission.claim_next(after_seq=0)

    assert outcome.claim is None
    assert outcome.conflict is not None
    assert outcome.conflict.kind == "run_inbox_attachment_conflict"
    assert "version" in outcome.conflict.reason


def test_bound_attachment_snapshot_reaches_model_message(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    attachment = (tmp_path / "guidance.png").resolve()
    content = b"immutable-image-bytes"
    attachment.write_bytes(content)
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    store.append_input(
        command_id="image-bound",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        intent="supplement",
        message=RuntimeMessage(
            role="user",
            content=[
                ContentBlock(
                    type="image",
                    data={
                        "source": {"type": "url", "url": str(attachment)},
                        "version": 1,
                        "digest": digest,
                        "grant_ref": control_input_grant_ref(
                            chat_id="chat-1",
                            run_id="run-1",
                            canonical_path=str(attachment),
                            digest=digest,
                        ),
                    },
                ),
            ],
        ),
    )
    admission = RuntimeInboxAdmission(
        store=store,
        run_id="run-1",
        runtime_metadata={"attempt_id": "attempt-1"},
    )

    outcome = admission.claim_next(after_seq=0)

    assert outcome.conflict is None
    assert outcome.claim is not None
    assert outcome.claim.message.content[0].data["digest"] == digest


def test_expired_attachment_grant_is_blocked_before_model_binding(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    attachment = (tmp_path / "expired-guidance.png").resolve()
    content = b"immutable-image-bytes"
    attachment.write_bytes(content)
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    store.append_input(
        command_id="image-expired",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        intent="supplement",
        message=RuntimeMessage(
            role="user",
            content=[
                ContentBlock(
                    type="image",
                    data={
                        "source": {"type": "url", "url": str(attachment)},
                        "version": 1,
                        "digest": digest,
                        "grant_ref": control_input_grant_ref(
                            chat_id="chat-1",
                            run_id="run-1",
                            canonical_path=str(attachment),
                            digest=digest,
                        ),
                        "grant_issued_at": 1.0,
                        "grant_expires_at": 2.0,
                    },
                ),
            ],
        ),
    )
    admission = RuntimeInboxAdmission(
        store=store,
        run_id="run-1",
        runtime_metadata={"attempt_id": "attempt-1"},
    )

    outcome = admission.claim_next(after_seq=0)

    assert outcome.claim is None
    assert outcome.conflict is not None
    assert outcome.conflict.kind == "run_inbox_attachment_grant_expired"
    assert "expired" in outcome.conflict.reason
    receipt = store.get_input_receipt(
        chat_id="chat-1",
        command_id="image-expired",
    )
    assert receipt is not None
    assert receipt.execution_status == "not_applied"
    assert receipt.disposition_status == "expired"


def test_revoked_attachment_grant_stays_blocked_after_store_restart(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    attachment = (tmp_path / "revoked-guidance.png").resolve()
    content = b"immutable-image-bytes"
    attachment.write_bytes(content)
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    grant_ref = control_input_grant_ref(
        chat_id="chat-1",
        run_id="run-1",
        canonical_path=str(attachment),
        digest=digest,
    )
    store.append_input(
        command_id="image-revoked",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        intent="supplement",
        message=RuntimeMessage(
            role="user",
            content=[
                ContentBlock(
                    type="image",
                    data={
                        "source": {"type": "url", "url": str(attachment)},
                        "version": 1,
                        "digest": digest,
                        "grant_ref": grant_ref,
                        "grant_issued_at": 1.0,
                        "grant_expires_at": 4_000_000_000.0,
                    },
                ),
            ],
        ),
    )

    receipt = store.revoke_attachment_grant(
        chat_id="chat-1",
        command_id="image-revoked",
        grant_ref=grant_ref,
        actor_id="chat:chat-1",
    )
    restarted = RuntimeTurnControlStore(tmp_path)
    admission = RuntimeInboxAdmission(
        store=restarted,
        run_id="run-1",
        runtime_metadata={"attempt_id": "attempt-1"},
    )

    assert receipt.execution_status == "not_applied"
    assert receipt.disposition_status == "rejected"
    assert "revoked" in receipt.disposition_reason
    assert restarted.attachment_grant_is_revoked(
        chat_id="chat-1",
        command_id="image-revoked",
        grant_ref=grant_ref,
    )
    assert admission.claim_next(after_seq=0).claim is None


def test_immediate_redirect_wakes_past_pending_boundary_supplement(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    store.append_input(
        command_id="supplement-first",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        intent="supplement",
        message=RuntimeMessage.text("user", "先补充这一点"),
        wakeup="model_boundary",
    )
    store.append_input(
        command_id="redirect-now",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        intent="redirect",
        message=RuntimeMessage.text("user", "立即换方向"),
        wakeup="immediate",
    )
    admission = RuntimeInboxAdmission(
        store=store,
        run_id="run-1",
        runtime_metadata={"attempt_id": "attempt-1"},
    )

    wakeup = admission.peek_immediate_redirect(after_seq=0)

    assert wakeup is not None
    assert wakeup.command_id == "redirect-now"
    assert not store.current_dispositions("run-1")


def test_immediate_redirect_with_foreign_fence_cannot_wake_owner(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    store.append_input(
        command_id="foreign-redirect",
        run_id="run-1",
        attempt_id="attempt-old",
        chat_id="chat-1",
        intent="redirect",
        message=RuntimeMessage.text("user", "旧 attempt 改向"),
        wakeup="immediate",
        fence=ExecutionFence(attempt_id="attempt-old", generation=1),
    )
    admission = RuntimeInboxAdmission(
        store=store,
        run_id="run-1",
        runtime_metadata={
            "attempt_id": "attempt-new",
            "runtime_generation": 2,
        },
    )

    assert admission.peek_immediate_redirect(after_seq=0) is None
    assert not store.current_dispositions("run-1")


def test_current_run_owner_skips_queued_next_task_input(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    queued_receipt, queued_item = store.append_input(
        command_id="queue-next-1",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        intent="queue_next",
        message=RuntimeMessage.text("user", "下一项独立任务"),
    )
    store.append_input(
        command_id="supplement-current-1",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        intent="supplement",
        message=RuntimeMessage.text("user", "当前任务再补充一点"),
    )
    admission = RuntimeInboxAdmission(
        store=store,
        run_id="run-1",
        runtime_metadata={"attempt_id": "attempt-1"},
    )

    outcome = admission.claim_next(after_seq=0)

    assert outcome.conflict is None
    assert outcome.claim is not None
    assert outcome.next_after_seq == 2
    assert outcome.claim.client_turn_id == "supplement-current-1"
    assert outcome.claim.message.text_content() == "当前任务再补充一点"
    dispositions = store.current_dispositions("run-1")
    assert queued_item.seq not in dispositions
    assert dispositions[outcome.claim.item_seq].status == "claimed"

    admission.settle_consumed(outcome.claim)

    assert not admission.has_pending(after_seq=0)
    queued_status = store.get_input_receipt(
        chat_id="chat-1",
        command_id=queued_receipt.command_id,
    )
    assert queued_status is not None
    assert queued_status.execution_status == "pending"
    assert queued_status.next_waiting_reason == "awaiting_next_turn"


def test_consumed_image_source_recovers_from_canonical_inbox_without_replay(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    attachment = (tmp_path / "recoverable-guidance.png").resolve()
    content = b"recoverable-image-bytes"
    attachment.write_bytes(content)
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    store.append_input(
        command_id="image-recoverable",
        client_turn_id="client-image-recoverable",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        intent="supplement",
        message=RuntimeMessage(
            role="user",
            content=[
                ContentBlock(
                    type="image",
                    data={
                        "source": {"type": "url", "url": str(attachment)},
                        "version": 1,
                        "digest": digest,
                        "grant_ref": control_input_grant_ref(
                            chat_id="chat-1",
                            run_id="run-1",
                            canonical_path=str(attachment),
                            digest=digest,
                        ),
                    },
                ),
            ],
        ),
    )
    admission = RuntimeInboxAdmission(
        store=store,
        run_id="run-1",
        runtime_metadata={"attempt_id": "attempt-1"},
    )
    outcome = admission.claim_next(after_seq=0)
    assert outcome.claim is not None
    admission.settle_consumed(outcome.claim)
    refs = continuation_input_refs((outcome.claim.message,))

    recovered, conflict = admission.recover_continuation_sources(refs)

    assert conflict is None
    assert recovered == (outcome.claim.message,)
    assert recovered[0].text_content() == ""
    assert [block.type for block in recovered[0].content] == ["image"]
    assert len(store.read_inbox("run-1")) == 1
    assert store.current_dispositions("run-1")[1].status == "consumed"
    assert admission.validate_continuation_sources(recovered) is None


def test_immediate_redirect_supersession_keeps_attachment_source_available(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    attachment = (tmp_path / "superseded-guidance.png").resolve()
    content = b"superseded-but-still-authoritative-image"
    attachment.write_bytes(content)
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    store.append_input(
        command_id="image-superseded",
        client_turn_id="client-image-superseded",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        intent="redirect",
        message=RuntimeMessage(
            role="user",
            content=[
                ContentBlock(
                    type="image",
                    data={
                        "source": {"type": "url", "url": str(attachment)},
                        "version": 1,
                        "digest": digest,
                        "grant_ref": control_input_grant_ref(
                            chat_id="chat-1",
                            run_id="run-1",
                            canonical_path=str(attachment),
                            digest=digest,
                        ),
                    },
                ),
            ],
        ),
    )
    admission = RuntimeInboxAdmission(
        store=store,
        run_id="run-1",
        runtime_metadata={"attempt_id": "attempt-1"},
    )
    outcome = admission.claim_next(after_seq=0)
    assert outcome.claim is not None

    admission.settle_superseded_before_surface(
        outcome.claim,
        superseding_command_id="image-new-direction",
    )

    disposition = store.current_dispositions("run-1")[1]
    assert disposition.status == "cancelled"
    assert disposition.superseded_by_command_id == "image-new-direction"
    assert admission.validate_continuation_sources(
        (outcome.claim.message,)
    ) is None


def test_user_cancelled_attachment_source_stays_unavailable(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    attachment = (tmp_path / "cancelled-guidance.png").resolve()
    content = b"user-cancelled-image"
    attachment.write_bytes(content)
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    store.append_input(
        command_id="image-cancelled",
        client_turn_id="client-image-cancelled",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        intent="redirect",
        message=RuntimeMessage(
            role="user",
            content=[
                ContentBlock(
                    type="image",
                    data={
                        "source": {"type": "url", "url": str(attachment)},
                        "version": 1,
                        "digest": digest,
                        "grant_ref": control_input_grant_ref(
                            chat_id="chat-1",
                            run_id="run-1",
                            canonical_path=str(attachment),
                            digest=digest,
                        ),
                    },
                ),
            ],
        ),
    )
    admission = RuntimeInboxAdmission(
        store=store,
        run_id="run-1",
        runtime_metadata={"attempt_id": "attempt-1"},
    )
    outcome = admission.claim_next(after_seq=0)
    assert outcome.claim is not None
    store.settle_input(
        run_id="run-1",
        item_seq=outcome.claim.item_seq,
        actor_id=outcome.claim.actor_id,
        fence=outcome.claim.fence,
        status="cancelled",
        reason="cancelled by user",
    )

    conflict = admission.validate_continuation_sources(
        (outcome.claim.message,)
    )

    assert conflict is not None
    assert conflict.kind == "run_inbox_continuation_source_unavailable"


def test_consumed_image_source_recovers_in_a_new_attempt_without_replay(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    attachment = (tmp_path / "cross-attempt-guidance.png").resolve()
    content = b"cross-attempt-image-bytes"
    attachment.write_bytes(content)
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    store.append_input(
        command_id="image-cross-attempt",
        client_turn_id="client-image-cross-attempt",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        intent="supplement",
        message=RuntimeMessage(
            role="user",
            content=[
                ContentBlock(
                    type="image",
                    data={
                        "source": {"type": "url", "url": str(attachment)},
                        "version": 1,
                        "digest": digest,
                        "grant_ref": control_input_grant_ref(
                            chat_id="chat-1",
                            run_id="run-1",
                            canonical_path=str(attachment),
                            digest=digest,
                        ),
                    },
                ),
            ],
        ),
    )
    first_attempt = RuntimeInboxAdmission(
        store=store,
        run_id="run-1",
        runtime_metadata={"attempt_id": "attempt-1"},
    )
    outcome = first_attempt.claim_next(after_seq=0)
    assert outcome.claim is not None
    first_attempt.settle_consumed(outcome.claim)
    refs = continuation_input_refs((outcome.claim.message,))

    assert refs[0].source_attempt_id == "attempt-1"
    resumed_attempt = RuntimeInboxAdmission(
        store=store,
        run_id="run-1",
        runtime_metadata={"attempt_id": "attempt-2"},
    )
    recovered, conflict = resumed_attempt.recover_continuation_sources(refs)

    assert conflict is None
    assert recovered == (outcome.claim.message,)
    assert len(store.read_inbox("run-1")) == 1
    assert store.current_dispositions("run-1")[1].status == "consumed"
    assert resumed_attempt.validate_continuation_sources(recovered) is None


def test_consumed_image_source_is_revalidated_before_continuation_reuse(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    attachment = (tmp_path / "changed-guidance.png").resolve()
    content = b"original-consumed-image-bytes"
    attachment.write_bytes(content)
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    store.append_input(
        command_id="image-changed-after-consume",
        client_turn_id="client-image-changed",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        intent="supplement",
        message=RuntimeMessage(
            role="user",
            content=[
                ContentBlock(
                    type="image",
                    data={
                        "source": {"type": "url", "url": str(attachment)},
                        "version": 1,
                        "digest": digest,
                        "grant_ref": control_input_grant_ref(
                            chat_id="chat-1",
                            run_id="run-1",
                            canonical_path=str(attachment),
                            digest=digest,
                        ),
                    },
                ),
            ],
        ),
    )
    admission = RuntimeInboxAdmission(
        store=store,
        run_id="run-1",
        runtime_metadata={"attempt_id": "attempt-1"},
    )
    outcome = admission.claim_next(after_seq=0)
    assert outcome.claim is not None
    admission.settle_consumed(outcome.claim)
    attachment.write_bytes(b"replacement-after-consume")

    conflict = admission.validate_continuation_sources(
        (outcome.claim.message,)
    )

    assert conflict is not None
    assert conflict.kind == "run_inbox_attachment_conflict"
    assert "digest" in conflict.reason
    assert store.current_dispositions("run-1")[1].status == "consumed"


def test_consumed_image_source_grant_expiry_blocks_continuation_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 2.0}
    monkeypatch.setattr(
        inbox_admission_module.time,
        "time",
        lambda: clock["now"],
    )
    store = RuntimeTurnControlStore(tmp_path)
    attachment = (tmp_path / "expiring-consumed-guidance.png").resolve()
    content = b"consumed-image-with-expiring-grant"
    attachment.write_bytes(content)
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    store.append_input(
        command_id="image-expiring-after-consume",
        client_turn_id="client-image-expiring",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        intent="supplement",
        message=RuntimeMessage(
            role="user",
            content=[
                ContentBlock(
                    type="image",
                    data={
                        "source": {"type": "url", "url": str(attachment)},
                        "version": 1,
                        "digest": digest,
                        "grant_ref": control_input_grant_ref(
                            chat_id="chat-1",
                            run_id="run-1",
                            canonical_path=str(attachment),
                            digest=digest,
                        ),
                        "grant_issued_at": 1.0,
                        "grant_expires_at": 10.0,
                    },
                ),
            ],
        ),
    )
    admission = RuntimeInboxAdmission(
        store=store,
        run_id="run-1",
        runtime_metadata={"attempt_id": "attempt-1"},
    )
    outcome = admission.claim_next(after_seq=0)
    assert outcome.claim is not None
    admission.settle_consumed(outcome.claim)
    clock["now"] = 11.0

    conflict = admission.validate_continuation_sources(
        (outcome.claim.message,)
    )

    assert conflict is not None
    assert conflict.kind == "run_inbox_attachment_grant_expired"
    assert "expired" in conflict.reason
    assert store.current_dispositions("run-1")[1].status == "consumed"
