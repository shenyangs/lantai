"""Unit coverage for configured-provider smoke staging without a network call."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "source_configured_provider_stream_smoke.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("configured_provider_smoke", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_configured_provider_request_uses_the_chat_page_reasoning_contract() -> None:
    module = _load_module()

    payload = module._configured_provider_request_payload("turn-1")

    assert payload["deep_thinking"] == {"enabled": True, "effort": "high"}
    assert payload["meta"] == {"client_turn_id": "configured-provider-smoke-turn-1"}
    assert payload["client_turn_id"] == "configured-provider-smoke-turn-1"


def test_configured_provider_request_supports_same_conversation_followup() -> None:
    module = _load_module()

    payload = module._configured_provider_request_payload(
        "turn-2",
        session_id="console:shared-session",
        prompt="follow-up prompt",
    )

    assert payload["session_id"] == "console:shared-session"
    assert payload["input"][0]["content"][0]["text"] == "follow-up prompt"


def test_stage_profile_keeps_provider_secret_isolated_and_forces_migration(tmp_path: Path) -> None:
    module = _load_module()
    source_working = tmp_path / "source-working"
    source_workspace = source_working / "workspaces" / "default"
    source_workspace.mkdir(parents=True)
    (source_working / "config.json").write_text(
        json.dumps({"agents": {"profiles": {"default": {"workspace_dir": str(source_workspace)}}}}),
        encoding="utf-8",
    )
    (source_workspace / "agent.json").write_text(
        json.dumps({"id": "default", "name": "Configured", "runtime": {"engine": "native"}, "runtime_migrations": [module._NATIVE_RUNTIME_DEFAULT_MIGRATION, module._NATIVE_RUNTIME_DEV_CANARY_MIGRATION], "active_model": {"provider_id": "kimi-codingplan", "model": "kimi-for-coding"}}),
        encoding="utf-8",
    )
    source_secret = tmp_path / "source-secret"
    (source_secret / "providers" / "builtin").mkdir(parents=True)
    (source_secret / ".master_key").write_text("not-a-real-key", encoding="utf-8")
    (source_secret / "providers" / "builtin" / "kimi-codingplan.json").write_text("{}", encoding="utf-8")

    working, secret, configured = module._stage_profile(
        source_working_dir=source_working,
        source_secret_dir=source_secret,
        agent_id="default",
        target_root=tmp_path / "staged",
    )

    staged_agent = json.loads((working / "workspaces" / "default" / "agent.json").read_text(encoding="utf-8"))
    assert configured == {"provider_id": "kimi-codingplan", "model": "kimi-for-coding", "agent_path": str(working / "workspaces" / "default" / "agent.json")}
    assert staged_agent["runtime"] == {"engine": "legacy"}
    assert module._NATIVE_RUNTIME_DEFAULT_MIGRATION not in staged_agent["runtime_migrations"]
    assert module._NATIVE_RUNTIME_DEV_CANARY_MIGRATION not in staged_agent["runtime_migrations"]
    assert (secret / ".master_key").is_file()
    assert (secret / "providers" / "builtin" / "kimi-codingplan.json").is_file()


def test_stage_profile_can_override_route_only_in_isolated_copy(tmp_path: Path) -> None:
    module = _load_module()
    source_working = tmp_path / "source-working"
    source_workspace = source_working / "workspaces" / "default"
    source_workspace.mkdir(parents=True)
    source_agent = {
        "id": "default",
        "active_model": {
            "provider_id": "kimi-codingplan",
            "model": "kimi-for-coding",
        },
    }
    (source_working / "config.json").write_text(
        json.dumps(
            {
                "agents": {
                    "profiles": {
                        "default": {"workspace_dir": str(source_workspace)},
                    },
                },
            },
        ),
        encoding="utf-8",
    )
    (source_workspace / "agent.json").write_text(
        json.dumps(source_agent),
        encoding="utf-8",
    )
    source_secret = tmp_path / "source-secret"
    (source_secret / "providers" / "builtin").mkdir(parents=True)
    (source_secret / ".master_key").write_text("not-a-real-key", encoding="utf-8")
    (source_secret / "providers" / "builtin" / "minimax-cn.json").write_text(
        "{}",
        encoding="utf-8",
    )
    source_active_path = source_secret / "providers" / "active_model.json"
    source_active_path.write_text(
        json.dumps(source_agent["active_model"]),
        encoding="utf-8",
    )

    working, secret, configured = module._stage_profile(
        source_working_dir=source_working,
        source_secret_dir=source_secret,
        agent_id="default",
        target_root=tmp_path / "staged",
        active_model_override={
            "provider_id": "minimax-cn",
            "model": "MiniMax-M3",
        },
    )

    staged_agent = json.loads(
        (working / "workspaces" / "default" / "agent.json").read_text(
            encoding="utf-8",
        ),
    )
    staged_active = json.loads(
        (secret / "providers" / "active_model.json").read_text(encoding="utf-8"),
    )
    assert configured["provider_id"] == "minimax-cn"
    assert configured["model"] == "MiniMax-M3"
    assert staged_agent["active_model"] == staged_active == {
        "provider_id": "minimax-cn",
        "model": "MiniMax-M3",
    }
    assert json.loads(source_active_path.read_text(encoding="utf-8")) == {
        "provider_id": "kimi-codingplan",
        "model": "kimi-for-coding",
    }
    assert json.loads(
        (source_workspace / "agent.json").read_text(encoding="utf-8"),
    ) == source_agent


def test_configured_model_evidence_omits_temporary_profile_path() -> None:
    module = _load_module()

    evidence = module._configured_model_evidence(
        {
            "provider_id": "kimi-codingplan",
            "model": "kimi-for-coding",
            "agent_path": "/private/tmp/copied-profile/agent.json",
        }
    )

    assert evidence == {
        "provider_id": "kimi-codingplan",
        "model": "kimi-for-coding",
    }
    assert "agent_path" not in evidence
