# -*- coding: utf-8 -*-
"""End-to-end behavior locks for the extracted Agent Loop layers."""

from pathlib import Path

import pytest

from lantai.runtime.adapters.native_runtime import NativeLantaiRuntime
from lantai.runtime.input import RuntimeInput, RuntimeRequestContext
from lantai.runtime.messages import RuntimeMessage
from lantai.runtime.models import (
    ModelContentBlock,
    ModelMessage,
    ModelResponse,
    ModelStreamEvent,
)
from lantai.runtime.tools import (
    LantaiToolRegistry,
    ToolPolicyContext,
    ToolSpec,
)


class TwoRoundClient:
    provider_id = "provider-a"
    model_name = "model-a"

    def __init__(self) -> None:
        self.requests = []

    async def stream(self, request):
        self.requests.append(request.model_copy(deep=True))
        if len(self.requests) == 1:
            yield ModelStreamEvent(
                kind="message_completed",
                response=ModelResponse(
                    message=ModelMessage(
                        role="assistant",
                        content=[
                            ModelContentBlock(
                                type="tool_call",
                                data={
                                    "id": "call-1",
                                    "name": "read_marker",
                                    "arguments": {},
                                },
                            ),
                        ],
                    ),
                    finish_reason="tool_calls",
                ),
            )
            return
        yield ModelStreamEvent(
            kind="message_completed",
            response=ModelResponse(
                message=ModelMessage.text("assistant", "done"),
                finish_reason="stop",
            ),
        )

    async def complete(self, _request):
        raise AssertionError("main loop must use streaming")


class DriftingModelClient(TwoRoundClient):
    async def stream(self, request):
        async for event in super().stream(request):
            yield event
        if len(self.requests) == 1:
            self.model_name = "model-b"


async def test_native_requests_consume_one_stable_effective_lineage(
    tmp_path: Path,
) -> None:
    client = TwoRoundClient()
    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(name="read_marker", read_only=True),
        lambda: "marker",
    )
    policy = ToolPolicyContext(
        agent_id="default",
        session_id="session-1",
        turn_id="run-1",
        workspace_dir=str(tmp_path),
        workspace_access_mode="read_only",
        workspace_trust_state="trusted",
    )
    runtime_input = RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id="session-1",
            run_id="run-1",
            workspace_dir=str(tmp_path),
            metadata={
                "attempt_id": "attempt-1",
                "runtime_generation": 2,
                "runtime_revision": 7,
                "contract_hash": "a" * 64,
                "policy_snapshot_hash": "b" * 64,
            },
        ),
        messages=[RuntimeMessage.text("user", "read the marker")],
        tool_policy_context=policy.model_dump(mode="json"),
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        tool_policy_context=policy,
        max_iters=4,
    )

    events = [event async for event in runtime.run_turn(runtime_input)]

    assert events[-1].type == "turn.completed"
    assert len(client.requests) == 2
    lineage_ids = [
        request.metadata["model_round_lineage_sha256"]
        for request in client.requests
    ]
    assert lineage_ids[0] == lineage_ids[1]
    assert len(lineage_ids[0]) == 64
    assert client.requests[0].metadata["attempt_id"] == "attempt-1"
    assert client.requests[0].metadata["policy_snapshot_hash"] == "b" * 64


async def test_model_identity_drift_fails_before_another_provider_request(
    tmp_path: Path,
) -> None:
    client = DriftingModelClient()
    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(name="read_marker", read_only=True),
        lambda: "marker",
    )
    policy = ToolPolicyContext(
        agent_id="default",
        session_id="session-1",
        turn_id="run-1",
        workspace_dir=str(tmp_path),
        workspace_access_mode="read_only",
        workspace_trust_state="trusted",
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        tool_policy_context=policy,
        max_iters=4,
    )
    runtime_input = RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id="session-1",
            run_id="run-1",
            workspace_dir=str(tmp_path),
        ),
        messages=[RuntimeMessage.text("user", "read the marker")],
        tool_policy_context=policy.model_dump(mode="json"),
    )
    events = []

    with pytest.raises(ValueError, match="model slot"):
        async for event in runtime.run_turn(runtime_input):
            events.append(event)

    assert len(client.requests) == 1
    assert events[-1].type == "turn.failed"
