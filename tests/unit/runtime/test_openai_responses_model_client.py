# -*- coding: utf-8 -*-
"""Behavior contracts for the native OpenAI Responses adapter."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lantai.providers.model_endpoint import ProviderModelEndpoint
from lantai.runtime.instruction_trust import project_untrusted_tool_evidence
from lantai.runtime.adapters.openai_responses_model_client import (
    OpenAIResponsesModelClient,
)
from lantai.runtime.models import ModelContentBlock, ModelMessage, ModelRequest
from lantai.runtime.tools import ToolSpec


def test_responses_transport_disables_hidden_sdk_retries() -> None:
    endpoint = ProviderModelEndpoint(
        provider_id="opencode",
        model_id="gpt-5.6-sol",
        base_url="https://opencode.ai/zen/v1",
        api_key="test-key",
        native_kind="openai_responses",
    )

    client = OpenAIResponsesModelClient(endpoint)

    assert client._client.max_retries == 0  # pylint: disable=protected-access


async def test_responses_client_streams_visible_text_and_final_message() -> None:
    endpoint = ProviderModelEndpoint(
        provider_id="opencode",
        model_id="gpt-5.6-sol",
        base_url="https://opencode.ai/zen/v1",
        api_key="test-key",
        native_kind="openai_responses",
    )
    client = OpenAIResponsesModelClient(endpoint)
    captured = {}

    class FakeResponses:
        async def create(self, **payload):
            captured.update(payload)

            async def stream():
                yield SimpleNamespace(
                    type="response.output_text.delta",
                    delta="hello",
                )
                yield SimpleNamespace(
                    type="response.completed",
                    response=SimpleNamespace(
                        status="completed",
                        usage=SimpleNamespace(
                            input_tokens=3,
                            output_tokens=1,
                            total_tokens=4,
                        ),
                    ),
                )

            return stream()

    client._client = SimpleNamespace(responses=FakeResponses())
    request = ModelRequest(messages=[ModelMessage.text("user", "say hello")])

    events = [event async for event in client.stream(request)]

    assert captured["model"] == "gpt-5.6-sol"
    assert captured["input"] == [{"role": "user", "content": "say hello"}]
    assert captured["stream"] is True
    assert [event.delta for event in events if event.kind == "text_delta"] == [
        "hello",
    ]
    completed = next(event for event in events if event.kind == "message_completed")
    assert completed.response is not None
    assert completed.response.message.text_content() == "hello"
    assert completed.usage is not None
    assert completed.usage.total_tokens == 4


async def test_responses_client_preserves_function_call_transaction() -> None:
    endpoint = ProviderModelEndpoint(
        provider_id="opencode",
        model_id="gpt-5.6-sol",
        base_url="https://opencode.ai/zen/v1",
        api_key="test-key",
        native_kind="openai_responses",
    )
    client = OpenAIResponsesModelClient(endpoint)
    captured = {}

    class FakeResponses:
        async def create(self, **payload):
            captured.update(payload)

            async def stream():
                yield SimpleNamespace(
                    type="response.output_item.added",
                    item=SimpleNamespace(
                        type="function_call",
                        id="fc_1",
                        call_id="call_1",
                        name="lookup",
                        arguments="",
                    ),
                )
                yield SimpleNamespace(
                    type="response.function_call_arguments.delta",
                    item_id="fc_1",
                    delta='{"city":"',
                )
                yield SimpleNamespace(
                    type="response.function_call_arguments.delta",
                    item_id="fc_1",
                    delta='Shanghai"}',
                )
                yield SimpleNamespace(
                    type="response.output_item.done",
                    item=SimpleNamespace(
                        type="function_call",
                        id="fc_1",
                        call_id="call_1",
                        name="lookup",
                        arguments='{"city":"Shanghai"}',
                    ),
                )
                yield SimpleNamespace(
                    type="response.completed",
                    response=SimpleNamespace(status="completed", usage=None),
                )

            return stream()

    client._client = SimpleNamespace(responses=FakeResponses())
    request = ModelRequest(
        messages=[ModelMessage.text("user", "check weather")],
        tools=[
            ToolSpec(
                name="lookup",
                description="Look up weather",
                input_schema={
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            ),
        ],
    )

    events = [event async for event in client.stream(request)]

    assert captured["tools"] == [
        {
            "type": "function",
            "name": "lookup",
            "description": "Look up weather",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    ]
    completed_call = next(
        event.tool_call
        for event in events
        if event.kind == "tool_call_completed"
    )
    assert completed_call is not None
    assert completed_call.id == "call_1"
    assert completed_call.name == "lookup"
    assert completed_call.arguments == {"city": "Shanghai"}
    completed = next(event for event in events if event.kind == "message_completed")
    assert completed.response is not None
    assert completed.response.message.content[0].type == "tool_call"


async def test_responses_client_replays_tool_call_and_result_as_response_items() -> None:
    endpoint = ProviderModelEndpoint(
        provider_id="opencode",
        model_id="gpt-5.6-sol",
        base_url="https://opencode.ai/zen/v1",
        api_key="test-key",
        native_kind="openai_responses",
    )
    client = OpenAIResponsesModelClient(endpoint)
    captured = {}

    class FakeResponses:
        async def create(self, **payload):
            captured.update(payload)

            async def stream():
                yield SimpleNamespace(
                    type="response.completed",
                    response=SimpleNamespace(status="completed", usage=None),
                )

            return stream()

    client._client = SimpleNamespace(responses=FakeResponses())
    assistant_call = ModelMessage(
        role="assistant",
        content=[
            ModelContentBlock(
                type="tool_call",
                data={
                    "id": "call_1",
                    "name": "lookup",
                    "arguments": {"city": "Shanghai"},
                },
            ),
        ],
    )
    tool_result = ModelMessage(
        role="tool",
        tool_call_id="call_1",
        content=[
            ModelContentBlock(
                type="tool_result",
                data={
                    "id": "call_1",
                    "name": "lookup",
                    "output": [{"type": "text", "text": "sunny"}],
                },
            ),
        ],
    )

    events = [
        event
        async for event in client.stream(
            ModelRequest(
                messages=[
                    ModelMessage.text("user", "check weather"),
                    assistant_call,
                    tool_result,
                ],
            )
        )
    ]

    assert captured["input"] == [
        {"role": "user", "content": "check weather"},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "lookup",
            "arguments": '{"city":"Shanghai"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": project_untrusted_tool_evidence("lookup", "sunny"),
        },
    ]
    assert any(event.kind == "message_completed" for event in events)


async def test_responses_client_preserves_opaque_reasoning_for_follow_up() -> None:
    endpoint = ProviderModelEndpoint(
        provider_id="opencode",
        model_id="gpt-5.6-sol",
        base_url="https://opencode.ai/zen/v1",
        api_key="test-key",
        native_kind="openai_responses",
    )
    client = OpenAIResponsesModelClient(endpoint)
    captured_requests = []
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [
            {"type": "summary_text", "text": "Need to inspect the evidence."},
        ],
        "encrypted_content": "opaque-provider-state",
    }

    class ReasoningItem:
        type = "reasoning"

        @staticmethod
        def model_dump():
            return dict(reasoning_item)

    class FakeResponses:
        async def create(self, **payload):
            captured_requests.append(payload)

            async def first_stream():
                yield SimpleNamespace(
                    type="response.output_item.done",
                    item=ReasoningItem(),
                )
                yield SimpleNamespace(
                    type="response.completed",
                    response=SimpleNamespace(status="completed", usage=None),
                )

            async def second_stream():
                yield SimpleNamespace(
                    type="response.completed",
                    response=SimpleNamespace(status="completed", usage=None),
                )

            return first_stream() if len(captured_requests) == 1 else second_stream()

    client._client = SimpleNamespace(responses=FakeResponses())
    first_events = [
        event
        async for event in client.stream(
            ModelRequest(messages=[ModelMessage.text("user", "investigate")])
        )
    ]
    first_response = next(
        event.response
        for event in first_events
        if event.kind == "message_completed"
    )
    assert first_response is not None
    assert first_response.message.content[0].type == "thinking"
    assert (
        first_response.message.content[0].data["openai_response_item"]
        == reasoning_item
    )

    _ = [
        event
        async for event in client.stream(
            ModelRequest(
                messages=[
                    ModelMessage.text("user", "investigate"),
                    first_response.message,
                    ModelMessage.text("user", "continue"),
                ]
            )
        )
    ]

    assert captured_requests[1]["input"] == [
        {"role": "user", "content": "investigate"},
        reasoning_item,
        {"role": "user", "content": "continue"},
    ]


async def test_responses_client_projects_text_and_image_as_multimodal_input() -> None:
    endpoint = ProviderModelEndpoint(
        provider_id="opencode",
        model_id="gpt-5.6-sol",
        base_url="https://opencode.ai/zen/v1",
        api_key="test-key",
        native_kind="openai_responses",
    )
    client = OpenAIResponsesModelClient(endpoint)
    captured = {}

    class FakeResponses:
        async def create(self, **payload):
            captured.update(payload)

            async def stream():
                yield SimpleNamespace(
                    type="response.completed",
                    response=SimpleNamespace(status="completed", usage=None),
                )

            return stream()

    client._client = SimpleNamespace(responses=FakeResponses())
    message = ModelMessage(
        role="user",
        content=[
            ModelContentBlock(type="text", text="describe this"),
            ModelContentBlock(
                type="image",
                data={"url": "https://example.test/photo.png"},
            ),
        ],
    )

    _ = [
        event
        async for event in client.stream(ModelRequest(messages=[message]))
    ]

    assert captured["input"] == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "describe this"},
                {
                    "type": "input_image",
                    "image_url": "https://example.test/photo.png",
                },
            ],
        },
    ]


async def test_responses_client_surfaces_decoded_nonsemantic_provider_activity() -> None:
    endpoint = ProviderModelEndpoint(
        provider_id="opencode",
        model_id="gpt-5.6-sol",
        base_url="https://opencode.ai/zen/v1",
        api_key="test-key",
        native_kind="openai_responses",
    )
    client = OpenAIResponsesModelClient(endpoint)

    class FakeResponses:
        async def create(self, **_payload):
            async def stream():
                yield SimpleNamespace(
                    type="response.in_progress",
                )
                yield SimpleNamespace(
                    type="response.output_text.delta",
                    delta="",
                )
                yield SimpleNamespace(
                    type="response.completed",
                    response=SimpleNamespace(status="completed", usage=None),
                )

            return stream()

    client._client = SimpleNamespace(responses=FakeResponses())

    events = [
        event
        async for event in client.stream(
            ModelRequest(messages=[ModelMessage.text("user", "keep going")])
        )
    ]

    activity = [event for event in events if event.kind == "provider_activity"]
    assert [event.metadata["provider_event_type"] for event in activity] == [
        "response.in_progress",
        "response.output_text.delta",
        "response.completed",
    ]
    assert all(event.metadata["semantic_output"] is False for event in activity)
    assert not any(event.kind == "text_delta" for event in events)
    assert events[-1].kind == "message_completed"


async def test_responses_client_rejects_eof_without_protocol_terminal() -> None:
    endpoint = ProviderModelEndpoint(
        provider_id="opencode",
        model_id="gpt-5.6-sol",
        base_url="https://opencode.ai/zen/v1",
        api_key="test-key",
        native_kind="openai_responses",
    )
    client = OpenAIResponsesModelClient(endpoint)

    class FakeResponses:
        async def create(self, **_payload):
            async def disconnected_stream():
                yield SimpleNamespace(
                    type="response.output_text.delta",
                    delta="partial",
                )

            return disconnected_stream()

    client._client = SimpleNamespace(responses=FakeResponses())

    with pytest.raises(RuntimeError, match="protocol terminal"):
        _ = [
            event
            async for event in client.stream(
                ModelRequest(messages=[ModelMessage.text("user", "continue")])
            )
        ]
