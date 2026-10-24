# -*- coding: utf-8 -*-
"""Behaviour locks for provider-neutral structured utility tasks."""

import pytest
from pydantic import BaseModel


class _FactPayload(BaseModel):
    subject: str
    predicate: str
    object: str


async def test_structured_task_validates_repaired_json_against_pydantic_schema(monkeypatch):
    """Provider formatting may vary, but callers only receive a typed value."""

    from lantai.app import structured_utility
    from lantai.app.utility_model import UtilityTextResult

    captured = {}

    async def fake_text_task(**kwargs):
        captured.update(kwargs)
        return UtilityTextResult(
            text=(
                "```json\n"
                "{'subject':'Aurora','predicate':'uses','object':'Engine Nova',}\n"
                "```"
            ),
            model_label="test/model",
        )

    monkeypatch.setattr(
        structured_utility,
        "run_utility_text_task",
        fake_text_task,
    )

    result = await structured_utility.run_structured_utility_task(
        agent_id="agent-1",
        system_prompt="Extract one fact.",
        user_prompt="Aurora uses Engine Nova.",
        purpose="fact-extraction",
        result_model=_FactPayload,
        timeout_seconds=360,
        first_output_timeout_seconds=120,
        idle_timeout_seconds=60,
        expected_provider_id="local-provider",
        require_no_api_cost=True,
        max_output_tokens=512,
        usage_session_id="structured:fact-1",
    )

    assert isinstance(result.value, _FactPayload)
    assert result.value.object == "Engine Nova"
    assert result.model_label == "test/model"
    assert result.repaired is True
    assert '"subject"' in captured["system_prompt"]
    assert "只输出一个符合该 schema 的 JSON 值" in captured["system_prompt"]
    assert captured["expected_provider_id"] == "local-provider"
    assert captured["require_no_api_cost"] is True
    assert captured["timeout_seconds"] == 360
    assert captured["first_output_timeout_seconds"] == 120
    assert captured["idle_timeout_seconds"] == 60
    assert captured["max_output_tokens"] == 512
    assert captured["usage_session_id"] == "structured:fact-1"
    assert captured["structured_output_schema"] == _FactPayload.model_json_schema()


async def test_structured_task_never_returns_schema_invalid_payload(monkeypatch):
    """Repair is syntax recovery, not permission to bypass semantic validation."""

    from lantai.app import structured_utility
    from lantai.app.utility_model import UtilityTextResult

    async def fake_text_task(**_kwargs):
        return UtilityTextResult(
            text='{"subject":"Aurora"}',
            model_label="test/model",
        )

    monkeypatch.setattr(
        structured_utility,
        "run_utility_text_task",
        fake_text_task,
    )

    with pytest.raises(
        structured_utility.StructuredTaskValidationError,
        match="failed schema validation",
    ) as captured:
        await structured_utility.run_structured_utility_task(
            agent_id="agent-1",
            system_prompt="Extract one fact.",
            user_prompt="Aurora uses Engine Nova.",
            purpose="fact-extraction",
            result_model=_FactPayload,
        )

    assert set(captured.value.validation_issues) == {
        "missing@$.predicate",
        "missing@$.object",
    }
    assert "Aurora" not in str(captured.value)


async def test_structured_schema_diagnostics_never_include_rejected_values(
    monkeypatch,
) -> None:
    from lantai.app import structured_utility
    from lantai.app.utility_model import UtilityTextResult

    async def fake_text_task(**_kwargs):
        return UtilityTextResult(
            text=(
                '{"subject":{"api_key":"sensitive-value"},'
                '"predicate":"uses","object":"Engine Nova"}'
            ),
            model_label="test/model",
        )

    monkeypatch.setattr(
        structured_utility,
        "run_utility_text_task",
        fake_text_task,
    )

    with pytest.raises(
        structured_utility.StructuredTaskValidationError,
    ) as captured:
        await structured_utility.run_structured_utility_task(
            agent_id="agent-1",
            system_prompt="Extract one fact.",
            user_prompt="Aurora uses Engine Nova.",
            purpose="fact-extraction",
            result_model=_FactPayload,
        )

    assert captured.value.validation_issues == ("string_type@$.subject",)
    assert "sensitive-value" not in str(captured.value)
    assert "api_key" not in str(captured.value)
