# -*- coding: utf-8 -*-
"""Behavior locks for persistent TurnReceipt and Run Inbox semantics."""

from pathlib import Path

import pytest

from lantai.runtime import turn_control
from lantai.runtime.coherence import ExecutionFence
from lantai.runtime.messages import ContentBlock, RuntimeMessage
from lantai.runtime.turn_control import (
    InputTarget,
    RunInboxMessageMetadata,
    RuntimeInputConflictError,
    RuntimeTurnControlStore,
)


def test_structured_input_queue_and_history_are_bounded_without_breaking_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        turn_control,
        "MAX_PENDING_CONTROL_INPUTS_PER_RUN",
        2,
        raising=False,
    )
    monkeypatch.setattr(
        turn_control,
        "MAX_CONTROL_INPUT_HISTORY_PER_RUN",
        4,
        raising=False,
    )
    store = RuntimeTurnControlStore(tmp_path)

    def append(command_id: str):
        return store.append_input(
            command_id=command_id,
            run_id="run-1",
            attempt_id="attempt-1",
            chat_id="chat-1",
            message=RuntimeMessage.text("user", command_id),
            intent="supplement",
        )

    first_receipt, _ = append("input-1")
    append("input-2")

    retry_receipt, _ = append("input-1")
    assert retry_receipt == first_receipt
    with pytest.raises(RuntimeError, match="queue.*full"):
        append("input-3")

    cancelled = store.cancel_input(
        chat_id="chat-1",
        command_id="input-1",
        actor_id="chat:chat-1",
    )
    assert cancelled.execution_status == "cancelled"
    append("input-3")
    store.cancel_input(
        chat_id="chat-1",
        command_id="input-2",
        actor_id="chat:chat-1",
    )
    append("input-4")
    store.cancel_input(
        chat_id="chat-1",
        command_id="input-3",
        actor_id="chat:chat-1",
    )

    with pytest.raises(RuntimeError, match="history.*full"):
        append("input-5")
    assert [item.command_id for item in store.read_inbox("run-1")] == [
        "input-1",
        "input-2",
        "input-3",
        "input-4",
    ]


def test_terminal_admission_gate_preserves_retry_and_next_turn_queue(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    fence = ExecutionFence(attempt_id="attempt-1", generation=3)
    original_receipt, original_item = store.append_input(
        command_id="input-applied",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        message=RuntimeMessage.text("user", "已经应用的补充"),
        intent="supplement",
        fence=fence,
    )
    store.claim_input(
        run_id="run-1",
        item_seq=original_item.seq,
        actor_id="native:attempt-1:3",
        fence=fence,
    )
    store.settle_input(
        run_id="run-1",
        item_seq=original_item.seq,
        actor_id="native:attempt-1:3",
        fence=fence,
        status="consumed",
    )

    gate = store.close_current_run_admission_if_drained(
        run_id="run-1",
        after_seq=original_item.seq,
        actor_id="native:attempt-1:3",
        fence=fence,
    )

    assert gate is not None
    assert gate.run_id == "run-1"
    assert gate.after_seq == original_item.seq
    retry_receipt, retry_item = store.append_input(
        command_id="input-applied",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        message=RuntimeMessage.text("user", "已经应用的补充"),
        intent="supplement",
        fence=fence,
    )
    assert retry_receipt.command_id == original_receipt.command_id
    assert retry_receipt.server_seq == original_receipt.server_seq
    assert retry_receipt.payload_digest == original_receipt.payload_digest
    assert retry_receipt.execution_status == "applied"
    assert retry_item == original_item

    with pytest.raises(
        turn_control.RuntimeInputClosedError,
        match="closed",
    ):
        store.append_input(
            command_id="input-too-late",
            run_id="run-1",
            attempt_id="attempt-1",
            chat_id="chat-1",
            message=RuntimeMessage.text("user", "不能成为永久孤儿"),
            intent="redirect",
            fence=fence,
        )

    queued_receipt, _ = store.append_input(
        command_id="input-next-turn",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        message=RuntimeMessage.text("user", "下一轮再做"),
        intent="queue_next",
        fence=fence,
    )
    assert queued_receipt.execution_status == "pending"
    assert queued_receipt.next_waiting_reason == "awaiting_next_turn"


def test_terminal_admission_gate_cannot_close_over_pending_current_input(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    fence = ExecutionFence(attempt_id="attempt-1")
    store.append_input(
        command_id="input-pending",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        message=RuntimeMessage.text("user", "结案前必须先消费"),
        intent="supplement",
        fence=fence,
    )

    gate = store.close_current_run_admission_if_drained(
        run_id="run-1",
        after_seq=0,
        actor_id="native:attempt-1:0",
        fence=fence,
    )

    assert gate is None


def test_submit_receipt_is_idempotent_by_client_turn_id(tmp_path: Path) -> None:
    store = RuntimeTurnControlStore(tmp_path)

    first = store.create_receipt(
        client_turn_id="client-1",
        operation="submit",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        status="accepted",
    )
    duplicate = store.create_receipt(
        client_turn_id="client-1",
        operation="submit",
        run_id="run-2",
        attempt_id="attempt-2",
        chat_id="chat-1",
        status="accepted",
    )

    assert duplicate == first
    assert duplicate.run_id == "run-1"


def test_steer_rejects_reusing_command_id_with_different_payload(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)

    first_receipt, first_item = store.append_steer(
        client_turn_id="steer-1",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        text="改成离线版",
    )

    with pytest.raises(RuntimeInputConflictError, match="payload"):
        store.append_steer(
            client_turn_id="steer-1",
            run_id="run-1",
            attempt_id="attempt-1",
            chat_id="chat-1",
            text="这条重传不应该覆盖",
        )

    assert first_receipt.status == "steered"
    assert first_item.text == "改成离线版"
    assert first_item.attempt_id == "attempt-1"
    assert first_item.target == InputTarget(kind="run", target_id="run-1")
    assert first_item.fence == ExecutionFence(attempt_id="attempt-1")
    assert first_item.provenance is not None
    assert first_item.provenance.source_kind == "direct_user_input"
    assert store.read_inbox("run-1") == [first_item]
    assert store.read_inbox("run-1", after_seq=1) == []


def test_steer_preserves_strict_artifact_target_metadata(tmp_path: Path) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    metadata = RunInboxMessageMetadata.model_validate(
        {
            "artifact_edit_target": {
                "schema_version": 1,
                "chat_id": "chat-1",
                "asset_id": "asset-1",
                "work_id": "work-1",
                "edit_path": "/managed/edit/deck.pptx",
                "base_revision": {
                    "version": 3,
                    "digest": "sha256:base",
                },
                "selection": {"kind": "page", "page": 3},
            },
            "work_edit_context": "Only edit the managed branch.",
        }
    )

    _, item = store.append_steer(
        client_turn_id="steer-with-target",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        text="把第三页收紧",
        message_metadata=metadata,
    )
    restored = store.read_inbox("run-1")[0]

    assert item.message_metadata == metadata
    assert restored.message_metadata == metadata
    assert restored.message_metadata is not None
    assert restored.message_metadata.artifact_edit_target is not None
    assert restored.message_metadata.artifact_edit_target.selection is not None
    assert restored.message_metadata.artifact_edit_target.selection.page == 3


def test_artifact_target_metadata_rejects_untrusted_extra_fields() -> None:
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        RunInboxMessageMetadata.model_validate(
            {
                "artifact_edit_target": {
                    "schema_version": 1,
                    "chat_id": "chat-1",
                    "asset_id": "asset-1",
                    "work_id": "work-1",
                    "edit_path": "/managed/edit/deck.pptx",
                    "base_revision": {"version": 1},
                    "selection": {"kind": "page", "page": 3},
                    "node_capability": "filesystem",
                }
            }
        )


def test_steer_retry_with_same_command_and_payload_returns_original(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)

    first_receipt, first_item = store.append_steer(
        client_turn_id="steer-1",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        text="改成离线版",
    )
    retry_receipt, retry_item = store.append_steer(
        client_turn_id="steer-1",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        text="改成离线版",
    )

    assert retry_receipt == first_receipt
    assert retry_item == first_item
    assert store.read_inbox("run-1") == [first_item]


def test_structured_input_persists_an_image_without_inventing_text(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    message = RuntimeMessage(
        role="user",
        content=[
            ContentBlock(
                type="image",
                data={
                    "attachment_id": "upload-1",
                    "version": 3,
                    "digest": "sha256:image-v3",
                    "grant_ref": "grant-1",
                },
            ),
        ],
    )

    receipt, item = store.append_input(
        command_id="input-1",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        message=message,
        intent="supplement",
    )

    assert receipt.admission_status == "admitted"
    assert receipt.execution_status == "pending"
    assert item.message == message
    assert item.text == ""
    assert item.payload_digest.startswith("sha256:")
    assert store.read_inbox("run-1") == [item]


def test_structured_input_is_idempotent_but_same_id_cannot_change_payload(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    original = RuntimeMessage.text("user", "保留当前约束")

    first_receipt, first_item = store.append_input(
        command_id="input-1",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        message=original,
        intent="redirect",
    )
    retry_receipt, retry_item = store.append_input(
        command_id="input-1",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        message=original,
        intent="redirect",
    )

    assert retry_receipt == first_receipt
    assert retry_item == first_item
    with pytest.raises(RuntimeInputConflictError, match="payload"):
        store.append_input(
            command_id="input-1",
            run_id="run-1",
            attempt_id="attempt-1",
            chat_id="chat-1",
            message=RuntimeMessage.text("user", "不要保留当前约束"),
            intent="redirect",
        )
    assert store.read_inbox("run-1") == [first_item]


def test_structured_inputs_with_equal_text_and_distinct_ids_stay_distinct(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    message = RuntimeMessage.text("user", "同一句也要执行两次")

    _, first = store.append_input(
        command_id="input-1",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        message=message,
        intent="supplement",
    )
    _, second = store.append_input(
        command_id="input-2",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        message=message,
        intent="supplement",
    )

    assert first.seq == 1
    assert second.seq == 2
    assert store.read_inbox("run-1") == [first, second]


def test_reopen_preserves_v1_and_v2_inputs_without_losing_attachments_or_reconsuming(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    _, legacy = store.append_steer(
        client_turn_id="legacy-steer",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        text="旧 schema 的文字引导",
    )
    store.claim_input(
        run_id="run-1",
        item_seq=legacy.seq,
        actor_id="native:attempt-1",
        fence=legacy.fence,
    )
    store.settle_input(
        run_id="run-1",
        item_seq=legacy.seq,
        actor_id="native:attempt-1",
        fence=legacy.fence,
        status="consumed",
    )
    message = RuntimeMessage(
        role="user",
        content=[
            ContentBlock(
                type="image",
                data={
                    "attachment_id": "upload-reopen",
                    "version": 7,
                    "digest": "sha256:reopen-image-v7",
                    "grant_ref": "grant-reopen",
                },
            ),
        ],
    )
    _, structured = store.append_input(
        command_id="input-reopen",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        message=message,
        intent="supplement",
    )
    store.claim_input(
        run_id="run-1",
        item_seq=structured.seq,
        actor_id="native:attempt-1",
        fence=structured.fence,
    )
    store.bind_input_to_model_request(
        run_id="run-1",
        item_seq=structured.seq,
        actor_id="native:attempt-1",
        fence=structured.fence,
        model_request_id="run-1:model:reopen",
        model_epoch=4,
        intent_revision=2,
    )
    store.settle_input(
        run_id="run-1",
        item_seq=structured.seq,
        actor_id="native:attempt-1",
        fence=structured.fence,
        status="consumed",
    )

    restarted = RuntimeTurnControlStore(tmp_path)
    inbox = restarted.read_inbox("run-1")
    receipt = restarted.get_input_receipt(
        chat_id="chat-1",
        command_id="input-reopen",
    )

    assert [item.seq for item in inbox] == [1, 2]
    assert inbox[0] == legacy
    assert inbox[1] == structured
    assert inbox[1].message == message
    assert inbox[1].text == ""
    assert inbox[1].message.content[0].data == {
        "attachment_id": "upload-reopen",
        "version": 7,
        "digest": "sha256:reopen-image-v7",
        "grant_ref": "grant-reopen",
    }
    assert receipt is not None
    assert receipt.execution_status == "applied"
    assert receipt.model_request_id == "run-1:model:reopen"
    assert receipt.model_epoch == 4
    assert receipt.intent_revision == 2
    assert restarted.read_admissible_inbox("run-1") == []


def test_pending_input_can_be_cancelled_idempotently_before_owner_claim(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    _, item = store.append_input(
        command_id="input-cancel-1",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        message=RuntimeMessage.text("user", "这条不用了"),
        intent="supplement",
    )

    first = store.cancel_input(
        chat_id="chat-1",
        command_id="input-cancel-1",
        actor_id="chat:chat-1",
    )
    retry = store.cancel_input(
        chat_id="chat-1",
        command_id="input-cancel-1",
        actor_id="chat:chat-1",
    )

    assert retry == first
    assert first.execution_status == "cancelled"
    assert first.disposition_status == "cancelled"
    assert first.superseded_by_command_id is None
    assert store.read_admissible_inbox("run-1") == []
    with pytest.raises(RuntimeInputConflictError, match="cancelled"):
        store.claim_input(
            run_id="run-1",
            item_seq=item.seq,
            actor_id="native:attempt-1",
            fence=item.fence,
        )


def test_cancel_claimed_input_wins_atomically_over_model_binding(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    fence = ExecutionFence(attempt_id="attempt-1", generation=2)
    _, item = store.append_input(
        command_id="input-cancel-claimed",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        message=RuntimeMessage.text("user", "先不要发给模型"),
        intent="redirect",
        fence=fence,
    )
    store.claim_input(
        run_id="run-1",
        item_seq=item.seq,
        actor_id="native:attempt-1:2",
        fence=fence,
    )

    receipt = store.cancel_input(
        chat_id="chat-1",
        command_id="input-cancel-claimed",
        actor_id="chat:chat-1",
    )

    assert receipt.execution_status == "cancelled"
    with pytest.raises(RuntimeInputConflictError, match="cancelled"):
        store.bind_input_to_model_request(
            run_id="run-1",
            item_seq=item.seq,
            actor_id="native:attempt-1:2",
            fence=fence,
            model_request_id="run-1:model:2",
            model_epoch=2,
            intent_revision=1,
        )


def test_bound_input_cannot_be_cancelled_or_replaced_as_if_unapplied(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    fence = ExecutionFence(attempt_id="attempt-1")
    _, item = store.append_input(
        command_id="input-bound",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        message=RuntimeMessage.text("user", "已经进入请求"),
        intent="supplement",
        fence=fence,
    )
    store.claim_input(
        run_id="run-1",
        item_seq=item.seq,
        actor_id="native:attempt-1",
        fence=fence,
    )
    store.bind_input_to_model_request(
        run_id="run-1",
        item_seq=item.seq,
        actor_id="native:attempt-1",
        fence=fence,
        model_request_id="run-1:model:1",
        model_epoch=1,
        intent_revision=1,
    )

    with pytest.raises(RuntimeInputConflictError, match="bound"):
        store.cancel_input(
            chat_id="chat-1",
            command_id="input-bound",
            actor_id="chat:chat-1",
        )
    with pytest.raises(RuntimeInputConflictError, match="bound"):
        store.replace_input(
            original_command_id="input-bound",
            command_id="input-replacement",
            run_id="run-1",
            attempt_id="attempt-1",
            chat_id="chat-1",
            message=RuntimeMessage.text("user", "不能覆盖已经进入请求的历史"),
            intent="supplement",
        )
    assert store.get_input_item(
        chat_id="chat-1",
        command_id="input-replacement",
    ) is None


def test_settle_superseded_input_persists_structured_lineage_idempotently(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    fence = ExecutionFence(attempt_id="attempt-1")
    _, item = store.append_input(
        command_id="input-old",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        message=RuntimeMessage.text("user", "旧方向"),
        intent="redirect",
        fence=fence,
    )
    store.claim_input(
        run_id="run-1",
        item_seq=item.seq,
        actor_id="native:attempt-1",
        fence=fence,
    )

    first = store.settle_input(
        run_id="run-1",
        item_seq=item.seq,
        actor_id="native:attempt-1",
        fence=fence,
        status="cancelled",
        reason="superseded before provider surface",
        superseded_by_command_id="input-new",
    )
    retry = store.settle_input(
        run_id="run-1",
        item_seq=item.seq,
        actor_id="native:attempt-1",
        fence=fence,
        status="cancelled",
        reason="superseded before provider surface",
        superseded_by_command_id="input-new",
    )

    assert retry == first
    assert first.superseded_by_command_id == "input-new"
    receipt = store.get_input_receipt(
        chat_id="chat-1",
        command_id="input-old",
    )
    assert receipt is not None
    assert receipt.execution_status == "superseded"
    assert receipt.superseded_by_command_id == "input-new"
    with pytest.raises(
        RuntimeInputConflictError,
        match="different supersession lineage",
    ):
        store.settle_input(
            run_id="run-1",
            item_seq=item.seq,
            actor_id="native:attempt-1",
            fence=fence,
            status="cancelled",
            reason="superseded before provider surface",
            superseded_by_command_id="input-other",
        )


def test_replace_pending_input_atomically_supersedes_old_and_admits_new(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    _, original = store.append_input(
        command_id="input-old",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        message=RuntimeMessage.text("user", "旧内容"),
        intent="supplement",
    )

    old_receipt, new_receipt, replacement = store.replace_input(
        original_command_id="input-old",
        command_id="input-new",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        message=RuntimeMessage.text("user", "新内容"),
        intent="redirect",
    )

    assert old_receipt.execution_status == "superseded"
    assert old_receipt.superseded_by_command_id == "input-new"
    assert new_receipt.execution_status == "pending"
    assert new_receipt.replaces_command_id == "input-old"
    assert replacement.replaces_command_id == "input-old"
    assert replacement.seq == original.seq + 1
    assert store.read_admissible_inbox("run-1") == [replacement]
    assert store.get_input_item(
        chat_id="chat-1",
        command_id="input-new",
    ) == replacement


def test_replace_retry_is_idempotent_but_new_command_cannot_change_payload(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    store.append_input(
        command_id="input-old",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        message=RuntimeMessage.text("user", "旧内容"),
        intent="supplement",
    )
    kwargs = {
        "original_command_id": "input-old",
        "command_id": "input-new",
        "run_id": "run-1",
        "attempt_id": "attempt-1",
        "chat_id": "chat-1",
        "message": RuntimeMessage.text("user", "修订内容"),
        "intent": "supplement",
    }

    first = store.replace_input(**kwargs)
    retry = store.replace_input(**kwargs)

    assert retry == first
    assert len(store.read_inbox("run-1")) == 2
    with pytest.raises(RuntimeInputConflictError, match="payload"):
        store.replace_input(
            **{
                **kwargs,
                "message": RuntimeMessage.text("user", "偷偷换成别的内容"),
            },
        )
    assert len(store.read_inbox("run-1")) == 2


def test_structured_input_status_distinguishes_admission_binding_and_application(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    fence = ExecutionFence(attempt_id="attempt-1", generation=2)
    receipt, item = store.append_input(
        command_id="input-status-1",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        message=RuntimeMessage.text("user", "立即改成蓝色"),
        intent="redirect",
        fence=fence,
    )

    assert receipt.execution_status == "pending"
    assert receipt.disposition_status is None

    store.claim_input(
        run_id="run-1",
        item_seq=item.seq,
        actor_id="native:attempt-1:2",
        fence=fence,
    )
    claimed = store.get_input_receipt(
        chat_id="chat-1",
        command_id="input-status-1",
    )
    assert claimed is not None
    assert claimed.execution_status == "claimed"
    assert claimed.disposition_status == "claimed"
    assert claimed.next_waiting_reason == "awaiting_model_request"

    store.bind_input_to_model_request(
        run_id="run-1",
        item_seq=item.seq,
        actor_id="native:attempt-1:2",
        fence=fence,
        model_request_id="run-1:model:2",
        model_epoch=2,
        intent_revision=1,
    )
    bound = store.get_input_receipt(
        chat_id="chat-1",
        command_id="input-status-1",
    )
    assert bound is not None
    assert bound.execution_status == "bound_to_request"
    assert bound.disposition_status == "bound"
    assert bound.model_request_id == "run-1:model:2"
    assert bound.model_epoch == 2
    assert bound.intent_revision == 1

    store.settle_input(
        run_id="run-1",
        item_seq=item.seq,
        actor_id="native:attempt-1:2",
        fence=fence,
        status="consumed",
    )
    applied = store.get_input_receipt(
        chat_id="chat-1",
        command_id="input-status-1",
    )
    assert applied is not None
    assert applied.execution_status == "applied"
    assert applied.disposition_status == "consumed"
    assert applied.next_waiting_reason == ""
    assert applied.model_request_id == "run-1:model:2"
    assert applied.model_epoch == 2
    assert applied.intent_revision == 1


def test_structured_input_status_exposes_superseded_without_claiming_applied(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    fence = ExecutionFence(attempt_id="attempt-1")
    _, item = store.append_input(
        command_id="input-status-superseded",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        message=RuntimeMessage.text("user", "先补这一点"),
        intent="supplement",
        fence=fence,
    )
    store.claim_input(
        run_id="run-1",
        item_seq=item.seq,
        actor_id="native:attempt-1:0",
        fence=fence,
    )
    store.settle_input(
        run_id="run-1",
        item_seq=item.seq,
        actor_id="native:attempt-1:0",
        fence=fence,
        status="cancelled",
        reason="superseded before provider surface by immediate redirect input-2",
    )

    status = store.get_input_receipt(
        chat_id="chat-1",
        command_id="input-status-superseded",
    )
    assert status is not None
    assert status.execution_status == "superseded"
    assert status.disposition_status == "cancelled"
    assert status.disposition_reason.startswith("superseded before")
    assert status.next_waiting_reason == ""


def test_input_claim_and_final_disposition_are_append_only(tmp_path: Path) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    fence = ExecutionFence(
        attempt_id="attempt-1",
        generation=3,
        expected_revision=7,
        lease_owner="worker-1",
    )
    _, item = store.append_steer(
        client_turn_id="steer-1",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        text="先完成测试",
        fence=fence,
    )

    claimed = store.claim_input(
        run_id="run-1",
        item_seq=item.seq,
        actor_id="native:attempt-1",
        fence=fence,
    )
    duplicate = store.claim_input(
        run_id="run-1",
        item_seq=item.seq,
        actor_id="native:attempt-1",
        fence=fence,
    )

    assert duplicate == claimed
    assert store.read_admissible_inbox("run-1") == []
    assert store.read_admissible_inbox(
        "run-1",
        claimant_id="native:attempt-1",
    ) == [item]

    consumed = store.settle_input(
        run_id="run-1",
        item_seq=item.seq,
        actor_id="native:attempt-1",
        fence=fence,
        status="consumed",
    )

    assert consumed.status == "consumed"
    assert [row.status for row in store.read_dispositions("run-1")] == [
        "claimed",
        "consumed",
    ]
    assert store.read_admissible_inbox(
        "run-1",
        claimant_id="native:attempt-1",
    ) == []


@pytest.mark.parametrize(
    "wrong_fence",
    [
        ExecutionFence(attempt_id="attempt-2", generation=3),
        ExecutionFence(attempt_id="attempt-1", generation=4),
        ExecutionFence(
            attempt_id="attempt-1",
            generation=3,
            lease_owner="worker-2",
        ),
        ExecutionFence(attempt_id="attempt-1", generation=3),
    ],
)
def test_input_claim_rejects_stale_or_foreign_fence(
    tmp_path: Path,
    wrong_fence: ExecutionFence,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    admitted_fence = ExecutionFence(
        attempt_id="attempt-1",
        generation=3,
        lease_owner="worker-1",
    )
    _, item = store.append_steer(
        client_turn_id="steer-1",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        text="不要串到别的运行",
        fence=admitted_fence,
    )

    with pytest.raises(RuntimeInputConflictError):
        store.claim_input(
            run_id="run-1",
            item_seq=item.seq,
            actor_id="foreign-worker",
            fence=wrong_fence,
        )


def test_input_claim_cannot_be_stolen_or_silently_rerouted(tmp_path: Path) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    fence = ExecutionFence(attempt_id="attempt-1")
    _, item = store.append_steer(
        client_turn_id="steer-1",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        text="继续当前任务",
    )
    store.claim_input(
        run_id="run-1",
        item_seq=item.seq,
        actor_id="worker-1",
        fence=fence,
    )

    with pytest.raises(RuntimeInputConflictError):
        store.claim_input(
            run_id="run-1",
            item_seq=item.seq,
            actor_id="worker-2",
            fence=fence,
        )

    rerouted = store.reroute_input(
        run_id="run-1",
        item_seq=item.seq,
        actor_id="worker-1",
        fence=fence,
        target=InputTarget(kind="next_turn", target_id="chat-1"),
        reason="run already settled",
    )

    assert rerouted.status == "rerouted"
    assert rerouted.reroute_target == InputTarget(
        kind="next_turn",
        target_id="chat-1",
    )
