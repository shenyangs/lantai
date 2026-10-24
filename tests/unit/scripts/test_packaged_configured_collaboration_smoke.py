# -*- coding: utf-8 -*-
"""Behavior locks for the packaged hosted-collaboration release smoke."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "packaged_configured_collaboration_smoke.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "packaged_configured_collaboration_smoke",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_selector_and_prose_requests_have_distinct_authoritative_contracts() -> None:
    module = _load_module()

    selector = module._collaboration_request_payload(
        client_turn_id="turn-selector",
        chat_id="chat-selector",
        session_id="session-selector",
        trigger="selector",
    )
    prose = module._collaboration_request_payload(
        client_turn_id="turn-prose",
        chat_id="chat-prose",
        session_id="session-prose",
        trigger="prose",
    )

    assert selector["work_method_id"] == "collaboration"
    assert selector["meta"]["work_method_id"] == "collaboration"
    assert prose["work_method_id"] == "standard"
    assert prose["meta"]["work_method_id"] == "standard"
    assert "真正使用协作室" in prose["input"][0]["content"][0]["text"]

    incident = module._collaboration_request_payload(
        client_turn_id="turn-incident",
        chat_id="chat-incident",
        session_id="session-incident",
        trigger="selector",
        prompt="拿破仑和希特勒决斗，谁厉害？谁会赢？",
    )
    assert incident["input"][0]["content"][0]["text"] == (
        "拿破仑和希特勒决斗，谁厉害？谁会赢？"
    )


def test_sse_reader_forwards_the_incident_prompt_to_the_request_payload(
    monkeypatch,
) -> None:
    module = _load_module()
    calls: list[dict[str, object]] = []

    def fake_payload(**kwargs):
        calls.append(kwargs)
        return {"input": []}

    class EmptyResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def readline(self) -> bytes:
            return b""

    monkeypatch.setattr(module, "_collaboration_request_payload", fake_payload)
    monkeypatch.setattr(module, "urlopen", lambda *_args, **_kwargs: EmptyResponse())

    assert module._read_sse(
        base_url="http://127.0.0.1:9000",
        desktop_token="test-token",
        chat_id="chat-id",
        session_id="session-id",
        trigger="prose",
        prompt="事故复现提示",
        timeout=1,
    ) == []
    assert calls[0]["prompt"] == "事故复现提示"


def test_host_delivery_is_verified_after_room_becomes_ready_for_synthesis() -> None:
    module = _load_module()
    events = [
        {
            "type": "item.completed",
            "payload": {
                "outcome_status": "delivered",
                "delivery_status": "verified",
            },
        },
        {
            "type": "turn.completed",
            "payload": {
                "outcome_status": "delivered",
                "delivery_status": "verified",
            },
        },
    ]

    assert module._verified_host_delivery(events) == {
        "outcome": "delivered",
        "delivery_status": "verified",
    }


def test_host_delivery_rejects_member_completion_without_verified_settlement() -> None:
    module = _load_module()
    events = [
        {
            "kind": "chat.final_answer",
            "payload": {
                "outcome_status": "delivered",
                "delivery_status": "verified",
            },
        },
        {
            "kind": "chat.turn.settled",
            "payload": {
                "outcome_status": "incomplete",
                "delivery_status": "not_delivered",
            },
        },
    ]

    try:
        module._verified_host_delivery(events)
    except AssertionError as exc:
        assert "not delivered" in str(exc)
    else:
        raise AssertionError("unverified host settlement was accepted")


def test_runtime_event_reader_waits_for_verified_host_settlement() -> None:
    module = _load_module()

    class Helpers:
        def __init__(self) -> None:
            self.calls = 0

        def _json_request(self, *_args, **_kwargs):
            self.calls += 1
            events = [
                {
                    "kind": "chat.final_answer",
                    "payload": {
                        "outcome_status": "delivered",
                        "delivery_status": "verified",
                    },
                }
            ]
            if self.calls > 1:
                events.append(
                    {
                        "kind": "chat.turn.settled",
                        "payload": {
                            "outcome_status": "delivered",
                            "delivery_status": "verified",
                        },
                    }
                )
            return 200, b"", {"events": events}

    helpers = Helpers()
    events, delivery = module._read_settled_runtime_events(
        helpers=helpers,
        base_url="http://127.0.0.1:9000",
        desktop_token="test-token",
        chat_id="chat-id",
        timeout=1,
        poll_interval=0,
    )

    assert helpers.calls == 2
    assert len(events) == 2
    assert delivery == {"outcome": "delivered", "delivery_status": "verified"}


def test_prose_profile_disables_auxiliary_router_and_can_override_host_model(
    tmp_path: Path,
) -> None:
    module = _load_module()
    source_working = tmp_path / "source-working"
    source_secret = tmp_path / "source-secret"
    profiles: dict[str, dict[str, str]] = {}
    for agent_id in (module.HOST_AGENT_ID, *module.MEMBER_AGENT_IDS):
        workspace = source_working / "workspaces" / agent_id
        workspace.mkdir(parents=True)
        (workspace / "agent.json").write_text(
            json.dumps(
                {
                    "id": agent_id,
                    "name": agent_id,
                    "active_model": {
                        "provider_id": "original",
                        "model": "original-model",
                    },
                }
            ),
            encoding="utf-8",
        )
        profiles[agent_id] = {"workspace_dir": str(workspace)}
    source_working.mkdir(parents=True, exist_ok=True)
    (source_working / "config.json").write_text(
        json.dumps(
            {
                "agents": {"active_agent": "default", "profiles": profiles},
                "utility_intelligence": {
                    "mode": "dedicated",
                    "model": {"provider_id": "helper", "model": "helper-model"},
                },
            }
        ),
        encoding="utf-8",
    )
    (source_secret / "providers").mkdir(parents=True)
    (source_secret / ".master_key").write_text("test-only", encoding="utf-8")
    encrypted_account = b"opaque-encrypted-cloud-account\x00\xff"
    source_cloud_account = source_secret / "cloud-account.json"
    source_cloud_account.write_bytes(encrypted_account)
    source_cloud_account_before = source_cloud_account.read_bytes()

    working, secret, _names = module._stage_profile(
        source_working_dir=source_working,
        source_secret_dir=source_secret,
        target_root=tmp_path / "staged",
        host_provider_id="deepseek",
        host_model="deepseek-chat",
        disable_utility_intelligence=True,
    )

    config = json.loads((working / "config.json").read_text(encoding="utf-8"))
    host = json.loads(
        (working / "workspaces" / "default" / "agent.json").read_text(
            encoding="utf-8"
        )
    )
    member = json.loads(
        (
            working
            / "workspaces"
            / module.MEMBER_AGENT_IDS[0]
            / "agent.json"
        ).read_text(encoding="utf-8")
    )
    assert config["utility_intelligence"] == {
        "mode": "follow_active",
        "background_processing_enabled": False,
    }
    assert host["active_model"] == {
        "provider_id": "deepseek",
        "model": "deepseek-chat",
    }
    assert member["active_model"] == {
        "provider_id": "original",
        "model": "original-model",
    }
    assert (secret / "cloud-account.json").read_bytes() == encrypted_account
    assert source_cloud_account.read_bytes() == source_cloud_account_before

    source_cloud_account.unlink()
    try:
        module._stage_profile(
            source_working_dir=source_working,
            source_secret_dir=source_secret,
            target_root=tmp_path / "staged-missing-cloud-account",
        )
    except RuntimeError as exc:
        assert "cloud-account.json" in str(exc)
    else:
        raise AssertionError("missing encrypted cloud account was accepted")
