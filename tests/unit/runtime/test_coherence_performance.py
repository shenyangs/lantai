# -*- coding: utf-8 -*-
"""Local performance budgets for foreground Harness work."""

from time import perf_counter

from lantai.providers.model_endpoint import ProviderModelEndpoint
from lantai.runtime.context_guard import prepare_model_request_for_context
from lantai.runtime.models import (
    ModelContentBlock,
    ModelMessage,
    ModelRequest,
)
from lantai.runtime.tools import ToolSpec
from lantai.runtime.turn_control import RuntimeTurnControlStore


def _p95_ms(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] * 1000


def _endpoint() -> ProviderModelEndpoint:
    return ProviderModelEndpoint(
        provider_id="kimi-codingplan",
        model_id="kimi-for-coding",
    )


def test_input_admission_p95_stays_under_five_milliseconds(tmp_path) -> None:
    store = RuntimeTurnControlStore(tmp_path)
    samples: list[float] = []

    for index in range(30):
        started = perf_counter()
        store.append_steer(
            client_turn_id=f"client-{index}",
            run_id="run-1",
            attempt_id="attempt-1",
            chat_id="chat-1",
            text=f"steer {index}",
        )
        samples.append(perf_counter() - started)

    assert _p95_ms(samples[5:]) < 5.0


def test_model_surface_compile_p95_stays_under_fifty_milliseconds() -> None:
    request = ModelRequest(
        messages=[
            ModelMessage.text("system", "stable prefix"),
            ModelMessage.text("user", "continue"),
        ],
        tools=[ToolSpec(name=f"tool_{index}") for index in range(12)],
        metadata={
            "turn_id": "run-1",
            "attempt_id": "attempt-1",
            "model_request_created_at": "2026-08-15T00:00:00+00:00",
        },
    )
    samples: list[float] = []

    for _ in range(40):
        started = perf_counter()
        prepare_model_request_for_context(request, _endpoint())
        samples.append(perf_counter() - started)

    assert _p95_ms(samples[5:]) < 50.0


def test_tool_order_and_surface_hash_are_stable_for_kv_cache_contract() -> (
    None
):
    tools = [ToolSpec(name=name) for name in ("read", "search", "write")]
    request = ModelRequest(
        messages=[ModelMessage.text("user", "continue")],
        tools=tools,
        metadata={
            "turn_id": "run-1",
            "attempt_id": "attempt-1",
            "model_request_created_at": "2026-08-15T00:00:00+00:00",
        },
    )

    first = prepare_model_request_for_context(request, _endpoint())
    second = prepare_model_request_for_context(request, _endpoint())
    first_surface = first.metadata["model_context_surface"]
    second_surface = second.metadata["model_context_surface"]

    assert (
        first_surface["tool_order_sha256"]
        == second_surface["tool_order_sha256"]
    )
    assert [row["name"] for row in first_surface["tool_entries"]] == [
        "read",
        "search",
        "write",
    ]
    assert (
        first_surface["integrity_sha256"] == second_surface["integrity_sha256"]
    )


def test_large_tool_evidence_reduces_peak_context_by_at_least_twenty_percent() -> (  # noqa: E501
    None
):
    request = ModelRequest(
        messages=[
            ModelMessage.text("user", "inspect"),
            ModelMessage(
                role="tool",
                tool_call_id="call-1",
                content=[
                    ModelContentBlock(
                        type="tool_result",
                        data={
                            "id": "call-1",
                            "name": "inspect",
                            "output": {"trace": "x" * 2_000_000},
                        },
                    ),
                ],
            ),
        ],
        metadata={
            "turn_id": "run-1",
            "attempt_id": "attempt-1",
            "model_request_created_at": "2026-08-15T00:00:00+00:00",
        },
    )

    prepared = prepare_model_request_for_context(request, _endpoint())
    budget = prepared.metadata["model_context_surface"]["budget"]

    assert budget["after_upper_bound"] <= budget["before_upper_bound"] * 0.8
