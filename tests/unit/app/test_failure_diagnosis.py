# -*- coding: utf-8 -*-
"""Behavior locks for structured Runtime failure attribution."""

import json
from types import SimpleNamespace

import pytest

from lantai.app.experience.failure_diagnosis import failure_diagnosis_for_event


@pytest.mark.parametrize("prefix", ["data: ", ""])
def test_nested_gateway_parameter_error_is_attributed_to_model_request(
    prefix,
) -> None:
    provider_message = "The tool_choice parameter does not support being set to required or object in thinking mode"
    message = prefix + json.dumps(
        {
            "error": {
                "code": "invalid_parameter_error",
                "param": None,
                "message": provider_message,
                "type": "invalid_request_error",
            },
            "id": "[redacted_id]",
        }
    )
    event = _failed_event(
        summary="本轮回复失败",
        payload={
            "error": {
                "error_type": "RuntimeError",
                "error_code": "runtime_turn_failed",
                "message": message,
            }
        },
    )
    event.type = "runtime.error"

    diagnosis = failure_diagnosis_for_event(event)

    assert diagnosis["category"] == "model_request"
    assert diagnosis["error_code"] == "invalid_parameter_error"
    assert diagnosis["summary"] == "模型服务拒绝了请求参数"
    assert "tool_choice" in diagnosis["evidence"]
    assert "Provider" in diagnosis["next_action"]
    assert "同一命令" not in diagnosis["next_action"]


def test_nested_error_message_is_preserved_as_failure_evidence() -> None:
    diagnosis = failure_diagnosis_for_event(
        _failed_event(
            summary="本轮回复失败",
            payload={
                "error": {
                    "error_code": "model_stream_idle_timeout",
                    "message": "模型响应流在第 3 步中断。",
                }
            },
        )
    )
    assert diagnosis["category"] == "model_stream_idle"
    assert diagnosis["evidence"] == "模型响应流在第 3 步中断。"


def test_command_output_json_does_not_impersonate_provider_request_failure() -> (
    None
):
    diagnosis = failure_diagnosis_for_event(
        _failed_event(
            summary="命令失败",
            payload={
                "command": "python fixture.py",
                "stderr": '{"error":{"code":"invalid_parameter_error"}}',
            },
        )
    )
    assert diagnosis["category"] == "command"


def _failed_event(*, summary: str, payload: dict[str, object]):
    return SimpleNamespace(
        seq=42,
        status="failed",
        type="turn.failed",
        summary=summary,
        payload=payload,
    )


def test_model_stream_idle_code_wins_over_command_shaped_text() -> None:
    diagnosis = failure_diagnosis_for_event(
        _failed_event(
            summary="模型响应已中断超过 120 秒。本轮已停止等待，请重试。",
            payload={
                "error_code": "model_stream_idle_timeout",
                "command": "python scripts/generate.py",
                "stderr": "Command failed with exit code 1.",
                "error": {"timeout_phase": "idle", "timeout_seconds": 120},
            },
        )
    )

    assert diagnosis is not None
    assert diagnosis["category"] == "model_stream_idle"
    assert diagnosis["summary"] == "模型响应中途长时间中断"
    assert "checkpoint" in diagnosis["next_action"]


def test_first_event_timeout_uses_its_structured_code_not_message_wording() -> None:
    diagnosis = failure_diagnosis_for_event(
        _failed_event(
            summary="上游暂时没有开始返回内容。",
            payload={
                "error_code": "model_first_event_timeout",
                "error": {"timeout_phase": "first_event", "timeout_seconds": 300},
            },
        )
    )

    assert diagnosis is not None
    assert diagnosis["category"] == "model_first_event"
    assert diagnosis["summary"] == "模型服务长时间未开始响应"


def test_output_truncation_is_not_mislabeled_as_command_failure() -> None:
    diagnosis = failure_diagnosis_for_event(
        _failed_event(
            summary="本轮回复失败",
            payload={
                "error": {
                    "error_code": "model_output_truncated",
                    "message": "模型输出在正文完成前被截断。",
                },
            },
        )
    )

    assert diagnosis is not None
    assert diagnosis["category"] == "model_output"
    assert diagnosis["summary"] == "模型单次输出达到上限"
    assert "已有进度" in diagnosis["next_action"]
    assert diagnosis["error_code"] == "model_output_truncated"


def test_artifact_cause_code_is_more_specific_than_required_capability() -> None:
    diagnosis = failure_diagnosis_for_event(
        _failed_event(
            summary="必需能力执行失败，已停止自动重试。",
            payload={
                "error_code": "required_capability_failed",
                "error": {
                    "capability": "durable_artifact_delivery",
                    "attempts": 2,
                    "cause_code": "artifact_not_created",
                },
            },
        )
    )

    assert diagnosis is not None
    assert diagnosis["category"] == "artifact"
    assert diagnosis["summary"] == "未生成要求的文件"
    assert "生成阶段" in diagnosis["next_action"]
