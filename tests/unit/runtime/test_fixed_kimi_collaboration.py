# -*- coding: utf-8 -*-
"""Lock the K2.7 wire contract and the required collaboration it must preserve."""

import json
from types import SimpleNamespace

import pytest

from lantai.providers.model_endpoint import ProviderModelEndpoint
from lantai.providers.reasoning import (
    ReasoningRequestOptions,
    reset_reasoning_options,
    set_reasoning_options,
)
from lantai.runtime.adapters.native_runtime import NativeLantaiRuntime
from lantai.runtime.adapters.openai_model_client import (
    OpenAICompatibleModelClient,
)
from lantai.runtime.input import RuntimeInput, RuntimeRequestContext
from lantai.runtime.messages import RuntimeMessage
from lantai.runtime.models import ModelMessage, ModelRequest
from lantai.runtime.runs import build_chat_task_contract
from lantai.runtime.tool_response import TextBlock, ToolResponse
from lantai.runtime.tools import LantaiToolRegistry, ToolSpec


class FakeStream:
    def __init__(self, chunks):
        self.chunks = chunks

    def __aiter__(self):
        self.iterator = iter(self.chunks)
        return self

    async def __anext__(self):
        try:
            return next(self.iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


def _chunk(*, text=None, thinking=None, tool_call=None, finish_reason="stop"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                index=0,
                delta=SimpleNamespace(
                    content=text,
                    reasoning_content=thinking,
                    tool_calls=[tool_call] if tool_call else None,
                ),
                finish_reason=finish_reason,
            )
        ],
        usage=None,
    )


class KimiGateway:
    """Reject the same invalid parameter combination as the actual gateway."""

    def __init__(self, streams):
        self.streams = list(streams)
        self.calls = []

    async def create(self, **payload):
        self.calls.append(payload)
        if payload.get("tool_choice") == "required":
            raise RuntimeError(
                'data: {"error":{"code":"invalid_parameter_error",'
                '"message":"The tool_choice parameter does not support being '
                'set to required or object in thinking mode",'
                '"type":"invalid_request_error"}}'
            )
        assert (
            payload.get("extra_body", {}).get("thinking", {}).get("type")
            != "disabled"
        )
        return self.streams.pop(0)


class RecordingKimiClient(OpenAICompatibleModelClient):
    def __init__(self, gateway, model="kimi-k2.7-code"):
        super().__init__(
            ProviderModelEndpoint(
                provider_id="tokendance",
                model_id=model,
                base_url="https://gateway.example/v1",
                api_key="sk-test",
                native_kind="openai_compatible",
                generate_kwargs={"extra_body": {"thinking": {"type": "enabled"}}},
            )
        )
        self._client = SimpleNamespace(
            chat=SimpleNamespace(completions=gateway)
        )
        self.requests = []
        self.responses = []

    async def stream(self, request):
        self.requests.append(request)
        async for event in super().stream(request):
            if event.response is not None:
                self.responses.append(event.response)
            yield event


@pytest.mark.parametrize("reasoning_enabled", [False, True])
async def test_fixed_kimi_required_tool_wire_keeps_thinking_and_request_contract(
    reasoning_enabled,
):
    gateway = KimiGateway(
        [FakeStream([_chunk(text="done", thinking="wire-state")])]
    )
    client = RecordingKimiClient(gateway)
    request = ModelRequest(
        messages=[ModelMessage.text("user", "use collaboration")],
        tools=[
            ToolSpec(
                name="collaborate_with_agents", input_schema={"type": "object"}
            )
        ],
        tool_choice="required",
    )
    token = set_reasoning_options(
        ReasoningRequestOptions(enabled=reasoning_enabled)
    )
    try:
        events = [event async for event in client.stream(request)]
    finally:
        reset_reasoning_options(token)

    assert len(gateway.calls) == 1
    assert gateway.calls[0]["tool_choice"] == "auto"
    assert request.tool_choice == "required"
    response = events[-1].response
    status = response.message.metadata["reasoning_status"]
    assert status["effective_enabled"] is True
    assert status["returned"] is True
    assert status["effective_tool_choice"] == "auto"


@pytest.mark.parametrize("invoke_collaboration", [False, True])
@pytest.mark.parametrize("model", ["kimi-k2.7-code", "unlisted-model-2030"])
async def test_fixed_kimi_wire_fallback_cannot_skip_required_collaboration(
    tmp_path,
    invoke_collaboration,
    model,
):
    invocations = []

    async def collaborate_with_agents(tasks):
        invocations.append(tasks)
        yield ToolResponse(
            content=[TextBlock(text="hosted batch complete")],
            metadata={
                "collaboration": {
                    "batch_id": "batch-required",
                    "revision": 1,
                    "status": "completed",
                    "phase": "closed",
                    "members": [
                        {
                            "member_id": "member-a",
                            "agent_id": "agent-a",
                            "status": "completed",
                        },
                        {
                            "member_id": "member-b",
                            "agent_id": "agent-b",
                            "status": "completed",
                        },
                    ],
                }
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
    tasks = [
        {"to_agent": "agent-a", "text": "甲", "label": "甲"},
        {"to_agent": "agent-b", "text": "乙", "label": "乙"},
    ]
    tool_call = SimpleNamespace(
        index=0,
        id="call-required-batch",
        function=SimpleNamespace(
            name="collaborate_with_agents",
            arguments=json.dumps({"tasks": tasks}),
        ),
    )
    streams = [
        FakeStream([_chunk(text="premature answer", thinking="first-state")])
    ]
    if invoke_collaboration:
        streams.extend(
            [
                FakeStream(
                    [
                        _chunk(
                            thinking="collaboration-state",
                            tool_call=tool_call,
                            finish_reason="tool_calls",
                        )
                    ]
                ),
                FakeStream(
                    [
                        _chunk(
                            text="collaboration delivered",
                            thinking="final-state",
                        )
                    ]
                ),
            ]
        )
    else:
        streams.append(FakeStream([_chunk(text="another premature answer")]))
    gateway = KimiGateway(streams)
    client = RecordingKimiClient(gateway, model=model)
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client, tool_registry=registry
    )
    contract = build_chat_task_contract(
        objective="通过协作形成交付",
        client_turn_id="client-kimi-collaboration",
        complexity="standard",
        work_method_id="collaboration",
    )
    runtime_input = RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id="session-kimi",
            user_id="test-user",
            run_id="turn-kimi",
            workspace_dir=str(tmp_path),
        ),
        messages=[RuntimeMessage.text("user", "通过协作形成交付")],
        metadata={"task_contract": contract.model_dump(mode="json")},
    )
    events = []
    if invoke_collaboration:
        events = [event async for event in runtime.run_turn(runtime_input)]
    else:
        with pytest.raises(RuntimeError, match="required capability"):
            async for event in runtime.run_turn(runtime_input):
                events.append(event)

    assert all(
        request.tool_choice == "required" for request in client.requests[:2]
    )
    rejected_count = 1 if model == "unlisted-model-2030" else 0
    accepted_calls = gateway.calls[rejected_count:]
    assert all(
        call.get("tool_choice", "auto") == "auto" for call in accepted_calls[:2]
    )
    assert all(
        [tool["function"]["name"] for tool in call["tools"]]
        == ["collaborate_with_agents"]
        for call in accepted_calls[:2]
    )
    delivered = [
        event.summary for event in events if event.type == "message.completed"
    ]
    if invoke_collaboration:
        assert len(gateway.calls) == 3 + rejected_count
        assert len(invocations) == 1
        assert delivered == ["collaboration delivered"]
        replay = next(
            message
            for message in accepted_calls[2]["messages"]
            if message.get("tool_calls")
        )
        assert replay["reasoning_content"] == "collaboration-state"
    else:
        assert len(gateway.calls) == 2 + rejected_count
        assert invocations == []
        assert delivered == []
        assert events[-1].type == "turn.failed"
        assert events[-1].payload["error_code"] == "required_capability_failed"
    if rejected_count:
        assert gateway.calls[0]["tool_choice"] == "required"
        assert (
            client.responses[0].message.metadata["tool_choice_compatibility"][
                "recovery_attempts"
            ]
            == 1
        )
        assert (
            client.responses[1].message.metadata["tool_choice_compatibility"]["source"]
            == "client_cache"
        )
