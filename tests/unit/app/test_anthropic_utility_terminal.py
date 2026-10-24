"""Replay real Anthropic compatibility transport into the utility consumer."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from types import SimpleNamespace as NS

import pytest

from lantai.agents.legacy_model_components import AnthropicChatModel
from lantai.app.utility_model import (
    UtilityTaskIncompleteError,
    UtilityTaskTimeoutError,
    consume_model_response,
)
from lantai.token_usage.model_wrapper import TokenRecordingModelWrapper


def _events(answer, reason, *, start=True):
    if start:
        yield NS(type="message_start", message=NS(
            id="synthetic-response", usage=NS(input_tokens=7, output_tokens=0),
        ))
    yield NS(type="content_block_delta", delta=NS(
        type="thinking_delta", thinking="synthetic private reasoning",
    ))
    if answer:
        yield NS(type="content_block_delta", delta=NS(type="text_delta", text=answer))
    yield NS(type="message_delta", delta=NS(stop_reason=reason),
             usage=NS(input_tokens=7, output_tokens=16384))


def _model(monkeypatch, *, answer="", reason="max_tokens", streaming=True,
           start=True, tail=None):
    calls, drained, recorded = [], [], []

    async def wire():
        for event in _events(answer, reason, start=start):
            yield event
        drained.append("terminal")
        if tail is not None:
            await tail()

    async def create(**kwargs):
        calls.append(kwargs)
        if streaming:
            return wire()
        return NS(id="synthetic-response", stop_reason=reason, content=[
            NS(type="thinking", thinking="synthetic private reasoning"),
            NS(type="text", text=answer),
        ], usage=NS(input_tokens=7, output_tokens=16384))

    raw = AnthropicChatModel(
        "synthetic-model", api_key="synthetic-key", max_tokens=16384,
        stream=streaming, thinking={"type": "adaptive"},
    )
    raw.client = NS(messages=NS(create=create))
    wrapped = TokenRecordingModelWrapper("synthetic-provider", raw)
    monkeypatch.setattr(wrapped, "_record_usage", recorded.append)
    return raw, wrapped, calls, drained, recorded


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("answer", ["", '{"complete_looking":true}'])
async def test_real_anthropic_max_tokens_is_incomplete_not_empty_or_valid_json(
    monkeypatch, streaming, answer,
):
    _raw, wrapped, calls, drained, recorded = _model(
        monkeypatch, answer=answer, streaming=streaming,
    )
    with pytest.raises(UtilityTaskIncompleteError) as captured:
        await consume_model_response(wrapped, [])
    assert captured.value.finish_reason == "length"
    assert str(captured.value) == "utility model output incomplete: length"
    assert len(calls) == 1
    assert calls[0]["max_tokens"] == 16384
    assert calls[0]["thinking"] == {"type": "adaptive"}
    assert "finish_reason" not in calls[0]
    assert recorded[-1].output_tokens == 16384
    assert drained == (["terminal"] if streaming else [])


@pytest.mark.parametrize("reason", ["max_tokens", "end_turn", "tool_use"])
@pytest.mark.parametrize("start", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
async def test_raw_stop_reason_survives_without_changing_serialized_response(
    monkeypatch, reason, start, streaming,
):
    raw, _wrapped, _calls, _drained, _recorded = _model(
        monkeypatch, reason=reason, start=start, streaming=streaming,
        answer='{"value":1000}',
    )
    result = await raw([])
    chunks = [c async for c in result] if streaming else [result]
    final = chunks[-1]
    assert final.finish_reason == reason
    assert set(asdict(final)) == {"content", "id", "usage", "metadata", "type"}
    assert final.text_semantics == "snapshot"
    assert final.content[-1] == {"type": "text", "text": '{"value":1000}'}
    assert final.content[0]["thinking"] == "synthetic private reasoning"


async def test_terminal_reason_is_emitted_even_without_content_or_initial_usage():
    async def wire():
        yield NS(type="message_delta", delta=NS(stop_reason="max_tokens"))

    raw = AnthropicChatModel("synthetic-model", api_key="synthetic-key")
    chunks = [c async for c in raw._parse_stream_response(0, wire())]
    assert len(chunks) == 1
    assert chunks[0].content == []
    assert chunks[0].finish_reason == "max_tokens"
    assert chunks[0].usage is None


@pytest.mark.parametrize("streaming", [False, True])
async def test_full_budget_usage_does_not_imply_truncation_when_provider_completed(
    monkeypatch, streaming,
):
    _raw, wrapped, calls, drained, recorded = _model(
        monkeypatch, answer='{"value":1000}', reason="end_turn",
        streaming=streaming,
    )
    assert await consume_model_response(wrapped, []) == '{"value":1000}'
    assert len(calls) == 1
    assert recorded[-1].output_tokens == 16384
    assert drained == (["terminal"] if streaming else [])


@pytest.mark.parametrize("tail_kind", ["transport_error", "idle_timeout", "total_timeout"])
async def test_real_terminal_reason_survives_tail_failure_without_retry(
    monkeypatch, tail_kind,
):
    async def tail():
        if tail_kind == "transport_error":
            raise RuntimeError("synthetic private transport detail")
        await asyncio.sleep(0.05)

    _raw, wrapped, calls, drained, _recorded = _model(monkeypatch, tail=tail)
    deadlines = ({"idle_timeout_seconds": 0.005} if tail_kind == "idle_timeout"
                 else {"total_timeout_seconds": 0.01} if tail_kind == "total_timeout"
                 else {})
    with pytest.raises(UtilityTaskIncompleteError) as captured:
        await consume_model_response(wrapped, [], **deadlines)
    assert captured.value.finish_reason == "length"
    assert len(calls) == 1
    assert drained == ["terminal"]
    assert "private" not in str(captured.value)


async def test_unknown_anthropic_finish_still_reports_actual_deadline(monkeypatch):
    async def tail():
        await asyncio.sleep(0.05)

    _raw, wrapped, calls, _drained, _recorded = _model(
        monkeypatch, reason=None, tail=tail,
    )
    with pytest.raises(UtilityTaskTimeoutError):
        await consume_model_response(wrapped, [], idle_timeout_seconds=0.005)
    assert len(calls) == 1


async def test_known_incomplete_anthropic_finish_does_not_swallow_cancellation(monkeypatch):
    reached_tail = asyncio.Event()

    async def tail():
        reached_tail.set()
        await asyncio.Event().wait()

    _raw, wrapped, calls, _drained, _recorded = _model(monkeypatch, tail=tail)
    task = asyncio.create_task(consume_model_response(wrapped, []))
    await asyncio.wait_for(reached_tail.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) == 1


@pytest.mark.parametrize("stream_tool_parsing", [False, True])
async def test_stop_marker_retains_tool_input_signatures_and_redacted_thinking(
    stream_tool_parsing,
):
    async def wire():
        yield NS(type="content_block_start", index=0, content_block=NS(
            type="redacted_thinking", data="synthetic-redacted-data",
        ))
        yield NS(type="content_block_delta", index=1, delta=NS(
            type="thinking_delta", thinking="synthetic reasoning",
        ))
        yield NS(type="content_block_delta", index=1, delta=NS(
            type="signature_delta", signature="synthetic-signature",
        ))
        yield NS(type="content_block_start", index=2, content_block=NS(
            type="tool_use", id="synthetic-tool", name="synthetic_tool",
        ))
        yield NS(type="content_block_delta", index=2, delta=NS(
            type="input_json_delta", partial_json='{"value":1000}',
        ))
        yield NS(type="message_delta", delta=NS(stop_reason="tool_use"))

    raw = AnthropicChatModel(
        "synthetic-model", api_key="synthetic-key",
        stream_tool_parsing=stream_tool_parsing,
    )
    chunks = [c async for c in raw._parse_stream_response(0, wire())]
    final = chunks[-1]
    assert final.finish_reason == "tool_use"
    assert final.content == [
        {"type": "thinking", "thinking": "", "provider_block_type": "redacted_thinking",
         "data": "synthetic-redacted-data"},
        {"type": "thinking", "thinking": "synthetic reasoning", "signature": "synthetic-signature"},
        {"type": "tool_use", "id": "synthetic-tool", "name": "synthetic_tool",
         "input": {"value": 1000}, "raw_input": '{"value":1000}'},
    ]
