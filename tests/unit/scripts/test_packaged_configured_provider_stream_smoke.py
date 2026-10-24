# -*- coding: utf-8 -*-
"""Unit coverage for the packaged configured-provider renderer smoke."""

# pylint: disable=protected-access

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "packaged_configured_provider_stream_smoke.py"
)
RENDERER_SCRIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "console"
    / "electron"
    / "configuredProviderStreamSmoke.cjs"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "packaged_configured_provider_stream_smoke",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_renderer_inspects_run_drawer_even_without_screenshot_output() -> None:
    source = RENDERER_SCRIPT_PATH.read_text(encoding="utf-8")

    assert "if (!screenshotDir) return pending;" not in source
    assert "result.run_drawer = runDrawer;" in source


def test_renderer_composer_gate_allows_long_answers_above_viewport() -> None:
    source = RENDERER_SCRIPT_PATH.read_text(encoding="utf-8")

    assert "rect.top >= messageListTop + 8" not in source
    assert "entirelyAboveMessageViewport" in source
    assert "entirelyAboveMessageViewport || visibleAboveComposer" in source
    assert "rect.top < composerTop - 8" in source
    assert "rect.bottom <= composerTop - 8" in source
    assert "rect.bottom > messageListTop + 8" in source
    assert "turn-${options.turnNumber}-failure.png" in source


def test_renderer_distinguishes_provider_text_repetition_from_duplicate_nodes() -> (  # noqa: E501
    None
):
    source = RENDERER_SCRIPT_PATH.read_text(encoding="utf-8")

    assert "matchingAnswerNodes > 1" in source
    assert "current.assistant_sentinel_occurrences > 1" not in source
    assert "rendered the answer in more than one node" in source


def test_renderer_uses_each_turn_submission_not_absolute_response_index() -> (
    None
):
    source = RENDERER_SCRIPT_PATH.read_text(encoding="utf-8")

    assert "responses[turnNumber - 1]" not in source
    assert ".find((item) => item.reconnect !== true)" in source
    assert "reconnect: reconnectRequest" in source


def test_renderer_waits_for_deep_thinking_capability_before_first_submission() -> (  # noqa: E501
    None
):
    source = RENDERER_SCRIPT_PATH.read_text(encoding="utf-8")

    assert "waitForDeepThinkingControlResolution" in source
    assert "await waitForDeepThinkingControlResolution(8000)" in source
    assert "current.deep_thinking_control_enabled" in source
    assert "let button = findDeepThinkingButton();" in source
    assert "if (button.disabled)" not in source


def test_renderer_has_an_optional_effect_record_file_card_turn() -> None:
    source = RENDERER_SCRIPT_PATH.read_text(encoding="utf-8")

    assert 'args["artifact-prompt"]' in source
    assert 'args["artifact-filename"]' in source
    assert '[data-lantai-artifact="file"]' in source
    assert "matching_artifact_card_count" in source
    assert "artifact_card_has_open_action" in source
    assert "artifact_card_has_download_action" in source
    assert "current.thinking_count >= turnNumber" in source
    assert 'current.thinking_statuses.at(-1) === "in_progress"' in source


def test_artifact_prompt_is_a_natural_file_delivery_request() -> None:
    module = _load_module()

    assert module._ARTIFACT_FILENAME == "delivery-check-note.txt"
    assert module._ARTIFACT_CONTENT == (
        "This note confirms that Lantai created and delivered the requested file."
    )
    assert module._ARTIFACT_ANSWER_SENTINEL == (
        "The requested file is ready to download."
    )
    assert "actual end-to-end file delivery check" in module._ARTIFACT_PROMPT
    assert "Use the available file tools now" in module._ARTIFACT_PROMPT
    assert "do not only describe the steps" in module._ARTIFACT_PROMPT
    assert "S6_EFFECT_RECORD_ARTIFACT" not in module._ARTIFACT_PROMPT


def test_artifact_smoke_selects_full_access_before_submitting() -> None:
    packaged_source = SCRIPT_PATH.read_text(encoding="utf-8")
    renderer_source = RENDERER_SCRIPT_PATH.read_text(encoding="utf-8")

    assert 'command.extend(["--permission-preset", "full"])' in packaged_source
    assert "selectPermissionPreset" in renderer_source
    assert 'permission_mode: String(parsedBody?.permission_mode || "")' in (
        renderer_source
    )
    assert 'permission_profile: String(parsedBody?.permission_profile || "")' in (
        renderer_source
    )
    assert 'transport?.request_shape?.permission_mode !== "full_auto"' in (
        renderer_source
    )
    assert 'transport?.request_shape?.permission_profile !== "full"' in (
        renderer_source
    )


def _write_artifact_delivery_fixture(
    module: Any,
    tmp_path: Path,
    *,
    artifact_bytes: bytes,
    candidate_status: str = "committed",
    delivery_status: str = "delivered",
    candidate_sha256: str | None = None,
    delivery_sha256: str | None = None,
    candidate_artifact_id: str = "artifact-shared",
    delivery_artifact_id: str = "artifact-shared",
    candidate_run_id: str = "run-1",
    delivery_run_id: str = "run-1",
    candidate_attempt_id: str = "attempt-1",
    delivery_attempt_id: str = "attempt-1",
    candidate_effect_id: str = "effect-candidate",
    delivery_effect_id: str = "effect-delivery",
    candidate_tool_call_id: str = "call-write",
    delivery_tool_call_id: str = "call-deliver",
) -> Path:
    agent_root = tmp_path / "default"
    topic_root = agent_root / "topic-workspaces" / "artifact-chat"
    topic_root.mkdir(parents=True)
    artifact_path = topic_root / module._ARTIFACT_FILENAME
    artifact_path.write_bytes(artifact_bytes)
    ledger = topic_root / (
        ".lantai/runtime/v1/runs/run-1/attempts/attempt-1/"
        "artifact-effects.jsonl"
    )
    ledger.parent.mkdir(parents=True)
    content_sha256 = module._sha256_bytes(artifact_bytes)
    rows = [
        {
            "artifact_effect_id": candidate_effect_id,
            "artifact_id": candidate_artifact_id,
            "artifact_role": "candidate",
            "relative_path": module._ARTIFACT_FILENAME,
            "status": candidate_status,
            "revision": 1,
            "sha256": candidate_sha256 or content_sha256,
            "size_bytes": len(artifact_bytes),
            "run_id": candidate_run_id,
            "attempt_id": candidate_attempt_id,
            "tool_call_id": candidate_tool_call_id,
        },
        {
            "artifact_effect_id": delivery_effect_id,
            "artifact_id": delivery_artifact_id,
            "artifact_role": "delivery",
            "relative_path": module._ARTIFACT_FILENAME,
            "status": delivery_status,
            "revision": 1,
            "sha256": delivery_sha256 or content_sha256,
            "size_bytes": len(artifact_bytes),
            "run_id": delivery_run_id,
            "attempt_id": delivery_attempt_id,
            "tool_call_id": delivery_tool_call_id,
        },
    ]
    ledger.write_text(
        "".join(
            json.dumps({"checksum": "test", "payload": row}) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    return agent_root


@pytest.mark.parametrize("with_bom", [False, True])
def test_packaged_artifact_inspection_requires_exact_file_and_delivery_chain(
    tmp_path: Path,
    with_bom: bool,
) -> None:
    module = _load_module()
    content_bytes = module._ARTIFACT_CONTENT.encode("utf-8")
    if with_bom:
        content_bytes = b"\xef\xbb\xbf" + content_bytes
    agent_root = _write_artifact_delivery_fixture(
        module,
        tmp_path,
        artifact_bytes=content_bytes,
    )

    result = module._inspect_packaged_artifact_delivery(agent_root)

    assert result == {
        "file_count": 1,
        "content_sha256": module._sha256_bytes(content_bytes),
        "content_bytes": len(content_bytes),
        "utf8_bom_count": int(with_bom),
        "candidate_committed": True,
        "delivery_delivered": True,
        "shared_artifact_identity": True,
        "shared_run_identity": True,
        "distinct_effect_identities": True,
        "distinct_tool_call_identities": True,
        "ledger_count": 1,
    }
    assert str(tmp_path) not in str(result)


@pytest.mark.parametrize(
    "artifact_bytes",
    [
        b"\xef\xbb\xbf\xef\xbb\xbf"
        + b"This note confirms that Lantai created and delivered the requested file.",
        b"XThis note confirms that Lantai created and delivered the requested file.",
        b"This note confirms that Lantai created and delivered the requested file.X",
        b"This note confirms that Lantai created and delivered the requested file",
        b"This note confirms that Lantai created\nand delivered the requested file.",
        b"This note confirms that Lantai created and delivered the requested file.\n",
        b'"This note confirms that Lantai created and delivered the requested file."',
    ],
)
def test_packaged_artifact_inspection_rejects_any_non_bom_content_change(
    tmp_path: Path,
    artifact_bytes: bytes,
) -> None:
    module = _load_module()
    agent_root = _write_artifact_delivery_fixture(
        module,
        tmp_path,
        artifact_bytes=artifact_bytes,
    )

    with pytest.raises(AssertionError, match="unexpected file content"):
        module._inspect_packaged_artifact_delivery(agent_root)


@pytest.mark.parametrize(
    "fixture_changes",
    [
        {"candidate_status": "pending"},
        {"delivery_status": "pending"},
        {"candidate_sha256": "sha256:wrong"},
        {"delivery_sha256": "sha256:wrong"},
        {"delivery_artifact_id": "artifact-other"},
        {"delivery_run_id": "run-other"},
        {"delivery_attempt_id": "attempt-other"},
        {"delivery_effect_id": "effect-candidate"},
        {"delivery_tool_call_id": "call-write"},
    ],
)
def test_packaged_artifact_inspection_rejects_inexact_effect_chains(
    tmp_path: Path,
    fixture_changes: dict[str, str],
) -> None:
    module = _load_module()
    artifact_bytes = b"\xef\xbb\xbf" + module._ARTIFACT_CONTENT.encode("utf-8")
    agent_root = _write_artifact_delivery_fixture(
        module,
        tmp_path,
        artifact_bytes=artifact_bytes,
        **fixture_changes,
    )

    with pytest.raises(AssertionError, match="committed-candidate-to-delivered"):
        module._inspect_packaged_artifact_delivery(agent_root)


def test_compact_renderer_payload_preserves_only_safe_observations() -> None:
    module = _load_module()
    user_phase = {
        "user_bubble_count": 1,
        # The UI may already show the non-empty "正在思考" assistant placeholder
        # when the user snapshot is sampled.
        "assistant_bubble_count": 1,
        "empty_assistant_count": 0,
        "thinking_count": 0,
        "thinking_statuses": [],
        "response_avatar_count": 0,
        "action_bar_count": 0,
        "deep_thinking_control_present": True,
        "deep_thinking_control_enabled": True,
        "deep_thinking_control_active": True,
        "user_contains_prompt": True,
        "assistant_contains_sentinel": False,
        "assistant_sentinel_occurrences": 0,
        "answer_node_completed": False,
        "user_clear_of_composer": True,
        "thinking_clear_of_composer": False,
        "answer_clear_of_composer": False,
        "inline_timeline_present": False,
        "run_drawer_trigger_present": True,
        "run_drawer_trigger_completed": False,
        "assistant_text": "must never be exposed",
        "prompt_text": "must never be exposed",
    }
    thinking_phase = {
        **user_phase,
        "assistant_bubble_count": 1,
        "thinking_count": 1,
        "thinking_statuses": ["in_progress"],
        "thinking_clear_of_composer": True,
        "response_avatar_count": 1,
        "action_bar_count": 1,
    }
    completed_phase = {
        **thinking_phase,
        "thinking_statuses": ["completed"],
        # The completed reasoning body is collapsed, so its content node may
        # legitimately have zero geometry even though the header stays above
        # the composer. Geometry is asserted while Thinking is expanded.
        "thinking_clear_of_composer": False,
        "assistant_contains_sentinel": True,
        "assistant_sentinel_occurrences": 1,
        "answer_node_completed": True,
        "answer_clear_of_composer": True,
        "run_drawer_trigger_completed": True,
    }
    second_user_phase = {
        **completed_phase,
        "user_bubble_count": 2,
        "assistant_contains_sentinel": False,
        "assistant_sentinel_occurrences": 0,
        "answer_node_completed": False,
        "run_drawer_trigger_completed": False,
    }
    second_thinking_phase = {
        **second_user_phase,
        "assistant_bubble_count": 2,
        "thinking_count": 2,
        "thinking_statuses": ["completed", "in_progress"],
        "thinking_clear_of_composer": True,
        "response_avatar_count": 2,
        "action_bar_count": 2,
        "run_drawer_trigger_completed": False,
    }
    second_completed_phase = {
        **second_thinking_phase,
        "thinking_statuses": ["completed", "completed"],
        "assistant_contains_sentinel": True,
        "assistant_sentinel_occurrences": 1,
        "answer_node_completed": True,
        "run_drawer_trigger_completed": True,
    }
    payload: dict[str, Any] = {
        "ok": True,
        "chat_id": "chat-safe",
        "turns": [
            {
                "turn_number": 1,
                "user_visible_ms": 10,
                "thinking_visible_ms": 20,
                "answer_visible_ms": 30,
                "deep_thinking_transport": {
                    "present": True,
                    "enabled": True,
                    "effort": "high",
                },
                "deep_thinking_admission": {
                    "deep_thinking_marker_count": 1,
                    "deep_thinking_control_active": True,
                },
                "run_drawer": {
                    "visible": True,
                    "stage_count": 1,
                    "trigger_completed": True,
                    "geometry": {
                        "width": 420,
                        "right": 1440,
                        "viewport_width": 1440,
                    },
                },
                "user": user_phase,
                "thinking": thinking_phase,
                "answer": completed_phase,
                "final": completed_phase,
            },
            {
                "turn_number": 2,
                "user_visible_ms": 40,
                "thinking_visible_ms": 50,
                "answer_visible_ms": 60,
                "deep_thinking_transport": {
                    "present": True,
                    "enabled": True,
                    "effort": "high",
                },
                "deep_thinking_admission": {
                    "deep_thinking_marker_count": 1,
                    "deep_thinking_control_active": True,
                },
                "run_drawer": {
                    "visible": True,
                    "stage_count": 1,
                    "trigger_completed": True,
                    "geometry": {
                        "width": 420,
                        "right": 1440,
                        "viewport_width": 1440,
                    },
                },
                "user": second_user_phase,
                "thinking": second_thinking_phase,
                "answer": second_completed_phase,
                "final": second_completed_phase,
            },
        ],
    }

    result = module._compact_renderer_payload(payload, chat_id="chat-safe")

    assert len(result["turns"]) == 2
    assert (
        result["turns"][0]["thinking_visible_ms"]
        < result["turns"][0]["answer_visible_ms"]
    )
    assert result["final"]["assistant_contains_sentinel"] is True
    assert result["final"]["deep_thinking_control_active"] is True
    assert (
        result["turns"][0]["phases"]["user"]["user_clear_of_composer"] is True
    )
    assert result["final"]["answer_clear_of_composer"] is True
    assert "assistant_text" not in str(result)
    assert "prompt_text" not in str(result)


def test_compact_renderer_payload_rejects_an_empty_assistant_before_thinking() -> (  # noqa: E501
    None
):
    module = _load_module()
    phase = {
        "user_bubble_count": 1,
        "assistant_bubble_count": 1,
        "empty_assistant_count": 1,
        "thinking_count": 0,
        "thinking_statuses": [],
        "response_avatar_count": 0,
        "action_bar_count": 0,
        "deep_thinking_control_present": True,
        "deep_thinking_control_active": True,
        "user_contains_prompt": True,
        "assistant_contains_sentinel": False,
        "assistant_sentinel_occurrences": 0,
        "answer_node_completed": False,
        "user_clear_of_composer": True,
        "thinking_clear_of_composer": False,
        "answer_clear_of_composer": False,
        "inline_timeline_present": False,
        "run_drawer_trigger_present": True,
        "run_drawer_trigger_completed": False,
    }
    complete = {
        **phase,
        "empty_assistant_count": 0,
        "thinking_count": 1,
        "thinking_statuses": ["completed"],
        "response_avatar_count": 1,
        "action_bar_count": 1,
        "assistant_contains_sentinel": True,
        "assistant_sentinel_occurrences": 1,
        "answer_node_completed": True,
        "thinking_clear_of_composer": True,
        "answer_clear_of_composer": True,
        "run_drawer_trigger_completed": True,
    }
    payload: dict[str, Any] = {
        "ok": True,
        "chat_id": "chat-empty-before-thinking",
        "turns": [
            {
                "turn_number": 1,
                "user_visible_ms": 10,
                "thinking_visible_ms": 20,
                "answer_visible_ms": 30,
                "run_drawer": {
                    "visible": True,
                    "stage_count": 1,
                    "trigger_completed": True,
                    "geometry": {
                        "width": 420,
                        "right": 1440,
                        "viewport_width": 1440,
                    },
                },
                "user": phase,
                "thinking": complete,
                "answer": complete,
                "final": complete,
            },
        ],
    }

    with pytest.raises(AssertionError, match="final state was not healthy"):
        module._compact_turn(payload["turns"][0], turn_number=1)


def test_compact_phase_accepts_provider_repeating_marker_in_one_answer_node() -> (  # noqa: E501
    None
):
    module = _load_module()

    result = module._compact_phase(
        {
            "thinking_statuses": ["completed"],
            "assistant_sentinel_occurrences": 2,
        },
    )

    assert result["assistant_sentinel_occurrences"] == 2


def test_compact_phase_rejects_an_invalid_marker_count() -> None:
    module = _load_module()

    with pytest.raises(AssertionError, match="invalid answer marker count"):
        module._compact_phase(
            {
                "thinking_statuses": ["completed"],
                "assistant_sentinel_occurrences": -1,
            },
        )


def test_compact_renderer_payload_accepts_reasoning_without_optional_toggle() -> (  # noqa: E501
    None
):
    module = _load_module()
    phase = {
        "user_bubble_count": 1,
        "assistant_bubble_count": 1,
        "empty_assistant_count": 0,
        "thinking_count": 1,
        "thinking_statuses": ["completed"],
        "response_avatar_count": 1,
        "action_bar_count": 1,
        "deep_thinking_control_present": False,
        "deep_thinking_control_enabled": False,
        "deep_thinking_control_active": False,
        "user_contains_prompt": True,
        "assistant_contains_sentinel": True,
        "assistant_sentinel_occurrences": 1,
        "answer_node_completed": True,
        "user_clear_of_composer": True,
        "thinking_clear_of_composer": False,
        "answer_clear_of_composer": True,
        "inline_timeline_present": False,
        "run_drawer_trigger_present": True,
        "run_drawer_trigger_completed": True,
    }
    payload = {
        "turn_number": 1,
        "user_visible_ms": 10,
        "thinking_visible_ms": 20,
        "answer_visible_ms": 30,
        "run_drawer": {
            "visible": True,
            "stage_count": 1,
            "trigger_completed": True,
            "geometry": {"width": 420, "right": 960, "viewport_width": 960},
        },
        "user": {
            **phase,
            "assistant_bubble_count": 0,
            "thinking_count": 0,
            "thinking_statuses": [],
            "response_avatar_count": 0,
            "action_bar_count": 0,
            "assistant_contains_sentinel": False,
            "assistant_sentinel_occurrences": 0,
            "answer_node_completed": False,
            "thinking_clear_of_composer": False,
            "answer_clear_of_composer": False,
            "run_drawer_trigger_completed": False,
        },
        "thinking": {
            **phase,
            "thinking_statuses": ["in_progress"],
            "thinking_clear_of_composer": True,
            "assistant_contains_sentinel": False,
            "assistant_sentinel_occurrences": 0,
            "answer_node_completed": False,
            "answer_clear_of_composer": False,
            "run_drawer_trigger_completed": False,
        },
        "answer": phase,
        "final": phase,
    }

    result = module._compact_turn(payload, turn_number=1)

    assert result["phases"]["final"]["deep_thinking_control_present"] is False
    assert result["phases"]["final"]["thinking_count"] == 1


def test_compact_renderer_payload_accepts_disabled_reasoning_toggle() -> None:
    module = _load_module()
    phase = {
        "user_bubble_count": 1,
        "assistant_bubble_count": 1,
        "empty_assistant_count": 0,
        "thinking_count": 1,
        "thinking_statuses": ["completed"],
        "response_avatar_count": 1,
        "action_bar_count": 1,
        "deep_thinking_control_present": True,
        "deep_thinking_control_enabled": False,
        "deep_thinking_control_active": False,
        "user_contains_prompt": True,
        "assistant_contains_sentinel": True,
        "assistant_sentinel_occurrences": 1,
        "answer_node_completed": True,
        "user_clear_of_composer": True,
        "thinking_clear_of_composer": False,
        "answer_clear_of_composer": True,
        "inline_timeline_present": False,
        "run_drawer_trigger_present": True,
        "run_drawer_trigger_completed": True,
    }
    payload = {
        "turn_number": 1,
        "user_visible_ms": 10,
        "thinking_visible_ms": 20,
        "answer_visible_ms": 30,
        "deep_thinking_transport": {
            "present": False,
            "enabled": False,
            "effort": "",
        },
        "deep_thinking_admission": {
            "deep_thinking_marker_count": 1,
            "deep_thinking_control_active": False,
        },
        "run_drawer": {
            "visible": True,
            "stage_count": 1,
            "trigger_completed": True,
            "geometry": {"width": 420, "right": 960, "viewport_width": 960},
        },
        "user": {
            **phase,
            "assistant_bubble_count": 0,
            "thinking_count": 0,
            "thinking_statuses": [],
            "response_avatar_count": 0,
            "action_bar_count": 0,
            "assistant_contains_sentinel": False,
            "assistant_sentinel_occurrences": 0,
            "answer_node_completed": False,
            "thinking_clear_of_composer": False,
            "answer_clear_of_composer": False,
            "run_drawer_trigger_completed": False,
        },
        "thinking": {
            **phase,
            "thinking_statuses": ["in_progress"],
            "thinking_clear_of_composer": True,
            "assistant_contains_sentinel": False,
            "assistant_sentinel_occurrences": 0,
            "answer_node_completed": False,
            "answer_clear_of_composer": False,
            "run_drawer_trigger_completed": False,
        },
        "answer": phase,
        "final": phase,
    }

    result = module._compact_turn(payload, turn_number=1)

    assert result["phases"]["final"]["deep_thinking_control_enabled"] is False
    assert result["phases"]["final"]["deep_thinking_control_active"] is False


def test_compact_turn_compares_transport_to_request_time_control() -> None:
    module = _load_module()
    phase = {
        "user_bubble_count": 1,
        "assistant_bubble_count": 1,
        "empty_assistant_count": 0,
        "thinking_count": 1,
        "thinking_statuses": ["completed"],
        "response_avatar_count": 1,
        "action_bar_count": 1,
        "deep_thinking_control_present": True,
        "deep_thinking_control_enabled": True,
        "deep_thinking_control_active": True,
        "user_contains_prompt": True,
        "assistant_contains_sentinel": True,
        "assistant_sentinel_occurrences": 1,
        "answer_node_completed": True,
        "user_clear_of_composer": True,
        "thinking_clear_of_composer": False,
        "answer_clear_of_composer": True,
        "inline_timeline_present": False,
        "run_drawer_trigger_present": True,
        "run_drawer_trigger_completed": True,
    }
    payload = {
        "turn_number": 1,
        "user_visible_ms": 10,
        "thinking_visible_ms": 20,
        "answer_visible_ms": 30,
        "deep_thinking_transport": {
            "present": False,
            "enabled": False,
            "effort": "",
        },
        "deep_thinking_admission": {
            "deep_thinking_marker_count": 0,
            "deep_thinking_control_active": False,
        },
        "run_drawer": {
            "visible": True,
            "stage_count": 1,
            "trigger_completed": True,
            "geometry": {"width": 420, "right": 960, "viewport_width": 960},
        },
        "user": {
            **phase,
            "thinking_count": 0,
            "thinking_statuses": [],
            "response_avatar_count": 0,
            "action_bar_count": 0,
            "assistant_contains_sentinel": False,
            "assistant_sentinel_occurrences": 0,
            "answer_node_completed": False,
            "thinking_clear_of_composer": False,
            "answer_clear_of_composer": False,
            "run_drawer_trigger_completed": False,
        },
        "thinking": {
            **phase,
            "thinking_statuses": ["in_progress"],
            "thinking_clear_of_composer": True,
            "assistant_contains_sentinel": False,
            "assistant_sentinel_occurrences": 0,
            "answer_node_completed": False,
            "answer_clear_of_composer": False,
            "run_drawer_trigger_completed": False,
        },
        "answer": phase,
        "final": phase,
    }

    result = module._compact_turn(payload, turn_number=1)

    assert result["deep_thinking_admission"] == {
        "marker_count": 0,
        "control_active": False,
    }
    assert result["phases"]["final"]["deep_thinking_control_active"] is True

    payload["final"] = {
        **payload["final"],
        "answer_node_completed": False,
    }
    with pytest.raises(AssertionError, match="final state was not healthy"):
        module._compact_turn(payload, turn_number=1)
    payload["final"] = phase

    payload["deep_thinking_admission"] = {
        "deep_thinking_marker_count": 1,
        "deep_thinking_control_active": True,
    }
    with pytest.raises(AssertionError, match="final state was not healthy"):
        module._compact_turn(payload, turn_number=1)


def test_clean_app_env_clears_host_runtime_paths(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    monkeypatch.setenv("LANTAI_WORKING_DIR", "/host/working")
    monkeypatch.setenv("LANTAI_SECRET_DIR", "/host/secret")
    monkeypatch.setenv("LANTAI_RUNTIME_ENGINE", "legacy")

    env = module._clean_app_env(
        user_data_dir=tmp_path / "user-data",
        state_path=tmp_path / "state.json",
    )

    assert env["LANTAI_DESKTOP_USER_DATA_DIR"] == str(tmp_path / "user-data")
    assert env["LANTAI_DESKTOP_LIFECYCLE_SMOKE_FILE"] == str(
        tmp_path / "state.json",
    )
    assert env["LANTAI_WORKING_DIR"] == str(tmp_path / "user-data" / "working")
    assert env["LANTAI_SECRET_DIR"] == str(
        tmp_path / "user-data" / "working.secret",
    )
    assert "LANTAI_RUNTIME_ENGINE" not in env


def test_staged_agent_workspace_dir_is_inside_the_default_agent_root(
    tmp_path: Path,
) -> None:
    module = _load_module()

    result = module._staged_agent_workspace_dir(tmp_path / "working")

    assert result == tmp_path / "working" / "workspaces" / "default"


def test_bounded_renderer_diagnostic_keeps_the_beginning_and_end() -> None:
    module = _load_module()
    oversized = "beginning-marker" + ("x" * 20_000) + "ending-marker"

    result = module._bounded_renderer_diagnostic(oversized)

    assert result.startswith("beginning-marker")
    assert result.endswith("ending-marker")
    assert "renderer diagnostic truncated in the middle" in result
    assert len(result) > module._MAX_RENDERER_DIAGNOSTIC_CHARS
