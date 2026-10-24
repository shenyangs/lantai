"""Behavior locks for the presentation-only tool progress contract."""

import pytest

from lantai.runtime.tool_public_progress import (
    PUBLIC_PROGRESS_ARGUMENT,
    collapse_repeated_public_progress,
    model_tool_public_progress,
    tool_specs_with_public_progress_contract,
    without_public_progress_argument,
)
from lantai.runtime.tools import ToolCall, ToolSpec
from lantai.runtime.models import ModelContentBlock


def test_provider_schema_is_augmented_without_mutating_the_canonical_tool() -> (
    None
):
    spec = ToolSpec(
        name="lookup",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    )

    projected = tool_specs_with_public_progress_contract([spec])[0]

    assert projected is not spec
    assert projected.input_schema["required"] == [
        "query",
        PUBLIC_PROGRESS_ARGUMENT,
    ]
    assert PUBLIC_PROGRESS_ARGUMENT in projected.input_schema["properties"]
    assert PUBLIC_PROGRESS_ARGUMENT not in spec.input_schema["properties"]


def test_reserved_argument_is_removed_before_canonical_tool_processing() -> (
    None
):
    call = ToolCall(
        id="call-1",
        name="lookup",
        arguments={"query": "records", PUBLIC_PROGRESS_ARGUMENT: "正在核对。"},
    )

    cleaned = without_public_progress_argument(call)

    assert cleaned.arguments == {"query": "records"}
    assert call.arguments[PUBLIC_PROGRESS_ARGUMENT] == "正在核对。"


def test_weak_provider_progress_alias_is_removed_before_tool_execution() -> (
    None
):
    call = ToolCall(
        id="call-shell",
        name="execute_shell_command",
        arguments={
            "command": "printf ok",
            "_public_progress": "PPT 已生成，正在核验页数与内容",
        },
    )

    cleaned = without_public_progress_argument(call)

    assert cleaned.arguments == {"command": "printf ok"}


def test_reserved_argument_collision_fails_closed() -> None:
    spec = ToolSpec(
        name="third_party_tool",
        input_schema={
            "type": "object",
            "properties": {PUBLIC_PROGRESS_ARGUMENT: {"type": "integer"}},
        },
    )

    with pytest.raises(
        ValueError, match="reserved Runtime presentation field"
    ):
        tool_specs_with_public_progress_contract([spec])


def test_internal_progressive_loader_is_not_given_a_user_facing_update_field() -> (
    None
):
    loader = ToolSpec(
        name="equip_lantai_tools",
        input_schema={"type": "object", "properties": {}},
        metadata={"progressive_tool_loader": True},
    )

    assert tool_specs_with_public_progress_contract([loader]) == [loader]


def test_internal_progressive_loader_chatter_is_not_projected_as_work_progress() -> (
    None
):
    content = [
        ModelContentBlock(type="text", text="我先读取文件。"),
        ModelContentBlock(
            type="tool_call",
            data={
                "id": "call-loader",
                "name": "equip_lantai_tools",
                "arguments": {"files": True},
            },
        ),
    ]

    assert model_tool_public_progress(content) is None


def test_exact_repeated_public_progress_collapses_without_rewriting() -> None:
    sentence = "我先读这个文件。"

    assert collapse_repeated_public_progress(sentence * 13) == sentence
    assert collapse_repeated_public_progress("文件找到了，我再核对版本。") == (
        "文件找到了，我再核对版本。"
    )


def test_dedicated_progress_field_wins_over_long_ordinary_preamble() -> None:
    content = [
        ModelContentBlock(
            type="text",
            text="这里是一段应当留在正文里的较长工具前解释。",
        ),
        ModelContentBlock(
            type="tool_call",
            data={
                "id": "call-read",
                "name": "read_file",
                "arguments": {
                    "file_path": "notes.txt",
                    PUBLIC_PROGRESS_ARGUMENT: "文件找到了，我正在读取。",
                },
            },
        ),
    ]

    projected = model_tool_public_progress(content)

    assert projected is not None
    assert projected[0] == "文件找到了，我正在读取。"
    assert projected[1] == ["call-read"]
    assert projected[2] == ["这里是一段应当留在正文里的较长工具前解释。"]


def test_non_default_tool_can_keep_a_namesake_argument_without_projection() -> (
    None
):
    content = [
        ModelContentBlock(
            type="tool_call",
            data={
                "id": "call-third-party",
                "name": "third_party_tool",
                "arguments": {PUBLIC_PROGRESS_ARGUMENT: "合法的第三方参数"},
            },
        )
    ]

    assert model_tool_public_progress(content, allow_structured=False) is None
