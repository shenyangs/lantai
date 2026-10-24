"""Regression tests for the source Chat Runtime renderer gate."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "source_chat_runtime_renderer_smoke.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "source_chat_runtime_renderer_smoke",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_legacy_profile_fixture_requires_runtime_migration(tmp_path: Path) -> None:
    module = _load_module()

    profile = module._write_legacy_runtime_profile_for_smoke(tmp_path / "working")

    agent_path = Path(profile["agent_path"])
    config_path = Path(profile["config_path"])
    agent = json.loads(agent_path.read_text(encoding="utf-8"))
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert agent["runtime"]["engine"] == "legacy"
    assert config["agents"]["active_agent"] == "default"
    assert config["agents"]["profiles"]["default"]["workspace_dir"] == str(
        agent_path.parent,
    )
