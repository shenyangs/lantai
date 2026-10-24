"""External writes cannot be retried because their receipt failed to persist."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from lantai.runtime.adapters.native_runtime import NativeLantaiRuntime
from lantai.runtime.effects import RuntimeEffectJournal, RuntimeEffectRecoveryCapability
from lantai.runtime.effects import RuntimeEffectJournalCorruptionError, RuntimeEffectUnresolvedError
from lantai.runtime.artifact_effects import RuntimeArtifactEffectJournal
import lantai.runtime.effects as effect_module
from lantai.runtime.models import ModelMessage, ModelResponse, ModelStreamEvent
from lantai.runtime.tools import LantaiToolRegistry, ToolCall, ToolSpec, ToolOutputSemantics
from tests.unit.runtime.test_effect_journal import _effect
from lantai.app.experience.runtime_runs import record_chat_runtime_run_started
from lantai.app.experience.events import build_runtime_run_events_response
from lantai.app.completion_gate import finalize_deliverable
from lantai.app.runner.runner import AgentRunner
from lantai.runtime.runs import build_chat_task_contract
from lantai.runtime.store import RuntimeRunStore, RuntimeRunStoreError
from lantai.runtime.effect_context import current_runtime_effect_context
from lantai.runtime.native_turn_support import _RuntimeDurabilityBlocked
from tests.unit.runtime.test_native_runtime import (
    ScriptedModelClient,
    _completed,
    _runtime_input,
)


def _call(call_id: str):
    return [
        ModelStreamEvent(
            kind="tool_call_completed",
            tool_call=ToolCall(id=call_id, name="external_write", arguments={"value": "once"}),
        ),
        ModelStreamEvent(
            kind="message_completed",
            response=ModelResponse(message=ModelMessage(role="assistant")),
        ),
    ]


def _registry(calls):
    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="external_write",
            input_schema={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
            effect_recovery=RuntimeEffectRecoveryCapability(
                external_side_effect=True,
                idempotency="not_idempotent",
                probe="manual",
                on_ambiguity="manual_disambiguation",
            ),
        ),
        lambda value: calls.append(value) or "applied",
    )
    return registry


@pytest.mark.asyncio
@pytest.mark.parametrize("after_write", [False, True])
async def test_failed_external_receipt_keeps_identity_and_blocks_new_call_id(tmp_path, after_write):
    class FailingJournal(RuntimeEffectJournal):
        failed = False

        def _append_envelope(self, path, effect):
            if effect.status == "committed" and not self.failed:
                self.failed = True
                if after_write:
                    super()._append_envelope(path, effect)
                raise OSError("private receipt details")
            super()._append_envelope(path, effect)

    calls = []
    client = ScriptedModelClient([_call("first"), _call("second"), [_completed("check receipt", message_id="final")]])
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _: client,
        tool_registry=_registry(calls),
        effect_journal=FailingJournal(tmp_path),
    )
    events = [event async for event in runtime.run_turn(_runtime_input())]
    completed = [event for event in events if event.type == "tool.call.completed"]
    assert calls == ["once"]
    assert len(completed) == 1
    metadata = completed[0].payload["metadata"]
    assert metadata["side_effect_outcome"] == "committed"
    assert metadata["automatic_retry_allowed"] is False
    assert metadata["effect_settlement_status"] == "unconfirmed"
    assert completed[0].payload["canonical_tool"]["effect_id"] == "effect:first"
    assert "private receipt details" not in json.dumps([e.model_dump(mode="json") for e in events])


@pytest.mark.asyncio
@pytest.mark.parametrize("after_write", [False, True])
@pytest.mark.parametrize("new_turn", [False, True])
async def test_unsettled_external_write_blocks_retry_after_runtime_restart(tmp_path, after_write, new_turn):
    class FailingJournal(RuntimeEffectJournal):
        def _append_envelope(self, path, effect):
            if effect.status == "committed":
                if after_write:
                    super()._append_envelope(path, effect)
                raise OSError("receipt unavailable")
            super()._append_envelope(path, effect)

    calls = []
    registry = _registry(calls)
    first = ScriptedModelClient([_call("first"), [_completed("check", message_id="end-1")]])
    runtime = NativeLantaiRuntime(model_client_factory=lambda _: first, tool_registry=registry, effect_journal=FailingJournal(tmp_path))
    _ = [event async for event in runtime.run_turn(_runtime_input())]
    second = ScriptedModelClient([_call("new-id"), [_completed("check", message_id="end-2")]])
    resumed = NativeLantaiRuntime(model_client_factory=lambda _: second, tool_registry=registry, effect_journal=RuntimeEffectJournal(tmp_path))
    request = _runtime_input()
    if new_turn:
        request = request.model_copy(update={"request": request.request.model_copy(update={"run_id": "turn-2"})})
    events = [event async for event in resumed.run_turn(request)]
    assert calls == ["once"]
    completed = next(event for event in events if event.type == "tool.call.completed")
    assert completed.payload["metadata"]["automatic_retry_allowed"] is False


@pytest.mark.asyncio
async def test_failed_after_tool_checkpoint_blocks_new_turn_after_restart(tmp_path, monkeypatch):
    calls = []
    task = build_chat_task_contract(objective="write once", client_turn_id="client-1", complexity="simple", work_method_id="standard")
    await record_chat_runtime_run_started(
        workspace_dir=tmp_path, agent_id="default", chat_id="chat-1",
        session_id="session-1", run_id="turn-1", effective_workspace_dir=str(tmp_path),
        task_contract=task,
    )
    original = RuntimeRunStore.save_contract

    async def fail_checkpoint(self, contract, **kwargs):
        if contract.checkpoint is not None and contract.checkpoint.stage in {"tool_boundary", "failed"}:
            raise RuntimeRunStoreError("PRIVATE_CHECKPOINT_DETAIL")
        return await original(self, contract, **kwargs)

    monkeypatch.setattr(RuntimeRunStore, "save_contract", fail_checkpoint)
    request = _runtime_input().model_copy(update={
        "request": _runtime_input().request.model_copy(update={"workspace_dir": str(tmp_path)}),
        "metadata": {"state_workspace_dir": str(tmp_path)},
    })
    client = ScriptedModelClient([_call("first"), [_completed("check", message_id="end")]])
    runtime = NativeLantaiRuntime(model_client_factory=lambda _: client, tool_registry=_registry(calls), effect_journal=RuntimeEffectJournal(tmp_path))
    with pytest.raises(_RuntimeDurabilityBlocked):
        _ = [event async for event in runtime.run_turn(request)]
    assert calls == ["once"]
    monkeypatch.setattr(RuntimeRunStore, "save_contract", original)
    client = ScriptedModelClient([_call("second"), [_completed("check", message_id="end-2")]])
    runtime = NativeLantaiRuntime(model_client_factory=lambda _: client, tool_registry=_registry(calls), effect_journal=RuntimeEffectJournal(tmp_path))
    request = request.model_copy(update={"request": request.request.model_copy(update={"run_id": "turn-2"})})
    _ = [event async for event in runtime.run_turn(request)]
    assert calls == ["once"]


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_workspace", [False, True])
async def test_runtime_key_reaches_server_and_is_inherited_after_response_loss(tmp_path, empty_workspace):
    physical_calls = []
    request_keys = []
    applied_keys = set()

    def server(value):
        context = current_runtime_effect_context()
        key = context.idempotency_key if context is not None else None
        request_keys.append(key)
        if key is None or key not in applied_keys:
            applied_keys.add(key)
            physical_calls.append(value)
            raise OSError("response lost")
        return "already applied"

    registry = LantaiToolRegistry()
    registry.register_tool(ToolSpec(
        name="external_write", input_schema={"type": "object"},
        effect_recovery=RuntimeEffectRecoveryCapability(
            external_side_effect=True, idempotency="idempotency_key",
            idempotency_key_transport="runtime_context", probe="tool_specific",
            on_ambiguity="safe_to_retry",
        ),
    ), server)
    for turn, call_id in (("turn-1", "first"), ("turn-2", "second")):
        client = ScriptedModelClient([_call(call_id), [_completed("check", message_id="end")]])
        runtime = NativeLantaiRuntime(model_client_factory=lambda _: client, tool_registry=registry, effect_journal=RuntimeEffectJournal(tmp_path))
        request = _runtime_input().model_copy(update={"request": _runtime_input().request.model_copy(update={"run_id": turn})})
        if empty_workspace:
            request = request.model_copy(update={"request": request.request.model_copy(update={"workspace_dir": ""})})
        _ = [event async for event in runtime.run_turn(request)]
    assert physical_calls == ["once"]
    assert len(request_keys) == 2
    assert request_keys[0] == request_keys[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["intent", "running", "directory"])
@pytest.mark.parametrize("after_write", [False, True])
async def test_dispatch_persistence_fault_never_calls_executor_or_exposes_private_error(tmp_path, monkeypatch, phase, after_write):
    class FailingJournal(RuntimeEffectJournal):
        def _append_envelope(self, path, effect):
            if effect.status == phase:
                if after_write:
                    super()._append_envelope(path, effect)
                raise RuntimeError("PRIVATE_DISPATCH_DETAIL")
            super()._append_envelope(path, effect)

    if phase == "directory":
        def fail_directory(_):
            raise OSError("PRIVATE_DISPATCH_DETAIL")
        monkeypatch.setattr(effect_module, "_fsync_dir", fail_directory)
    calls = []
    client = ScriptedModelClient([_call("first"), [_completed("check", message_id="end")]])
    runtime = NativeLantaiRuntime(model_client_factory=lambda _: client, tool_registry=_registry(calls), effect_journal=FailingJournal(tmp_path))
    events = [event async for event in runtime.run_turn(_runtime_input())]
    assert calls == []
    assert "PRIVATE_DISPATCH_DETAIL" not in json.dumps([event.model_dump(mode="json") for event in events])


@pytest.mark.asyncio
@pytest.mark.parametrize("after_write", [False, True])
async def test_confirmation_failure_blocks_new_turn_with_new_call_id(tmp_path, after_write):
    class FailingConfirmation(RuntimeEffectJournal):
        def _confirm_settlement(self, effect):
            if after_write:
                super()._confirm_settlement(effect)
                self._receipt_path(effect).unlink()
            raise OSError("PRIVATE_CONFIRMATION_DETAIL")

    calls = []
    registry = _registry(calls)
    client = ScriptedModelClient([_call("first"), [_completed("check", message_id="end")]])
    runtime = NativeLantaiRuntime(model_client_factory=lambda _: client, tool_registry=registry, effect_journal=FailingConfirmation(tmp_path))
    with pytest.raises(_RuntimeDurabilityBlocked):
        _ = [event async for event in runtime.run_turn(_runtime_input())]
    client = ScriptedModelClient([_call("second"), [_completed("check", message_id="end-2")]])
    runtime = NativeLantaiRuntime(model_client_factory=lambda _: client, tool_registry=registry, effect_journal=RuntimeEffectJournal(tmp_path))
    request = _runtime_input().model_copy(update={"request": _runtime_input().request.model_copy(update={"run_id": "turn-2"})})
    _ = [event async for event in runtime.run_turn(request)]
    assert calls == ["once"]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["intent", "committed"])
async def test_artifact_persistence_fault_preserves_real_receipt_and_blocks_restart(tmp_path, phase):
    class FailingArtifact(RuntimeArtifactEffectJournal):
        def append(self, effect):
            if effect.status == phase:
                raise OSError("PRIVATE_ARTIFACT_DETAIL")
            return super().append(effect)

    calls = []
    def execute(value):
        calls.append(value)
        artifact = tmp_path / "deliverables" / "result.txt"
        artifact.parent.mkdir(exist_ok=True)
        artifact.write_text("external result")
        return {"path": str(artifact), "receipt": "physical receipt"}
    registry = LantaiToolRegistry()
    registry.register_tool(ToolSpec(
        name="external_write", input_schema={"type": "object"},
        output_semantics=ToolOutputSemantics(artifact_role="primary", result_path_fields=("path",)),
        effect_recovery=RuntimeEffectRecoveryCapability(external_side_effect=True, idempotency="not_idempotent", probe="manual", on_ambiguity="manual_disambiguation"),
    ), execute)
    client = ScriptedModelClient([_call("first"), [_completed("check", message_id="end")]])
    runtime = NativeLantaiRuntime(model_client_factory=lambda _: client, tool_registry=registry, effect_journal=RuntimeEffectJournal(tmp_path), artifact_effect_journal=FailingArtifact(tmp_path))
    request = _runtime_input().model_copy(update={"request": _runtime_input().request.model_copy(update={"workspace_dir": str(tmp_path)})})
    events = [event async for event in runtime.run_turn(request)]
    completed = next(event for event in events if event.type == "tool.call.completed")
    assert "physical receipt" in json.dumps(completed.payload)
    assert completed.payload["metadata"]["automatic_retry_allowed"] is False
    assert "PRIVATE_ARTIFACT_DETAIL" not in json.dumps([event.model_dump(mode="json") for event in events])
    client = ScriptedModelClient([_call("second"), [_completed("check", message_id="end-2")]])
    runtime = NativeLantaiRuntime(model_client_factory=lambda _: client, tool_registry=registry, effect_journal=RuntimeEffectJournal(tmp_path))
    request = request.model_copy(update={"request": request.request.model_copy(update={"run_id": "turn-2"})})
    _ = [event async for event in runtime.run_turn(request)]
    assert calls == ["once"]


def test_removed_effect_ledger_is_detected_after_process_restart(tmp_path):
    journal = RuntimeEffectJournal(tmp_path)
    journal.append_effect(_effect(status="running", revision=1))
    journal.ledger_path("run-1", "attempt-1").unlink()
    restarted = RuntimeEffectJournal(tmp_path)
    with pytest.raises(RuntimeEffectJournalCorruptionError):
        restarted.assert_effect_admissible(_effect().model_copy(update={"run_id": "run-2", "effect_id": "new-id"}), RuntimeEffectRecoveryCapability(external_side_effect=True))


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["partial_append", "removed_ledger"])
async def test_corrupt_prior_journal_blocks_current_attempt_completion(
    tmp_path, corruption,
):
    run_id = "run-after-corrupt-journal"
    chat_id = "chat-corrupt-journal"
    session_id = "session-1"
    contract = await record_chat_runtime_run_started(
        workspace_dir=tmp_path,
        agent_id="default",
        chat_id=chat_id,
        session_id=session_id,
        run_id=run_id,
        effective_workspace_dir=str(tmp_path),
        task_contract=build_chat_task_contract(
            objective="Check the previous write before retrying.",
            client_turn_id="client-corrupt-journal",
            complexity="light",
            work_method_id="standard",
        ),
    )
    assert contract is not None
    previous = _effect().model_copy(update={
        "run_id": "run-before-corrupt-journal",
        "attempt_id": "run-before-corrupt-journal:chat-attempt-1",
        "source": "chat",
        "owner": contract.owner,
        "tool_name": "external_write",
    })
    journal = RuntimeEffectJournal(tmp_path)
    journal.append_effect(previous)
    journal.append_effect(previous.model_copy(update={
        "status": "running", "revision": 1,
    }))
    ledger = journal.ledger_path(previous.run_id, previous.attempt_id)
    if corruption == "partial_append":
        with ledger.open("ab") as handle:
            handle.write(ledger.read_bytes()[:16])
        corrupted_bytes = ledger.read_bytes()
    else:
        ledger.unlink()
        corrupted_bytes = None

    calls = []
    client = ScriptedModelClient([
        _call("new-id-after-corrupt-journal"),
        [_completed("Check complete.", message_id="final-corrupt-journal")],
    ])
    runtime_input = _runtime_input().model_copy(update={
        "request": _runtime_input().request.model_copy(update={
            "run_id": run_id,
            "chat_id": chat_id,
            "user_id": contract.owner.user_id,
            "workspace_dir": str(tmp_path),
        }),
        "metadata": {
            "state_workspace_dir": str(tmp_path),
            "attempt_id": contract.attempt_id,
            "runtime_lease_owner": contract.lease_owner,
        },
    })
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _: client,
        tool_registry=_registry(calls),
        effect_journal=RuntimeEffectJournal(tmp_path),
    )
    events = [event async for event in runtime.run_turn(runtime_input)]
    completed = next(e for e in events if e.type == "tool.call.completed")
    assert calls == []
    assert completed.status == "failed"
    runner = AgentRunner(agent_id="default", workspace_dir=tmp_path)
    for event in events:
        if event.type in {"tool.call.started", "tool.call.completed"}:
            await runner._persist_native_runtime_control_event(
                event=event,
                request_context={"chat_id": chat_id, "run_id": run_id},
                workspace_dir=tmp_path,
                session_id=session_id,
                attempt_id=contract.attempt_id,
            )
    attempt_events = build_runtime_run_events_response(
        [tmp_path], run_id, attempt_id=contract.attempt_id,
    ).events
    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id=run_id,
        chat_id=chat_id,
        response_status="completed",
        assistant_text="Check complete.",
        final_response_id="final-corrupt-journal",
        runtime_events=attempt_events,
        durable_artifact_required=False,
    )

    assert result.allow_success is False
    settlement = next(r for r in result.acceptance_results
                      if r["id"] == "effects_settled")
    assert settlement["passed"] is False
    assert "unconfirmed_tool_effect" in settlement["detail"]
    metadata = completed.payload["metadata"]
    assert metadata["invocation_status"] == "not_started"
    assert metadata["side_effect_outcome"] == "known_not_applied"
    assert metadata["automatic_retry_allowed"] is False
    assert metadata["requires_human"] is True
    assert metadata["effect_settlement_status"] == "unconfirmed"
    assert metadata["error_code"] == "effect_journal_corrupt"
    # A corrupt journal cannot supply a trustworthy prior effect identity.
    assert not completed.payload.get("canonical_tool", {}).get("effect_id")
    persisted = next(e for e in attempt_events
                     if e.payload.get("details", {}).get("native_runtime_event_id")
                     == completed.id)
    assert persisted.payload["attempt_id"] == contract.attempt_id
    assert persisted.payload["metadata"] == metadata
    assert RuntimeEffectJournal(tmp_path).list_effects(
        run_id, contract.attempt_id,
    ) == []
    if corrupted_bytes is None:
        assert not ledger.exists()
    else:
        assert ledger.read_bytes() == corrupted_bytes


def test_concurrent_new_call_ids_share_one_external_dispatch_claim(tmp_path):
    recovery = RuntimeEffectRecoveryCapability(external_side_effect=True, idempotency="not_idempotent")
    def claim(index):
        journal = RuntimeEffectJournal(tmp_path)
        record = _effect(status="running", revision=1).model_copy(update={"run_id": f"run-{index}", "effect_id": f"effect-{index}"})
        try:
            journal.append_effect_if_admissible(record, recovery)
            return "admitted"
        except RuntimeEffectUnresolvedError:
            return "blocked"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, [1, 2])) == ["admitted", "blocked"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault,canonical", [("confirmation_fsync", False), ("confirmation_fsync", True), ("checkpoint", True)])
async def test_completion_receipt_never_advertises_retry_before_durable_confirmation(tmp_path, monkeypatch, fault, canonical):
    calls = []
    request = _runtime_input().model_copy(update={
        "request": _runtime_input().request.model_copy(update={"workspace_dir": str(tmp_path)}),
        "metadata": {"state_workspace_dir": str(tmp_path)},
    })
    if canonical:
        task = build_chat_task_contract(objective="write once", client_turn_id="client-1", complexity="simple", work_method_id="standard")
        contract = await record_chat_runtime_run_started(workspace_dir=tmp_path, agent_id="default", chat_id="chat-1", session_id="session-1", run_id="turn-1", effective_workspace_dir=str(tmp_path), task_contract=task)
        request = request.model_copy(update={"metadata": {**request.metadata, "attempt_id": contract.attempt_id, "runtime_lease_owner": contract.lease_owner}})
    if fault == "checkpoint":
        original = RuntimeRunStore.save_contract
        async def fail_checkpoint(self, contract, **kwargs):
            if contract.checkpoint is not None and contract.checkpoint.stage in {"tool_boundary", "failed"}:
                raise RuntimeRunStoreError("PRIVATE_COMPLETION_DETAIL")
            return await original(self, contract, **kwargs)
        monkeypatch.setattr(RuntimeRunStore, "save_contract", fail_checkpoint)
    else:
        original_fsync = effect_module._fsync_dir
        def fail_receipt_fsync(path):
            if path.name == "receipts" and any(path.iterdir()):
                raise OSError("PRIVATE_COMPLETION_DETAIL")
            return original_fsync(path)
        monkeypatch.setattr(effect_module, "_fsync_dir", fail_receipt_fsync)
    client = ScriptedModelClient([_call("first"), [_completed("check", message_id="end")]])
    runtime = NativeLantaiRuntime(model_client_factory=lambda _: client, tool_registry=_registry(calls), effect_journal=RuntimeEffectJournal(tmp_path))
    events = []
    with pytest.raises(_RuntimeDurabilityBlocked):
        async for event in runtime.run_turn(request):
            events.append(event)
    completed = [event for event in events if event.type == "tool.call.completed"]
    assert calls == ["once"]
    assert len(completed) == 1
    metadata = completed[0].payload["metadata"]
    assert metadata["side_effect_outcome"] == "committed"
    assert metadata["automatic_retry_allowed"] is False
    assert metadata["effect_settlement_status"] == "unconfirmed"
    assert metadata["requires_probe"] is True
    assert metadata["requires_human"] is True
    assert completed[0].payload["canonical_tool"]["effect_id"] == "effect:first"
    assert "applied" in json.dumps(completed[0].payload)
    assert "PRIVATE_COMPLETION_DETAIL" not in json.dumps([event.model_dump(mode="json") for event in events])
