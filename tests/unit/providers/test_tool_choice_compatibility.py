# -*- coding: utf-8 -*-
"""Exercise unknown-model negotiation through both real OpenAI adapters."""

from copy import deepcopy
import asyncio
from types import SimpleNamespace

import pytest

from lantai.providers.model_endpoint import ProviderModelEndpoint
from lantai.providers.compatibility_probe import run_model_compatibility_probe
from lantai.providers.openai_chat_model_compat import OpenAIChatModelCompat
from lantai.runtime.adapters.openai_model_client import OpenAICompatibleModelClient
from lantai.runtime.models import ModelMessage, ModelRequest, ModelContentBlock
from lantai.runtime.tools import ToolSpec

REJECTION = {
    "code": "invalid_parameter_error",
    "type": "invalid_request_error",
    "message": "The tool_choice parameter does not support being set to required or object in thinking mode",
}


class ParameterError(Exception):
    def __init__(self, *, status=400, body=None, message=None):
        self.status_code = status
        self.body = {"error": dict(REJECTION)} if body is None else body
        super().__init__(message or str(self.body))


class Stream:
    def __init__(self, items):
        self.items = items
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True

    def __aiter__(self):
        self.iterator = iter(self.items)
        return self

    async def __anext__(self):
        try:
            item = next(self.iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc
        if isinstance(item, Exception):
            raise item
        return item


def chunk(*, text=None, thinking=None, tool=None, usage=None, finish=None):
    return SimpleNamespace(
        id="test-response",
        choices=[
            SimpleNamespace(
                index=0,
                delta=SimpleNamespace(
                    role="assistant",
                    content=text,
                    reasoning_content=thinking,
                    tool_calls=[tool] if tool else None,
                ),
                finish_reason=finish,
            )
        ],
        usage=usage,
    )


def success():
    return Stream([chunk(thinking="preserved-state", text="done", finish="stop")])


class Gateway:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def create(self, **payload):
        self.calls.append(deepcopy(payload))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def adapter(
    kind, gateway, *, model="unlisted-model-2030", base="https://gateway.example/v1"
):
    client = SimpleNamespace(
        base_url=base,
        chat=SimpleNamespace(completions=gateway),
    )
    generation = {"extra_body": {"thinking": {"type": "enabled"}}}
    if kind == "native":
        model_client = OpenAICompatibleModelClient(
            ProviderModelEndpoint(
                provider_id="custom-gateway",
                model_id=model,
                base_url=base,
                api_key="sk-test",
                generate_kwargs=generation,
            )
        )
        model_client._client = client
    else:
        model_client = OpenAIChatModelCompat(
            model,
            api_key="sk-test",
            generate_kwargs=generation,
        )
        model_client.client = client
    return model_client


async def collect(
    kind, client, *, required=True, has_tools=True, generation=None, model=None
):
    choice = "required" if required else "auto"
    if kind == "native":
        request = ModelRequest(
            messages=[
                ModelMessage(
                    role="assistant",
                    content=[
                        ModelContentBlock(type="thinking", text="prior-state"),
                        ModelContentBlock(type="text", text="prior"),
                    ],
                ),
                ModelMessage.text("user", "use the available tool"),
            ],
            tools=(
                [
                    ToolSpec(
                        name="collaborate_with_agents", input_schema={"type": "object"}
                    )
                ]
                if has_tools
                else []
            ),
            tool_choice=choice,
            generation=generation or {},
            model=model,
        )
        events = [event async for event in client.stream(request)]
        assert request.tool_choice == choice
        return events[-1].response.message.metadata
    response = await client(
        [
            {
                "role": "assistant",
                "content": "prior",
                "reasoning_content": "prior-state",
            },
            {"role": "user", "content": "use the available tool"},
        ],
        tools=(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "collaborate_with_agents",
                        "parameters": {"type": "object"},
                    },
                }
            ]
            if has_tools
            else []
        ),
        tool_choice=choice,
        **(generation or {}),
    )
    events = [event async for event in response]
    return getattr(events[-1], "metadata", {})


@pytest.mark.parametrize("kind", ["native", "legacy"])
@pytest.mark.parametrize(
    "model", ["unlisted-model-2030", "vendor/future-build", "private-alias"]
)
@pytest.mark.parametrize("rejection_at", ["http", "sse"])
async def test_unknown_model_recovers_once_and_remembers_only_after_success(
    kind, model, rejection_at
):
    error = (
        ParameterError()
        if rejection_at == "http"
        else RuntimeError(
            'data: {"error":{"code":"invalid_parameter_error","type":"invalid_request_error",'
            '"message":"tool_choice required is not supported in thinking mode"}}'
        )
    )
    failed_stream = Stream([chunk(), error])
    gateway = Gateway(
        [error if rejection_at == "http" else failed_stream, success(), success()]
    )
    client = adapter(kind, gateway, model=model)

    metadata = await collect(kind, client)
    await collect(kind, client)

    assert len(gateway.calls) == 3
    assert gateway.calls[0]["tool_choice"] == "required"
    assert all(call.get("tool_choice", "auto") == "auto" for call in gateway.calls[1:])
    original = {
        key: value for key, value in gateway.calls[0].items() if key != "tool_choice"
    }
    assert all(
        {key: value for key, value in call.items() if key != "tool_choice"} == original
        for call in gateway.calls[1:]
    )
    assert gateway.calls[1]["messages"][0]["reasoning_content"] == "prior-state"
    receipt = metadata["tool_choice_compatibility"]
    assert receipt["requested_tool_choice"] == "required"
    assert receipt["effective_tool_choice"] == "auto"
    assert receipt["recovery_attempts"] == 1
    assert "message" not in receipt and "error" not in receipt
    if rejection_at == "sse":
        assert failed_stream.closed


@pytest.mark.parametrize("kind", ["native", "legacy"])
@pytest.mark.parametrize("code", ["unsupported_parameter", "unsupported_value"])
@pytest.mark.parametrize("rejection_at", ["http", "sse"])
async def test_structured_tool_choice_rejection_needs_no_english_message(
    kind, code, rejection_at
):
    error = ParameterError(
        status=400 if rejection_at == "http" else None,
        body={
            "error": {
                "code": code,
                "param": "tool_choice",
                "message": "当前模式仅支持自动选择工具",
            }
        },
    )
    gateway = Gateway([error if rejection_at == "http" else Stream([error]), success()])
    receipt = (await collect(kind, adapter(kind, gateway)))["tool_choice_compatibility"]
    assert receipt["recovery_attempts"] == 1
    assert len(gateway.calls) == 2
    assert gateway.calls[0]["tool_choice"] == "required"
    assert "tool_choice" not in gateway.calls[1]


@pytest.mark.parametrize("kind", ["native", "legacy"])
@pytest.mark.parametrize(
    "error",
    [
        ParameterError(status=401),
        ParameterError(status=403),
        ParameterError(status=429),
        ParameterError(status=500),
        TimeoutError("tool_choice required is not supported"),
        RuntimeError("tool_choice required is not supported"),
        ParameterError(
            body={
                "error": {
                    "type": "invalid_request_error",
                    "message": "tool_choice required; malformed tools schema",
                }
            }
        ),
        ParameterError(
            body={
                "error": {
                    "type": "invalid_request_error",
                    "message": "tool_choice must be an object containing a valid function",
                }
            }
        ),
        ParameterError(
            body={
                "error": {
                    "type": "invalid_request_error",
                    "message": "tool_choice only supports objects containing a valid function",
                }
            }
        ),
        ParameterError(
            body={
                "error": {
                    "type": "invalid_request_error",
                    "param": "tool_choice",
                    "message": "only supports objects containing a valid function",
                }
            }
        ),
        ParameterError(
            body={
                "error": {
                    "code": "unsupported_parameter",
                    "param": "tools",
                    "message": "不支持此参数",
                }
            }
        ),
        ParameterError(
            body={
                "error": {
                    "code": "unsupported_value",
                    "param": "functions",
                    "message": "不支持此参数值",
                }
            }
        ),
    ],
)
async def test_unrelated_errors_are_never_compatibility_retried(kind, error):
    gateway = Gateway([error, success()])
    with pytest.raises(type(error)):
        await collect(kind, adapter(kind, gateway))
    assert len(gateway.calls) == 1


@pytest.mark.parametrize("kind", ["native", "legacy"])
@pytest.mark.parametrize(
    "first",
    [
        chunk(text="partial"),
        chunk(thinking="partial-state"),
        chunk(
            tool=SimpleNamespace(
                index=0,
                id="partial-call",
                function=SimpleNamespace(name="collaborate_with_agents", arguments="{"),
            )
        ),
        chunk(
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=0, total_tokens=1)
        ),
        chunk(finish="stop"),
    ],
)
async def test_error_after_any_model_output_is_never_retried(kind, first):
    gateway = Gateway([Stream([first, ParameterError(status=None)]), success()])
    with pytest.raises(ParameterError):
        await collect(kind, adapter(kind, gateway))
    assert len(gateway.calls) == 1


@pytest.mark.parametrize("kind", ["native", "legacy"])
async def test_recovery_failure_is_bounded_and_does_not_poison_next_call(kind):
    gateway = Gateway([ParameterError(), ParameterError(), success()])
    client = adapter(kind, gateway)
    with pytest.raises(ParameterError):
        await collect(kind, client)
    assert len(gateway.calls) == 2
    await collect(kind, client)
    assert gateway.calls[-1]["tool_choice"] == "required"


@pytest.mark.parametrize("kind", ["native", "legacy"])
async def test_observed_capability_is_scoped_to_actual_thinking_mode(kind):
    gateway = Gateway([ParameterError(), success(), success()])
    client = adapter(kind, gateway)
    await collect(kind, client)
    await collect(
        kind, client, generation={"extra_body": {"thinking": {"type": "disabled"}}}
    )
    assert gateway.calls[-1]["tool_choice"] == "required"


@pytest.mark.parametrize("kind", ["native", "legacy"])
@pytest.mark.parametrize("required,has_tools", [(False, True), (True, False)])
async def test_only_forced_choice_with_available_tools_is_eligible(
    kind, required, has_tools
):
    gateway = Gateway([ParameterError(), success()])
    with pytest.raises(ParameterError):
        await collect(
            kind, adapter(kind, gateway), required=required, has_tools=has_tools
        )
    assert len(gateway.calls) == 1


@pytest.mark.parametrize("kind", ["native", "legacy"])
@pytest.mark.parametrize("different", ["model", "endpoint"])
async def test_observations_do_not_cross_models_or_endpoints(kind, different):
    gateway = Gateway([ParameterError(), success(), success()])
    client = adapter(kind, gateway)
    await collect(kind, client)
    if different == "model":
        if kind == "legacy":
            client.model_name = "other-unlisted-model"
        await collect(kind, client, model="other-unlisted-model")
    else:
        if kind == "legacy":
            client.client.base_url = "https://other-gateway.example/v1"
        else:
            client.endpoint = ProviderModelEndpoint(
                provider_id="other-gateway",
                model_id="unlisted-model-2030",
                base_url="https://other-gateway.example/v1",
                api_key="sk-test",
                generate_kwargs={"extra_body": {"thinking": {"type": "enabled"}}},
            )
        await collect(kind, client)
    assert gateway.calls[-1]["tool_choice"] == "required"


@pytest.mark.parametrize("kind", ["native", "legacy"])
async def test_recovered_but_unfinished_stream_does_not_teach_capability(kind):
    gateway = Gateway([ParameterError(), Stream([chunk(text="partial")]), success()])
    client = adapter(kind, gateway)
    if kind == "native":
        with pytest.raises(RuntimeError, match="terminal marker"):
            await collect(kind, client)
    else:
        await collect(kind, client)
    await collect(kind, client)
    assert gateway.calls[-1]["tool_choice"] == "required"


@pytest.mark.parametrize("kind", ["native", "legacy"])
async def test_cancellation_closes_stream_and_never_retries(kind):
    class CancelledStream(Stream):
        async def __anext__(self):
            raise asyncio.CancelledError

    cancelled = CancelledStream([])
    gateway = Gateway([cancelled, success()])
    client = adapter(kind, gateway)
    with pytest.raises(asyncio.CancelledError):
        await collect(kind, client)
    assert cancelled.closed
    assert len(gateway.calls) == 1
    await collect(kind, client)
    assert gateway.calls[-1]["tool_choice"] == "required"


@pytest.mark.parametrize("kind", ["native", "legacy"])
async def test_consumer_stop_closes_recovered_stream_without_teaching_capability(kind):
    recovered = success()
    gateway = Gateway([ParameterError(), recovered, success()])
    client = adapter(kind, gateway)
    if kind == "native":
        stream = client.stream(
            ModelRequest(
                messages=[ModelMessage.text("user", "use tools")],
                tools=[
                    ToolSpec(
                        name="collaborate_with_agents", input_schema={"type": "object"}
                    )
                ],
                tool_choice="required",
            )
        )
        assert (await stream.__anext__()).kind == "message_start"
        assert (await stream.__anext__()).kind == "thinking_delta"
    else:
        stream = await client(
            [{"role": "user", "content": "use tools"}],
            tools=[
                {"type": "function", "function": {"name": "collaborate_with_agents"}}
            ],
            tool_choice="required",
        )
        await stream.__anext__()
    await stream.aclose()
    assert recovered.closed
    assert len(gateway.calls) == 2
    await collect(kind, client)
    assert gateway.calls[-1]["tool_choice"] == "required"


async def test_explicit_named_tool_choice_is_never_relaxed():
    gateway = Gateway([ParameterError(), success()])
    client = adapter("legacy", gateway)
    choice = {"type": "function", "function": {"name": "collaborate_with_agents"}}
    with pytest.raises(ParameterError):
        await client(
            [{"role": "user", "content": "use this tool"}],
            tools=[
                {"type": "function", "function": {"name": "collaborate_with_agents"}}
            ],
            tool_choice=choice,
        )
    assert len(gateway.calls) == 1
    assert gateway.calls[0]["tool_choice"] == choice


async def test_diagnostic_probe_keeps_its_two_request_budget():
    gateway = Gateway([success(), ParameterError(), success()])
    report = await run_model_compatibility_probe(
        provider_id="custom-gateway",
        model_id="unlisted-model-2030",
        client=adapter("native", gateway),
    )
    assert len(gateway.calls) == 2
    assert report.status == "partial"
    assert (
        next(check for check in report.checks if check.id == "tool_call").detail_code
        == "provider_error"
    )


async def test_diagnostic_probe_does_not_reuse_conversation_negotiation():
    gateway = Gateway([ParameterError(), success(), ParameterError()])
    client = adapter("native", gateway)
    await collect("native", client)
    # Match the diagnostic request's scope, so this specifically exercises a
    # warmed cache rather than depending on different generation settings.
    request = ModelRequest(
        messages=[ModelMessage.text("user", "probe")],
        tools=[ToolSpec(name="probe_tool", input_schema={"type": "object"})],
        tool_choice="required",
        metadata={"surface": "model_compatibility_probe"},
    )
    with pytest.raises(ParameterError):
        await client.complete(request)
    assert len(gateway.calls) == 3
    assert gateway.calls[-1]["tool_choice"] == "required"


@pytest.mark.parametrize("kind", ["native", "legacy"])
@pytest.mark.parametrize("error_kind", ["http_422", "sse"])
async def test_real_sdk_error_shapes_support_generic_negotiation(kind, error_kind):
    import httpx
    from openai import APIError, UnprocessableEntityError

    request = httpx.Request("POST", "https://gateway.example/v1/chat/completions")
    body = {**REJECTION, "param": "tool_choice"}
    error = (
        UnprocessableEntityError(
            REJECTION["message"],
            response=httpx.Response(422, request=request),
            body=body,
        )
        if error_kind == "http_422"
        else APIError(REJECTION["message"], request=request, body=body)
    )
    gateway = Gateway(
        [error if error_kind == "http_422" else Stream([error]), success()]
    )
    receipt = (await collect(kind, adapter(kind, gateway)))["tool_choice_compatibility"]
    assert receipt["recovery_attempts"] == 1
    assert len(gateway.calls) == 2


async def test_native_negotiation_preserves_prior_reasoning_content_repair():
    class MissingReasoning(Exception):
        status_code = 400

        def __str__(self):
            return "Missing reasoning_content field in the assistant message at index 0"

    gateway = Gateway([MissingReasoning(), ParameterError(), success()])
    client = adapter("native", gateway)
    request = ModelRequest(
        messages=[
            ModelMessage.text("assistant", "prior"),
            ModelMessage.text("user", "use the available tool"),
        ],
        tools=[
            ToolSpec(name="collaborate_with_agents", input_schema={"type": "object"})
        ],
        tool_choice="required",
    )
    await client.complete(request)
    assert len(gateway.calls) == 3
    assert gateway.calls[1]["messages"][0]["reasoning_content"] == " "
    assert gateway.calls[2]["messages"] == gateway.calls[1]["messages"]
    assert gateway.calls[2].get("tool_choice", "auto") == "auto"
    assert all(block.type != "thinking" for block in request.messages[0].content)


@pytest.mark.parametrize("stream", [True, False])
async def test_legacy_canonical_adapter_preserves_compatibility_receipt(stream):
    from lantai.runtime.adapters.legacy_model_client import LegacyModelClient

    class Formatter:
        async def format(self, messages):
            return [
                {"role": message.role, "content": message.content}
                for message in messages
            ]

    response = (
        success()
        if stream
        else SimpleNamespace(
            id="nonstream",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="done",
                        reasoning_content="preserved-state",
                        tool_calls=[],
                    )
                )
            ],
        )
    )
    gateway = Gateway([ParameterError(), response])
    model = adapter("legacy", gateway)
    model.stream = stream
    client = LegacyModelClient(
        endpoint=ProviderModelEndpoint(
            provider_id="custom-gateway", model_id=model.model_name
        ),
        model=model,
        formatter=Formatter(),
    )
    result = await client.complete(
        ModelRequest(
            messages=[ModelMessage.text("user", "use tools")],
            tools=[
                ToolSpec(
                    name="collaborate_with_agents", input_schema={"type": "object"}
                )
            ],
            tool_choice="required",
        )
    )
    assert (
        result.message.metadata["tool_choice_compatibility"]["recovery_attempts"] == 1
    )


async def test_nonstream_legacy_recovers_and_records_confirmed_capability():
    response = SimpleNamespace(
        id="nonstream",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content="done", reasoning_content="preserved-state", tool_calls=[]
                ),
            )
        ],
    )
    gateway = Gateway([ParameterError(status=422), response, response])
    client = adapter("legacy", gateway)
    client.stream = False
    tools = [
        {
            "type": "function",
            "function": {
                "name": "collaborate_with_agents",
                "parameters": {"type": "object"},
            },
        }
    ]
    result = await client(
        [{"role": "user", "content": "use tools"}], tools=tools, tool_choice="required"
    )
    await client(
        [{"role": "user", "content": "use tools"}], tools=tools, tool_choice="required"
    )
    assert len(gateway.calls) == 3
    assert gateway.calls[-1].get("tool_choice", "auto") == "auto"
    assert result.metadata["tool_choice_compatibility"]["recovery_attempts"] == 1
    assert {block["type"] for block in result.content} == {"text", "thinking"}
