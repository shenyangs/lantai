# -*- coding: utf-8 -*-
"""Replay the versioned P0 chat incident library through product code."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lantai.app.completion_gate import finalize_deliverable
from lantai.app.experience.runtime_runs import record_chat_runtime_run_started
from lantai.app.works import list_works
from lantai.runtime.adapters.native_runtime import NativeLantaiRuntime
from lantai.runtime.input import RuntimeInput, RuntimeRequestContext
from lantai.runtime.messages import RuntimeMessage
from lantai.runtime.models import (
    ModelContentBlock,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
)
from lantai.runtime.runs import build_chat_task_contract
from lantai.runtime.tools import LantaiToolRegistry
from lantai.runtime.turn_control import RuntimeTurnControlStore


_LIBRARY_PATH = (
    Path(__file__).parents[2]
    / "fixtures"
    / "runtime"
    / "incidents"
    / "p0_incident_library_v1.json"
)


def _library() -> dict[str, Any]:
    return json.loads(_LIBRARY_PATH.read_text(encoding="utf-8"))


def _incident(incident_id: str) -> dict[str, Any]:
    return next(
        incident
        for incident in _library()["incidents"]
        if incident["incident_id"] == incident_id
    )


def test_p0_incident_library_covers_every_mandatory_class_once() -> None:
    library = _library()
    incidents = library["incidents"]
    repository_root = Path(__file__).parents[3]

    assert library["schema_version"] == 1
    assert library["anonymized"] is True
    assert len({item["incident_id"] for item in incidents}) == len(incidents)
    assert {item["incident_class"] for item in incidents} == set(
        library["required_classes"]
    )
    assert all(item["replay_targets"] for item in incidents)
    assert all(
        (repository_root / target).is_file()
        for item in incidents
        for target in item["replay_targets"]
    )


def test_duplicate_submit_replays_only_the_original_run(tmp_path: Path) -> None:
    incident = _incident("duplicate-execution-and-reply-v1")
    case = incident["input"]
    expected = incident["expected"]
    store = RuntimeTurnControlStore(tmp_path)

    original = store.create_receipt(
        client_turn_id=case["client_turn_id"],
        operation="submit",
        run_id=case["first_run_id"],
        attempt_id=case["first_attempt_id"],
        chat_id=case["chat_id"],
        status="accepted",
    )
    retry = store.create_receipt(
        client_turn_id=case["client_turn_id"],
        operation="retry",
        run_id=case["retry_run_id"],
        attempt_id=case["retry_attempt_id"],
        chat_id=case["chat_id"],
        status="accepted",
    )

    receipt_files = list((store.root / "turn-receipts").glob("*.json"))
    assert retry == original
    assert retry.run_id == expected["executed_run_id"]
    assert len(receipt_files) == expected["receipt_count"]


@pytest.mark.asyncio
async def test_false_completion_without_artifact_is_rejected(
    tmp_path: Path,
) -> None:
    incident = _incident("false-completion-without-artifact-v1")
    case = incident["input"]
    expected = incident["expected"]
    contract = build_chat_task_contract(
        objective=case["objective"],
        client_turn_id="incident-client-turn-false-completion",
        complexity="standard",
        work_method_id="standard",
    )
    await record_chat_runtime_run_started(
        workspace_dir=tmp_path,
        agent_id="default",
        chat_id=case["chat_id"],
        session_id=f"console:{case['chat_id']}",
        run_id=case["run_id"],
        effective_workspace_dir=str(tmp_path),
        task_contract=contract,
    )

    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id=case["run_id"],
        chat_id=case["chat_id"],
        response_status=case["response_status"],
        assistant_text=case["assistant_text"],
        final_response_id=case["final_response_id"],
    )

    assert result.allow_success is expected["allow_success"]
    assert result.delivery_status == expected["delivery_status"]
    assert len(list_works(tmp_path)) == expected["works_item_count"]


class _ScriptedModelClient:
    provider_id = "incident-fixture"
    model_name = "incident-fixture-model"

    def __init__(self, scripts: list[list[ModelStreamEvent]]) -> None:
        self.scripts = scripts
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest):
        self.requests.append(request)
        for event in self.scripts.pop(0):
            yield event

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(message=ModelMessage.text("assistant", "done"))


@pytest.mark.asyncio
async def test_empty_first_final_response_recovers_one_visible_answer(
    tmp_path: Path,
) -> None:
    incident = _incident("empty-first-final-response-v1")
    case = incident["input"]
    expected = incident["expected"]
    empty = ModelStreamEvent(
        kind="message_completed",
        message_id=case["empty_message_id"],
        response=ModelResponse(
            message=ModelMessage(
                id=case["empty_message_id"],
                role="assistant",
                content=[],
            )
        ),
    )
    recovered = ModelStreamEvent(
        kind="message_completed",
        message_id=case["recovery_message_id"],
        response=ModelResponse(
            message=ModelMessage(
                id=case["recovery_message_id"],
                role="assistant",
                content=[
                    ModelContentBlock(type="text", text=case["recovery_text"])
                ],
            )
        ),
    )
    client = _ScriptedModelClient([[empty], [recovered]])
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=LantaiToolRegistry(),
    )
    runtime_input = RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id=f"console:{case['chat_id']}",
            user_id="incident-user",
            run_id=case["run_id"],
            workspace_dir=str(tmp_path),
        ),
        messages=[RuntimeMessage.text("user", "在吗？")],
    )

    events = [event async for event in runtime.run_turn(runtime_input)]
    completed = [event for event in events if event.type == "message.completed"]

    assert len(client.requests) == expected["model_request_count"]
    assert len(completed) == expected["visible_completed_count"]
    assert completed[0].summary == case["recovery_text"]
    assert events[-1].type == expected["final_event_type"]
