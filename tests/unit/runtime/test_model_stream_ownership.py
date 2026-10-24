import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from lantai.runtime.adapters.legacy_model_client import LegacyModelClient
from lantai.runtime.adapters.openai_model_client import (
    OpenAICompatibleModelClient,
)
from lantai.runtime.adapters.openai_responses_model_client import (
    OpenAIResponsesModelClient,
)
from lantai.providers.model_endpoint import ProviderModelEndpoint
from lantai.runtime.models import ModelRequest, ModelStreamEvent
from lantai.runtime.model_wrappers import RetryingModelClient


class Stream:
    def __init__(self):
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def aclose(self):
        self.closed = True

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cls",
    [
        LegacyModelClient,
        OpenAICompatibleModelClient,
        OpenAIResponsesModelClient,
    ],
)
async def test_first_event_close_owns_acquired_provider_response(cls):
    response = Stream()
    client = object.__new__(cls)
    client.endpoint = ProviderModelEndpoint("synthetic", "synthetic")
    client._model = object()
    if cls is LegacyModelClient:
        client._call_model = AsyncMock(return_value=response)
    elif cls is OpenAICompatibleModelClient:
        client._create_stream = AsyncMock(
            return_value=SimpleNamespace(response=response)
        )
        client._reasoning_turn_plan = lambda _: None
    else:
        client._client = SimpleNamespace(
            responses=SimpleNamespace(create=AsyncMock(return_value=response))
        )
    stream = client.stream(ModelRequest())
    assert (await anext(stream)).kind == "message_start"
    await stream.aclose()
    assert response.closed


@pytest.mark.asyncio
async def test_actual_retry_wrapper_closes_child_before_return():
    trace = []
    held = []

    async def source(_):
        try:
            yield ModelStreamEvent(
                kind="message_start", message_id="synthetic"
            )
        finally:
            trace.append("closed")

    def stream(request):
        child = source(request)
        held.append(child)
        return child

    client = RetryingModelClient(
        SimpleNamespace(
            provider_id="synthetic", model_name="synthetic", stream=stream
        )
    )
    outer = client.stream(ModelRequest())
    await anext(outer)
    try:
        await outer.aclose()
        assert trace == ["closed"]
    finally:
        await held[0].aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wrapper",
    [
        "retry",
        "recording",
        "failover_primary",
        "failover_fallback",
        "tokendance",
    ],
)
@pytest.mark.parametrize("boundary", ["first", "semantic", "normal"])
async def test_wrappers_retain_local_child_ownership(
    monkeypatch, wrapper, boundary
):
    import lantai.runtime.model_wrappers as mod

    trace = []
    held = []

    async def source(_):
        try:
            yield ModelStreamEvent(
                kind="message_start", message_id="synthetic"
            )
            yield ModelStreamEvent(
                kind="text_delta", message_id="synthetic", delta="visible"
            )
            yield ModelStreamEvent(
                kind="message_completed", message_id="synthetic"
            )
        finally:
            trace.append("closed")

    def stream(request):
        child = source(request)
        held.append(child)
        return child

    inner = SimpleNamespace(
        provider_id="synthetic", model_name="synthetic", stream=stream
    )
    if wrapper == "retry":
        client = mod.RetryingModelClient(inner)
    elif wrapper == "recording":
        monkeypatch.setattr(
            mod, "reserve_model_dispatch_budget", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(
            mod, "mark_model_dispatch_budget_ambiguous", lambda *a, **k: None
        )
        monkeypatch.setattr(
            mod, "finalize_model_dispatch_budget", lambda *a, **k: None
        )
        client = mod.TokenRecordingModelClient(inner)
        client._record_usage = lambda *a, **k: None
    elif wrapper.startswith("failover"):
        client = mod.ModelAccessFailoverClient(
            inner, inner, first_event_timeout_seconds=None
        )
        if wrapper.endswith("fallback"):
            client._primary_failure_reason = "synthetic"
    else:
        monkeypatch.setattr(
            mod,
            "assert_tokendance_authorization_current",
            lambda *a, **k: None,
        )
        client = mod.TokenDanceBoundModelClient(
            inner, manager=None, authorization=None, routing_snapshot={}
        )
    outer = client.stream(ModelRequest())
    try:
        if boundary == "normal":
            assert len([x async for x in outer]) == 3
        else:
            assert (await anext(outer)).kind == "message_start"
            if boundary == "semantic":
                assert (await anext(outer)).delta == "visible"
            await outer.aclose()
        assert trace == ["closed"]
    finally:
        await outer.aclose()
        for child in held:
            await child.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["normal", "cancel", "error", "close_error"])
async def test_budget_outcome_and_close_ownership(monkeypatch, mode):
    import lantai.runtime.model_wrappers as mod

    trace = []
    held = []

    async def source(_):
        try:
            yield ModelStreamEvent(
                kind="message_start", message_id="synthetic"
            )
            if mode == "error":
                raise OSError("synthetic")
        finally:
            trace.append("closed")
            if mode == "close_error":
                raise OSError("synthetic-close")

    def stream(request):
        child = source(request)
        held.append(child)
        return child

    monkeypatch.setattr(
        mod,
        "reserve_model_dispatch_budget",
        AsyncMock(return_value="reservation"),
    )
    monkeypatch.setattr(
        mod,
        "mark_model_dispatch_budget_ambiguous",
        lambda *a, **k: trace.append("ambiguous"),
    )
    monkeypatch.setattr(
        mod,
        "finalize_model_dispatch_budget",
        lambda *a, **k: trace.append("finalized"),
    )
    client = mod.TokenRecordingModelClient(
        SimpleNamespace(
            provider_id="synthetic", model_name="synthetic", stream=stream
        )
    )
    client._record_usage = lambda *a, **k: None
    outer = client.stream(ModelRequest())
    await anext(outer)
    try:
        if mode == "normal":
            with pytest.raises(StopAsyncIteration):
                await anext(outer)
            assert trace == ["closed", "finalized"]
        elif mode == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await outer.athrow(asyncio.CancelledError())
            assert trace == ["closed", "ambiguous"]
        else:
            with pytest.raises(OSError):
                if mode == "error":
                    await anext(outer)
                else:
                    await outer.aclose()
            assert trace == ["closed", "ambiguous"]
    finally:
        await outer.aclose()
