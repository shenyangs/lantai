# -*- coding: utf-8 -*-
"""Canonical value/model/UI separation for Harness tool results."""

from lantai.runtime.coherence import (
    CanonicalToolEnvelope,
    build_canonical_tool_envelope,
    parse_or_build_tool_envelope,
)
from lantai.runtime.tools import ToolResult


def test_tool_envelope_is_deterministic_and_integrity_checked() -> None:
    result = ToolResult(
        call_id="call-1",
        name="lookup",
        content=[{"type": "text", "text": "done"}],
        metadata={
            "effect_id": "effect:call-1",
            "attempt_id": "attempt-1",
            "effect_revision": 2,
            "effect_status": "committed",
        },
    )

    first = build_canonical_tool_envelope(result)
    second = build_canonical_tool_envelope(result)

    assert first == second
    assert first.verify_integrity()
    assert first.canonical.value == [{"type": "text", "text": "done"}]
    assert first.model_projection.blocks == ({"type": "text", "text": "done"},)
    assert first.ui_intent.kind == "activity"
    assert first.effect_id == "effect:call-1"
    assert first.effect_status == "committed"


def test_structured_canonical_value_is_not_duplicated_in_model_projection() -> (  # noqa: E501
    None
):
    result = ToolResult(
        call_id="call-structured",
        name="search",
        content=[{"type": "text", "text": "2 results"}],
        metadata={
            "structured_content": {
                "items": ["a", "b"],
                "private_trace": "kept in canonical value only",
            },
        },
    )

    envelope = build_canonical_tool_envelope(result)

    assert envelope.canonical.kind == "structured"
    assert envelope.canonical.value["items"] == ["a", "b"]
    assert envelope.model_projection.blocks == (
        {"type": "text", "text": "2 results"},
    )


def test_ui_intent_is_semantic_and_independent_from_model_blocks() -> None:
    result = ToolResult(
        call_id="call-file",
        name="send_file_to_user",
        content=[
            {
                "type": "file",
                "name": "report.pdf",
                "url": "lantai://artifact/report",
            },
        ],
    )

    envelope = build_canonical_tool_envelope(result)

    assert envelope.ui_intent.kind == "artifact"
    assert envelope.ui_intent.artifact_count == 1
    assert envelope.model_projection.blocks[0]["type"] == "file"


def test_tampered_cached_envelope_is_rebuilt_from_tool_result() -> None:
    result = ToolResult(
        call_id="call-1",
        name="lookup",
        content=[{"type": "text", "text": "truth"}],
    )
    envelope = build_canonical_tool_envelope(result)
    tampered = envelope.model_dump(mode="json")
    tampered["canonical"]["value"] = "forged"
    with_tampered_cache = result.model_copy(
        update={
            "metadata": {
                "canonical_tool_envelope": tampered,
            },
        },
    )

    rebuilt = parse_or_build_tool_envelope(with_tampered_cache)

    assert isinstance(rebuilt, CanonicalToolEnvelope)
    assert rebuilt.verify_integrity()
    assert rebuilt.canonical.value == [{"type": "text", "text": "truth"}]
