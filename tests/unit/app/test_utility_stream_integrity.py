"""Public utility-consumer locks for transport text integrity."""

from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace

import pytest

from lantai.agents.legacy_model_components import (
    AnthropicChatModel,
    ChatResponse,
    GeminiChatModel,
)
from lantai.app.utility_model import consume_model_response
from lantai.providers.openai_chat_model_compat import OpenAIChatModelCompat


async def test_utility_preserves_repeated_digits_across_delta_boundary() -> None:
    class DeltaModel:
        async def __call__(self, _messages):
            async def stream():
                for text in ('{"value":10', '00}'):
                    yield SimpleNamespace(text=text)

            return stream()

    assert await consume_model_response(DeltaModel(), []) == '{"value":1000}'


async def test_openai_utility_keeps_one_answer_while_draining_snapshot_tail() -> None:
    usage = SimpleNamespace(prompt_tokens=4, completion_tokens=3, total_tokens=7)
    drained: list[str] = []

    async def stream():
        yield SimpleNamespace(choices=[SimpleNamespace(
            delta=SimpleNamespace(content='{"value":1000}'),
        )], usage=None)
        yield SimpleNamespace(choices=[SimpleNamespace(
            delta=None, finish_reason="stop",
        )], usage=None)
        yield SimpleNamespace(choices=[], usage=usage)
        drained.append("usage")

    calls: list[dict] = []

    async def create(**kwargs):
        calls.append(kwargs)
        return stream()

    model = OpenAIChatModelCompat("synthetic-model", api_key="synthetic-key")
    model.client = SimpleNamespace(
        base_url="https://synthetic.invalid/v1",
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )

    assert await consume_model_response(
        model,
        [{"role": "user", "content": "synthetic transport test"}],
        generation_kwargs={"max_tokens": 123},
    ) == '{"value":1000}'
    assert drained == ["usage"]
    assert len(calls) == 1
    assert calls[0]["max_tokens"] == 123
    assert calls[0]["stream_options"] == {"include_usage": True}
    assert "text_semantics" not in calls[0]


async def test_gemini_utility_keeps_one_answer_on_usage_only_snapshot() -> None:
    drained: list[str] = []

    async def stream():
        yield SimpleNamespace(candidates=[SimpleNamespace(
            content=SimpleNamespace(parts=[SimpleNamespace(text='{"value":1000}')]),
        )])
        yield SimpleNamespace(candidates=[], usage_metadata=SimpleNamespace(
            prompt_token_count=4, candidates_token_count=3,
        ))
        drained.append("usage")

    async def generate_content_stream(**_kwargs):
        return stream()

    model = GeminiChatModel("synthetic-model", api_key="synthetic-key")
    model.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
        generate_content_stream=generate_content_stream,
    )))

    assert await consume_model_response(model, []) == '{"value":1000}'
    assert drained == ["usage"]


async def test_anthropic_utility_keeps_one_answer_when_only_thinking_changes() -> None:
    drained: list[str] = []

    async def stream():
        yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(
            type="text_delta", text='{"value":1000}',
        ))
        yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(
            type="thinking_delta", thinking="synthetic reasoning",
        ))
        yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(
            type="signature_delta", signature="synthetic signature",
        ))
        drained.append("tail")

    async def create(**_kwargs):
        return stream()

    model = AnthropicChatModel("synthetic-model", api_key="synthetic-key")
    model.client = SimpleNamespace(messages=SimpleNamespace(create=create))

    assert await consume_model_response(model, []) == '{"value":1000}'
    assert drained == ["tail"]


def test_snapshot_contract_does_not_change_serialized_chat_response() -> None:
    response = ChatResponse(content=[{"type": "text", "text": "synthetic"}], id="test")

    assert response.text_semantics == "snapshot"
    assert asdict(response) == {
        "content": [{"type": "text", "text": "synthetic"}],
        "id": "test",
        "usage": None,
        "metadata": None,
        "type": "chat",
    }


@pytest.mark.parametrize(
    ("chunks", "semantics", "expected"),
    [
        (["哈", "哈"], None, "哈哈"),
        (['{"a":{"b":1}', '}'], None, '{"a":{"b":1}}'),
        (["1", "11"], "delta", "111"),
        (["answer", "answer"], "snapshot", "answer"),
        (["an", "answer", "answer"], "snapshot", "answer"),
        (["an", "answer", "answer"], None, "answer"),
        (["哈", "哈"], {"unsupported": True}, "哈哈"),
        (["哈", "哈"], ["snapshot"], "哈哈"),
    ],
)
async def test_utility_honors_text_semantics_without_overlap_guessing(
    chunks: list[str],
    semantics: object,
    expected: str,
) -> None:
    class Model:
        async def __call__(self, _messages):
            async def stream():
                for text in chunks:
                    yield SimpleNamespace(text=text, text_semantics=semantics)

            return stream()

    model = Model()
    assert await consume_model_response(model, []) == expected
    # Reusing a model must not carry the previous exchange's text state.
    assert await consume_model_response(model, []) == expected
