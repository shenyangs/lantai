# -*- coding: utf-8 -*-
"""Privacy-preserving provider/model compatibility diagnostics."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter
from time import perf_counter
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..runtime.models import (
    LantaiModelClient,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
)
from ..runtime.tools import ToolSpec


CompatibilityStatus = Literal["supported", "partial", "unsupported"]
CompatibilityCheckStatus = Literal["passed", "failed", "not_observed"]


class ModelCompatibilityCheck(BaseModel):
    """One observable protocol capability in a compatibility report."""

    id: str
    status: CompatibilityCheckStatus
    detail_code: str
    event_count: int = Field(default=0, ge=0)


class ModelCompatibilityReport(BaseModel):
    """Redacted structural report returned to Settings → Models."""

    schema_version: Literal[1] = 1
    provider_id: str
    model_id: str
    status: CompatibilityStatus
    checks: list[ModelCompatibilityCheck] = Field(default_factory=list)
    duration_ms: int = Field(default=0, ge=0)
    event_counts: dict[str, int] = Field(default_factory=dict)
    trace_digest: str
    data_classification: Literal["structure_only"] = "structure_only"


_TEXT_PROMPT = (
    "Lantai compatibility probe. Reply with a short plain-text "
    "acknowledgement. "
    "Do not include private or account information."
)
_TOOL_PROMPT = (
    "Lantai compatibility probe. Call the provided tool exactly once with "
    'the JSON argument {"token":"ok"}. Do not answer in plain text.'
)
_PROBE_TOOL = ToolSpec(
    name="lantai_compatibility_probe",
    description="Return a fixed probe token without external side effects.",
    input_schema={
        "type": "object",
        "properties": {"token": {"type": "string", "enum": ["ok"]}},
        "required": ["token"],
        "additionalProperties": False,
    },
    read_only=True,
    destructive=False,
)


def _trace_digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


async def _collect_events(
    client: LantaiModelClient,
    request: ModelRequest,
    *,
    timeout_seconds: float,
) -> list[ModelStreamEvent]:
    async def collect() -> list[ModelStreamEvent]:
        return [event async for event in client.stream(request)]

    return await asyncio.wait_for(collect(), timeout=timeout_seconds)


def _event_counts(events: list[ModelStreamEvent]) -> dict[str, int]:
    return dict(Counter(event.kind for event in events))


def _completed_response(
    events: list[ModelStreamEvent],
) -> ModelResponse | None:
    for event in reversed(events):
        if event.kind == "message_completed" and event.response is not None:
            return event.response
    return None


def _channel_text(events: list[ModelStreamEvent], kind: str) -> str:
    return "".join(event.delta for event in events if event.kind == kind)


def _response_channel_text(
    response: ModelResponse | None,
    block_type: str,
) -> str:
    if response is None:
        return ""
    return "".join(
        block.text or ""
        for block in response.message.content
        if block.type == block_type
    )


def _text_checks(
    events: list[ModelStreamEvent],
) -> list[ModelCompatibilityCheck]:
    counts = _event_counts(events)
    response = _completed_response(events)
    streamed_text = _channel_text(events, "text_delta")
    final_text = _response_channel_text(response, "text")
    text_ok = bool(
        counts.get("message_start")
        and counts.get("message_completed")
        and streamed_text
        and final_text == streamed_text,
    )
    checks = [
        ModelCompatibilityCheck(
            id="text_stream",
            status="passed" if text_ok else "failed",
            detail_code=(
                "append_only_complete"
                if text_ok
                else "stream_contract_incomplete"
            ),
            event_count=counts.get("text_delta", 0),
        ),
    ]

    streamed_thinking = _channel_text(events, "thinking_delta")
    final_thinking = _response_channel_text(response, "thinking")
    if not streamed_thinking and not final_thinking:
        reasoning_status: CompatibilityCheckStatus = "not_observed"
        reasoning_code = "not_returned"
    elif streamed_thinking and final_thinking == streamed_thinking:
        reasoning_status = "passed"
        reasoning_code = "separate_channel"
    else:
        reasoning_status = "failed"
        reasoning_code = "channel_mismatch"

    usage_observed = any(
        event.usage is not None
        or (event.response is not None and event.response.usage is not None)
        for event in events
    )
    finish_observed = any(
        bool(event.finish_reason)
        or bool(event.response and event.response.finish_reason)
        for event in events
    )
    checks.extend(
        [
            ModelCompatibilityCheck(
                id="reasoning_channel",
                status=reasoning_status,
                detail_code=reasoning_code,
                event_count=counts.get("thinking_delta", 0),
            ),
            ModelCompatibilityCheck(
                id="usage",
                status="passed" if usage_observed else "not_observed",
                detail_code="normalized" if usage_observed else "not_returned",
                event_count=sum(
                    1
                    for event in events
                    if event.usage is not None
                    or (
                        event.response is not None
                        and event.response.usage is not None
                    )
                ),
            ),
            ModelCompatibilityCheck(
                id="finish_reason",
                status="passed" if finish_observed else "not_observed",
                detail_code="normalized"
                if finish_observed
                else "not_returned",
                event_count=sum(
                    1
                    for event in events
                    if event.finish_reason
                    or (event.response and event.response.finish_reason)
                ),
            ),
        ],
    )
    return checks


def _tool_check(events: list[ModelStreamEvent]) -> ModelCompatibilityCheck:
    completed = [
        event.tool_call
        for event in events
        if event.kind == "tool_call_completed" and event.tool_call is not None
    ]
    valid = [
        call
        for call in completed
        if call.name == _PROBE_TOOL.name and call.arguments == {"token": "ok"}
    ]
    return ModelCompatibilityCheck(
        id="tool_call",
        status="passed"
        if len(completed) == 1 and len(valid) == 1
        else "failed",
        detail_code=(
            "structured_complete"
            if len(completed) == 1 and len(valid) == 1
            else "not_returned"
            if not completed
            else "malformed_or_multiple"
        ),
        event_count=len(completed),
    )


def _report(
    *,
    provider_id: str,
    model_id: str,
    status: CompatibilityStatus,
    checks: list[ModelCompatibilityCheck],
    events: list[ModelStreamEvent],
    started_at: float,
) -> ModelCompatibilityReport:
    counts = _event_counts(events)
    structural_trace = {
        "provider_id": provider_id,
        "model_id": model_id,
        "checks": [check.model_dump() for check in checks],
        "event_counts": counts,
    }
    return ModelCompatibilityReport(
        provider_id=provider_id,
        model_id=model_id,
        status=status,
        checks=checks,
        duration_ms=max(0, int((perf_counter() - started_at) * 1000)),
        event_counts=counts,
        trace_digest=_trace_digest(structural_trace),
    )


async def run_model_compatibility_probe(
    *,
    provider_id: str,
    model_id: str,
    client: LantaiModelClient,
    timeout_seconds: float = 20.0,
) -> ModelCompatibilityReport:
    """Run bounded diagnostics without persisting prompts or response text."""

    started_at = perf_counter()
    events: list[ModelStreamEvent] = []
    text_request = ModelRequest(
        provider_id=provider_id,
        model=model_id,
        messages=[ModelMessage.text("user", _TEXT_PROMPT)],
        tool_choice="none",
        metadata={"surface": "model_compatibility_probe", "probe": "text"},
    )
    try:
        text_events = await _collect_events(
            client,
            text_request,
            timeout_seconds=timeout_seconds,
        )
        events.extend(text_events)
    except asyncio.TimeoutError:
        check = ModelCompatibilityCheck(
            id="text_stream",
            status="failed",
            detail_code="timeout",
        )
        return _report(
            provider_id=provider_id,
            model_id=model_id,
            status="unsupported",
            checks=[check],
            events=events,
            started_at=started_at,
        )
    except Exception:  # external provider boundary; raw errors stay private
        check = ModelCompatibilityCheck(
            id="text_stream",
            status="failed",
            detail_code="provider_error",
        )
        return _report(
            provider_id=provider_id,
            model_id=model_id,
            status="unsupported",
            checks=[check],
            events=events,
            started_at=started_at,
        )

    text_checks = _text_checks(text_events)
    if text_checks[0].status != "passed":
        return _report(
            provider_id=provider_id,
            model_id=model_id,
            status="unsupported",
            checks=text_checks,
            events=events,
            started_at=started_at,
        )

    tool_request = ModelRequest(
        provider_id=provider_id,
        model=model_id,
        messages=[ModelMessage.text("user", _TOOL_PROMPT)],
        tools=[_PROBE_TOOL],
        tool_choice="required",
        metadata={
            "surface": "model_compatibility_probe",
            "probe": "tool_call",
        },
    )
    try:
        tool_events = await _collect_events(
            client,
            tool_request,
            timeout_seconds=timeout_seconds,
        )
        events.extend(tool_events)
        tool_check = _tool_check(tool_events)
    except asyncio.TimeoutError:
        tool_check = ModelCompatibilityCheck(
            id="tool_call",
            status="failed",
            detail_code="timeout",
        )
    except Exception:  # external provider boundary; raw errors stay private
        tool_check = ModelCompatibilityCheck(
            id="tool_call",
            status="failed",
            detail_code="provider_error",
        )

    checks = [text_checks[0], tool_check, *text_checks[1:]]
    has_protocol_failure = any(
        check.status == "failed" for check in checks[2:]
    )
    return _report(
        provider_id=provider_id,
        model_id=model_id,
        status=(
            "supported"
            if tool_check.status == "passed" and not has_protocol_failure
            else "partial"
        ),
        checks=checks,
        events=events,
        started_at=started_at,
    )


__all__ = [
    "ModelCompatibilityCheck",
    "ModelCompatibilityReport",
    "run_model_compatibility_probe",
]
