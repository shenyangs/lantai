from lantai.config.config import (
    BuiltinToolConfig,
    ToolsConfig,
    _apply_shell_sync_default_migration_to_raw,
)


def test_shell_async_execution_is_disabled_by_default():
    config = ToolsConfig()

    shell = config.builtin_tools["execute_shell_command"]
    assert shell.async_execution is False
    assert config.shell_sync_default_applied is True


def test_retired_external_assistant_tools_are_not_configured():
    config = ToolsConfig()

    assert "sessions_spawn" not in config.builtin_tools
    assert "delegate_external_agent" not in config.builtin_tools


def test_shell_async_default_is_reset_once_for_legacy_configs():
    config = ToolsConfig(
        builtin_tools={
            "execute_shell_command": BuiltinToolConfig(
                name="execute_shell_command",
                async_execution=True,
            ),
        },
    )

    assert config.builtin_tools["execute_shell_command"].async_execution is False
    assert config.shell_sync_default_applied is True


def test_user_enabled_shell_async_is_preserved_after_sync_marker():
    config = ToolsConfig(
        shell_sync_default_applied=True,
        builtin_tools={
            "execute_shell_command": BuiltinToolConfig(
                name="execute_shell_command",
                async_execution=True,
            ),
        },
    )

    assert config.builtin_tools["execute_shell_command"].async_execution is True


def test_raw_shell_sync_migration_adds_marker_and_disables_async():
    data = {
        "tools": {
            "builtin_tools": {
                "execute_shell_command": {
                    "name": "execute_shell_command",
                    "async_execution": True,
                },
            },
        },
    }

    assert _apply_shell_sync_default_migration_to_raw(data) is True
    shell = data["tools"]["builtin_tools"]["execute_shell_command"]
    assert shell["async_execution"] is False
    assert data["tools"]["shell_async_default_enabled_applied"] is True
    assert data["tools"]["shell_sync_default_applied"] is True


def test_raw_shell_sync_migration_respects_marker():
    data = {
        "tools": {
            "shell_sync_default_applied": True,
            "shell_async_default_enabled_applied": True,
            "builtin_tools": {
                "execute_shell_command": {
                    "name": "execute_shell_command",
                    "async_execution": True,
                },
            },
        },
    }

    assert _apply_shell_sync_default_migration_to_raw(data) is False
    shell = data["tools"]["builtin_tools"]["execute_shell_command"]
    assert shell["async_execution"] is True


def test_raw_shell_sync_migration_backfills_legacy_marker():
    data = {
        "tools": {
            "shell_sync_default_applied": True,
            "builtin_tools": {
                "execute_shell_command": {
                    "name": "execute_shell_command",
                    "async_execution": False,
                },
            },
        },
    }

    assert _apply_shell_sync_default_migration_to_raw(data) is True
    assert data["tools"]["shell_async_default_enabled_applied"] is True
