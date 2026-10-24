from types import SimpleNamespace

from lantai.backup._ops import estimate as estimate_module
from lantai.backup.models import BackupScope, CreateBackupRequest


def test_estimate_backup_reports_exact_scope_without_paths(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "note.md").write_bytes(b"12345")
    (workspace / "data.bin").write_bytes(b"1234567")
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "token").write_bytes(b"secret")
    skill_pool = tmp_path / "skills"
    skill_pool.mkdir()
    (skill_pool / "SKILL.md").write_bytes(b"skill")
    (tmp_path / "config.json").write_bytes(b"{}")

    config = SimpleNamespace(
        agents=SimpleNamespace(
            profiles={"default": SimpleNamespace(workspace_dir=str(workspace))}
        )
    )
    monkeypatch.setattr(estimate_module, "load_config", lambda: config)
    monkeypatch.setattr(estimate_module, "WORKING_DIR", tmp_path)
    monkeypatch.setattr(estimate_module, "SECRET_DIR", secrets)
    monkeypatch.setattr(
        "lantai.agents.skills_manager.get_skill_pool_dir", lambda: skill_pool
    )

    estimate = estimate_module.estimate_backup(
        CreateBackupRequest(
            name="estimate",
            agents=["default"],
            scope=BackupScope(
                include_agents=True,
                include_global_config=True,
                include_skill_pool=True,
                include_secrets=False,
            ),
        )
    )

    assert estimate.files == 4
    assert estimate.bytes == 19
    assert estimate.missing_agents == []
    assert [item.label for item in estimate.components] == [
        "智能体 · default",
        "全局设置",
        "技能库",
    ]
    assert all("/" not in item.label for item in estimate.components)
