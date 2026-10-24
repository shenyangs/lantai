from types import SimpleNamespace

from lantai.backup._ops.create import _compute_initial_agents
from lantai.backup._ops.scope import resolve_requested_agent_ids
from lantai.backup.models import BackupScope


def _config():
    return SimpleNamespace(
        agents=SimpleNamespace(
            profiles={
                "default": SimpleNamespace(workspace_dir="/tmp/default"),
                "research": SimpleNamespace(workspace_dir="/tmp/research"),
            }
        )
    )


def test_complete_disaster_scope_resolves_every_configured_agent():
    scope = BackupScope(
        include_agents=True,
        include_global_config=True,
        include_secrets=True,
        include_skill_pool=True,
        sanitized=False,
    )

    assert resolve_requested_agent_ids(scope, ["default"], _config()) == [
        "default",
        "research",
    ]
    valid, missing = _compute_initial_agents(["default"], _config(), scope)
    assert [agent_id for agent_id, _ in valid] == ["default", "research"]
    assert missing == []


def test_sanitized_partial_scope_preserves_the_explicit_subset():
    scope = BackupScope(
        include_agents=True,
        include_global_config=True,
        include_secrets=False,
        include_skill_pool=True,
        sanitized=True,
    )

    assert resolve_requested_agent_ids(scope, ["default"], _config()) == [
        "default"
    ]
