# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""The source Provider smoke follows the configured global model."""

import importlib.util
import json
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "source_configured_provider_stream_smoke.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "configured_provider_follow_global",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stage_profile_uses_global_model_when_agent_follows_active(
    tmp_path: Path,
) -> None:
    module = _load_module()
    source_working = tmp_path / "working"
    source_workspace = source_working / "workspaces" / "default"
    source_workspace.mkdir(parents=True)
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
        json.dumps({"id": "default", "runtime": {"engine": "native"}}),
        encoding="utf-8",
    )
    source_secret = tmp_path / "secret"
    providers = source_secret / "providers"
    (providers / "builtin").mkdir(parents=True)
    (source_secret / ".master_key").write_text(
        "not-a-real-key",
        encoding="utf-8",
    )
    (providers / "active_model.json").write_text(
        json.dumps(
            {
                "provider_id": "kimi-codingplan",
                "model": "kimi-for-coding",
            },
        ),
        encoding="utf-8",
    )
    (providers / "builtin" / "kimi-codingplan.json").write_text(
        "{}",
        encoding="utf-8",
    )

    _working, _secret, configured = module._stage_profile(
        source_working_dir=source_working,
        source_secret_dir=source_secret,
        agent_id="default",
        target_root=tmp_path / "staged",
    )

    assert configured["provider_id"] == "kimi-codingplan"
    assert configured["model"] == "kimi-for-coding"
