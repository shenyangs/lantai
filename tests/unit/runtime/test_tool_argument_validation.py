"""Schema feedback must be actionable without echoing authored payloads."""

from copy import deepcopy

import pytest

from lantai.agents.tools.presentation import CREATE_PRESENTATION_INPUT_SCHEMA
from lantai.runtime.model_round import ModelRoundProtocolError
from lantai.runtime.native_turn_support import _validate_tool_call_arguments
from lantai.runtime.tool_argument_validation import tool_argument_schema_feedback
from lantai.runtime.tools import ToolCall, ToolSpec


def test_presentation_schema_feedback_reports_each_page_leaf_without_payload() -> None:
    marker = "private-authored-content-not-a-schema-diagnostic"
    arguments = {
        "title": "Synthetic schema recovery",
        "slides": [
            {
                "layout_id": "freeform",
                "objects": [
                    {
                        "kind": "shape",
                        "shape_type": "text" if index == 0 else "rect",
                        "slot": "body",
                        "frame": {"x": 1, "y": 1, "w": 4, "h": 1},
                        "text": marker * 100,
                        "font_size": 12,
                    },
                ],
            }
            for index in range(9)
        ],
    }
    original = deepcopy(arguments)
    call = ToolCall(id="invalid-deck", name="create_presentation", arguments=arguments)
    spec = ToolSpec(name=call.name, input_schema=CREATE_PRESENTATION_INPUT_SCHEMA)

    with pytest.raises(ModelRoundProtocolError) as raised:
        _validate_tool_call_arguments(call, specs_by_name={call.name: spec})

    feedback = str(raised.value)
    for index in range(9):
        assert f"arguments.slides[{index}].objects[0].font_size" in feedback
    assert "arguments.slides[0].objects[0].shape_type" in feedback
    assert "17" in feedback
    assert "rect" in feedback
    assert marker not in feedback
    assert len(feedback) < 6000
    assert arguments == original


@pytest.mark.parametrize(
    ("schema", "arguments", "expected"),
    [
        ({"required": ["file_path"]}, {}, "file_path"),
        ({"properties": {"n": {"minimum": 17}}}, {"n": 12}, "minimum=17"),
        ({"properties": {"n": {"maximum": 72}}}, {"n": 90}, "maximum=72"),
        ({"properties": {"n": {"type": "integer"}}}, {"n": "private-value"}, "integer"),
        ({"properties": {"s": {"pattern": "^[A-Z]+$"}}}, {"s": "private-value"}, "pattern="),
        ({"properties": {"a": {"maxItems": 1}}}, {"a": [1, 2]}, "maxItems=1"),
        ({"properties": {"a": {"minItems": 1}}}, {"a": []}, "minItems=1"),
        ({"properties": {"s": {"minLength": 20}}}, {"s": "private-value"}, "minLength=20"),
        ({"properties": {"s": {"enum": ["rect", "line"]}}}, {"s": "private-value"}, "rect"),
        ({"additionalProperties": False}, {"extra": "private-value"}, "unexpected properties"),
        ({"oneOf": [{"type": "object"}, {"type": "object"}]}, {"text": "private-value"}, "exactly one"),
    ],
)
def test_schema_feedback_keeps_constraints_but_never_echoes_values(
    schema, arguments, expected,
) -> None:
    feedback = tool_argument_schema_feedback(schema, arguments)
    assert feedback is not None
    assert expected in feedback
    assert "private-value" not in feedback


def test_schema_feedback_does_not_guess_between_alternatives() -> None:
    schema = {"oneOf": [{"const": "a"}, {"const": "b"}]}
    feedback = tool_argument_schema_feedback(schema, {"text": "private-value"})
    assert "permitted schema (oneOf)" in feedback
    assert "private-value" not in feedback


def test_schema_feedback_is_bounded_and_does_not_skip_validation() -> None:
    schema = {"properties": {"items": {"items": {"minimum": 17}}}}
    feedback = tool_argument_schema_feedback(schema, {"items": [1] * 500})
    assert feedback is not None
    assert "Additional violations omitted" in feedback
    assert len(feedback) < 6000
    assert tool_argument_schema_feedback(schema, {"items": [17] * 500}) is None
