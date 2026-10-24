"""Contracts for the real-provider public work-presence source smoke."""

import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "run_source_persona_presence_smoke.py"
)
SPEC = importlib.util.spec_from_file_location("run_source_persona_presence_smoke", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def _events(text: str) -> list[dict]:
    return [
        {
            "type": "tool.call.started",
            "item_id": "call-read",
            "seq": 2,
            "payload": {"client_turn_id": "client-1", "id": "call-read"},
        },
        {
            "type": "message.completed",
            "seq": 1,
            "payload": {
                "client_turn_id": "client-1",
                "public_progress": {
                    "schema_version": 2,
                    "id": "progress-1",
                    "revision": 1,
                    "source": "runtime_facts",
                    "text_origin": "assistant_public_text",
                    "tool_call_ids": ["call-read"],
                },
                "message": {
                    "content": [{"type": "thinking", "text": text}]
                },
            },
        },
        {
            "type": "tool.call.completed",
            "item_id": "call-read",
            "seq": 3,
            "payload": {"client_turn_id": "client-1", "id": "call-read"},
        },
    ]


def test_presence_smoke_accepts_grounded_progress_and_tool_identity() -> None:
    progress, tool_ids = MODULE._progress_evidence(
        _events("文件已经找到，我正在核对里面的两个版本号。"),
        client_turn_id="client-1",
    )

    assert tool_ids == ["call-read"]
    assert progress == [
        {
            "text": "文件已经找到，我正在核对里面的两个版本号。",
            "tool_call_ids": ["call-read"],
            "event_seq": 1,
            "progress_id_present": True,
            "revision": 1,
            "tool_names": [""],
            "tool_started_seqs": [2],
            "tool_completed_seqs": [3],
        }
    ]


@pytest.mark.parametrize("text", ["请放心，我马上就好。", "你最在意的是我懂不懂你。"])
def test_presence_smoke_rejects_overdone_or_reassuring_copy(text: str) -> None:
    with pytest.raises(AssertionError, match="overdone wording"):
        MODULE._progress_evidence(_events(text), client_turn_id="client-1")


def test_presence_smoke_accepts_a_no_tool_followup_without_progress() -> None:
    progress, tool_ids = MODULE._progress_evidence(
        [
            {
                "type": "turn.completed",
                "status": "completed",
                "payload": {"client_turn_id": "client-2"},
            }
        ],
        client_turn_id="client-2",
    )

    assert progress == []
    assert tool_ids == []


def test_presence_smoke_model_override_is_confined_to_the_staged_profile(
    tmp_path: Path,
) -> None:
    working = tmp_path / "working"
    secret = tmp_path / "working.secret"
    agent_path = working / "workspaces" / "default" / "agent.json"
    agent_path.parent.mkdir(parents=True)
    agent_path.write_text(
        json.dumps({"active_model": {"provider_id": "old", "model": "old"}}),
        encoding="utf-8",
    )
    (secret / "providers" / "builtin").mkdir(parents=True)
    (secret / "providers" / "builtin" / "gemini.json").write_text(
        "{}",
        encoding="utf-8",
    )

    configured = MODULE._override_staged_model(
        working_dir=working,
        secret_dir=secret,
        configured={"provider_id": "old", "model": "old"},
        provider_id="gemini",
        model="gemini-3-flash-preview",
    )

    assert configured["provider_id"] == "gemini"
    assert json.loads(agent_path.read_text(encoding="utf-8"))[
        "active_model"
    ] == {"provider_id": "gemini", "model": "gemini-3-flash-preview"}
