# -*- coding: utf-8 -*-
"""Regression tests for builtin team agent repair."""

from pathlib import Path
from types import SimpleNamespace

from lantai.app import migration
from lantai.config.config import AgentProfileRef


def test_existing_team_agent_repairs_missing_role_skill(
    monkeypatch,
    tmp_path,
):
    """Existing builtin team agents should not remain empty shells."""
    workspace = tmp_path / "Lantai_Water_Agent"
    workspace.mkdir()

    config = SimpleNamespace(
        agents=SimpleNamespace(
            language="zh",
            active_agent="default",
            profiles={
                "Lantai_Water_Agent": AgentProfileRef(
                    id="Lantai_Water_Agent",
                    workspace_dir=str(workspace),
                ),
            },
        ),
    )
    agent_config = SimpleNamespace(
        name="白水",
        description="开发工具 / 自动化 — old",
        active_model=SimpleNamespace(provider_id="p", model="m"),
        system_prompt_files=[
            "AGENTS.md",
            "SOUL.md",
            "PROFILE.md",
            "BACKSTORY.md",
            "SELF_CANON.md",
        ],
    )

    installed: list[tuple[str, Path, bool]] = []
    imported: list[list[dict[str, str]]] = []
    saved_agent_configs: list[object] = []

    class FakeSkillModule:
        @staticmethod
        def import_builtin_skills(imports=None):
            imported.append(imports or [])
            return {
                "imported": ["water"],
                "updated": [],
                "unchanged": [],
                "conflicts": [],
            }

    class FakePoolService:
        def download_to_workspace(
            self,
            skill_name,
            workspace_dir,
            *,
            overwrite=False,
        ):
            installed.append((skill_name, workspace_dir, overwrite))
            return {"success": True, "name": skill_name}

    monkeypatch.setattr(
        migration,
        "BUILTIN_TEAM_AGENT_CONFIGS",
        (
            {
                "id": "Lantai_Water_Agent",
                "name": "白水",
                "description": (
                    "开发工具 / 自动化 — 负责代码、脚本、工具调用、"
                    "自动化、Agent人格体系和skill工程落地"
                ),
                "skill_name": "water",
                "emoji": "💧",
            },
        ),
    )
    monkeypatch.setattr(migration, "load_config", lambda: config)
    monkeypatch.setattr(migration, "load_agent_config", lambda agent_id: agent_config)
    monkeypatch.setattr(
        migration,
        "save_agent_config",
        lambda _agent_id, value: saved_agent_configs.append(value),
    )
    monkeypatch.setattr(migration, "save_config", lambda config_obj: None)
    monkeypatch.setattr(
        migration,
        "_ensure_workspace_json_files",
        lambda workspace_dir, label: None,
    )
    monkeypatch.setattr(
        "lantai.agents.skills_manager.import_builtin_skills",
        FakeSkillModule.import_builtin_skills,
    )
    monkeypatch.setattr(
        "lantai.agents.skills_manager.SkillPoolService",
        FakePoolService,
    )

    migration._do_ensure_team_agents()

    assert imported == [[{"skill_name": "water", "language": "zh"}]]
    assert installed == [("water", workspace, False)]
    agents_md = (workspace / "AGENTS.md").read_text(encoding="utf-8")
    assert "白水" in agents_md
    assert "内部运行实现，不是前台身份" in agents_md
    assert "用户当前明确要求引用、安装、查看、管理或调试" in agents_md
    assert agent_config.system_prompt_files == ["AGENTS.md"]
    assert saved_agent_configs == [agent_config]


def test_new_builtin_team_agent_template_uses_only_module_contract() -> None:
    """Builtin specialists must not inherit the companion persona file set."""
    agent_config = SimpleNamespace(
        system_prompt_files=[
            "AGENTS.md",
            "SOUL.md",
            "PROFILE.md",
            "BACKSTORY.md",
            "SELF_CANON.md",
        ],
    )

    changed = migration._apply_builtin_team_agent_prompt_boundary(agent_config)

    assert changed is True
    assert agent_config.system_prompt_files == ["AGENTS.md"]
    assert migration._apply_builtin_team_agent_prompt_boundary(agent_config) is False


def test_team_agent_skill_repair_overwrites_builtin_language_switch(
    monkeypatch,
    tmp_path,
):
    """Builtin role skills should follow the current builtin language."""
    calls: list[bool] = []

    class FakePoolService:
        def download_to_workspace(
            self,
            skill_name,
            workspace_dir,
            *,
            overwrite=False,
        ):
            calls.append(overwrite)
            if not overwrite:
                return {"success": False, "reason": "language_switch"}
            return {"success": True, "name": skill_name}

    monkeypatch.setattr(
        "lantai.agents.skills_manager.import_builtin_skills",
        lambda imports=None: {
            "imported": [],
            "updated": [],
            "unchanged": ["matcha"],
            "conflicts": [],
        },
    )
    monkeypatch.setattr(
        "lantai.agents.skills_manager.SkillPoolService",
        FakePoolService,
    )

    migration._ensure_team_agent_skill_installed(
        tmp_path,
        "matcha",
        "Lantai_Matcha_Agent",
    )

    assert calls == [False, True]


def test_roster_upgrade_disables_retired_agents_without_deleting_history() -> None:
    retired_id = "Lantai_Cola_Agent"
    retained_workspace = "/tmp/historical-cola-workspace"
    config = SimpleNamespace(
        agents=SimpleNamespace(
            active_agent=retired_id,
            profiles={
                retired_id: AgentProfileRef(
                    id=retired_id,
                    workspace_dir=retained_workspace,
                ),
                "default": AgentProfileRef(
                    id="default",
                    workspace_dir="/tmp/default-workspace",
                ),
            },
        ),
    )

    changed = migration._disable_retired_team_agents_for_roster_upgrade(
        config,
        roster_upgrade_needed=True,
    )

    assert changed is True
    assert retired_id in config.agents.profiles
    assert config.agents.profiles[retired_id].workspace_dir == retained_workspace
    assert config.agents.profiles[retired_id].enabled is False
    assert config.agents.active_agent == "default"


def test_retired_agent_can_be_reenabled_after_roster_upgrade() -> None:
    retired_id = "Lantai_Cola_Agent"
    config = SimpleNamespace(
        agents=SimpleNamespace(
            active_agent="default",
            profiles={
                retired_id: AgentProfileRef(
                    id=retired_id,
                    workspace_dir="/tmp/historical-cola-workspace",
                    enabled=True,
                ),
            },
        ),
    )

    changed = migration._disable_retired_team_agents_for_roster_upgrade(
        config,
        roster_upgrade_needed=False,
    )

    assert changed is False
    assert config.agents.profiles[retired_id].enabled is True
