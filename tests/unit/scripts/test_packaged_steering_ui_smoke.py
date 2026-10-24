# -*- coding: utf-8 -*-
"""Unit coverage for the exact-candidate steering UI acceptance driver."""

# pylint: disable=protected-access

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "packaged_steering_ui_smoke.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "packaged_steering_ui_smoke",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_evidence_requires_safe_ime_enter_and_drag_attachment() -> None:
    module = _load_module()
    renderer_payload = {
        "terminal_type": "turn.completed",
        "terminal_status": "completed",
        "no_replay_event_count": 0,
        "reconnect_event_count": 1,
        "basic_chat_ui_send": {"ok": True},
        "provider_stream_ui": {
            "ok": True,
            "active_controls": {
                "primary_label": "补充当前任务",
                "explicit_intents": ["立即调整方向", "排队下一任务"],
            },
            "thinking_visible_ms": 1,
            "partial_visible_ms": 2,
            "final_visible_ms": 3,
            "input_method": {
                "composition_enter_blocked": True,
                "draft_preserved": True,
                "post_composition_submission_count": 1,
            },
            "attachment_drag": {
                "file_name": "steering-drag.png",
                "media_type": "image/png",
                "dragover_prevented": True,
                "drop_prevented": True,
                "uploaded_ready": True,
                "duplicate_deduplicated": True,
                "removed_cleanly": True,
                "chat_submission_count": 0,
            },
            "rapid_queue": {
                "queued_before_first_answer": True,
                "submission_intents": ["supplement", "redirect"],
                "final": {
                    "user_texts_in_fifo_order": True,
                    "assistant_texts_in_fifo_order": True,
                    "old_partial_preserved": True,
                    "old_full_answer_absent": True,
                    "superseded_answer_absent": True,
                    "redirect_answer_present": True,
                    "empty_assistant_count": 0,
                    "supplement_disposition": {"status": "superseded"},
                    "redirect_disposition": {"status": "applied"},
                },
            },
            "backend": {"pending_user_message": ""},
        },
        "provider_stop_resend_ui": {
            "ok": True,
            "stop_terminal": "interrupted",
            "full_stopped_answer_absent": True,
            "final": {"empty_assistant_count": 0},
        },
        "provider_stream_route_switch_ui": {
            "ok": True,
            "isolation": {
                "user_bubble_count": 0,
                "assistant_bubble_count": 0,
            },
            "restored": {
                "user_bubble_count": 1,
                "assistant_bubble_count": 1,
                "empty_assistant_count": 0,
            },
        },
    }

    evidence = module._build_evidence(
        app_metadata={"short_version": "2.2.12"},
        provenance={"canonicalMain": False, "gitSha": "abc123"},
        renderer_payload=renderer_payload,
        provider_request_count=1,
    )

    assert evidence["schema"] == "com.lantai.packaged-steering-ui-smoke.v2"
    assert evidence["checks"]["ime_composition_enter_safe"] is True
    assert (
        evidence["checks"]["drag_attachment_ready_deduplicated_without_submit"]
        is True
    )
    assert evidence["renderer"]["active_turn"]["input_method"] == {
        "composition_enter_blocked": True,
        "draft_preserved": True,
        "post_composition_submission_count": 1,
    }
    assert evidence["renderer"]["active_turn"]["attachment_drag"] == {
        "file_name": "steering-drag.png",
        "media_type": "image/png",
        "dragover_prevented": True,
        "drop_prevented": True,
        "uploaded_ready": True,
        "duplicate_deduplicated": True,
        "removed_cleanly": True,
        "chat_submission_count": 0,
    }
    assert evidence["ok"] is True

    missing_media_payload = copy.deepcopy(renderer_payload)
    del missing_media_payload["provider_stream_ui"]["attachment_drag"][
        "media_type"
    ]
    missing_media_evidence = module._build_evidence(
        app_metadata={"short_version": "2.2.12"},
        provenance={"canonicalMain": False, "gitSha": "abc123"},
        renderer_payload=missing_media_payload,
        provider_request_count=1,
    )
    assert (
        missing_media_evidence["checks"][
            "drag_attachment_ready_deduplicated_without_submit"
        ]
        is False
    )
    assert missing_media_evidence["ok"] is False
