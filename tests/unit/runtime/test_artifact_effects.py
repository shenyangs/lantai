from __future__ import annotations

import socket
import tempfile
from pathlib import Path

from lantai.runtime.artifact_effects import (
    RuntimeArtifactEffectJournal,
    artifact_effect_intents,
    artifact_effect_intents_from_result,
    recover_artifact_delivery,
    settle_artifact_effects,
)
from lantai.runtime.effects import (
    RuntimeEffectJournal,
    RuntimeEffectReducerAction,
    reduce_runtime_effect,
)
from lantai.runtime.runs import RuntimeEffectRecord, RuntimeRunOwner
from lantai.runtime.tools import ToolCall, ToolOutputSemantics, ToolResult, ToolSpec


def _source_effect() -> RuntimeEffectRecord:
    return RuntimeEffectRecord(
        run_id="run-artifact",
        attempt_id="attempt-1",
        step_id="tool-call:finalize-1",
        effect_id="effect-finalize-1",
        source="chat",
        owner=RuntimeRunOwner(agent_id="default"),
        status="intent",
        tool_name="finalize_deliverable",
        args_hash="sha256:args",
        policy_snapshot_hash="sha256:policy",
        idempotency_key="run-artifact:attempt-1:effect-finalize-1",
        release_edition="official",
    )


def test_primary_artifact_intent_has_stable_identity_without_absolute_path(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "deliverables" / "deck.pptx"
    artifact.parent.mkdir()
    artifact.write_bytes(b"pptx-fixture")
    semantics = ToolOutputSemantics(
        artifact_role="primary",
        argument_path_fields=("primary_path",),
    )
    spec = ToolSpec(
        name="finalize_deliverable",
        output_semantics=semantics,
    )
    call = ToolCall(
        id="finalize-1",
        name="finalize_deliverable",
        arguments={"primary_path": str(artifact)},
    )

    source_effect = _source_effect()
    first = artifact_effect_intents(
        source_effect=source_effect,
        tool_call=call,
        tool_spec=spec,
        workspace_dir=tmp_path,
    )
    second = artifact_effect_intents(
        source_effect=source_effect,
        tool_call=call,
        tool_spec=spec,
        workspace_dir=tmp_path,
    )

    assert len(first) == 1
    assert first == second
    record = first[0]
    assert record.status == "intent"
    assert record.artifact_role == "primary"
    assert record.relative_path == "deliverables/deck.pptx"
    assert str(tmp_path) not in record.model_dump_json()
    assert record.artifact_id.startswith("artifact:sha256:")
    assert record.delivery_idempotency_key == (
        f"artifact-delivery:{record.artifact_id}"
    )
    assert record.source_effect_id == "effect-finalize-1"
    assert record.output_semantics_version == 1
    assert record.source_adapter == "declared"


def test_successful_artifact_settlement_captures_content_identity(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "report.pdf"
    artifact.write_bytes(b"verified-pdf")
    call = ToolCall(
        id="finalize-1",
        name="finalize_deliverable",
        arguments={"primary_path": str(artifact)},
    )
    spec = ToolSpec(
        name="finalize_deliverable",
        output_semantics=ToolOutputSemantics(
            artifact_role="primary",
            argument_path_fields=("primary_path",),
        ),
    )
    intents = artifact_effect_intents(
        source_effect=_source_effect(),
        tool_call=call,
        tool_spec=spec,
        workspace_dir=tmp_path,
    )

    settled = settle_artifact_effects(
        intents,
        result=ToolResult(
            call_id=call.id,
            name=call.name,
            ok=True,
        ),
        workspace_dir=tmp_path,
    )

    assert len(settled) == 1
    assert settled[0].status == "committed"
    assert settled[0].revision == 1
    assert settled[0].size_bytes == len(b"verified-pdf")
    assert settled[0].sha256 == (
        "sha256:9cb8eef678c4caf0b3eea50a172998dce1dc83cd2a2564e8a18c26facf941185"
    )
    assert settled[0].media_type == "application/pdf"
    assert settled[0].settled_at is not None


def test_structured_result_path_creates_native_artifact_evidence(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "native-result.pdf"
    artifact.write_bytes(b"native")
    preview = tmp_path / "native-preview.png"
    preview.write_bytes(b"preview")
    call = ToolCall(id="native-1", name="native_export")
    spec = ToolSpec(
        name=call.name,
        output_semantics=ToolOutputSemantics(
            artifact_role="candidate",
            result_path_fields=("output_path",),
        ),
    )

    intents = artifact_effect_intents_from_result(
        source_effect=_source_effect(),
        tool_call=call,
        tool_spec=spec,
        result=ToolResult(
            call_id=call.id,
            name=call.name,
            metadata={
                "raw": {
                    "output_path": [
                        "native-result.pdf",
                        "native-preview.png",
                    ]
                }
            },
        ),
        workspace_dir=tmp_path,
    )

    assert len(intents) == 2
    assert {intent.artifact_role for intent in intents} == {"candidate"}
    assert [intent.relative_path for intent in intents] == [
        "native-result.pdf",
        "native-preview.png",
    ]


def test_artifact_effect_journal_is_additive_and_old_effect_reader_ignores_it(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "result.html"
    artifact.write_text("<h1>done</h1>", encoding="utf-8")
    call = ToolCall(
        id="finalize-1",
        name="finalize_deliverable",
        arguments={"primary_path": str(artifact)},
    )
    spec = ToolSpec(
        name=call.name,
        output_semantics=ToolOutputSemantics(
            artifact_role="primary",
            argument_path_fields=("primary_path",),
        ),
    )
    intent = artifact_effect_intents(
        source_effect=_source_effect(),
        tool_call=call,
        tool_spec=spec,
        workspace_dir=tmp_path,
    )[0]
    committed = settle_artifact_effects(
        (intent,),
        result=ToolResult(call_id=call.id, name=call.name, ok=True),
        workspace_dir=tmp_path,
    )[0]

    journal = RuntimeArtifactEffectJournal(tmp_path)
    journal.append(intent)
    journal.append(committed)

    assert journal.list_records("run-artifact", "attempt-1") == [
        intent,
        committed,
    ]
    assert journal.latest(
        "run-artifact",
        "attempt-1",
        intent.artifact_effect_id,
    ) == committed
    assert journal.ledger_path("run-artifact", "attempt-1").name == (
        "artifact-effects.jsonl"
    )
    assert RuntimeEffectJournal(tmp_path).list_effects(
        "run-artifact",
        "attempt-1",
    ) == []


def test_artifact_intent_rejects_non_file_and_workspace_escape_candidates(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "directory.pdf"
    directory.mkdir()
    target = tmp_path / "target.pdf"
    target.write_bytes(b"target")
    symlink = tmp_path / "symlink.pdf"
    symlink.symlink_to(target)
    outside = tmp_path.parent / f"{tmp_path.name}-outside.pdf"
    outside.write_bytes(b"outside")
    call = ToolCall(
        id="write-many",
        name="write_file",
        arguments={
            "directory": str(directory),
            "symlink": str(symlink),
            "outside": str(outside),
            "unsupported": "script.py",
            "valid": "result.pdf",
        },
    )
    spec = ToolSpec(
        name=call.name,
        output_semantics=ToolOutputSemantics(
            artifact_role="candidate",
            argument_path_fields=(
                "directory",
                "symlink",
                "outside",
                "unsupported",
                "valid",
            ),
        ),
    )

    intents = artifact_effect_intents(
        source_effect=_source_effect(),
        tool_call=call,
        tool_spec=spec,
        workspace_dir=tmp_path,
    )

    assert [record.relative_path for record in intents] == ["result.pdf"]


def test_artifact_intent_rejects_socket() -> None:
    with tempfile.TemporaryDirectory(prefix="lt-s6-", dir="/tmp") as raw_dir:
        workspace = Path(raw_dir)
        socket_path = workspace / "output.pdf"
        unix_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            unix_socket.bind(str(socket_path))
            intents = artifact_effect_intents(
                source_effect=_source_effect(),
                tool_call=ToolCall(
                    id="socket-output",
                    name="write_file",
                    arguments={"file_path": str(socket_path)},
                ),
                tool_spec=ToolSpec(
                    name="write_file",
                    output_semantics=ToolOutputSemantics(
                        artifact_role="candidate",
                        argument_path_fields=("file_path",),
                    ),
                ),
                workspace_dir=workspace,
            )
        finally:
            unix_socket.close()

    assert intents == ()


def test_artifact_semantics_support_multiple_explicit_output_files(
    tmp_path: Path,
) -> None:
    call = ToolCall(
        id="multi-output",
        name="export_bundle",
        arguments={
            "document_path": "report.pdf",
            "preview_path": "preview.png",
        },
    )
    spec = ToolSpec(
        name=call.name,
        output_semantics=ToolOutputSemantics(
            artifact_role="candidate",
            argument_path_fields=("document_path", "preview_path"),
        ),
    )

    intents = artifact_effect_intents(
        source_effect=_source_effect(),
        tool_call=call,
        tool_spec=spec,
        workspace_dir=tmp_path,
    )

    assert [record.relative_path for record in intents] == [
        "report.pdf",
        "preview.png",
    ]


def test_delivery_settlement_requires_matching_file_card_media(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "preview.png"
    artifact.write_bytes(b"png")
    call = ToolCall(
        id="delivery-media",
        name="send_file_to_user",
        arguments={"file_path": str(artifact)},
    )
    intent = artifact_effect_intents(
        source_effect=_source_effect(),
        tool_call=call,
        tool_spec=ToolSpec(
            name=call.name,
            output_semantics=ToolOutputSemantics(
                artifact_role="delivery",
                argument_path_fields=("file_path",),
                content_media_types=("file", "image", "audio", "video"),
            ),
        ),
        workspace_dir=tmp_path,
    )[0]

    missing_card = settle_artifact_effects(
        (intent,),
        result=ToolResult(
            call_id=call.id,
            name=call.name,
            content=[{"type": "text", "text": "sent"}],
        ),
        workspace_dir=tmp_path,
    )[0]
    wrong_card = settle_artifact_effects(
        (intent,),
        result=ToolResult(
            call_id=call.id,
            name=call.name,
            content=[{"type": "file", "filename": "preview.png"}],
        ),
        workspace_dir=tmp_path,
    )[0]
    image_card = settle_artifact_effects(
        (intent,),
        result=ToolResult(
            call_id=call.id,
            name=call.name,
            content=[{"type": "image", "source": {"path": str(artifact)}}],
        ),
        workspace_dir=tmp_path,
    )[0]

    assert missing_card.status == "failed"
    assert wrong_card.status == "failed"
    assert image_card.status == "delivered"


def test_delivery_recovery_resumes_committed_primary_once(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "deck.pptx"
    artifact.write_bytes(b"pptx")
    primary_call = ToolCall(
        id="finalize-1",
        name="finalize_deliverable",
        arguments={"primary_path": str(artifact)},
    )
    primary_spec = ToolSpec(
        name=primary_call.name,
        output_semantics=ToolOutputSemantics(
            artifact_role="primary",
            argument_path_fields=("primary_path",),
        ),
    )
    primary_intent = artifact_effect_intents(
        source_effect=_source_effect(),
        tool_call=primary_call,
        tool_spec=primary_spec,
        workspace_dir=tmp_path,
    )[0]
    primary = settle_artifact_effects(
        (primary_intent,),
        result=ToolResult(call_id=primary_call.id, name=primary_call.name),
        workspace_dir=tmp_path,
    )[0]
    journal = RuntimeArtifactEffectJournal(tmp_path)
    journal.append(primary_intent)
    journal.append(primary)

    pending = recover_artifact_delivery(
        journal,
        run_id="run-artifact",
        attempt_id="attempt-1",
        workspace_dir=tmp_path,
    )

    assert pending.pending_paths == (str(artifact),)
    assert pending.completed is False
    assert pending.idempotency_keys[str(artifact)] == (
        primary.delivery_idempotency_key
    )

    delivery_call = ToolCall(
        id="delivery-1",
        name="send_file_to_user",
        arguments={"file_path": str(artifact)},
    )
    delivery_spec = ToolSpec(
        name=delivery_call.name,
        output_semantics=ToolOutputSemantics(
            artifact_role="delivery",
            argument_path_fields=("file_path",),
        ),
    )
    delivery_source = _source_effect().model_copy(
        update={
            "effect_id": "effect-delivery-1",
            "tool_name": "send_file_to_user",
        }
    )
    delivery_intent = artifact_effect_intents(
        source_effect=delivery_source,
        tool_call=delivery_call,
        tool_spec=delivery_spec,
        workspace_dir=tmp_path,
    )[0]
    delivery = settle_artifact_effects(
        (delivery_intent,),
        result=ToolResult(call_id=delivery_call.id, name=delivery_call.name),
        workspace_dir=tmp_path,
    )[0]
    journal.append(delivery_intent)
    journal.append(delivery)

    recovered = recover_artifact_delivery(
        journal,
        run_id="run-artifact",
        attempt_id="attempt-1",
        workspace_dir=tmp_path,
    )

    assert recovered.pending_paths == ()
    assert recovered.completed is True
    record_count = len(
        journal.list_records("run-artifact", "attempt-1")
    )
    for _index in range(100):
        replayed = recover_artifact_delivery(
            journal,
            run_id="run-artifact",
            attempt_id="attempt-1",
            workspace_dir=tmp_path,
        )
        assert replayed.pending_paths == ()
        assert replayed.completed is True
    assert len(journal.list_records("run-artifact", "attempt-1")) == record_count


def test_delivery_recovery_ignores_candidates_and_delivers_only_primary(
    tmp_path: Path,
) -> None:
    draft = tmp_path / "draft.md"
    draft.write_text("draft", encoding="utf-8")
    primary_path = tmp_path / "final.pdf"
    primary_path.write_bytes(b"final")
    journal = RuntimeArtifactEffectJournal(tmp_path)

    candidate_source = _source_effect().model_copy(
        update={"effect_id": "effect-write-draft", "tool_name": "write_file"}
    )
    candidate_call = ToolCall(
        id="write-draft",
        name="write_file",
        arguments={"file_path": str(draft)},
    )
    candidate_intent = artifact_effect_intents(
        source_effect=candidate_source,
        tool_call=candidate_call,
        tool_spec=ToolSpec(
            name=candidate_call.name,
            output_semantics=ToolOutputSemantics(
                artifact_role="candidate",
                argument_path_fields=("file_path",),
            ),
        ),
        workspace_dir=tmp_path,
    )[0]
    candidate = settle_artifact_effects(
        (candidate_intent,),
        result=ToolResult(call_id=candidate_call.id, name=candidate_call.name),
        workspace_dir=tmp_path,
    )[0]
    journal.append(candidate_intent)
    journal.append(candidate)

    primary_call = ToolCall(
        id="finalize-primary",
        name="finalize_deliverable",
        arguments={"primary_path": str(primary_path)},
    )
    primary_intent = artifact_effect_intents(
        source_effect=_source_effect(),
        tool_call=primary_call,
        tool_spec=ToolSpec(
            name=primary_call.name,
            output_semantics=ToolOutputSemantics(
                artifact_role="primary",
                argument_path_fields=("primary_path",),
            ),
        ),
        workspace_dir=tmp_path,
    )[0]
    primary = settle_artifact_effects(
        (primary_intent,),
        result=ToolResult(call_id=primary_call.id, name=primary_call.name),
        workspace_dir=tmp_path,
    )[0]
    journal.append(primary_intent)
    journal.append(primary)

    recovered = recover_artifact_delivery(
        journal,
        run_id="run-artifact",
        attempt_id="attempt-1",
        workspace_dir=tmp_path,
    )

    assert recovered.pending_paths == (str(primary_path),)
    assert str(draft) not in recovered.pending_paths


def test_t069_delivery_recovery_settles_primary_after_commit_crash_window(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "crash-window.pdf"
    artifact.write_bytes(b"durable")
    source = _source_effect()
    call = ToolCall(
        id="finalize-crash-window",
        name="finalize_deliverable",
        arguments={"primary_path": str(artifact)},
    )
    intent = artifact_effect_intents(
        source_effect=source,
        tool_call=call,
        tool_spec=ToolSpec(
            name=call.name,
            output_semantics=ToolOutputSemantics(
                artifact_role="primary",
                argument_path_fields=("primary_path",),
            ),
        ),
        workspace_dir=tmp_path,
    )[0]
    artifact_journal = RuntimeArtifactEffectJournal(tmp_path)
    artifact_journal.append(intent)
    effect_journal = RuntimeEffectJournal(tmp_path)
    effect_journal.append_effect(source)
    running = reduce_runtime_effect(
        source,
        RuntimeEffectReducerAction(type="start"),
    ).effect
    effect_journal.append_effect(running)
    committed = reduce_runtime_effect(
        running,
        RuntimeEffectReducerAction(
            type="commit",
            result_hash="sha256:tool-result",
        ),
    ).effect
    effect_journal.append_effect(committed)

    recovered = recover_artifact_delivery(
        artifact_journal,
        effect_journal=effect_journal,
        run_id="run-artifact",
        attempt_id="attempt-1",
        workspace_dir=tmp_path,
    )

    assert recovered.pending_paths == (str(artifact),)
    assert recovered.completed is False
    records = artifact_journal.list_records("run-artifact", "attempt-1")
    assert [record.status for record in records] == ["intent", "committed"]
    assert records[-1].sha256 is not None


def test_delivery_recovery_does_not_replay_ambiguous_started_send(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "report.pdf"
    artifact.write_bytes(b"pdf")
    primary_call = ToolCall(
        id="finalize-1",
        name="finalize_deliverable",
        arguments={"primary_path": str(artifact)},
    )
    primary = settle_artifact_effects(
        artifact_effect_intents(
            source_effect=_source_effect(),
            tool_call=primary_call,
            tool_spec=ToolSpec(
                name=primary_call.name,
                output_semantics=ToolOutputSemantics(
                    artifact_role="primary",
                    argument_path_fields=("primary_path",),
                ),
            ),
            workspace_dir=tmp_path,
        ),
        result=ToolResult(call_id=primary_call.id, name=primary_call.name),
        workspace_dir=tmp_path,
    )[0]
    artifact_journal = RuntimeArtifactEffectJournal(tmp_path)
    artifact_journal.append(primary.model_copy(update={"revision": 0, "status": "intent"}))
    artifact_journal.append(primary)

    delivery_source = _source_effect().model_copy(
        update={
            "effect_id": "effect-delivery-ambiguous",
            "tool_name": "send_file_to_user",
            "status": "intent",
            "revision": 0,
        }
    )
    effect_journal = RuntimeEffectJournal(tmp_path)
    effect_journal.append_effect(delivery_source)
    running = reduce_runtime_effect(
        delivery_source,
        RuntimeEffectReducerAction(type="start"),
    ).effect
    effect_journal.append_effect(running)
    delivery_intent = artifact_effect_intents(
        source_effect=delivery_source,
        tool_call=ToolCall(
            id="delivery-ambiguous",
            name="send_file_to_user",
            arguments={"file_path": str(artifact)},
        ),
        tool_spec=ToolSpec(
            name="send_file_to_user",
            output_semantics=ToolOutputSemantics(
                artifact_role="delivery",
                argument_path_fields=("file_path",),
            ),
        ),
        workspace_dir=tmp_path,
    )[0]
    artifact_journal.append(delivery_intent)

    recovered = recover_artifact_delivery(
        artifact_journal,
        effect_journal=effect_journal,
        run_id="run-artifact",
        attempt_id="attempt-1",
        workspace_dir=tmp_path,
    )

    assert recovered.pending_paths == ()
    assert recovered.completed is False
    assert recovered.ambiguous_artifact_ids == (primary.artifact_id,)


def test_delivery_recovery_marks_deleted_primary_invalid(tmp_path: Path) -> None:
    artifact = tmp_path / "deleted.md"
    artifact.write_text("content", encoding="utf-8")
    call = ToolCall(
        id="finalize-deleted",
        name="finalize_deliverable",
        arguments={"primary_path": str(artifact)},
    )
    primary = settle_artifact_effects(
        artifact_effect_intents(
            source_effect=_source_effect(),
            tool_call=call,
            tool_spec=ToolSpec(
                name=call.name,
                output_semantics=ToolOutputSemantics(
                    artifact_role="primary",
                    argument_path_fields=("primary_path",),
                ),
            ),
            workspace_dir=tmp_path,
        ),
        result=ToolResult(call_id=call.id, name=call.name),
        workspace_dir=tmp_path,
    )[0]
    journal = RuntimeArtifactEffectJournal(tmp_path)
    journal.append(primary.model_copy(update={"revision": 0, "status": "intent"}))
    journal.append(primary)
    artifact.unlink()

    recovered = recover_artifact_delivery(
        journal,
        run_id="run-artifact",
        attempt_id="attempt-1",
        workspace_dir=tmp_path,
    )

    assert recovered.pending_paths == ()
    assert recovered.completed is False
    assert recovered.invalid_artifact_ids == (primary.artifact_id,)


def test_delivery_recovery_rejects_moved_or_tampered_primary(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "report.pdf"
    artifact.write_bytes(b"original")
    call = ToolCall(
        id="finalize-tamper",
        name="finalize_deliverable",
        arguments={"primary_path": str(artifact)},
    )
    primary_intent = artifact_effect_intents(
        source_effect=_source_effect(),
        tool_call=call,
        tool_spec=ToolSpec(
            name=call.name,
            output_semantics=ToolOutputSemantics(
                artifact_role="primary",
                argument_path_fields=("primary_path",),
            ),
        ),
        workspace_dir=tmp_path,
    )[0]
    primary = settle_artifact_effects(
        (primary_intent,),
        result=ToolResult(call_id=call.id, name=call.name),
        workspace_dir=tmp_path,
    )[0]
    journal = RuntimeArtifactEffectJournal(tmp_path)
    journal.append(primary_intent)
    journal.append(primary)

    artifact.write_bytes(b"tampered")
    tampered = recover_artifact_delivery(
        journal,
        run_id="run-artifact",
        attempt_id="attempt-1",
        workspace_dir=tmp_path,
    )
    assert tampered.pending_paths == ()
    assert tampered.invalid_artifact_ids == (primary.artifact_id,)

    artifact.rename(tmp_path / "moved.pdf")
    moved = recover_artifact_delivery(
        journal,
        run_id="run-artifact",
        attempt_id="attempt-1",
        workspace_dir=tmp_path,
    )
    assert moved.pending_paths == ()
    assert moved.invalid_artifact_ids == (primary.artifact_id,)
