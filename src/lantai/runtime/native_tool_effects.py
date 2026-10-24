"""Native tool Effect intent, admission and settlement without model semantics."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable

from .coherence import parse_or_build_tool_envelope
from .effects import (
    RuntimeEffectReducerAction, RuntimeEffectRecoveryCapability,
    RuntimeEffectJournalError, RuntimeEffectUnresolvedError,
    assess_runtime_effect_ambiguity, reduce_runtime_effect,
)
from .input import RuntimeInput
from .events import RuntimeEvent
from .mac_use_effects import interpret_mac_use_effect
from .native_turn_support import (
    _approval_binding_for_tool_call, _public_tool_result_metadata,
    _runtime_owner, _runtime_release_edition, _stable_sha256,
    _RuntimeDurabilityBlocked,
)
from .runs import RuntimeEffectRecord
from .tool_policy import ToolPolicyOutcome
from .tools import ToolCall, ToolSpec, ToolResult


class NativeToolEffectMixin:
    """Narrow deterministic portion of NativeLantaiRuntime's tool port."""

    def _record_tool_effect_intent(
        self,
        tool_call: ToolCall,
        spec: ToolSpec,
        outcome: ToolPolicyOutcome,
        *,
        runtime_input: RuntimeInput,
        turn_id: str,
        approval_id: str | None = None,
    ) -> RuntimeEffectRecord | None:
        if self._effect_journal is None:
            return None
        binding = _approval_binding_for_tool_call(
            tool_call=tool_call,
            spec=spec,
            outcome=outcome,
            runtime_input=runtime_input,
            turn_id=turn_id,
        )
        effect = RuntimeEffectRecord(
            run_id=binding.run_id,
            attempt_id=binding.attempt_id,
            step_id=f"tool-call:{tool_call.id}",
            effect_id=binding.effect_id,
            source="chat",
            owner=_runtime_owner(runtime_input),
            status="intent",
            tool_name=tool_call.name,
            args_hash=_stable_sha256(tool_call.arguments),
            policy_snapshot_hash=binding.policy_snapshot_hash,
            idempotency_key=str(
                tool_call.metadata.get("idempotency_key")
                or f"{binding.run_id}:{binding.attempt_id}:{binding.effect_id}",
            ),
            approval_id=approval_id,
            release_edition=_runtime_release_edition(runtime_input),  # type: ignore[arg-type]
        )
        retry_key = getattr(self._effect_journal, "retry_idempotency_key", None)
        if callable(retry_key) and not tool_call.metadata.get("idempotency_key"):
            inherited_key = retry_key(effect, spec.effect_recovery)
            if inherited_key:
                effect = effect.model_copy(update={"idempotency_key": inherited_key})
        admission = getattr(self._effect_journal, "assert_effect_admissible", None)
        if callable(admission):
            admission(effect, spec.effect_recovery)
        self._effect_journal.append_effect(effect)
        return effect

    def _record_tool_effect_started(
        self,
        effect: RuntimeEffectRecord | None,
        *,
        recovery: RuntimeEffectRecoveryCapability,
    ) -> RuntimeEffectRecord | None:
        if self._effect_journal is None or effect is None:
            return effect
        running = reduce_runtime_effect(
            effect,
            RuntimeEffectReducerAction(
                type="start", approval_id=effect.approval_id
            ),
        ).effect
        try:
            self._effect_journal.append_effect_if_admissible(running, recovery)
        except RuntimeEffectUnresolvedError:
            raise
        except Exception as exc:
            raise RuntimeEffectJournalError("Effect dispatch receipt could not be persisted") from exc
        return running

    def _queue_tool_effect_receipt(self, effect, result) -> None:
        if effect is None or self._effect_journal is None or result.metadata.get("effect_status") != "committed":
            return
        latest = self._effect_journal.latest_effect(effect.run_id, effect.attempt_id, effect.effect_id)
        if latest is None:
            return
        pending = getattr(self, "_pending_tool_effect_receipts", None)
        if pending is None:
            pending = self._pending_tool_effect_receipts = {}
        pending[(effect.run_id, effect.attempt_id, effect.effect_id)] = (latest, self._tool_result_model_message(result))

    def _checkpoint_tool_result_context(self, turn_id, messages):
        existing = {message.tool_call_id for message in messages if message.role == "tool"}
        extra = [message for (run_id, _, _), (_, message) in getattr(self, "_pending_tool_effect_receipts", {}).items()
                 if run_id == turn_id and message.tool_call_id not in existing]
        return [*messages, *extra]

    def _confirm_checkpoint_tool_receipts(self, turn_id, stage, checkpoint=None) -> None:
        if stage not in {"tool_boundary", "completed", "interrupted", "failed"}:
            return
        pending = getattr(self, "_pending_tool_effect_receipts", {})
        persisted = set(checkpoint.compressed_context) if checkpoint is not None else None
        for key, (effect, message) in list(pending.items()):
            if key[0] != turn_id:
                continue
            if checkpoint is not None and (effect.effect_id not in checkpoint.effect_ids or json.dumps(message.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")) not in persisted):
                continue
            try:
                self._effect_journal.confirm_effect_receipt(effect)
            except Exception as exc:
                raise RuntimeEffectJournalError("Effect receipt confirmation could not be persisted") from exc
            pending.pop(key, None)

    async def _durable_tool_completion_events(
        self,
        transaction: AsyncIterator[tuple[RuntimeEvent, ToolResult]],
        *,
        persist_checkpoint: Callable[..., Awaitable[None]],
        runtime_input: RuntimeInput,
        turn_id: str,
        client_turn_id: str,
        spec: ToolSpec | None,
    ) -> AsyncIterator[tuple[RuntimeEvent, ToolResult]]:
        """Confirm the checkpoint and receipt before publishing completion."""
        from .tool_transaction import _unconfirmed_receipt_result

        async for event, result in transaction:
            pending = getattr(self, "_pending_tool_effect_receipts", {})
            key = next((key for key in pending
                        if key[0] == turn_id and key[2] == result.metadata.get("effect_id")), None)
            if event.type == "tool.call.completed" and key is not None:
                effect, _ = pending[key]
                try:
                    await persist_checkpoint("tool_boundary", force=True, next_execution_position="after_tool")
                    if key in pending:
                        raise RuntimeEffectJournalError("Checkpoint lacks the corresponding tool receipt")
                except Exception as exc:
                    recovery = spec.effect_recovery if spec is not None else RuntimeEffectRecoveryCapability(
                        external_side_effect=True, probe="manual", on_ambiguity="manual_disambiguation",
                    )
                    result = _unconfirmed_receipt_result(
                        result, effect, recovery, exc,
                        "Tool receipt confirmation failed after execution.", settled_effect=True,
                    )
                    yield self._tool_completed_event(
                        result, runtime_input=runtime_input, turn_id=turn_id,
                        client_turn_id=client_turn_id, seq=event.seq,
                    ), result
                    if isinstance(exc, _RuntimeDurabilityBlocked):
                        raise
                    raise _RuntimeDurabilityBlocked(
                        "tool_boundary", kind="persistence_error", cause=exc, attempts=1,
                    ) from exc
            yield event, result

    def _record_tool_effect_settled(
        self,
        effect: RuntimeEffectRecord | None,
        result: ToolResult,
        *,
        recovery: RuntimeEffectRecoveryCapability,
    ) -> ToolResult:
        result_metadata = dict(result.metadata)
        side_effect_outcome = str(
            result_metadata.get("side_effect_outcome") or ""
        )
        ambiguity = (
            assess_runtime_effect_ambiguity(
                recovery,
                effect_id=effect.effect_id if effect is not None else None,
            )
            if side_effect_outcome == "unknown"
            else None
        )
        result_metadata.update(
            {
                "automatic_retry_allowed": (
                    False
                    if result_metadata.get("effect_settlement_status") == "unconfirmed"
                    else
                    ambiguity.automatic_external_call_allowed
                    if ambiguity is not None
                    else True
                ),
                "requires_probe": bool(
                    ambiguity is not None and ambiguity.requires_probe
                    or result_metadata.get("effect_settlement_status") == "unconfirmed"
                    and result_metadata.get("requires_probe")
                ),
                "requires_human": bool(
                    ambiguity is not None and ambiguity.requires_human
                    or result_metadata.get("effect_settlement_status") == "unconfirmed"
                    and result_metadata.get("requires_human")
                ),
                **(
                    {"effect_recovery_reason": ambiguity.reason}
                    if ambiguity is not None
                    else {}
                ),
            }
        )
        result = result.model_copy(update={"metadata": result_metadata})
        if self._effect_journal is None or effect is None:
            return result
        effect_records = (
            self._effect_journal.list_effects(effect.run_id, effect.attempt_id)
            if result.name == "mac_verify"
            and result_metadata.get("resolves_external_effect_id")
            and result_metadata.get("external_effect_status")
            in {"verified", "no_effect_verified"}
            else ()
        )
        mac_effect = interpret_mac_use_effect(
            effect_records,
            result_name=result.name,
            metadata=result_metadata,
        )
        if mac_effect.verification_target is not None:
            resolved = reduce_runtime_effect(
                mac_effect.verification_target,
                RuntimeEffectReducerAction(
                    type=(
                        "probe_committed"
                        if mac_effect.external_status == "verified"
                        else "mark_safe_to_retry"
                    ),
                    result_hash=_stable_sha256(
                        {
                            "external_effect_id": (
                                mac_effect.resolves_external_effect_id
                            ),
                            "verification": mac_effect.external_status,
                            "tool_call": result.call_id,
                        },
                    ),
                ),
            ).effect
            self._effect_journal.append_effect(resolved)
            result_metadata["resolved_effect_id"] = resolved.effect_id
            result = result.model_copy(update={"metadata": result_metadata})
        settlement_type = (
            "cancel"
            if result_metadata.get("invocation_status") == "not_started"
            else "commit"
            if side_effect_outcome == "committed" or result.ok
            else "mark_ambiguous"
            if side_effect_outcome == "unknown"
            else "fail"
        )
        if mac_effect.ambiguity_reason is not None:
            settlement_type = "mark_ambiguous"
            side_effect_outcome = "unknown"
            result_metadata["side_effect_outcome"] = side_effect_outcome
            ambiguity = assess_runtime_effect_ambiguity(
                recovery,
                effect_id=effect.effect_id,
            )
            result_metadata.update(
                {
                    "automatic_retry_allowed": (
                        ambiguity.automatic_external_call_allowed
                    ),
                    "requires_probe": ambiguity.requires_probe,
                    "requires_human": ambiguity.requires_human,
                    "effect_recovery_reason": ambiguity.reason,
                }
            )
            result = result.model_copy(update={"metadata": result_metadata})
        settled = reduce_runtime_effect(
            effect,
            RuntimeEffectReducerAction(
                type=settlement_type,
                result_hash=_stable_sha256(
                    {
                        "ok": result.ok,
                        "content": result.content,
                        "error": result.error,
                        "metadata": _public_tool_result_metadata(
                            result.metadata
                        ),
                    },
                ),
                ambiguity_reason=(
                    mac_effect.ambiguity_reason
                    or (
                        ambiguity.reason
                        if settlement_type == "mark_ambiguous"
                        and ambiguity is not None
                        else None
                    )
                ),
            ),
        ).effect
        self._effect_journal.append_effect(settled)
        if (
            settled.status == "ambiguous"
            and ambiguity is not None
            and ambiguity.automatic_external_call_allowed
            and recovery.idempotency != "idempotency_key"
        ):
            settled = reduce_runtime_effect(
                settled,
                RuntimeEffectReducerAction(
                    type="mark_safe_to_retry",
                    result_hash=settled.result_hash,
                    ambiguity_reason=settled.ambiguity_reason,
                ),
            ).effect
            self._effect_journal.append_effect(settled)
        settled_result = result.model_copy(
            update={
                "metadata": {
                    **result.metadata,
                    "effect_id": settled.effect_id,
                    "attempt_id": settled.attempt_id,
                    "effect_revision": settled.revision,
                    "effect_status": settled.status,
                },
            },
        )
        envelope = parse_or_build_tool_envelope(settled_result)
        return settled_result.model_copy(
            update={
                "metadata": {
                    **settled_result.metadata,
                    "canonical_tool_envelope": envelope.model_dump(
                        mode="json"
                    ),
                },
            },
        )
