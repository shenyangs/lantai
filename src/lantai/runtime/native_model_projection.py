"""Model request and event projection shared by the native adapter."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, AsyncIterator
from uuid import uuid4
from .events import RuntimeEvent
from .input import RuntimeInput, RuntimeRequestContext
from .models import ModelStreamEvent, ModelMessage, ModelContentBlock
from .tools import ToolPolicyContext, ToolResult
from .coherence import parse_or_build_tool_envelope
from .native_turn_support import _tool_result_output
from .native_turn_support import _runtime_metadata, _DeadlineBudget, _runtime_usage, _runtime_message_from_model_response, _model_response_has_output, _message_from_delta, _tool_preamble_public_progress_event, _agent_presentation_context


class NativeModelProjectionMixin:
    def _policy_context_for_turn(
        self,
        runtime_input: RuntimeInput,
        *,
        turn_id: str,
        budget: _DeadlineBudget,
    ) -> ToolPolicyContext:
        request = runtime_input.request
        base_payload = dict(self._base_policy_context)
        input_payload = dict(runtime_input.tool_policy_context or {})
        base_request_context = dict(
            base_payload.pop("request_context", {}) or {}
        )
        input_request_context = dict(
            input_payload.pop("request_context", {}) or {}
        )
        base_metadata = dict(base_payload.pop("metadata", {}) or {})
        input_metadata = dict(input_payload.pop("metadata", {}) or {})

        request_context = {
            **base_request_context,
            **input_request_context,
            **self._request_context_payload(request, turn_id=turn_id),
        }
        runtime_metadata = _runtime_metadata(runtime_input)
        request_context.update(runtime_metadata)

        metadata = {
            **base_metadata,
            **input_metadata,
            **runtime_metadata,
        }
        if budget.deadline_monotonic is not None:
            metadata["turn_deadline_monotonic"] = budget.deadline_monotonic

        payload = {
            **base_payload,
            **input_payload,
            "agent_id": request.agent_id,
            "session_id": request.session_id,
            "root_session_id": request.root_session_id or request.session_id,
            "chat_id": request.chat_id,
            "turn_id": turn_id,
            "workspace_dir": request.workspace_dir
            or base_payload.get("workspace_dir"),
            "request_context": request_context,
            "metadata": metadata,
        }
        return ToolPolicyContext.model_validate(payload)

    @staticmethod
    def _request_context_payload(
        request: RuntimeRequestContext,
        *,
        turn_id: str,
    ) -> dict[str, Any]:
        payload = {
            key: value
            for key, value in request.model_dump(
                mode="python",
                exclude_none=True,
            ).items()
            if key != "metadata" and value not in ({}, [])
        }
        payload["turn_id"] = turn_id
        payload.update(dict(request.metadata or {}))
        return payload

    def _model_request_metadata(
        self,
        *,
        request: RuntimeRequestContext,
        turn_id: str,
        budget: _DeadlineBudget,
        runtime_metadata: dict[str, Any] | None = None,
        request_ordinal: int | str | None = None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "session_id": request.session_id,
            "turn_id": turn_id,
            "run_id": request.run_id or turn_id,
            "model_request_created_at": datetime.now(timezone.utc).isoformat(),
        }
        source_metadata = dict(runtime_metadata or {})
        for key in (
            "state_workspace_dir",
            "attempt_id",
            "runtime_generation",
            "runtime_revision",
            "runtime_lease_owner",
            "contract_hash",
            "policy_snapshot_hash",
            "client_turn_id",
            "harness_mode",
        ):
            value = source_metadata.get(key)
            if value not in (None, ""):
                metadata[key] = value
        if request_ordinal is not None:
            metadata[
                "model_request_id"
            ] = f"{request.run_id or turn_id}:model:{request_ordinal}"
        if budget.deadline_monotonic is not None:
            metadata["turn_deadline_monotonic"] = budget.deadline_monotonic
        return metadata

    async def _runtime_events_from_model_event(
        self,
        model_event: ModelStreamEvent,
        *,
        runtime_input: RuntimeInput,
        turn_id: str,
        seq_start: int,
        client_turn_id: str = "",
    ) -> AsyncIterator[RuntimeEvent]:
        request = runtime_input.request
        seq = seq_start
        if model_event.kind == "provider_activity":
            # Provider heartbeats and empty semantic chunks keep the liveness
            # watchdog fresh without becoming assistant transcript content.
            # Surface one truthful status row for a round so a long ping-only
            # wait is observable without claiming semantic model progress.
            if not model_event.metadata.get("first_provider_activity"):
                return
            upstream_model_confirmed = model_event.metadata.get(
                "upstream_model_confirmed",
                True,
            )
            activity_scope = str(
                model_event.metadata.get("activity_scope")
                or "provider_event"
            )
            seq += 1
            yield RuntimeEvent(
                id=str(uuid4()),
                seq=seq,
                type="runtime.warning",
                status="info",
                source="system",
                session_id=request.session_id,
                turn_id=turn_id,
                summary=(
                    "上游连接仍有响应，尚未收到可交付内容。"
                    if upstream_model_confirmed
                    else (
                        "传输链路仍有活动，但尚不能确认模型正在生成内容。"
                    )
                ),
                payload={
                    "kind": "model_transport_activity",
                    "details": {
                        "activity_scope": activity_scope,
                        "semantic_output": False,
                        "upstream_model_confirmed": bool(
                            upstream_model_confirmed
                        ),
                    },
                },
            )
            return
        if model_event.kind == "message_start":
            source_message_id = model_event.message_id
            seq += 1
            yield RuntimeEvent(
                id=str(uuid4()),
                seq=seq,
                type="message.started",
                status="running",
                source="assistant",
                session_id=request.session_id,
                turn_id=turn_id,
                item_id=model_event.message_id,
                payload={
                    "message_id": model_event.message_id,
                    "source_message_id": source_message_id,
                    **(
                        {"client_turn_id": client_turn_id}
                        if client_turn_id
                        else {}
                    ),
                },
            )
            return

        if (
            model_event.kind in {"thinking_delta", "text_delta"}
            and model_event.delta
        ):
            block_type = (
                "thinking" if model_event.kind == "thinking_delta" else "text"
            )
            stream_message_type = (
                "reasoning"
                if model_event.kind == "thinking_delta"
                else "message"
            )
            message = _message_from_delta(
                message_id=model_event.message_id,
                block_type=block_type,
                delta=model_event.delta,
            )
            source_message_id = model_event.message_id or message.id
            message.metadata = {
                **dict(message.metadata or {}),
                **(
                    {"source_message_id": source_message_id}
                    if source_message_id
                    else {}
                ),
                **_agent_presentation_context(runtime_input),
                **(
                    {"client_turn_id": client_turn_id}
                    if client_turn_id
                    else {}
                ),
            }
            seq += 1
            yield RuntimeEvent(
                id=str(uuid4()),
                seq=seq,
                type="message.delta",
                status="running",
                source="assistant",
                session_id=request.session_id,
                turn_id=turn_id,
                item_id=model_event.message_id,
                summary=model_event.delta,
                payload={
                    "message": message.model_dump(mode="json"),
                    **(
                        {"source_message_id": source_message_id}
                        if source_message_id
                        else {}
                    ),
                    "delta": model_event.delta,
                    "content_type": block_type,
                    "stream_message_type": stream_message_type,
                },
            )
            return

        if model_event.kind == "usage":
            seq += 1
            yield RuntimeEvent(
                id=str(uuid4()),
                seq=seq,
                type="runtime.warning",
                status="info",
                source="system",
                session_id=request.session_id,
                turn_id=turn_id,
                item_id=model_event.message_id,
                summary="model usage",
                payload={"kind": "model_usage"},
                usage=_runtime_usage(model_event.usage),
            )
            return

        if (
            model_event.kind == "message_completed"
            and model_event.response is not None
        ):
            if not _model_response_has_output(model_event.response):
                return
            message = _runtime_message_from_model_response(
                model_event.response,
                strip_tool_public_progress=(request.agent_id == "default"),
            )
            source_message_id = model_event.message_id or message.id
            message.metadata = {
                **dict(message.metadata or {}),
                **(
                    {"source_message_id": source_message_id}
                    if source_message_id
                    else {}
                ),
                **_agent_presentation_context(runtime_input),
                **(
                    {"client_turn_id": client_turn_id}
                    if client_turn_id
                    else {}
                ),
            }
            seq += 1
            yield RuntimeEvent(
                id=str(uuid4()),
                seq=seq,
                type="message.completed",
                status="completed",
                source="assistant",
                session_id=request.session_id,
                turn_id=turn_id,
                item_id=model_event.message_id or message.id,
                summary=message.text_content(),
                payload={
                    "message": message.model_dump(mode="json"),
                    **(
                        {"source_message_id": source_message_id}
                        if source_message_id
                        else {}
                    ),
                },
                usage=_runtime_usage(model_event.usage),
            )
            public_progress = _tool_preamble_public_progress_event(
                model_event.response,
                runtime_input=runtime_input,
                turn_id=turn_id,
                seq=seq + 1,
                client_turn_id=client_turn_id,
                allow_structured=(request.agent_id == "default"),
            )
            if public_progress is not None:
                seq += 1
                yield public_progress
            return

        if model_event.kind == "error":
            seq += 1
            yield RuntimeEvent(
                id=str(uuid4()),
                seq=seq,
                type="runtime.error",
                status="failed",
                source="system",
                session_id=request.session_id,
                turn_id=turn_id,
                item_id=model_event.message_id,
                summary=model_event.error or "model error",
                payload={
                    "error_type": "model_error",
                    "has_raw_payload": bool(model_event.raw),
                },
            )

    @staticmethod
    def _tool_result_model_message(result: ToolResult) -> ModelMessage:
        envelope = parse_or_build_tool_envelope(result)
        return ModelMessage(
            id=result.call_id,
            role="tool",
            tool_call_id=result.call_id,
            content=[
                ModelContentBlock(
                    type="tool_result",
                    data={
                        "id": result.call_id,
                        "name": result.name,
                        "output": _tool_result_output(result),
                        "canonical_value_sha256": (
                            envelope.canonical.value_sha256
                        ),
                        "tool_projection_id": envelope.envelope_id,
                    },
                    metadata={
                        "instruction_trust": "untrusted_tool_evidence",
                        "canonical_tool_envelope_sha256": (
                            envelope.integrity_sha256
                        ),
                    },
                ),
            ],
            metadata={
                "tool_call_id": result.call_id,
                "instruction_trust": "untrusted_tool_evidence",
                "canonical_tool_envelope_sha256": envelope.integrity_sha256,
            },
        )

