# -*- coding: utf-8 -*-
"""Transactional desktop workspace recovery tests."""

import json
from pathlib import Path

import pytest

from lantai.app import workspace_recovery
from lantai.config.config import AgentProfileConfig, AgentProfileRef, Config


def _prepare_recovery(monkeypatch, tmp_path):
    managed = tmp_path / "managed"
    external = tmp_path / "external"
    external.mkdir()
    target = managed / "workspaces" / "default"
    target.mkdir(parents=True)
    (target / "old.txt").write_text("old", encoding="utf-8")
    stage = managed / "workspace-imports" / "default-test"
    stage.mkdir(parents=True)
    agent = AgentProfileConfig(
        id="default",
        name="Recovered",
        workspace_dir=str(external),
    )
    (stage / "agent.json").write_text(
        agent.model_dump_json(indent=2),
        encoding="utf-8",
    )
    (stage / "new.txt").write_text("new", encoding="utf-8")
    config = Config()
    config.agents.profiles = {
        "default": AgentProfileRef(
            id="default",
            workspace_dir=str(external),
        ),
    }
    monkeypatch.setenv("LANTAI_DESKTOP_APP", "1")
    monkeypatch.setattr(workspace_recovery, "load_config", lambda: config)
    return managed, external, target, stage, config


def test_adopt_staged_workspace_backs_up_and_switches_config(
    monkeypatch,
    tmp_path,
):
    managed, external, target, stage, config = _prepare_recovery(
        monkeypatch,
        tmp_path,
    )
    saved = []
    monkeypatch.setattr(workspace_recovery, "save_config", saved.append)

    result = workspace_recovery.adopt_staged_desktop_workspace(
        agent_id="default",
        expected_source_dir=str(external),
        staged_workspace_dir=str(stage),
        working_dir=managed,
    )

    assert result["success"] is True
    assert result["source_preserved"] is True
    assert not stage.exists()
    assert (target / "new.txt").read_text(encoding="utf-8") == "new"
    assert config.agents.profiles["default"].workspace_dir == str(target)
    assert saved == [config]
    backup = result["backup_dir"]
    assert backup and (Path(backup) / "old.txt").exists()
    recovered = json.loads((target / "agent.json").read_text(encoding="utf-8"))
    assert recovered["workspace_dir"] == str(target)


def test_adopt_staged_workspace_rolls_back_when_config_save_fails(
    monkeypatch,
    tmp_path,
):
    managed, external, target, stage, _config = _prepare_recovery(
        monkeypatch,
        tmp_path,
    )

    def fail_save(_config):
        raise OSError("disk full")

    monkeypatch.setattr(workspace_recovery, "save_config", fail_save)

    with pytest.raises(OSError, match="disk full"):
        workspace_recovery.adopt_staged_desktop_workspace(
            agent_id="default",
            expected_source_dir=str(external),
            staged_workspace_dir=str(stage),
            working_dir=managed,
        )

    assert (target / "old.txt").read_text(encoding="utf-8") == "old"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"
