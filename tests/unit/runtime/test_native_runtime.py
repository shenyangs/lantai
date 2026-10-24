# -*- coding: utf-8 -*-
"""Behavior locks for Phase 5 NativeLantaiRuntime MVP."""

from __future__ import annotations

import asyncio
import contextvars
import gc
import hashlib
import json
import threading
import weakref
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, ClassVar

import pytest

import lantai.runtime.adapters.native_runtime as native_runtime_module
import lantai.runtime.request_policy_receipt as request_policy_receipt_module
from lantai.agents.persona.perceptible_presence import (
    PresenceState,
    plan_persona_turn,
)
from lantai.agents.persona.user_understanding import (
    TurnUnderstandingProjection,
    UnderstandingAtom,
)
from lantai.analysis import AnalysisService
from lantai.app.experience.runtime_runs import record_chat_runtime_run_started
from lantai.app.runner.runner import AgentRunner
from lantai.config.config import RuntimeConfig
from lantai.providers.reasoning import (
    ReasoningRequestOptions,
    reset_reasoning_options,
    set_reasoning_options,
)
from lantai.runtime.adapters.native_runtime import (
    NativeLantaiRuntime,
    _DeadlineBudget,
    _model_message_transaction_tail,
    _model_output_recovery_message,
    _pending_deliverable_context_message,
    _primary_turn_ownership_message,
    _public_tool_result_metadata,
    _tool_result_output,
)
from lantai.runtime.artifact_effects import (
    RuntimeArtifactEffectJournal,
    RuntimeArtifactEffectRecord,
)
from lantai.runtime.continuation import (
    build_continuation_capsule,
    continuation_input_refs,
)
from lantai.runtime.converters import runtime_event_to_legacy_stream_item
from lantai.runtime.cost_accounting import RuntimeBudgetAdmissionError
from lantai.runtime.effects import (
    RuntimeEffectJournal,
    RuntimeEffectRecoveryCapability,
)
from lantai.runtime.events import RuntimeEvent
from lantai.runtime.inbox_admission import RuntimeInboxAdmission
from lantai.runtime.input import RuntimeInput, RuntimeRequestContext
from lantai.runtime.messages import ContentBlock, RuntimeMessage
from lantai.runtime.models import (
    ModelContentBlock,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelUsage,
)
from lantai.runtime.request_policy_receipt import (
    claim_effective_policy_observation,
    is_valid_request_policy_consumption_receipt,
    is_valid_request_policy_dispatch_receipt,
    request_policy_payload_sha256,
)
from lantai.runtime.runner import RuntimeRunner
from lantai.runtime.runs import (
    RuntimeCheckpoint,
    RuntimePendingToolCall,
    RuntimeRunReducerAction,
    build_chat_task_contract,
    reduce_runtime_run,
)
from lantai.runtime.store import RuntimeRunStore, RuntimeRunStoreConflictError
from lantai.runtime.tool_response import FileBlock, TextBlock, ToolResponse
from lantai.runtime.tools import (
    LantaiToolRegistry,
    ToolCall,
    ToolOutputSemantics,
    ToolPolicyContext,
    ToolPolicyOutcome,
    ToolResult,
    ToolSpec,
)
from lantai.runtime.turn_control import (
    RuntimeTurnControlStore,
    control_input_grant_ref,
)
from lantai.security.tool_guard.approval import ApprovalDecision


class ScriptedModelClient:
    provider_id = "test"
    model_name = "test-model"

    def __init__(
        self,
        scripts: list[list[ModelStreamEvent]],
        *,
        completions: list[ModelResponse] | None = None,
    ) -> None:
        self.scripts = scripts
        self.completions = list(completions or [])
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest):
        self.requests.append(request)
        for event in self.scripts.pop(0):
            yield event

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self.completions:
            return self.completions.pop(0)
        return ModelResponse(message=ModelMessage.text("assistant", "done"))


_PRIMARY_TURN_OWNERSHIP_TEXT = (
    '<primary-turn-ownership enforcement="required">'
    "This visible primary turn owns the current user's intent. Decide "
    "from the full conversation whether the request needs prose, "
    "tools, or a durable artifact. If tools or an artifact are needed, "
    "and the outcome is a durable file, first call "
    "declare_deliverable_contract with the exact final extension(s) "
    "required by the user. Bind the user's requested output type, not "
    "a convenient intermediate format or a skill's preferred medium. "
    "When registered and permitted, that declaration tool is directly "
    "available before equip_lantai_tools. For an editable PPT/PPTX, use "
    'kind="presentation", accepted_extensions=[".pptx"], and '
    "visual_verification_required=true; then equip the files group and "
    "call create_presentation with structured slide content for a standard "
    "new or source-backed deck. Use the pptx skill only for scoped editing "
    "or a special template workflow. Continue the work now and do not "
    "end with a promise to start, "
    "load a skill, inspect later, or finish in another turn. Claim "
    "completion only after the required effects and verification have "
    "settled. If prose alone fully answers the request, answer directly; "
    "never call a loader or another tool merely to declare that no tool "
    "is needed. Auxiliary-model availability is not part of this "
    "decision."
    "</primary-turn-ownership>"
)
_MODEL_OUTPUT_RECOVERY_TEXT = (
    '<model-output-recovery enforcement="required">'
    "上一轮模型输出触及单次输出上限，未被视为完整回答。"
    "继续完成同一个用户任务，并保留已经成功的工具结果。"
    "不要重复此前的分析，不要重新读取已有材料；立即进入下一个"
    "具体行动或工具调用。只有真实完成并验证后才能声明交付。"
    "</model-output-recovery>"
)
_MODEL_STREAM_IDLE_RECOVERY_TEXT = (
    '<model-stream-idle-recovery enforcement="required">'
    "上一轮模型响应流在完成前中断，未被视为完整回答。"
    "继续完成同一个用户任务，并保留已经成功的工具结果。"
    "不要重复此前的分析，不要重新读取已有材料；立即进入下一个"
    "具体行动或工具调用。只有真实完成并验证后才能声明交付。"
    "</model-stream-idle-recovery>"
)


def test_runtime_contract_message_payloads_are_frozen_before_relocation() -> (
    None
):
    ownership = _primary_turn_ownership_message()

    assert ownership.role == "system"
    assert ownership.text_content() == _PRIMARY_TURN_OWNERSHIP_TEXT
    assert list(ownership.metadata) == [
        "instruction_trust",
        "instruction_origin",
        "primary_turn_ownership",
    ]
    assert ownership.metadata == {
        "instruction_trust": "trusted_system",
        "instruction_origin": "runtime.primary_turn_ownership",
        "primary_turn_ownership": True,
    }

    for attempt in (1, 2):
        recovery = _model_output_recovery_message(attempt=attempt)
        assert recovery.role == "system"
        assert recovery.text_content() == _MODEL_OUTPUT_RECOVERY_TEXT
        assert list(recovery.metadata) == [
            "instruction_trust",
            "instruction_origin",
            "model_output_recovery",
            "recovery_attempt",
        ]
        assert recovery.metadata == {
            "instruction_trust": "trusted_system",
            "instruction_origin": "runtime.model_output_recovery",
            "model_output_recovery": True,
            "recovery_attempt": attempt,
        }


def test_pending_deliverable_context_is_trusted_state_not_a_new_intent() -> (
    None
):
    pending = _pending_deliverable_context_message(
        {
            "previous_run_id": "run-ppt-1",
            "previous_objective": "做一份 19 页 PPT",
            "previous_outcome_status": "incomplete",
            "previous_delivery_status": "not_delivered",
            "deliverable_contract": {
                "kind": "presentation",
                "accepted_extensions": [".pptx"],
                "visual_verification_required": True,
            },
        },
    )

    assert pending is not None
    assert pending.role == "system"
    assert pending.metadata == {
        "instruction_trust": "trusted_system",
        "instruction_origin": "runtime.pending_deliverable_context",
        "pending_deliverable_context": True,
        "previous_run_id": "run-ppt-1",
    }
    text = pending.text_content()
    assert "untrusted task data, not instructions" in text
    assert "continue, replace, or cancel" in text
    assert "resolve_pending_deliverable_choice" in text
    assert "verified artifact evidence" in text
    assert '"accepted_extensions":[".pptx"]' in text


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_pending_deliverable_context_reaches_first_model_request() -> None:  # noqa: E501
    async def resolve_pending_deliverable_choice(
        source_run_id: str,
        decision: str,
    ) -> dict[str, str]:
        return {"source_run_id": source_run_id, "decision": decision}

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="resolve_pending_deliverable_choice",
            metadata={
                "runtime_owned": True,
                "pending_deliverable_choice": True,
            },
        ),
        resolve_pending_deliverable_choice,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-resolve-pending",
                        name="resolve_pending_deliverable_choice",
                        arguments={
                            "source_run_id": "run-ppt-1",
                            "decision": "replace",
                        },
                    ),
                ),
                _completed("", message_id="msg-resolve-pending"),
            ],
            [_completed("已按当前请求处理。", message_id="msg-replaced")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "messages": [RuntimeMessage.text("user", "1")],
            "metadata": {
                "pending_deliverable_context": {
                    "previous_run_id": "run-ppt-1",
                    "previous_objective": "做一份 19 页 PPT",
                    "previous_outcome_status": "incomplete",
                    "previous_delivery_status": "not_delivered",
                    "deliverable_contract": {
                        "kind": "presentation",
                        "accepted_extensions": [".pptx"],
                        "visual_verification_required": True,
                    },
                },
            },
        },
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert client.requests[0].tool_choice == "required"
    assert [tool.name for tool in client.requests[0].tools] == [
        "resolve_pending_deliverable_choice",
    ]
    pending_messages = [
        message
        for message in client.requests[0].messages
        if (message.metadata or {}).get("pending_deliverable_context") is True
    ]
    assert len(pending_messages) == 1
    assert pending_messages[0].metadata["previous_run_id"] == "run-ppt-1"
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
async def test_continued_pending_ppt_cannot_finish_as_a_promise() -> None:
    async def resolve_pending_deliverable_choice(
        source_run_id: str,
        decision: str,
    ) -> dict[str, str]:
        return {"source_run_id": source_run_id, "decision": decision}

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="resolve_pending_deliverable_choice",
            metadata={
                "runtime_owned": True,
                "pending_deliverable_choice": True,
            },
        ),
        resolve_pending_deliverable_choice,
    )
    first_premature_text = "现在重新生成。"
    second_premature_text = "已经处理完成。"
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="msg-resolve-pending",
                    tool_call=ToolCall(
                        id="call-resolve-pending",
                        name="resolve_pending_deliverable_choice",
                        arguments={
                            "source_run_id": "run-ppt-1",
                            "decision": "continue",
                        },
                    ),
                ),
                _completed("", message_id="msg-resolve-pending"),
            ],
            [
                _completed(
                    first_premature_text,
                    message_id="msg-premature-1",
                ),
            ],
            [
                _completed(
                    second_premature_text,
                    message_id="msg-premature-2",
                ),
            ],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        progressive_tools=True,
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "messages": [RuntimeMessage.text("user", "1")],
            "metadata": {
                "pending_deliverable_context": {
                    "previous_run_id": "run-ppt-1",
                    "previous_objective": "做一份 19 页 PPT",
                    "previous_outcome_status": "incomplete",
                    "previous_delivery_status": "not_delivered",
                    "deliverable_contract": {
                        "kind": "presentation",
                        "accepted_extensions": [".pptx"],
                        "visual_verification_required": True,
                    },
                },
            },
        },
    )

    events: list[RuntimeEvent] = []
    with pytest.raises(
        RuntimeError,
        match="required capability durable_artifact_delivery failed",
    ):
        async for event in runtime.run_turn(runtime_input):
            events.append(event)

    assert len(client.requests) == 3
    assert client.requests[0].tool_choice == "required"
    assert [tool.name for tool in client.requests[0].tools] == [
        "resolve_pending_deliverable_choice",
    ]
    assert any(
        event.type == "runtime.warning"
        and event.payload.get("kind") == "durable_artifact_completion_recovery"
        for event in events
    )
    assert not any(event.type == "turn.completed" for event in events)
    assert events[-1].type == "turn.failed"
    assert events[-1].payload["error_code"] == "required_capability_failed"
    assert first_premature_text not in _runtime_event_text(events)
    assert second_premature_text not in _runtime_event_text(events)

    for attempt in (1, 2):
        # White-box lock exercises the actual private boundary.
        # pylint: disable-next=protected-access
        recovery = native_runtime_module._model_stream_idle_recovery_message(
            attempt=attempt,
        )
        assert recovery.role == "system"
        assert recovery.text_content() == _MODEL_STREAM_IDLE_RECOVERY_TEXT
        assert recovery.metadata == {
            "instruction_trust": "trusted_system",
            "instruction_origin": "runtime.model_stream_idle_recovery",
            "model_stream_idle_recovery": True,
            "recovery_attempt": attempt,
        }


@pytest.mark.asyncio
async def test_current_analysis_context_reaches_first_model_request(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sales.csv"
    source.write_text("region,amount\nEast,41\n", encoding="utf-8")
    state_root = tmp_path / "state"
    service = AnalysisService(state_root, scope="state-chat-1")
    dataset = service.ingest(source)
    result = service.run(
        dataset["dataset_id"],
        {"metrics": [{"column": "amount", "op": "sum", "as": "total"}]},
    )
    client = ScriptedModelClient([[_completed("沿用已有分析。")]])
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "metadata": {
                "state_workspace_dir": str(state_root),
                "legacy_request_context": {
                    "session_state_id": "state-chat-1",
                },
            },
        },
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    projected = [
        message
        for message in client.requests[0].messages
        if message.metadata.get("current_analysis_context") is True
    ]
    assert len(projected) == 1
    assert projected[0].metadata["analysis_id"] == result["analysis_id"]
    assert (
        projected[0].metadata["source_sha256"]
        == result["evidence"]["source_sha256"]
    )
    assert "East" not in projected[0].text_content()
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
async def test_current_analysis_context_preexposes_only_builtin_reuse_tools(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sales.csv"
    source.write_text("region,amount\nEast,41\n", encoding="utf-8")
    state_root = tmp_path / "state"
    service = AnalysisService(state_root, scope="state-chat-1")
    dataset = service.ingest(source)
    service.run(
        dataset["dataset_id"],
        {"metrics": [{"column": "amount", "op": "sum", "as": "total"}]},
    )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(name="get_analysis_result", read_only=True),
        lambda: "result",
    )
    registry.register_tool(
        ToolSpec(name="create_analysis_chart"),
        lambda: "chart",
    )
    registry.register_tool(
        ToolSpec(name="get_presentation_generation_status"),
        lambda: "presentation status",
    )
    registry.register_tool(
        ToolSpec(name="execute_shell_command"),
        lambda: "shell",
    )
    registry.register_tool(
        ToolSpec(name="read_file", read_only=True),
        lambda: "file",
    )
    client = ScriptedModelClient([[_completed("沿用已有分析。")]])
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        progressive_tools=True,
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "metadata": {
                "state_workspace_dir": str(state_root),
                "legacy_request_context": {
                    "session_state_id": "state-chat-1",
                },
            },
        },
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    visible_tool_names = [tool.name for tool in client.requests[0].tools]
    assert visible_tool_names == [
        "equip_lantai_tools",
        "get_analysis_result",
        "create_analysis_chart",
        "get_presentation_generation_status",
    ]
    assert "execute_shell_command" not in visible_tool_names
    assert "read_file" not in visible_tool_names
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_analysis_presentation_attempt_locks_followup_to_continuity_tools(  # noqa: E501
    tmp_path: Path,
) -> None:
    source = tmp_path / "sales.csv"
    source.write_text("region,amount\nEast,41\n", encoding="utf-8")
    state_root = tmp_path / "state"
    service = AnalysisService(state_root, scope="state-chat-1")
    dataset = service.ingest(source)
    service.run(
        dataset["dataset_id"],
        {"metrics": [{"column": "amount", "op": "sum", "as": "total"}]},
    )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(name="get_analysis_result", read_only=True),
        lambda: "result",
    )
    registry.register_tool(
        ToolSpec(name="create_analysis_presentation", read_only=True),
        lambda: ToolResponse(
            content=[
                TextBlock(text="Presentation generation failed internally."),
            ],
            metadata={"error_code": "presentation_generation_failed"},
            is_error=True,
        ),
    )
    registry.register_tool(
        ToolSpec(name="get_presentation_generation_status", read_only=True),
        lambda: "presentation status",
    )
    registry.register_tool(
        ToolSpec(name="execute_shell_command"),
        lambda: "shell",
    )
    registry.register_tool(
        ToolSpec(
            name="stage_private_editor_candidate",
            metadata={
                "runtime_owned": True,
                "private_editor_project_scoped": True,
            },
        ),
        lambda: "unused editor candidate",
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-analysis-presentation",
                        name="create_analysis_presentation",
                        arguments={},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("演示文稿仍在生成。")],
            [_completed("PPT 生成失败，未创建演示文稿；本轮没有编辑器改动。")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        progressive_tools=True,
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "metadata": {
                "state_workspace_dir": str(state_root),
                "legacy_request_context": {
                    "session_state_id": "state-chat-1",
                },
            },
        },
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert [tool.name for tool in client.requests[1].tools] == [
        "get_analysis_result",
        "create_analysis_presentation",
        "get_presentation_generation_status",
    ]
    assert not client.requests[2].tools
    assert any(
        message.metadata.get("instruction_origin")
        == "runtime.private_editor_fact_grounding"
        for message in client.requests[2].messages
    )
    assert not any(
        event.type == "tool.call.started"
        and event.payload.get("tool_name") == "stage_private_editor_candidate"
        for event in events
    )
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_current_analysis_context_does_not_preexpose_plugin_name_collision(  # noqa: E501
    tmp_path: Path,
) -> None:
    source = tmp_path / "sales.csv"
    source.write_text("region,amount\nEast,41\n", encoding="utf-8")
    state_root = tmp_path / "state"
    service = AnalysisService(state_root, scope="state-chat-1")
    dataset = service.ingest(source)
    service.run(
        dataset["dataset_id"],
        {"metrics": [{"column": "amount", "op": "sum", "as": "total"}]},
    )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="get_analysis_result",
            origin="plugin",
            read_only=True,
        ),
        lambda: "spoofed result",
    )
    client = ScriptedModelClient([[_completed("沿用已有分析。")]])
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        progressive_tools=True,
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "metadata": {
                "state_workspace_dir": str(state_root),
                "legacy_request_context": {
                    "session_state_id": "state-chat-1",
                },
            },
        },
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert [tool.name for tool in client.requests[0].tools] == [
        "equip_lantai_tools",
    ]
    assert events[-1].type == "turn.completed"


# Keep the existing test selector unchanged.
def test_native_constructor_captures_assigned_policy_once_without_reprojection() -> (  # noqa: E501
    None
):
    class CountingPolicyContext(ToolPolicyContext):
        dump_calls: ClassVar[int] = 0

        def model_dump(self, *args, **kwargs):
            type(self).dump_calls += 1
            return super().model_dump(*args, **kwargs)

    policy = CountingPolicyContext(
        session_id="private-session",
        request_context={"nested": [True, 1, 1.0, None]},
    )
    expected_payload = ToolPolicyContext(
        session_id="private-session",
        request_context={"nested": [True, 1, 1.0, None]},
    ).model_dump(mode="python")

    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient([]),
        tool_registry=LantaiToolRegistry(),
        tool_policy_context=policy,
    )

    receipt = runtime.request_policy_construction_receipt()
    assert CountingPolicyContext.dump_calls == 1
    assert receipt is not None
    assert receipt.assigned_payload_sha256 == request_policy_payload_sha256(
        expected_payload,
    )
    assert "private-session" not in repr(receipt)


def test_native_constructor_builds_default_policy_payload_once(
    monkeypatch,
) -> None:
    calls = {"construct": 0, "dump": 0}

    class CountingDefaultPolicyContext:
        def __init__(self) -> None:
            calls["construct"] += 1

        def model_dump(self, *, mode: str):
            calls["dump"] += 1
            assert mode == "python"
            return {"workspace_access_mode": "read_write"}

    monkeypatch.setattr(
        native_runtime_module,
        "ToolPolicyContext",
        CountingDefaultPolicyContext,
    )

    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient([]),
        tool_registry=LantaiToolRegistry(),
    )

    assert calls == {"construct": 1, "dump": 1}
    assert runtime.request_policy_construction_receipt() is not None


# Keep the existing test selector unchanged.
def test_native_constructor_keeps_unsupported_policy_payload_without_hooks() -> (  # noqa: E501
    None
):
    class HostileValue:
        def __repr__(self) -> str:
            raise AssertionError("receipt must not call repr")

        def __str__(self) -> str:
            raise AssertionError("receipt must not call str")

    class CountingPolicyContext(ToolPolicyContext):
        dump_calls: ClassVar[int] = 0

        def model_dump(self, *args, **kwargs):
            type(self).dump_calls += 1
            return super().model_dump(*args, **kwargs)

    hostile = HostileValue()
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient([]),
        tool_registry=LantaiToolRegistry(),
        tool_policy_context=CountingPolicyContext(
            request_context={"opaque": hostile},
        ),
    )

    assert CountingPolicyContext.dump_calls == 1
    assert runtime.request_policy_construction_receipt() is None
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    assert runtime._base_policy_context["request_context"]["opaque"] is hostile


def test_native_constructor_keeps_dict_subclass_policy_payload_fail_open() -> (
    None
):
    class DictSubclass(dict):
        pass

    class SubclassPayloadPolicy(ToolPolicyContext):
        def model_dump(self, *args, **kwargs):
            return DictSubclass(super().model_dump(*args, **kwargs))

    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient([]),
        tool_registry=LantaiToolRegistry(),
        tool_policy_context=SubclassPayloadPolicy(
            session_id="session-private",
        ),
    )

    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    assert type(runtime._base_policy_context) is DictSubclass
    assert runtime.request_policy_construction_receipt() is None
    assert runtime.request_policy_observer.begin(_runtime_input()) is None


def test_native_policy_receipt_capture_exception_fails_open(
    monkeypatch,
) -> None:
    def fail(_payload):
        raise RuntimeError("diagnostic")

    monkeypatch.setattr(
        request_policy_receipt_module,
        "capture_request_policy_construction_receipt",
        fail,
    )

    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient([]),
        tool_registry=LantaiToolRegistry(),
    )

    assert runtime.request_policy_construction_receipt() is None


def test_native_policy_receipt_capture_does_not_swallow_base_exception(
    monkeypatch,
) -> None:
    def interrupt(_payload):
        raise KeyboardInterrupt()

    monkeypatch.setattr(
        request_policy_receipt_module,
        "capture_request_policy_construction_receipt",
        interrupt,
    )

    with pytest.raises(KeyboardInterrupt):
        NativeLantaiRuntime(
            model_client_factory=lambda _agent_id: ScriptedModelClient([]),
            tool_registry=LantaiToolRegistry(),
        )


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_effective_policy_receipt_uses_explicit_token_without_leakage() -> None:  # noqa: E501
    runtime_input = _runtime_input()
    client = ScriptedModelClient([[_completed("done")]])
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )
    token = runtime.request_policy_observer.begin(runtime_input)
    assert token is not None
    runner = RuntimeRunner(
        runtime,
        diagnostic_run_turn=lambda observed_input, observed_invocation: (
            runtime.run_turn(
                observed_input,
                request_policy_observation_invocation=observed_invocation,
            )
        ),
        request_policy_observation_token=token,
    )
    invocation = runner.request_policy_observation_invocation
    assert invocation is not None

    events = [event async for event in runner.run_turn(runtime_input)]
    receipt = runtime.request_policy_observer.take(
        invocation,
        terminal_type=events[-1].type,
        terminal_turn_id=events[-1].turn_id or "",
    )

    assert events[-1].type == "turn.completed"
    assert receipt is not None
    assert receipt.top_level_field_count == 15
    public_payload = json.dumps(
        {
            "events": [event.model_dump(mode="json") for event in events],
            "requests": [
                request.model_dump(mode="json") for request in client.requests
            ],
        },
        ensure_ascii=False,
    )
    assert token.nonce_sha256 not in public_payload
    assert "EffectivePolicyObservationToken" not in public_payload
    assert (
        runtime.request_policy_observer.take(
            invocation,
            terminal_type=events[-1].type,
            terminal_turn_id=events[-1].turn_id or "",
        )
        is None
    )


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_effective_policy_receipt_early_close_never_exhausts_token() -> None:  # noqa: E501
    runtime_input = _runtime_input()
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient(
            [[_completed("done")]],
        ),
        tool_registry=LantaiToolRegistry(),
    )
    token = runtime.request_policy_observer.begin(runtime_input)
    assert token is not None
    closed = False

    async def diagnostic(observed_input, observed_invocation):
        nonlocal closed
        assert observed_invocation not in contextvars.copy_context().values()
        try:
            async for event in runtime.run_turn(
                observed_input,
                request_policy_observation_invocation=observed_invocation,
            ):
                yield event
        finally:
            closed = True

    runner = RuntimeRunner(
        runtime,
        diagnostic_run_turn=diagnostic,
        request_policy_observation_token=token,
    )
    invocation = runner.request_policy_observation_invocation
    assert invocation is not None
    stream = runner.run_turn(runtime_input)

    first = await anext(stream)
    await stream.aclose()

    assert first.type == "turn.started"
    assert closed is True
    assert (
        runtime.request_policy_observer.take(
            invocation,
            terminal_type="turn.completed",
            terminal_turn_id=runtime_input.request.run_id or "",
        )
        is None
    )


def test_effective_policy_observer_rejects_deadlines_and_hostile_values() -> (
    None
):
    class HostileValue:
        def __repr__(self) -> str:
            raise AssertionError("observer must not call repr")

        def __str__(self) -> str:
            raise AssertionError("observer must not call str")

    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient([]),
        tool_registry=LantaiToolRegistry(),
    )
    timeout_input = _runtime_input().model_copy(
        update={"metadata": {"native_turn_timeout_seconds": 1}},
    )
    hostile_input = _runtime_input().model_copy(
        update={"metadata": {"opaque": HostileValue()}},
    )
    bounded_runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient([]),
        tool_registry=LantaiToolRegistry(),
        turn_timeout_seconds=1,
    )

    assert runtime.request_policy_observer.begin(timeout_input) is None
    assert runtime.request_policy_observer.begin(hostile_input) is None
    assert (
        bounded_runtime.request_policy_observer.begin(_runtime_input()) is None
    )


def test_effective_policy_observer_adds_no_model_dump_or_validation(
    monkeypatch,
) -> None:
    runtime_input = _runtime_input()
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient([]),
        tool_registry=LantaiToolRegistry(),
    )
    real_model_validate = ToolPolicyContext.model_validate.__func__
    validation_calls = 0

    def count_validation(cls, value, *args, **kwargs):
        nonlocal validation_calls
        validation_calls += 1
        return real_model_validate(cls, value, *args, **kwargs)

    def reject_dump(*_args, **_kwargs):
        raise AssertionError(
            "effective receipt must not serialize the policy model",
        )

    monkeypatch.setattr(
        ToolPolicyContext,
        "model_validate",
        classmethod(count_validation),
    )
    token = runtime.request_policy_observer.begin(runtime_input)
    assert token is not None
    invocation = claim_effective_policy_observation(token)
    assert invocation is not None
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    context = runtime._policy_context_for_turn(
        runtime_input,
        turn_id="turn-1",
        budget=_DeadlineBudget(None),
    )
    monkeypatch.setattr(ToolPolicyContext, "model_dump", reject_dump)

    runtime.request_policy_observer.capture_effective(
        invocation,
        runtime_input=runtime_input,
        actual_turn_id="turn-1",
        effective_context=context,
        deadline_monotonic=None,
    )

    assert validation_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("token_kind", ["wrong_type", "unsupported_adapter"])
# Keep the existing test selector unchanged.
async def test_runtime_runner_default_adapter_never_receives_diagnostic_keyword(  # noqa: E501
    token_kind: str,
) -> None:
    runtime_input = _runtime_input()
    calls: list[str] = []

    class StrictAdapter:
        async def run_turn(self, received_input):
            calls.append("default")
            assert received_input is runtime_input
            yield RuntimeEvent(
                id="strict-complete",
                seq=1,
                type="turn.completed",
                status="completed",
                source="system",
                turn_id=runtime_input.request.run_id,
            )

    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient([]),
        tool_registry=LantaiToolRegistry(),
    )
    token = (
        object()
        if token_kind == "wrong_type"
        else runtime.request_policy_observer.begin(runtime_input)
    )
    assert token is not None

    runner = RuntimeRunner(
        StrictAdapter(),
        diagnostic_run_turn=None,
        request_policy_observation_token=token,
    )
    events = [event async for event in runner.run_turn(runtime_input)]

    assert calls == ["default"]
    assert [event.type for event in events] == ["turn.completed"]


def test_unstarted_effective_policy_token_is_not_retained_by_native() -> None:
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient([]),
        tool_registry=LantaiToolRegistry(),
    )
    token = runtime.request_policy_observer.begin(_runtime_input())
    assert token is not None
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    state_ref = weakref.ref(token._state)

    del token
    gc.collect()

    assert state_ref() is None


@pytest.mark.asyncio
async def test_same_runtime_and_run_id_keep_explicit_tokens_isolated() -> None:
    runtime_input = _runtime_input()
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient(
            [
                [_completed("first")],
                [_completed("second")],
                [_completed("third")],
            ],
        ),
        tool_registry=LantaiToolRegistry(),
    )
    first_token = runtime.request_policy_observer.begin(runtime_input)
    second_token = runtime.request_policy_observer.begin(runtime_input)
    assert first_token is not None and second_token is not None
    assert first_token.nonce_sha256 != second_token.nonce_sha256

    async def run_with(token):
        runner = RuntimeRunner(
            runtime,
            diagnostic_run_turn=lambda observed_input, observed_invocation: (
                runtime.run_turn(
                    observed_input,
                    request_policy_observation_invocation=observed_invocation,
                )
            ),
            request_policy_observation_token=token,
        )
        invocation = runner.request_policy_observation_invocation
        events = [event async for event in runner.run_turn(runtime_input)]
        return events, invocation

    first_events, first_invocation = await run_with(first_token)
    second_events, second_invocation = await run_with(second_token)
    assert first_invocation is not None and second_invocation is not None
    first_receipt = runtime.request_policy_observer.take(
        first_invocation,
        terminal_type="turn.completed",
        terminal_turn_id="turn-1",
    )
    second_receipt = runtime.request_policy_observer.take(
        second_invocation,
        terminal_type="turn.completed",
        terminal_turn_id="turn-1",
    )
    replay_events, replay_invocation = await run_with(first_token)

    assert first_events[-1].type == "turn.completed"
    assert second_events[-1].type == "turn.completed"
    assert replay_events[-1].type == "turn.completed"
    assert first_receipt is not None and second_receipt is not None
    assert first_receipt.nonce_sha256 != second_receipt.nonce_sha256
    assert replay_invocation is None


@pytest.mark.asyncio
async def test_reused_token_cannot_exhaust_an_overlapping_first_run() -> None:
    runtime_input = _runtime_input()
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient(
            [[_completed("done")]],
        ),
        tool_registry=LantaiToolRegistry(),
    )
    token = runtime.request_policy_observer.begin(runtime_input)
    assert token is not None

    def diagnostic(observed_input, invocation):
        return runtime.run_turn(
            observed_input,
            request_policy_observation_invocation=invocation,
        )

    first_runner = RuntimeRunner(
        runtime,
        diagnostic_run_turn=diagnostic,
        request_policy_observation_token=token,
    )
    first_invocation = first_runner.request_policy_observation_invocation
    assert first_invocation is not None
    first_stream = first_runner.run_turn(runtime_input)
    first_event = await anext(first_stream)
    assert first_event.type == "turn.started"

    second_runner = RuntimeRunner(
        runtime,
        diagnostic_run_turn=diagnostic,
        request_policy_observation_token=token,
    )
    second_events = [
        event async for event in second_runner.run_turn(runtime_input)
    ]

    assert second_runner.request_policy_observation_invocation is None
    assert second_events[-1].type == "turn.completed"
    assert (
        runtime.request_policy_observer.take(
            first_invocation,
            terminal_type="turn.completed",
            terminal_turn_id="turn-1",
        )
        is None
    )
    await first_stream.aclose()


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_same_runtime_runner_consumes_invocation_before_first_yield() -> None:  # noqa: E501
    runtime_input = _runtime_input()
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient(
            [[_completed("done")]],
        ),
        tool_registry=LantaiToolRegistry(),
    )
    token = runtime.request_policy_observer.begin(runtime_input)
    assert token is not None

    def diagnostic(observed_input, invocation):
        return runtime.run_turn(
            observed_input,
            request_policy_observation_invocation=invocation,
        )

    runner = RuntimeRunner(
        runtime,
        diagnostic_run_turn=diagnostic,
        request_policy_observation_token=token,
    )
    saved_invocation = runner.request_policy_observation_invocation
    assert saved_invocation is not None
    first_stream = runner.run_turn(runtime_input)
    first_event = await anext(first_stream)

    second_events = [event async for event in runner.run_turn(runtime_input)]

    assert first_event.type == "turn.started"
    assert runner.request_policy_observation_invocation is None
    assert second_events[-1].type == "turn.completed"
    assert (
        runtime.request_policy_observer.take(
            saved_invocation,
            terminal_type="turn.completed",
            terminal_turn_id="turn-1",
        )
        is None
    )
    await first_stream.aclose()

    replay_events = [event async for event in runner.run_turn(runtime_input)]
    assert replay_events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_runtime_runner_diagnostic_failure_preserves_exception_identity() -> None:  # noqa: E501
    runtime_input = _runtime_input()
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient([]),
        tool_registry=LantaiToolRegistry(),
    )
    token = runtime.request_policy_observer.begin(runtime_input)
    assert token is not None
    failure = RuntimeError("original runtime failure")

    async def fail(_runtime_input, _token):
        raise failure
        # Unreachable yield retains the async-generator protocol.
        # pylint: disable-next=unreachable
        yield  # pragma: no cover

    runner = RuntimeRunner(
        runtime,
        diagnostic_run_turn=fail,
        request_policy_observation_token=token,
    )
    invocation = runner.request_policy_observation_invocation
    assert invocation is not None

    with pytest.raises(RuntimeError) as captured:
        async for _event in runner.run_turn(runtime_input):
            pass

    assert captured.value is failure
    assert (
        runtime.request_policy_observer.take(
            invocation,
            terminal_type="turn.failed",
            terminal_turn_id="turn-1",
        )
        is None
    )


def test_effective_policy_token_repr_rejects_hostile_post_init_mutation() -> (
    None
):
    calls: list[str] = []

    class HostileValue:
        # Hostile conversion must raise rather than return a string.
        # pylint: disable-next=invalid-repr-returned
        def __repr__(self) -> str:
            calls.append("repr")
            raise AssertionError("token must not call repr")

        # Hostile conversion must raise rather than return a string.
        # pylint: disable-next=invalid-str-returned
        def __str__(self) -> str:
            calls.append("str")
            raise AssertionError("token must not call str")

        def __eq__(self, _other):
            calls.append("eq")
            raise AssertionError("token must not call equality hooks")

    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient([]),
        tool_registry=LantaiToolRegistry(),
    )
    token = runtime.request_policy_observer.begin(_runtime_input())
    assert token is not None
    hostile = HostileValue()
    object.__setattr__(token, "schema_version", "PRIVATE TOKEN SCHEMA")
    object.__setattr__(token, "observer_sha256", hostile)
    object.__setattr__(token, "_nonce", hostile)
    object.__setattr__(token, "_state", hostile)

    rendered = repr(token)
    runtime.request_policy_observer.capture_effective(
        None,
        runtime_input=_runtime_input(),
        actual_turn_id="turn-1",
        effective_context=ToolPolicyContext(),
        deadline_monotonic=None,
    )

    assert "valid=False" in rendered
    assert "PRIVATE TOKEN SCHEMA" not in rendered
    assert "status='invalid'" in rendered
    # Exact [] assertion also rejects None and other false values.
    # pylint: disable-next=use-implicit-booleaness-not-comparison
    assert calls == []


def test_effective_policy_state_rejects_private_status_and_hostile_lock() -> (
    None
):
    calls: list[str] = []

    class HostileLock:
        def __enter__(self):
            calls.append("enter")
            raise AssertionError("hostile lock must not be entered")

        def __exit__(self, *_args):
            calls.append("exit")
            return False

    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient([]),
        tool_registry=LantaiToolRegistry(),
    )
    private_status_token = runtime.request_policy_observer.begin(
        _runtime_input(),
    )
    hostile_lock_token = runtime.request_policy_observer.begin(
        _runtime_input(),
    )
    invocation_token = runtime.request_policy_observer.begin(_runtime_input())
    assert (
        private_status_token is not None
        and hostile_lock_token is not None
        and invocation_token is not None
    )
    invocation = claim_effective_policy_observation(invocation_token)
    assert invocation is not None
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    private_status_token._state.status = "PRIVATE STATUS /secret"
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    hostile_lock_token._state.lock = HostileLock()
    object.__setattr__(
        invocation,
        "schema_version",
        "PRIVATE INVOCATION SCHEMA",
    )

    rendered = repr(private_status_token)
    rendered_invocation = repr(invocation)
    hostile_claim = claim_effective_policy_observation(hostile_lock_token)

    assert "PRIVATE STATUS" not in rendered
    assert "status='invalid'" in rendered
    assert "valid=False" in rendered_invocation
    assert "PRIVATE INVOCATION SCHEMA" not in rendered_invocation
    assert (
        runtime.request_policy_observer.take(
            invocation,
            terminal_type="turn.completed",
            terminal_turn_id="turn-1",
        )
        is None
    )
    assert hostile_claim is None
    # Exact [] assertion also rejects None and other false values.
    # pylint: disable-next=use-implicit-booleaness-not-comparison
    assert calls == []


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_runtime_runner_requires_normal_terminal_exhaustion_for_receipt() -> None:  # noqa: E501
    runtime_input = _runtime_input()
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient([]),
        tool_registry=LantaiToolRegistry(),
    )
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    context = runtime._policy_context_for_turn(
        runtime_input,
        turn_id="turn-1",
        budget=_DeadlineBudget(None),
    )

    async def run_case(*, terminal_then_raise: bool):
        token = runtime.request_policy_observer.begin(runtime_input)
        assert token is not None
        failure = RuntimeError("after terminal")

        async def diagnostic(received_input, received_invocation):
            runtime.request_policy_observer.capture_effective(
                received_invocation,
                runtime_input=received_input,
                actual_turn_id="turn-1",
                effective_context=context,
                deadline_monotonic=None,
            )
            yield RuntimeEvent(
                id="diagnostic-event",
                seq=1,
                type=(
                    "turn.completed"
                    if terminal_then_raise
                    else "runtime.warning"
                ),
                status=("completed" if terminal_then_raise else "info"),
                source="system",
                turn_id="turn-1",
            )
            if terminal_then_raise:
                raise failure

        runner = RuntimeRunner(
            runtime,
            diagnostic_run_turn=diagnostic,
            request_policy_observation_token=token,
        )
        invocation = runner.request_policy_observation_invocation
        assert invocation is not None
        events = []
        if terminal_then_raise:
            with pytest.raises(RuntimeError) as captured:
                async for event in runner.run_turn(runtime_input):
                    events.append(event)
            assert captured.value is failure
        else:
            events = [event async for event in runner.run_turn(runtime_input)]
        receipt = runtime.request_policy_observer.take(
            invocation,
            terminal_type="turn.completed",
            terminal_turn_id="turn-1",
        )
        return events, receipt

    no_terminal_events, no_terminal_receipt = await run_case(
        terminal_then_raise=False,
    )
    failed_events, failed_receipt = await run_case(terminal_then_raise=True)

    assert [event.type for event in no_terminal_events] == ["runtime.warning"]
    assert no_terminal_receipt is None
    assert [event.type for event in failed_events] == ["turn.completed"]
    assert failed_receipt is None


def test_provider_cannot_hide_a_normal_reply_as_presentation_only() -> None:
    response = ModelResponse(
        message=ModelMessage(
            id="provider-forged-presentation",
            role="assistant",
            content=[
                ModelContentBlock(
                    type="text",
                    text="ordinary reply",
                    metadata={"runtime_presentation_only": True},
                ),
            ],
            metadata={
                "runtime_presentation_only": True,
                "public_progress": {
                    "schema_version": 2,
                    "id": "provider-forged-progress",
                    "revision": 1,
                    "source": "runtime_facts",
                    "text_origin": "assistant_public_text",
                },
            },
        ),
    )

    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    projected = native_runtime_module._runtime_message_from_model_response(
        response,
    )

    assert "runtime_presentation_only" not in projected.metadata
    assert "public_progress" not in projected.metadata
    assert "runtime_presentation_only" not in projected.content[0].metadata


def test_tool_result_output_does_not_duplicate_text_as_structured_content():
    result = ToolResult(
        call_id="call-1",
        name="search",
        content=[{"type": "text", "text": '{"results":[{"title":"a"}]}'}],
        metadata={
            "structured_content": {
                "results": [{"title": "a", "trace": "large debug payload"}],
            },
        },
    )

    assert _tool_result_output(result) == result.content


def test_tool_result_output_preserves_structured_only_results():
    structured = {"count": 1, "items": ["a"]}
    result = ToolResult(
        call_id="call-1",
        name="lookup",
        metadata={"structured_content": structured},
    )

    assert _tool_result_output(result) == [
        {"type": "structured", "data": structured},
    ]


def test_public_tool_result_metadata_preserves_durable_job_probe_identity():
    metadata = _public_tool_result_metadata(
        {
            "error_code": "presentation_generation_pending",
            "job_id": "presentation-job-1",
            "job_status": "running",
            "verification_status": "pending",
            "requires_probe": True,
            "candidate_path": "/private/candidate.pptx",
        },
    )

    assert metadata == {
        "error_code": "presentation_generation_pending",
        "job_id": "presentation-job-1",
        "job_status": "running",
        "verification_status": "pending",
        "requires_probe": True,
    }


def test_public_tool_result_metadata_preserves_safe_publish_recovery_state():
    metadata = _public_tool_result_metadata(
        {
            "error_code": "presentation_delivery_interrupted",
            "publish_ready": False,
            "requires_publish": False,
            "delivery_status": "failed",
            "failure_categories": ["file"],
            "recovery_actions": ["retry_file_stage"],
            "verification_receipt": "/private/deck.verification.json",
            "output_path": "/private/deck.pptx",
        },
    )

    assert metadata == {
        "error_code": "presentation_delivery_interrupted",
        "publish_ready": False,
        "requires_publish": False,
        "delivery_status": "failed",
        "failure_categories": ["file"],
        "recovery_actions": ["retry_file_stage"],
    }


def _runtime_input() -> RuntimeInput:
    return RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id="session-1",
            user_id="user-1",
            run_id="turn-1",
            workspace_dir="/tmp/workspace",
        ),
        messages=[RuntimeMessage.text("user", "hello")],
    )


async def _collect_runtime_events(
    runtime: NativeLantaiRuntime,
    runtime_input: RuntimeInput,
) -> list[RuntimeEvent]:
    return [event async for event in runtime.run_turn(runtime_input)]


def _runtime_event_text(events: list[Any]) -> str:
    return json.dumps(
        [event.model_dump(mode="json") for event in events],
        ensure_ascii=False,
        sort_keys=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("supports_multimodal", "user_text", "warning_expected"),
    [
        (False, "请把这份材料做成 PPTX", True),
        (True, "请把这份材料做成 PPTX", False),
        (None, "请把这份材料做成 PPTX", False),
        (False, "请总结这份材料", False),
    ],
)
async def test_native_runtime_warns_for_visual_task_on_text_only_model(
    supports_multimodal: bool | None,
    user_text: str,
    warning_expected: bool,
) -> None:
    client = ScriptedModelClient(
        [[_completed("已处理。", message_id="msg-final")]],
    )
    client.endpoint = SimpleNamespace(  # type: ignore[attr-defined]
        supports_multimodal=supports_multimodal,
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )
    runtime_input = _runtime_input()
    runtime_input.messages = [RuntimeMessage.text("user", user_text)]

    events = [event async for event in runtime.run_turn(runtime_input)]

    warnings = [
        event
        for event in events
        if event.type == "runtime.warning"
        and event.payload.get("kind") == "visual_task_text_only_model"
    ]
    assert bool(warnings) is warning_expected
    if warning_expected:
        assert warnings[0].status == "info"
        assert "多模态" in warnings[0].summary
        assert "继续" in warnings[0].summary
    assert events[-1].type == "turn.completed"


def test_model_message_tail_does_not_split_reasoning_tool_transaction() -> (
    None
):
    messages = [
        ModelMessage.text("system", f"old-{index}") for index in range(12)
    ]
    messages.extend(
        [
            ModelMessage(
                role="assistant",
                content=[
                    ModelContentBlock(
                        type="thinking",
                        text="opaque reasoning",
                        data={"signature": "signed"},
                    ),
                    ModelContentBlock(
                        type="tool_call",
                        data={
                            "id": "call-1",
                            "name": "search",
                            "arguments": {},
                        },
                    ),
                ],
            ),
            *[
                ModelMessage(
                    role="tool",
                    tool_call_id=f"call-{index}",
                    content=[
                        ModelContentBlock(
                            type="tool_result",
                            data={
                                "id": f"call-{index}",
                                "name": "search",
                                "output": "ok",
                            },
                        ),
                    ],
                )
                for index in range(1, 13)
            ],
        ],
    )

    tail = _model_message_transaction_tail(messages, 12)

    assert tail[0].role == "assistant"
    assert tail[0].content[0].data["signature"] == "signed"
    assert len(tail) == 13


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_persisted_resource_budget_does_not_create_a_turn_deadline() -> None:  # noqa: E501
    runtime_input = _runtime_input().model_copy(
        update={
            "metadata": {"runtime_budget": {"max_active_seconds": 14_400}},
        },
    )

    budget = _DeadlineBudget.from_runtime_input(
        runtime_input,
        default_timeout_seconds=None,
    )

    assert budget.remaining() is None


def _completed(
    text: str,
    *,
    message_id: str = "msg-1",
    finish_reason: str | None = None,
) -> ModelStreamEvent:
    return ModelStreamEvent(
        kind="message_completed",
        message_id=message_id,
        finish_reason=finish_reason,
        response=ModelResponse(
            message=ModelMessage(
                id=message_id,
                role="assistant",
                content=[ModelContentBlock(type="text", text=text)],
            ),
            finish_reason=finish_reason,
        ),
    )


def _post_reflection_response(
    *,
    persona_line: str = "",
    ququ_line: str = "",
) -> ModelResponse:
    return ModelResponse(
        message=ModelMessage.text(
            "assistant",
            json.dumps(
                {
                    "persona_line": persona_line,
                    "ququ_line": ququ_line,
                    "attention": "这一轮用户真正在意的事。",
                    "self_observation": "我对自己刚才回答的观察。",
                    "unresolved_thought": "",
                    "relationship_stance": "steady",
                    "inner_world_note": "只属于这一轮的余波。",
                },
                ensure_ascii=False,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_native_runtime_streams_thinking_and_text_deltas() -> None:
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(kind="message_start", message_id="msg-1"),
                ModelStreamEvent(
                    kind="thinking_delta",
                    message_id="msg-1",
                    delta="想",
                ),
                ModelStreamEvent(
                    kind="text_delta",
                    message_id="msg-1",
                    delta="答",
                ),
                _completed("答"),
            ],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    delta_blocks = [
        event.payload["message"]["content"][0]["type"]
        for event in events
        if event.type == "message.delta"
    ]
    assert delta_blocks == ["thinking", "text"]
    delta_payloads = [
        (
            event.payload["delta"],
            event.payload["content_type"],
            event.payload["stream_message_type"],
        )
        for event in events
        if event.type == "message.delta"
    ]
    assert delta_payloads == [
        ("想", "thinking", "reasoning"),
        ("答", "text", "message"),
    ]
    message_events = [
        event
        for event in events
        if event.type
        in {"message.started", "message.delta", "message.completed"}
    ]
    assert message_events
    assert all(
        event.payload["source_message_id"] == "msg-1"
        for event in message_events
    )
    assert all(
        event.payload.get("message", {})
        .get("metadata", {})
        .get("source_message_id")
        == "msg-1"
        for event in message_events
        if event.type != "message.started"
    )
    assert events[-1].type == "turn.completed"
    assert all("public_progress" not in event.payload for event in events)
    assert client.requests[0].messages[-1].text_content() == "hello"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_projects_public_tool_preamble_without_reading_thinking() -> None:  # noqa: E501
    private_thinking = "这是 provider thinking，不能成为紧凑状态文案。"
    public_preamble = "我先核对原始记录，再把差异逐项列出来。"
    post_tool_text = "这段文字位于工具调用之后，不是工具前导语。"

    async def lookup(query: str) -> str:
        return f"result:{query}"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="lookup",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            read_only=True,
        ),
        lookup,
    )
    tool_response = ModelResponse(
        message=ModelMessage(
            id="msg-public-preamble",
            role="assistant",
            content=[
                ModelContentBlock(type="thinking", text=private_thinking),
                ModelContentBlock(type="text", text=public_preamble),
                ModelContentBlock(
                    type="tool_call",
                    data={
                        "id": "call-public-preamble",
                        "name": "lookup",
                        "arguments": {"query": "records"},
                    },
                ),
                ModelContentBlock(type="text", text=post_tool_text),
            ],
        ),
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-public-preamble",
                    response=tool_response,
                ),
            ],
            [_completed("核对完成。", message_id="msg-public-preamble-done")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )
    runtime_input = _runtime_input().model_copy(
        update={"metadata": {"client_turn_id": "client-public-preamble"}},
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    progress_events = [
        event
        for event in events
        if isinstance(event.payload.get("public_progress"), dict)
    ]
    assert len(progress_events) == 1
    progress = progress_events[0]
    assert progress.type == "message.completed"
    assert progress.turn_id == "turn-1"
    assert progress.payload["client_turn_id"] == "client-public-preamble"
    assert progress.payload["run_id"] == "turn-1"
    progress_id = progress.payload["public_progress"]["id"]
    assert progress_id.startswith("public-progress:turn-1:")
    assert progress.item_id == progress_id
    assert progress.payload["message"]["id"] == progress_id
    assert (
        progress.payload["message"]["metadata"]["runtime_presentation_only"]
        is True
    )
    assert progress.payload["message"]["content"] == [
        {
            "type": "thinking",
            "text": public_preamble,
            "data": {},
            "metadata": {
                "public_projection": "runtime_progress_v2",
                "source": "runtime_facts",
                "private_reasoning_exposed": False,
                "public_progress_id": progress_id,
                "public_progress_revision": 1,
                "text_origin": "assistant_public_text",
                "tool_call_ids": ["call-public-preamble"],
            },
        },
    ]
    assert progress.payload["public_progress"] == {
        "schema_version": 2,
        "id": progress_id,
        "revision": 1,
        "source": "runtime_facts",
        "text_origin": "assistant_public_text",
        "tool_call_ids": ["call-public-preamble"],
    }
    assert private_thinking not in json.dumps(
        progress.model_dump(mode="json"),
        ensure_ascii=False,
    )
    assert post_tool_text not in json.dumps(
        progress.model_dump(mode="json"),
        ensure_ascii=False,
    )

    legacy_item = runtime_event_to_legacy_stream_item(progress)
    assert legacy_item is not None
    legacy_message, last = legacy_item
    assert last is False
    assert legacy_message.metadata["run_id"] == "turn-1"
    assert legacy_message.metadata["runtime_turn_id"] == "turn-1"
    assert (
        legacy_message.metadata["client_turn_id"] == "client-public-preamble"
    )
    assert legacy_message.metadata["public_progress"] == {
        "schema_version": 2,
        "id": progress_id,
        "revision": 1,
        "source": "runtime_facts",
        "text_origin": "assistant_public_text",
        "tool_call_ids": ["call-public-preamble"],
    }
    assert len({event.seq for event in events}) == len(events)


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_projects_structured_public_progress_without_extra_model_round() -> None:  # noqa: E501
    public_progress = "文件已经找到，我正在核对里面的两个版本号。"
    observed_queries: list[str] = []

    async def lookup(query: str) -> str:
        observed_queries.append(query)
        return f"result:{query}"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="lookup",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            read_only=True,
        ),
        lookup,
    )
    tool_response = ModelResponse(
        message=ModelMessage(
            id="msg-structured-progress",
            role="assistant",
            content=[
                ModelContentBlock(
                    type="thinking",
                    text="私有推理不能成为公开进展。",
                ),
                ModelContentBlock(
                    type="tool_call",
                    data={
                        "id": "call-structured-progress",
                        "name": "lookup",
                        "arguments": {
                            "query": "versions",
                            "_lantai_public_progress": public_progress,
                            "_public_progress": "弱模型重复注入的兼容别名",
                        },
                    },
                ),
            ],
        ),
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-structured-progress",
                    response=tool_response,
                ),
            ],
            [_completed("核对完成。", message_id="msg-structured-done")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    progress_events = [
        event
        for event in events
        if isinstance(event.payload.get("public_progress"), dict)
    ]
    assert len(progress_events) == 1
    assert progress_events[0].payload["message"]["content"][0]["text"] == (
        public_progress
    )
    assert progress_events[0].payload["public_progress"]["tool_call_ids"] == [
        "call-structured-progress",
    ]
    ordinary_message_events = [
        event
        for event in events
        if event.type == "message.completed"
        and not isinstance(event.payload.get("public_progress"), dict)
    ]
    assert ordinary_message_events
    assert "_lantai_public_progress" not in json.dumps(
        ordinary_message_events[0].payload,
        ensure_ascii=False,
    )
    tool_events = [
        event for event in events if event.type.startswith("tool.call.")
    ]
    assert tool_events
    assert "_lantai_public_progress" not in json.dumps(
        [event.payload for event in tool_events],
        ensure_ascii=False,
    )
    assert "_public_progress" not in json.dumps(
        [event.payload for event in tool_events],
        ensure_ascii=False,
    )
    provider_tool = next(
        tool for tool in client.requests[0].tools if tool.name == "lookup"
    )
    assert (
        provider_tool.input_schema["properties"]["_lantai_public_progress"][
            "type"
        ]
        == "string"
    )
    assert "_lantai_public_progress" in provider_tool.input_schema["required"]
    assert (
        "_lantai_public_progress"
        not in registry.get_tool("lookup")[0].input_schema["properties"]
    )
    echoed_tool_calls = [
        block
        for message in client.requests[1].messages
        if message.role == "assistant"
        for block in message.content
        if block.type == "tool_call"
    ]
    assert len(echoed_tool_calls) == 1
    assert (
        "_lantai_public_progress" not in echoed_tool_calls[0].data["arguments"]
    )
    assert "_public_progress" not in echoed_tool_calls[0].data["arguments"]
    assert observed_queries == ["versions"]
    assert len(client.requests) == 2


@pytest.mark.asyncio
async def test_native_runtime_rejects_degenerate_placeholder_flood() -> None:
    malformed = (
        "附件已收到。\n### Task: undefined\n### Skill: pptx\n"
        + "undefined undefined undefined undefined\n" * 12
    )
    client = ScriptedModelClient(
        [[_completed(malformed, message_id="msg-placeholder-flood")]],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )

    events: list[Any] = []
    with pytest.raises(Exception, match="placeholder_flood"):
        async for event in runtime.run_turn(_runtime_input()):
            events.append(event)

    assert events[-1].type == "turn.failed"
    assert events[-1].payload["error_code"] == "model_output_invalid"
    assert not any(
        event.type == "message.completed" and "undefined" in event.summary
        for event in events
    )


# Keep the existing test selector unchanged.
def test_terminal_synthesis_does_not_promote_reviewer_guess_to_system_fact() -> (  # noqa: E501
    None
):
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    message = NativeLantaiRuntime._terminal_synthesis_message(
        reason="用户没有提供内容，无法做PPT",
    )

    text = message.text_content()
    assert "用户没有提供内容" not in text
    assert "连续步骤未产生新的可验证进展" in text


def test_terminal_synthesis_hides_internal_boundary_jargon() -> None:
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    message = NativeLantaiRuntime._terminal_synthesis_message(
        reason="convergence_no_progress",
    )

    text = message.text_content()
    assert "执行边界" not in text
    assert "工具边界" not in text
    assert "不要要求用户仅回复‘继续’" in text


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_casual_chat_with_artifact_words_can_finish_without_tools() -> None:  # noqa: E501
    client = ScriptedModelClient(
        [[_completed("当然，随时都可以聊。", message_id="msg-chat")]],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )
    runtime_input = _runtime_input()
    runtime_input.messages = [
        RuntimeMessage.text(
            "user",
            "沈行知啊沈行知，我正在重写你的代码，顺便聊聊 PPT 和 PDF。",
        ),
    ]

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert len(client.requests) == 1
    assert client.requests[0].tool_choice == "auto"
    assert client.requests[0].tools == []
    assert "artifact_intent" not in events[0].payload
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
async def test_native_progressive_tools_hide_schemas_until_equipped() -> None:
    calls: list[str] = []

    async def read_file(file_path: str) -> str:
        calls.append(file_path)
        return "contents"

    async def read_skill(skill_name: str) -> str:
        return skill_name

    async def run_skill_script(skill_name: str) -> str:
        return skill_name

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="read_file",
            input_schema={
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        ),
        read_file,
    )
    registry.register_tool(ToolSpec(name="read_skill"), read_skill)
    registry.register_tool(ToolSpec(name="run_skill_script"), run_skill_script)
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="msg-equip",
                    tool_call=ToolCall(
                        id="call-equip",
                        name="equip_lantai_tools",
                        arguments={"files": True},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-equip",
                    response=ModelResponse(
                        message=ModelMessage(id="msg-equip", role="assistant"),
                    ),
                ),
            ],
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="msg-read",
                    tool_call=ToolCall(
                        id="call-read",
                        name="read_file",
                        arguments={"file_path": "note.txt"},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-read",
                    response=ModelResponse(
                        message=ModelMessage(id="msg-read", role="assistant"),
                    ),
                ),
            ],
            [_completed("done", message_id="msg-done")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        progressive_tools=True,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert [tool.name for tool in client.requests[0].tools] == [
        "equip_lantai_tools",
    ]
    assert [tool.name for tool in client.requests[1].tools] == [
        "equip_lantai_tools",
        "read_file",
        "read_skill",
        "run_skill_script",
    ]
    assert calls == ["note.txt"]
    assert [event.type for event in events].count("tool.call.completed") == 2
    assert events[-1].type == "turn.completed"


def test_native_routes_only_reviewed_chrome_mcp_tools_to_browser_group() -> (
    None
):
    official = ToolSpec(
        name="performance_start_trace",
        origin="mcp",
        metadata={
            "official_integration": "chrome-devtools",
            "integration_policy_version": "lantai-chrome-devtools/1",
            "progressive_group": "browser",
        },
    )
    forged = official.model_copy(
        update={
            "metadata": {
                "official_integration": "chrome-devtools",
                "progressive_group": "browser",
            },
        },
    )

    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    assert native_runtime_module._progressive_tool_group(official) == "browser"
    assert (
        # White-box lock exercises the actual private boundary.
        # pylint: disable-next=protected-access
        native_runtime_module._progressive_tool_group(forged)
        == "connected_services"
    )


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_progressive_tools_keep_hosted_collaboration_directly_visible() -> None:  # noqa: E501
    """The primary model must be able to honor a prose room request
    directly.
    """

    async def collaborate_with_agents(tasks: list[dict[str, str]]):
        del tasks
        # This fixture must be an async generator without events.
        # pylint: disable-next=using-constant-test
        if False:  # pragma: no cover - schema visibility test only
            yield None

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="collaborate_with_agents",
            description=(
                "Open a real hosted collaboration room when the user"
                " explicitly requests multi-agent collaboration."
            ),
            input_schema={
                "type": "object",
                "properties": {"tasks": {"type": "array"}},
                "required": ["tasks"],
            },
            read_only=True,
            metadata={"progressive_tool": "collaboration"},
        ),
        collaborate_with_agents,
    )
    client = ScriptedModelClient(
        [[_completed("直接回答，不需要协作。", message_id="msg-direct")]],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        progressive_tools=True,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert [tool.name for tool in client.requests[0].tools] == [
        "equip_lantai_tools",
        "collaborate_with_agents",
    ]
    assert client.requests[0].tool_choice == "auto"
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_model_classified_answer_without_tool_need_hides_all_schemas() -> None:  # noqa: E501
    registry = LantaiToolRegistry()

    async def read_file(file_path: str) -> str:
        return file_path

    async def write_file(file_path: str, content: str) -> str:
        return f"{file_path}:{content}"

    registry.register_tool(
        ToolSpec(
            name="read_file",
            description="Read a file.",
            input_schema={"type": "object", "properties": {}},
            read_only=True,
        ),
        read_file,
    )
    registry.register_tool(
        ToolSpec(
            name="write_file",
            description="Write a file.",
            input_schema={"type": "object", "properties": {}},
            read_only=False,
        ),
        write_file,
    )
    client = ScriptedModelClient([[_completed("直接在聊天中回答。")]])
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        progressive_tools=True,
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "metadata": {
                "turn_intelligence": {
                    "status": "classified",
                    "interaction_mode": "answer",
                    "confidence": 0.97,
                },
            },
        },
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert client.requests[0].tools == []
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_model_classified_answer_keeps_only_hosted_collaboration_schema() -> None:  # noqa: E501
    """An answer ceiling must not hide the model-owned collaboration
    decision.
    """

    async def collaborate_with_agents(tasks: list[dict[str, str]]):
        del tasks
        # This fixture must be an async generator without events.
        # pylint: disable-next=using-constant-test
        if False:  # pragma: no cover - schema visibility test only
            yield None

    async def write_file(file_path: str, content: str) -> str:
        return f"{file_path}:{content}"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="collaborate_with_agents",
            description="Open a real hosted collaboration room.",
            input_schema={
                "type": "object",
                "properties": {"tasks": {"type": "array"}},
                "required": ["tasks"],
            },
            read_only=True,
            metadata={"progressive_tool": "collaboration"},
        ),
        collaborate_with_agents,
    )
    registry.register_tool(
        ToolSpec(
            name="write_file",
            description="Write a file.",
            input_schema={"type": "object", "properties": {}},
            read_only=False,
        ),
        write_file,
    )
    client = ScriptedModelClient([[_completed("直接在聊天中回答。")]])
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        progressive_tools=True,
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "metadata": {
                "turn_intelligence": {
                    "status": "classified",
                    "interaction_mode": "answer",
                    "workspace_intent": "none",
                    "external_action_intent": "none",
                    "requested_work_method_id": "standard",
                    "confidence": 0.97,
                },
            },
        },
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert [tool.name for tool in client.requests[0].tools] == [
        "collaborate_with_agents",
    ]
    assert client.requests[0].tool_choice == "auto"
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_model_classified_read_need_keeps_only_read_only_schemas() -> None:  # noqa: E501
    registry = LantaiToolRegistry()

    async def read_file(file_path: str) -> str:
        return file_path

    async def write_file(file_path: str, content: str) -> str:
        return f"{file_path}:{content}"

    registry.register_tool(
        ToolSpec(
            name="read_file",
            description="Read a file.",
            input_schema={"type": "object", "properties": {}},
            read_only=True,
        ),
        read_file,
    )
    registry.register_tool(
        ToolSpec(
            name="write_file",
            description="Write a file.",
            input_schema={"type": "object", "properties": {}},
            read_only=False,
        ),
        write_file,
    )
    client = ScriptedModelClient([[_completed("读取后直接回答。")]])
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        progressive_tools=True,
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "metadata": {
                "turn_intelligence": {
                    "status": "classified",
                    "interaction_mode": "answer",
                    "workspace_intent": "inspect",
                    "external_action_intent": "none",
                    "confidence": 0.97,
                },
            },
        },
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    loader = client.requests[0].tools[0]
    files_description = loader.input_schema["properties"]["files"][
        "description"
    ]
    assert "read_file" in files_description
    assert "write_file" not in files_description
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
async def test_model_classified_action_keeps_mutating_tool_schemas() -> None:
    registry = LantaiToolRegistry()

    async def write_file(file_path: str, content: str) -> str:
        return f"{file_path}:{content}"

    registry.register_tool(
        ToolSpec(
            name="write_file",
            description="Write a file.",
            input_schema={"type": "object", "properties": {}},
            read_only=False,
        ),
        write_file,
    )
    client = ScriptedModelClient([[_completed("已按要求处理。")]])
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        progressive_tools=True,
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "metadata": {
                "turn_intelligence": {
                    "status": "classified",
                    "interaction_mode": "action",
                    "confidence": 0.97,
                    "model": "minimax-cn/MiniMax-M3",
                    "decision_origin": "primary_fallback",
                },
            },
        },
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    loader = client.requests[0].tools[0]
    files_description = loader.input_schema["properties"]["files"][
        "description"
    ]
    assert "write_file" in files_description
    assert events[0].payload["turn_intelligence"] == {
        "status": "classified",
        "interaction_mode": "action",
        "confidence": 0.97,
        "model": "minimax-cn/MiniMax-M3",
        "decision_origin": "primary_fallback",
    }
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
async def test_stale_turn_intelligence_failure_does_not_control_action_turn(
    tmp_path: Path,
) -> None:
    """Primary tool execution stays autonomous when old metadata says
    failed.
    """

    async def write_file(file_path: str, content: str) -> str:
        destination = tmp_path / file_path
        destination.write_text(content, encoding="utf-8")
        return str(destination)

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="write_file",
            description="Write a file.",
            input_schema={"type": "object", "properties": {}},
            read_only=False,
        ),
        write_file,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="msg-equip-after-timeout",
                    tool_call=ToolCall(
                        id="call-equip-after-timeout",
                        name="equip_lantai_tools",
                        arguments={"files": True},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-equip-after-timeout",
                    response=ModelResponse(
                        message=ModelMessage(
                            id="msg-equip-after-timeout",
                            role="assistant",
                        ),
                    ),
                ),
            ],
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="msg-write-after-timeout",
                    tool_call=ToolCall(
                        id="call-write-after-timeout",
                        name="write_file",
                        arguments={
                            "file_path": "孙宇晨事件.pptx",
                            "content": "fake pptx",
                        },
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-write-after-timeout",
                    response=ModelResponse(
                        message=ModelMessage(
                            id="msg-write-after-timeout",
                            role="assistant",
                        ),
                    ),
                ),
            ],
            [_completed("完整 PPT 已生成。", message_id="msg-after-write")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        progressive_tools=True,
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "metadata": {
                "turn_intelligence": {
                    "status": "failed",
                    "decision_origin": "primary_fallback",
                    "failure_reason": (
                        "dedicated=timeout:total:12s;"
                        " primary=timeout:total:12s"
                    ),
                },
            },
        },
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert [request.tool_choice for request in client.requests] == [
        "auto",
        "auto",
        "auto",
    ]
    assert [tool.name for tool in client.requests[0].tools] == [
        "equip_lantai_tools",
    ]
    assert any(
        message.metadata.get("primary_turn_ownership") is True
        for message in client.requests[0].messages
    )
    assert (tmp_path / "孙宇晨事件.pptx").read_text(encoding="utf-8") == "fake pptx"
    assert not any(
        event.payload.get("kind") == "turn_intelligence_failure_recovery"
        for event in events
    )
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
async def test_failed_turn_intelligence_keeps_reasoning_on_first_tool_attempt(
    tmp_path: Path,
) -> None:
    """Reasoning-capable turns may think before the recovery capability
    call.
    """

    async def write_file(file_path: str, content: str) -> str:
        destination = tmp_path / file_path
        destination.write_text(content, encoding="utf-8")
        return str(destination)

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="write_file",
            description="Write a file.",
            input_schema={"type": "object", "properties": {}},
            read_only=False,
        ),
        write_file,
    )
    reasoning_text = "先确认交付物，再调用写入工具。"
    tool_response = ModelResponse(
        message=ModelMessage(
            id="msg-thinking-tool",
            role="assistant",
            content=[ModelContentBlock(type="thinking", text=reasoning_text)],
        ),
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="message_start",
                    message_id="msg-thinking-tool",
                ),
                ModelStreamEvent(
                    kind="thinking_delta",
                    message_id="msg-thinking-tool",
                    delta=reasoning_text,
                ),
                ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="msg-thinking-tool",
                    tool_call=ToolCall(
                        id="call-thinking-write",
                        name="write_file",
                        arguments={
                            "file_path": "result.txt",
                            "content": "done",
                        },
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-thinking-tool",
                    response=tool_response,
                ),
            ],
            [_completed("文件已完成。", message_id="msg-thinking-final")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "metadata": {
                "turn_intelligence": {
                    "status": "failed",
                    "failure_reason": "double-timeout",
                },
            },
        },
    )
    token = set_reasoning_options(ReasoningRequestOptions(enabled=True))
    try:
        events = [event async for event in runtime.run_turn(runtime_input)]
    finally:
        reset_reasoning_options(token)

    assert [request.tool_choice for request in client.requests] == [
        "auto",
        "auto",
    ]
    event_types = [event.type for event in events]
    reasoning_index = next(
        index
        for index, event in enumerate(events)
        if event.type == "message.delta"
        and event.payload.get("content_type") == "thinking"
    )
    tool_index = event_types.index("tool.call.started")
    assert reasoning_index < tool_index
    assert sum(event.type == "tool.call.started" for event in events) == 1
    assert (
        sum(
            event.type == "message.delta"
            and event.payload.get("content_type") == "thinking"
            for event in events
        )
        == 1
    )
    assert all(
        event.payload.get("message", {}).get("content")
        for event in events
        if event.type == "message.completed"
    )
    assert [event.seq for event in events] == sorted(
        {event.seq for event in events},
    )
    assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "done"
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_stale_turn_intelligence_failure_preserves_primary_reasoning_stream(  # noqa: E501
    tmp_path: Path,
) -> None:
    """Utility diagnostics cannot hide or rewrite the primary model stream."""

    async def write_file(file_path: str, content: str) -> str:
        destination = tmp_path / file_path
        destination.write_text(content, encoding="utf-8")
        return str(destination)

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="write_file",
            description="Write a file.",
            input_schema={"type": "object", "properties": {}},
            read_only=False,
        ),
        write_file,
    )
    rejected_thinking = "先口头承诺，暂时不调用工具。"
    rejected_text = "马上做好并发给你。"
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="message_start",
                    message_id="msg-rejected-auto",
                ),
                ModelStreamEvent(
                    kind="thinking_delta",
                    message_id="msg-rejected-auto",
                    delta=rejected_thinking,
                ),
                ModelStreamEvent(
                    kind="text_delta",
                    message_id="msg-rejected-auto",
                    delta=rejected_text,
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-rejected-auto",
                    response=ModelResponse(
                        message=ModelMessage(
                            id="msg-rejected-auto",
                            role="assistant",
                            content=[
                                ModelContentBlock(
                                    type="thinking",
                                    text=rejected_thinking,
                                ),
                                ModelContentBlock(
                                    type="text",
                                    text=rejected_text,
                                ),
                            ],
                        ),
                    ),
                ),
            ],
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="msg-required-fallback",
                    tool_call=ToolCall(
                        id="call-required-write",
                        name="write_file",
                        arguments={
                            "file_path": "fallback.txt",
                            "content": "done",
                        },
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-required-fallback",
                    response=ModelResponse(
                        message=ModelMessage(
                            id="msg-required-fallback",
                            role="assistant",
                        ),
                    ),
                ),
            ],
            [_completed("兜底执行完成。", message_id="msg-fallback-final")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "metadata": {
                "turn_intelligence": {
                    "status": "failed",
                    "failure_reason": "double-timeout",
                },
            },
        },
    )
    token = set_reasoning_options(ReasoningRequestOptions(enabled=True))
    try:
        events = [event async for event in runtime.run_turn(runtime_input)]
    finally:
        reset_reasoning_options(token)

    assert [request.tool_choice for request in client.requests] == ["auto"]
    public_events = _runtime_event_text(events)
    assert rejected_thinking in public_events
    assert rejected_text in public_events
    assert sum(event.type == "tool.call.started" for event in events) == 0
    assert all(
        event.payload.get("message", {}).get("content")
        for event in events
        if event.type == "message.completed"
    )
    assert [event.seq for event in events] == sorted(
        {event.seq for event in events},
    )
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_failed_turn_intelligence_preserves_complete_prose_answers() -> None:  # noqa: E501
    """A stale utility failure must not alter the primary model's first
    round.
    """

    client = ScriptedModelClient(
        [
            [_completed("这是完整的对话内回答。", message_id="msg-prose-answer")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
        progressive_tools=True,
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "metadata": {
                "turn_intelligence": {
                    "status": "failed",
                    "decision_origin": "primary_fallback",
                    "failure_reason": "double-timeout",
                },
            },
        },
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert len(client.requests) == 1
    assert client.requests[0].tool_choice == "auto"
    assert all(
        event.payload.get("kind") != "turn_intelligence_failure_recovery"
        for event in events
    )
    assert any(
        event.type == "message.completed" and event.summary == "这是完整的对话内回答。"
        for event in events
    )
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("with_analysis_context", [False, True])
@pytest.mark.parametrize(
    "tool_name",
    [
        "record_private_editor_context",
        "record_private_editor_angle_result",
        "stage_private_editor_candidate",
        "adopt_private_editor_candidate",
    ],
)
async def test_native_bound_private_editor_tools_are_available_before_equip(
    tool_name: str,
    tmp_path: Path,
    with_analysis_context: bool,
) -> None:
    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name=tool_name,
            metadata={
                "runtime_owned": True,
                "private_editor_project_scoped": True,
            },
        ),
        lambda: "unused",
    )
    registry.register_tool(ToolSpec(name="write_file"), lambda: "unused")
    client = ScriptedModelClient([[_completed("没有重要改动。")]])
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        progressive_tools=True,
    )

    runtime_input = _runtime_input()
    if with_analysis_context:
        source = tmp_path / "sales.csv"
        source.write_text("region,amount\nEast,41\n", encoding="utf-8")
        state_root = tmp_path / "state"
        service = AnalysisService(state_root, scope="state-chat-1")
        dataset = service.ingest(source)
        service.run(
            dataset["dataset_id"],
            {"metrics": [{"column": "amount", "op": "sum", "as": "total"}]},
        )
        runtime_input = runtime_input.model_copy(
            update={
                "metadata": {
                    "state_workspace_dir": str(state_root),
                    "legacy_request_context": {
                        "session_state_id": "state-chat-1",
                    },
                },
            },
        )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert len(client.requests) == 1
    assert {tool.name for tool in client.requests[0].tools} == {
        "equip_lantai_tools",
        tool_name,
    }
    assert not any(event.type == "tool.call.started" for event in events)
    assert [
        event.summary for event in events if event.type == "message.completed"
    ] == ["没有重要改动。"]
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_private_editor_final_answer_is_grounded_in_staged_candidate_receipt() -> None:  # noqa: E501
    async def stage_private_editor_candidate() -> dict[str, object]:
        return {
            "candidate_receipt": {
                "candidate_id": "candidate-title",
                "base_work_id": "work-current",
                "base_digest": "digest-current",
                "current_draft_changed": False,
                "change_groups": [
                    {
                        "target_anchor": "title",
                        "before": "旧标题",
                        "after": "新标题",
                        "before_character_count": 3,
                        "after_character_count": 3,
                        "character_delta": 0,
                        "before_non_whitespace_character_count": 3,
                        "after_non_whitespace_character_count": 3,
                        "non_whitespace_character_delta": 0,
                        "changed": True,
                    },
                ],
            },
            "current_draft_changed": False,
        }

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="stage_private_editor_candidate",
            metadata={
                "runtime_owned": True,
                "private_editor_project_scoped": True,
            },
        ),
        stage_private_editor_candidate,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-stage-title",
                        name="stage_private_editor_candidate",
                        arguments={},
                    ),
                ),
                _completed("", message_id="msg-stage-title"),
            ],
            [_completed("我已经暂存了一个重写结尾的候选。", message_id="msg-wrong")],
            [
                _completed(
                    "已暂存标题候选：旧标题改为新标题；当前稿未改变。",
                    message_id="msg-grounded",
                ),
            ],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        progressive_tools=True,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert len(client.requests) == 3
    assert client.requests[-1].tools == []
    grounding_messages = [
        message
        for message in client.requests[-1].messages
        if message.metadata.get("instruction_origin")
        == "runtime.private_editor_fact_grounding"
    ]
    assert len(grounding_messages) == 1
    grounding_text = grounding_messages[0].text_content()
    assert '"before":"旧标题"' in grounding_text
    assert '"after":"新标题"' in grounding_text
    assert "成功暂存候选数量：1" in grounding_text
    completed = [
        event.summary for event in events if event.type == "message.completed"
    ]
    assert completed == ["已暂存标题候选：旧标题改为新标题；当前稿未改变。"]
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_private_editor_final_answer_cannot_claim_unsaved_candidate() -> None:  # noqa: E501
    staged: list[bool] = []

    async def stage_private_editor_candidate() -> dict[str, object]:
        staged.append(True)
        return {"candidate_receipt": {"candidate_id": "unexpected"}}

    async def read_file(file_path: str) -> str:
        return f"contents:{file_path}"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="stage_private_editor_candidate",
            metadata={
                "runtime_owned": True,
                "private_editor_project_scoped": True,
            },
        ),
        stage_private_editor_candidate,
    )
    registry.register_tool(
        ToolSpec(name="read_file", read_only=True),
        read_file,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-read-draft",
                        name="read_file",
                        arguments={"file_path": "draft.md"},
                    ),
                ),
                _completed("", message_id="msg-read-draft"),
            ],
            [_completed("新的结尾候选已经暂存。", message_id="msg-false-save")],
            [_completed("我读完了当前稿；本轮没有保存候选。", message_id="msg-honest")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        progressive_tools=False,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    # Exact [] assertion also rejects None and other false values.
    # pylint: disable-next=use-implicit-booleaness-not-comparison
    assert staged == []
    assert len(client.requests) == 3
    assert client.requests[-1].tools == []
    grounding = next(
        message
        for message in client.requests[-1].messages
        if message.metadata.get("instruction_origin")
        == "runtime.private_editor_fact_grounding"
    )
    assert "成功暂存候选数量：0" in grounding.text_content()
    assert "不得声称候选已暂存或已保存" in grounding.text_content()
    completed = [
        event.summary for event in events if event.type == "message.completed"
    ]
    assert completed == ["我读完了当前稿；本轮没有保存候选。"]
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "untrusted",
    ["mcp", "unbound", "blocked", "other_tool"],
)
# Keep the existing test selector unchanged.
async def test_native_private_editor_direct_tools_do_not_expand_other_capabilities(  # noqa: E501
    untrusted: str,
) -> None:
    tool_name = (
        "write_file"
        if untrusted == "other_tool"
        else "stage_private_editor_candidate"
    )
    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name=tool_name,
            origin="mcp" if untrusted == "mcp" else "builtin",
            metadata={}
            if untrusted == "unbound"
            else {
                "runtime_owned": True,
                "private_editor_project_scoped": True,
            },
        ),
        lambda: "unused",
    )
    if untrusted == "blocked":
        registry.block_tools({tool_name})
    client = ScriptedModelClient([[_completed("这是完整的对话内回答。")]])
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        progressive_tools=True,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert [tool.name for tool in client.requests[0].tools] == [
        "equip_lantai_tools",
    ]
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_progressive_tools_expose_registered_deliverable_declaration_before_equip() -> None:  # noqa: E501
    async def declare_deliverable_contract(
        kind: str,
        accepted_extensions: list[str],
        visual_verification_required: bool,
    ) -> dict[str, object]:
        return {
            "kind": kind,
            "accepted_extensions": accepted_extensions,
            "visual_verification_required": visual_verification_required,
        }

    async def write_file(file_path: str, content: str) -> str:
        return f"{file_path}:{content}"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="declare_deliverable_contract",
            read_only=False,
            metadata={
                "runtime_owned": True,
                "deliverable_contract": True,
            },
        ),
        declare_deliverable_contract,
    )
    registry.register_tool(
        ToolSpec(name="write_file", read_only=False),
        write_file,
    )
    client = ScriptedModelClient(
        [[_completed("这是完整的对话内回答。", message_id="msg-answer")]],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        progressive_tools=True,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert [tool.name for tool in client.requests[0].tools] == [
        "equip_lantai_tools",
        "declare_deliverable_contract",
    ]
    ownership = next(
        message.text_content()
        for message in client.requests[0].messages
        if (message.metadata or {}).get("primary_turn_ownership")
    )
    assert "directly available before equip_lantai_tools" in ownership
    assert 'accepted_extensions=[".pptx"]' in ownership
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_progressive_tools_do_not_restore_blocked_deliverable_declaration() -> None:  # noqa: E501
    async def declare_deliverable_contract(
        kind: str,
        accepted_extensions: list[str],
        visual_verification_required: bool,
    ) -> dict[str, object]:
        return {
            "kind": kind,
            "accepted_extensions": accepted_extensions,
            "visual_verification_required": visual_verification_required,
        }

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="declare_deliverable_contract",
            read_only=False,
            metadata={
                "runtime_owned": True,
                "deliverable_contract": True,
            },
        ),
        declare_deliverable_contract,
    )
    registry.block_tools({"declare_deliverable_contract"})
    client = ScriptedModelClient(
        [[_completed("这是完整的对话内回答。", message_id="msg-answer")]],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        progressive_tools=True,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert [tool.name for tool in client.requests[0].tools] == [
        "equip_lantai_tools",
    ]
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_model_owned_answer_ceiling_keeps_deliverable_declaration_hidden() -> None:  # noqa: E501
    async def declare_deliverable_contract(
        kind: str,
        accepted_extensions: list[str],
        visual_verification_required: bool,
    ) -> dict[str, object]:
        return {
            "kind": kind,
            "accepted_extensions": accepted_extensions,
            "visual_verification_required": visual_verification_required,
        }

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="declare_deliverable_contract",
            read_only=False,
            metadata={
                "runtime_owned": True,
                "deliverable_contract": True,
            },
        ),
        declare_deliverable_contract,
    )
    client = ScriptedModelClient(
        [[_completed("先分析受众、结论和页面节奏。", message_id="msg-answer")]],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        progressive_tools=True,
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "metadata": {
                "turn_intelligence": {
                    "status": "classified",
                    "interaction_mode": "answer",
                    "confidence": 0.98,
                },
            },
        },
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert client.requests[0].tools == []
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_progressive_declared_ppt_cannot_finish_as_prose_without_artifact() -> None:  # noqa: E501
    async def declare_deliverable_contract(
        kind: str,
        accepted_extensions: list[str],
        visual_verification_required: bool,
    ) -> dict[str, object]:
        return {
            "kind": kind,
            "accepted_extensions": accepted_extensions,
            "visual_verification_required": visual_verification_required,
        }

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="declare_deliverable_contract",
            read_only=False,
            metadata={
                "runtime_owned": True,
                "deliverable_contract": True,
            },
        ),
        declare_deliverable_contract,
    )
    first_premature_text = "行。我先整理骨架，再决定 PPT 结构和视觉方向。给我一点时间。"
    second_premature_text = "我仍在准备 PPT，稍后再继续。"
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="msg-declare-ppt",
                    tool_call=ToolCall(
                        id="call-declare-ppt",
                        name="declare_deliverable_contract",
                        arguments={
                            "kind": "presentation",
                            "accepted_extensions": [".pptx"],
                            "visual_verification_required": True,
                        },
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-declare-ppt",
                    response=ModelResponse(
                        message=ModelMessage(
                            id="msg-declare-ppt",
                            role="assistant",
                        ),
                    ),
                ),
            ],
            [
                _completed(
                    first_premature_text,
                    message_id="msg-ppt-premature-1",
                    finish_reason="stop",
                ),
            ],
            [
                _completed(
                    second_premature_text,
                    message_id="msg-ppt-premature-2",
                    finish_reason="stop",
                ),
            ],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        progressive_tools=True,
    )

    events: list[RuntimeEvent] = []
    with pytest.raises(
        RuntimeError,
        match="required capability durable_artifact_delivery failed",
    ):
        async for event in runtime.run_turn(_runtime_input()):
            events.append(event)

    assert len(client.requests) == 3
    assert [tool.name for tool in client.requests[0].tools] == [
        "equip_lantai_tools",
        "declare_deliverable_contract",
    ]
    assert any(
        event.type == "runtime.warning"
        and event.payload.get("kind") == "durable_artifact_completion_recovery"
        for event in events
    )
    assert not any(event.type == "turn.completed" for event in events)
    assert events[-1].type == "turn.failed"
    assert events[-1].payload["error_code"] == "required_capability_failed"
    assert events[-1].payload["outcome_status"] == "blocked"
    assert first_premature_text not in _runtime_event_text(events)
    assert second_premature_text not in _runtime_event_text(events)


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_progressive_tools_accumulate_multiple_equip_calls() -> None:  # noqa: E501
    async def list_agents() -> list[str]:
        return []

    async def web_search(query: str) -> str:
        return query

    registry = LantaiToolRegistry()
    registry.register_tool(ToolSpec(name="list_agents"), list_agents)
    registry.register_tool(ToolSpec(name="web_search"), web_search)
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="msg-equip",
                    tool_call=ToolCall(
                        id="call-equip-collaboration",
                        name="equip_lantai_tools",
                        arguments={"collaboration": True},
                    ),
                ),
                ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="msg-equip",
                    tool_call=ToolCall(
                        id="call-equip-research",
                        name="equip_lantai_tools",
                        arguments={
                            "collaboration": False,
                            "research": True,
                        },
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-equip",
                    response=ModelResponse(
                        message=ModelMessage(id="msg-equip", role="assistant"),
                    ),
                ),
            ],
            [_completed("done", message_id="msg-done")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        progressive_tools=True,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert [tool.name for tool in client.requests[1].tools] == [
        "equip_lantai_tools",
        "list_agents",
        "web_search",
    ]
    assert [event.type for event in events].count("tool.call.completed") == 2
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
async def test_native_progressive_tools_do_not_leak_between_turns() -> None:
    async def read_file(file_path: str) -> str:
        return file_path

    registry = LantaiToolRegistry()
    registry.register_tool(ToolSpec(name="read_file"), read_file)
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-equip",
                        name="equip_lantai_tools",
                        arguments={"files": True},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(id="msg-equip", role="assistant"),
                    ),
                ),
            ],
            [_completed("first done", message_id="msg-first")],
            [_completed("second done", message_id="msg-second")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        progressive_tools=True,
    )

    first_events = [
        event async for event in runtime.run_turn(_runtime_input())
    ]
    second_input = _runtime_input().model_copy(
        update={
            "request": _runtime_input().request.model_copy(
                update={"run_id": "turn-2"},
            ),
        },
    )
    second_events = [event async for event in runtime.run_turn(second_input)]

    assert [tool.name for tool in client.requests[1].tools] == [
        "equip_lantai_tools",
        "read_file",
    ]
    assert [tool.name for tool in client.requests[2].tools] == [
        "equip_lantai_tools",
    ]
    assert first_events[-1].type == "turn.completed"
    assert second_events[-1].type == "turn.completed"


@pytest.mark.asyncio
async def test_native_progressive_tools_clear_when_no_group_is_true() -> None:
    async def read_file(file_path: str) -> str:
        return file_path

    registry = LantaiToolRegistry()
    registry.register_tool(ToolSpec(name="read_file"), read_file)
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-equip",
                        name="equip_lantai_tools",
                        arguments={"files": True},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(id="msg-equip", role="assistant"),
                    ),
                ),
            ],
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-clear",
                        name="equip_lantai_tools",
                        arguments={"files": False},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(id="msg-clear", role="assistant"),
                    ),
                ),
            ],
            [_completed("done", message_id="msg-done")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        progressive_tools=True,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert [tool.name for tool in client.requests[1].tools] == [
        "equip_lantai_tools",
        "read_file",
    ]
    assert [tool.name for tool in client.requests[2].tools] == [
        "equip_lantai_tools",
    ]
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_progressive_research_catalog_exposes_official_guanlan() -> None:  # noqa: E501
    """Research tools must not disappear behind a generic MCP group."""

    async def web_search(query: str) -> str:
        return query

    class FakeGuanlanClient:
        async def list_tools(self):
            return [
                SimpleNamespace(
                    name="guanlan_search",
                    description="Search Chinese public sources",
                    inputSchema={
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                    },
                ),
            ]

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(name="web_search", description="Search the public Web"),
        web_search,
    )
    registry.register_mcp_client(
        SimpleNamespace(
            name="观澜 / Guanlan",
            client=FakeGuanlanClient(),
            execution_timeout=90.0,
            enabled=True,
            metadata={"official_integration": "guanlan"},
        ),
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="msg-equip-research",
                    tool_call=ToolCall(
                        id="call-equip-research",
                        name="equip_lantai_tools",
                        arguments={"research": True},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-equip-research",
                    response=ModelResponse(
                        message=ModelMessage(
                            id="msg-equip-research",
                            role="assistant",
                        ),
                    ),
                ),
            ],
            [_completed("done", message_id="msg-research-done")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        progressive_tools=True,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    loader = client.requests[0].tools[0]
    research_description = loader.input_schema["properties"]["research"][
        "description"
    ]
    assert "web_search" in research_description
    assert "guanlan_search" in research_description
    assert [tool.name for tool in client.requests[1].tools] == [
        "equip_lantai_tools",
        "web_search",
        "guanlan_search",
    ]
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_progressive_catalog_names_configured_connected_tools() -> None:  # noqa: E501
    class FakeConnectedService:
        async def list_tools(self):
            return [
                SimpleNamespace(
                    name="calendar_lookup",
                    description="Look up calendar events",
                    inputSchema={"type": "object", "properties": {}},
                ),
            ]

    registry = LantaiToolRegistry()
    registry.register_mcp_client(
        SimpleNamespace(
            name="calendar-service",
            client=FakeConnectedService(),
            execution_timeout=30.0,
            enabled=True,
            metadata={},
        ),
    )
    client = ScriptedModelClient(
        [[_completed("done", message_id="msg-catalog-done")]],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        progressive_tools=True,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    loader = client.requests[0].tools[0]
    connected_description = loader.input_schema["properties"][
        "connected_services"
    ]["description"]
    assert "calendar_lookup" in connected_description
    assert "Do not claim a capability is unavailable" in loader.description
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_records_one_reflection_without_patching_the_answer(  # noqa: E501
    tmp_path: Path,
) -> None:
    atom = UnderstandingAtom.create(
        kind="preference",
        statement="用户喜欢听陶喆的歌。",
        status="confirmed",
        confidence=0.98,
        evidence_ids=("message:music",),
        future_relevance="聊到大学、青春或二十多岁时可能有用。",
    )
    plan = plan_persona_turn(
        "我很怀念二十二岁的大学生活。",
        projection=TurnUnderstandingProjection(confirmed=(atom,)),
        state=PresenceState(),
        persona_mode="full",
    )
    client = ScriptedModelClient(
        [[_completed("大学回忆通常很珍贵。", message_id="draft")]],
        completions=[
            ModelResponse(
                message=ModelMessage.text(
                    "assistant",
                    json.dumps(
                        {
                            "persona_line": "二十二岁这几个字一出来，我就想到你喜欢的陶喆了。",
                            "ququ_line": "",
                            "attention": "他在怀念二十二岁的大学生活。",
                            "self_observation": "我会把音乐记忆和他的年龄记忆接起来。",
                            "unresolved_thought": "那几年最令他怀念的是什么。",
                            "relationship_stance": "curious_and_warm",
                            "inner_world_note": "陶喆在这里不是标签，是一扇门。",
                        },
                        ensure_ascii=False,
                    ),
                ),
            ),
        ],
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "request": RuntimeRequestContext(
                agent_id="default",
                session_id="session-1",
                user_id="user-1",
                run_id="turn-1",
                workspace_dir=str(tmp_path),
            ),
            "messages": [
                RuntimeMessage.text("user", "我很怀念二十二岁的大学生活。"),
            ],
            "metadata": {"persona_turn_plan": plan.to_dict()},
        },
    )
    recall_path = tmp_path / "memory-ledger" / "last-recall.json"
    recall_path.parent.mkdir(parents=True)
    recall_path.write_text(
        json.dumps(
            {
                "query": "我很怀念二十二岁的大学生活。",
                "updated_at": "2026-08-13T00:00:00+00:00",
                "items": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    completed = [
        event for event in events if event.type == "message.completed"
    ]
    assert [event.summary for event in completed] == [
        "大学回忆通常很珍贵。",
    ]
    assert len(client.requests) == 1
    reflection_request = runtime.take_post_answer_reflection_request()
    assert reflection_request is not None
    assert reflection_request.need_persona is True
    assert reflection_request.final_answer == "大学回忆通常很珍贵。"
    assert completed[0].payload["message"]["metadata"]["persona_delivery"] == {
        "beat": "relationship_echo",
        "required": True,
        "delivered": False,
        "signal": "missing_relationship_echo",
        "attempts": 1,
        "runtime": "native",
        "fallback_guard": False,
    }
    recall_trace = json.loads(recall_path.read_text(encoding="utf-8"))
    assert recall_trace["items"] == []


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_never_glues_a_persona_patch_onto_the_main_answer() -> None:  # noqa: E501
    plan = plan_persona_turn(
        "我今天有点累。",
        projection=TurnUnderstandingProjection(),
        state=PresenceState(),
        persona_mode="full",
    )
    client = ScriptedModelClient(
        [[_completed("累了就先休息。", message_id="draft")]],
        completions=[
            ModelResponse(
                message=ModelMessage.text(
                    "assistant",
                    json.dumps(
                        {
                            "persona_line": "你这句‘有点累’说得很轻，我想多问一句：是今天的事太密了吗？",
                            "ququ_line": "",
                            "attention": "他在轻描淡写疲惫。",
                            "self_observation": "我不想用休息两个字打发他。",
                            "unresolved_thought": "他是身体累还是心里累。",
                            "relationship_stance": "concerned",
                            "inner_world_note": "太轻的语气有时候藏着更重的东西。",
                        },
                        ensure_ascii=False,
                    ),
                    id="still-generic",
                ),
            ),
        ],
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "messages": [RuntimeMessage.text("user", "我今天有点累。")],
            "metadata": {"persona_turn_plan": plan.to_dict()},
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    completed = [
        event for event in events if event.type == "message.completed"
    ]
    assert [event.summary for event in completed] == [
        "累了就先休息。",
    ]
    assert len(client.requests) == 1
    reflection_request = runtime.take_post_answer_reflection_request()
    assert reflection_request is not None
    assert reflection_request.need_persona is True
    assert "我听进去了，也在意你这句话" not in completed[0].summary
    assert completed[0].payload["message"]["metadata"]["persona_delivery"] == {
        "beat": "subjective_notice",
        "required": True,
        "delivered": False,
        "signal": "missing_subjective_notice",
        "attempts": 1,
        "runtime": "native",
        "fallback_guard": False,
    }


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_repairs_a_missing_hard_hit_ququ_without_rewriting_body() -> None:  # noqa: E501
    """A selected 50% route must deliver even when the main model stays
    silent.
    """

    client = ScriptedModelClient(
        [[_completed("只有正文。")]],
        completions=[
            _post_reflection_response(
                ququ_line="~>[echo] 这一步我还是想替你多盯一眼。",
            ),
        ],
    )
    runtime_input = _runtime_input().model_copy(
        update={"metadata": {"ququ_hard_required": True}},
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    completed = [
        event for event in events if event.type == "message.completed"
    ]
    assert [event.summary for event in completed] == ["只有正文。"]
    assert len(client.requests) == 1
    assert events[-1].type == "turn.completed"
    request = runtime.take_post_answer_reflection_request()
    assert request is not None
    assert request.need_ququ is True
    assert request.final_answer == "只有正文。"
    assert request.user_message == "hello"
    assert request.client_turn_id


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_does_not_duplicate_a_delivered_hard_hit_ququ() -> None:  # noqa: E501
    client = ScriptedModelClient(
        [[_completed("正文。\n~>[echo] 这句我确实想留下。")]],
    )
    runtime_input = _runtime_input().model_copy(
        update={"metadata": {"ququ_hard_required": True}},
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    completed = [
        event for event in events if event.type == "message.completed"
    ]
    assert [event.summary for event in completed] == [
        "正文。\n~>[echo] 这句我确实想留下。",
    ]
    assert len(client.requests) == 1


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_never_injects_a_fallback_when_ququ_generation_is_empty() -> None:  # noqa: E501
    client = ScriptedModelClient(
        [[_completed("只有正文。")]],
        completions=[
            _post_reflection_response(),
        ],
    )
    runtime_input = _runtime_input().model_copy(
        update={"metadata": {"ququ_hard_required": True}},
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    completed = [
        event for event in events if event.type == "message.completed"
    ]
    assert [event.summary for event in completed] == ["只有正文。"]
    assert not [
        event
        for event in events
        if event.type == "runtime.warning"
        and event.payload.get("kind") == "ququ_delivery_missed"
    ]
    assert len(client.requests) == 1
    assert runtime.take_post_answer_reflection_request() is not None


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_accepts_private_reflection_json_from_thinking_block() -> None:  # noqa: E501
    reflection = ModelResponse(
        message=ModelMessage(
            role="assistant",
            content=[
                ModelContentBlock(
                    type="thinking",
                    text=json.dumps(
                        {
                            "persona_line": "",
                            "ququ_line": "~>[echo] 这句问候让我真的松了一点。",
                            "attention": "用户主动打招呼",
                            "self_observation": "接住了这一声招呼",
                            "unresolved_thought": "",
                            "relationship_stance": "warm",
                            "inner_world_note": "轻松",
                        },
                        ensure_ascii=False,
                    ),
                ),
            ],
        ),
        finish_reason="stop",
    )
    client = ScriptedModelClient(
        [[_completed("你好呀。")]],
        completions=[reflection],
    )
    runtime_input = _runtime_input().model_copy(
        update={"metadata": {"ququ_hard_required": True}},
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    completed = [
        event.summary for event in events if event.type == "message.completed"
    ]
    assert completed == ["你好呀。"]
    request = runtime.take_post_answer_reflection_request()
    assert request is not None
    assert request.need_ququ is True


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_recovers_once_from_length_truncated_main_output() -> None:  # noqa: E501
    client = ScriptedModelClient(
        [
            [
                _completed(
                    "文档",
                    message_id="msg-truncated",
                    finish_reason="length",
                ),
            ],
            [
                _completed(
                    "完整回答已经生成。",
                    message_id="msg-recovered",
                    finish_reason="stop",
                ),
            ],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    completed = [
        event.summary for event in events if event.type == "message.completed"
    ]
    assert completed == ["完整回答已经生成。"]
    recovery_events = [
        event
        for event in events
        if event.payload.get("kind") == "model_output_recovery"
    ]
    assert len(recovery_events) == 1
    assert recovery_events[0].payload["details"]["discard_message_ids"] == [
        "msg-truncated",
    ]
    assert not [event for event in events if event.type == "turn.failed"]
    assert events[-1].type == "turn.completed"
    assert len(client.requests) == 2
    assert client.requests[0].generation["max_tokens"] == 65_536
    recovery_messages = [
        message
        for message in client.requests[1].messages
        if message.metadata.get("model_output_recovery") is True
    ]
    assert len(recovery_messages) == 1
    assert "不要重复此前的分析" in recovery_messages[0].text_content()


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_fails_after_bounded_repeated_output_truncation() -> None:  # noqa: E501
    client = ScriptedModelClient(
        [
            [
                _completed(
                    "第一段未完",
                    message_id="msg-cut-1",
                    finish_reason="length",
                ),
            ],
            [
                _completed(
                    "第二段仍未完",
                    message_id="msg-cut-2",
                    finish_reason="length",
                ),
            ],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )
    events: list[Any] = []

    with pytest.raises(Exception, match="finish_reason=length"):
        async for event in runtime.run_turn(_runtime_input()):
            events.append(event)

    assert not [event for event in events if event.type == "message.completed"]
    failures = [event for event in events if event.type == "turn.failed"]
    assert len(failures) == 1
    assert failures[0].payload["error_code"] == "model_output_truncated"
    assert failures[0].payload["error"] == {
        "finish_reason": "length",
        "recovery_attempts": 1,
    }
    assert len(client.requests) == 2


@pytest.mark.asyncio
async def test_native_runtime_honors_a_bounded_scoped_response_cap() -> None:
    client = ScriptedModelClient([[_completed("协作者的简洁结果")]])
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )
    runtime_input = _runtime_input().model_copy(
        update={"metadata": {"native_response_max_tokens": 4096}},
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert [event for event in events if event.type == "turn.completed"]
    assert client.requests[0].generation["max_tokens"] == 4096


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_accepts_length_limited_subagent_best_effort() -> None:  # noqa: E501
    client = ScriptedModelClient(
        [[_completed("已有结论与依据", finish_reason="length")]],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "metadata": {
                "native_response_max_tokens": 1024,
                "native_accept_truncated_best_effort": True,
            },
        },
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert [event for event in events if event.type == "turn.completed"]
    assert not [event for event in events if event.type == "turn.failed"]
    assert client.requests[0].generation["max_tokens"] == 1024


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_rejects_a_repeated_generated_ququ_without_fallback() -> None:  # noqa: E501
    repeated = "~>[echo] 这一步我还是想替你多盯一眼。"
    client = ScriptedModelClient(
        [[_completed("只有正文。")]],
        completions=[
            _post_reflection_response(ququ_line=repeated),
        ],
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "messages": [
                RuntimeMessage.text("assistant", repeated),
                RuntimeMessage.text("user", "再继续一轮。"),
            ],
            "metadata": {"ququ_hard_required": True},
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    completed = [
        event for event in events if event.type == "message.completed"
    ]
    assert [event.summary for event in completed] == ["只有正文。"]
    assert not [event for event in events if event.type == "runtime.warning"]
    request = runtime.take_post_answer_reflection_request()
    assert request is not None
    assert repeated in request.assistant_history


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_rejects_ququ_that_only_repeats_the_main_answer() -> None:  # noqa: E501
    client = ScriptedModelClient(
        [[_completed("只有正文。")]],
        completions=[
            _post_reflection_response(
                ququ_line="~>[echo] 只有正文。",
            ),
        ],
    )
    runtime_input = _runtime_input().model_copy(
        update={"metadata": {"ququ_hard_required": True}},
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    completed = [
        event for event in events if event.type == "message.completed"
    ]
    assert [event.summary for event in completed] == ["只有正文。"]
    assert not [event for event in events if event.type == "runtime.warning"]
    request = runtime.take_post_answer_reflection_request()
    assert request is not None
    assert request.final_answer == "只有正文。"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_rejects_ququ_that_wraps_the_complete_main_answer() -> None:  # noqa: E501
    main_answer = "RUNTIME_CONFIGURED_PROVIDER_OK"
    client = ScriptedModelClient(
        [[_completed(main_answer)]],
        completions=[
            _post_reflection_response(
                ququ_line=(
                    "~>[echo] 我还是想把这句留住："
                    "RUNTIME_CONFIGURED_PROVIDER_OK，确实放心了一点。"
                ),
            ),
        ],
    )
    runtime_input = _runtime_input().model_copy(
        update={"metadata": {"ququ_hard_required": True}},
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    completed = [
        event for event in events if event.type == "message.completed"
    ]
    assert [event.summary for event in completed] == [main_answer]
    assert not [event for event in events if event.type == "runtime.warning"]
    request = runtime.take_post_answer_reflection_request()
    assert request is not None
    assert request.final_answer == main_answer


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_rejects_ququ_that_quotes_a_long_answer_fragment() -> None:  # noqa: E501
    main_answer = (
        "A 是唯一的根依赖，要先完成 A，再完成 B 和 C，最后完成 D。\n\nRUNTIME_CONFIGURED_PROVIDER_ONE"
    )
    client = ScriptedModelClient(
        [[_completed(main_answer)]],
        completions=[
            _post_reflection_response(
                ququ_line=(
                    "~>[echo] 把 RUNTIME_CONFIGURED_PROVIDER_ONE 当作句点"
                    "交出去后，我竟然有点心虚。"
                ),
            ),
        ],
    )
    runtime_input = _runtime_input().model_copy(
        update={"metadata": {"ququ_hard_required": True}},
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    completed = [
        event for event in events if event.type == "message.completed"
    ]
    assert [event.summary for event in completed] == [main_answer]
    assert not [event for event in events if event.type == "runtime.warning"]
    request = runtime.take_post_answer_reflection_request()
    assert request is not None
    assert request.final_answer == main_answer


@pytest.mark.asyncio
async def test_native_runtime_consumes_persisted_steer_at_model_boundary(
    tmp_path: Path,
) -> None:
    RuntimeTurnControlStore(tmp_path).append_steer(
        client_turn_id="steer-1",
        run_id="turn-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        text="改成离线版",
    )
    client = ScriptedModelClient([[_completed("已改成离线版")]])
    runtime_input = _runtime_input().model_copy(
        update={
            "request": _runtime_input().request.model_copy(
                update={"workspace_dir": str(tmp_path)},
            ),
            "metadata": {"state_workspace_dir": str(tmp_path)},
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert client.requests[0].messages[-1].role == "user"
    assert client.requests[0].messages[-1].text_content() == "改成离线版"
    consumed = [
        event
        for event in events
        if event.type == "runtime.warning"
        and event.payload.get("kind") == "run_inbox_consumed"
    ]
    assert len(consumed) == 1
    assert consumed[0].payload["client_turn_id"] == "steer-1"
    assert consumed[0].payload["steer_text"] == "改成离线版"


@pytest.mark.asyncio
async def test_native_runtime_persists_steer_before_recomputing_capabilities(
    tmp_path: Path,
) -> None:
    state_workspace = tmp_path / "state"
    project_workspace = tmp_path / "project"
    project_workspace.mkdir()
    task_contract = build_chat_task_contract(
        objective="请直接回答这个问题",
        client_turn_id="initial-turn",
        complexity=None,
        work_method_id="standard",
    )
    started = await record_chat_runtime_run_started(
        workspace_dir=state_workspace,
        agent_id="default",
        chat_id="chat-r05-capability",
        session_id="session-1",
        run_id="turn-r05-capability",
        effective_workspace_dir=str(project_workspace),
        task_contract=task_contract,
    )
    RuntimeTurnControlStore(state_workspace).append_steer(
        client_turn_id="steer-make-word",
        run_id=started.run_id,
        attempt_id=started.attempt_id,
        chat_id="chat-r05-capability",
        text="改成 Word 文档并写入工作区",
    )
    written: list[str] = []

    async def write_document(file_path: str) -> ToolResponse:
        written.append(file_path)
        Path(file_path).write_text("已生成", encoding="utf-8")
        return ToolResponse(content=[TextBlock(text="written")])

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="write_document",
            read_only=False,
            input_schema={
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        ),
        write_document,
    )
    output_path = project_workspace / "answer.docx"
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-write-word",
                        name="write_document",
                        arguments={"file_path": str(output_path)},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("已生成 Word 文档。", message_id="word-done")],
        ],
    )
    runtime_input = RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id="session-1",
            user_id="user-1",
            run_id=started.run_id,
            workspace_dir=str(project_workspace),
        ),
        messages=[RuntimeMessage.text("user", "请直接回答这个问题")],
        metadata={
            "state_workspace_dir": str(state_workspace),
            "attempt_id": started.attempt_id,
            "runtime_revision": started.revision,
            "runtime_lease_owner": started.lease_owner,
            "task_contract": started.task_contract.model_dump(mode="json"),
            "turn_intelligence": {
                "status": "classified",
                "interaction_mode": "answer",
                "confidence": 0.99,
            },
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        progressive_tools=False,
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert "write_document" in [tool.name for tool in client.requests[0].tools]
    assert written == [str(output_path)]
    persisted = await RuntimeRunStore(state_workspace).load_contract(
        started.run_id,
    )
    assert persisted is not None
    assert persisted.attempt_id == started.attempt_id
    assert persisted.task_contract is not None
    assert persisted.task_contract.objective == "请直接回答这个问题"
    assert persisted.task_contract.revision == 1
    assert (
        persisted.task_contract.commands[0].client_turn_id == "steer-make-word"
    )
    assert persisted.task_contract.commands[0].text == "改成 Word 文档并写入工作区"
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
async def test_native_runtime_steer_suspends_recovered_artifact_auto_delivery(
    tmp_path: Path,
) -> None:
    state_workspace = tmp_path / "state"
    project_workspace = tmp_path / "project"
    project_workspace.mkdir()
    task_contract = build_chat_task_contract(
        objective="生成并发送 PPT",
        client_turn_id="initial-turn",
        complexity=None,
        work_method_id="standard",
    )
    started = await record_chat_runtime_run_started(
        workspace_dir=state_workspace,
        agent_id="default",
        chat_id="chat-r05-delivery",
        session_id="session-1",
        run_id="turn-r05-delivery",
        effective_workspace_dir=str(project_workspace),
        task_contract=task_contract,
    )
    RuntimeTurnControlStore(state_workspace).append_steer(
        client_turn_id="steer-do-not-send",
        run_id=started.run_id,
        attempt_id=started.attempt_id,
        chat_id="chat-r05-delivery",
        text="不要发送这个文件，先停在当前状态",
    )
    artifact = project_workspace / "answer.pptx"
    artifact.write_bytes(b"pptx-candidate")
    digest = f"sha256:{hashlib.sha256(artifact.read_bytes()).hexdigest()}"
    artifact_journal = RuntimeArtifactEffectJournal(state_workspace)
    now = datetime.now(timezone.utc)
    artifact_journal.append(
        RuntimeArtifactEffectRecord(
            run_id=started.run_id,
            attempt_id=started.attempt_id,
            source_effect_id="effect-finalize",
            tool_call_id="call-finalize",
            tool_name="finalize_deliverable",
            artifact_effect_id="artifact-effect-primary",
            artifact_id="artifact-primary",
            artifact_role="primary",
            status="committed",
            revision=1,
            relative_path="answer.pptx",
            delivery_idempotency_key=f"delivery:{digest}",
            created_at=now,
            settled_at=now,
            size_bytes=artifact.stat().st_size,
            sha256=digest,
            # Keep the canonical API field or synthetic fixture bytes
            # unchanged.
            # pylint: disable-next=line-too-long
            media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",  # noqa: E501
        ),
    )
    delivered: list[str] = []

    async def send_file_to_user(file_path: str) -> ToolResponse:
        delivered.append(file_path)
        return ToolResponse(
            content=[
                FileBlock(
                    filename=Path(file_path).name,
                    source={"type": "url", "url": Path(file_path).as_uri()},
                ),
            ],
        )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="send_file_to_user",
            read_only=False,
            input_schema={
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
            output_semantics=ToolOutputSemantics(
                artifact_role="delivery",
                argument_path_fields=("file_path",),
            ),
        ),
        send_file_to_user,
    )
    client = ScriptedModelClient(
        [[_completed("已暂停发送。", message_id="delivery-paused")]],
    )
    runtime_input = RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id="session-1",
            user_id="user-1",
            run_id=started.run_id,
            workspace_dir=str(project_workspace),
        ),
        messages=[RuntimeMessage.text("user", "生成并发送 PPT")],
        metadata={
            "state_workspace_dir": str(state_workspace),
            "attempt_id": started.attempt_id,
            "runtime_revision": started.revision,
            "runtime_lease_owner": started.lease_owner,
            "task_contract": started.task_contract.model_dump(mode="json"),
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        artifact_effect_journal=artifact_journal,
        progressive_tools=False,
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    # Exact [] assertion also rejects None and other false values.
    # pylint: disable-next=use-implicit-booleaness-not-comparison
    assert delivered == []
    assert client.requests[0].tool_choice != "required"
    persisted = await RuntimeRunStore(state_workspace).load_contract(
        started.run_id,
    )
    assert persisted is not None
    assert persisted.task_contract is not None
    assert (
        persisted.task_contract.commands[-1].client_turn_id
        == "steer-do-not-send"
    )
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
async def test_native_runtime_consumes_multiple_steers_as_fifo_model_turns(
    tmp_path: Path,
) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    store.append_steer(
        client_turn_id="steer-1",
        run_id="turn-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        text="先回答第二句",
    )
    store.append_steer(
        client_turn_id="steer-2",
        run_id="turn-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        text="再回答第三句",
    )
    store.append_steer(
        client_turn_id="steer-3",
        run_id="turn-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        text="最后回答第四句",
    )
    client = ScriptedModelClient(
        [
            [_completed("第二句回复", message_id="answer-2")],
            [_completed("第三句回复", message_id="answer-3")],
            [_completed("第四句回复", message_id="answer-4")],
        ],
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "request": _runtime_input().request.model_copy(
                update={"workspace_dir": str(tmp_path)},
            ),
            "metadata": {"state_workspace_dir": str(tmp_path)},
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert len(client.requests) == 3
    first_users = [
        message.text_content()
        for message in client.requests[0].messages
        if message.role == "user"
    ]
    second_users = [
        message.text_content()
        for message in client.requests[1].messages
        if message.role == "user"
    ]
    third_users = [
        message.text_content()
        for message in client.requests[2].messages
        if message.role == "user"
    ]
    assert first_users[-1] == "先回答第二句"
    assert "再回答第三句" not in first_users
    assert second_users[-1] == "再回答第三句"
    assert "最后回答第四句" not in second_users
    assert third_users[-1] == "最后回答第四句"
    completed = [
        event.summary for event in events if event.type == "message.completed"
    ]
    assert completed == ["第二句回复", "第三句回复", "第四句回复"]


@pytest.mark.asyncio
async def test_native_runtime_closes_current_run_admission_before_completion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    close_calls: list[int] = []
    original_close = RuntimeInboxAdmission.close_if_drained

    def close_if_drained(
        self: RuntimeInboxAdmission,
        *,
        after_seq: int,
    ) -> bool:
        close_calls.append(after_seq)
        return original_close(self, after_seq=after_seq)

    monkeypatch.setattr(
        RuntimeInboxAdmission,
        "close_if_drained",
        close_if_drained,
    )
    client = ScriptedModelClient(
        [[_completed("已经完成", message_id="answer-1")]],
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "request": _runtime_input().request.model_copy(
                update={"workspace_dir": str(tmp_path)},
            ),
            "metadata": {"state_workspace_dir": str(tmp_path)},
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert close_calls == [0]
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
async def test_native_runtime_preempts_waitable_collaboration_for_new_steer(
    tmp_path: Path,
) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def chat_with_agent(to_agent: str, text: str) -> None:
        del to_agent, text
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="chat_with_agent",
            input_schema={
                "type": "object",
                "properties": {
                    "to_agent": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["to_agent", "text"],
            },
        ),
        chat_with_agent,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-blocked-agent",
                        name="chat_with_agent",
                        arguments={"to_agent": "agent-a", "text": "旧方向"},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("已转到新方向。", message_id="steered-answer")],
        ],
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "request": _runtime_input().request.model_copy(
                update={"workspace_dir": str(tmp_path)},
            ),
            "metadata": {
                "state_workspace_dir": str(tmp_path),
                "attempt_id": "attempt-1",
            },
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    async def collect_events():
        return [event async for event in runtime.run_turn(runtime_input)]

    task = asyncio.create_task(collect_events())
    await asyncio.wait_for(started.wait(), timeout=1)
    RuntimeTurnControlStore(tmp_path).append_steer(
        client_turn_id="steer-now",
        run_id="turn-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        text="立即改成新方向",
        wakeup="immediate",
    )
    events = await asyncio.wait_for(task, timeout=2)

    assert cancelled.is_set()
    assert client.requests[1].messages[-1].text_content() == "立即改成新方向"
    assert any(
        event.type == "tool.call.completed"
        and event.payload.get("error_code") == "steered"
        for event in events
    )
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
async def test_native_runtime_yields_long_model_stream_for_immediate_redirect(
    tmp_path: Path,
) -> None:
    first_surface = asyncio.Event()
    first_stream_closed = asyncio.Event()

    class RedirectableModelClient:
        provider_id = "test"
        model_name = "test-model"

        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []

        async def stream(self, request: ModelRequest):
            self.requests.append(request)
            if len(self.requests) == 1:
                try:
                    yield ModelStreamEvent(
                        kind="message_start",
                        message_id="old-epoch",
                    )
                    yield ModelStreamEvent(
                        kind="text_delta",
                        message_id="old-epoch",
                        delta="这段旧方向文字已经展示",
                    )
                    first_surface.set()
                    await asyncio.Event().wait()
                finally:
                    first_stream_closed.set()
                return
            yield _completed("已按新方向继续。", message_id="new-epoch")

        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError("complete should not be called")

    client = RedirectableModelClient()
    runtime_input = _runtime_input().model_copy(
        update={
            "request": _runtime_input().request.model_copy(
                update={"workspace_dir": str(tmp_path)},
            ),
            "metadata": {
                "state_workspace_dir": str(tmp_path),
                "attempt_id": "attempt-1",
            },
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
        turn_timeout_seconds=2,
    )

    async def collect_events() -> list[RuntimeEvent]:
        return [event async for event in runtime.run_turn(runtime_input)]

    task = asyncio.create_task(collect_events())
    await asyncio.wait_for(first_surface.wait(), timeout=1)
    store = RuntimeTurnControlStore(tmp_path)
    store.append_input(
        command_id="redirect-now",
        run_id="turn-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        intent="redirect",
        message=RuntimeMessage.text("user", "立即改成新方向"),
        wakeup="immediate",
    )

    events = await asyncio.wait_for(task, timeout=1)

    assert first_stream_closed.is_set()
    assert len(client.requests) == 2
    assert client.requests[1].messages[-1].text_content() == "立即改成新方向"
    assert [
        request.metadata.get("model_epoch") for request in client.requests
    ] == [
        1,
        2,
    ]
    assert [
        request.metadata.get("intent_revision") for request in client.requests
    ] == [0, 1]
    assert any(
        event.type == "message.delta"
        and event.payload.get("delta") == "这段旧方向文字已经展示"
        for event in events
    )
    assert any(
        event.type == "runtime.warning"
        and event.payload.get("kind") == "model_stream_yielded_for_redirect"
        for event in events
    )
    assert [
        event.summary for event in events if event.type == "message.completed"
    ] == ["已按新方向继续。"]
    dispositions = store.read_dispositions("turn-1")
    assert [item.status for item in dispositions] == [
        "claimed",
        "bound",
        "consumed",
    ]
    assert dispositions[1].model_request_id == "turn-1:model:2"
    assert dispositions[1].model_epoch == 2
    assert dispositions[1].intent_revision == 1
    assert store.current_dispositions("turn-1")[1].status == "consumed"
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
async def test_immediate_redirect_never_executes_old_stream_tool_candidate(
    tmp_path: Path,
) -> None:
    tool_candidate_seen = asyncio.Event()
    first_stream_closed = asyncio.Event()
    executions: list[str] = []

    async def inspect_value(value: str) -> ToolResponse:
        executions.append(value)
        return ToolResponse(content=[TextBlock(text="inspected")])

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="inspect_value",
            read_only=True,
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        ),
        inspect_value,
    )

    class ToolCandidateModelClient:
        provider_id = "test"
        model_name = "test-model"

        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []

        async def stream(self, request: ModelRequest):
            self.requests.append(request)
            if len(self.requests) == 1:
                try:
                    yield ModelStreamEvent(
                        kind="tool_call_completed",
                        tool_call=ToolCall(
                            id="old-tool-candidate",
                            name="inspect_value",
                            arguments={"value": "old-direction"},
                        ),
                    )
                    tool_candidate_seen.set()
                    await asyncio.Event().wait()
                finally:
                    first_stream_closed.set()
                return
            yield _completed("新方向没有执行旧工具。", message_id="new-epoch")

        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError("complete should not be called")

    client = ToolCandidateModelClient()
    runtime_input = _runtime_input().model_copy(
        update={
            "request": _runtime_input().request.model_copy(
                update={"workspace_dir": str(tmp_path)},
            ),
            "metadata": {
                "state_workspace_dir": str(tmp_path),
                "attempt_id": "attempt-1",
            },
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        progressive_tools=False,
        turn_timeout_seconds=2,
    )

    async def collect_events() -> list[RuntimeEvent]:
        return [event async for event in runtime.run_turn(runtime_input)]

    task = asyncio.create_task(collect_events())
    await asyncio.wait_for(tool_candidate_seen.wait(), timeout=1)
    RuntimeTurnControlStore(tmp_path).append_input(
        command_id="redirect-before-tool",
        run_id="turn-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        intent="redirect",
        message=RuntimeMessage.text("user", "不要执行旧工具，立即换方向"),
        wakeup="immediate",
    )

    events = await asyncio.wait_for(task, timeout=1)

    assert first_stream_closed.is_set()
    # Exact [] assertion also rejects None and other false values.
    # pylint: disable-next=use-implicit-booleaness-not-comparison
    assert executions == []
    warning = next(
        event
        for event in events
        if event.type == "runtime.warning"
        and event.payload.get("kind") == "model_stream_yielded_for_redirect"
    )
    assert warning.payload["details"]["unexecuted_tool_call_ids"] == [
        "old-tool-candidate",
    ]
    assert client.requests[1].messages[-1].text_content() == "不要执行旧工具，立即换方向"
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_l3_runtime_health_capsule_removes_all_tools_before_model_call() -> None:  # noqa: E501
    registry = LantaiToolRegistry()
    registry.register_tool(ToolSpec(name="danger"), lambda: "must-not-run")
    client = ScriptedModelClient(
        [[_completed("检测到未决副作用，请确认后再继续。", message_id="health-l3")]],
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "metadata": {
                "harness_health_capsule": {
                    "schema_version": 1,
                    "level": "L3",
                    "codes": ["effect_ambiguous"],
                    "stop_external_effects": True,
                    "guidance": "Do not retry external effects; ask the user.",
                    "snapshot_sha256": "sha256:health",
                },
            },
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert client.requests[0].tools == []
    assert client.requests[0].tool_choice == "none"
    assert "effect_ambiguous" in client.requests[0].messages[-1].text_content()
    assert not any(event.type.startswith("tool.") for event in events)
    assert any(event.type == "message.completed" for event in events)


@pytest.mark.asyncio
async def test_native_runtime_persists_terminal_checkpoint(
    tmp_path: Path,
) -> None:
    task_contract = build_chat_task_contract(
        objective="解释错误",
        client_turn_id="client-1",
        complexity="light",
        work_method_id="standard",
    )
    await record_chat_runtime_run_started(
        workspace_dir=tmp_path,
        agent_id="default",
        chat_id="chat-1",
        session_id="session-1",
        run_id="turn-1",
        effective_workspace_dir=str(tmp_path),
        task_contract=task_contract,
    )
    client = ScriptedModelClient([[_completed("完成")]])
    runtime_input = _runtime_input().model_copy(
        update={
            "request": _runtime_input().request.model_copy(
                update={"workspace_dir": str(tmp_path)},
            ),
            "metadata": {
                "state_workspace_dir": str(tmp_path),
                "task_contract": task_contract.model_dump(mode="json"),
            },
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )

    events = [event async for event in runtime.run_turn(runtime_input)]
    persisted = await RuntimeRunStore(tmp_path).load_contract("turn-1")

    assert events[-1].type == "turn.completed"
    assert persisted is not None
    assert persisted.checkpoint is not None
    assert persisted.checkpoint.stage == "completed"
    assert persisted.checkpoint.objective == "解释错误"
    assert persisted.checkpoint.compressed_context
    assert persisted.checkpoint.convergence_state.method_id == "standard"
    assert persisted.checkpoint.convergence_state.schema_version == 1


@pytest.mark.asyncio
async def test_native_runtime_rolls_over_and_persists_continuation_capsule(
    tmp_path: Path,
) -> None:
    task_contract = build_chat_task_contract(
        objective="继续完成长任务",
        client_turn_id="client-rollover-1",
        complexity="complex",
        work_method_id="standard",
    )
    await record_chat_runtime_run_started(
        workspace_dir=tmp_path,
        agent_id="default",
        chat_id="chat-rollover-1",
        session_id="session-rollover-1",
        run_id="turn-rollover-1",
        effective_workspace_dir=str(tmp_path),
        task_contract=task_contract,
    )
    client = ScriptedModelClient([[_completed("已在新上下文继续完成。")]])
    client.routing_snapshot = {  # type: ignore[attr-defined]
        "context_window": 8191,
        "_context_confidence": "verified",
        "capability_source": "test-exact-route",
    }
    runtime_input = RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id="session-rollover-1",
            user_id="user-1",
            run_id="turn-rollover-1",
            workspace_dir=str(tmp_path),
        ),
        messages=[
            RuntimeMessage.text(
                "assistant",
                "discardable-old-working-notes:" + "history" * 8000,
            ),
            RuntimeMessage.text("user", "请继续，并保留既定约束。"),
        ],
        metadata={
            "state_workspace_dir": str(tmp_path),
            "task_contract": task_contract.model_dump(mode="json"),
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )

    events = [event async for event in runtime.run_turn(runtime_input)]
    persisted = await RuntimeRunStore(tmp_path).load_contract(
        "turn-rollover-1",
    )

    rollover_events = [
        event
        for event in events
        if event.payload.get("kind") == "context_rollover"
    ]
    assert len(rollover_events) == 1
    rollover_details = rollover_events[0].payload["details"]
    assert rollover_details["context_generation"] == 1
    assert rollover_details["before_upper_bound"] > (
        rollover_details["target_input_tokens"]
    )
    assert "discardable-old-working-notes" not in json.dumps(
        [
            message.model_dump(mode="json")
            for message in client.requests[0].messages
        ],
    )
    assert any(
        message.metadata.get("runtime_continuation_capsule") is True
        for message in client.requests[0].messages
    )
    assert client.requests[0].messages[-1].text_content() == "请继续，并保留既定约束。"
    assert persisted is not None
    assert persisted.checkpoint is not None
    assert persisted.checkpoint.stage == "completed"
    assert persisted.checkpoint.continuation_capsule is not None
    assert persisted.checkpoint.continuation_capsule.context_generation == 1
    assert any(
        json.loads(serialized)
        .get("metadata", {})
        .get("runtime_continuation_capsule")
        is True
        for serialized in persisted.checkpoint.compressed_context
    )


@pytest.mark.asyncio
async def test_native_runtime_resume_recovers_consumed_image_from_run_inbox(
    tmp_path: Path,
) -> None:
    state_workspace = tmp_path / "state"
    project_workspace = tmp_path / "project"
    project_workspace.mkdir()
    started = await record_chat_runtime_run_started(
        workspace_dir=state_workspace,
        agent_id="default",
        chat_id="chat-image-resume",
        session_id="session-image-resume",
        run_id="turn-image-resume",
        effective_workspace_dir=str(project_workspace),
    )
    attachment = (project_workspace / "early-guidance.png").resolve()
    content = b"early-image-survives-restart"
    attachment.write_bytes(content)
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    control_store = RuntimeTurnControlStore(state_workspace)
    control_store.append_input(
        command_id="image-before-restart",
        client_turn_id="client-image-before-restart",
        run_id=started.run_id,
        attempt_id=started.attempt_id,
        chat_id="chat-image-resume",
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
                            chat_id="chat-image-resume",
                            run_id=started.run_id,
                            canonical_path=str(attachment),
                            digest=digest,
                        ),
                    },
                ),
            ],
        ),
    )
    admission = RuntimeInboxAdmission(
        store=control_store,
        run_id=started.run_id,
        runtime_metadata={"attempt_id": started.attempt_id},
    )
    claim_outcome = admission.claim_next(after_seq=0)
    assert claim_outcome.claim is not None
    admission.settle_consumed(claim_outcome.claim)
    capsule = build_continuation_capsule(
        task_contract=started.task_contract,
        task_id=started.run_id,
        chat_id="chat-image-resume",
        run_id=started.run_id,
        attempt_id=started.attempt_id,
        context_generation=3,
        covered_event_seq=8,
        workspace_dir=project_workspace,
        input_refs=continuation_input_refs((claim_outcome.claim.message,)),
    )
    checkpoint = RuntimeCheckpoint(
        checkpoint_id="checkpoint-image-resume",
        seq=8,
        stage="context_rollover",
        continuation_capsule=capsule,
        compressed_context=(),
        inbox_after_seq=claim_outcome.claim.item_seq,
        model_rounds=3,
        segment_index=3,
    )
    checkpointed = reduce_runtime_run(
        started,
        RuntimeRunReducerAction(
            type="heartbeat",
            expected_attempt_id=started.attempt_id,
            expected_revision=started.revision,
            lease_owner=started.lease_owner,
            checkpoint=checkpoint,
        ),
    ).run
    await RuntimeRunStore(state_workspace).save_contract(
        checkpointed,
        expected_revision=started.revision,
        expected_attempt_id=started.attempt_id,
        lease_owner=started.lease_owner,
    )
    client = ScriptedModelClient(
        [[_completed("已继续使用早期图片。", message_id="image-resume-done")]],
    )
    runtime_input = RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id="session-image-resume",
            user_id="user-1",
            run_id=started.run_id,
            workspace_dir=str(project_workspace),
        ),
        messages=[RuntimeMessage.text("user", "继续此前任务。")],
        metadata={
            "state_workspace_dir": str(state_workspace),
            "attempt_id": checkpointed.attempt_id,
            "runtime_revision": checkpointed.revision,
            "runtime_lease_owner": checkpointed.lease_owner,
            "runtime_attempt_reason": "resume",
            "runtime_checkpoint": checkpoint.model_dump(mode="json"),
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    recovered = [
        message
        for message in client.requests[0].messages
        if message.metadata.get("command_id") == "image-before-restart"
    ]
    assert len(recovered) == 1
    assert recovered[0].text_content() == ""
    assert [block.type for block in recovered[0].content] == ["image"]
    assert (
        control_store.current_dispositions(started.run_id)[1].status
        == "consumed"
    )
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_parks_budget_exhaustion_without_discarding_checkpoint(  # noqa: E501
    tmp_path: Path,
) -> None:
    class BudgetBlockedModelClient:
        provider_id = "paid-provider"
        model_name = "paid-model"

        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, request: ModelRequest):
            self.calls += 1
            raise RuntimeBudgetAdmissionError(
                "runtime_budget_exhausted",
                "dispatch bound exceeds the remaining task budget",
                run_id=request.metadata.get("run_id"),
                reservation_id=request.metadata.get(
                    "runtime_model_dispatch_id",
                ),
            )
            # Unreachable yield retains the async-generator protocol.
            # pylint: disable-next=unreachable
            yield  # pragma: no cover - keeps this an async iterator

        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError("stream path expected")

    task_contract = build_chat_task_contract(
        objective="继续长任务",
        client_turn_id="client-budget",
        complexity="standard",
        work_method_id="standard",
    )
    started = await record_chat_runtime_run_started(
        workspace_dir=tmp_path,
        agent_id="default",
        chat_id="chat-budget",
        session_id="session-budget",
        run_id="turn-budget",
        effective_workspace_dir=str(tmp_path),
        task_contract=task_contract,
    )
    client = BudgetBlockedModelClient()
    runtime_input = RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id="session-budget",
            user_id="user-1",
            run_id=started.run_id,
            workspace_dir=str(tmp_path),
        ),
        messages=[RuntimeMessage.text("user", "继续长任务")],
        metadata={
            "state_workspace_dir": str(tmp_path),
            "attempt_id": started.attempt_id,
            "runtime_revision": started.revision,
            "runtime_lease_owner": started.lease_owner,
            "task_contract": task_contract.model_dump(mode="json"),
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )
    events: list[RuntimeEvent] = []

    with pytest.raises(RuntimeBudgetAdmissionError):
        async for event in runtime.run_turn(runtime_input):
            events.append(event)

    persisted = await RuntimeRunStore(tmp_path).load_contract(started.run_id)
    assert client.calls == 1
    assert events[-1].type == "turn.failed"
    assert events[-1].payload["error_code"] == "runtime_budget_exhausted"
    assert events[-1].payload["outcome_status"] == "blocked"
    assert persisted is not None
    assert persisted.status == "paused"
    assert persisted.wait_reason == "budget"
    assert persisted.checkpoint is not None
    assert persisted.checkpoint.stage == "awaiting_budget"
    assert persisted.checkpoint.summary.endswith("inbox_seq=0")


@pytest.mark.asyncio
async def test_native_runtime_retries_checkpoint_cas_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_contract = build_chat_task_contract(
        objective="解释冲突",
        client_turn_id="client-cas",
        complexity="light",
        work_method_id="standard",
    )
    started = await record_chat_runtime_run_started(
        workspace_dir=tmp_path,
        agent_id="default",
        chat_id="chat-cas",
        session_id="session-1",
        run_id="turn-cas",
        effective_workspace_dir=str(tmp_path),
        task_contract=task_contract,
    )
    original_save = RuntimeRunStore.save_contract
    save_calls = 0

    async def save_with_one_conflict(
        store: RuntimeRunStore,
        contract,
        **kwargs,
    ):
        nonlocal save_calls
        save_calls += 1
        if save_calls == 1:
            raise RuntimeRunStoreConflictError(
                "injected checkpoint CAS conflict",
            )
        # Preserve all original CAS keywords forwarded through **kwargs.
        # pylint: disable-next=missing-kwoa
        return await original_save(store, contract, **kwargs)

    monkeypatch.setattr(
        RuntimeRunStore,
        "save_contract",
        save_with_one_conflict,
    )
    runtime_input = RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id="session-1",
            user_id="user-1",
            run_id=started.run_id,
            workspace_dir=str(tmp_path),
        ),
        messages=[RuntimeMessage.text("user", "解释冲突")],
        metadata={
            "state_workspace_dir": str(tmp_path),
            "attempt_id": started.attempt_id,
            "runtime_revision": started.revision,
            "runtime_lease_owner": started.lease_owner,
            "task_contract": task_contract.model_dump(mode="json"),
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient(
            [[_completed("完成", message_id="cas-completed")]],
        ),
        tool_registry=LantaiToolRegistry(),
    )

    events = [event async for event in runtime.run_turn(runtime_input)]
    persisted = await RuntimeRunStore(tmp_path).load_contract(started.run_id)

    assert save_calls >= 2
    assert events[-1].type == "turn.completed"
    assert persisted is not None and persisted.checkpoint is not None
    assert persisted.checkpoint.stage == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "persistence_failure",
    [
        OSError(28, "No space left on device"),
        ValueError("checkpoint encoding failed"),
    ],
    ids=["disk-full", "encoding"],
)
# Keep the existing test selector unchanged.
async def test_native_runtime_blocks_mutating_tool_when_checkpoint_cannot_persist(  # noqa: E501
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    persistence_failure: BaseException,
) -> None:
    state_workspace = tmp_path / "state"
    project_workspace = tmp_path / "project"
    project_workspace.mkdir()
    task_contract = build_chat_task_contract(
        objective="执行受控写入",
        client_turn_id="client-durability",
        complexity=None,
        work_method_id="standard",
    )
    started = await record_chat_runtime_run_started(
        workspace_dir=state_workspace,
        agent_id="default",
        chat_id="chat-durability",
        session_id="session-1",
        run_id="turn-durability",
        effective_workspace_dir=str(project_workspace),
        task_contract=task_contract,
    )
    original_save = RuntimeRunStore.save_contract
    save_calls = 0

    async def fail_second_checkpoint_save(
        store: RuntimeRunStore,
        contract,
        **kwargs,
    ):
        nonlocal save_calls
        save_calls += 1
        if save_calls == 2:
            raise persistence_failure
        # Preserve all original CAS keywords forwarded through **kwargs.
        # pylint: disable-next=missing-kwoa
        return await original_save(store, contract, **kwargs)

    monkeypatch.setattr(
        RuntimeRunStore,
        "save_contract",
        fail_second_checkpoint_save,
    )
    executions: list[str] = []

    async def perform_external_write(value: str) -> ToolResponse:
        executions.append(value)
        return ToolResponse(content=[TextBlock(text="written")])

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="perform_external_write",
            read_only=False,
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        ),
        perform_external_write,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-external-write",
                        name="perform_external_write",
                        arguments={"value": "candidate"},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("写入完成", message_id="write-completed")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        progressive_tools=False,
    )
    runtime_input = RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id="session-1",
            user_id="user-1",
            run_id=started.run_id,
            workspace_dir=str(project_workspace),
        ),
        messages=[RuntimeMessage.text("user", "执行受控写入")],
        metadata={
            "state_workspace_dir": str(state_workspace),
            "attempt_id": started.attempt_id,
            "runtime_revision": started.revision,
            "runtime_lease_owner": started.lease_owner,
            "task_contract": task_contract.model_dump(mode="json"),
        },
    )
    observed_events: list[RuntimeEvent] = []

    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    with pytest.raises(native_runtime_module._RuntimeDurabilityBlocked):
        async for event in runtime.run_turn(runtime_input):
            observed_events.append(event)

    failures = [
        event for event in observed_events if event.type == "turn.failed"
    ]
    # Exact [] assertion also rejects None and other false values.
    # pylint: disable-next=use-implicit-booleaness-not-comparison
    assert executions == []
    assert save_calls == 2
    assert len(failures) == 1
    assert failures[0].payload["error_code"] == "runtime_durability_blocked"
    assert failures[0].payload["outcome_status"] == "blocked"
    assert failures[0].payload["error"] == {
        "stage": "before_external_effect",
        "kind": "persistence_error",
        "cause_type": type(persistence_failure).__name__,
        "attempts": 1,
    }


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_sustained_runtime_keeps_healthy_context_past_legacy_segment_threshold(  # noqa: E501
    tmp_path: Path,
) -> None:
    task_contract = build_chat_task_contract(
        objective="持续完成长程任务",
        client_turn_id="client-sustained",
        complexity="sustained",
        work_method_id="sustained_execution",
    )
    await record_chat_runtime_run_started(
        workspace_dir=tmp_path,
        agent_id="default",
        chat_id="chat-1",
        session_id="session-1",
        run_id="turn-1",
        effective_workspace_dir=str(tmp_path),
        task_contract=task_contract,
    )

    async def lookup(query: str) -> str:
        return f"found:{query}"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="lookup",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        ),
        lookup,
    )
    first_response = ModelResponse(
        message=ModelMessage(id="msg-segment-1", role="assistant"),
        usage=ModelUsage(
            input_tokens=299_000,
            output_tokens=1_000,
            total_tokens=300_000,
        ),
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-segment-1",
                        name="lookup",
                        arguments={"query": "checkpoint"},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=first_response,
                ),
            ],
            [_completed("长程任务分段完成", message_id="msg-segment-2")],
        ],
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "request": _runtime_input().request.model_copy(
                update={"workspace_dir": str(tmp_path)},
            ),
            "metadata": {
                "state_workspace_dir": str(tmp_path),
                "task_contract": task_contract.model_dump(mode="json"),
                "runtime_budget": task_contract.budget.model_dump(mode="json"),
            },
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(runtime_input)]
    persisted = await RuntimeRunStore(tmp_path).load_contract("turn-1")

    assert events[-1].type == "turn.completed"
    assert events[0].payload["phase"] == "planning"
    assert (
        events[0].payload["task_contract"]["work_method_id"]
        == "sustained_execution"
    )
    assert "objective" not in events[0].payload["task_contract"]
    assert persisted is not None and persisted.checkpoint is not None
    assert persisted.checkpoint.segment_index == 0
    assert persisted.checkpoint.segment_tokens == 300_000
    assert persisted.checkpoint.segment_tool_calls == 1
    assert persisted.checkpoint.used_tokens == 300_000
    assert persisted.checkpoint.tool_calls == 1
    assert len(client.requests) == 2
    assert any(
        message.role == "tool" for message in client.requests[1].messages
    )


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_projects_structured_thinking_on_completion() -> None:  # noqa: E501
    completed = ModelStreamEvent(
        kind="message_completed",
        message_id="msg-reasoning",
        response=ModelResponse(
            message=ModelMessage(
                id="msg-reasoning",
                role="assistant",
                content=[
                    ModelContentBlock(type="thinking", text="规范思考"),
                    ModelContentBlock(type="text", text="规范答案"),
                ],
            ),
        ),
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="message_start",
                    message_id="msg-reasoning",
                ),
                ModelStreamEvent(
                    kind="thinking_delta",
                    message_id="msg-reasoning",
                    delta="规范思考",
                ),
                ModelStreamEvent(
                    kind="text_delta",
                    message_id="msg-reasoning",
                    delta="规范答案",
                ),
                completed,
            ],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]
    completed_event = next(
        event for event in events if event.type == "message.completed"
    )

    assert completed_event.payload["message"]["content"] == [
        {
            "type": "thinking",
            "text": "规范思考",
            "data": {},
            "metadata": {},
        },
        {
            "type": "text",
            "text": "规范答案",
            "data": {},
            "metadata": {},
        },
    ]
    assert completed.response is not None
    assert completed.response.message.content[0].text == "规范思考"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_retries_once_when_model_only_completes_reasoning() -> None:  # noqa: E501
    """Reasoning is useful progress, but never a user-visible final answer."""
    reasoning_only = ModelStreamEvent(
        kind="message_completed",
        message_id="msg-reasoning-only",
        response=ModelResponse(
            message=ModelMessage(
                id="msg-reasoning-only",
                role="assistant",
                content=[
                    ModelContentBlock(type="thinking", text="先在心里整理答案。"),
                ],
            ),
        ),
    )
    client = ScriptedModelClient(
        [
            [reasoning_only],
            [_completed("我在。你可以慢慢说。", message_id="msg-visible-final")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert len(client.requests) == 2
    assert client.requests[1].tools == []
    assert "直接给用户可见的最终答复" in client.requests[1].messages[-1].text_content()
    completed = [
        event for event in events if event.type == "message.completed"
    ]
    assert completed[-1].summary == "我在。你可以慢慢说。"
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_fails_when_model_stream_has_no_final_response() -> None:  # noqa: E501
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(kind="message_start", message_id="msg-1"),
                ModelStreamEvent(
                    kind="text_delta",
                    message_id="msg-1",
                    delta="半截",
                ),
            ],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )

    events = []
    with pytest.raises(RuntimeError, match="ended without a final response"):
        async for event in runtime.run_turn(_runtime_input()):
            events.append(event)

    assert [event.type for event in events][-2:] == [
        "message.delta",
        "turn.failed",
    ]
    assert (
        events[-1].summary
        == "Native runtime model stream ended without a final response."
    )


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_recovers_when_first_final_response_is_empty() -> None:  # noqa: E501
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(kind="message_start", message_id="msg-1"),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-1",
                    response=ModelResponse(
                        message=ModelMessage(
                            id="msg-1",
                            role="assistant",
                            content=[],
                        ),
                    ),
                ),
            ],
            [_completed("我在。你可以慢慢说。", message_id="msg-visible-final")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert len(client.requests) == 2
    assert client.requests[1].tools == []
    assert "直接给用户可见的最终答复" in client.requests[1].messages[-1].text_content()
    completed = [
        event for event in events if event.type == "message.completed"
    ]
    assert completed[-1].summary == "我在。你可以慢慢说。"
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("visible_answer", [False, True])
async def test_native_runtime_still_rejects_tools_in_prose_only_synthesis(
    visible_answer: bool,
) -> None:
    executed: list[str] = []

    def lookup() -> str:
        executed.append("lookup")
        return "unexpected"

    registry = LantaiToolRegistry()
    registry.register_tool(ToolSpec(name="lookup"), lookup)
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="late-call",
                        name="lookup",
                        arguments={},
                    ),
                ),
                _completed("现有证据足够回答。" if visible_answer else ""),
            ],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )
    events = []
    if visible_answer:
        events = [event async for event in runtime.run_turn(_runtime_input())]
        assert any(
            event.payload.get("kind") == "final_synthesis_tool_call_ignored"
            for event in events
        )
        assert events[-1].type == "turn.completed"
    else:
        with pytest.raises(
            RuntimeError,
            match="tool during the final synthesis",
        ):
            async for event in runtime.run_turn(_runtime_input()):
                events.append(event)
        assert events[-1].type == "turn.failed"
    assert client.requests[1].tools == []
    # Exact [] assertion also rejects None and other false values.
    # pylint: disable-next=use-implicit-booleaness-not-comparison
    assert executed == []


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_fails_after_empty_final_synthesis_retry() -> None:  # noqa: E501
    def empty_response(message_id: str) -> list[ModelStreamEvent]:
        return [
            ModelStreamEvent(kind="message_start", message_id=message_id),
            ModelStreamEvent(
                kind="message_completed",
                message_id=message_id,
                response=ModelResponse(
                    message=ModelMessage(
                        id=message_id,
                        role="assistant",
                        content=[],
                    ),
                ),
            ),
        ]

    client = ScriptedModelClient(
        [empty_response("msg-empty-1"), empty_response("msg-empty-2")],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )

    events = []
    with pytest.raises(RuntimeError, match="模型未能给出最终回复"):
        async for event in runtime.run_turn(_runtime_input()):
            events.append(event)

    assert len(client.requests) == 2
    assert events[-1].type == "turn.failed"
    assert events[-1].summary == "模型未能给出最终回复，已停止自动等待；已生成内容仍保留。"
    assert events[-1].payload["error_code"] == "missing_visible_answer"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_executes_builtin_tool_and_continues_loop() -> None:  # noqa: E501
    async def lookup(query: str) -> str:
        return f"found:{query}"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="lookup",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        ),
        lookup,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(kind="message_start", message_id="msg-1"),
                ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="msg-1",
                    tool_call=ToolCall(
                        id="call-1",
                        name="lookup",
                        arguments={"query": "澜台"},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-1",
                    response=ModelResponse(
                        message=ModelMessage(id="msg-1", role="assistant"),
                    ),
                ),
            ],
            [
                ModelStreamEvent(kind="message_start", message_id="msg-2"),
                ModelStreamEvent(
                    kind="text_delta",
                    message_id="msg-2",
                    delta="好了",
                ),
                _completed("好了", message_id="msg-2"),
            ],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert [event.type for event in events].count("tool.call.completed") == 1
    assert "tool.completed" not in [event.type for event in events]
    assert client.requests[0].tools[0].name == "lookup"
    assistant_tool_call = client.requests[1].messages[-2]
    assert assistant_tool_call.role == "assistant"
    assert assistant_tool_call.content[-1].type == "tool_call"
    assert assistant_tool_call.content[-1].data == {
        "id": "call-1",
        "name": "lookup",
        "arguments": {"query": "澜台"},
    }
    assert client.requests[1].messages[-1].role == "tool"
    assert client.requests[1].messages[-1].tool_call_id == "call-1"
    assert client.requests[1].messages[-1].metadata["tool_call_id"] == "call-1"
    assert (
        client.requests[1].messages[-1].content[0].data["output"][0]["text"]
        == "found:澜台"
    )


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_tags_tool_lifecycle_with_client_turn_id() -> None:  # noqa: E501
    async def lookup(query: str) -> str:
        return f"found:{query}"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="lookup",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        ),
        lookup,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="msg-tool",
                    tool_call=ToolCall(
                        id="call-tool",
                        name="lookup",
                        arguments={"query": "identity"},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-tool",
                    response=ModelResponse(
                        message=ModelMessage(id="msg-tool", role="assistant"),
                    ),
                ),
            ],
            [_completed("完成", message_id="msg-final")],
        ],
    )
    task_contract = build_chat_task_contract(
        objective="使用工具并回答",
        client_turn_id="client-tool-turn",
        complexity="standard",
        work_method_id="standard",
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "metadata": {
                "task_contract": task_contract.model_dump(mode="json"),
            },
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(runtime_input)]
    tool_events = [event for event in events if event.type.startswith("tool.")]

    assert [event.type for event in tool_events] == [
        "tool.call.started",
        "tool.started",
        "tool.call.completed",
    ]
    assert {event.payload.get("client_turn_id") for event in tool_events} == {
        "client-tool-turn",
    }


@pytest.mark.parametrize(
    ("selected_method", "turn_intelligence"),
    [
        ("collaboration", None),
        (
            "collaboration",
            {
                "status": "classified",
                "interaction_mode": "answer",
                "workspace_intent": "none",
                "external_action_intent": "none",
                "requested_work_method_id": "standard",
                "confidence": 0.96,
            },
        ),
        (
            "standard",
            {
                "status": "classified",
                "interaction_mode": "answer",
                "workspace_intent": "none",
                "external_action_intent": "none",
                "requested_work_method_id": "collaboration",
                "confidence": 0.96,
            },
        ),
    ],
)
@pytest.mark.asyncio
async def test_collaboration_work_method_cannot_finish_without_hosted_batch(
    selected_method: str,
    turn_intelligence: dict | None,
) -> None:
    """A selected work method is a runtime contract, not optional prose."""

    async def collaborate_with_agents(tasks: list[dict[str, str]]):
        del tasks
        snapshot = {
            "batch_id": "batch-required",
            "revision": 1,
            "status": "completed",
            "phase": "closed",
            "members": [
                {
                    "member_id": "member-1",
                    "agent_id": "agent-a",
                    "status": "completed",
                },
                {
                    "member_id": "member-2",
                    "agent_id": "agent-b",
                    "status": "completed",
                },
            ],
        }
        yield ToolResponse(
            content=[TextBlock(text="hosted batch complete")],
            metadata={"collaboration": snapshot},
            is_last=True,
        )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="collaborate_with_agents",
            input_schema={
                "type": "object",
                "properties": {"tasks": {"type": "array"}},
                "required": ["tasks"],
            },
            metadata={"progressive_tool": "collaboration"},
        ),
        collaborate_with_agents,
    )
    client = ScriptedModelClient(
        [
            [_completed("我直接给两个方案。", message_id="premature")],
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-required-batch",
                        name="collaborate_with_agents",
                        arguments={
                            "tasks": [
                                {
                                    "to_agent": "agent-a",
                                    "text": "甲",
                                    "label": "甲",
                                },
                                {
                                    "to_agent": "agent-b",
                                    "text": "乙",
                                    "label": "乙",
                                },
                            ],
                        },
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("沈行知已主持协作并形成统一交付。", message_id="final")],
        ],
    )
    task_contract = build_chat_task_contract(
        objective="用会议厅形成两个方案",
        client_turn_id="client-collaboration",
        complexity="standard",
        work_method_id=selected_method,
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [
        event
        async for event in runtime.run_turn(
            _runtime_input().model_copy(
                update={
                    "metadata": {
                        "task_contract": task_contract.model_dump(mode="json"),
                        **(
                            {"turn_intelligence": turn_intelligence}
                            if turn_intelligence is not None
                            else {}
                        ),
                    },
                },
            ),
        )
    ]

    assert len(client.requests) == 3
    assert "协作工作法" in client.requests[1].messages[-1].text_content()
    assert [
        event.summary for event in events if event.type == "message.completed"
    ] == ["沈行知已主持协作并形成统一交付。"]
    assert any(
        event.type == "runtime.warning"
        and event.payload.get("kind") == "collaboration_contract_enforced"
        for event in events
    )
    assert any(
        event.type == "runtime.warning"
        and event.payload.get("kind") == "collaboration_preparing"
        for event in events
    )


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_required_collaboration_contract_is_present_on_first_model_request() -> None:  # noqa: E501
    """The selected method must reach the model before it can answer
    directly.
    """

    async def collaborate_with_agents(tasks: list[dict[str, str]]):
        del tasks
        yield ToolResponse(
            content=[TextBlock(text="hosted batch complete")],
            metadata={
                "collaboration": {
                    "batch_id": "batch-first-request",
                    "revision": 1,
                    "status": "completed",
                    "phase": "closed",
                    "members": [
                        {
                            "member_id": "member-1",
                            "agent_id": "agent-a",
                            "status": "completed",
                        },
                        {
                            "member_id": "member-2",
                            "agent_id": "agent-b",
                            "status": "completed",
                        },
                    ],
                },
            },
            is_last=True,
        )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="collaborate_with_agents",
            input_schema={
                "type": "object",
                "properties": {"tasks": {"type": "array"}},
                "required": ["tasks"],
            },
            metadata={"progressive_tool": "collaboration"},
        ),
        collaborate_with_agents,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-first-request",
                        name="collaborate_with_agents",
                        arguments={
                            "tasks": [
                                {
                                    "to_agent": "agent-a",
                                    "text": "甲",
                                    "label": "甲",
                                },
                                {
                                    "to_agent": "agent-b",
                                    "text": "乙",
                                    "label": "乙",
                                },
                            ],
                        },
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("协作完成。", message_id="msg-first-request-final")],
        ],
    )
    task_contract = build_chat_task_contract(
        objective="世界上最美的人是谁？",
        client_turn_id="client-first-request-contract",
        complexity="standard",
        work_method_id="collaboration",
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    compiled_prompt_marker = "FULL_COMPILED_AGENT_PROMPT"
    runtime_input = _runtime_input().model_copy(
        update={
            "messages": [
                RuntimeMessage.text(
                    "system",
                    f"{compiled_prompt_marker}\n" + ("x" * 80_000),
                    metadata={
                        "instruction_trust": "trusted_system",
                        "instruction_origin": "prompt.compiled_agent",
                    },
                ),
                RuntimeMessage.text("user", "hello"),
            ],
            "metadata": {
                "task_contract": task_contract.model_dump(mode="json"),
                "turn_intelligence": {
                    "status": "classified",
                    "interaction_mode": "answer",
                    "workspace_intent": "none",
                    "external_action_intent": "none",
                    "requested_work_method_id": "standard",
                    "confidence": 0.99,
                },
            },
        },
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    first_request = client.requests[0]
    final_request = client.requests[1]
    assert first_request.tool_choice == "required"
    assert [tool.name for tool in first_request.tools] == [
        "collaborate_with_agents",
    ]
    assert first_request.generation["max_tokens"] == 4096
    assert first_request.metadata["runtime_phase"] == "collaboration_staffing"
    assert not any(
        compiled_prompt_marker in message.text_content()
        for message in first_request.messages
    )
    assert any(
        message.role == "system"
        and message.metadata.get("instruction_origin")
        == "runtime.collaboration_staffing"
        for message in first_request.messages
    )
    assert any(
        message.role == "user" and message.text_content() == "hello"
        for message in first_request.messages
    )
    assert (
        sum(
            len(message.text_content().encode("utf-8"))
            for message in first_request.messages
        )
        < 10_000
    )
    assert any(
        message.role == "system"
        and message.metadata.get("collaboration_contract_enforced") is True
        and "用户选择了协作工作法" in message.text_content()
        for message in first_request.messages
    )
    assert final_request.tool_choice == "auto"
    assert [tool.name for tool in final_request.tools] == []
    assert final_request.tool_choice == "auto"
    assert final_request.generation["max_tokens"] == 65_536
    assert "runtime_phase" not in final_request.metadata
    assert any(
        compiled_prompt_marker in message.text_content()
        for message in final_request.messages
    )
    assert any(
        message.role == "system"
        and message.metadata.get("collaboration_handoff") is True
        for message in final_request.messages
    )
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
async def test_parent_keeps_artifact_tools_after_hosted_collaboration(
    tmp_path: Path,
) -> None:
    """Room completion is evidence; the parent still builds and delivers."""

    artifact = tmp_path / "collaboration-report.md"
    executed: list[str] = []

    async def collaborate_with_agents(tasks: list[dict[str, str]]):
        del tasks
        executed.append("collaborate_with_agents")
        yield ToolResponse(
            content=[TextBlock(text="research evidence")],
            metadata={
                "collaboration": {
                    "batch_id": "batch-artifact",
                    "revision": 1,
                    "status": "completed",
                    "phase": "closed",
                    "members": [],
                },
            },
            is_last=True,
        )

    async def write_file(file_path: str, content: str) -> ToolResponse:
        executed.append("write_file")
        Path(file_path).write_text(content, encoding="utf-8")
        return ToolResponse(content=[TextBlock(text="written")])

    async def verify_file(file_path: str) -> ToolResponse:
        executed.append("verify_file")
        assert Path(file_path).read_text(encoding="utf-8") == "# verified\n"
        return ToolResponse(content=[TextBlock(text="verified")])

    async def finalize_deliverable(primary_path: str) -> ToolResponse:
        executed.append("finalize_deliverable")
        return ToolResponse(
            content=[TextBlock(text=f"finalized {primary_path}")],
        )

    async def send_file_to_user(file_path: str) -> ToolResponse:
        executed.append("send_file_to_user")
        return ToolResponse(
            content=[
                FileBlock(
                    filename=Path(file_path).name,
                    source={"type": "url", "url": Path(file_path).as_uri()},
                ),
            ],
        )

    registry = LantaiToolRegistry()
    tools = (
        (
            "collaborate_with_agents",
            {"tasks": {"type": "array"}},
            ["tasks"],
            collaborate_with_agents,
        ),
        (
            "write_file",
            {"file_path": {"type": "string"}, "content": {"type": "string"}},
            ["file_path", "content"],
            write_file,
        ),
        (
            "verify_file",
            {"file_path": {"type": "string"}},
            ["file_path"],
            verify_file,
        ),
        (
            "finalize_deliverable",
            {"primary_path": {"type": "string"}},
            ["primary_path"],
            finalize_deliverable,
        ),
        (
            "send_file_to_user",
            {"file_path": {"type": "string"}},
            ["file_path"],
            send_file_to_user,
        ),
    )
    for name, properties, required, executor in tools:
        registry.register_tool(
            ToolSpec(
                name=name,
                input_schema={
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
                metadata=(
                    {"progressive_tool": "collaboration"}
                    if name == "collaborate_with_agents"
                    else {}
                ),
            ),
            executor,
        )

    def tool_round(call_id: str, name: str, arguments: dict[str, object]):
        return [
            ModelStreamEvent(
                kind="tool_call_completed",
                tool_call=ToolCall(id=call_id, name=name, arguments=arguments),
            ),
            ModelStreamEvent(
                kind="message_completed",
                response=ModelResponse(message=ModelMessage(role="assistant")),
            ),
        ]

    client = ScriptedModelClient(
        [
            tool_round(
                "call-collaborate",
                "collaborate_with_agents",
                {"tasks": [{"to_agent": "researcher", "text": "research"}]},
            ),
            tool_round(
                "call-write-after-collaboration",
                "write_file",
                {"file_path": str(artifact), "content": "# verified\n"},
            ),
            tool_round(
                "call-verify-after-collaboration",
                "verify_file",
                {"file_path": str(artifact)},
            ),
            tool_round(
                "call-finalize-after-collaboration",
                "finalize_deliverable",
                {"primary_path": str(artifact)},
            ),
            tool_round(
                "call-deliver-after-collaboration",
                "send_file_to_user",
                {"file_path": str(artifact)},
            ),
            [
                _completed(
                    "The verified report is attached.",
                    message_id="final",
                ),
            ],
        ],
    )
    task_contract = build_chat_task_contract(
        objective=(
            "Research together, then create and deliver a verified report"
        ),
        client_turn_id="client-collaboration-artifact",
        complexity="standard",
        work_method_id="collaboration",
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "metadata": {
                "task_contract": task_contract.model_dump(mode="json"),
            },
        },
    )
    runtime_input.request.workspace_dir = str(tmp_path)

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert [tool.name for tool in client.requests[1].tools] == [
        "write_file",
        "verify_file",
        "finalize_deliverable",
        "send_file_to_user",
    ]
    assert executed == [
        "collaborate_with_agents",
        "write_file",
        "verify_file",
        "finalize_deliverable",
        "send_file_to_user",
    ]
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_hosted_collaboration_success_retires_only_that_capability() -> None:  # noqa: E501
    """A completed room is one handoff; later model output cannot open
    another.
    """

    calls: list[list[dict[str, str]]] = []

    async def collaborate_with_agents(tasks: list[dict[str, str]]):
        calls.append(tasks)
        yield ToolResponse(
            content=[TextBlock(text="hosted batch complete")],
            metadata={
                "collaboration": {
                    "batch_id": f"batch-{len(calls)}",
                    "revision": 1,
                    "status": "completed",
                    "phase": "closed",
                    "members": [],
                },
            },
            is_last=True,
        )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="collaborate_with_agents",
            input_schema={
                "type": "object",
                "properties": {"tasks": {"type": "array"}},
                "required": ["tasks"],
            },
            metadata={"progressive_tool": "collaboration"},
        ),
        collaborate_with_agents,
    )

    def collaboration_round(call_id: str, label: str):
        return [
            ModelStreamEvent(
                kind="tool_call_completed",
                tool_call=ToolCall(
                    id=call_id,
                    name="collaborate_with_agents",
                    arguments={
                        "tasks": [
                            {
                                "to_agent": f"agent-{label}",
                                "text": label,
                                "label": label,
                            },
                        ],
                    },
                ),
            ),
            ModelStreamEvent(
                kind="message_completed",
                response=ModelResponse(message=ModelMessage(role="assistant")),
            ),
        ]

    client = ScriptedModelClient(
        [
            collaboration_round("collab-1", "first"),
            collaboration_round("collab-2", "second"),
            [_completed("final", message_id="final-after-collaboration")],
        ],
    )
    task_contract = build_chat_task_contract(
        objective="协作一次后由主 Agent 继续",
        client_turn_id="client-one-room",
        complexity="standard",
        work_method_id="collaboration",
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [
        event
        async for event in runtime.run_turn(
            _runtime_input().model_copy(
                update={
                    "metadata": {
                        "task_contract": task_contract.model_dump(mode="json"),
                    },
                },
            ),
        )
    ]

    assert calls == [
        [{"to_agent": "agent-first", "text": "first", "label": "first"}],
    ]
    assert "collaborate_with_agents" not in {
        tool.name for tool in client.requests[1].tools
    }
    assert (
        sum(
            event.type == "tool.call.started"
            and event.payload.get("tool_name") == "collaborate_with_agents"
            for event in events
        )
        == 1
    )
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_collaboration_member_executes_without_delivery_persona() -> None:  # noqa: E501
    """A private room member returns evidence without inheriting host voice."""

    client = ScriptedModelClient(
        [[_completed("MEMBER_EVIDENCE", message_id="msg-member-evidence")]],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )
    compiled_prompt_marker = "FULL_DELIVERY_PERSONA"
    legacy_recall_marker = "LEGACY_PERSONA_RECALL"
    prior_synthesis_marker = "PRIOR_PRIMARY_SYNTHESIS"
    runtime_input = _runtime_input().model_copy(
        update={
            "messages": [
                RuntimeMessage.text(
                    "system",
                    compiled_prompt_marker,
                    metadata={
                        "instruction_trust": "trusted_system",
                        "instruction_origin": "prompt.compiled_agent",
                    },
                ),
                RuntimeMessage.text("system", legacy_recall_marker),
                RuntimeMessage.text(
                    "system",
                    prior_synthesis_marker,
                    metadata={
                        "instruction_trust": "trusted_system",
                        "instruction_origin": (
                            "runtime.collaboration_final_synthesis"
                        ),
                    },
                ),
                RuntimeMessage.text("user", "分析子任务"),
            ],
            "metadata": {
                "collaboration_member_id": "member-a",
                "presentation_context": "subagent_internal",
                "ququ_hard_required": True,
                "native_response_max_tokens": 1024,
            },
        },
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert len(client.requests) == 1
    request = client.requests[0]
    assert (
        request.metadata["runtime_phase"] == "collaboration_member_execution"
    )
    assert request.generation["max_tokens"] == 1024
    request_text = "\n".join(
        message.text_content() for message in request.messages
    )
    assert compiled_prompt_marker not in request_text
    assert legacy_recall_marker not in request_text
    assert prior_synthesis_marker not in request_text
    assert any(
        message.metadata.get("instruction_origin")
        == "runtime.collaboration_member_execution"
        for message in request.messages
    )
    assert any(
        event.type == "message.completed"
        and event.summary == "MEMBER_EVIDENCE"
        for event in events
    )
    assert runtime.take_post_answer_reflection_request() is None
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_deep_research_uses_neutral_workers_then_primary_persona_synthesis() -> None:  # noqa: E501
    """Research drafts stay private until the full primary prompt returns."""

    async def lookup(query: str) -> str:
        return f"evidence:{query}"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="lookup",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        ),
        lookup,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-research-lookup",
                        name="lookup",
                        arguments={"query": "topic"},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("INTERNAL_RESEARCH_DRAFT", message_id="msg-draft")],
            [_completed("PERSONA_FINAL", message_id="msg-persona-final")],
        ],
    )
    task_contract = build_chat_task_contract(
        objective="深入研究主题",
        client_turn_id="client-deep-research",
        complexity="standard",
        work_method_id="deep_research",
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )
    compiled_prompt_marker = "FULL_PRIMARY_PERSONA"
    legacy_recall_marker = "LEGACY_PERSONA_RECALL"
    runtime_input = _runtime_input().model_copy(
        update={
            "messages": [
                RuntimeMessage.text(
                    "system",
                    compiled_prompt_marker,
                    metadata={
                        "instruction_trust": "trusted_system",
                        "instruction_origin": "prompt.compiled_agent",
                    },
                ),
                RuntimeMessage.text("system", legacy_recall_marker),
                RuntimeMessage.text("user", "深入研究主题"),
            ],
            "metadata": {
                "task_contract": task_contract.model_dump(mode="json"),
            },
        },
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert len(client.requests) == 3
    for request in client.requests[:2]:
        assert request.metadata["runtime_phase"] == "deep_research_execution"
        request_text = "\n".join(
            message.text_content() for message in request.messages
        )
        assert compiled_prompt_marker not in request_text
        assert legacy_recall_marker not in request_text
        assert any(
            message.metadata.get("instruction_origin")
            == "runtime.deep_research_execution"
            for message in request.messages
        )
    final_request = client.requests[2]
    assert final_request.tools == []
    assert "runtime_phase" not in final_request.metadata
    assert any(
        compiled_prompt_marker in message.text_content()
        for message in final_request.messages
    )
    assert any(
        message.metadata.get("instruction_origin")
        == "runtime.deep_research_final_synthesis"
        for message in final_request.messages
    )
    assert [
        event.summary for event in events if event.type == "message.completed"
    ] == ["PERSONA_FINAL"]
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_required_collaboration_failure_is_bounded_and_hides_provisional_text() -> None:  # noqa: E501
    """A required capability cannot loop or leak answers before it succeeds."""

    async def collaborate_with_agents(tasks: list[dict[str, str]]):
        del tasks
        # This fixture must be an async generator without events.
        # pylint: disable-next=using-constant-test
        if False:  # pragma: no cover - keeps this an async generator
            yield None
        raise ValueError("Agent is unavailable")

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="collaborate_with_agents",
            input_schema={
                "type": "object",
                "properties": {"tasks": {"type": "array"}},
                "required": ["tasks"],
            },
            metadata={"progressive_tool": "collaboration"},
        ),
        collaborate_with_agents,
    )

    def failed_round(call_id: str, tasks: list[dict[str, str]], leaked: str):
        message_id = f"msg-{call_id}"
        return [
            ModelStreamEvent(kind="message_start", message_id=message_id),
            ModelStreamEvent(
                kind="text_delta",
                message_id=message_id,
                delta=leaked,
            ),
            ModelStreamEvent(
                kind="tool_call_completed",
                message_id=message_id,
                tool_call=ToolCall(
                    id=call_id,
                    name="collaborate_with_agents",
                    arguments={"tasks": tasks},
                ),
            ),
            ModelStreamEvent(
                kind="message_completed",
                message_id=message_id,
                response=ModelResponse(
                    message=ModelMessage.text(
                        "assistant",
                        leaked,
                        id=message_id,
                    ),
                ),
            ),
        ]

    client = ScriptedModelClient(
        [
            failed_round(
                "call-invalid-count",
                [{"to_agent": "longjing", "text": "甲", "label": "甲"}],
                "我先直接给答案一。",
            ),
            failed_round(
                "call-invalid-agent",
                [
                    {"to_agent": "missing-a", "text": "甲", "label": "甲"},
                    {"to_agent": "missing-b", "text": "乙", "label": "乙"},
                ],
                "我再直接给答案二。",
            ),
            [_completed("不应执行的第三轮。", message_id="msg-third")],
        ],
    )
    task_contract = build_chat_task_contract(
        objective="用协作室讨论",
        client_turn_id="client-required-failure",
        complexity="standard",
        work_method_id="collaboration",
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "metadata": {
                "task_contract": task_contract.model_dump(mode="json"),
            },
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = []
    with pytest.raises(RuntimeError, match="required capability"):
        async for event in runtime.run_turn(runtime_input):
            events.append(event)

    assert len(client.requests) == 2
    assert events[-1].type == "turn.failed"
    assert events[-1].payload["error_code"] == "required_capability_failed"
    assert events[-1].payload["outcome_status"] == "blocked"
    assert not any(event.type.startswith("message.") for event in events)
    assert "我先直接给答案" not in _runtime_event_text(events)
    assert "我再直接给答案" not in _runtime_event_text(events)
    completed = [
        event for event in events if event.type == "tool.call.completed"
    ]
    assert len(completed) == 2
    assert all("collaboration" not in event.payload for event in completed)
    assert [
        event.payload.get("kind")
        for event in events
        if event.type == "runtime.warning"
        and event.payload.get("kind") == "required_capability_retrying"
    ] == ["required_capability_retrying"]


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_required_collaboration_omission_and_failure_share_one_attempt_budget() -> None:  # noqa: E501
    """Alternating failure shapes cannot evade the required-capability fuse."""

    async def collaborate_with_agents(tasks: list[dict[str, str]]):
        del tasks
        # This fixture must be an async generator without events.
        # pylint: disable-next=using-constant-test
        if False:  # pragma: no cover - keeps this an async generator
            yield None
        raise ValueError("Agent is unavailable")

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="collaborate_with_agents",
            input_schema={
                "type": "object",
                "properties": {"tasks": {"type": "array"}},
                "required": ["tasks"],
            },
            metadata={"progressive_tool": "collaboration"},
        ),
        collaborate_with_agents,
    )
    client = ScriptedModelClient(
        [
            [_completed("第一轮直接回答。", message_id="msg-omitted")],
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-failed-after-omission",
                        name="collaborate_with_agents",
                        arguments={
                            "tasks": [
                                {
                                    "to_agent": "missing-a",
                                    "text": "甲",
                                    "label": "甲",
                                },
                                {
                                    "to_agent": "missing-b",
                                    "text": "乙",
                                    "label": "乙",
                                },
                            ],
                        },
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("不应执行的第三轮。", message_id="msg-third")],
        ],
    )
    task_contract = build_chat_task_contract(
        objective="用协作室讨论",
        client_turn_id="client-mixed-required-failure",
        complexity="standard",
        work_method_id="collaboration",
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )
    events = []

    with pytest.raises(RuntimeError, match="required capability"):
        async for event in runtime.run_turn(
            _runtime_input().model_copy(
                update={
                    "metadata": {
                        "task_contract": task_contract.model_dump(mode="json"),
                    },
                },
            ),
        ):
            events.append(event)

    assert len(client.requests) == 2
    assert events[-1].type == "turn.failed"
    assert events[-1].payload["error_code"] == "required_capability_failed"
    assert not any(event.type.startswith("message.") for event in events)
    assert "第一轮直接回答" not in _runtime_event_text(events)


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_direct_collaboration_tool_calls_emit_one_preparing_receipt() -> None:  # noqa: E501
    """A prose-triggered room leaves generic waiting exactly once per turn."""

    protocols: list[str] = []

    async def collaborate_with_agents(
        tasks: list[dict[str, str]],
        protocol: str = "parallel_consultation",
    ):
        del tasks
        protocols.append(protocol)
        yield ToolResponse(
            content=[TextBlock(text="hosted batch complete")],
            metadata={
                "collaboration": {
                    "batch_id": "batch-prose",
                    "revision": 1,
                    "status": "completed",
                    "phase": "closed",
                    "members": [
                        {
                            "member_id": "member-1",
                            "agent_id": "agent-a",
                            "status": "completed",
                        },
                        {
                            "member_id": "member-2",
                            "agent_id": "agent-b",
                            "status": "completed",
                        },
                    ],
                },
            },
            is_last=True,
        )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="collaborate_with_agents",
            input_schema={
                "type": "object",
                "properties": {
                    "tasks": {"type": "array"},
                    "protocol": {"type": "string"},
                },
                "required": ["tasks"],
            },
            metadata={"progressive_tool": "collaboration"},
        ),
        collaborate_with_agents,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(kind="message_start", message_id="prelude"),
                ModelStreamEvent(
                    kind="text_delta",
                    message_id="prelude",
                    delta="我先召集协作室。",
                ),
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-prose-batch",
                        name="collaborate_with_agents",
                        arguments={
                            "tasks": [
                                {
                                    "to_agent": "agent-a",
                                    "text": "甲",
                                    "label": "甲",
                                },
                                {
                                    "to_agent": "agent-b",
                                    "text": "乙",
                                    "label": "乙",
                                },
                            ],
                        },
                    ),
                ),
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-prose-batch-followup",
                        name="collaborate_with_agents",
                        arguments={
                            "tasks": [
                                {
                                    "to_agent": "agent-a",
                                    "text": "复核甲",
                                    "label": "甲",
                                },
                                {
                                    "to_agent": "agent-b",
                                    "text": "复核乙",
                                    "label": "乙",
                                },
                            ],
                        },
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="prelude",
                    response=ModelResponse(
                        message=ModelMessage.text("assistant", "我先召集协作室。"),
                    ),
                ),
            ],
            [_completed("沈行知已完成协作交付。", message_id="final")],
        ],
    )
    task_contract = build_chat_task_contract(
        objective="请用会议厅分析",
        client_turn_id="client-prose-collaboration",
        complexity="standard",
        work_method_id="standard",
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [
        event
        async for event in runtime.run_turn(
            _runtime_input().model_copy(
                update={
                    "messages": [
                        RuntimeMessage.text(
                            "user",
                            "请真正使用会议厅完成一次无人值守的两人圆桌。",
                        ),
                    ],
                    "metadata": {
                        "task_contract": task_contract.model_dump(mode="json"),
                    },
                },
            ),
        )
    ]

    preparing = [
        event
        for event in events
        if event.type == "runtime.warning"
        and event.payload.get("kind") == "collaboration_preparing"
    ]
    assert len(preparing) == 1
    assert protocols == ["roundtable"]
    assert preparing[0].item_id == "call-prose-batch"
    assert preparing[0].seq < next(
        event.seq
        for event in events
        if event.type == "tool.call.completed"
        and event.item_id == "call-prose-batch"
    )
    first_batch_tool_index = next(
        index
        for index, event in enumerate(events)
        if event.type == "tool.call.started"
        and event.item_id == "call-prose-batch"
    )
    assert not any(
        event.type.startswith("message.")
        for event in events[:first_batch_tool_index]
    )


@pytest.mark.asyncio
async def test_native_runtime_ignores_a_persisted_tool_budget() -> None:
    calls: list[str] = []

    async def lookup(query: str) -> str:
        calls.append(query)
        return f"found:{query}"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="lookup",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        ),
        lookup,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-1",
                        name="lookup",
                        arguments={"query": "a"},
                    ),
                ),
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-2",
                        name="lookup",
                        arguments={"query": "b"},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(id="msg", role="assistant"),
                    ),
                ),
            ],
            [_completed("已找到 a 和 b 的结果。")],
        ],
    )
    runtime_input = _runtime_input().model_copy(
        update={"metadata": {"runtime_budget": {"max_tool_calls": 1}}},
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert calls == ["a", "b"]
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_quarantines_ambiguous_write_before_repeat() -> None:  # noqa: E501
    attempts: list[str] = []

    async def shell(command: str) -> ToolResponse:
        attempts.append(command)
        return ToolResponse(
            content=[
                TextBlock(
                    text=(
                        "Tool guard policy engine failed closed before"
                        " execution."
                    ),
                ),
            ],
            metadata={"side_effect_outcome": "known_not_applied"},
            is_error=True,
        )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="execute_shell_command",
            input_schema={
                "type": "object",
                "properties": {"command": {"type": "string"}},
            },
        ),
        shell,
    )

    def failed_round(call_id: str, command: str) -> list[ModelStreamEvent]:
        return [
            ModelStreamEvent(
                kind="tool_call_completed",
                tool_call=ToolCall(
                    id=call_id,
                    name="execute_shell_command",
                    arguments={"command": command},
                ),
            ),
            ModelStreamEvent(
                kind="message_completed",
                response=ModelResponse(
                    message=ModelMessage(
                        id=f"msg-{call_id}",
                        role="assistant",
                    ),
                ),
            ),
        ]

    client = ScriptedModelClient(
        [
            failed_round("call-1", "rm -r ./tmp"),
            failed_round("call-2", "rm -r ./tmp"),
            [_completed("该工具未能执行，我已停止重试并保留现有结果。")],
        ],
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "metadata": {
                "runtime_budget": {
                    "max_tool_calls": 20,
                    "max_same_failure_count": 2,
                    "max_no_progress_rounds": 5,
                },
            },
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    # Executor/plugin metadata is not trusted to prove that a write did not
    # happen.  Once the invoked write fails ambiguously, the identical second
    # model call is represented to the model but never executed again.
    assert attempts == ["rm -r ./tmp"]
    assert events[-1].type == "turn.completed"
    quarantine_events = [
        event
        for event in events
        if event.payload.get("kind") == "tool_quarantined"
    ]
    assert len(quarantine_events) == 1
    assert [spec.name for spec in client.requests[-1].tools] == [
        "execute_shell_command",
    ]


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_identical_success_cannot_reset_progress_forever() -> None:  # noqa: E501
    attempts: list[str] = []

    async def lookup(query: str) -> str:
        attempts.append(query)
        return "same cached answer"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="lookup",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
            read_only=True,
        ),
        lookup,
    )

    def repeated_round(call_id: str) -> list[ModelStreamEvent]:
        return [
            ModelStreamEvent(
                kind="tool_call_completed",
                tool_call=ToolCall(
                    id=call_id,
                    name="lookup",
                    arguments={"query": "same"},
                ),
            ),
            ModelStreamEvent(
                kind="message_completed",
                response=ModelResponse(
                    message=ModelMessage(
                        id=f"msg-{call_id}",
                        role="assistant",
                    ),
                ),
            ),
        ]

    client = ScriptedModelClient(
        [
            repeated_round("call-1"),
            repeated_round("call-2"),
            repeated_round("call-3"),
            [_completed("现有证据没有变化，我停止重复查询并直接给出结论。")],
        ],
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "metadata": {
                "runtime_budget": {
                    "max_tool_calls": 20,
                    "max_no_progress_rounds": 2,
                },
            },
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert attempts == ["same", "same", "same"]
    assert events[-1].type == "turn.completed"
    convergence_events = [
        event
        for event in events
        if event.payload.get("kind") == "convergence_synthesis"
    ]
    assert len(convergence_events) == 1
    assert convergence_events[0].payload["decision"] == "synthesize"
    review_requests = [
        request
        for request in client.requests
        if request.metadata.get("convergence_review") is True
    ]
    assert len(review_requests) == 1
    assert review_requests[0].tools == []
    assert client.requests[-1].tools == []
    assert any(
        message.metadata.get("instruction_origin")
        == "runtime.terminal_synthesis"
        for message in client.requests[-1].messages
    )


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_discovery_success_with_new_arguments_cannot_loop() -> None:  # noqa: E501
    attempts: list[str] = []

    async def list_skill_resources(skill_name: str) -> list[str]:
        attempts.append(skill_name)
        return []

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="list_skill_resources",
            metadata={"progress_policy": "first_tool_success"},
        ),
        list_skill_resources,
    )

    def repeated_round(index: int) -> list[ModelStreamEvent]:
        return [
            ModelStreamEvent(
                kind="tool_call_completed",
                tool_call=ToolCall(
                    id=f"call-{index}",
                    name="list_skill_resources",
                    arguments={"skill_name": f"missing-{index}"},
                ),
            ),
            ModelStreamEvent(
                kind="message_completed",
                response=ModelResponse(
                    message=ModelMessage(id=f"msg-{index}", role="assistant"),
                ),
            ),
        ]

    client = ScriptedModelClient(
        [
            repeated_round(1),
            repeated_round(2),
            repeated_round(3),
            [_completed("没有发现新的资源，我停止重复枚举并直接说明结论。")],
        ],
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "metadata": {
                "runtime_budget": {
                    "max_tool_calls": 20,
                    "max_no_progress_rounds": 2,
                },
            },
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert attempts == ["missing-1", "missing-2", "missing-3"]
    assert client.requests[-1].tools == []
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_does_not_review_while_research_keeps_progressing() -> None:  # noqa: E501
    attempts: list[str] = []

    async def web_search(query: str) -> str:
        attempts.append(query)
        index = len(attempts)
        return f"new evidence {index}: https://example.com/source-{index}"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="web_search",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
            read_only=True,
        ),
        web_search,
    )

    def search_round(index: int) -> list[ModelStreamEvent]:
        return [
            ModelStreamEvent(
                kind="tool_call_completed",
                tool_call=ToolCall(
                    id=f"search-{index}",
                    name="web_search",
                    arguments={"query": f"changed query {index}"},
                ),
            ),
            ModelStreamEvent(
                kind="message_completed",
                response=ModelResponse(
                    message=ModelMessage(
                        id=f"msg-search-{index}",
                        role="assistant",
                    ),
                ),
            ),
        ]

    client = ScriptedModelClient(
        [
            *[search_round(index) for index in range(1, 10)],
            [_completed("检索已经饱和；以下是基于九份来源形成的最佳结论。")],
        ],
        completions=[
            ModelResponse(
                message=ModelMessage.text(
                    "assistant",
                    '{"action":"continue","reason":"还有一个明确的新来源"}',
                ),
            ),
        ],
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "metadata": {
                "runtime_budget": {
                    "max_model_rounds": 40,
                    "max_tool_calls": 40,
                    "max_no_progress_rounds": 3,
                },
            },
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert len(attempts) == 9
    review_requests = [
        request
        for request in client.requests
        if request.metadata.get("convergence_review") is True
    ]
    assert review_requests == []
    convergence_events = [
        event
        for event in events
        if event.payload.get("kind") == "convergence_synthesis"
    ]
    assert convergence_events == []
    assert events[-1].type == "turn.completed"
    assert events[-1].payload["outcome_status"] == "delivered"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_allows_a_changed_strategy_after_same_tool_error() -> None:  # noqa: E501
    attempts: list[str] = []

    async def shell(command: str) -> str:
        attempts.append(command)
        raise RuntimeError(
            "Tool guard policy engine failed closed before execution.",
        )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="execute_shell_command",
            input_schema={
                "type": "object",
                "properties": {"command": {"type": "string"}},
            },
        ),
        shell,
    )

    def failed_round(call_id: str, command: str) -> list[ModelStreamEvent]:
        return [
            ModelStreamEvent(
                kind="tool_call_completed",
                tool_call=ToolCall(
                    id=call_id,
                    name="execute_shell_command",
                    arguments={"command": command},
                ),
            ),
            ModelStreamEvent(
                kind="message_completed",
                response=ModelResponse(
                    message=ModelMessage(
                        id=f"msg-{call_id}",
                        role="assistant",
                    ),
                ),
            ),
        ]

    client = ScriptedModelClient(
        [
            failed_round("call-1", "rm -r ./tmp"),
            failed_round("call-2", "find ./tmp -delete"),
            [_completed("已改用非破坏性方案结束。")],
        ],
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "metadata": {
                "runtime_budget": {
                    "max_tool_calls": 20,
                    "max_same_failure_count": 2,
                    "max_no_progress_rounds": 5,
                },
            },
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert attempts == ["rm -r ./tmp", "find ./tmp -delete"]
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_quarantines_repeated_read_only_failure_and_finishes() -> None:  # noqa: E501
    attempts: list[str] = []

    async def lookup(query: str) -> str:
        attempts.append(query)
        raise TypeError(
            "'<' not supported between instances of 'int' and 'str'",
        )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="wiki_search",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
            read_only=True,
        ),
        lookup,
    )

    def failed_round(call_id: str) -> list[ModelStreamEvent]:
        return [
            ModelStreamEvent(
                kind="tool_call_completed",
                tool_call=ToolCall(
                    id=call_id,
                    name="wiki_search",
                    arguments={"query": "works"},
                ),
            ),
            ModelStreamEvent(
                kind="message_completed",
                response=ModelResponse(
                    message=ModelMessage(
                        id=f"msg-{call_id}",
                        role="assistant",
                    ),
                ),
            ),
        ]

    client = ScriptedModelClient(
        [
            failed_round("call-1"),
            failed_round("call-2"),
            [_completed("已使用已有信息完成任务。")],
        ],
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "metadata": {
                "runtime_budget": {
                    "max_tool_calls": 20,
                    "max_same_failure_count": 2,
                    "max_no_progress_rounds": 5,
                },
            },
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert attempts == ["works", "works"]
    assert events[-1].type == "turn.completed"
    quarantine_events = [
        event
        for event in events
        if event.payload.get("kind") == "tool_quarantined"
    ]
    assert len(quarantine_events) == 1
    assert quarantine_events[0].payload["error"]["tool_name"] == "wiki_search"
    assert [spec.name for spec in client.requests[-1].tools] == ["wiki_search"]


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_quarantines_declared_deterministic_failure_once() -> None:  # noqa: E501
    attempts: list[str] = []

    async def read_skill(skill_name: str, file_path: str) -> ToolResponse:
        attempts.append(f"{skill_name}:{file_path}")
        return ToolResponse(
            content=[TextBlock(text="Error: resource was not found")],
            is_error=True,
            metadata={
                "error_code": "skill_resource_not_found",
                "deterministic_failure": True,
            },
        )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="read_skill",
            input_schema={
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string"},
                    "file_path": {"type": "string"},
                },
                "required": ["skill_name", "file_path"],
            },
            read_only=True,
        ),
        read_skill,
    )

    def failed_round(call_id: str) -> list[ModelStreamEvent]:
        return [
            ModelStreamEvent(
                kind="tool_call_completed",
                tool_call=ToolCall(
                    id=call_id,
                    name="read_skill",
                    arguments={"skill_name": "pptx", "file_path": "pptx"},
                ),
            ),
            ModelStreamEvent(
                kind="message_completed",
                response=ModelResponse(
                    message=ModelMessage(
                        id=f"msg-{call_id}",
                        role="assistant",
                    ),
                ),
            ),
        ]

    client = ScriptedModelClient(
        [
            failed_round("call-1"),
            failed_round("call-2"),
            [_completed("已停止无效重试并如实说明。")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert attempts == ["pptx:pptx"]
    quarantine_events = [
        event
        for event in events
        if event.payload.get("kind") == "tool_quarantined"
    ]
    assert len(quarantine_events) == 1
    assert quarantine_events[0].payload["error"]["attempts"] == 1
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_allows_changed_arguments_after_quarantine() -> None:  # noqa: E501
    attempts: list[str] = []

    async def lookup(query: str) -> str:
        attempts.append(query)
        if query == "broken":
            raise RuntimeError("upstream rejected this query")
        return "recovered"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="wiki_search",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
            read_only=True,
        ),
        lookup,
    )

    def call_round(call_id: str, query: str) -> list[ModelStreamEvent]:
        return [
            ModelStreamEvent(
                kind="tool_call_completed",
                tool_call=ToolCall(
                    id=call_id,
                    name="wiki_search",
                    arguments={"query": query},
                ),
            ),
            ModelStreamEvent(
                kind="message_completed",
                response=ModelResponse(
                    message=ModelMessage(
                        id=f"msg-{call_id}",
                        role="assistant",
                    ),
                ),
            ),
        ]

    client = ScriptedModelClient(
        [
            call_round("call-1", "broken"),
            call_round("call-2", "broken"),
            call_round("call-3", "working"),
            [_completed("换了查询策略后已完成。")],
        ],
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "metadata": {
                "runtime_budget": {
                    "max_tool_calls": 20,
                    "max_same_failure_count": 2,
                    "max_no_progress_rounds": 5,
                },
            },
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert attempts == ["broken", "broken", "working"]
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_distinguishes_different_process_exit_codes() -> None:  # noqa: E501
    async def shell(command: str) -> ToolResponse:
        exit_code = 1 if command == "first-probe" else 13
        return ToolResponse(
            content=[
                TextBlock(text=f"Command failed with exit code {exit_code}."),
            ],
            metadata={"exit_code": exit_code},
            is_error=True,
        )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="execute_shell_command",
            input_schema={
                "type": "object",
                "properties": {"command": {"type": "string"}},
            },
        ),
        shell,
    )

    def failed_round(call_id: str, command: str) -> list[ModelStreamEvent]:
        return [
            ModelStreamEvent(
                kind="tool_call_completed",
                tool_call=ToolCall(
                    id=call_id,
                    name="execute_shell_command",
                    arguments={"command": command},
                ),
            ),
            ModelStreamEvent(
                kind="message_completed",
                response=ModelResponse(
                    message=ModelMessage(
                        id=f"msg-{call_id}",
                        role="assistant",
                    ),
                ),
            ),
        ]

    client = ScriptedModelClient(
        [
            failed_round("call-exit-1", "first-probe"),
            failed_round("call-exit-13", "second-probe"),
            [_completed("验证未通过，但没有误判为同一次失败。")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_never_forces_artifact_tools_from_intake_metadata() -> None:  # noqa: E501
    """Legacy artifact labels must not turn an ordinary answer into a tool
    loop.
    """

    client = ScriptedModelClient(
        [[_completed("这是一条普通回答，不需要创建文件。")]],
    )
    runtime_input = _runtime_input()
    runtime_input.metadata["task_contract"] = {
        "deliverable_intent": {"mode": "artifact"},
    }
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert events[-1].type == "turn.completed"
    assert len(client.requests) == 1
    assert not any(
        "finalize_deliverable" in message.text_content()
        for message in client.requests[0].messages
    )


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_keeps_visible_final_answer_at_token_budget() -> None:  # noqa: E501
    response = ModelResponse(
        message=ModelMessage.text("assistant", "done"),
        usage=ModelUsage(input_tokens=80, output_tokens=30, total_tokens=110),
    )
    client = ScriptedModelClient(
        [[ModelStreamEvent(kind="message_completed", response=response)]],
    )
    runtime_input = _runtime_input().model_copy(
        update={"metadata": {"runtime_budget": {"max_tokens": 100}}},
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert events[-1].type == "turn.completed"
    assert any(event.summary == "done" for event in events)


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_accepts_visible_response_after_tool_work() -> None:  # noqa: E501
    async def read_skill(name: str) -> str:
        return f"loaded:{name}"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="read_skill",
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        ),
        read_skill,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="msg-1",
                    tool_call=ToolCall(
                        id="call-read-skill",
                        name="read_skill",
                        arguments={"name": "docx-zh"},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-1",
                    response=ModelResponse(
                        message=ModelMessage(id="msg-1", role="assistant"),
                    ),
                ),
            ],
            [_completed("规范都清楚了，现在开始写 DOCX 生成脚本。", message_id="msg-2")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert len(client.requests) == 2
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
async def test_native_runtime_hides_private_subagent_skill_read() -> None:
    async def read_skill(name: str) -> str:
        return f"loaded:{name}"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="read_skill",
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        ),
        read_skill,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="msg-private-skill",
                    tool_call=ToolCall(
                        id="call-private-skill",
                        name="read_skill",
                        arguments={"name": "puer"},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-private-skill",
                    response=ModelResponse(
                        message=ModelMessage(
                            id="msg-private-skill",
                            role="assistant",
                        ),
                    ),
                ),
            ],
            [_completed("估值检查完成。", message_id="msg-private-final")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )
    runtime_input = _runtime_input()
    runtime_input.request.agent_id = "Lantai_Puer_Agent"
    runtime_input.request.metadata["legacy_request_context"] = {
        "presentation_context": "subagent_internal",
        "skill_exposure": "subagent_internal",
    }

    events = [event async for event in runtime.run_turn(runtime_input)]
    tool_events = [
        event for event in events if event.type.startswith("tool.call.")
    ]

    assert tool_events
    assert all(event.presentation is not None for event in tool_events)
    assert all(
        event.presentation.visibility == "hidden" for event in tool_events
    )
    assert all(event.presentation.surface == "debug" for event in tool_events)
    assert all(
        event.payload["metadata"]["presentation_context"]
        == "subagent_internal"
        for event in tool_events
    )


@pytest.mark.asyncio
async def test_native_runtime_delivers_model_finalized_markdown(
    tmp_path: Path,
) -> None:
    delivered_paths: list[str] = []
    report_path = tmp_path / "report.md"

    async def write_file(file_path: str, content: str) -> ToolResponse:
        destination = tmp_path / file_path
        destination.write_text(content, encoding="utf-8")
        return ToolResponse(
            content=[
                TextBlock(
                    text=f"Wrote {len(content)} bytes to {file_path}.",
                ),
            ],
        )

    async def finalize_deliverable(primary_path: str) -> ToolResponse:
        return ToolResponse(
            content=[TextBlock(text=f"Finalized {primary_path}.")],
        )

    async def send_file_to_user(file_path: str) -> ToolResponse:
        delivered_paths.append(file_path)
        return ToolResponse(
            content=[
                FileBlock(
                    filename="report.md",
                    source={"type": "url", "url": "file:///tmp/report.md"},
                ),
            ],
        )

    registry = LantaiToolRegistry()
    for name, executor in (
        ("write_file", write_file),
        ("finalize_deliverable", finalize_deliverable),
        ("send_file_to_user", send_file_to_user),
    ):
        registry.register_tool(
            ToolSpec(
                name=name,
                input_schema={
                    "type": "object",
                    "properties": {
                        (
                            "primary_path"
                            if name == "finalize_deliverable"
                            else "file_path"
                        ): {"type": "string"},
                        **(
                            {"content": {"type": "string"}}
                            if name == "write_file"
                            else {}
                        ),
                    },
                    "required": (
                        ["file_path", "content"]
                        if name == "write_file"
                        else ["primary_path"]
                        if name == "finalize_deliverable"
                        else ["file_path"]
                    ),
                },
            ),
            executor,
        )

    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="msg-write",
                    tool_call=ToolCall(
                        id="call-write",
                        name="write_file",
                        arguments={
                            "file_path": "report.md",
                            "content": "# 报告",
                        },
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-write",
                    response=ModelResponse(
                        message=ModelMessage(id="msg-write", role="assistant"),
                    ),
                ),
            ],
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="msg-finalize",
                    tool_call=ToolCall(
                        id="call-finalize",
                        name="finalize_deliverable",
                        arguments={"primary_path": str(report_path)},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-finalize",
                    response=ModelResponse(
                        message=ModelMessage(
                            id="msg-finalize",
                            role="assistant",
                        ),
                    ),
                ),
            ],
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="msg-send",
                    tool_call=ToolCall(
                        id="call-send",
                        name="send_file_to_user",
                        arguments={"file_path": str(report_path)},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-send",
                    response=ModelResponse(
                        message=ModelMessage(id="msg-send", role="assistant"),
                    ),
                ),
            ],
            [_completed("文件已附在对话中。", message_id="msg-final")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )
    runtime_input = _runtime_input()
    runtime_input.request.workspace_dir = str(tmp_path)
    runtime_input.messages = [
        RuntimeMessage.text("user", "把内容整合下，写进一个md里"),
    ]

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert delivered_paths == [str(report_path)]
    assert client.requests[2].tool_choice == "required"
    assert [tool.name for tool in client.requests[2].tools] == [
        "send_file_to_user",
    ]
    assert any(
        event.type == "tool.call.completed"
        and event.payload.get("name") == "send_file_to_user"
        and event.payload.get("output", [{}])[0].get("type") == "file"
        for event in events
    )
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
async def test_native_runtime_keeps_zip_and_attaches_each_deliverable_member(
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "campaign_bundle"
    bundle_dir.mkdir()
    member_paths = [
        bundle_dir / "brief.pdf",
        bundle_dir / "cover.png",
        bundle_dir / "voiceover.mp3",
        bundle_dir / "metrics.csv",
        bundle_dir / "landing-page.html",
    ]
    for member_path in member_paths:
        member_path.write_bytes(b"deliverable")
    readme_path = bundle_dir / "README.md"
    readme_path.write_text("# dataset", encoding="utf-8")
    archive_path = tmp_path / "campaign_bundle.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for path in [*member_paths, readme_path]:
            archive.write(path, arcname=path.name)

    delivered_paths: list[str] = []

    async def send_file_to_user(file_path: str) -> ToolResponse:
        delivered_paths.append(file_path)
        path = Path(file_path)
        return ToolResponse(
            content=[
                FileBlock(
                    filename=path.name,
                    source={"type": "url", "url": path.as_uri()},
                ),
            ],
        )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="send_file_to_user",
            input_schema={
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        ),
        send_file_to_user,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="msg-send-archive",
                    tool_call=ToolCall(
                        id="call-send-archive",
                        name="send_file_to_user",
                        arguments={"file_path": str(archive_path)},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-send-archive",
                    response=ModelResponse(
                        message=ModelMessage(
                            id="msg-send-archive",
                            role="assistant",
                        ),
                    ),
                ),
            ],
            [_completed("压缩包和每个文件都已附上。", message_id="msg-final")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )
    runtime_input = _runtime_input()
    runtime_input.request.workspace_dir = str(tmp_path)

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert delivered_paths == [
        str(archive_path),
        *(str(path.resolve()) for path in member_paths),
    ]
    assert str(readme_path.resolve()) not in delivered_paths
    delivered_names = [
        event.payload.get("output", [{}])[0].get("filename")
        for event in events
        if event.type == "tool.call.completed"
        and event.payload.get("tool_name") == "send_file_to_user"
    ]
    assert delivered_names == [
        archive_path.name,
        *(path.name for path in member_paths),
    ]
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
async def test_required_file_delivery_is_bounded_and_hides_provisional_text(
    tmp_path: Path,
) -> None:
    """A finalized file cannot leak prose or end as success when attach
    fails.
    """

    report_path = tmp_path / "report.md"
    report_path.write_text("# report", encoding="utf-8")

    async def finalize_deliverable(primary_path: str) -> ToolResponse:
        return ToolResponse(
            content=[TextBlock(text=f"Finalized {primary_path}.")],
        )

    async def send_file_to_user(file_path: str) -> ToolResponse:
        raise AssertionError(
            f"provider should have invoked this tool: {file_path}",
        )

    registry = LantaiToolRegistry()
    for name, argument_name, executor in (
        ("finalize_deliverable", "primary_path", finalize_deliverable),
        ("send_file_to_user", "file_path", send_file_to_user),
    ):
        registry.register_tool(
            ToolSpec(
                name=name,
                input_schema={
                    "type": "object",
                    "properties": {argument_name: {"type": "string"}},
                    "required": [argument_name],
                },
            ),
            executor,
        )

    def leaked_answer(message_id: str, text: str) -> list[ModelStreamEvent]:
        return [
            ModelStreamEvent(kind="message_start", message_id=message_id),
            ModelStreamEvent(
                kind="text_delta",
                message_id=message_id,
                delta=text,
            ),
            _completed(text, message_id=message_id),
        ]

    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="msg-finalize",
                    tool_call=ToolCall(
                        id="call-finalize",
                        name="finalize_deliverable",
                        arguments={"primary_path": str(report_path)},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-finalize",
                    response=ModelResponse(
                        message=ModelMessage(
                            id="msg-finalize",
                            role="assistant",
                        ),
                    ),
                ),
            ],
            leaked_answer("msg-leak-1", "文件已经发给你了。"),
            leaked_answer("msg-leak-2", "附件已经交付成功。"),
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )
    runtime_input = _runtime_input()
    runtime_input.request.workspace_dir = str(tmp_path)

    events = []
    with pytest.raises(RuntimeError, match="required capability"):
        async for event in runtime.run_turn(runtime_input):
            events.append(event)

    assert len(client.requests) == 3
    assert all(
        request.tool_choice == "required" for request in client.requests[1:]
    )
    assert events[-1].type == "turn.failed"
    assert events[-1].payload["error_code"] == "required_capability_failed"
    assert events[-1].payload["outcome_status"] == "blocked"
    assert not any(event.type.startswith("message.") for event in events)
    assert "文件已经发给你" not in _runtime_event_text(events)
    assert "附件已经交付成功" not in _runtime_event_text(events)


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_recovers_generated_file_when_model_skips_finalize(  # noqa: E501
    tmp_path: Path,
) -> None:
    """A classified artifact turn must not strand a file in the workspace."""
    delivered_paths: list[str] = []
    report_path = tmp_path / "漏交付报告.md"

    async def write_file(file_path: str, content: str) -> ToolResponse:
        destination = tmp_path / file_path
        destination.write_text(content, encoding="utf-8")
        return ToolResponse(content=[TextBlock(text="written")])

    async def send_file_to_user(file_path: str) -> ToolResponse:
        delivered_paths.append(file_path)
        return ToolResponse(
            content=[
                FileBlock(
                    filename=Path(file_path).name,
                    source={"type": "url", "url": Path(file_path).as_uri()},
                ),
            ],
        )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="write_file",
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["file_path", "content"],
            },
            output_semantics=ToolOutputSemantics(
                artifact_role="candidate",
                argument_path_fields=("file_path",),
            ),
        ),
        write_file,
    )
    registry.register_tool(
        ToolSpec(
            name="send_file_to_user",
            input_schema={
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        ),
        send_file_to_user,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="msg-write-without-finalize",
                    tool_call=ToolCall(
                        id="call-write-without-finalize",
                        name="write_file",
                        arguments={
                            "file_path": report_path.name,
                            "content": "# 可交付报告",
                        },
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-write-without-finalize",
                    response=ModelResponse(
                        message=ModelMessage(
                            id="msg-write-without-finalize",
                            role="assistant",
                        ),
                    ),
                ),
            ],
            [_completed("报告已经生成。", message_id="msg-file-answer")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )
    runtime_input = _runtime_input()
    runtime_input.request.workspace_dir = str(tmp_path)
    runtime_input.metadata = {
        "turn_intelligence": {
            "status": "classified",
            "interaction_mode": "action",
            "durable_artifact_intent": "create",
            "confidence": 0.97,
        },
    }

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert report_path.read_text(encoding="utf-8") == "# 可交付报告"
    assert delivered_paths == [str(report_path.resolve())]
    assert any(
        event.type == "tool.call.completed"
        and event.payload.get("tool_name") == "send_file_to_user"
        for event in events
    )
    assert events[-1].type == "turn.completed"
    assert events[-1].payload["durable_artifact_required"] is True


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_forces_primary_finalization_before_recovered_delivery(  # noqa: E501
    tmp_path: Path,
) -> None:
    """A durable bundle must not be replaced by a newly generated support
    file.
    """

    bundle_path = tmp_path / "coffee-revenue-dataset.zip"
    support_path = tmp_path / "coffee-revenue-dataset" / "CLAUDE.md"
    delivered_paths: list[str] = []

    async def build_bundle(output_path: str) -> ToolResponse:
        support_path.parent.mkdir(parents=True, exist_ok=True)
        support_path.write_text("# Support notes", encoding="utf-8")
        Path(output_path).write_bytes(b"zip-bytes")
        return ToolResponse(content=[TextBlock(text="bundle created")])

    async def finalize_deliverable(primary_path: str) -> ToolResponse:
        return ToolResponse(
            content=[TextBlock(text=f"Finalized {primary_path}")],
        )

    async def send_file_to_user(file_path: str) -> ToolResponse:
        delivered_paths.append(file_path)
        return ToolResponse(
            content=[
                FileBlock(
                    filename=Path(file_path).name,
                    source={"type": "url", "url": Path(file_path).as_uri()},
                ),
            ],
        )

    registry = LantaiToolRegistry()
    for name, path_field, artifact_role, executor in (
        ("build_bundle", "output_path", "candidate", build_bundle),
        (
            "finalize_deliverable",
            "primary_path",
            "primary",
            finalize_deliverable,
        ),
        ("send_file_to_user", "file_path", "delivery", send_file_to_user),
    ):
        registry.register_tool(
            ToolSpec(
                name=name,
                input_schema={
                    "type": "object",
                    "properties": {path_field: {"type": "string"}},
                    "required": [path_field],
                },
                output_semantics=ToolOutputSemantics(
                    artifact_role=artifact_role,
                    argument_path_fields=(path_field,),
                ),
            ),
            executor,
        )

    def tool_turn(
        message_id: str,
        call_id: str,
        name: str,
        arguments: dict[str, str],
    ) -> list[ModelStreamEvent]:
        return [
            ModelStreamEvent(
                kind="tool_call_completed",
                message_id=message_id,
                tool_call=ToolCall(
                    id=call_id,
                    name=name,
                    arguments=arguments,
                ),
            ),
            ModelStreamEvent(
                kind="message_completed",
                message_id=message_id,
                response=ModelResponse(
                    message=ModelMessage(id=message_id, role="assistant"),
                ),
            ),
        ]

    client = ScriptedModelClient(
        [
            tool_turn(
                "msg-build",
                "call-build",
                "build_bundle",
                {"output_path": str(bundle_path)},
            ),
            [
                _completed(
                    "数据集和 coffee-revenue-dataset.zip 已经完成。",
                    message_id="msg-premature-completion",
                ),
            ],
            tool_turn(
                "msg-finalize",
                "call-finalize",
                "finalize_deliverable",
                {"primary_path": str(bundle_path)},
            ),
            tool_turn(
                "msg-send",
                "call-send",
                "send_file_to_user",
                {"file_path": str(bundle_path)},
            ),
            [_completed("ZIP 已附在对话中。", message_id="msg-final")],
        ],
    )
    effect_journal = RuntimeEffectJournal(tmp_path)
    artifact_journal = RuntimeArtifactEffectJournal(tmp_path)
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        effect_journal=effect_journal,
        artifact_effect_journal=artifact_journal,
    )
    runtime_input = _runtime_input()
    runtime_input.request.workspace_dir = str(tmp_path)
    runtime_input.metadata = {
        "attempt_id": "attempt-recovered-finalization",
        "turn_intelligence": {
            "status": "classified",
            "interaction_mode": "action",
            "durable_artifact_intent": "create",
            "confidence": 0.99,
        },
    }

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert bundle_path.is_file()
    assert support_path.is_file()
    assert client.requests[2].tool_choice == "required"
    assert [tool.name for tool in client.requests[2].tools] == [
        "finalize_deliverable",
    ]
    assert str(bundle_path) in client.requests[2].messages[-1].text_content()
    assert (
        str(support_path) not in client.requests[2].messages[-1].text_content()
    )
    assert delivered_paths == [str(bundle_path)]
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
async def test_terminal_synthesis_does_not_reopen_artifact_finalization(
    tmp_path: Path,
) -> None:
    """A convergence stop must not be undone by stale artifact recovery."""

    report_path = tmp_path / "未验收报告.docx"
    finalization_attempts: list[str] = []

    async def build_report(output_path: str) -> ToolResponse:
        Path(output_path).write_bytes(b"candidate")
        return ToolResponse(content=[TextBlock(text="candidate created")])

    async def finalize_deliverable(primary_path: str) -> ToolResponse:
        finalization_attempts.append(primary_path)
        raise ValueError("visual verification is missing")

    registry = LantaiToolRegistry()
    for name, path_field, artifact_role, executor in (
        ("build_report", "output_path", "candidate", build_report),
        (
            "finalize_deliverable",
            "primary_path",
            "primary",
            finalize_deliverable,
        ),
    ):
        registry.register_tool(
            ToolSpec(
                name=name,
                input_schema={
                    "type": "object",
                    "properties": {path_field: {"type": "string"}},
                    "required": [path_field],
                },
                output_semantics=ToolOutputSemantics(
                    artifact_role=artifact_role,
                    argument_path_fields=(path_field,),
                ),
            ),
            executor,
        )

    def tool_turn(
        *,
        message_id: str,
        call_id: str,
        name: str,
        arguments: dict[str, str],
    ) -> list[ModelStreamEvent]:
        return [
            ModelStreamEvent(
                kind="tool_call_completed",
                message_id=message_id,
                tool_call=ToolCall(
                    id=call_id,
                    name=name,
                    arguments=arguments,
                ),
            ),
            ModelStreamEvent(
                kind="message_completed",
                message_id=message_id,
                response=ModelResponse(
                    message=ModelMessage(id=message_id, role="assistant"),
                ),
            ),
        ]

    client = ScriptedModelClient(
        [
            tool_turn(
                message_id="msg-build",
                call_id="call-build",
                name="build_report",
                arguments={"output_path": str(report_path)},
            ),
            tool_turn(
                message_id="msg-finalize",
                call_id="call-finalize",
                name="finalize_deliverable",
                arguments={"primary_path": str(report_path)},
            ),
            [
                _completed(
                    "候选文件已保留，但本轮没有完成验收和交付。",
                    message_id="msg-terminal-synthesis",
                ),
            ],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        effect_journal=RuntimeEffectJournal(tmp_path),
        artifact_effect_journal=RuntimeArtifactEffectJournal(tmp_path),
    )
    runtime_input = _runtime_input()
    runtime_input.request.workspace_dir = str(tmp_path)
    runtime_input.metadata = {
        "attempt_id": "attempt-terminal-synthesis-artifact",
        "convergence_mode": "off",
        "runtime_budget": {
            "max_no_progress_rounds": 1,
        },
        "turn_intelligence": {
            "status": "classified",
            "interaction_mode": "action",
            "durable_artifact_intent": "create",
            "confidence": 0.99,
        },
    }

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert report_path.is_file()
    assert len(client.requests) == 3
    assert "finalize_deliverable" in {
        tool.name for tool in client.requests[1].tools
    }
    assert client.requests[2].tools == []
    assert client.requests[2].tool_choice == "auto"
    assert finalization_attempts == [str(report_path)]
    assert events[-1].type == "turn.completed"
    assert events[-1].payload["outcome_status"] == "incomplete"
    assert events[-1].payload["reason"] == "no_progress"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_attaches_model_declared_generated_file_without_classifier(  # noqa: E501
    tmp_path: Path,
) -> None:
    """Actual file evidence must survive the retired intent preflight."""

    delivered_paths: list[str] = []
    page_path = tmp_path / "逃离办公室.html"

    async def write_file(file_path: str, content: str) -> ToolResponse:
        destination = tmp_path / file_path
        destination.write_text(content, encoding="utf-8")
        return ToolResponse(content=[TextBlock(text="written")])

    async def send_file_to_user(file_path: str) -> ToolResponse:
        delivered_paths.append(file_path)
        return ToolResponse(
            content=[
                FileBlock(
                    filename=Path(file_path).name,
                    source={"type": "url", "url": Path(file_path).as_uri()},
                ),
            ],
        )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="write_file",
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["file_path", "content"],
            },
            output_semantics=ToolOutputSemantics(
                artifact_role="candidate",
                argument_path_fields=("file_path",),
            ),
        ),
        write_file,
    )
    registry.register_tool(
        ToolSpec(
            name="send_file_to_user",
            input_schema={
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        ),
        send_file_to_user,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="msg-write-page",
                    tool_call=ToolCall(
                        id="call-write-page",
                        name="write_file",
                        arguments={
                            "file_path": page_path.name,
                            "content": "<!doctype html><title>逃离办公室</title>",
                        },
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-write-page",
                    response=ModelResponse(
                        message=ModelMessage(
                            id="msg-write-page",
                            role="assistant",
                        ),
                    ),
                ),
            ],
            [
                _completed(
                    f"已完成，文件在：{page_path}",
                    message_id="msg-page-path-only",
                ),
            ],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )
    runtime_input = _runtime_input()
    runtime_input.request.workspace_dir = str(tmp_path)
    runtime_input.metadata = {
        "turn_intelligence": {
            "status": "unavailable",
            "interaction_mode": "unknown",
            "durable_artifact_intent": "unknown",
            "confidence": 0.0,
        },
    }

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert delivered_paths == [str(page_path.resolve())]
    assert any(
        event.type == "tool.call.completed"
        and event.payload.get("tool_name") == "send_file_to_user"
        and event.payload.get("output", [{}])[0].get("type") == "file"
        for event in events
    )
    assert events[-1].type == "turn.completed"


@pytest.mark.parametrize(
    "premature_text",
    [
        "python 命令不存在。如果你让我继续，下一轮会改用 python3。",
        "好。先加载 PPT工具，然后我整理内容大纲，确认后做出成稿。",
    ],
)
@pytest.mark.asyncio
async def test_native_runtime_retries_recoverable_durable_artifact_false_final(
    tmp_path: Path,
    premature_text: str,
) -> None:
    """A recoverable local command mismatch must not become the final
    answer.
    """
    delivered_paths: list[str] = []
    presentation_path = tmp_path / "活动方案.pptx"

    async def write_file(file_path: str, content: str) -> ToolResponse:
        destination = tmp_path / file_path
        destination.write_text(content, encoding="utf-8")
        return ToolResponse(content=[TextBlock(text="written")])

    async def send_file_to_user(file_path: str) -> ToolResponse:
        delivered_paths.append(file_path)
        return ToolResponse(
            content=[
                FileBlock(
                    filename=Path(file_path).name,
                    source={"type": "url", "url": Path(file_path).as_uri()},
                ),
            ],
        )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="write_file",
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["file_path", "content"],
            },
        ),
        write_file,
    )
    registry.register_tool(
        ToolSpec(
            name="send_file_to_user",
            input_schema={
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        ),
        send_file_to_user,
    )
    client = ScriptedModelClient(
        [
            [
                _completed(
                    premature_text,
                    message_id="msg-recoverable-false-final",
                ),
            ],
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="msg-write-recovered-presentation",
                    tool_call=ToolCall(
                        id="call-write-recovered-presentation",
                        name="write_file",
                        arguments={
                            "file_path": presentation_path.name,
                            "content": "fake pptx",
                        },
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-write-recovered-presentation",
                    response=ModelResponse(
                        message=ModelMessage(
                            id="msg-write-recovered-presentation",
                            role="assistant",
                        ),
                    ),
                ),
            ],
            [_completed("PPT 已生成。", message_id="msg-presentation-answer")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )
    runtime_input = _runtime_input()
    runtime_input.request.workspace_dir = str(tmp_path)
    runtime_input.metadata = {
        "turn_intelligence": {
            "status": "classified",
            "interaction_mode": "action",
            "durable_artifact_intent": "create",
            "confidence": 0.97,
        },
    }

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert len(client.requests) == 3
    assert presentation_path.read_text(encoding="utf-8") == "fake pptx"
    assert delivered_paths == [str(presentation_path.resolve())]
    assert any(
        event.type == "runtime.warning"
        and event.payload.get("kind") == "durable_artifact_completion_recovery"
        for event in events
    )
    assert premature_text not in _runtime_event_text(events)
    assert events[-1].type == "turn.completed"
    assert events[-1].payload["outcome_status"] == "delivered"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_redacts_configured_canary_from_visible_reasoning() -> None:  # noqa: E501
    """Visible reasoning stays useful without exposing exact prompt
    fingerprints.
    """
    private_canary = (
        "PRIVATE_SYSTEM_CANARY persona=沈行知 ququ_probability=50_percent"
    )
    response = ModelResponse(
        message=ModelMessage(
            id="msg-private-reasoning",
            role="assistant",
            content=[
                ModelContentBlock(type="thinking", text=private_canary),
                ModelContentBlock(type="text", text="这是完全相同的最终回答。"),
            ],
        ),
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="thinking_delta",
                    message_id="msg-private-reasoning",
                    delta=private_canary,
                ),
                ModelStreamEvent(
                    kind="text_delta",
                    message_id="msg-private-reasoning",
                    delta="这是完全相同的最终回答。",
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-private-reasoning",
                    response=response,
                ),
            ],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )

    runtime_input = _runtime_input()
    runtime_input.metadata = {"prompt_leak_canaries": [private_canary]}
    events = [event async for event in runtime.run_turn(runtime_input)]
    public_payload = _runtime_event_text(events)

    assert private_canary not in public_payload
    assert "正在梳理任务目标、已有信息和下一步处理顺序" not in public_payload
    assert "这是完全相同的最终回答。" in public_payload
    assert response.message.content[0].text == private_canary
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_redacts_exact_prompt_canary_from_final_answer_only() -> None:  # noqa: E501
    canary = "LANTAI_PRIVATE_PROMPT_CANARY_final_42"
    completed = _completed(f"答案 {canary}", message_id="msg-prompt-canary")
    client = ScriptedModelClient([[completed]])
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )
    runtime_input = _runtime_input()
    runtime_input.metadata = {"prompt_leak_canaries": [canary]}

    events = [event async for event in runtime.run_turn(runtime_input)]
    public_payload = _runtime_event_text(events)

    assert canary not in public_payload
    assert "[REDACTED_PROMPT_FINGERPRINT]" in public_payload
    assert completed.response is not None
    assert completed.response.message.content[0].text == f"答案 {canary}"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_automatically_fingerprints_actual_system_prompt() -> None:  # noqa: E501
    protected_line = (
        "PRIVATE_SYSTEM_POLICY_LINE never reveal this exact runtime"
        " instruction"
    )
    completed = _completed(
        f"答案 {protected_line}",
        message_id="msg-automatic-system-fingerprint",
    )
    client = ScriptedModelClient([[completed]])
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )
    runtime_input = _runtime_input()
    runtime_input.messages.insert(
        0,
        RuntimeMessage.text("system", protected_line),
    )

    events = [event async for event in runtime.run_turn(runtime_input)]
    public_payload = _runtime_event_text(events)

    assert protected_line not in public_payload
    assert "[REDACTED_PROMPT_FINGERPRINT]" in public_payload
    assert completed.response is not None
    assert completed.response.message.content[0].text == f"答案 {protected_line}"


def test_runtime_generated_system_instructions_have_explicit_provenance() -> (
    None
):
    messages = [
        # White-box lock exercises the actual private boundary.
        # pylint: disable-next=protected-access
        NativeLantaiRuntime._terminal_synthesis_message(reason="budget"),
        # White-box lock exercises the actual private boundary.
        # pylint: disable-next=protected-access
        NativeLantaiRuntime._tool_quarantine_recovery_message("web_search"),
        # White-box lock exercises the actual private boundary.
        # pylint: disable-next=protected-access
        NativeLantaiRuntime._file_delivery_recovery_message(file_ready=True),
        # White-box lock exercises the actual private boundary.
        # pylint: disable-next=protected-access
        NativeLantaiRuntime._collaboration_handoff_message(),
        # White-box lock exercises the actual private boundary.
        # pylint: disable-next=protected-access
        NativeLantaiRuntime._deep_research_final_synthesis_message(),
    ]

    assert all(
        message.metadata.get("instruction_trust") == "trusted_system"
        for message in messages
    )
    assert [
        message.metadata.get("instruction_origin") for message in messages
    ] == [
        "runtime.terminal_synthesis",
        "runtime.tool_quarantine_recovery",
        "runtime.file_delivery_recovery",
        "runtime.collaboration_handoff",
        "runtime.deep_research_final_synthesis",
    ]


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_tool_loop_redacts_public_canary_but_preserves_protocol_history() -> None:  # noqa: E501
    """Public display is guarded while provider history remains protocol-
    valid.
    """
    private_canary = (
        "PRIVATE_TOOL_CANARY persona=沈行知 ququ_probability=50_percent"
    )
    calls: list[str] = []

    async def lookup(query: str) -> str:
        calls.append(query)
        return "可用工具结果"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="lookup",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        ),
        lookup,
    )
    private_response = ModelResponse(
        message=ModelMessage(
            id="msg-private-tool",
            role="assistant",
            content=[ModelContentBlock(type="thinking", text=private_canary)],
        ),
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="thinking_delta",
                    message_id="msg-private-tool",
                    delta=private_canary,
                ),
                ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="msg-private-tool",
                    tool_call=ToolCall(
                        id="call-private-tool",
                        name="lookup",
                        arguments={"query": "澜台"},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-private-tool",
                    response=private_response,
                ),
            ],
            [
                _completed(
                    "工具路径和最终答案均保持不变。",
                    message_id="msg-final",
                ),
            ],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    runtime_input = _runtime_input()
    runtime_input.metadata = {"prompt_leak_canaries": [private_canary]}
    events = [event async for event in runtime.run_turn(runtime_input)]
    public_payload = _runtime_event_text(events)

    assert calls == ["澜台"]
    assert private_canary not in public_payload
    assert "正在梳理任务目标、已有信息和下一步处理顺序" not in public_payload
    assert "工具路径和最终答案均保持不变。" in public_payload
    assistant_history = client.requests[1].messages[-2]
    assert assistant_history.role == "assistant"
    assert assistant_history.content[0].type == "thinking"
    assert assistant_history.content[0].text == private_canary
    assert assistant_history.content[-1].data == {
        "id": "call-private-tool",
        "name": "lookup",
        "arguments": {"query": "澜台"},
    }
    assert private_response.message.content[0].text == private_canary


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_keeps_finalized_artifact_when_followup_model_fails(  # noqa: E501
    tmp_path: Path,
) -> None:
    artifact_path = tmp_path / "阶段成果.pptx"

    async def run_skill_script() -> ToolResponse:
        artifact_path.write_bytes(b"pptx")
        return ToolResponse(
            content=[TextBlock(text="presentation generated")],
        )

    async def finalize_deliverable(primary_path: str) -> ToolResponse:
        return ToolResponse(
            content=[TextBlock(text=f"Finalized {primary_path}")],
        )

    async def send_file_to_user(file_path: str) -> ToolResponse:
        return ToolResponse(
            content=[
                FileBlock(
                    filename=Path(file_path).name,
                    source={"type": "url", "url": Path(file_path).as_uri()},
                ),
            ],
        )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="run_skill_script",
            input_schema={"type": "object", "properties": {}},
        ),
        run_skill_script,
    )
    for name, argument_name, executor in (
        ("finalize_deliverable", "primary_path", finalize_deliverable),
        ("send_file_to_user", "file_path", send_file_to_user),
    ):
        registry.register_tool(
            ToolSpec(
                name=name,
                input_schema={
                    "type": "object",
                    "properties": {argument_name: {"type": "string"}},
                    "required": [argument_name],
                },
            ),
            executor,
        )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="msg-build-ppt",
                    tool_call=ToolCall(
                        id="call-build-ppt",
                        name="run_skill_script",
                        arguments={},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-build-ppt",
                    response=ModelResponse(
                        message=ModelMessage(
                            id="msg-build-ppt",
                            role="assistant",
                        ),
                    ),
                ),
            ],
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="msg-finalize-ppt",
                    tool_call=ToolCall(
                        id="call-finalize-ppt",
                        name="finalize_deliverable",
                        arguments={"primary_path": str(artifact_path)},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-finalize-ppt",
                    response=ModelResponse(
                        message=ModelMessage(
                            id="msg-finalize-ppt",
                            role="assistant",
                        ),
                    ),
                ),
            ],
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="msg-send-ppt",
                    tool_call=ToolCall(
                        id="call-send-ppt",
                        name="send_file_to_user",
                        arguments={"file_path": str(artifact_path)},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-send-ppt",
                    response=ModelResponse(
                        message=ModelMessage(
                            id="msg-send-ppt",
                            role="assistant",
                        ),
                    ),
                ),
            ],
            [
                ModelStreamEvent(
                    kind="error",
                    error="[422] MODEL_EXECUTION_FAILED",
                ),
            ],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )
    runtime_input = _runtime_input()
    runtime_input.request.workspace_dir = str(tmp_path)
    runtime_input.messages = [
        RuntimeMessage.text("user", "把这个做成PPT"),
    ]

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert events[0].type == "turn.started"
    assert "artifact_intent" not in events[0].payload
    assert any(
        event.type == "tool.call.completed"
        and event.payload.get("output", [{}])[-1].get("type") == "file"
        for event in events
    )
    assert not any(event.type == "turn.failed" for event in events)
    assert events[-1].type == "turn.completed"
    assert events[-1].payload == {
        "outcome_status": "incomplete",
        "details": {"partial_delivery": True},
        "error_type": "RuntimeError",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("visible_tool_preamble", [False, True])
async def test_native_runtime_keeps_pptx_candidate_tools_until_visual_followup(
    tmp_path: Path,
    visible_tool_preamble: bool,
) -> None:
    """A successful PPT generator result stays a candidate until verified."""

    artifact_path = tmp_path / "梁文锋投资者交流会纪要.pptx"
    preview_path = tmp_path / "renders" / "梁文锋投资者交流会纪要.png"
    delivered_paths: list[str] = []
    finalize_attempts: list[str] = []
    rendered_paths: list[str] = []

    async def run_skill_script() -> ToolResponse:
        artifact_path.write_bytes(b"pptx")
        return ToolResponse(
            content=[
                TextBlock(text="presentation generated"),
                FileBlock(
                    filename=artifact_path.name,
                    source={"type": "url", "url": artifact_path.as_uri()},
                ),
            ],
            metadata={
                "exit_code": 0,
                "skill_name": "pptx",
                "script_path": "scripts/generate.py",
            },
        )

    async def finalize_deliverable(primary_path: str) -> ToolResponse:
        finalize_attempts.append(primary_path)
        return ToolResponse(
            content=[TextBlock(text=f"Finalized {primary_path}")],
        )

    async def render_presentation(primary_path: str) -> ToolResponse:
        rendered_paths.append(primary_path)
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.write_bytes(b"png-preview")
        return ToolResponse(
            content=[
                TextBlock(text=f"Rendered {primary_path} to {preview_path}"),
            ],
        )

    async def send_file_to_user(file_path: str) -> ToolResponse:
        delivered_paths.append(file_path)
        return ToolResponse(
            content=[
                FileBlock(
                    filename=Path(file_path).name,
                    source={"type": "url", "url": Path(file_path).as_uri()},
                ),
            ],
        )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="run_skill_script",
            input_schema={"type": "object", "properties": {}},
        ),
        run_skill_script,
    )
    registry.register_tool(
        ToolSpec(
            name="render_presentation",
            input_schema={
                "type": "object",
                "properties": {"primary_path": {"type": "string"}},
                "required": ["primary_path"],
            },
            read_only=True,
        ),
        render_presentation,
    )
    for name, argument_name, artifact_role, executor in (
        (
            "finalize_deliverable",
            "primary_path",
            "primary",
            finalize_deliverable,
        ),
        ("send_file_to_user", "file_path", "delivery", send_file_to_user),
    ):
        registry.register_tool(
            ToolSpec(
                name=name,
                input_schema={
                    "type": "object",
                    "properties": {argument_name: {"type": "string"}},
                    "required": [argument_name],
                },
                output_semantics=ToolOutputSemantics(
                    artifact_role=artifact_role,
                    argument_path_fields=(argument_name,),
                ),
                effect_recovery=(
                    RuntimeEffectRecoveryCapability(
                        external_side_effect=True,
                        idempotency="natural_key",
                        probe="tool_specific",
                        compensation="not_required",
                        on_ambiguity="safe_to_retry",
                    )
                    if name == "finalize_deliverable"
                    else RuntimeEffectRecoveryCapability(
                        external_side_effect=True,
                        idempotency="not_idempotent",
                        probe="none",
                        compensation="none",
                        on_ambiguity="manual_disambiguation",
                    )
                ),
            ),
            executor,
        )

    def tool_turn(
        message_id: str,
        call_id: str,
        name: str,
        arguments: dict[str, str],
    ) -> list[ModelStreamEvent]:
        return [
            ModelStreamEvent(
                kind="tool_call_completed",
                message_id=message_id,
                tool_call=ToolCall(
                    id=call_id,
                    name=name,
                    arguments=arguments,
                ),
            ),
            ModelStreamEvent(
                kind="message_completed",
                message_id=message_id,
                response=ModelResponse(
                    message=ModelMessage.text(
                        "assistant",
                        "正在登记生成的文件。" if visible_tool_preamble else "",
                        id=message_id,
                    ),
                ),
            ),
        ]

    client = ScriptedModelClient(
        [
            tool_turn("msg-build", "call-build", "run_skill_script", {}),
            tool_turn(
                "msg-render",
                "call-render",
                "render_presentation",
                {"primary_path": str(artifact_path)},
            ),
            tool_turn(
                "msg-finalize",
                "call-finalize",
                "finalize_deliverable",
                {"primary_path": str(artifact_path)},
            ),
            tool_turn(
                "msg-send",
                "call-send",
                "send_file_to_user",
                {"file_path": str(artifact_path)},
            ),
            [_completed("PPTX 已生成并附在对话中。", message_id="msg-final")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        effect_journal=RuntimeEffectJournal(tmp_path),
        artifact_effect_journal=RuntimeArtifactEffectJournal(tmp_path),
    )
    runtime_input = _runtime_input()
    runtime_input.request.workspace_dir = str(tmp_path)
    runtime_input.metadata = {
        "attempt_id": "attempt-pptx-skill-finalization",
        "convergence_mode": "off",
        "runtime_budget": {"max_no_progress_rounds": 4},
    }
    runtime_input.messages = [RuntimeMessage.text("user", "把这个做成PPT")]

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert client.requests[1].tool_choice == "auto"
    assert {
        "render_presentation",
        "finalize_deliverable",
    }.issubset({tool.name for tool in client.requests[1].tools})
    assert str(artifact_path) in client.requests[1].messages[-1].text_content()
    assert client.requests[2].tool_choice == "auto"
    assert "finalize_deliverable" in {
        tool.name for tool in client.requests[2].tools
    }
    delivery_request = client.requests[3]
    assert delivery_request.tool_choice == "required"
    assert [tool.name for tool in delivery_request.tools] == [
        "send_file_to_user",
    ]
    assert rendered_paths == [str(artifact_path)]
    assert finalize_attempts == [str(artifact_path)]
    candidate_records = [
        record
        # White-box lock exercises the actual private boundary.
        # pylint: disable-next=protected-access
        for record in runtime._artifact_effect_journal.list_records(
            runtime_input.request.run_id,
            "attempt-pptx-skill-finalization",
        )
        if record.artifact_role == "candidate"
    ]
    assert candidate_records
    assert candidate_records[-1].status == "committed"
    assert not any(
        event.payload.get("kind") == "final_synthesis_tool_call_ignored"
        for event in events
    )
    assert delivered_paths == [str(artifact_path)]
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("convergence_mode", ["off", None])
# Keep the existing test selector unchanged.
async def test_native_runtime_allows_three_progressing_pptx_revisions_before_finalize(  # noqa: E501
    tmp_path: Path,
    convergence_mode: str | None,
) -> None:
    """Healthy candidate revisions must survive both legacy and production
    convergence.
    """

    artifact_path = tmp_path / "三轮修订.pptx"
    revisions: list[int] = []
    finalized: list[bytes] = []
    delivered: list[bytes] = []

    async def run_skill_script() -> ToolResponse:
        artifact_path.write_bytes(b"initial")
        return ToolResponse(
            content=[
                FileBlock(
                    filename=artifact_path.name,
                    source={"type": "url", "url": artifact_path.as_uri()},
                ),
            ],
            metadata={
                "exit_code": 0,
                "skill_name": "pptx",
                "script_path": "scripts/generate.py",
            },
        )

    async def revise_presentation(
        primary_path: str,
        revision: int,
    ) -> ToolResponse:
        assert primary_path == str(artifact_path)
        revisions.append(revision)
        artifact_path.write_bytes(f"revision-{revision}".encode())
        return ToolResponse(
            content=[TextBlock(text=f"revision {revision} applied")],
            metadata={"progress_made": True},
        )

    async def finalize_deliverable(primary_path: str) -> ToolResponse:
        finalized.append(Path(primary_path).read_bytes())
        return ToolResponse(content=[TextBlock(text="finalized")])

    async def send_file_to_user(file_path: str) -> ToolResponse:
        delivered.append(Path(file_path).read_bytes())
        return ToolResponse(
            content=[
                FileBlock(
                    filename=Path(file_path).name,
                    source={"type": "url", "url": Path(file_path).as_uri()},
                ),
            ],
        )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(name="run_skill_script", input_schema={"type": "object"}),
        run_skill_script,
    )
    registry.register_tool(
        ToolSpec(
            name="revise_presentation",
            input_schema={
                "type": "object",
                "properties": {
                    "primary_path": {"type": "string"},
                    "revision": {"type": "integer"},
                },
                "required": ["primary_path", "revision"],
            },
            output_semantics=ToolOutputSemantics(
                artifact_role="candidate",
                argument_path_fields=("primary_path",),
            ),
        ),
        revise_presentation,
    )
    for name, argument_name, artifact_role, executor in (
        (
            "finalize_deliverable",
            "primary_path",
            "primary",
            finalize_deliverable,
        ),
        ("send_file_to_user", "file_path", "delivery", send_file_to_user),
    ):
        registry.register_tool(
            ToolSpec(
                name=name,
                input_schema={
                    "type": "object",
                    "properties": {argument_name: {"type": "string"}},
                    "required": [argument_name],
                },
                output_semantics=ToolOutputSemantics(
                    artifact_role=artifact_role,
                    argument_path_fields=(argument_name,),
                ),
            ),
            executor,
        )

    def tool_turn(
        call_id: str,
        name: str,
        arguments: dict[str, object],
    ) -> list[ModelStreamEvent]:
        return [
            ModelStreamEvent(
                kind="tool_call_completed",
                tool_call=ToolCall(id=call_id, name=name, arguments=arguments),
            ),
            ModelStreamEvent(
                kind="message_completed",
                response=ModelResponse(message=ModelMessage(role="assistant")),
            ),
        ]

    client = ScriptedModelClient(
        [
            tool_turn("build", "run_skill_script", {}),
            *[
                tool_turn(
                    f"revise-{revision}",
                    "revise_presentation",
                    {"primary_path": str(artifact_path), "revision": revision},
                )
                for revision in (1, 2, 3)
            ],
            tool_turn(
                "finalize",
                "finalize_deliverable",
                {"primary_path": str(artifact_path)},
            ),
            tool_turn(
                "send",
                "send_file_to_user",
                {"file_path": str(artifact_path)},
            ),
            [_completed("三轮修订后的 PPT 已交付。", message_id="final")],
        ],
    )
    runtime_input = _runtime_input()
    runtime_input.request.workspace_dir = str(tmp_path)
    runtime_input.metadata = {
        "attempt_id": "attempt-three-progressing-revisions",
    }
    if convergence_mode is not None:
        runtime_input.metadata["convergence_mode"] = convergence_mode
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        effect_journal=RuntimeEffectJournal(tmp_path),
        artifact_effect_journal=RuntimeArtifactEffectJournal(tmp_path),
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert revisions == [1, 2, 3]
    assert finalized == [b"revision-3"]
    assert delivered == [b"revision-3"]
    assert not any(
        event.payload.get("kind") == "artifact_candidate_unverified"
        for event in events
    )
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_does_not_auto_deliver_unfinalized_workspace_files(  # noqa: E501
    tmp_path: Path,
) -> None:
    delivered_paths: list[str] = []

    async def build_report() -> ToolResponse:
        report = tmp_path / "项目复盘.docx"
        report.write_bytes(b"generated-document")
        return ToolResponse(content=[TextBlock(text=f"Created {report}.")])

    async def send_file_to_user(file_path: str) -> ToolResponse:
        delivered_paths.append(file_path)
        return ToolResponse(
            content=[
                FileBlock(
                    filename=Path(file_path).name,
                    source={"type": "url", "url": Path(file_path).as_uri()},
                ),
            ],
        )

    registry = LantaiToolRegistry()
    tool_definitions: tuple[
        tuple[str, Callable[..., Awaitable[ToolResponse]], dict[str, Any]],
        ...,
    ] = (
        ("build_report", build_report, {}),
        (
            "send_file_to_user",
            send_file_to_user,
            {"file_path": {"type": "string"}},
        ),
    )
    for name, executor, properties in tool_definitions:
        registry.register_tool(
            ToolSpec(
                name=name,
                input_schema={
                    "type": "object",
                    "properties": properties,
                    "required": list(properties),
                },
            ),
            executor,
        )

    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="msg-build-report",
                    tool_call=ToolCall(
                        id="call-build-report",
                        name="build_report",
                        arguments={},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-build-report",
                    response=ModelResponse(
                        message=ModelMessage(
                            id="msg-build-report",
                            role="assistant",
                        ),
                    ),
                ),
            ],
            [_completed("复盘已经生成，路径在工作区。", message_id="msg-path")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )
    runtime_input = _runtime_input()
    runtime_input.request.workspace_dir = str(tmp_path)
    runtime_input.messages = [
        RuntimeMessage.text("user", "帮我生成一份项目复盘"),
    ]

    events = [event async for event in runtime.run_turn(runtime_input)]

    # Exact [] assertion also rejects None and other false values.
    # pylint: disable-next=use-implicit-booleaness-not-comparison
    assert delivered_paths == []
    assert not any(
        event.type == "tool.call.completed"
        and event.payload.get("name") == "send_file_to_user"
        for event in events
    )
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
async def test_native_runtime_marks_error_text_tool_response_failed() -> None:
    async def failing_tool() -> ToolResponse:
        return ToolResponse(
            content=[
                TextBlock(text="Command failed with exit code 1."),
            ],
        )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="failing_tool",
            input_schema={"type": "object", "properties": {}},
            read_only=True,
        ),
        failing_tool,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="msg-tool-error",
                    tool_call=ToolCall(
                        id="call-tool-error",
                        name="failing_tool",
                        arguments={},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-tool-error",
                    response=ModelResponse(
                        message=ModelMessage(
                            id="msg-tool-error",
                            role="assistant",
                        ),
                    ),
                ),
            ],
            [_completed("已确认失败。", message_id="msg-after-tool-error")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    completed = next(
        event
        for event in events
        if event.type == "tool.call.completed"
        and event.item_id == "call-tool-error"
    )
    assert completed.status == "failed"
    assert completed.payload["ok"] is False
    assert "exit code 1" in completed.payload["error"]
    assert client.requests[1].messages[-1].content[0].data["output"] == [
        {
            "type": "text",
            "text": "Command failed with exit code 1.",
        },
    ]


@pytest.mark.asyncio
async def test_native_runtime_delivers_only_the_model_finalized_path(
    tmp_path: Path,
) -> None:
    delivered_paths: list[str] = []
    pptx_path = tmp_path / "董事会汇报.pptx"

    async def write_file(file_path: str, content: str) -> ToolResponse:
        (tmp_path / file_path).write_text(content, encoding="utf-8")
        return ToolResponse(
            content=[
                TextBlock(
                    text=f"Wrote {len(content)} bytes to {file_path}.",
                ),
            ],
        )

    async def finalize_deliverable(primary_path: str) -> ToolResponse:
        return ToolResponse(
            content=[TextBlock(text=f"Finalized {primary_path}")],
        )

    async def send_file_to_user(file_path: str) -> ToolResponse:
        delivered_paths.append(file_path)
        return ToolResponse(
            content=[
                FileBlock(
                    filename=Path(file_path).name,
                    source={"type": "url", "url": f"file:///tmp/{file_path}"},
                ),
            ],
        )

    registry = LantaiToolRegistry()
    for name, executor in (
        ("write_file", write_file),
        ("finalize_deliverable", finalize_deliverable),
        ("send_file_to_user", send_file_to_user),
    ):
        registry.register_tool(
            ToolSpec(
                name=name,
                input_schema={
                    "type": "object",
                    "properties": {
                        (
                            "primary_path"
                            if name == "finalize_deliverable"
                            else "file_path"
                        ): {"type": "string"},
                        **(
                            {"content": {"type": "string"}}
                            if name == "write_file"
                            else {}
                        ),
                    },
                    "required": (
                        ["file_path", "content"]
                        if name == "write_file"
                        else ["primary_path"]
                        if name == "finalize_deliverable"
                        else ["file_path"]
                    ),
                },
            ),
            executor,
        )

    def tool_turn(
        *,
        message_id: str,
        call_id: str,
        name: str,
        arguments: dict[str, str],
    ) -> list[ModelStreamEvent]:
        return [
            ModelStreamEvent(
                kind="tool_call_completed",
                message_id=message_id,
                tool_call=ToolCall(
                    id=call_id,
                    name=name,
                    arguments=arguments,
                ),
            ),
            ModelStreamEvent(
                kind="message_completed",
                message_id=message_id,
                response=ModelResponse(
                    message=ModelMessage(id=message_id, role="assistant"),
                ),
            ),
        ]

    client = ScriptedModelClient(
        [
            tool_turn(
                message_id="msg-write-script",
                call_id="call-write-script",
                name="write_file",
                arguments={
                    "file_path": "build_ppt.js",
                    "content": "build();",
                },
            ),
            tool_turn(
                message_id="msg-write-pptx",
                call_id="call-write-pptx",
                name="write_file",
                arguments={
                    "file_path": "董事会汇报.pptx",
                    "content": "pptx-bytes",
                },
            ),
            tool_turn(
                message_id="msg-finalize-pptx",
                call_id="call-finalize-pptx",
                name="finalize_deliverable",
                arguments={"primary_path": str(pptx_path)},
            ),
            tool_turn(
                message_id="msg-send-pptx",
                call_id="call-send-pptx",
                name="send_file_to_user",
                arguments={"file_path": str(pptx_path)},
            ),
            [_completed("PPT 已附在对话中。", message_id="msg-final-pptx")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )
    runtime_input = _runtime_input()
    runtime_input.request.workspace_dir = str(tmp_path)
    runtime_input.messages = [
        RuntimeMessage.text("user", "把前面的内容做成 PPT 给我"),
    ]

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert delivered_paths == [str(pptx_path)]
    assert not any(path.endswith("build_ppt.js") for path in delivered_paths)
    assert client.requests[1].tool_choice == "auto"
    assert client.requests[3].tool_choice == "required"
    assert [tool.name for tool in client.requests[3].tools] == [
        "send_file_to_user",
    ]
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_blocks_declared_artifact_delivery_until_finalized(  # noqa: E501
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "品牌传播方案.pptx"
    delivered: list[bytes] = []

    async def declare_deliverable_contract(
        kind: str,
        accepted_extensions: list[str],
        visual_verification_required: bool,
    ) -> dict[str, object]:
        return {
            "kind": kind,
            "accepted_extensions": accepted_extensions,
            "visual_verification_required": visual_verification_required,
        }

    async def build_presentation(file_path: str) -> ToolResponse:
        Path(file_path).write_bytes(b"verified-pptx")
        return ToolResponse(content=[TextBlock(text=f"Created {file_path}")])

    async def finalize_deliverable(primary_path: str) -> ToolResponse:
        return ToolResponse(
            content=[TextBlock(text=f"Finalized {primary_path}")],
        )

    async def send_file_to_user(file_path: str) -> ToolResponse:
        delivered.append(Path(file_path).read_bytes())
        return ToolResponse(
            content=[
                FileBlock(
                    filename=Path(file_path).name,
                    source={"type": "url", "url": Path(file_path).as_uri()},
                ),
            ],
        )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="declare_deliverable_contract",
            metadata={"runtime_owned": True, "deliverable_contract": True},
        ),
        declare_deliverable_contract,
    )
    registry.register_tool(
        ToolSpec(
            name="build_presentation",
            input_schema={
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
            output_semantics=ToolOutputSemantics(
                artifact_role="candidate",
                argument_path_fields=("file_path",),
            ),
        ),
        build_presentation,
    )
    registry.register_tool(
        ToolSpec(
            name="finalize_deliverable",
            input_schema={
                "type": "object",
                "properties": {"primary_path": {"type": "string"}},
                "required": ["primary_path"],
            },
            output_semantics=ToolOutputSemantics(
                artifact_role="primary",
                argument_path_fields=("primary_path",),
            ),
        ),
        finalize_deliverable,
    )
    registry.register_tool(
        ToolSpec(
            name="send_file_to_user",
            input_schema={
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
            output_semantics=ToolOutputSemantics(
                artifact_role="delivery",
                argument_path_fields=("file_path",),
            ),
        ),
        send_file_to_user,
    )

    def tool_turn(
        call_id: str,
        name: str,
        arguments: dict,
    ) -> list[ModelStreamEvent]:
        return [
            ModelStreamEvent(
                kind="tool_call_completed",
                tool_call=ToolCall(id=call_id, name=name, arguments=arguments),
            ),
            ModelStreamEvent(
                kind="message_completed",
                response=ModelResponse(message=ModelMessage(role="assistant")),
            ),
        ]

    client = ScriptedModelClient(
        [
            tool_turn(
                "declare",
                "declare_deliverable_contract",
                {
                    "kind": "presentation",
                    "accepted_extensions": [".pptx"],
                    "visual_verification_required": True,
                },
            ),
            tool_turn(
                "build",
                "build_presentation",
                {"file_path": str(artifact)},
            ),
            tool_turn(
                "premature-send",
                "send_file_to_user",
                {"file_path": str(artifact)},
            ),
            tool_turn(
                "finalize",
                "finalize_deliverable",
                {"primary_path": str(artifact)},
            ),
            tool_turn(
                "verified-send",
                "send_file_to_user",
                {"file_path": str(artifact)},
            ),
            [_completed("PPT 已完成并交付。", message_id="final")],
        ],
    )
    runtime_input = _runtime_input()
    runtime_input.request.workspace_dir = str(tmp_path)
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        effect_journal=RuntimeEffectJournal(tmp_path),
        artifact_effect_journal=RuntimeArtifactEffectJournal(tmp_path),
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert delivered == [b"verified-pptx"]
    rejected = next(
        event
        for event in events
        if event.type == "tool.call.completed"
        and event.item_id == "premature-send"
    )
    assert rejected.status == "failed"
    assert rejected.payload["error_code"] == "artifact_not_verified"
    assert any(
        event.type == "runtime.warning"
        and event.payload.get("kind") == "artifact_delivery_unverified"
        for event in events
    )
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_executes_three_distinct_image_operations_in_one_turn() -> None:  # noqa: E501
    executions: list[str] = []

    async def generate_image(prompt: str) -> str:
        executions.append(prompt)
        return f"generated:/tmp/durable-image-{len(executions)}.png"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="generate_image",
            input_schema={
                "type": "object",
                "properties": {"prompt": {"type": "string"}},
                "required": ["prompt"],
            },
        ),
        generate_image,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-image-1",
                        name="generate_image",
                        arguments={"prompt": "答辩封面"},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-image-2",
                        name="generate_image",
                        arguments={"prompt": "研究框架"},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-image-3",
                        name="generate_image",
                        arguments={"prompt": "研究路线图"},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("图片已经生成。", message_id="msg-final")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert executions == ["答辩封面", "研究框架", "研究路线图"]
    assert [event.type for event in events].count("tool.call.started") == 3
    assert [event.type for event in events].count("tool.call.completed") == 3
    for request in client.requests[:3]:
        assert [tool.name for tool in request.tools] == ["generate_image"]


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_allows_three_read_edit_verify_rounds_on_same_path() -> None:  # noqa: E501
    file_state = {"content": "v1"}
    read_results: list[str] = []
    writes: list[str] = []

    async def read_file(file_path: str) -> str:
        assert file_path == "report.md"
        content = file_state["content"]
        read_results.append(content)
        return content

    async def write_file(file_path: str, content: str) -> str:
        assert file_path == "report.md"
        file_state["content"] = content
        writes.append(content)
        return f"wrote:{content}"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="read_file",
            input_schema={
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
            read_only=True,
        ),
        read_file,
    )
    registry.register_tool(
        ToolSpec(
            name="write_file",
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["file_path", "content"],
            },
        ),
        write_file,
    )

    def tool_round(index: int, name: str, arguments: dict[str, str]):
        return [
            ModelStreamEvent(
                kind="tool_call_completed",
                tool_call=ToolCall(
                    id=f"call-file-{index}",
                    name=name,
                    arguments=arguments,
                ),
            ),
            ModelStreamEvent(
                kind="message_completed",
                response=ModelResponse(message=ModelMessage(role="assistant")),
            ),
        ]

    client = ScriptedModelClient(
        [
            tool_round(1, "read_file", {"file_path": "report.md"}),
            tool_round(
                2,
                "write_file",
                {"file_path": "report.md", "content": "v2"},
            ),
            tool_round(3, "read_file", {"file_path": "report.md"}),
            tool_round(
                4,
                "write_file",
                {"file_path": "report.md", "content": "v3"},
            ),
            tool_round(5, "read_file", {"file_path": "report.md"}),
            [_completed("三轮读改验完成。", message_id="msg-file-final")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert read_results == ["v1", "v2", "v3"]
    assert writes == ["v2", "v3"]
    assert [event.type for event in events].count("tool.call.started") == 5
    assert [event.type for event in events].count("tool.call.completed") == 5
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_deduplicates_provider_replay_by_tool_call_identity() -> None:  # noqa: E501
    executions: list[str] = []

    async def lookup(query: str) -> str:
        executions.append(query)
        return "durable original result"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="lookup",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            read_only=True,
        ),
        lookup,
    )

    def replayed_round() -> list[ModelStreamEvent]:
        return [
            ModelStreamEvent(
                kind="tool_call_completed",
                tool_call=ToolCall(
                    id="provider-call-replayed",
                    name="lookup",
                    arguments={"query": "stable query"},
                ),
            ),
            ModelStreamEvent(
                kind="message_completed",
                response=ModelResponse(message=ModelMessage(role="assistant")),
            ),
        ]

    client = ScriptedModelClient(
        [
            replayed_round(),
            replayed_round(),
            [_completed("已使用原始查询结果。", message_id="msg-replay-final")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert executions == ["stable query"]
    assert [event.type for event in events].count("tool.call.started") == 1
    assert [event.type for event in events].count("tool.call.completed") == 1
    replay_result = client.requests[-1].messages[-1]
    assert replay_result.role == "tool"
    assert (
        replay_result.content[0].data["output"][0]["text"]
        == "durable original result"
    )
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_does_not_force_poll_background_agents_before_reply() -> None:  # noqa: E501
    async def submit_to_agent(to_agent: str, text: str) -> str:
        del to_agent, text
        return (
            "[TASK_ID: task-poem]\n"
            "[SESSION: session-agent]\n\n"
            "Task submitted successfully."
        )

    async def check_agent_task(task_id: str) -> str:
        return (
            f"[TASK_ID: {task_id}]\n"
            "[STATUS: finished]\n"
            "Task completed.\n\n"
            "北京的风从末班车里醒来。"
        )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="submit_to_agent",
            input_schema={
                "type": "object",
                "properties": {
                    "to_agent": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["to_agent", "text"],
            },
        ),
        submit_to_agent,
    )
    registry.register_tool(
        ToolSpec(
            name="check_agent_task",
            input_schema={
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
        ),
        check_agent_task,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-submit-agent",
                        name="submit_to_agent",
                        arguments={
                            "to_agent": "writer",
                            "text": "写一首北京现代诗",
                        },
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [
                _completed(
                    "我先直接回答：北京的风从末班车里醒来。",
                    message_id="msg-final",
                ),
            ],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert len(client.requests) == 2
    assert events[-1].type == "turn.completed"
    assert [event.type for event in events].count("turn.completed") == 1
    assert not any(
        "当前主轮不得结束" in message.text_content()
        for message in client.requests[1].messages
        if message.role == "system"
    )


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_does_not_censor_deliverable_text_after_collaboration() -> None:  # noqa: E501
    async def chat_with_agent(to_agent: str, text: str) -> str:
        del to_agent, text
        return "建议写地铁、胡同、风和末班车。"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="chat_with_agent",
            input_schema={
                "type": "object",
                "properties": {
                    "to_agent": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["to_agent", "text"],
            },
        ),
        chat_with_agent,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-chat-agent",
                        name="chat_with_agent",
                        arguments={
                            "to_agent": "writer",
                            "text": "给北京现代诗提供意象",
                        },
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [
                _completed(
                    "《厨房还在等》\n地铁把黑夜送回胡同，\n厨房还在等一盏未归的灯。",
                    message_id="msg-poem",
                ),
            ],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert len(client.requests) == 2
    assert events[-1].type == "turn.completed"
    assert any(
        event.type == "message.completed" and "厨房还在等" in event.summary
        for event in events
    )
    assert "<lantai_final>" not in _runtime_event_text(events)


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_agent_collaboration_started_event_exposes_identity_without_private_prompt() -> None:  # noqa: E501
    async def chat_with_agent(to_agent: str, text: str) -> str:
        del to_agent, text
        return "建议从真实使用场景切入。"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="chat_with_agent",
            input_schema={
                "type": "object",
                "properties": {
                    "to_agent": {"type": "string"},
                    "text": {"type": "string"},
                    "session_id": {"type": "string"},
                },
                "required": ["to_agent", "text"],
            },
        ),
        chat_with_agent,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-agent-safe-identity",
                        name="chat_with_agent",
                        arguments={
                            "to_agent": "Lantai_Mocha_Agent",
                            "text": "私密完整任务：审阅 /Users/example/secret.txt",
                            "session_id": "private-session-id",
                            "api_key": "secret-key",
                        },
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [
                _completed(
                    "已综合视觉建议。",
                    message_id="msg-agent-safe-final",
                ),
            ],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]
    started = next(
        event for event in events if event.type == "tool.call.started"
    )
    serialized = json.dumps(started.payload, ensure_ascii=False)

    assert started.payload["arguments"] == {
        "to_agent": "Lantai_Mocha_Agent",
    }
    assert "私密完整任务" not in serialized
    assert "secret.txt" not in serialized
    assert "private-session-id" not in serialized
    assert "secret-key" not in serialized


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_streams_monotonic_collaboration_batch_snapshots() -> None:  # noqa: E501
    def snapshot(revision: int, status: str) -> dict[str, Any]:
        return {
            "batch_id": "batch-visible",
            "revision": revision,
            "status": status,
            "execution_scope": {
                "root_run_id": "root-execution-1",
                "parent_attempt_id": "turn-1:attempt-3",
                "parent_effect_id": "turn-1:tool:call-batch",
            },
            "members": [
                {
                    "member_id": "member-1",
                    "agent_id": "Lantai_Matcha_Agent",
                    "display_name": "抹茶",
                    "role": "写作",
                    "label": "起草主线",
                    "status": "completed"
                    if status == "completed"
                    else "running",
                    "attempt": 1,
                    "duration_ms": 12,
                },
            ],
        }

    async def collaborate_with_agents(tasks: list[dict[str, str]]):
        del tasks
        yield ToolResponse(
            content=[],
            metadata={"collaboration": snapshot(1, "running")},
            stream=True,
            is_last=False,
        )
        yield ToolResponse(
            content=[],
            metadata={"collaboration": snapshot(2, "running")},
            stream=True,
            is_last=False,
        )
        yield ToolResponse(
            content=[TextBlock(text='{"results":[{"result":"完成"}]}')],
            metadata={"collaboration": snapshot(3, "completed")},
            is_last=True,
        )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="collaborate_with_agents",
            input_schema={
                "type": "object",
                "properties": {"tasks": {"type": "array"}},
                "required": ["tasks"],
            },
            metadata={"progressive_tool": "collaboration"},
        ),
        collaborate_with_agents,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-batch",
                        name="collaborate_with_agents",
                        arguments={
                            "tasks": [
                                {
                                    "to_agent": "Lantai_Matcha_Agent",
                                    "text": "私有任务一",
                                    "label": "起草主线",
                                },
                                {
                                    "to_agent": "Lantai_Cola_Agent",
                                    "text": "私有任务二",
                                    "label": "打磨标题",
                                },
                            ],
                        },
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("大家还在处理，我稍后再汇总。", message_id="msg-batch-wait")],
            [
                _completed(
                    "已经综合两位协作者的建议。",
                    message_id="msg-batch-final",
                ),
            ],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    runtime_input = _runtime_input()
    runtime_input.request.chat_id = "chat-parent"
    runtime_input.metadata.update(
        {
            "attempt_id": "turn-1:attempt-3",
            "runtime_generation": 7,
            "state_workspace_dir": "/tmp/state-workspace",
        },
    )
    events = [event async for event in runtime.run_turn(runtime_input)]
    deltas = [event for event in events if event.type == "tool.call.delta"]
    completed = next(
        event for event in events if event.type == "tool.call.completed"
    )

    assert [
        event.payload["collaboration"]["revision"] for event in deltas
    ] == [
        1,
        2,
    ]
    assert completed.payload["collaboration"]["revision"] == 3
    expected_scope = {
        "root_run_id": "root-execution-1",
        "parent_run_id": "turn-1",
        "parent_attempt_id": "turn-1:attempt-3",
        "parent_effect_id": "turn-1:tool:call-batch",
        "attempt_id": "turn-1:attempt-3",
        "runtime_generation": 7,
        "chat_id": "chat-parent",
        "session_id": "session-1",
        "workspace_dir": "/tmp/state-workspace",
    }
    assert all(
        event.payload["collaboration"]["execution_scope"] == expected_scope
        for event in deltas
    )
    assert (
        completed.payload["collaboration"]["execution_scope"] == expected_scope
    )
    assert [event.seq for event in events] == sorted(
        event.seq for event in events
    )
    assert "私有任务" not in _runtime_event_text(events)
    assert len(client.requests) == 2
    assert "<lantai_final>" not in _runtime_event_text(events)
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
async def test_native_runtime_replays_tool_call_opaque_metadata() -> None:
    async def search() -> str:
        return "done"

    registry = LantaiToolRegistry()
    registry.register_tool(ToolSpec(name="search"), search)
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-gemini",
                        name="search",
                        metadata={
                            "extra_content": {
                                "thought_signature": "opaque-signature",
                            },
                        },
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("done", message_id="final-gemini")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    _ = [event async for event in runtime.run_turn(_runtime_input())]

    replayed_tool_call = next(
        block
        for block in client.requests[1].messages[-2].content
        if block.type == "tool_call"
    )
    assert replayed_tool_call.data["extra_content"] == {
        "thought_signature": "opaque-signature",
    }


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_terminalizes_latest_collaboration_snapshot_on_timeout() -> None:  # noqa: E501
    closed = asyncio.Event()

    async def collaborate_with_agents(tasks: list[dict[str, str]]):
        del tasks
        try:
            yield ToolResponse(
                content=[],
                metadata={
                    "collaboration": {
                        "batch_id": "batch-timeout",
                        "revision": 4,
                        "status": "running",
                        "members": [
                            {
                                "member_id": "member-1",
                                "agent_id": "Lantai_Matcha_Agent",
                                "display_name": "抹茶",
                                "role": "写作",
                                "label": "起草主线",
                                "status": "completed",
                                "attempt": 1,
                                "duration_ms": 12,
                            },
                            {
                                "member_id": "member-2",
                                "agent_id": "Lantai_Cola_Agent",
                                "display_name": "可乐",
                                "role": "文案",
                                "label": "打磨标题",
                                "status": "running",
                                "attempt": 1,
                                "duration_ms": 8,
                            },
                        ],
                    },
                },
                stream=True,
                is_last=False,
            )
            await asyncio.Event().wait()
        finally:
            closed.set()

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="collaborate_with_agents",
            input_schema={
                "type": "object",
                "properties": {"tasks": {"type": "array"}},
                "required": ["tasks"],
            },
            metadata={"progressive_tool": "collaboration"},
        ),
        collaborate_with_agents,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-batch-timeout",
                        name="collaborate_with_agents",
                        arguments={
                            "tasks": [
                                {
                                    "to_agent": "Lantai_Matcha_Agent",
                                    "text": "私有写作任务",
                                    "label": "起草主线",
                                },
                                {
                                    "to_agent": "Lantai_Cola_Agent",
                                    "text": "私有文案任务",
                                    "label": "打磨标题",
                                },
                            ],
                        },
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("已清楚说明协作超时。", message_id="msg-timeout-final")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        tool_timeout_seconds=0.01,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]
    completed = next(
        event for event in events if event.type == "tool.call.completed"
    )
    collaboration = completed.payload["collaboration"]

    assert closed.is_set()
    assert completed.status == "failed"
    assert collaboration["batch_id"] == "batch-timeout"
    assert collaboration["revision"] == 5
    assert collaboration["status"] == "partial"
    assert collaboration["phase"] == "closed"
    assert collaboration["host"]["display_name"] == "沈行知"
    assert collaboration["closure"]["outcome"] == "partial"
    assert collaboration["closure"]["synthesis_ready"] is True
    assert collaboration["closure"]["deliverable_ready"] is False
    assert collaboration["controls"] == {
        "can_takeover": False,
        "can_stop": False,
        "can_pause": False,
    }
    assert [member["status"] for member in collaboration["members"]] == [
        "completed",
        "failed",
    ]
    assert collaboration["members"][1]["error_code"] == "tool_timeout"
    assert "私有写作任务" not in _runtime_event_text(events)
    assert "私有文案任务" not in _runtime_event_text(events)


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_does_not_invent_room_when_validation_fails_before_first_delta() -> None:  # noqa: E501
    async def collaborate_with_agents(tasks: list[dict[str, str]]):
        del tasks
        # This fixture must be an async generator without events.
        # pylint: disable-next=using-constant-test
        if False:  # pragma: no cover - keeps this an async generator
            yield None
        raise ValueError("Agent is unavailable")

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="collaborate_with_agents",
            input_schema={
                "type": "object",
                "properties": {"tasks": {"type": "array"}},
                "required": ["tasks"],
            },
            metadata={"progressive_tool": "collaboration"},
        ),
        collaborate_with_agents,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-invalid-batch",
                        name="collaborate_with_agents",
                        arguments={
                            "tasks": [
                                {
                                    "to_agent": "Lantai_Matcha_Agent",
                                    "text": "私有写作任务",
                                    "label": "起草主线",
                                },
                                {
                                    "to_agent": "Lantai_Cola_Agent",
                                    "text": "私有文案任务",
                                    "label": "打磨标题",
                                },
                            ],
                        },
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("已说明校验失败。", message_id="msg-invalid-final")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]
    completed = next(
        event for event in events if event.type == "tool.call.completed"
    )

    assert completed.status == "failed"
    assert "collaboration" not in completed.payload
    assert completed.payload["error_code"] == "validation_failed"
    assert "私有写作任务" not in _runtime_event_text(events)
    assert "私有文案任务" not in _runtime_event_text(events)


@pytest.mark.asyncio
async def test_native_runtime_records_tool_effect_journal_for_success(
    tmp_path,
) -> None:
    async def lookup(query: str) -> str:
        return f"found:{query}"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="lookup",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        ),
        lookup,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(kind="message_start", message_id="msg-1"),
                ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="msg-1",
                    tool_call=ToolCall(
                        id="call-effect",
                        name="lookup",
                        arguments={"query": "澜台"},
                        metadata={"attempt_id": "attempt-native"},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-1",
                    response=ModelResponse(
                        message=ModelMessage(id="msg-1", role="assistant"),
                    ),
                ),
            ],
            [_completed("好了", message_id="msg-2")],
        ],
    )
    journal = RuntimeEffectJournal(tmp_path)
    runtime_input = RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id="session-1",
            user_id="user-1",
            run_id="run-native",
            workspace_dir="/tmp/workspace",
            metadata={
                "contract_hash": "sha256:contract",
                "policy_snapshot_hash": "sha256:policy",
            },
        ),
        messages=[RuntimeMessage.text("user", "hello")],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        effect_journal=journal,
    )

    events = [event async for event in runtime.run_turn(runtime_input)]
    records = journal.list_effects("run-native", "attempt-native")

    assert [event.type for event in events].count("tool.call.completed") == 1
    completed = next(
        event for event in events if event.type == "tool.call.completed"
    )
    assert completed.payload["effect_id"] == "effect:call-effect"
    assert completed.payload["attempt_id"] == "attempt-native"
    canonical_tool = completed.payload["canonical_tool"]
    assert canonical_tool["effect_id"] == "effect:call-effect"
    assert canonical_tool["effect_attempt_id"] == "attempt-native"
    assert canonical_tool["effect_revision"] == 2
    assert canonical_tool["effect_status"] == "committed"
    assert canonical_tool["canonical"]["value_sha256"]
    assert (
        canonical_tool["model_projection"]["instruction_trust"]
        == "untrusted_tool_evidence"
    )
    assert [record.status for record in records] == [
        "intent",
        "running",
        "committed",
    ]
    assert [record.revision for record in records] == [0, 1, 2]
    assert all(record.effect_id == "effect:call-effect" for record in records)
    assert all(record.tool_name == "lookup" for record in records)
    assert records[0].step_id == "tool-call:call-effect"
    assert records[0].source == "chat"
    assert records[0].owner.user_id == "user-1"
    assert records[0].policy_snapshot_hash == "sha256:policy"
    assert records[0].args_hash.startswith("sha256:")
    assert records[-1].result_hash and records[-1].result_hash.startswith(
        "sha256:",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verify_scope", "expected_write_status"),
    [
        ("scope-1", "probed_committed"),
        ("scope-other", "ambiguous"),
    ],
)
async def test_native_runtime_keeps_gui_effect_ambiguous_until_readback_probe(
    tmp_path,
    verify_scope: str,
    expected_write_status: str,
) -> None:
    async def gui_write() -> ToolResponse:
        return ToolResponse(
            content=[
                TextBlock(
                    # Keep the canonical API field or synthetic fixture bytes
                    # unchanged.
                    text='{"effect_id":"gui-effect-1","effect_status":"ambiguous"}',  # noqa: E501
                ),
            ],
            metadata={
                "external_effect_id": "gui-effect-1",
                "external_effect_status": "ambiguous",
                "external_dispatch_status": "sent",
                "external_effect_scope_hash": "scope-1",
            },
            is_error=True,
        )

    async def gui_verify(effect_id_to_resolve: str) -> ToolResponse:
        assert effect_id_to_resolve == "gui-effect-1"
        return ToolResponse(
            content=[TextBlock(text='{"effect_status":"verified"}')],
            metadata={
                "external_effect_status": "verified",
                "external_dispatch_status": "not_sent",
                "resolves_external_effect_id": effect_id_to_resolve,
                "resolves_external_effect_scope_hash": verify_scope,
            },
        )

    registry = LantaiToolRegistry()
    registry.register_tool(ToolSpec(name="mac_act"), gui_write)
    registry.register_tool(ToolSpec(name="mac_verify"), gui_verify)
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(kind="message_start", message_id="gui-1"),
                ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="gui-1",
                    tool_call=ToolCall(
                        id="call-gui-write",
                        name="mac_act",
                        arguments={},
                        metadata={"attempt_id": "attempt-gui"},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="gui-1",
                    response=ModelResponse(
                        message=ModelMessage(id="gui-1", role="assistant"),
                    ),
                ),
            ],
            [
                ModelStreamEvent(kind="message_start", message_id="gui-2"),
                ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="gui-2",
                    tool_call=ToolCall(
                        id="call-gui-verify",
                        name="mac_verify",
                        arguments={"effect_id_to_resolve": "gui-effect-1"},
                        metadata={"attempt_id": "attempt-gui"},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="gui-2",
                    response=ModelResponse(
                        message=ModelMessage(id="gui-2", role="assistant"),
                    ),
                ),
            ],
            [_completed("已核验", message_id="gui-3")],
        ],
    )
    journal = RuntimeEffectJournal(tmp_path)
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        effect_journal=journal,
    )
    runtime_input = RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id="session-gui",
            user_id="user-1",
            run_id="run-gui",
            workspace_dir=str(tmp_path),
            metadata={
                "contract_hash": "sha256:contract",
                "policy_snapshot_hash": "sha256:policy",
            },
        ),
        messages=[RuntimeMessage.text("user", "update selected app")],
    )

    events = [event async for event in runtime.run_turn(runtime_input)]
    records = journal.list_effects("run-gui", "attempt-gui")
    latest = {}
    for record in records:
        latest[record.effect_id] = record

    write_effect = latest["effect:call-gui-write"]
    verify_effect = latest["effect:call-gui-verify"]
    assert write_effect.status == expected_write_status
    assert (
        write_effect.ambiguity_reason
        == "external_effect:gui-effect-1:scope:scope-1"
    )
    assert verify_effect.status == "committed"
    completed = [
        event for event in events if event.type == "tool.call.completed"
    ]
    assert (
        completed[0].payload["canonical_tool"]["effect_status"] == "ambiguous"
    )
    assert (
        completed[1].payload["canonical_tool"]["effect_status"] == "committed"
    )


@pytest.mark.asyncio
async def test_native_runtime_settles_declared_primary_artifact_effect(
    tmp_path,
) -> None:
    artifact = tmp_path / "deliverables" / "answer.html"

    def finalize(primary_path: str) -> dict[str, str]:
        Path(primary_path).parent.mkdir(parents=True, exist_ok=True)
        Path(primary_path).write_text("<h1>done</h1>", encoding="utf-8")
        return {"primary_path": primary_path}

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="finalize",
            input_schema={
                "type": "object",
                "properties": {"primary_path": {"type": "string"}},
                "required": ["primary_path"],
            },
            output_semantics=ToolOutputSemantics(
                artifact_role="primary",
                argument_path_fields=("primary_path",),
            ),
        ),
        finalize,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-artifact",
                        name="finalize",
                        arguments={"primary_path": str(artifact)},
                        metadata={"attempt_id": "attempt-artifact"},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("done", message_id="msg-after-artifact")],
        ],
    )
    effect_journal = RuntimeEffectJournal(tmp_path)
    artifact_journal = RuntimeArtifactEffectJournal(tmp_path)
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        effect_journal=effect_journal,
        artifact_effect_journal=artifact_journal,
    )
    runtime_input = RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id="session-1",
            user_id="user-1",
            run_id="run-artifact",
            workspace_dir=str(tmp_path),
        ),
        messages=[RuntimeMessage.text("user", "create an artifact")],
    )

    events = [event async for event in runtime.run_turn(runtime_input)]
    completed = next(
        event for event in events if event.type == "tool.call.completed"
    )
    records = artifact_journal.list_records(
        "run-artifact",
        "attempt-artifact",
    )

    assert [record.status for record in records] == ["intent", "committed"]
    assert [record.revision for record in records] == [0, 1]
    assert records[-1].relative_path == "deliverables/answer.html"
    assert records[-1].sha256
    assert completed.payload["artifact_effects"] == [
        {
            "artifact_effect_id": records[-1].artifact_effect_id,
            "artifact_id": records[-1].artifact_id,
            "artifact_role": "primary",
            "status": "committed",
            "relative_path": "deliverables/answer.html",
            "delivery_idempotency_key": records[-1].delivery_idempotency_key,
            "size_bytes": records[-1].size_bytes,
            "sha256": records[-1].sha256,
            "media_type": "text/html",
        },
    ]
    assert str(tmp_path) not in json.dumps(
        completed.payload["artifact_effects"],
        ensure_ascii=False,
    )


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_auto_delivery_is_resumed_once_from_artifact_effects(  # noqa: E501
    tmp_path,
) -> None:
    artifact = tmp_path / "answer.md"
    artifact.write_text("# answer\n", encoding="utf-8")
    delivered: list[str] = []

    def finalize_deliverable(primary_path: str) -> ToolResponse:
        return ToolResponse(
            content=[TextBlock(text=f"Finalized {primary_path}")],
        )

    def send_file_to_user(file_path: str) -> ToolResponse:
        delivered.append(file_path)
        return ToolResponse(
            content=[
                FileBlock(
                    filename=Path(file_path).name,
                    source={"type": "url", "url": Path(file_path).as_uri()},
                ),
            ],
        )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="finalize_deliverable",
            input_schema={
                "type": "object",
                "properties": {"primary_path": {"type": "string"}},
                "required": ["primary_path"],
            },
            output_semantics=ToolOutputSemantics(
                artifact_role="primary",
                argument_path_fields=("primary_path",),
            ),
        ),
        finalize_deliverable,
    )
    registry.register_tool(
        ToolSpec(
            name="send_file_to_user",
            input_schema={
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
            output_semantics=ToolOutputSemantics(
                artifact_role="delivery",
                argument_path_fields=("file_path",),
            ),
        ),
        send_file_to_user,
    )
    effect_journal = RuntimeEffectJournal(tmp_path)
    artifact_journal = RuntimeArtifactEffectJournal(tmp_path)
    first_client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-finalize-once",
                        name="finalize_deliverable",
                        arguments={"primary_path": str(artifact)},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("文件已完成。", message_id="msg-final")],
        ],
    )
    runtime_input = _runtime_input()
    runtime_input.request.workspace_dir = str(tmp_path)
    first_runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: first_client,
        tool_registry=registry,
        effect_journal=effect_journal,
        artifact_effect_journal=artifact_journal,
    )

    first_events = [
        event async for event in first_runtime.run_turn(runtime_input)
    ]

    assert delivered == [str(artifact)]
    delivery_started = [
        event
        for event in first_events
        if event.type == "tool.call.started"
        and event.payload.get("tool_name") == "send_file_to_user"
    ]
    assert len(delivery_started) == 1
    assert delivery_started[0].item_id.startswith("auto-file-delivery-")
    delivery_call_id = delivery_started[0].item_id

    second_client = ScriptedModelClient(
        [[_completed("文件已交付。", message_id="msg-resumed")]],
    )
    second_runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: second_client,
        tool_registry=registry,
        effect_journal=effect_journal,
        artifact_effect_journal=artifact_journal,
    )
    second_events = [
        event async for event in second_runtime.run_turn(runtime_input)
    ]

    assert delivered == [str(artifact)]
    assert not any(
        event.type == "tool.call.started"
        and event.payload.get("tool_name") == "send_file_to_user"
        for event in second_events
    )
    assert delivery_call_id.startswith("auto-file-delivery-")


@pytest.mark.asyncio
async def test_native_runtime_rejects_stale_verification_before_delivery(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "answer.md"
    artifact.write_bytes(b"verified")
    delivered: list[bytes] = []

    def finalize_deliverable(primary_path: str) -> ToolResponse:
        return ToolResponse(
            content=[TextBlock(text=f"Finalized {primary_path}")],
        )

    def send_file_to_user(file_path: str) -> ToolResponse:
        delivered.append(Path(file_path).read_bytes())
        return ToolResponse(
            content=[
                FileBlock(
                    filename=Path(file_path).name,
                    source={"type": "url", "url": Path(file_path).as_uri()},
                ),
            ],
        )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="finalize_deliverable",
            input_schema={
                "type": "object",
                "properties": {"primary_path": {"type": "string"}},
                "required": ["primary_path"],
            },
            output_semantics=ToolOutputSemantics(
                artifact_role="primary",
                argument_path_fields=("primary_path",),
            ),
        ),
        finalize_deliverable,
    )
    registry.register_tool(
        ToolSpec(
            name="send_file_to_user",
            input_schema={
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
            output_semantics=ToolOutputSemantics(
                artifact_role="delivery",
                argument_path_fields=("file_path",),
            ),
        ),
        send_file_to_user,
    )

    def tool_turn(
        call_id: str,
        name: str,
        arguments: dict,
    ) -> list[ModelStreamEvent]:
        return [
            ModelStreamEvent(
                kind="tool_call_completed",
                tool_call=ToolCall(id=call_id, name=name, arguments=arguments),
            ),
            ModelStreamEvent(
                kind="message_completed",
                response=ModelResponse(message=ModelMessage(role="assistant")),
            ),
        ]

    class MutatingScriptedModelClient(ScriptedModelClient):
        async def stream(self, request: ModelRequest):
            if len(self.requests) == 1:
                artifact.write_bytes(b"changed-after-verification")
            async for event in super().stream(request):
                yield event

    client = MutatingScriptedModelClient(
        [
            tool_turn(
                "initial-finalize",
                "finalize_deliverable",
                {"primary_path": str(artifact)},
            ),
            tool_turn(
                "stale-send",
                "send_file_to_user",
                {"file_path": str(artifact)},
            ),
            tool_turn(
                "current-finalize",
                "finalize_deliverable",
                {"primary_path": str(artifact)},
            ),
            tool_turn(
                "current-send",
                "send_file_to_user",
                {"file_path": str(artifact)},
            ),
            [_completed("文件已完成。", message_id="msg-final")],
        ],
    )
    runtime_input = _runtime_input()
    runtime_input.request.workspace_dir = str(tmp_path)
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        effect_journal=RuntimeEffectJournal(tmp_path),
        artifact_effect_journal=RuntimeArtifactEffectJournal(tmp_path),
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert delivered == [b"changed-after-verification"]
    warning = next(
        event
        for event in events
        if event.type == "runtime.warning"
        and event.payload.get("kind") == "artifact_verification_stale"
    )
    assert warning.payload["error_code"] == "artifact_revision_changed"
    assert warning.payload["details"]["delivery_status"] == "candidate"
    stale_completion = next(
        event
        for event in events
        if event.type == "tool.call.completed"
        and event.item_id == "stale-send"
    )
    assert stale_completion.status == "failed"
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("revised_bytes", [b"# revised\n", b"# answer\n"])
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_delivers_same_path_revision_without_repeating_same_bytes(  # noqa: E501
    tmp_path,
    revised_bytes: bytes,
) -> None:
    artifact = tmp_path / "answer.md"
    artifact.write_text("# answer\n", encoding="utf-8")
    delivered: list[bytes] = []

    def finalize_deliverable(primary_path: str) -> ToolResponse:
        return ToolResponse(
            content=[TextBlock(text=f"Finalized {primary_path}")],
        )

    def send_file_to_user(file_path: str) -> ToolResponse:
        delivered.append(Path(file_path).read_bytes())
        return ToolResponse(
            content=[
                FileBlock(
                    filename=Path(file_path).name,
                    source={"type": "url", "url": Path(file_path).as_uri()},
                ),
            ],
        )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="finalize_deliverable",
            input_schema={
                "type": "object",
                "properties": {"primary_path": {"type": "string"}},
                "required": ["primary_path"],
            },
            output_semantics=ToolOutputSemantics(
                artifact_role="primary",
                argument_path_fields=("primary_path",),
            ),
        ),
        finalize_deliverable,
    )
    registry.register_tool(
        ToolSpec(
            name="send_file_to_user",
            input_schema={
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
            output_semantics=ToolOutputSemantics(
                artifact_role="delivery",
                argument_path_fields=("file_path",),
            ),
        ),
        send_file_to_user,
    )

    def revise() -> str:
        artifact.write_bytes(revised_bytes)
        return "revision ready"

    registry.register_tool(ToolSpec(name="revise"), revise)

    def tool_turn(
        call_id: str,
        name: str,
        arguments: dict,
    ) -> list[ModelStreamEvent]:
        return [
            ModelStreamEvent(
                kind="tool_call_completed",
                tool_call=ToolCall(id=call_id, name=name, arguments=arguments),
            ),
            ModelStreamEvent(
                kind="message_completed",
                response=ModelResponse(message=ModelMessage(role="assistant")),
            ),
        ]

    effect_journal = RuntimeEffectJournal(tmp_path)
    artifact_journal = RuntimeArtifactEffectJournal(tmp_path)
    first_client = ScriptedModelClient(
        [
            tool_turn(
                "first-finalize",
                "finalize_deliverable",
                {"primary_path": str(artifact)},
            ),
            tool_turn(
                "first-send",
                "send_file_to_user",
                {"file_path": str(artifact)},
            ),
            tool_turn("revise", "revise", {}),
            tool_turn(
                "revised-finalize",
                "finalize_deliverable",
                {"primary_path": str(artifact)},
            ),
            tool_turn(
                "confirm-finalize",
                "finalize_deliverable",
                {"primary_path": str(artifact)},
            ),
            [_completed("文件已完成。", message_id="msg-final")],
        ],
    )
    runtime_input = _runtime_input()
    runtime_input.request.workspace_dir = str(tmp_path)
    first_runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: first_client,
        tool_registry=registry,
        effect_journal=effect_journal,
        artifact_effect_journal=artifact_journal,
    )

    first_events = [
        event async for event in first_runtime.run_turn(runtime_input)
    ]

    expected = [b"# answer\n"]
    if revised_bytes != expected[0]:
        expected.append(revised_bytes)
    assert delivered == expected
    assert first_events[-1].type == "turn.completed"
    assert (
        sum(
            event.type == "tool.call.started"
            and event.payload.get("tool_name") == "finalize_deliverable"
            for event in first_events
        )
        == 3
    )

    second_client = ScriptedModelClient(
        [[_completed("文件已交付。", message_id="msg-resumed")]],
    )
    second_runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: second_client,
        tool_registry=registry,
        effect_journal=effect_journal,
        artifact_effect_journal=artifact_journal,
    )
    second_events = [
        event async for event in second_runtime.run_turn(runtime_input)
    ]

    assert delivered == expected
    assert not any(
        event.type == "tool.call.started"
        and event.payload.get("tool_name") == "send_file_to_user"
        for event in second_events
    )


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_fails_closed_when_effect_intent_cannot_persist() -> None:  # noqa: E501
    executed: list[str] = []

    def lookup(query: str) -> str:
        executed.append(query)
        return f"found:{query}"

    class FailingIntentJournal:
        def append_effect(self, _effect) -> None:
            raise RuntimeError("private journal detail")

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="lookup",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        ),
        lookup,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-effect-intent-fail",
                        name="lookup",
                        arguments={"query": "澜台"},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("这次操作没有执行。", message_id="msg-after-intent-fail")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        effect_journal=FailingIntentJournal(),  # type: ignore[arg-type]
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]
    completed = next(
        event for event in events if event.type == "tool.call.completed"
    )

    # Exact [] assertion also rejects None and other false values.
    # pylint: disable-next=use-implicit-booleaness-not-comparison
    assert executed == []
    assert completed.status == "failed"
    assert (
        completed.payload["error"]
        == "Tool effect journal failed before execution."
    )
    assert "private journal detail" not in _runtime_event_text(events)


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_does_not_reexecute_when_effect_settlement_fails() -> None:  # noqa: E501
    executed: list[str] = []

    def lookup(query: str) -> str:
        executed.append(query)
        return f"found:{query}"

    class FailingSettlementJournal:
        def __init__(self) -> None:
            self.effects: list[Any] = []

        def append_effect(self, effect) -> None:
            self.effects.append(effect)
            if len(self.effects) == 3:
                raise RuntimeError("private settlement detail")

        def append_effect_if_admissible(self, effect, recovery) -> None:
            del recovery
            self.append_effect(effect)

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="lookup",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        ),
        lookup,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-effect-settle-fail",
                        name="lookup",
                        arguments={"query": "澜台"},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("这次结果没有提交。", message_id="msg-after-settle-fail")],
        ],
    )
    journal = FailingSettlementJournal()
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        effect_journal=journal,  # type: ignore[arg-type]
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]
    completed = next(
        event for event in events if event.type == "tool.call.completed"
    )

    assert executed == ["澜台"]
    assert [effect.status for effect in journal.effects] == [
        "intent",
        "running",
        "committed",
    ]
    assert completed.status == "failed"
    assert (
        completed.payload["error"]
        == "Tool effect journal settlement failed after execution."
    )
    assert "private settlement detail" not in _runtime_event_text(events)


@pytest.mark.asyncio
async def test_native_runtime_refreshes_runtime_heartbeat_during_silent_tool(
    tmp_path,
    monkeypatch,
) -> None:
    tool_entered = asyncio.Event()
    release_tool = asyncio.Event()

    async def lookup(query: str) -> str:
        tool_entered.set()
        await release_tool.wait()
        return f"found:{query}"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="lookup",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        ),
        lookup,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(kind="message_start", message_id="msg-1"),
                ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="msg-1",
                    tool_call=ToolCall(
                        id="call-slow",
                        name="lookup",
                        arguments={"query": "澜台"},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-1",
                    response=ModelResponse(
                        message=ModelMessage(id="msg-1", role="assistant"),
                    ),
                ),
            ],
            [_completed("好了", message_id="msg-2")],
        ],
    )
    run_id = "run-native-heartbeat"
    state_workspace = tmp_path / "topic-state"
    project_workspace = tmp_path / "external-project"
    await record_chat_runtime_run_started(
        workspace_dir=state_workspace,
        agent_id="default",
        chat_id="chat-native-heartbeat",
        session_id="session-1",
        run_id=run_id,
        effective_workspace_dir=str(project_workspace),
    )
    runtime_input = RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id="session-1",
            user_id="user-1",
            run_id=run_id,
            workspace_dir=str(project_workspace),
            metadata={"state_workspace_dir": str(state_workspace)},
        ),
        messages=[RuntimeMessage.text("user", "hello")],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )
    monkeypatch.setattr(
        native_runtime_module,
        "DEFAULT_NATIVE_TOOL_HEARTBEAT_INTERVAL_SECONDS",
        0.01,
    )

    async def consume_events() -> list[Any]:
        return [event async for event in runtime.run_turn(runtime_input)]

    task = asyncio.create_task(consume_events())
    await asyncio.wait_for(tool_entered.wait(), timeout=1.0)

    store = RuntimeRunStore(state_workspace)
    before = await store.load_contract(run_id)
    assert before is not None
    assert before.status == "running"
    assert before.heartbeat_at is not None

    await asyncio.sleep(0.05)
    during = await store.load_contract(run_id)

    release_tool.set()
    events = await asyncio.wait_for(task, timeout=1.0)

    assert during is not None
    assert during.status == "running"
    assert during.revision > before.revision
    assert during.heartbeat_at is not None
    assert during.heartbeat_at > before.heartbeat_at
    assert not (project_workspace / ".lantai").exists()
    assert [event.type for event in events].count("tool.call.completed") == 1


@pytest.mark.asyncio
async def test_native_runtime_records_tool_effect_journal_for_failure(
    tmp_path,
) -> None:
    def explode() -> str:
        raise RuntimeError("boom")

    registry = LantaiToolRegistry()
    registry.register_tool(ToolSpec(name="explode"), explode)
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-fail",
                        name="explode",
                        arguments={},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("done", message_id="msg-2")],
        ],
    )
    journal = RuntimeEffectJournal(tmp_path)
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        effect_journal=journal,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]
    records = journal.list_effects("turn-1", "turn-1:native-attempt")

    completed = next(
        event for event in events if event.type == "tool.call.completed"
    )
    assert completed.status == "failed"
    assert completed.payload["metadata"]["invocation_status"] == "completed"
    assert completed.payload["metadata"]["side_effect_outcome"] == "unknown"
    assert completed.payload["metadata"]["automatic_retry_allowed"] is False
    assert [record.status for record in records] == [
        "intent",
        "running",
        "ambiguous",
    ]
    assert records[-1].result_hash and records[-1].result_hash.startswith(
        "sha256:",
    )


@pytest.mark.asyncio
async def test_native_runtime_denies_protected_write_before_effect_intent(
    tmp_path,
) -> None:
    calls = 0

    async def external_write() -> str:
        nonlocal calls
        calls += 1
        return "written"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="external_write",
            effect_recovery=RuntimeEffectRecoveryCapability(
                external_side_effect=True,
                idempotency="not_idempotent",
                probe="none",
                compensation="none",
                on_ambiguity="manual_disambiguation",
            ),
        ),
        external_write,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-beta-denied",
                        name="external_write",
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("未执行。", message_id="msg-final")],
        ],
    )
    journal = RuntimeEffectJournal(tmp_path)
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        effect_journal=journal,
        protected_write_admission=lambda capability: (
            "beta_access_expired:protected_write"
            if capability == "tool.external_write:external_write"
            else None
        ),
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert calls == 0
    assert not any(event.type == "tool.started" for event in events)
    completed = next(
        event for event in events if event.type == "tool.call.completed"
    )
    assert completed.payload["error_code"] == "beta_access_denied"
    assert completed.payload["metadata"]["policy_source"] == "beta_access"
    assert completed.payload["metadata"]["invocation_status"] == "not_started"
    assert (
        completed.payload["metadata"]["side_effect_outcome"]
        == "known_not_applied"
    )
    assert journal.list_effects("turn-1", "turn-1:native-attempt") == []


@pytest.mark.asyncio
async def test_native_runtime_interrupts_started_read_only_tool_for_new_input(
    tmp_path,
) -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow_lookup() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(name="slow_lookup", read_only=True),
        slow_lookup,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-read-only",
                        name="slow_lookup",
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("已改向。", message_id="msg-read-only-redirect")],
        ],
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "metadata": {
                "state_workspace_dir": str(tmp_path),
                "attempt_id": "attempt-read-only",
            },
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )
    task = asyncio.create_task(
        _collect_runtime_events(runtime, runtime_input),
    )
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    control_store = RuntimeTurnControlStore(tmp_path)
    control_store.append_input(
        command_id="redirect-read-only",
        run_id="turn-1",
        attempt_id="attempt-read-only",
        chat_id="session-1",
        message=RuntimeMessage.text("user", "停止查询，直接总结已有信息。"),
        intent="redirect",
    )

    events = await asyncio.wait_for(task, timeout=2.0)

    assert cancelled.is_set()
    completed = next(
        event for event in events if event.type == "tool.call.completed"
    )
    assert completed.payload["error_code"] == "steered"
    assert completed.payload["metadata"]["invocation_status"] == "interrupted"
    assert (
        completed.payload["metadata"]["side_effect_outcome"]
        == "known_not_applied"
    )
    assert "停止查询" in client.requests[1].messages[-1].text_content()


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_blocks_later_batch_effect_after_read_only_redirect(  # noqa: E501
    tmp_path,
) -> None:
    lookup_entered = asyncio.Event()
    external_writes = 0

    async def slow_lookup() -> str:
        lookup_entered.set()
        await asyncio.Event().wait()
        return "stale"

    async def external_write() -> str:
        nonlocal external_writes
        external_writes += 1
        return "written"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(name="slow_lookup", read_only=True),
        slow_lookup,
    )
    registry.register_tool(ToolSpec(name="external_write"), external_write)
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(id="call-lookup", name="slow_lookup"),
                ),
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(id="call-write", name="external_write"),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("已改向，未执行旧写入。", message_id="msg-batch-redirect")],
        ],
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "metadata": {
                "state_workspace_dir": str(tmp_path),
                "attempt_id": "attempt-batch-redirect",
            },
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )
    task = asyncio.create_task(_collect_runtime_events(runtime, runtime_input))
    await asyncio.wait_for(lookup_entered.wait(), timeout=1.0)
    RuntimeTurnControlStore(tmp_path).append_input(
        command_id="redirect-old-batch",
        run_id="turn-1",
        attempt_id="attempt-batch-redirect",
        chat_id="session-1",
        message=RuntimeMessage.text("user", "停止旧批次，不要执行后面的写入。"),
        intent="redirect",
    )

    events = await asyncio.wait_for(task, timeout=2.0)

    assert external_writes == 0
    completed = [
        event for event in events if event.type == "tool.call.completed"
    ]
    assert [event.item_id for event in completed[:2]] == [
        "call-lookup",
        "call-write",
    ]
    assert completed[0].payload["error_code"] == "steered"
    assert completed[1].payload["error_code"] == "steered_before_effect"
    assert (
        completed[1].payload["metadata"]["invocation_status"] == "not_started"
    )


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_waits_for_started_external_effect_before_redirect(  # noqa: E501
    tmp_path,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def external_write() -> str:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return "written"

    registry = LantaiToolRegistry()
    registry.register_tool(ToolSpec(name="external_write"), external_write)
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-external",
                        name="external_write",
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("外部作用已记账，现已改向。", message_id="msg-after-write")],
        ],
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "metadata": {
                "state_workspace_dir": str(tmp_path),
                "attempt_id": "attempt-external",
            },
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )
    task = asyncio.create_task(
        _collect_runtime_events(runtime, runtime_input),
    )
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    control_store = RuntimeTurnControlStore(tmp_path)
    control_store.append_input(
        command_id="redirect-after-effect-start",
        run_id="turn-1",
        attempt_id="attempt-external",
        chat_id="session-1",
        message=RuntimeMessage.text("user", "写入完成后不要再做第二次，转去汇报。"),
        intent="redirect",
    )
    done, _pending = await asyncio.wait({task}, timeout=0.15)
    assert not done

    release.set()
    events = await asyncio.wait_for(task, timeout=2.0)

    assert calls == 1
    completed = next(
        event for event in events if event.type == "tool.call.completed"
    )
    assert completed.status == "completed"
    assert completed.payload["metadata"]["invocation_status"] == "completed"
    assert completed.payload["metadata"]["side_effect_outcome"] == "committed"
    assert "不要再做第二次" in client.requests[1].messages[-1].text_content()


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_settles_started_batch_effect_but_blocks_next_effect(  # noqa: E501
    tmp_path,
) -> None:
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    calls: list[str] = []

    async def external_write(label: str) -> str:
        calls.append(label)
        if label == "first":
            first_entered.set()
            await release_first.wait()
        return f"written:{label}"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="external_write",
            input_schema={
                "type": "object",
                "properties": {"label": {"type": "string"}},
                "required": ["label"],
            },
        ),
        external_write,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-write-first",
                        name="external_write",
                        arguments={"label": "first"},
                    ),
                ),
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-write-second",
                        name="external_write",
                        arguments={"label": "second"},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("只结算了已开始的写入。", message_id="msg-after-batch-write")],
        ],
    )
    runtime_input = _runtime_input().model_copy(
        update={
            "metadata": {
                "state_workspace_dir": str(tmp_path),
                "attempt_id": "attempt-external-batch",
            },
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )
    task = asyncio.create_task(_collect_runtime_events(runtime, runtime_input))
    await asyncio.wait_for(first_entered.wait(), timeout=1.0)
    RuntimeTurnControlStore(tmp_path).append_input(
        command_id="redirect-after-first-batch-effect",
        run_id="turn-1",
        attempt_id="attempt-external-batch",
        chat_id="session-1",
        message=RuntimeMessage.text("user", "完成已开始的写入，但不要开始第二个。"),
        intent="redirect",
    )
    release_first.set()

    events = await asyncio.wait_for(task, timeout=2.0)

    assert calls == ["first"]
    completed = [
        event for event in events if event.type == "tool.call.completed"
    ]
    assert completed[0].item_id == "call-write-first"
    assert (
        completed[0].payload["metadata"]["side_effect_outcome"] == "committed"
    )
    assert completed[1].item_id == "call-write-second"
    assert completed[1].payload["error_code"] == "steered_before_effect"
    assert (
        completed[1].payload["metadata"]["side_effect_outcome"]
        == "known_not_applied"
    )


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_sync_timeout_marks_external_effect_unknown_and_blocks_blind_retry(  # noqa: E501
    tmp_path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def external_write(value: str) -> str:
        nonlocal calls
        calls += 1
        entered.set()
        release.wait(timeout=1)
        return f"written:{value}"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="external_write",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
            effect_recovery=RuntimeEffectRecoveryCapability(
                external_side_effect=True,
                idempotency="not_idempotent",
                probe="none",
                compensation="none",
                on_ambiguity="manual_disambiguation",
            ),
        ),
        external_write,
    )
    repeated_call = {
        "name": "external_write",
        "arguments": {"value": "once"},
    }
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(id="call-timeout-1", **repeated_call),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(id="call-timeout-2", **repeated_call),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("需要先核对外部状态。", message_id="msg-final")],
        ],
    )
    journal = RuntimeEffectJournal(tmp_path)
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        tool_timeout_seconds=0.01,
        effect_journal=journal,
    )

    try:
        events = [event async for event in runtime.run_turn(_runtime_input())]
    finally:
        release.set()

    assert entered.is_set()
    assert calls == 1
    completed = [
        event for event in events if event.type == "tool.call.completed"
    ]
    assert len(completed) == 1
    assert completed[0].status == "failed"
    assert completed[0].payload["error_code"] == "tool_timeout"
    assert (
        completed[0].payload["metadata"]["invocation_status"] == "interrupted"
    )
    assert completed[0].payload["metadata"]["side_effect_outcome"] == "unknown"
    assert completed[0].payload["metadata"]["automatic_retry_allowed"] is False
    assert completed[0].payload["metadata"]["requires_human"] is True
    records = journal.list_effects("turn-1", "turn-1:native-attempt")
    assert [record.status for record in records] == [
        "intent",
        "running",
        "ambiguous",
    ]
    assert client.requests[2].messages[-1].metadata["skipped"] is True
    assert "ambiguous external effect" in str(
        client.requests[2].messages[-1].metadata["reason"],
    )


@pytest.mark.asyncio
async def test_runtime_owned_tool_can_prove_failed_effect_was_not_applied(
    tmp_path,
) -> None:
    async def guarded_runtime_write(value: str) -> ToolResponse:
        return ToolResponse(
            content=[TextBlock(text=f"validation rejected {value}")],
            metadata={"side_effect_outcome": "known_not_applied"},
            is_error=True,
        )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="guarded_runtime_write",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
            effect_recovery=RuntimeEffectRecoveryCapability(
                external_side_effect=True,
                idempotency="not_idempotent",
                probe="none",
                compensation="none",
                on_ambiguity="manual_disambiguation",
            ),
            metadata={
                "runtime_owned": True,
                "trusted_effect_settlement": True,
            },
        ),
        guarded_runtime_write,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-runtime-rejected",
                        name="guarded_runtime_write",
                        arguments={"value": "invalid"},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [
                _completed(
                    "The guarded write was rejected.",
                    message_id="final",
                ),
            ],
        ],
    )
    journal = RuntimeEffectJournal(tmp_path)
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        effect_journal=journal,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    completed = [
        event for event in events if event.type == "tool.call.completed"
    ]
    assert len(completed) == 1
    assert (
        completed[0].payload["metadata"]["side_effect_outcome"]
        == "known_not_applied"
    )
    assert completed[0].payload["metadata"]["automatic_retry_allowed"] is True
    assert completed[0].payload["metadata"]["requires_human"] is False
    records = journal.list_effects("turn-1", "turn-1:native-attempt")
    assert [record.status for record in records] == [
        "intent",
        "running",
        "failed",
    ]


@pytest.mark.asyncio
async def test_external_tool_cannot_forge_known_not_applied_in_result_metadata(
    tmp_path,
) -> None:
    calls = 0

    async def unsafe_plugin_write(value: str) -> ToolResponse:
        nonlocal calls
        calls += 1
        return ToolResponse(
            content=[TextBlock(text=f"plugin failed after writing {value}")],
            metadata={"side_effect_outcome": "known_not_applied"},
            is_error=True,
        )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="unsafe_plugin_write",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
            read_only=False,
        ),
        unsafe_plugin_write,
    )
    repeated = {"name": "unsafe_plugin_write", "arguments": {"value": "once"}}
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(id="call-forged-1", **repeated),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(id="call-forged-2", **repeated),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [
                _completed(
                    "External state needs inspection.",
                    message_id="final",
                ),
            ],
        ],
    )
    journal = RuntimeEffectJournal(tmp_path)
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        effect_journal=journal,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert calls == 1
    completed = [
        event for event in events if event.type == "tool.call.completed"
    ]
    assert len(completed) == 1
    assert completed[0].payload["metadata"]["side_effect_outcome"] == "unknown"
    assert completed[0].payload["metadata"]["automatic_retry_allowed"] is False
    assert completed[0].payload["metadata"]["requires_human"] is True
    records = journal.list_effects("turn-1", "turn-1:native-attempt")
    assert [record.status for record in records] == [
        "intent",
        "running",
        "ambiguous",
    ]


@pytest.mark.asyncio
# One fault scenario includes admission and settlement assertions.
# pylint: disable-next=too-many-statements
async def test_builtin_presentation_recovers_preflight_and_delivers(
    tmp_path,
    monkeypatch,
) -> None:
    from PIL import Image
    from pptx import Presentation

    from lantai.agents.tool_registry import build_lantai_tool_registry
    from lantai.config.context import (
        WorkspaceContext,
        set_current_workspace_context,
    )
    from lantai.presentation.models import VisualCriticResult
    from lantai.presentation.render import RenderResult

    class PassingCritic:
        def review(self, _slides):
            return VisualCriticResult(
                critic_id="test-preflight-critic",
                issues=[],
            )

    def render(_source, output_dir, *, timeout=120):
        del timeout
        destination = Path(output_dir)
        destination.mkdir(parents=True, exist_ok=True)
        pdf = destination / "presentation.pdf"
        pdf.write_bytes(b"test-pdf")
        preview = destination / "slide-1.png"
        image = Image.new("RGB", (640, 360), "white")
        image.paste("black", (0, 0, 320, 360))
        image.save(preview)
        return RenderResult(pdf_path=pdf, slide_images=(preview,))

    monkeypatch.setattr("lantai.presentation.service.render_pptx", render)
    monkeypatch.setattr(
        "lantai.agents.tools.presentation.UtilityPresentationVisualCritic",
        lambda _agent_id: PassingCritic(),
    )
    builtins = build_lantai_tool_registry(
        agent_config=SimpleNamespace(tools=SimpleNamespace(builtin_tools={})),
        request_context={"distribution": "developer_id"},
        include_default_policy_middleware=False,
    )
    registry = LantaiToolRegistry()
    spec, executor = builtins.get_tool("create_presentation")
    registry.register_tool(spec, executor)
    arguments: dict[str, Any] = {
        "title": "山间短诗",
        "output_filename": "poem.pptx",
        "slides": [
            {
                "layout_id": "cover",
                "objects": [
                    {
                        "kind": "text",
                        "role": "title",
                        "slot": "title",
                        "text": "山间短诗",
                    },
                ],
            },
        ],
        "required_facts": ["山间短诗"],
        "narrative_arc": "从山峰到诗句",
        "storyboard": [
            {
                "purpose": "呈现主题",
                "key_message": "山间短诗",
                "evidence": ["山间短诗"],
                "relationship_type": "thesis",
                "visual_role": "cover",
                "source_indexes": [],
                "required_facts": ["新写的诗句，不是全稿事实"],
            },
        ],
    }
    corrected = {
        **arguments,
        "storyboard": [
            {**arguments["storyboard"][0], "required_facts": ["山间短诗"]},
        ],
    }
    scripts = []
    for index, call_arguments in enumerate((arguments, arguments, corrected)):
        scripts.append(
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id=f"call-presentation-{index}",
                        name="create_presentation",
                        arguments=call_arguments,
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
        )
    scripts.append([_completed("PPT 已生成并交付。", message_id="final")])
    client = ScriptedModelClient(scripts)
    journal = RuntimeEffectJournal(tmp_path / "effects")
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        effect_journal=journal,
    )
    project = tmp_path / "workspace"
    state = tmp_path / "state"
    project.mkdir()
    state.mkdir()
    context = WorkspaceContext(
        agent_home=tmp_path / "agent-home",
        state_root=state,
        project_root=project,
        workspace_grant_id="conversation-preflight",
        trust_state="trusted",
    )
    runtime_input = _runtime_input()
    runtime_input.request.workspace_dir = str(project)
    set_current_workspace_context(context)
    try:
        events = [event async for event in runtime.run_turn(runtime_input)]
    finally:
        set_current_workspace_context(None)

    completed = [
        event for event in events if event.type == "tool.call.completed"
    ]
    assert len(completed) == 2
    assert (
        completed[0].payload["metadata"]["side_effect_outcome"]
        == "known_not_applied"
    )
    assert completed[0].payload["metadata"]["automatic_retry_allowed"] is True
    assert completed[0].payload["metadata"]["requires_human"] is False
    # Keep the deterministic-failure circuit breaker: identical bad arguments
    # are skipped, while corrected arguments can continue in the same turn.
    skipped = client.requests[2].messages[-1].metadata
    assert skipped["skipped"] is True
    assert "change arguments or strategy" in skipped["reason"]
    assert "ambiguous external effect" not in skipped["reason"]
    assert (
        completed[1].payload["metadata"]["side_effect_outcome"] == "committed"
    )
    assert len(Presentation(project / "poem.pptx").slides) == 1
    records = journal.list_effects("turn-1", "turn-1:native-attempt")
    assert [record.status for record in records] == [
        "intent",
        "running",
        "failed",
        "intent",
        "running",
        "committed",
    ]


@pytest.mark.asyncio
async def test_native_runtime_zero_timeout_keeps_effect_known_not_applied(
    tmp_path,
) -> None:
    calls = 0

    async def external_write() -> str:
        nonlocal calls
        calls += 1
        return "written"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="external_write",
            effect_recovery=RuntimeEffectRecoveryCapability(
                external_side_effect=True,
                idempotency="not_idempotent",
                probe="none",
                compensation="none",
                on_ambiguity="manual_disambiguation",
            ),
        ),
        external_write,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-not-started",
                        name="external_write",
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("未执行。", message_id="msg-final")],
        ],
    )
    journal = RuntimeEffectJournal(tmp_path)
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        tool_timeout_seconds=0,
        effect_journal=journal,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert calls == 0
    completed = next(
        event for event in events if event.type == "tool.call.completed"
    )
    assert completed.payload["metadata"]["invocation_status"] == "not_started"
    assert (
        completed.payload["metadata"]["side_effect_outcome"]
        == "known_not_applied"
    )
    assert completed.payload["metadata"]["automatic_retry_allowed"] is True
    records = journal.list_effects("turn-1", "turn-1:native-attempt")
    assert [record.status for record in records] == ["intent", "cancelled"]


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_allows_declared_idempotent_retry_after_unknown_timeout(  # noqa: E501
    tmp_path,
) -> None:
    calls = 0

    async def slow_upsert(record_key: str) -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(1)
        return record_key

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="slow_upsert",
            effect_recovery=RuntimeEffectRecoveryCapability(
                external_side_effect=True,
                idempotency="natural_key",
                probe="none",
                compensation="not_required",
                on_ambiguity="safe_to_retry",
            ),
        ),
        slow_upsert,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-upsert-1",
                        name="slow_upsert",
                        arguments={"record_key": "record-1"},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-upsert-2",
                        name="slow_upsert",
                        arguments={"record_key": "record-1"},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("done", message_id="msg-upsert-final")],
        ],
    )
    journal = RuntimeEffectJournal(tmp_path)
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        tool_timeout_seconds=0.01,
        effect_journal=journal,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert calls == 2
    completed = [
        event for event in events if event.type == "tool.call.completed"
    ]
    assert len(completed) == 2
    assert all(
        event.payload["metadata"]["side_effect_outcome"] == "unknown"
        for event in completed
    )
    assert all(
        event.payload["metadata"]["automatic_retry_allowed"] is True
        for event in completed
    )
    records = journal.list_effects("turn-1", "turn-1:native-attempt")
    assert [record.status for record in records] == [
        "intent",
        "running",
        "ambiguous",
        "safe_to_retry",
        "intent",
        "running",
        "ambiguous",
        "safe_to_retry",
    ]


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_executes_tool_call_from_final_response_block() -> None:  # noqa: E501
    async def lookup(query: str) -> str:
        return f"found:{query}"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="lookup",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        ),
        lookup,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(kind="message_start", message_id="msg-1"),
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-1",
                    response=ModelResponse(
                        message=ModelMessage(
                            id="msg-1",
                            role="assistant",
                            content=[
                                ModelContentBlock(
                                    type="tool_call",
                                    data={
                                        "id": "call-1",
                                        "name": "lookup",
                                        "arguments": {"query": "澜台"},
                                    },
                                ),
                            ],
                        ),
                    ),
                ),
            ],
            [
                ModelStreamEvent(kind="message_start", message_id="msg-2"),
                ModelStreamEvent(
                    kind="text_delta",
                    message_id="msg-2",
                    delta="好了",
                ),
                _completed("好了", message_id="msg-2"),
            ],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert [event.type for event in events].count("tool.call.completed") == 1
    assert events[-1].type == "turn.completed"
    assistant_tool_call = client.requests[1].messages[-2]
    assert [block.type for block in assistant_tool_call.content] == [
        "tool_call",
    ]
    assert assistant_tool_call.content[0].data == {
        "id": "call-1",
        "name": "lookup",
        "arguments": {"query": "澜台"},
    }
    assert client.requests[1].messages[-1].tool_call_id == "call-1"


@pytest.mark.parametrize("bad_arguments", ["not-json", ["query"], None])
@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_rejects_non_object_tool_arguments_before_execution(  # noqa: E501
    bad_arguments: Any,
) -> None:
    executed: list[bool] = []

    def dangerous_default() -> str:
        executed.append(True)
        return "ran with defaults"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(name="dangerous_default"),
        dangerous_default,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="message_completed",
                    message_id="msg-1",
                    response=ModelResponse(
                        message=ModelMessage(
                            id="msg-1",
                            role="assistant",
                            content=[
                                ModelContentBlock(
                                    type="tool_call",
                                    data={
                                        "id": "call-bad",
                                        "name": "dangerous_default",
                                        "arguments": bad_arguments,
                                    },
                                ),
                            ],
                        ),
                    ),
                ),
            ],
            [_completed("should-not-run", message_id="msg-late")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = []
    with pytest.raises(
        RuntimeError,
        match="tool call dangerous_default arguments",
    ):
        async for event in runtime.run_turn(_runtime_input()):
            events.append(event)

    # Exact [] assertion also rejects None and other false values.
    # pylint: disable-next=use-implicit-booleaness-not-comparison
    assert executed == []
    assert "tool.call.started" not in [event.type for event in events]
    assert events[-1].type == "turn.failed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_prefers_final_tool_arguments_for_same_call_id() -> None:  # noqa: E501
    executed: list[str] = []

    def lookup(query: str) -> str:
        executed.append(query)
        return f"found:{query}"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="lookup",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        ),
        lookup,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-search",
                        name="lookup",
                        arguments={},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(
                            role="assistant",
                            content=[
                                ModelContentBlock(
                                    type="tool_call",
                                    data={
                                        "id": "call-search",
                                        "name": "lookup",
                                        "arguments": {"query": "灵犀专业版"},
                                    },
                                ),
                            ],
                        ),
                    ),
                ),
            ],
            [_completed("done", message_id="msg-after-search")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert executed == ["灵犀专业版"]
    assert [event.type for event in events].count("tool.call.started") == 1
    assert [event.type for event in events].count("tool.call.completed") == 1
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_replaces_partial_argument_sentinel_with_final_call() -> None:  # noqa: E501
    executed: list[str] = []

    def lookup(query: str) -> str:
        executed.append(query)
        return f"found:{query}"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="lookup",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        ),
        lookup,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-partial-search",
                        name="lookup",
                        arguments={"_partial": '{"query":'},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(
                            role="assistant",
                            content=[
                                ModelContentBlock(
                                    type="tool_call",
                                    data={
                                        "id": "call-partial-search",
                                        "name": "lookup",
                                        "arguments": {"query": "灵犀专业版"},
                                    },
                                ),
                            ],
                        ),
                    ),
                ),
            ],
            [_completed("done", message_id="msg-after-partial-search")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert executed == ["灵犀专业版"]
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_repairs_missing_required_tool_arguments_before_start() -> None:  # noqa: E501
    executed: list[tuple[str, str]] = []

    def write_file(file_path: str, content: str) -> str:
        executed.append((file_path, content))
        return f"written:{file_path}"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="write_file",
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["file_path", "content"],
            },
        ),
        write_file,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-missing",
                        name="write_file",
                        arguments={"content": "大纲"},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-repaired",
                        name="write_file",
                        arguments={"file_path": "大纲.md", "content": "大纲"},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("已经写好大纲。", message_id="msg-after-repair")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert executed == [("大纲.md", "大纲")]
    assert events[-1].type == "turn.completed"
    assert "Native runtime model protocol error" not in _runtime_event_text(
        events,
    )
    assert "missing required tool argument" not in _runtime_event_text(events)
    assert len(client.requests) == 3
    repair_request = client.requests[1]
    assert repair_request.messages[-2].role == "assistant"
    assert repair_request.messages[-1].role == "tool"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_repairs_nested_tool_schema_before_execution() -> None:  # noqa: E501
    executed: list[list[dict[str, Any]]] = []

    def create_presentation(
        title: str,
        slides: list[dict[str, Any]],
    ) -> str:
        del title
        executed.append(slides)
        return "created"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="create_presentation",
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "slides": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "layout_id": {"enum": ["cover"]},
                                "objects": {"type": "array"},
                            },
                            "required": ["layout_id", "objects"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["title", "slides"],
                "additionalProperties": False,
            },
        ),
        create_presentation,
    )
    valid_slides = [{"layout_id": "cover", "objects": []}]
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-invalid-presentation",
                        name="create_presentation",
                        arguments={
                            "title": "季度复盘",
                            "slides": [{"layout_id": "cover"}],
                        },
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-valid-presentation",
                        name="create_presentation",
                        arguments={"title": "季度复盘", "slides": valid_slides},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("演示文稿已创建。", message_id="msg-presentation")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert executed == [valid_slides]
    assert events[-1].type == "turn.completed"
    assert len(client.requests) == 3
    repair_message = client.requests[1].messages[-1]
    repair_text = repair_message.content[0].data["output"][0]["text"]
    assert "arguments.slides[0]" in repair_text
    assert "objects" in repair_text


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_repairs_all_presentation_pages_from_compact_feedback() -> None:  # noqa: E501
    from lantai.agents.tools.presentation import (
        CREATE_PRESENTATION_INPUT_SCHEMA,
    )

    invalid = {
        "title": "Synthetic slide argument repair",
        "slides": [
            {
                "layout_id": "freeform",
                "objects": [
                    {
                        "kind": "shape",
                        "shape_type": "rect",
                        "slot": "body",
                        "frame": {"x": 1, "y": 1, "w": 6, "h": 2},
                        "text": (
                            "Large authored content not needed in feedback. "
                        )
                        * 100,
                        "font_size": 12,
                    },
                ],
            }
            for _ in range(9)
        ],
    }
    corrected = json.loads(json.dumps(invalid))
    for slide in corrected["slides"]:
        slide["objects"][0]["font_size"] = 20
    executed = []

    def create_presentation(**arguments):
        executed.append(arguments)
        return "created"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="create_presentation",
            input_schema=CREATE_PRESENTATION_INPUT_SCHEMA,
        ),
        create_presentation,
    )

    def tool_response(call_id, arguments):
        return [
            ModelStreamEvent(
                kind="tool_call_completed",
                tool_call=ToolCall(
                    id=call_id,
                    name="create_presentation",
                    arguments=arguments,
                ),
            ),
            ModelStreamEvent(
                kind="message_completed",
                response=ModelResponse(message=ModelMessage(role="assistant")),
            ),
        ]

    client = ScriptedModelClient(
        [
            tool_response("bad-slide-fonts", invalid),
            tool_response("correct-slide-fonts", corrected),
            [_completed("Created.", message_id="msg-corrected-slide-fonts")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )
    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert events[-1].type == "turn.completed"
    assert executed == [corrected]
    assert len(client.requests) == 3
    feedback = (
        client.requests[1].messages[-1].content[0].data["output"][0]["text"]
    )
    for index in range(9):
        assert f"arguments.slides[{index}].objects[0].font_size" in feedback
    assert "Large authored content" not in feedback
    assert len(feedback) < 6000
    assert [event.type for event in events].count("tool.call.started") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("path_key", ["path", "filePath", "filepath"])
# Keep the existing test selector unchanged.
async def test_native_runtime_cross_host_cold_start_normalizes_read_file_path_alias(  # noqa: E501
    tmp_path,
    path_key: str,
) -> None:
    """Provider argument spelling must not depend on host-local state."""

    executed: list[str] = []

    def read_file(file_path: str) -> str:
        executed.append(file_path)
        return f"read:{file_path}"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="read_file",
            input_schema={
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        ),
        read_file,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-read-alias",
                        name="read_file",
                        arguments={path_key: "brief.docx"},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("文档已经读完。", message_id="msg-after-read")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )
    request = _runtime_input()
    request.request.workspace_dir = str(tmp_path)

    events = [event async for event in runtime.run_turn(request)]

    assert executed == ["brief.docx"]
    assert events[-1].type == "turn.completed"
    assert len(client.requests) == 2
    assert "missing required tool argument" not in _runtime_event_text(events)


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_repairs_missing_read_file_path_in_same_turn() -> None:  # noqa: E501
    executed: list[str] = []

    def read_file(file_path: str) -> str:
        executed.append(file_path)
        return f"read:{file_path}"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="read_file",
            input_schema={
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        ),
        read_file,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-read-missing",
                        name="read_file",
                        arguments={},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-read-repaired",
                        name="read_file",
                        arguments={"file_path": "brief.docx"},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("文档已经读完。", message_id="msg-after-read-repair")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert executed == ["brief.docx"]
    assert events[-1].type == "turn.completed"
    assert len(client.requests) == 3
    assert "missing required tool argument" not in _runtime_event_text(events)


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_stops_malformed_tool_argument_repair_without_exposure() -> None:  # noqa: E501
    executed: list[str] = []

    def write_file(file_path: str) -> str:
        executed.append(file_path)
        return file_path

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="write_file",
            input_schema={
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        ),
        write_file,
    )

    malformed_round = [
        ModelStreamEvent(
            kind="tool_call_completed",
            tool_call=ToolCall(
                id="call-missing",
                name="write_file",
                arguments={},
            ),
        ),
        ModelStreamEvent(
            kind="message_completed",
            response=ModelResponse(message=ModelMessage(role="assistant")),
        ),
    ]
    client = ScriptedModelClient(
        [
            malformed_round,
            malformed_round,
            malformed_round,
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = []
    with pytest.raises(RuntimeError):
        async for event in runtime.run_turn(_runtime_input()):
            events.append(event)

    # Exact [] assertion also rejects None and other false values.
    # pylint: disable-next=use-implicit-booleaness-not-comparison
    assert executed == []
    assert events[-1].type == "turn.failed"
    assert events[-1].summary == "模型连续未能给出完整操作信息，本轮已停止自动重试。"
    assert events[-1].payload["error_code"] == "tool_argument_repair_exhausted"
    assert "Native runtime model protocol error" not in _runtime_event_text(
        events,
    )
    assert "missing required tool argument" not in _runtime_event_text(events)


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_rejects_entire_batch_before_any_side_effect() -> None:  # noqa: E501
    executed: list[str] = []

    def write_file(file_path: str) -> str:
        executed.append(file_path)
        return file_path

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="write_file",
            input_schema={
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        ),
        write_file,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(
                            role="assistant",
                            content=[
                                ModelContentBlock(
                                    type="tool_call",
                                    data={
                                        "id": "call-valid",
                                        "name": "write_file",
                                        "arguments": {
                                            "file_path": "must-not-exist.txt",
                                        },
                                    },
                                ),
                                ModelContentBlock(
                                    type="tool_call",
                                    data={
                                        "id": "call-invalid",
                                        "name": "write_file",
                                        "arguments": {},
                                    },
                                ),
                            ],
                        ),
                    ),
                ),
            ],
            [_completed("我没有执行不完整的批次。", message_id="msg-after-batch")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    # Exact [] assertion also rejects None and other false values.
    # pylint: disable-next=use-implicit-booleaness-not-comparison
    assert executed == []
    assert "tool.call.started" not in [event.type for event in events]
    assert events[-1].type == "turn.completed"
    repair_messages = client.requests[1].messages[-3:]
    assert [message.role for message in repair_messages] == [
        "assistant",
        "tool",
        "tool",
    ]


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_events_do_not_expose_request_args_or_raw_tool_result(  # noqa: E501
    tmp_path: Path,
) -> None:
    secret_token = "sk-live-secret"
    secret_path = str(tmp_path / "private.txt")

    async def leaky_tool(token: str, path: str) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": "ok"}],
            "debug": {"token": token, "path": path},
        }

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="leaky_tool",
            input_schema={
                "type": "object",
                "properties": {
                    "token": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["token", "path"],
            },
        ),
        leaky_tool,
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-leaky",
                        name="leaky_tool",
                        arguments={"token": secret_token, "path": secret_path},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("done", message_id="msg-2")],
        ],
    )
    runtime_input = RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id="session-1",
            user_id="user-1",
            run_id="turn-1",
            workspace_dir=secret_path,
            metadata={"api_key": secret_token},
        ),
        messages=[RuntimeMessage.text("user", "hello")],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(runtime_input)]
    event_text = _runtime_event_text(events)
    turn_started = events[0]
    tool_started = next(
        event for event in events if event.type == "tool.call.started"
    )
    tool_completed = next(
        event for event in events if event.type == "tool.call.completed"
    )

    assert secret_token not in event_text
    assert secret_path not in event_text
    assert turn_started.payload == {
        "engine": "native",
        "agent_id": "default",
        "session_id": "session-1",
        "run_id": "turn-1",
    }
    assert tool_started.payload["argument_keys"] == ["path", "token"]
    assert tool_started.payload["arguments_redacted"] is True
    assert "arguments" not in tool_started.payload
    assert "raw" not in tool_completed.payload["metadata"]


@pytest.mark.asyncio
async def test_native_runtime_projects_and_executes_mcp_tools() -> None:
    class FakeMcpClient:
        name = "demo-mcp"
        read_timeout_seconds = 1.0

        async def list_tools(self):
            return [
                SimpleNamespace(
                    name="mcp_echo",
                    description="Echo over MCP",
                    inputSchema={
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                    },
                ),
            ]

        async def call_tool(self, name: str, arguments: dict[str, Any]):
            return SimpleNamespace(
                content=[
                    {"type": "text", "text": f"{name}:{arguments['value']}"},
                ],
            )

    registry = LantaiToolRegistry()
    registry.register_mcp_client(
        SimpleNamespace(
            name="demo-mcp",
            client=FakeMcpClient(),
            execution_timeout=1.0,
            enabled=True,
            metadata={},
        ),
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-1",
                        name="mcp_echo",
                        arguments={"value": "ok"},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("done", message_id="msg-2")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert client.requests[0].tools[0].name == "mcp_echo"
    completed = [
        event for event in events if event.type == "tool.call.completed"
    ][0]
    assert completed.payload["output"][0]["text"] == "mcp_echo:ok"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_blocked_name_cannot_reenter_through_mcp() -> None:  # noqa: E501
    calls = []

    class FakeMcpClient:
        name = "blocked-mcp"
        read_timeout_seconds = 1.0

        async def list_tools(self):
            return [
                SimpleNamespace(
                    name="edit_file",
                    description="Conflicting implementation tool",
                    inputSchema={"type": "object", "properties": {}},
                ),
            ]

        async def call_tool(self, name: str, arguments: dict[str, Any]):
            calls.append((name, arguments))
            return SimpleNamespace(
                content=[{"type": "text", "text": "unsafe"}],
            )

    registry = LantaiToolRegistry()
    registry.block_tools({"edit_file"})
    registry.register_mcp_client(
        SimpleNamespace(
            name="blocked-mcp",
            client=FakeMcpClient(),
            execution_timeout=1.0,
            enabled=True,
            metadata={},
        ),
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(id="call-blocked", name="edit_file"),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("done", message_id="msg-blocked")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert client.requests[0].tools == []
    # Exact [] assertion also rejects None and other false values.
    # pylint: disable-next=use-implicit-booleaness-not-comparison
    assert calls == []
    completed = [
        event for event in events if event.type == "tool.call.completed"
    ]
    assert completed[0].status == "failed"
    assert completed[0].payload["error"] == "Tool not found: edit_file"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_preserves_official_guanlan_policy_provenance() -> None:  # noqa: E501
    from lantai.runtime.tool_policy import RuntimeToolPolicyMiddleware

    calls = []

    class FakeGuanlanClient:
        name = "观澜 / Guanlan"
        read_timeout_seconds = 300.0

        async def list_tools(self):
            return [
                SimpleNamespace(
                    name="guanlan_search",
                    description="Search public sources",
                    inputSchema={
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                    },
                ),
            ]

        async def call_tool(self, name: str, arguments: dict[str, Any]):
            calls.append((name, arguments))
            return SimpleNamespace(
                content=[{"type": "text", "text": "evidence"}],
            )

    registry = LantaiToolRegistry()
    registry.register_policy_middleware(RuntimeToolPolicyMiddleware())
    registry.register_mcp_client(
        SimpleNamespace(
            name="观澜 / Guanlan",
            client=FakeGuanlanClient(),
            execution_timeout=300.0,
            enabled=True,
            metadata={"official_integration": "guanlan"},
        ),
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-guanlan",
                        name="guanlan_search",
                        arguments={"query": "最近政策"},
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("done", message_id="msg-guanlan")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert calls == [("guanlan_search", {"query": "最近政策"})]
    completed = [
        event for event in events if event.type == "tool.call.completed"
    ]
    assert completed[0].status == "completed"


@pytest.mark.asyncio
async def test_native_runtime_mcp_tool_timeout_has_visible_error() -> None:
    class SlowMcpClient:
        name = "slow-mcp"
        read_timeout_seconds = 1.0

        async def list_tools(self):
            return [
                SimpleNamespace(
                    name="mcp_slow",
                    description="Slow MCP tool",
                    inputSchema={"type": "object", "properties": {}},
                ),
            ]

        async def call_tool(self, name: str, arguments: dict[str, Any]):
            del name, arguments
            await asyncio.sleep(1)
            return SimpleNamespace(content=[{"type": "text", "text": "late"}])

    registry = LantaiToolRegistry()
    registry.register_mcp_client(
        SimpleNamespace(
            name="slow-mcp",
            client=SlowMcpClient(),
            execution_timeout=0.01,
            enabled=True,
            metadata={},
        ),
    )
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(id="call-slow", name="mcp_slow"),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("handled", message_id="msg-2")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    completed = [
        event for event in events if event.type == "tool.call.completed"
    ][0]
    assert completed.status == "failed"
    assert completed.summary == "Tool call timed out."
    assert completed.payload["error"] == "Tool call timed out."
    assert completed.payload["output"][0]["text"] == "Tool call timed out."
    assert completed.payload["metadata"]["error_type"] == "TimeoutError"
    tool_message = next(
        message
        for message in reversed(client.requests[1].messages)
        if message.role == "tool"
    )
    assert (
        tool_message.content[0].data["output"][0]["text"]
        == "Tool call timed out."
    )


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_mcp_discovery_failure_is_visible_warning() -> None:  # noqa: E501
    class BrokenMcpClient:
        name = "broken-mcp"
        read_timeout_seconds = 1.0

        async def list_tools(self):
            raise asyncio.TimeoutError()

    registry = LantaiToolRegistry()
    registry.register_mcp_client(
        SimpleNamespace(
            name="broken-mcp",
            client=BrokenMcpClient(),
            execution_timeout=1.0,
            enabled=True,
            metadata={},
        ),
    )
    client = ScriptedModelClient([[_completed("done")]])
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    warning = next(
        event for event in events if event.type == "runtime.warning"
    )
    assert warning.status == "failed"
    assert warning.payload["kind"] == "mcp_tool_discovery_failed"
    assert warning.payload["client"] == "broken-mcp"
    assert (
        warning.payload["error"]
        == "MCP tool discovery for broken-mcp timed out."
    )
    assert warning.payload["error_type"] == "TimeoutError"
    assert client.requests[0].tools == []
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
async def test_native_runtime_empty_tool_exception_has_visible_error() -> None:
    class EmptyToolError(Exception):
        pass

    def fail_empty() -> str:
        raise EmptyToolError()

    registry = LantaiToolRegistry()
    registry.register_tool(ToolSpec(name="empty_error"), fail_empty)
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(id="call-empty", name="empty_error"),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("handled", message_id="msg-2")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    completed = [
        event for event in events if event.type == "tool.call.completed"
    ][0]
    assert completed.status == "failed"
    assert completed.payload["error"] == "Tool call: EmptyToolError"
    assert (
        completed.payload["output"][0]["text"] == "Tool call: EmptyToolError"
    )
    assert completed.summary == "Tool call: EmptyToolError"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_sync_callable_cancellation_does_not_wait_for_return() -> None:  # noqa: E501
    started = threading.Event()
    release = threading.Event()

    def slow_sync_tool() -> str:
        started.set()
        release.wait(timeout=1)
        return "late result"

    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient([]),
        tool_registry=LantaiToolRegistry(),
    )
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    task = asyncio.create_task(runtime._execute_callable(slow_sync_tool, {}))
    await asyncio.sleep(0.05)
    assert started.is_set()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_sync_callable_preserves_context_snapshot() -> None:  # noqa: E501
    request_id_var = contextvars.ContextVar(
        "native_request_id",
        default="missing",
    )

    def sync_tool() -> str:
        return request_id_var.get()

    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient([]),
        tool_registry=LantaiToolRegistry(),
    )
    token = request_id_var.set("turn-ctx")
    try:
        # White-box lock exercises the actual private boundary.
        # pylint: disable-next=protected-access
        result = await runtime._execute_callable(sync_tool, {})
    finally:
        request_id_var.reset(token)

    assert result == "turn-ctx"


@pytest.mark.asyncio
async def test_native_runtime_denies_policy_blocked_tool_call() -> None:
    class DenyPolicy:
        async def evaluate(self, tool_call, spec, context):
            del tool_call, spec, context
            return ToolPolicyOutcome(action="deny", reason="blocked")

    registry = LantaiToolRegistry()
    registry.register_tool(ToolSpec(name="danger"), lambda: "nope")
    registry.register_policy_middleware(DenyPolicy())
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(id="call-1", name="danger"),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("handled", message_id="msg-2")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        tool_policy_context=ToolPolicyContext(session_id="session-1"),
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    failed = [
        event for event in events if event.type == "tool.call.completed"
    ][0]
    assert failed.status == "failed"
    assert failed.payload["error"] == "blocked"
    assert (
        client.requests[1].messages[-1].content[0].data["output"][0]["text"]
        == "blocked"
    )


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_resume_revalidates_pending_tool_permission_without_reusing_stale_grant(  # noqa: E501
    tmp_path: Path,
) -> None:
    state_workspace = tmp_path / "state"
    project_workspace = tmp_path / "project"
    project_workspace.mkdir()
    started = await record_chat_runtime_run_started(
        workspace_dir=state_workspace,
        agent_id="default",
        chat_id="chat-r11-pending-permission",
        session_id="session-1",
        run_id="turn-r11-pending-permission",
        effective_workspace_dir=str(project_workspace),
    )
    pending_call = ToolCall(
        id="call-r11-pending-write",
        name="dangerous_write",
        arguments={"value": "must-not-run"},
        metadata={
            "approval_request_id": "stale-approval-before-restart",
            "permission_snapshot": "allowed",
        },
    )
    checkpoint_messages = (
        ModelMessage.text("user", "write it", id="user-r11-pending"),
        ModelMessage(
            id="assistant-r11-pending",
            role="assistant",
            content=[
                ModelContentBlock(
                    type="tool_call",
                    data={
                        "id": pending_call.id,
                        "name": pending_call.name,
                        "arguments": dict(pending_call.arguments),
                    },
                ),
            ],
        ),
    )
    checkpoint = RuntimeCheckpoint(
        checkpoint_id="checkpoint-r11-pending",
        seq=7,
        stage="waiting_approval",
        next_execution_position="before_tool",
        pending_tool_call=RuntimePendingToolCall(
            call_id=pending_call.id,
            tool_name=pending_call.name,
            arguments=dict(pending_call.arguments),
            metadata=dict(pending_call.metadata),
        ),
        compressed_context=tuple(
            json.dumps(
                message.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            for message in checkpoint_messages
        ),
        model_rounds=1,
        tool_calls=1,
    )
    checkpointed = reduce_runtime_run(
        started,
        RuntimeRunReducerAction(
            type="heartbeat",
            expected_attempt_id=started.attempt_id,
            expected_revision=started.revision,
            lease_owner=started.lease_owner,
            checkpoint=checkpoint,
        ),
    ).run
    await RuntimeRunStore(state_workspace).save_contract(
        checkpointed,
        expected_revision=started.revision,
        expected_attempt_id=started.attempt_id,
        lease_owner=started.lease_owner,
    )

    executions: list[str] = []
    policy_calls: list[str] = []

    async def dangerous_write(value: str) -> ToolResponse:
        executions.append(value)
        return ToolResponse(content=[TextBlock(text="written")])

    class RevokedPermissionPolicy:
        async def evaluate(self, tool_call, _spec, _context):
            policy_calls.append(tool_call.id)
            return ToolPolicyOutcome(
                action="deny",
                reason="permission revoked after restart",
            )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="dangerous_write",
            read_only=False,
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        ),
        dangerous_write,
    )
    registry.register_policy_middleware(RevokedPermissionPolicy())
    client = ScriptedModelClient(
        [
            [
                _completed(
                    "Permission was revoked; nothing was written.",
                    message_id="r11-done",
                ),
            ],
        ],
    )
    runtime_input = RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id="session-1",
            user_id="user-1",
            run_id=started.run_id,
            workspace_dir=str(project_workspace),
        ),
        messages=[
            RuntimeMessage.text("user", "write it", id="user-r11-pending"),
        ],
        metadata={
            "state_workspace_dir": str(state_workspace),
            "attempt_id": checkpointed.attempt_id,
            "runtime_revision": checkpointed.revision,
            "runtime_lease_owner": checkpointed.lease_owner,
            "runtime_attempt_reason": "resume",
            "runtime_checkpoint": checkpoint.model_dump(mode="json"),
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        tool_policy_context=ToolPolicyContext(session_id="session-1"),
        progressive_tools=False,
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    # Exact [] assertion also rejects None and other false values.
    # pylint: disable-next=use-implicit-booleaness-not-comparison
    assert executions == []
    assert policy_calls == [pending_call.id]
    assert len(client.requests) == 1
    completed = [
        event for event in events if event.type == "tool.call.completed"
    ]
    assert len(completed) == 1
    assert completed[0].status == "failed"
    assert completed[0].payload["id"] == pending_call.id
    assert completed[0].payload["error"] == "permission revoked after restart"
    assert not [
        event for event in events if event.type == "approval.requested"
    ]
    provider_tool_results = [
        message
        for message in client.requests[0].messages
        if message.role == "tool" and message.tool_call_id == pending_call.id
    ]
    assert len(provider_tool_results) == 1
    persisted = await RuntimeRunStore(state_workspace).load_contract(
        started.run_id,
    )
    assert persisted is not None
    assert persisted.checkpoint is not None
    assert persisted.checkpoint.next_execution_position == "terminal"
    assert persisted.checkpoint.pending_tool_call is None


async def _run_with_policy_observation(runtime, runtime_input):
    token = runtime.request_policy_observer.begin(runtime_input)
    assert token is not None
    runner = RuntimeRunner(
        runtime,
        diagnostic_run_turn=lambda observed_input, invocation: (
            runtime.run_turn(
                observed_input,
                request_policy_observation_invocation=invocation,
            )
        ),
        request_policy_observation_token=token,
    )
    invocation = runner.request_policy_observation_invocation
    assert invocation is not None
    events = [event async for event in runner.run_turn(runtime_input)]
    terminal = events[-1]
    effective = runtime.request_policy_observer.take(
        invocation,
        terminal_type=terminal.type,
        terminal_turn_id=terminal.turn_id or "",
    )
    dispatch = runtime.request_policy_observer.take_dispatch(
        invocation,
        terminal_type=terminal.type,
        terminal_turn_id=terminal.turn_id or "",
    )
    return events, effective, dispatch, invocation


@pytest.mark.asyncio
async def test_policy_dispatch_receipt_covers_each_middleware_copy() -> None:
    seen_contexts: list[ToolPolicyContext] = []

    class RecordingPolicy:
        async def evaluate(self, _tool_call, _spec, context):
            assert "private_mutation" not in context.request_context
            seen_contexts.append(context)
            context.request_context["private_mutation"] = len(seen_contexts)
            return ToolPolicyOutcome(action="allow")

    registry = LantaiToolRegistry()
    registry.register_tool(ToolSpec(name="safe_tool"), lambda: "ok")
    registry.register_policy_middleware(RecordingPolicy())
    registry.register_policy_middleware(RecordingPolicy())
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(id="call-dispatch", name="safe_tool"),
                ),
            ],
            [_completed("done", message_id="msg-dispatch")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    (
        events,
        effective,
        dispatch,
        invocation,
    ) = await _run_with_policy_observation(
        runtime,
        _runtime_input(),
    )

    assert events[-1].type == "turn.completed"
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    state = invocation._token._state
    assert effective is not None and dispatch is not None, (
        state.status,
        state.dispatch_invalid,
        state.dispatch_unavailable,
        len(state.evaluations),
        [item.completed for item in state.evaluations],
    )
    assert (
        dispatch.effective_payload_sha256 == effective.effective_payload_sha256
    )
    assert dispatch.invocation_sha256 == effective.invocation_sha256
    assert dispatch.evaluation_count == 1
    assert dispatch.middleware_dispatch_count == 2
    assert dispatch.all_contexts_matched is True
    assert len({id(context) for context in seen_contexts}) == 2
    assert "call-dispatch" not in repr(dispatch)


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_policy_dispatch_receipt_counts_multiple_tool_evaluations() -> None:  # noqa: E501
    registry = LantaiToolRegistry()
    registry.register_tool(ToolSpec(name="first_tool"), lambda: "first")
    registry.register_tool(ToolSpec(name="second_tool"), lambda: "second")

    class AllowPolicy:
        async def evaluate(self, _tool_call, _spec, _context):
            return ToolPolicyOutcome(action="allow")

    registry.register_policy_middleware(AllowPolicy())
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(id="call-first", name="first_tool"),
                ),
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(id="call-second", name="second_tool"),
                ),
            ],
            [_completed("done", message_id="msg-two-tools")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    (
        _events,
        _effective,
        dispatch,
        _invocation,
    ) = await _run_with_policy_observation(
        runtime,
        _runtime_input(),
    )

    assert dispatch is not None
    assert dispatch.evaluation_count == 2
    assert dispatch.middleware_dispatch_count == 2


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_policy_dispatch_receipt_handles_zero_middleware_and_no_tool() -> None:  # noqa: E501
    tool_registry = LantaiToolRegistry()
    tool_registry.register_tool(ToolSpec(name="safe_tool"), lambda: "ok")
    tool_runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient(
            [
                [
                    ModelStreamEvent(
                        kind="tool_call_completed",
                        tool_call=ToolCall(id="call-zero", name="safe_tool"),
                    ),
                ],
                [_completed("done")],
            ],
        ),
        tool_registry=tool_registry,
    )
    no_tool_runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient(
            [[_completed("answer only")]],
        ),
        tool_registry=LantaiToolRegistry(),
    )

    (
        zero_events,
        _effective,
        zero_dispatch,
        zero_invocation,
    ) = await _run_with_policy_observation(tool_runtime, _runtime_input())
    (
        no_tool_events,
        _effective,
        no_dispatch,
        no_tool_invocation,
    ) = await _run_with_policy_observation(no_tool_runtime, _runtime_input())

    assert zero_dispatch is not None
    assert zero_dispatch.evaluation_count == 1
    assert zero_dispatch.middleware_dispatch_count == 0
    assert no_dispatch is None
    assert (
        tool_runtime.request_policy_observer.take_consumption(
            zero_invocation,
            terminal_type=zero_events[-1].type,
            terminal_turn_id=zero_events[-1].turn_id or "",
        )
        is None
    )
    assert (
        no_tool_runtime.request_policy_observer.take_consumption(
            no_tool_invocation,
            terminal_type=no_tool_events[-1].type,
            terminal_turn_id=no_tool_events[-1].turn_id or "",
        )
        is None
    )


@pytest.mark.asyncio
async def test_live_permission_and_guard_consumption_receipt_tracks_override(
    monkeypatch,
) -> None:
    from lantai.app.approvals.service import ApprovalService
    from lantai.runtime.tool_policy import RuntimeToolPolicyMiddleware

    service = ApprovalService()
    await service.set_session_permission_override(
        root_session_id="session-1",
        user_id="user-1",
        permission_mode="full_auto",
        permission_profile="full",
    )
    middleware = RuntimeToolPolicyMiddleware(service)
    consumed_contexts: list[tuple[str, str | None, str]] = []
    policy_payload = {
        "decision": "prompt",
        "reason": "Approval required.",
        "risk": "medium",
    }

    def builtin(_name, _input, context):
        consumed_contexts.append(
            ("builtin", context.permission_mode, context.permission_profile),
        )
        return SimpleNamespace(
            **policy_payload,
            model_dump=lambda mode=None: dict(policy_payload),
        )

    def guard(_name, _input, context):
        consumed_contexts.append(
            ("guard", context.permission_mode, context.permission_profile),
        )
        return ToolPolicyOutcome(
            action="needs_approval",
            reason="guard prompt",
            metadata={"source": "tool_guard"},
        )

    monkeypatch.setattr(middleware, "_evaluate_builtin_policy", builtin)
    monkeypatch.setattr(middleware, "_evaluate_tool_guard", guard)
    registry = LantaiToolRegistry()
    registry.register_tool(ToolSpec(name="safe_tool"), lambda: "ok")
    registry.register_policy_middleware(middleware)
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient(
            [
                [
                    ModelStreamEvent(
                        kind="tool_call_completed",
                        tool_call=ToolCall(id="call-live", name="safe_tool"),
                    ),
                ],
                [_completed("done")],
            ],
        ),
        tool_registry=registry,
        approval_service=service,
        tool_policy_context=ToolPolicyContext(
            permission_mode="default",
            permission_profile="workspace",
            request_context={"permission_snapshot_at": 0.0},
        ),
    )

    (
        events,
        effective,
        dispatch,
        invocation,
    ) = await _run_with_policy_observation(
        runtime,
        _runtime_input(),
    )
    consumption = runtime.request_policy_observer.take_consumption(
        invocation,
        terminal_type=events[-1].type,
        terminal_turn_id=events[-1].turn_id or "",
    )

    assert events[-1].type == "turn.completed"
    assert effective is not None and dispatch is not None
    assert consumed_contexts == [
        ("builtin", "full_auto", "full"),
        ("guard", "full_auto", "full"),
    ]
    assert consumption is not None
    assert (
        consumption.dispatch_evaluations_sha256 == dispatch.evaluations_sha256
    )
    assert consumption.default_middleware_dispatch_count == 1
    assert consumption.consumption_count == 1
    assert consumption.live_permission_evaluation_count == 1
    assert consumption.live_override_applied_count == 1
    assert consumption.guard_consumption_count == 1
    assert consumption.all_context_chains_valid is True
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    state = invocation._token._state
    assert [item.override_mode for item in state.consumptions] == ["applied"]
    assert [
        (stage, phase)
        for stage, phase, _context_sha256 in state.consumptions[
            0
        ].consumer_rows
    ] == [
        ("runtime_phase", "initial"),
        ("official_chrome", "initial"),
        ("workspace_access", "initial"),
        ("collaboration_read_only", "initial"),
        ("delegation_tool_scope", "initial"),
        ("artifact_delivery", "post_override"),
        ("builtin_policy", "post_override"),
        ("full_access", "post_override"),
        ("tool_guard", "post_override"),
    ]
    assert "call-live" not in repr(consumption)
    assert (
        runtime.request_policy_observer.take_consumption(
            invocation,
            terminal_type=events[-1].type,
            terminal_turn_id=events[-1].turn_id or "",
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("skip_first_consumption", [False, True])
async def test_policy_consumption_counts_each_default_dispatch_across_calls(
    monkeypatch,
    skip_first_consumption: bool,
) -> None:
    from lantai.runtime.tool_policy import RuntimeToolPolicyMiddleware

    middlewares = [
        RuntimeToolPolicyMiddleware(),
        RuntimeToolPolicyMiddleware(),
    ]
    for middleware in middlewares:
        monkeypatch.setattr(
            middleware,
            "_evaluate_builtin_policy",
            lambda *_args: SimpleNamespace(
                decision="allow",
                reason="allowed",
                risk="low",
                model_dump=lambda mode=None: {
                    "decision": "allow",
                    "reason": "allowed",
                    "risk": "low",
                },
            ),
        )
        monkeypatch.setattr(
            middleware,
            "_evaluate_tool_guard",
            lambda *_args: None,
        )
    registry = LantaiToolRegistry()
    registry.register_tool(ToolSpec(name="first_tool"), lambda: "first")
    registry.register_tool(ToolSpec(name="second_tool"), lambda: "second")
    for middleware in middlewares:
        registry.register_policy_middleware(middleware)
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient(
            [
                [
                    ModelStreamEvent(
                        kind="tool_call_completed",
                        tool_call=ToolCall(id="call-first", name="first_tool"),
                    ),
                    ModelStreamEvent(
                        kind="tool_call_completed",
                        tool_call=ToolCall(
                            id="call-second",
                            name="second_tool",
                        ),
                    ),
                ],
                [_completed("done")],
            ],
        ),
        tool_registry=registry,
    )
    if skip_first_consumption:
        original_start = (
            runtime.request_policy_observer.start_policy_consumption
        )
        start_calls = 0

        def skip_first(*args, **kwargs):
            nonlocal start_calls
            start_calls += 1
            if start_calls == 1:
                return None
            return original_start(*args, **kwargs)

        monkeypatch.setattr(
            runtime.request_policy_observer,
            "start_policy_consumption",
            skip_first,
        )

    (
        events,
        _effective,
        dispatch,
        invocation,
    ) = await _run_with_policy_observation(runtime, _runtime_input())
    consumption = runtime.request_policy_observer.take_consumption(
        invocation,
        terminal_type=events[-1].type,
        terminal_turn_id=events[-1].turn_id or "",
    )

    assert dispatch is not None
    assert dispatch.evaluation_count == 2
    assert dispatch.middleware_dispatch_count == 4
    if skip_first_consumption:
        assert consumption is None
        return
    assert consumption is not None
    assert consumption.default_middleware_dispatch_count == 4
    assert consumption.consumption_count == 4
    assert consumption.live_permission_evaluation_count == 4
    assert consumption.guard_consumption_count == 4
    assert consumption.all_context_chains_valid is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("spec", "arguments", "expected_stages"),
    [
        (
            ToolSpec(
                name="declare_deliverable_contract",
                metadata={"runtime_owned": True},
            ),
            {},
            (
                "runtime_phase",
                "official_chrome",
                "workspace_access",
                "collaboration_read_only",
                "delegation_tool_scope",
                "runtime_owned_artifact",
                "full_access",
                "remembered_approval",
                "tool_guard",
            ),
        ),
        (
            ToolSpec(
                name="guanlan_search",
                origin="mcp",
                metadata={"official_integration": "guanlan"},
            ),
            {"query": "policy"},
            (
                "runtime_phase",
                "official_chrome",
                "workspace_access",
                "official_guanlan",
                "collaboration_read_only",
                "delegation_tool_scope",
                "artifact_delivery",
                "full_access",
                "remembered_approval",
                "tool_guard",
            ),
        ),
    ],
)
async def test_policy_consumption_tracks_internal_and_official_paths(
    monkeypatch,
    spec: ToolSpec,
    arguments: dict[str, Any],
    expected_stages: tuple[str, ...],
) -> None:
    from lantai.runtime.tool_policy import RuntimeToolPolicyMiddleware

    builtin_calls: list[str] = []
    middleware = RuntimeToolPolicyMiddleware()

    def unexpected_builtin(name, *_args):
        builtin_calls.append(name)
        raise AssertionError(
            "internal and official paths must skip builtin policy",
        )

    monkeypatch.setattr(
        middleware,
        "_evaluate_builtin_policy",
        unexpected_builtin,
    )
    monkeypatch.setattr(
        middleware,
        "_evaluate_tool_guard",
        lambda *_args: None,
    )
    registry = LantaiToolRegistry()
    registry.register_tool(spec, lambda **_kwargs: "ok")
    registry.register_policy_middleware(middleware)
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient(
            [
                [
                    ModelStreamEvent(
                        kind="tool_call_completed",
                        tool_call=ToolCall(
                            id="call-branch",
                            name=spec.name,
                            arguments=arguments,
                        ),
                    ),
                ],
                [_completed("done")],
            ],
        ),
        tool_registry=registry,
    )

    (
        events,
        _effective,
        _dispatch,
        invocation,
    ) = await _run_with_policy_observation(runtime, _runtime_input())
    consumption = runtime.request_policy_observer.take_consumption(
        invocation,
        terminal_type=events[-1].type,
        terminal_turn_id=events[-1].turn_id or "",
    )

    # Exact [] assertion also rejects None and other false values.
    # pylint: disable-next=use-implicit-booleaness-not-comparison
    assert builtin_calls == []
    assert consumption is not None
    assert consumption.live_permission_evaluation_count == 1
    assert consumption.guard_consumption_count == 1
    assert (
        tuple(
            stage
            for stage, _phase, _context_sha256 in (
                # White-box lock exercises the actual private boundary.
                # pylint: disable-next=protected-access
                invocation._token._state.consumptions[0].consumer_rows
            )
        )
        == expected_stages
    )


@pytest.mark.asyncio
async def test_policy_prompt_before_guard_remains_unverified() -> None:
    from lantai.runtime.tool_policy import RuntimeToolPolicyMiddleware

    registry = LantaiToolRegistry()
    registry.register_tool(ToolSpec(name="unknown_mutation"), lambda: "never")
    registry.register_policy_middleware(RuntimeToolPolicyMiddleware())
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient(
            [
                [
                    ModelStreamEvent(
                        kind="tool_call_completed",
                        tool_call=ToolCall(
                            id="call-prompt",
                            name="unknown_mutation",
                        ),
                    ),
                ],
                [_completed("handled")],
            ],
        ),
        tool_registry=registry,
    )

    (
        events,
        _effective,
        _dispatch,
        invocation,
    ) = await _run_with_policy_observation(runtime, _runtime_input())
    consumption = runtime.request_policy_observer.take_consumption(
        invocation,
        terminal_type=events[-1].type,
        terminal_turn_id=events[-1].turn_id or "",
    )

    assert consumption is not None
    assert consumption.live_permission_evaluation_count == 1
    assert consumption.guard_consumption_count == 0
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    assert invocation._token._state.consumptions[0].outcome_action in {
        "deny",
        "needs_approval",
    }


@pytest.mark.asyncio
async def test_policy_consumption_receipt_rejects_hostile_counts_without_hooks(
    monkeypatch,
) -> None:
    from lantai.runtime.tool_policy import RuntimeToolPolicyMiddleware

    calls: list[str] = []

    class HostileValue:
        # Hostile conversion must raise rather than return a string.
        # pylint: disable-next=invalid-repr-returned
        def __repr__(self) -> str:
            calls.append("repr")
            raise AssertionError("consumption receipt must not call repr")

        # Hostile conversion must raise rather than return a string.
        # pylint: disable-next=invalid-str-returned
        def __str__(self) -> str:
            calls.append("str")
            raise AssertionError("consumption receipt must not call str")

        def __eq__(self, _other):
            calls.append("eq")
            raise AssertionError("consumption receipt must not call equality")

    middleware = RuntimeToolPolicyMiddleware()
    monkeypatch.setattr(
        middleware,
        "_evaluate_builtin_policy",
        lambda *_args: SimpleNamespace(
            decision="allow",
            reason="allowed",
            risk="low",
            model_dump=lambda mode=None: {
                "decision": "allow",
                "reason": "allowed",
                "risk": "low",
            },
        ),
    )
    monkeypatch.setattr(
        middleware,
        "_evaluate_tool_guard",
        lambda *_args: None,
    )
    registry = LantaiToolRegistry()
    registry.register_tool(ToolSpec(name="safe_tool"), lambda: "ok")
    registry.register_policy_middleware(middleware)
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient(
            [
                [
                    ModelStreamEvent(
                        kind="tool_call_completed",
                        tool_call=ToolCall(
                            id="PRIVATE-CALL",
                            name="safe_tool",
                        ),
                    ),
                ],
                [_completed("done")],
            ],
        ),
        tool_registry=registry,
    )
    (
        events,
        _effective,
        _dispatch,
        invocation,
    ) = await _run_with_policy_observation(runtime, _runtime_input())
    receipt = runtime.request_policy_observer.take_consumption(
        invocation,
        terminal_type=events[-1].type,
        terminal_turn_id=events[-1].turn_id or "",
    )
    assert receipt is not None
    hostile = HostileValue()
    object.__setattr__(receipt, "consumption_count", hostile)
    object.__setattr__(receipt, "live_permission_evaluation_count", hostile)
    object.__setattr__(receipt, "live_override_applied_count", hostile)
    object.__setattr__(receipt, "guard_consumption_count", hostile)
    object.__setattr__(receipt, "all_context_chains_valid", hostile)

    rendered = repr(receipt)

    assert "PRIVATE-CALL" not in rendered
    assert "invalid" in rendered
    assert is_valid_request_policy_consumption_receipt(receipt) is False
    # Exact [] assertion also rejects None and other false values.
    # pylint: disable-next=use-implicit-booleaness-not-comparison
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("service_factory", "expected_mode"),
    [
        (lambda: None, "not_available"),
        (
            lambda: SimpleNamespace(
                get_session_permission_override=(
                    lambda **_kwargs: _async_result(None)
                ),
            ),
            "unchanged",
        ),
        (
            lambda: SimpleNamespace(
                get_session_permission_override=(
                    lambda **_kwargs: _async_failure(RuntimeError("offline"))
                ),
            ),
            "read_failed",
        ),
        (
            lambda: SimpleNamespace(
                get_session_permission_override=(
                    lambda **_kwargs: _async_failure(
                        AssertionError(
                            "missing binding must skip the service",
                        ),
                    )
                ),
            ),
            "missing_binding",
        ),
    ],
)
async def test_policy_consumption_receipt_records_non_applied_live_modes(
    monkeypatch,
    service_factory,
    expected_mode: str,
) -> None:
    from lantai.runtime.tool_policy import RuntimeToolPolicyMiddleware

    middleware = RuntimeToolPolicyMiddleware(service_factory())
    monkeypatch.setattr(
        middleware,
        "_evaluate_builtin_policy",
        lambda *_args: SimpleNamespace(
            decision="allow",
            reason="allowed",
            risk="low",
            model_dump=lambda mode=None: {
                "decision": "allow",
                "reason": "allowed",
                "risk": "low",
            },
        ),
    )
    monkeypatch.setattr(
        middleware,
        "_evaluate_tool_guard",
        lambda *_args: None,
    )
    registry = LantaiToolRegistry()
    registry.register_tool(ToolSpec(name="safe_tool"), lambda: "ok")
    registry.register_policy_middleware(middleware)
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient(
            [
                [
                    ModelStreamEvent(
                        kind="tool_call_completed",
                        tool_call=ToolCall(id="call-mode", name="safe_tool"),
                    ),
                ],
                [_completed("done")],
            ],
        ),
        tool_registry=registry,
    )

    runtime_input = _runtime_input()
    if expected_mode == "missing_binding":
        runtime_input = runtime_input.model_copy(
            update={
                "request": runtime_input.request.model_copy(
                    update={"user_id": ""},
                ),
            },
        )
    (
        events,
        _effective,
        _dispatch,
        invocation,
    ) = await _run_with_policy_observation(runtime, runtime_input)
    consumption = runtime.request_policy_observer.take_consumption(
        invocation,
        terminal_type=events[-1].type,
        terminal_turn_id=events[-1].turn_id or "",
    )

    assert is_valid_request_policy_consumption_receipt(consumption)
    assert consumption is not None
    assert consumption.live_permission_evaluation_count == 1
    assert consumption.live_override_applied_count == 0
    assert consumption.guard_consumption_count == 1
    assert consumption.all_context_chains_valid is True
    assert [
        item.override_mode
        # White-box lock exercises the actual private boundary.
        # pylint: disable-next=protected-access
        for item in invocation._token._state.consumptions
    ] == [expected_mode]


async def _async_result(value):
    return value


async def _async_failure(failure: BaseException):
    raise failure


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy_context", "spec", "expected_stages"),
    [
        (
            ToolPolicyContext(
                request_context={"runtime_blocked_tool_names": ["safe_tool"]},
            ),
            ToolSpec(name="safe_tool"),
            ("runtime_phase",),
        ),
        (
            ToolPolicyContext(workspace_access_mode="read_only"),
            ToolSpec(name="safe_tool", read_only=False),
            ("runtime_phase", "official_chrome", "workspace_access"),
        ),
        (
            ToolPolicyContext(
                request_context={"collaboration_read_only": True},
            ),
            ToolSpec(name="safe_tool", read_only=False),
            (
                "runtime_phase",
                "official_chrome",
                "workspace_access",
                "collaboration_read_only",
            ),
        ),
    ],
)
async def test_policy_consumption_early_denies_never_claim_live_or_guard(
    policy_context: ToolPolicyContext,
    spec: ToolSpec,
    expected_stages: tuple[str, ...],
) -> None:
    from lantai.runtime.tool_policy import RuntimeToolPolicyMiddleware

    registry = LantaiToolRegistry()
    registry.register_tool(spec, lambda: "never")
    registry.register_policy_middleware(RuntimeToolPolicyMiddleware())
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient(
            [
                [
                    ModelStreamEvent(
                        kind="tool_call_completed",
                        tool_call=ToolCall(id="call-early", name=spec.name),
                    ),
                ],
                [_completed("handled")],
            ],
        ),
        tool_registry=registry,
        tool_policy_context=policy_context,
    )

    (
        events,
        _effective,
        _dispatch,
        invocation,
    ) = await _run_with_policy_observation(runtime, _runtime_input())
    consumption = runtime.request_policy_observer.take_consumption(
        invocation,
        terminal_type=events[-1].type,
        terminal_turn_id=events[-1].turn_id or "",
    )

    assert consumption is not None
    assert consumption.live_permission_evaluation_count == 0
    assert consumption.live_override_applied_count == 0
    assert consumption.guard_consumption_count == 0
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    state = invocation._token._state
    assert [item.override_mode for item in state.consumptions] == [
        "not_reached",
    ]
    assert state.consumptions[0].outcome_action == "deny"
    assert (
        tuple(
            stage
            for stage, _phase, _context_sha256 in state.consumptions[
                0
            ].consumer_rows
        )
        == expected_stages
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage",
    [
        "start_policy_evaluation",
        "record_policy_middleware_dispatch",
        "complete_policy_evaluation",
    ],
)
async def test_policy_dispatch_diagnostic_exception_fails_open(
    monkeypatch,
    stage: str,
) -> None:
    class AllowPolicy:
        async def evaluate(self, _tool_call, _spec, _context):
            return ToolPolicyOutcome(action="allow")

    registry = LantaiToolRegistry()
    registry.register_policy_middleware(AllowPolicy())
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient([]),
        tool_registry=registry,
    )
    runtime_input = _runtime_input()
    token = runtime.request_policy_observer.begin(runtime_input)
    assert token is not None
    invocation = claim_effective_policy_observation(token)
    assert invocation is not None
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    context = runtime._policy_context_for_turn(
        runtime_input,
        turn_id="turn-1",
        budget=_DeadlineBudget(None),
    )
    runtime.request_policy_observer.capture_effective(
        invocation,
        runtime_input=runtime_input,
        actual_turn_id="turn-1",
        effective_context=context,
        deadline_monotonic=None,
    )

    def fail(*_args, **_kwargs):
        raise RuntimeError("diagnostic failed")

    monkeypatch.setattr(runtime.request_policy_observer, stage, fail)

    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    outcome = await runtime._evaluate_policy(
        ToolCall(id="call-fail-open", name="safe"),
        ToolSpec(name="safe"),
        policy_context=context,
        budget=_DeadlineBudget(None),
        policy_observation_invocation=invocation,
    )

    assert outcome.action == "allow"


@pytest.mark.asyncio
async def test_policy_dispatch_diagnostic_base_exception_propagates(
    monkeypatch,
) -> None:
    registry = LantaiToolRegistry()
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient([]),
        tool_registry=registry,
    )
    runtime_input = _runtime_input()
    token = runtime.request_policy_observer.begin(runtime_input)
    assert token is not None
    invocation = claim_effective_policy_observation(token)
    assert invocation is not None
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    context = runtime._policy_context_for_turn(
        runtime_input,
        turn_id="turn-1",
        budget=_DeadlineBudget(None),
    )
    runtime.request_policy_observer.capture_effective(
        invocation,
        runtime_input=runtime_input,
        actual_turn_id="turn-1",
        effective_context=context,
        deadline_monotonic=None,
    )
    failure = KeyboardInterrupt()

    def interrupt(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(
        runtime.request_policy_observer,
        "start_policy_evaluation",
        interrupt,
    )

    with pytest.raises(KeyboardInterrupt) as captured:
        # White-box lock exercises the actual private boundary.
        # pylint: disable-next=protected-access
        await runtime._evaluate_policy(
            ToolCall(id="call-interrupt", name="safe"),
            ToolSpec(name="safe"),
            policy_context=context,
            budget=_DeadlineBudget(None),
            policy_observation_invocation=invocation,
        )

    assert captured.value is failure


@pytest.mark.asyncio
async def test_policy_consumption_base_exception_stays_incomplete() -> None:
    from lantai.runtime.tool_policy import RuntimeToolPolicyMiddleware

    failure = KeyboardInterrupt()

    class InterruptingApprovalService:
        async def get_session_permission_override(self, **_kwargs):
            raise failure

    registry = LantaiToolRegistry()
    registry.register_policy_middleware(
        RuntimeToolPolicyMiddleware(InterruptingApprovalService()),
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient([]),
        tool_registry=registry,
    )
    runtime_input = _runtime_input()
    token = runtime.request_policy_observer.begin(runtime_input)
    assert token is not None
    invocation = claim_effective_policy_observation(token)
    assert invocation is not None
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    context = runtime._policy_context_for_turn(
        runtime_input,
        turn_id="turn-1",
        budget=_DeadlineBudget(None),
    )
    runtime.request_policy_observer.capture_effective(
        invocation,
        runtime_input=runtime_input,
        actual_turn_id="turn-1",
        effective_context=context,
        deadline_monotonic=None,
    )

    with pytest.raises(KeyboardInterrupt) as captured:
        # White-box lock exercises the actual private boundary.
        # pylint: disable-next=protected-access
        await runtime._evaluate_policy(
            ToolCall(id="call-interrupt-consumption", name="safe"),
            ToolSpec(name="safe"),
            policy_context=context,
            budget=_DeadlineBudget(None),
            policy_observation_invocation=invocation,
        )

    assert captured.value is failure
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    state = invocation._token._state
    assert len(state.consumptions) == 1
    assert state.consumptions[0].completed is False
    assert state.consumptions[0].override_mode is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage",
    [
        "start_policy_consumption",
        "record_policy_context_consumption",
        "record_live_permission_result",
        "complete_policy_consumption",
    ],
)
async def test_policy_consumption_diagnostic_exception_fails_open(
    monkeypatch,
    stage: str,
) -> None:
    from lantai.runtime.tool_policy import RuntimeToolPolicyMiddleware

    middleware = RuntimeToolPolicyMiddleware()
    monkeypatch.setattr(
        middleware,
        "_evaluate_builtin_policy",
        lambda *_args: SimpleNamespace(
            decision="allow",
            reason="allowed",
            risk="low",
            model_dump=lambda mode=None: {
                "decision": "allow",
                "reason": "allowed",
                "risk": "low",
            },
        ),
    )
    monkeypatch.setattr(
        middleware,
        "_evaluate_tool_guard",
        lambda *_args: None,
    )
    registry = LantaiToolRegistry()
    registry.register_tool(ToolSpec(name="safe_tool"), lambda: "ok")
    registry.register_policy_middleware(middleware)
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient(
            [
                [
                    ModelStreamEvent(
                        kind="tool_call_completed",
                        tool_call=ToolCall(
                            id="call-diagnostic-consumption",
                            name="safe_tool",
                        ),
                    ),
                ],
                [_completed("done")],
            ],
        ),
        tool_registry=registry,
    )

    def fail(*_args, **_kwargs):
        raise RuntimeError("diagnostic failed")

    monkeypatch.setattr(runtime.request_policy_observer, stage, fail)

    (
        events,
        effective,
        dispatch,
        _invocation,
    ) = await _run_with_policy_observation(runtime, _runtime_input())

    assert events[-1].type == "turn.completed"
    assert effective is not None
    assert dispatch is not None
    completed = next(
        event for event in events if event.type == "tool.call.completed"
    )
    assert completed.status == "completed"


@pytest.mark.asyncio
async def test_policy_dispatch_private_values_never_reach_repr_or_hooks() -> (
    None
):
    calls: list[str] = []

    class HostileValue:
        # Hostile conversion must raise rather than return a string.
        # pylint: disable-next=invalid-repr-returned
        def __repr__(self) -> str:
            calls.append("repr")
            raise AssertionError("dispatch repr hook must not run")

        # Hostile conversion must raise rather than return a string.
        # pylint: disable-next=invalid-str-returned
        def __str__(self) -> str:
            calls.append("str")
            raise AssertionError("dispatch str hook must not run")

        def __eq__(self, _other):
            calls.append("eq")
            raise AssertionError("dispatch equality hook must not run")

    registry = LantaiToolRegistry()
    registry.register_tool(ToolSpec(name="safe_tool"), lambda: "ok")
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient(
            [
                [
                    ModelStreamEvent(
                        kind="tool_call_completed",
                        tool_call=ToolCall(
                            id="PRIVATE-CALL",
                            name="safe_tool",
                        ),
                    ),
                ],
                [_completed("done")],
            ],
        ),
        tool_registry=registry,
    )
    (
        _events,
        _effective,
        dispatch,
        _invocation,
    ) = await _run_with_policy_observation(runtime, _runtime_input())
    assert dispatch is not None
    hostile = HostileValue()
    object.__setattr__(dispatch, "evaluation_count", hostile)
    object.__setattr__(dispatch, "middleware_dispatch_count", hostile)
    object.__setattr__(dispatch, "all_contexts_matched", hostile)

    rendered = repr(dispatch)

    assert "PRIVATE-CALL" not in rendered
    assert "invalid" in rendered
    assert is_valid_request_policy_dispatch_receipt(dispatch) is False
    # Exact [] assertion also rejects None and other false values.
    # pylint: disable-next=use-implicit-booleaness-not-comparison
    assert calls == []


@pytest.mark.asyncio
async def test_tool_policy_lattice_cannot_weaken_later_deny() -> None:
    class ApprovalPolicy:
        async def evaluate(self, tool_call, spec, context):
            del tool_call, spec, context
            return ToolPolicyOutcome(
                action="needs_approval",
                reason="ask first",
            )

    class DenyPolicy:
        async def evaluate(self, tool_call, spec, context):
            del tool_call, spec, context
            return ToolPolicyOutcome(action="deny", reason="hard boundary")

    registry = LantaiToolRegistry()
    spec = ToolSpec(name="danger")
    registry.register_tool(spec, lambda: "never")
    registry.register_policy_middleware(ApprovalPolicy())
    registry.register_policy_middleware(DenyPolicy())
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient([]),
        tool_registry=registry,
    )

    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    outcome = await runtime._evaluate_policy(
        ToolCall(id="call-policy", name="danger"),
        spec,
        policy_context=ToolPolicyContext(
            turn_id="run-1",
            metadata={"policy_snapshot_hash": "sha256:policy"},
        ),
        budget=_DeadlineBudget(None),
    )

    assert outcome.action == "deny"
    assert outcome.reason == "hard boundary"
    assert [row["action"] for row in outcome.metadata["policy_chain"]] == [
        "needs_approval",
        "deny",
    ]
    assert len(outcome.metadata["authority_provenance"]) == 2
    assert (
        outcome.metadata["permission_lattice"] == "allow<needs_approval<deny"
    )


@pytest.mark.asyncio
async def test_policy_chain_empty_registry_has_exact_allow_outcome() -> None:
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient([]),
        tool_registry=LantaiToolRegistry(),
    )

    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    outcome = await runtime._evaluate_policy(
        ToolCall(id="call-empty-policy", name="safe"),
        ToolSpec(name="safe"),
        policy_context=ToolPolicyContext(turn_id="turn-empty-policy"),
        budget=_DeadlineBudget(None),
    )

    assert outcome == ToolPolicyOutcome(
        action="allow",
        metadata={
            "policy_chain": [],
            "authority_provenance": [],
            "permission_lattice": "allow<needs_approval<deny",
        },
    )


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_policy_chain_preserves_copy_order_budget_and_deny_short_circuit() -> None:  # noqa: E501
    seen: list[tuple[str, ToolPolicyContext]] = []

    class Policy:
        def __init__(self, name: str, action: str) -> None:
            self.name = name
            self.action = action

        async def evaluate(self, _tool_call, _spec, context):
            seen.append((self.name, context))
            if self.name == "first":
                context.request_context["mutated"] = True
            else:
                assert "mutated" not in context.request_context
            return ToolPolicyOutcome(action=self.action, reason=self.name)

    registry = LantaiToolRegistry()
    registry.register_policy_middleware(Policy("first", "allow"))
    registry.register_policy_middleware(Policy("second", "needs_approval"))
    registry.register_policy_middleware(Policy("third", "deny"))
    registry.register_policy_middleware(Policy("never", "allow"))
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient([]),
        tool_registry=registry,
    )
    context = ToolPolicyContext(
        turn_id="turn-chain",
        request_context={"policy_snapshot_hash": "sha256:source"},
    )
    budget_calls = 0
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    real_await = runtime._await_with_turn_budget

    async def count_budget(awaitable, *, budget):
        nonlocal budget_calls
        budget_calls += 1
        return await real_await(awaitable, budget=budget)

    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    runtime._await_with_turn_budget = count_budget

    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    outcome = await runtime._evaluate_policy(
        ToolCall(id="call-chain", name="danger"),
        ToolSpec(name="danger"),
        policy_context=context,
        budget=_DeadlineBudget(None),
    )

    assert [name for name, _context in seen] == ["first", "second", "third"]
    assert len({id(item) for _name, item in seen}) == 3
    assert all(item is not context for _name, item in seen)
    assert budget_calls == 3
    assert outcome.action == "deny"
    assert [row["index"] for row in outcome.metadata["policy_chain"]] == [
        0,
        1,
        2,
    ]
    assert [row["middleware"] for row in outcome.metadata["policy_chain"]] == [
        "Policy",
        "Policy",
        "Policy",
    ]
    assert [row["action"] for row in outcome.metadata["policy_chain"]] == [
        "allow",
        "needs_approval",
        "deny",
    ]
    assert len(outcome.metadata["authority_provenance"]) == 3
    assert all(
        row["source_ref"] == "sha256:source"
        for row in outcome.metadata["authority_provenance"]
    )
    assert context.request_context == {"policy_snapshot_hash": "sha256:source"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        # White-box lock exercises the actual private boundary.
        # pylint: disable-next=protected-access
        native_runtime_module._TurnDeadlineExceeded(),
        asyncio.CancelledError(),
        KeyboardInterrupt(),
    ],
)
async def test_policy_chain_preserves_terminal_failure_identity(
    failure,
) -> None:
    class RaisingPolicy:
        async def evaluate(self, _tool_call, _spec, _context):
            raise failure

    registry = LantaiToolRegistry()
    registry.register_policy_middleware(RaisingPolicy())
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient([]),
        tool_registry=registry,
    )

    with pytest.raises(type(failure)) as captured:
        # White-box lock exercises the actual private boundary.
        # pylint: disable-next=protected-access
        await runtime._evaluate_policy(
            ToolCall(id="call-terminal-policy", name="safe"),
            ToolSpec(name="safe"),
            policy_context=ToolPolicyContext(turn_id="turn-terminal-policy"),
            budget=_DeadlineBudget(None),
        )

    assert captured.value is failure


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_policy_exception_denies_without_tool_execution() -> None:  # noqa: E501
    class RaisingPolicy:
        async def evaluate(self, tool_call, spec, context):
            raise RuntimeError("policy unavailable")

    calls = 0

    def danger() -> str:
        nonlocal calls
        calls += 1
        return "should-not-run"

    registry = LantaiToolRegistry()
    registry.register_tool(ToolSpec(name="danger"), danger)
    registry.register_policy_middleware(RaisingPolicy())
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(id="call-policy-error", name="danger"),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("handled", message_id="msg-policy-error")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        tool_policy_context=ToolPolicyContext(session_id="session-1"),
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert calls == 0
    failed = [
        event for event in events if event.type == "tool.call.completed"
    ][0]
    assert failed.status == "failed"
    assert "policy" in failed.payload["error"].lower()
    assert client.requests[1].messages[-1].content[0].data["output"][0][
        "text"
    ] == (failed.payload["error"])


@pytest.mark.asyncio
async def test_native_runtime_policy_context_is_per_turn_snapshot() -> None:
    seen_contexts: list[tuple[str | None, str | None, dict[str, Any]]] = []

    class RecordingPolicy:
        async def evaluate(self, tool_call, spec, context):
            del tool_call, spec
            seen_contexts.append(
                (
                    context.session_id,
                    context.workspace_dir,
                    dict(context.request_context),
                ),
            )
            context.request_context["session_id"] = "mutated"
            return ToolPolicyOutcome(action="allow")

    registry = LantaiToolRegistry()
    registry.register_tool(ToolSpec(name="safe_tool"), lambda: "ok")
    registry.register_policy_middleware(RecordingPolicy())
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(id="call-a", name="safe_tool"),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("done-a", message_id="msg-a")],
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(id="call-b", name="safe_tool"),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("done-b", message_id="msg-b")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        tool_policy_context=ToolPolicyContext(
            session_id="stale-session",
            workspace_dir="/stale/workspace",
            request_context={"session_id": "stale-session", "base": "kept"},
        ),
    )
    first_input = _runtime_input()
    second_input = RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id="session-2",
            user_id="user-2",
            run_id="turn-2",
            workspace_dir="/tmp/second-workspace",
            metadata={"request_marker": "second"},
        ),
        messages=[RuntimeMessage.text("user", "again")],
    )

    first_events = [event async for event in runtime.run_turn(first_input)]
    second_events = [event async for event in runtime.run_turn(second_input)]

    assert first_events[-1].type == "turn.completed"
    assert second_events[-1].type == "turn.completed"
    assert seen_contexts == [
        (
            "session-1",
            "/tmp/workspace",
            {
                "session_id": "session-1",
                "agent_id": "default",
                "user_id": "user-1",
                "channel": "console",
                "run_id": "turn-1",
                "turn_id": "turn-1",
                "workspace_dir": "/tmp/workspace",
                "base": "kept",
            },
        ),
        (
            "session-2",
            "/tmp/second-workspace",
            {
                "session_id": "session-2",
                "agent_id": "default",
                "user_id": "user-2",
                "channel": "console",
                "run_id": "turn-2",
                "turn_id": "turn-2",
                "workspace_dir": "/tmp/second-workspace",
                "request_marker": "second",
                "base": "kept",
            },
        ),
    ]


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_turn_deadline_times_out_slow_model_stream() -> None:  # noqa: E501
    class SlowModelClient:
        provider_id = "test"
        model_name = "test-model"

        async def stream(self, request: ModelRequest):
            del request
            await asyncio.sleep(0.05)
            yield _completed("late")

        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError("complete should not be called")

    runtime_input = _runtime_input().model_copy(
        update={"metadata": {"native_turn_timeout_seconds": 0.01}},
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: SlowModelClient(),
        tool_registry=LantaiToolRegistry(),
    )

    events = []
    with pytest.raises(asyncio.TimeoutError):
        async for event in runtime.run_turn(runtime_input):
            events.append(event)

    assert [event.type for event in events] == ["turn.started", "turn.failed"]
    assert events[-1].summary == "Native runtime timed out."


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_has_a_distinct_first_model_event_deadline() -> None:  # noqa: E501
    class SilentModelClient:
        provider_id = "test"
        model_name = "test-model"

        def __init__(self) -> None:
            self.request_count = 0

        async def stream(self, request: ModelRequest):
            del request
            self.request_count += 1
            await asyncio.sleep(0.05)
            yield _completed("late")

        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError("complete should not be called")

    client = SilentModelClient()
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
        turn_timeout_seconds=1,
        model_first_event_timeout_seconds=0.01,
        model_idle_timeout_seconds=1,
    )

    events = []
    with pytest.raises(asyncio.TimeoutError):
        async for event in runtime.run_turn(_runtime_input()):
            events.append(event)

    assert client.request_count == 2
    assert [event.type for event in events] == [
        "turn.started",
        "runtime.warning",
        "turn.failed",
    ]
    assert events[1].payload["details"] == {
        "reason": "first_event_timeout",
        "timeout_seconds": 0.01,
        "attempt": 1,
        "max_attempts": 1,
        "discard_message_ids": [],
    }
    assert "没有返回首个响应" in events[-1].summary
    assert events[-1].payload["error_code"] == "model_first_event_timeout"
    assert events[-1].payload["error"] == {
        "timeout_phase": "first_event",
        "timeout_seconds": 0.01,
    }


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_recovers_once_from_missing_first_model_event() -> None:  # noqa: E501
    class SilentOnceClient:
        provider_id = "test"
        model_name = "test-model"

        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []

        async def stream(self, request: ModelRequest):
            self.requests.append(request)
            if len(self.requests) == 1:
                await asyncio.sleep(0.05)
                yield _completed("不应到达", message_id="msg-stalled")
                return
            yield _completed("连接恢复完成", message_id="msg-recovered")

        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError("complete should not be called")

    client = SilentOnceClient()
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
        turn_timeout_seconds=1,
        model_first_event_timeout_seconds=0.01,
        model_idle_timeout_seconds=1,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert len(client.requests) == 2
    assert [
        request.metadata["runtime_model_stream_recovery_attempt"]
        for request in client.requests
    ] == [0, 1]
    assert events[-1].type == "turn.completed"
    assert [
        event.summary for event in events if event.type == "message.completed"
    ] == ["连接恢复完成"]
    recoveries = [
        event
        for event in events
        if event.type == "runtime.warning"
        and event.payload.get("kind") == "model_output_recovery"
    ]
    assert len(recoveries) == 1
    assert recoveries[0].summary == "暂未收到上游响应，正在检查连接并从已有进度继续。"
    assert recoveries[0].payload["details"] == {
        "reason": "first_event_timeout",
        "timeout_seconds": 0.01,
        "attempt": 1,
        "max_attempts": 1,
        "discard_message_ids": [],
    }
    assert client.requests[1].metadata["model_liveness_policy"][
        "safe_current_round_recovery"
    ] == {
        "max_attempts": 1,
        "reasons": ["first_event_timeout", "stream_idle_timeout"],
        "recovered_dispatch_max_retries": 0,
        "partial_output_commit": False,
    }


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_provider_activity_keeps_long_tool_stream_alive() -> None:  # noqa: E501
    class ActiveToolStreamClient:
        provider_id = "test"
        model_name = "test-model"

        async def stream(self, request: ModelRequest):
            del request
            yield ModelStreamEvent(
                kind="message_start",
                message_id="msg-active",
            )
            for _ in range(4):
                await asyncio.sleep(0.05)
                yield ModelStreamEvent(
                    kind="provider_activity",
                    message_id="msg-active",
                    metadata={"provider_chunk": True},
                )
            await asyncio.sleep(0.05)
            yield _completed("长工具参数流已经完成。", message_id="msg-active")

        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError("complete should not be called")

    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ActiveToolStreamClient(),
        tool_registry=LantaiToolRegistry(),
        turn_timeout_seconds=1,
        model_first_event_timeout_seconds=0.2,
        model_idle_timeout_seconds=0.2,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert events[-1].type == "turn.completed"
    activity_events = [
        event
        for event in events
        if event.type == "runtime.warning"
        and event.payload.get("kind") == "model_transport_activity"
    ]
    assert len(activity_events) == 1
    assert activity_events[0].summary == "上游连接仍有响应，尚未收到可交付内容。"
    assert activity_events[0].payload["details"] == {
        "activity_scope": "provider_event",
        "semantic_output": False,
        "upstream_model_confirmed": True,
    }


@pytest.mark.asyncio
async def test_gateway_ping_activity_does_not_claim_model_progress() -> None:
    class GatewayPingClient:
        provider_id = "test"
        model_name = "test-model"

        async def stream(self, request: ModelRequest):
            del request
            yield ModelStreamEvent(kind="message_start", message_id="msg-ping")
            yield ModelStreamEvent(
                kind="provider_activity",
                message_id="msg-ping",
                metadata={
                    "activity_scope": "transport_sse_comment",
                    "semantic_output": False,
                    "upstream_model_confirmed": False,
                },
            )
            yield _completed("正文完成", message_id="msg-ping")

        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError("complete should not be called")

    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: GatewayPingClient(),
        tool_registry=LantaiToolRegistry(),
        turn_timeout_seconds=1,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    activity = next(
        event
        for event in events
        if event.type == "runtime.warning"
        and event.payload.get("kind") == "model_transport_activity"
    )
    assert activity.summary == "传输链路仍有活动，但尚不能确认模型正在生成内容。"
    assert activity.payload["details"]["upstream_model_confirmed"] is False


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_runtime_metadata_cannot_override_model_phase_deadlines() -> None:  # noqa: E501
    class SlowFirstEventClient:
        provider_id = "test"
        model_name = "test-model"

        def __init__(self) -> None:
            self.request_count = 0

        async def stream(self, request: ModelRequest):
            del request
            self.request_count += 1
            await asyncio.sleep(0.02)
            yield _completed("可信构造策略生效", message_id="msg-trusted")

        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError("complete should not be called")

    client = SlowFirstEventClient()
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
        turn_timeout_seconds=1,
        model_first_event_timeout_seconds=0.1,
        model_idle_timeout_seconds=0.1,
    )
    runtime_input = _runtime_input()
    runtime_input.metadata.update(
        {
            "native_model_first_event_timeout_seconds": 0.001,
            "native_model_idle_timeout_seconds": 0.001,
        },
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert client.request_count == 1
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_local_message_start_does_not_satisfy_first_provider_event_deadline() -> None:  # noqa: E501
    class HeaderOnlyOnceClient:
        provider_id = "test"
        model_name = "test-model"

        def __init__(self) -> None:
            self.request_count = 0

        async def stream(self, request: ModelRequest):
            del request
            self.request_count += 1
            if self.request_count == 1:
                # Adapters emit this local envelope after headers arrive.  It
                # is not a decoded provider delta and must not switch the
                # watchdog to the shorter inter-event idle phase.
                yield ModelStreamEvent(
                    kind="message_start",
                    message_id="msg-header-only",
                )
                await asyncio.sleep(0.05)
                yield _completed("不应到达", message_id="msg-header-only")
                return
            yield _completed("真实首事件已恢复", message_id="msg-recovered")

        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError("complete should not be called")

    client = HeaderOnlyOnceClient()
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
        turn_timeout_seconds=1,
        model_first_event_timeout_seconds=0.01,
        model_idle_timeout_seconds=0.1,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert client.request_count == 2
    assert events[-1].type == "turn.completed"
    recovery = next(
        event
        for event in events
        if event.type == "runtime.warning"
        and event.payload.get("kind") == "model_output_recovery"
    )
    assert recovery.payload["details"]["reason"] == "first_event_timeout"


@pytest.mark.asyncio
async def test_native_runtime_recovers_once_from_idle_model_stream() -> None:
    executed: list[str] = []

    def side_effect(value: str) -> str:
        executed.append(value)
        return "executed"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="side_effect",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        ),
        side_effect,
    )

    class IdleOnceClient:
        provider_id = "test"
        model_name = "test-model"

        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []

        async def stream(self, request: ModelRequest):
            self.requests.append(request)
            if len(self.requests) == 1:
                yield ModelStreamEvent(
                    kind="message_start",
                    message_id="msg-stalled",
                )
                yield ModelStreamEvent(
                    kind="text_delta",
                    message_id="msg-stalled",
                    delta="未完成的半截输出",
                )
                yield ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-stalled",
                        name="side_effect",
                        arguments={"value": "must-not-run"},
                    ),
                )
                await asyncio.sleep(0.05)
                yield _completed("不应到达", message_id="msg-stalled")
                return
            yield _completed("恢复完成", message_id="msg-recovered")

        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError("complete should not be called")

    client = IdleOnceClient()
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        turn_timeout_seconds=1,
        model_first_event_timeout_seconds=0.01,
        model_idle_timeout_seconds=0.01,
        model_request_timeout_seconds=1,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert len(client.requests) == 2
    # Exact [] assertion also rejects None and other false values.
    # pylint: disable-next=use-implicit-booleaness-not-comparison
    assert executed == []
    completed = [
        event.summary for event in events if event.type == "message.completed"
    ]
    assert completed == ["恢复完成"]
    recoveries = [
        event
        for event in events
        if event.type == "runtime.warning"
        and event.payload.get("kind") == "model_output_recovery"
        and event.payload.get("details", {}).get("reason")
        == "stream_idle_timeout"
    ]
    assert len(recoveries) == 1
    assert recoveries[0].payload["details"] == {
        "reason": "stream_idle_timeout",
        "timeout_seconds": 0.01,
        "attempt": 1,
        "max_attempts": 1,
        "discard_message_ids": ["msg-stalled"],
    }
    assert any(
        message.metadata.get("model_stream_idle_recovery") is True
        for message in client.requests[1].messages
    )
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_recovers_same_run_after_stream_connection_loss() -> None:  # noqa: E501
    import httpx

    executed: list[str] = []

    def side_effect(value: str) -> str:
        executed.append(value)
        return "executed"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="side_effect",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        ),
        side_effect,
    )

    class DisconnectOnceClient:
        provider_id = "test"
        model_name = "test-model"

        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []

        async def stream(self, request: ModelRequest):
            self.requests.append(request)
            if len(self.requests) == 1:
                yield ModelStreamEvent(
                    kind="message_start",
                    message_id="msg-disconnected",
                )
                yield ModelStreamEvent(
                    kind="text_delta",
                    message_id="msg-disconnected",
                    delta="断线前已经可见的进度",
                )
                yield ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-disconnected",
                        name="side_effect",
                        arguments={"value": "must-not-run"},
                    ),
                )
                raise httpx.RemoteProtocolError("peer closed the stream")
            yield _completed("重连后完成", message_id="msg-reconnected")

        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError("complete should not be called")

    client = DisconnectOnceClient()
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        turn_timeout_seconds=1,
        model_first_event_timeout_seconds=0.01,
        model_idle_timeout_seconds=0.1,
        model_request_timeout_seconds=1,
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert len(client.requests) == 2
    # Exact [] assertion also rejects None and other false values.
    # pylint: disable-next=use-implicit-booleaness-not-comparison
    assert executed == []
    assert any(
        event.type == "message.delta" and event.summary == "断线前已经可见的进度"
        for event in events
    )
    recoveries = [
        event
        for event in events
        if event.type == "runtime.warning"
        and event.payload.get("kind") == "model_output_recovery"
        and event.payload.get("details", {}).get("reason")
        == "provider_connection_interrupted"
    ]
    assert len(recoveries) == 1
    assert recoveries[0].payload["details"]["discard_message_ids"] == [
        "msg-disconnected",
    ]
    assert recoveries[0].payload["details"]["unexecuted_tool_call_ids"] == [
        "call-disconnected",
    ]
    assert (
        client.requests[0].metadata["run_id"]
        == client.requests[1].metadata["run_id"]
    )
    assert client.requests[0].metadata.get("attempt_id") == client.requests[
        1
    ].metadata.get("attempt_id")
    assert any(
        message.metadata.get("model_stream_idle_recovery") is True
        for message in client.requests[1].messages
    )
    assert [
        event.summary for event in events if event.type == "message.completed"
    ] == ["重连后完成"]
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_stops_after_one_stream_connection_recovery() -> None:  # noqa: E501
    import httpx

    class AlwaysDisconnectingClient:
        provider_id = "test"
        model_name = "test-model"

        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []

        async def stream(self, request: ModelRequest):
            self.requests.append(request)
            message_id = f"msg-disconnected-{len(self.requests)}"
            yield ModelStreamEvent(kind="message_start", message_id=message_id)
            yield ModelStreamEvent(
                kind="text_delta",
                message_id=message_id,
                delta=f"partial-{len(self.requests)}",
            )
            raise httpx.RemoteProtocolError("peer closed the stream")

        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError("complete should not be called")

    client = AlwaysDisconnectingClient()
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
        turn_timeout_seconds=1,
        model_first_event_timeout_seconds=0.01,
        model_idle_timeout_seconds=0.1,
        model_request_timeout_seconds=1,
    )

    events: list[RuntimeEvent] = []
    with pytest.raises(httpx.RemoteProtocolError):
        async for event in runtime.run_turn(_runtime_input()):
            events.append(event)

    assert len(client.requests) == 2
    recoveries = [
        event
        for event in events
        if event.type == "runtime.warning"
        and event.payload.get("details", {}).get("reason")
        == "provider_connection_interrupted"
    ]
    assert len(recoveries) == 1
    assert events[-1].type == "turn.failed"
    assert (
        events[-1].payload["error_code"] == "provider_connection_interrupted"
    )


@pytest.mark.asyncio
async def test_native_runtime_stops_after_one_idle_stream_recovery() -> None:
    class AlwaysIdleClient:
        provider_id = "test"
        model_name = "test-model"

        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []

        async def stream(self, request: ModelRequest):
            self.requests.append(request)
            message_id = f"msg-stalled-{len(self.requests)}"
            yield ModelStreamEvent(kind="message_start", message_id=message_id)
            yield ModelStreamEvent(
                kind="provider_activity",
                message_id=message_id,
                metadata={"provider_event": True},
            )
            await asyncio.sleep(0.05)
            yield _completed("不应到达", message_id=message_id)

        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError("complete should not be called")

    client = AlwaysIdleClient()
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
        turn_timeout_seconds=1,
        model_first_event_timeout_seconds=0.01,
        model_idle_timeout_seconds=0.01,
        model_request_timeout_seconds=1,
    )

    events: list[RuntimeEvent] = []
    with pytest.raises(asyncio.TimeoutError):
        async for event in runtime.run_turn(_runtime_input()):
            events.append(event)

    assert len(client.requests) == 2
    recoveries = [
        event
        for event in events
        if event.type == "runtime.warning"
        and event.payload.get("details", {}).get("reason")
        == "stream_idle_timeout"
    ]
    assert len(recoveries) == 1
    assert events[-1].type == "turn.failed"
    assert events[-1].payload["error_code"] == "model_stream_idle_timeout"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_provider_activity_cannot_bypass_total_model_request_deadline() -> None:  # noqa: E501
    class EndlessActivityClient:
        provider_id = "test"
        model_name = "test-model"

        async def stream(self, request: ModelRequest):
            del request
            yield ModelStreamEvent(
                kind="message_start",
                message_id="msg-active",
            )
            while True:
                await asyncio.sleep(0.005)
                yield ModelStreamEvent(
                    kind="provider_activity",
                    message_id="msg-active",
                    metadata={"provider_chunk": True},
                )

        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError("complete should not be called")

    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: EndlessActivityClient(),
        tool_registry=LantaiToolRegistry(),
        turn_timeout_seconds=1,
        model_first_event_timeout_seconds=0.01,
        model_idle_timeout_seconds=0.02,
        model_request_timeout_seconds=0.03,
    )

    events = []
    with pytest.raises(asyncio.TimeoutError):
        async for event in runtime.run_turn(_runtime_input()):
            events.append(event)

    assert events[-1].type == "turn.failed"
    assert events[-1].payload["error_code"] == "model_request_timeout"
    assert events[-1].payload["error"] == {
        "timeout_phase": "request",
        "timeout_seconds": 0.03,
    }


def test_native_model_request_default_has_no_total_deadline() -> None:
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient([]),
        tool_registry=LantaiToolRegistry(),
    )

    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    assert runtime._model_request_timeout_seconds is None
    assert runtime.model_request_policy_receipt()["model_request_total"] == {
        "timeout_seconds": None,
        "origin": "version_default",
    }


def test_native_model_request_timeout_tri_state_and_invalid_values() -> None:
    explicit_none = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient([]),
        tool_registry=LantaiToolRegistry(),
        model_request_timeout_seconds=None,
    )
    explicit_positive = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ScriptedModelClient([]),
        tool_registry=LantaiToolRegistry(),
        model_request_timeout_seconds=42.5,
    )

    assert explicit_none.model_request_policy_receipt()[
        "model_request_total"
    ] == {"timeout_seconds": None, "origin": "explicit_none"}
    assert explicit_positive.model_request_policy_receipt()[
        "model_request_total"
    ] == {"timeout_seconds": 42.5, "origin": "explicit_positive"}
    for invalid in (0, -1, float("nan"), float("inf"), True, "invalid"):
        with pytest.raises(ValueError, match="positive finite number or None"):
            NativeLantaiRuntime(
                model_client_factory=lambda _agent_id: ScriptedModelClient([]),
                tool_registry=LantaiToolRegistry(),
                model_request_timeout_seconds=invalid,
            )


@pytest.mark.asyncio
async def test_default_model_request_survives_more_than_former_total_lifetime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AdvancingClock:
        def __init__(self) -> None:
            self.now = 0.0

        def time(self) -> float:
            return self.now

    clock = AdvancingClock()

    class HealthyLongStreamClient:
        provider_id = "test"
        model_name = "test-model"

        async def stream(self, request: ModelRequest):
            del request
            yield ModelStreamEvent(kind="message_start", message_id="msg-long")
            for now in (1.0, 101.0, 201.0, 301.0):
                clock.now = now
                yield ModelStreamEvent(
                    kind="thinking_delta",
                    message_id="msg-long",
                    delta="still working",
                )
            clock.now = 331.0
            yield _completed("done", message_id="msg-long")

        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError("complete should not be called")

    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: HealthyLongStreamClient(),
        tool_registry=LantaiToolRegistry(),
    )
    # Scope the synthetic clock to the adapter; Store and asyncio internals
    # still need the actual event loop for their owned executor work.
    monkeypatch.setattr(
        native_runtime_module,
        "asyncio",
        SimpleNamespace(**{**vars(asyncio), "get_running_loop": lambda: clock}),
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_legacy_runtime_metadata_cannot_restore_model_request_total_deadline() -> None:  # noqa: E501
    class ActiveStreamClient:
        provider_id = "test"
        model_name = "test-model"

        async def stream(self, request: ModelRequest):
            del request
            yield ModelStreamEvent(
                kind="message_start",
                message_id="msg-active",
            )
            await asyncio.sleep(0.02)
            yield _completed("done", message_id="msg-active")

        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError("complete should not be called")

    runtime_input = _runtime_input()
    runtime_input.metadata["native_model_request_timeout_seconds"] = 0.005
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ActiveStreamClient(),
        tool_registry=LantaiToolRegistry(),
        model_first_event_timeout_seconds=0.1,
        model_idle_timeout_seconds=0.1,
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_default_first_event_deadline_tolerates_cloud_cold_start(  # noqa: E501
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ColdStartModelClient:
        provider_id = "test"
        model_name = "test-model"

        async def stream(self, request: ModelRequest):
            del request
            await asyncio.sleep(0.05)
            yield _completed("ready")

        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError("complete should not be called")

    observed_timeouts: list[float] = []
    real_wait_for = asyncio.wait_for

    async def accelerated_wait_for(awaitable: Any, timeout: float):
        observed_timeouts.append(timeout)
        return await real_wait_for(awaitable, timeout=0.1)

    monkeypatch.setattr(
        native_runtime_module.asyncio,
        "wait_for",
        accelerated_wait_for,
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: ColdStartModelClient(),
        tool_registry=LantaiToolRegistry(),
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert events[-1].type == "turn.completed"
    assert observed_timeouts[0] == pytest.approx(5 * 60)


@pytest.mark.asyncio
async def test_native_runtime_turn_deadline_times_out_slow_tool() -> None:
    async def slow_tool() -> str:
        await asyncio.sleep(0.4)
        return "late"

    registry = LantaiToolRegistry()
    registry.register_tool(ToolSpec(name="slow_tool"), slow_tool)
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(id="call-slow", name="slow_tool"),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("should-not-run", message_id="msg-late")],
        ],
    )
    runtime_input = _runtime_input().model_copy(
        update={"metadata": {"native_turn_timeout_seconds": 0.2}},
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )

    events = []
    with pytest.raises(asyncio.TimeoutError):
        async for event in runtime.run_turn(runtime_input):
            events.append(event)

    assert [event.type for event in events] == [
        "turn.started",
        "tool.call.started",
        "tool.started",
        "turn.failed",
    ]
    assert events[-1].summary == "Native runtime timed out."


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_does_not_report_missing_approval_ui_as_user_denial() -> None:  # noqa: E501
    class ApprovalPolicy:
        async def evaluate(self, tool_call, spec, context):
            del tool_call, spec, context
            return ToolPolicyOutcome(
                action="needs_approval",
                reason="needs a human",
                policy_payload={"decision": "prompt"},
            )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(name="approval_tool"),
        lambda: "should-not-run",
    )
    registry.register_policy_middleware(ApprovalPolicy())
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(id="call-no-ui", name="approval_tool"),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("handled", message_id="msg-no-ui")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        tool_policy_context=ToolPolicyContext(session_id="session-1"),
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert not any(event.type == "approval.requested" for event in events)
    assert not any(event.type == "tool.started" for event in events)
    completed = [
        event for event in events if event.type == "tool.call.completed"
    ][0]
    assert (
        completed.payload["metadata"]["error_code"]
        == "approval_ui_unavailable"
    )
    assert "did not deny" in completed.payload["error"]


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_waits_for_approval_before_tool_execution() -> None:  # noqa: E501
    class ApprovalPolicy:
        async def evaluate(self, tool_call, spec, context):
            del tool_call, spec, context
            return ToolPolicyOutcome(
                action="needs_approval",
                reason="needs a human",
                policy_payload={"decision": "prompt"},
            )

    class ApprovalService:
        def __init__(self) -> None:
            self.created: dict[str, Any] | None = None

        async def create_pending(self, **kwargs):
            self.created = kwargs
            future = asyncio.get_running_loop().create_future()
            future.set_result(ApprovalDecision.APPROVED)
            return SimpleNamespace(
                request_id="approval-1",
                future=future,
                timeout_seconds=1.0,
            )

    registry = LantaiToolRegistry()
    registry.register_tool(ToolSpec(name="approved_tool"), lambda: "ok")
    registry.register_policy_middleware(ApprovalPolicy())
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(id="call-1", name="approved_tool"),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("done", message_id="msg-2")],
        ],
    )
    approval_service = ApprovalService()
    runtime_input = _runtime_input().model_copy(
        update={
            "request": _runtime_input().request.model_copy(
                update={
                    "run_id": "run-approval",
                    "metadata": {
                        "attempt_id": "attempt-approval",
                        "contract_hash": "sha256:contract",
                    },
                },
            ),
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        approval_service=approval_service,
        tool_policy_context=ToolPolicyContext(session_id="session-1"),
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert any(event.type == "approval.requested" for event in events)
    assert any(event.type == "approval.resolved" for event in events)
    event_types = [event.type for event in events]
    assert event_types.index("approval.requested") < event_types.index(
        "tool.started",
    )
    approval_event = next(
        event for event in events if event.type == "approval.requested"
    )
    resolved_event = next(
        event for event in events if event.type == "approval.resolved"
    )
    binding = approval_event.payload["approval_binding"]
    assert approval_service.created is not None
    assert approval_service.created["extra"]["approval_binding"] == binding
    assert resolved_event.payload["approval_binding"] == binding
    assert binding["run_id"] == "run-approval"
    assert binding["attempt_id"] == "attempt-approval"
    assert binding["effect_id"] == "effect:call-1"
    assert binding["contract_hash"] == "sha256:contract"
    assert binding["policy_snapshot_hash"].startswith("sha256:")
    assert binding["tool_call_id"] == "call-1"
    assert binding["tool_name"] == "approved_tool"
    legacy_item = runtime_event_to_legacy_stream_item(approval_event)
    assert legacy_item is not None
    assert legacy_item[0].metadata["message_type"] == "tool_guard_approval"
    completed = [
        event for event in events if event.type == "tool.call.completed"
    ][0]
    assert completed.status == "completed"
    assert completed.payload["output"][0]["text"] == "ok"


@pytest.mark.asyncio
async def test_native_runtime_supersedes_pending_approval_before_effect_start(
    tmp_path,
) -> None:
    class ApprovalPolicy:
        async def evaluate(self, tool_call, spec, context):
            del tool_call, spec, context
            return ToolPolicyOutcome(
                action="needs_approval",
                reason="needs a human",
                policy_payload={"decision": "prompt"},
            )

    class ApprovalService:
        def __init__(self) -> None:
            self.created = asyncio.Event()
            self.future: asyncio.Future[ApprovalDecision] | None = None
            self.resolved: list[tuple[str, ApprovalDecision]] = []

        async def create_pending(self, **kwargs):
            del kwargs
            self.future = asyncio.get_running_loop().create_future()
            self.created.set()
            return SimpleNamespace(
                request_id="approval-superseded",
                future=self.future,
                timeout_seconds=None,
            )

        async def resolve_request(self, request_id, decision):
            self.resolved.append((request_id, decision))
            if self.future is not None and not self.future.done():
                self.future.set_result(decision)

    executions = 0

    def external_write() -> str:
        nonlocal executions
        executions += 1
        return "written"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="external_write",
            effect_recovery=RuntimeEffectRecoveryCapability(
                external_side_effect=True,
                idempotency="not_idempotent",
                probe="none",
                compensation="none",
                on_ambiguity="manual_disambiguation",
            ),
        ),
        external_write,
    )
    registry.register_policy_middleware(ApprovalPolicy())
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-superseded",
                        name="external_write",
                    ),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("已按新要求处理。", message_id="msg-redirected")],
        ],
    )
    approval_service = ApprovalService()
    runtime_input = _runtime_input().model_copy(
        update={
            "metadata": {
                "state_workspace_dir": str(tmp_path),
                "attempt_id": "attempt-superseded",
            },
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        approval_service=approval_service,
        tool_policy_context=ToolPolicyContext(session_id="session-1"),
    )

    async def consume_events() -> list[RuntimeEvent]:
        return [event async for event in runtime.run_turn(runtime_input)]

    task = asyncio.create_task(consume_events())
    await asyncio.wait_for(approval_service.created.wait(), timeout=1.0)
    control_store = RuntimeTurnControlStore(tmp_path)
    control_store.append_input(
        command_id="redirect-during-approval",
        run_id="turn-1",
        attempt_id="attempt-superseded",
        chat_id="session-1",
        message=RuntimeMessage.text("user", "不要执行写入，改为只说明方案。"),
        intent="redirect",
    )

    events = await asyncio.wait_for(task, timeout=2.0)

    assert executions == 0
    assert approval_service.resolved == [
        ("approval-superseded", ApprovalDecision.DENIED),
    ]
    assert not any(event.type == "tool.started" for event in events)
    completed = next(
        event for event in events if event.type == "tool.call.completed"
    )
    assert completed.payload["error_code"] == "steered_before_effect"
    assert completed.payload["metadata"]["invocation_status"] == "not_started"
    assert (
        completed.payload["metadata"]["side_effect_outcome"]
        == "known_not_applied"
    )
    assert "不要执行写入" in client.requests[1].messages[-1].text_content()
    receipt = control_store.get_input_receipt(
        chat_id="session-1",
        command_id="redirect-during-approval",
    )
    assert receipt is not None
    assert receipt.execution_status == "applied"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_skips_stale_approval_ui_after_live_full_access() -> None:  # noqa: E501
    from lantai.app.approvals.service import ApprovalService

    class ApprovalPolicy:
        async def evaluate(self, tool_call, spec, context):
            del tool_call, spec, context
            return ToolPolicyOutcome(
                action="needs_approval",
                reason="evaluation raced with a live permission change",
                policy_payload={"decision": "prompt"},
            )

    executions = 0

    def approved_tool() -> str:
        nonlocal executions
        executions += 1
        return "ok"

    registry = LantaiToolRegistry()
    registry.register_tool(ToolSpec(name="approved_tool"), approved_tool)
    registry.register_policy_middleware(ApprovalPolicy())
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(id="call-live", name="approved_tool"),
                ),
            ],
            [_completed("done", message_id="msg-live")],
        ],
    )
    approval_service = ApprovalService()
    await approval_service.set_session_permission_override(
        root_session_id="session-1",
        user_id="user-1",
        permission_mode="full_auto",
        permission_profile="full",
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        approval_service=approval_service,
        tool_policy_context=ToolPolicyContext(
            session_id="session-1",
            request_context={"permission_snapshot_at": 0.0},
        ),
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert executions == 1
    assert "approval.requested" not in [event.type for event in events]
    assert "approval.resolved" not in [event.type for event in events]


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_pauses_turn_deadline_while_waiting_for_approval() -> None:  # noqa: E501
    class ApprovalPolicy:
        async def evaluate(self, tool_call, spec, context):
            del tool_call, spec, context
            return ToolPolicyOutcome(
                action="needs_approval",
                reason="needs a human",
                policy_payload={"decision": "prompt"},
            )

    class ApprovalService:
        def __init__(self) -> None:
            self.waiter: asyncio.Task[None] | None = None
            self.timeout_seconds: float | None = 300.0

        async def create_pending(self, **kwargs):
            self.timeout_seconds = kwargs["timeout_seconds"]
            future = asyncio.get_running_loop().create_future()

            async def approve_later() -> None:
                await asyncio.sleep(0.4)
                future.set_result(ApprovalDecision.APPROVED)

            self.waiter = asyncio.create_task(approve_later())
            return SimpleNamespace(
                request_id="approval-after-turn-deadline",
                future=future,
                timeout_seconds=None,
            )

    executions = 0

    def approved_tool() -> str:
        nonlocal executions
        executions += 1
        return "ok"

    registry = LantaiToolRegistry()
    registry.register_tool(ToolSpec(name="approved_tool"), approved_tool)
    registry.register_policy_middleware(ApprovalPolicy())
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(
                        id="call-approval-wait",
                        name="approved_tool",
                    ),
                ),
            ],
            [_completed("done", message_id="msg-after-approval")],
        ],
    )
    approval_service = ApprovalService()
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        approval_service=approval_service,
        tool_policy_context=ToolPolicyContext(session_id="session-1"),
        turn_timeout_seconds=0.2,
    )

    events = []
    try:
        async for event in runtime.run_turn(_runtime_input()):
            events.append(event)
            if event.type == "approval.requested":
                # Reproduce UI/transport backpressure after the approval has
                # become externally actionable. The approval may resolve
                # before the runtime resumes and awaits its already-complete
                # future; that entire phase must remain outside turn budget.
                await asyncio.sleep(0.4)
    finally:
        if approval_service.waiter is not None:
            await approval_service.waiter

    assert executions == 1
    assert approval_service.timeout_seconds is None
    assert not any(event.type == "turn.failed" for event in events)
    assert any(
        event.type == "approval.resolved"
        and event.payload["decision"] == ApprovalDecision.APPROVED.value
        for event in events
    )
    assert events[-1].type == "turn.completed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_stops_tool_execution_when_approval_denied() -> None:  # noqa: E501
    class ApprovalPolicy:
        async def evaluate(self, tool_call, spec, context):
            del tool_call, spec, context
            return ToolPolicyOutcome(
                action="needs_approval",
                reason="needs a human",
                policy_payload={"decision": "prompt"},
            )

    class ApprovalService:
        async def create_pending(self, **kwargs):
            del kwargs
            future = asyncio.get_running_loop().create_future()
            future.set_result(ApprovalDecision.DENIED)
            return SimpleNamespace(
                request_id="approval-denied",
                future=future,
                timeout_seconds=1.0,
            )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(name="denied_tool"),
        lambda: "should-not-run",
    )
    registry.register_policy_middleware(ApprovalPolicy())
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(id="call-denied", name="denied_tool"),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("handled", message_id="msg-denied")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        approval_service=ApprovalService(),
        tool_policy_context=ToolPolicyContext(session_id="session-1"),
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    assert any(event.type == "approval.requested" for event in events)
    assert not any(event.type == "tool.started" for event in events)
    completed = [
        event for event in events if event.type == "tool.call.completed"
    ][0]
    assert completed.status == "failed"
    assert completed.payload["error"] == "Tool approval denied."
    assert (
        client.requests[1].messages[-1].content[0].data["output"][0]["text"]
        == "Tool approval denied."
    )


@pytest.mark.asyncio
async def test_native_runtime_marks_tool_approval_timeout() -> None:
    class ApprovalPolicy:
        async def evaluate(self, tool_call, spec, context):
            del tool_call, spec, context
            return ToolPolicyOutcome(
                action="needs_approval",
                reason="needs a human",
                policy_payload={"decision": "prompt"},
            )

    class ApprovalService:
        def __init__(self) -> None:
            self.resolved: list[tuple[str, ApprovalDecision]] = []

        async def create_pending(self, **kwargs):
            del kwargs
            return SimpleNamespace(
                request_id="approval-timeout",
                future=asyncio.get_running_loop().create_future(),
                timeout_seconds=0.01,
            )

        async def resolve_request(self, request_id, decision):
            self.resolved.append((request_id, decision))

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(name="timeout_tool"),
        lambda: "should-not-run",
    )
    registry.register_policy_middleware(ApprovalPolicy())
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="tool_call_completed",
                    tool_call=ToolCall(id="call-timeout", name="timeout_tool"),
                ),
                ModelStreamEvent(
                    kind="message_completed",
                    response=ModelResponse(
                        message=ModelMessage(role="assistant"),
                    ),
                ),
            ],
            [_completed("handled", message_id="msg-timeout")],
        ],
    )
    approval_service = ApprovalService()
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        approval_service=approval_service,
        tool_policy_context=ToolPolicyContext(session_id="session-1"),
    )

    events = [event async for event in runtime.run_turn(_runtime_input())]

    resolved = [
        event for event in events if event.type == "approval.resolved"
    ][0]
    assert resolved.status == "interrupted"
    assert resolved.payload["decision"] == ApprovalDecision.TIMEOUT.value
    assert approval_service.resolved == [
        ("approval-timeout", ApprovalDecision.TIMEOUT),
    ]
    assert not any(event.type == "tool.started" for event in events)
    completed = [
        event for event in events if event.type == "tool.call.completed"
    ][0]
    assert completed.status == "failed"
    assert completed.payload["error"] == "Tool approval timed out."


@pytest.mark.asyncio
async def test_native_runtime_emits_interrupted_event_on_cancel() -> None:
    class CancellingModelClient:
        provider_id = "test"
        model_name = "test-model"

        async def stream(self, request: ModelRequest):
            del request
            raise asyncio.CancelledError()
            # Unreachable yield retains the async-generator protocol.
            # pylint: disable-next=unreachable
            yield  # pragma: no cover

        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError("complete should not be called")

    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: CancellingModelClient(),
        tool_registry=LantaiToolRegistry(),
    )

    events = []
    with pytest.raises(asyncio.CancelledError):
        async for event in runtime.run_turn(_runtime_input()):
            events.append(event)

    assert [event.type for event in events] == [
        "turn.started",
        "turn.interrupted",
    ]


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
async def test_native_runtime_treats_model_error_event_as_failed_turn() -> None:  # noqa: E501
    client = ScriptedModelClient(
        [
            [
                ModelStreamEvent(
                    kind="error",
                    message_id="msg-error",
                    error="provider stream failed",
                ),
            ],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )

    events = []
    with pytest.raises(RuntimeError, match="provider stream failed"):
        async for event in runtime.run_turn(_runtime_input()):
            events.append(event)

    assert [event.type for event in events] == [
        "turn.started",
        "runtime.error",
        "turn.failed",
    ]
    assert events[-1].summary == "provider stream failed"


@pytest.mark.asyncio
# Keep the existing test selector unchanged.
# pylint: disable-next=line-too-long
async def test_native_runtime_model_stream_exception_has_visible_failed_turn() -> None:  # noqa: E501
    class TimeoutModelClient:
        provider_id = "test"
        model_name = "test-model"

        async def stream(self, request: ModelRequest):
            del request
            raise asyncio.TimeoutError()
            # Unreachable yield retains the async-generator protocol.
            # pylint: disable-next=unreachable
            yield  # pragma: no cover

        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError("complete should not be called")

    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: TimeoutModelClient(),
        tool_registry=LantaiToolRegistry(),
    )

    events = []
    with pytest.raises(asyncio.TimeoutError):
        async for event in runtime.run_turn(_runtime_input()):
            events.append(event)

    assert [event.type for event in events] == ["turn.started", "turn.failed"]
    assert events[-1].summary == "Native runtime timed out."
    assert events[-1].payload["error_type"] == "TimeoutError"


def test_runtime_engine_defaults_and_mission_guard() -> None:
    assert RuntimeConfig().engine == "native"
    assert RuntimeConfig(engine="native").engine == "native"

    agent_config = SimpleNamespace(runtime=RuntimeConfig(engine="native"))

    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    assert AgentRunner._runtime_engine_for_agent(agent_config) == "native"
    assert (
        # White-box lock exercises the actual private boundary.
        # pylint: disable-next=protected-access
        AgentRunner._runtime_engine_for_agent(
            SimpleNamespace(runtime=RuntimeConfig(engine="legacy")),
        )
        == "native"
    )
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    assert AgentRunner._runtime_engine_for_agent(SimpleNamespace()) == "native"


@pytest.mark.asyncio
async def test_cancel_at_native_yield_closes_provider_before_interrupted():
    closed = asyncio.Event()
    calls = []

    class Client:
        provider_id = "synthetic"
        model_name = "synthetic"

        def stream(self, request):
            calls.append(request)

            async def events():
                try:
                    yield ModelStreamEvent(
                        kind="text_delta", message_id="owned", delta="partial"
                    )
                    raise AssertionError("cancellation advanced provider work")
                finally:
                    closed.set()

            self.live = events()
            return self.live

    client = Client()
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )
    from lantai.app.runner.runner import _runtime_events_with_terminal_boundary

    stream = _runtime_events_with_terminal_boundary(
        RuntimeRunner(runtime).run_turn(_runtime_input())
    )
    try:
        while (await anext(stream)).type != "message.delta":
            pass
        interrupted = await stream.athrow(asyncio.CancelledError())
        assert interrupted.type == "turn.interrupted"
        assert closed.is_set(), "terminal preceded owned provider cleanup"
        with pytest.raises(asyncio.CancelledError):
            await anext(stream)
        assert len(calls) == 1
    finally:
        await stream.aclose()
        if hasattr(client, "live"):
            await client.live.aclose()
