# -*- coding: utf-8 -*-
"""Replay the anonymized DeepSeek collaboration continuation incident."""

from __future__ import annotations

import json
from pathlib import Path

from lantai.providers.model_endpoint import ProviderModelEndpoint
from lantai.providers.reasoning import (
    ReasoningRequestOptions,
    reset_reasoning_options,
    set_reasoning_options,
)
from lantai.runtime.adapters.openai_model_client import OpenAICompatibleModelClient
from lantai.runtime.models import ModelContentBlock, ModelMessage, ModelRequest
from lantai.runtime.tools import ToolSpec


_FIXTURE = (
    Path(__file__).parents[2]
    / "fixtures"
    / "runtime"
    / "incidents"
    / "deepseek_collaboration_reasoning_continuity_v1.json"
)


def test_deepseek_collaboration_continuation_replay_is_transaction_safe() -> None:
    incident = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    endpoint = incident["endpoint"]
    client = OpenAICompatibleModelClient(
        ProviderModelEndpoint(
            provider_id=endpoint["provider_id"],
            model_id=endpoint["model_id"],
            base_url=endpoint["base_url"],
            api_key="fixture-key",
            native_kind="openai_compatible",
        ),
    )
    collaboration_tool = ToolSpec(
        name="collaborate_with_agents",
        input_schema={"type": "object", "properties": {}},
    )
    assistant_tool_turn = ModelMessage(
        role="assistant",
        content=[
            ModelContentBlock(
                type="tool_call",
                data={
                    "id": "call-collaboration",
                    "name": "collaborate_with_agents",
                    "arguments": {},
                },
            ),
        ],
        metadata={
            "reasoning_status": {
                "requested": True,
                "effective_enabled": False,
                "forced_disabled_reason": "required_tool_choice",
            },
        },
    )
    tool_result = ModelMessage(
        role="tool",
        tool_call_id="call-collaboration",
        content=[
            ModelContentBlock(
                type="tool_result",
                data={
                    "id": "call-collaboration",
                    "name": "collaborate_with_agents",
                    "output": "members complete",
                },
            ),
        ],
    )
    history = [
        ModelMessage.text("user", "请协作分析"),
        assistant_tool_turn,
        tool_result,
    ]
    reasoning_token = set_reasoning_options(
        ReasoningRequestOptions(enabled=True),
    )
    try:
        initial = client._payload(  # pylint: disable=protected-access
            ModelRequest(
                messages=[history[0]],
                tools=[collaboration_tool],
                tool_choice="required",
            ),
            include_usage=True,
        )
        open_chain = client._payload(  # pylint: disable=protected-access
            ModelRequest(
                messages=history,
                tools=[collaboration_tool],
                tool_choice="auto",
            ),
            include_usage=True,
        )
        final_synthesis = client._payload(  # pylint: disable=protected-access
            ModelRequest(messages=history, tools=[], tool_choice="auto"),
            include_usage=True,
        )
    finally:
        reset_reasoning_options(reasoning_token)

    assert incident["observed"]["status_code"] == 400
    assert initial["extra_body"]["thinking"] == {"type": "disabled"}
    assert open_chain["extra_body"]["thinking"] == {"type": "disabled"}
    assert final_synthesis["extra_body"]["thinking"] == {"type": "enabled"}
    assert "tools" not in final_synthesis
    assert incident["expected"][
        "disabled_tool_turn_replayed_as_blank_reasoning_content"
    ] is True
    assert final_synthesis["messages"][1]["reasoning_content"] == " "
