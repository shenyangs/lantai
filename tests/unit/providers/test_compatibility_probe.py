# -*- coding: utf-8 -*-
"""Behavior locks for the provider/model compatibility probe."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from lantai.app.routers import providers as providers_router
from lantai.providers.compatibility_probe import run_model_compatibility_probe
from lantai.runtime.models import (
    ModelContentBlock,
    ModelMessage,
    ModelResponse,
    ModelStreamEvent,
    ModelUsage,
)
from lantai.runtime.tools import ToolCall


class _FailingClient:
    async def stream(self, _request):
        if _request is None:  # pragma: no cover - preserve async iteration
            yield None
        raise RuntimeError("secret provider response must not escape")


async def test_probe_redacts_unreachable_text_stream_error() -> None:
    report = await run_model_compatibility_probe(
        provider_id="example",
        model_id="model-a",
        client=_FailingClient(),
    )

    assert report.status == "unsupported"
    assert report.checks[0].id == "text_stream"
    assert report.checks[0].status == "failed"
    assert report.checks[0].detail_code == "provider_error"
    assert report.data_classification == "structure_only"
    assert "secret provider response" not in report.model_dump_json()


class _TextOnlyClient:
    def __init__(self) -> None:
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        if len(self.requests) == 2:
            raise RuntimeError("tool choice unsupported")
        message_id = "probe-text"
        yield ModelStreamEvent(kind="message_start", message_id=message_id)
        yield ModelStreamEvent(
            kind="text_delta",
            message_id=message_id,
            delta="LANTAI_",
        )
        yield ModelStreamEvent(
            kind="text_delta",
            message_id=message_id,
            delta="OK",
        )
        usage = ModelUsage(input_tokens=8, output_tokens=2, total_tokens=10)
        yield ModelStreamEvent(kind="usage", usage=usage)
        yield ModelStreamEvent(
            kind="message_completed",
            message_id=message_id,
            finish_reason="stop",
            usage=usage,
            response=ModelResponse(
                message=ModelMessage(
                    id=message_id,
                    role="assistant",
                    content=[
                        ModelContentBlock(type="text", text="LANTAI_OK"),
                    ],
                ),
                usage=usage,
                finish_reason="stop",
            ),
        )


async def test_probe_marks_text_only_partial_with_fixed_payload() -> None:
    client = _TextOnlyClient()

    report = await run_model_compatibility_probe(
        provider_id="example",
        model_id="model-a",
        client=client,
    )

    assert report.status == "partial"
    assert [(check.id, check.status) for check in report.checks] == [
        ("text_stream", "passed"),
        ("tool_call", "failed"),
        ("reasoning_channel", "not_observed"),
        ("usage", "passed"),
        ("finish_reason", "passed"),
    ]
    assert report.event_counts == {
        "message_start": 1,
        "text_delta": 2,
        "usage": 1,
        "message_completed": 1,
    }
    assert len(client.requests) == 2
    assert (
        client.requests[0]
        .messages[0]
        .text_content()
        .startswith(
            "Lantai compatibility probe",
        )
    )
    assert client.requests[1].tool_choice == "required"
    assert client.requests[1].tools[0].read_only is True


class _ToolCapableClient:
    def __init__(
        self,
        *,
        final_reasoning: str = "brief private reasoning",
        streamed_reasoning: str = "brief private reasoning",
    ) -> None:
        self.requests = []
        self.final_reasoning = final_reasoning
        self.streamed_reasoning = streamed_reasoning

    async def stream(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            message_id = "probe-text"
            yield ModelStreamEvent(kind="message_start", message_id=message_id)
            if self.streamed_reasoning:
                yield ModelStreamEvent(
                    kind="thinking_delta",
                    message_id=message_id,
                    delta=self.streamed_reasoning,
                )
            yield ModelStreamEvent(
                kind="text_delta",
                message_id=message_id,
                delta="ready",
            )
            yield ModelStreamEvent(
                kind="message_completed",
                message_id=message_id,
                finish_reason="stop",
                response=ModelResponse(
                    message=ModelMessage(
                        id=message_id,
                        role="assistant",
                        content=[
                            ModelContentBlock(
                                type="thinking",
                                text=self.final_reasoning,
                            ),
                            ModelContentBlock(type="text", text="ready"),
                        ],
                    ),
                    finish_reason="stop",
                ),
            )
            return

        message_id = "probe-tool"
        call = ToolCall(
            id="call-1",
            name="lantai_compatibility_probe",
            arguments={"token": "ok"},
        )
        yield ModelStreamEvent(kind="message_start", message_id=message_id)
        yield ModelStreamEvent(
            kind="tool_call_delta",
            message_id=message_id,
            tool_call=call,
        )
        yield ModelStreamEvent(
            kind="tool_call_completed",
            message_id=message_id,
            tool_call=call,
        )
        yield ModelStreamEvent(
            kind="message_completed",
            message_id=message_id,
            finish_reason="tool_calls",
            response=ModelResponse(
                message=ModelMessage(
                    id=message_id,
                    role="assistant",
                    content=[
                        ModelContentBlock(
                            type="tool_call",
                            data={
                                "id": call.id,
                                "name": call.name,
                                "arguments": call.arguments,
                            },
                        ),
                    ],
                ),
                finish_reason="tool_calls",
            ),
        )


async def test_probe_reports_normalized_tool_and_reasoning_channels() -> None:
    report = await run_model_compatibility_probe(
        provider_id="example",
        model_id="model-a",
        client=_ToolCapableClient(),
    )

    assert report.status == "supported"
    checks = {check.id: check for check in report.checks}
    assert checks["text_stream"].status == "passed"
    assert checks["tool_call"].detail_code == "structured_complete"
    assert checks["reasoning_channel"].detail_code == "separate_channel"
    assert checks["usage"].status == "not_observed"
    assert report.event_counts["message_completed"] == 2
    assert len(report.trace_digest) == 64
    assert "brief private reasoning" not in report.model_dump_json()


async def test_probe_rejects_mismatched_reasoning_stream() -> None:
    report = await run_model_compatibility_probe(
        provider_id="example",
        model_id="model-a",
        client=_ToolCapableClient(final_reasoning="different final reasoning"),
    )

    assert report.status == "partial"
    checks = {check.id: check for check in report.checks}
    assert checks["reasoning_channel"].status == "failed"
    assert checks["reasoning_channel"].detail_code == "channel_mismatch"


async def test_probe_detects_final_only_reasoning() -> None:
    report = await run_model_compatibility_probe(
        provider_id="example",
        model_id="model-a",
        client=_ToolCapableClient(streamed_reasoning=""),
    )

    assert report.status == "partial"
    checks = {check.id: check for check in report.checks}
    assert checks["reasoning_channel"].status == "failed"
    assert checks["reasoning_channel"].detail_code == "channel_mismatch"


async def test_compatibility_route_uses_selected_model_without_mutation(
    monkeypatch,
) -> None:
    class Provider:
        def has_model(self, model_id: str) -> bool:
            return model_id == "model-a"

    class Manager:
        provider = Provider()

        def get_provider(self, provider_id: str):
            return self.provider if provider_id == "example" else None

        def is_model_slot_available(self, slot) -> bool:
            return slot.provider_id == "example" and slot.model == "model-a"

    captured = {}

    def create_client(provider_id, model_id):
        captured["provider_id"] = provider_id
        captured["model_id"] = model_id
        return _ToolCapableClient()

    monkeypatch.setattr(
        providers_router,
        "_create_compatibility_model_client",
        create_client,
    )

    report = await providers_router.test_model_compatibility(
        provider_id="example",
        body=providers_router.TestModelRequest(model_id="model-a"),
        manager=Manager(),
    )

    assert report.status == "supported"
    assert report.provider_id == "example"
    assert report.model_id == "model-a"
    assert captured == {"provider_id": "example", "model_id": "model-a"}


async def test_model_compatibility_route_redacts_client_factory_errors(
    monkeypatch,
) -> None:
    class Provider:
        def has_model(self, _model_id: str) -> bool:
            return True

    class Manager:
        def get_provider(self, _provider_id: str):
            return Provider()

        def is_model_slot_available(self, _slot) -> bool:
            return True

    def fail_to_create(_provider_id, _model_id):
        raise RuntimeError("secret local dependency path")

    monkeypatch.setattr(
        providers_router,
        "_create_compatibility_model_client",
        fail_to_create,
    )

    with pytest.raises(HTTPException) as exc_info:
        await providers_router.test_model_compatibility(
            provider_id="example",
            body=providers_router.TestModelRequest(model_id="model-a"),
            manager=Manager(),
        )

    assert exc_info.value.status_code == 400
    assert (
        exc_info.value.detail == "Model client is not available for probing."
    )


def test_model_compatibility_http_contract(monkeypatch) -> None:
    class Provider:
        def has_model(self, _model_id: str) -> bool:
            return True

    class Manager:
        def get_provider(self, _provider_id: str):
            return Provider()

        def is_model_slot_available(self, _slot) -> bool:
            return True

    monkeypatch.setattr(
        providers_router,
        "_create_compatibility_model_client",
        lambda _provider_id, _model_id: _ToolCapableClient(),
    )
    app = FastAPI()
    app.include_router(providers_router.router)
    app.dependency_overrides[providers_router.get_provider_manager] = Manager

    response = TestClient(app).post(
        "/models/example/models/compatibility-check",
        json={"model_id": "model-a"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "supported"
    assert response.json()["data_classification"] == "structure_only"
    assert "brief private reasoning" not in response.text
