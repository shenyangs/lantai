# -*- coding: utf-8 -*-
"""Shared contract locks for console transcript and control SSE frames."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from lantai.app.routers.console import _turn_receipt_sse
from lantai.app.runner.task_tracker import _progress_sse
from lantai.runtime.turn_control import TurnReceipt


ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tests" / "fixtures" / "console_stream_protocol_v1.json"


def _sse_object(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("data:"):
            payload = json.loads(line.removeprefix("data:").strip())
            return str(payload.get("object") or "")
    raise AssertionError(f"SSE frame has no data payload: {text!r}")


def test_backend_control_frames_match_shared_console_stream_protocol() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    receipt = TurnReceipt(
        client_turn_id="client-turn-contract",
        operation="submit",
        run_id="run-contract",
        attempt_id="attempt-contract",
        chat_id="chat-contract",
        accepted_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
        status="accepted",
    )

    backend_control_objects = {
        _sse_object(_turn_receipt_sse(receipt)),
        _sse_object(_progress_sse("chat-contract")),
    }

    assert fixture["schema_version"] == 1
    assert backend_control_objects == set(fixture["control_objects"])
