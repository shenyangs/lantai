import pytest

from lantai.exceptions import convert_model_exception


class _ModelProtocolError(RuntimeError):
    pass


def test_file_delivery_protocol_failure_has_actionable_error_code() -> None:
    converted = convert_model_exception(
        _ModelProtocolError("The requested file was not attached to the conversation."),
        "kimi-for-coding",
    )

    assert converted.code == "ARTIFACT_DELIVERY_INCOMPLETE"
    assert "附件" in converted.message
    assert converted.details["original_error_type"] == "_ModelProtocolError"


def test_tool_choice_thinking_conflict_has_configuration_error_code() -> None:
    converted = convert_model_exception(
        RuntimeError("tool_choice 'required' is incompatible with thinking enabled"),
        "kimi-for-coding",
    )

    assert converted.code == "MODEL_CONFIGURATION_INCOMPATIBLE"
    assert "模型配置" in converted.message


@pytest.mark.parametrize(
    ("action", "expected_code"),
    [
        ("top_up_balance", "TOKENDANCE_TOP_UP_REQUIRED"),
        ("reauthorize_api_key", "TOKENDANCE_REAUTHORIZE_REQUIRED"),
        ("api_key_quota", "TOKENDANCE_API_KEY_QUOTA"),
    ],
)
def test_tokendance_recovery_header_becomes_actionable_error(
    action,
    expected_code,
) -> None:
    class UpstreamError(Exception):
        status_code = 402
        response = type(
            "Response",
            (),
            {"headers": {"TokenDance-Recovery-Action": action}},
        )()

    converted = convert_model_exception(
        UpstreamError("upstream request failed"),
        model_name="tokendance/qwen3.8-flash",
    )

    assert converted.code == expected_code
    assert converted.details["recovery_action"] == action
    assert action not in converted.message
