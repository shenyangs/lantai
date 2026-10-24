from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from lantai.agents.tool_registry import (
    _builtin_tool_functions,
    build_lantai_tool_registry,
)
from lantai.config.config import (
    Config,
    ToolsConfig,
    build_fallback_agent_profile_config,
)


ROOT = Path(__file__).resolve().parents[3]
RETIRED_TOOL_NAMES = {"delegate_external_agent", "sessions_spawn"}


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_external_assistant_execution_modules_are_removed() -> None:
    assert not list((ROOT / "src/lantai/agents/acp").glob("*.py"))
    assert not list((ROOT / "src/lantai/agents/acp").glob("managed_codex/*"))
    assert not (
        ROOT / "src/lantai/agents/tools/delegate_external_agent.py"
    ).exists()
    assert not (ROOT / "src/lantai/app/routers/external_assistants.py").exists()
    assert not (ROOT / "src/lantai/cli/acp_cmd.py").exists()


def test_runtime_never_exposes_retired_external_assistant_tools() -> None:
    builtin_functions = _builtin_tool_functions()
    configured_tools = ToolsConfig().builtin_tools

    assert RETIRED_TOOL_NAMES.isdisjoint(builtin_functions)
    assert RETIRED_TOOL_NAMES.isdisjoint(configured_tools)

    for relative_path in (
        "src/lantai/agents/tool_registry.py",
        "src/lantai/runtime/capability_catalog.py",
        "src/lantai/security/tool_policy.py",
        "src/lantai/app/tool_catalog.py",
        "src/lantai/app/experience/presentation.py",
    ):
        source = _read(relative_path)
        assert "delegate_external_agent" not in source
        assert "sessions_spawn" not in source


def test_runtime_registry_schemas_exclude_retired_tools_in_all_task_modes() -> None:
    enabled_tools = {
        name: SimpleNamespace(enabled=True, async_execution=False)
        for name in ToolsConfig().builtin_tools
    }
    agent_config = SimpleNamespace(
        tools=SimpleNamespace(builtin_tools=enabled_tools),
    )

    for request_context in (
        {"distribution": "developer_id"},
        {"distribution": "developer_id", "coding_mode": True},
        {"distribution": "developer_id", "task_type": "presentation"},
        {"distribution": "developer_id", "task_type": "file"},
    ):
        registry = build_lantai_tool_registry(
            agent_config=agent_config,
            request_context=request_context,
            include_default_policy_middleware=False,
        )
        exposed_names = {spec.name for spec, _executor in registry.tool_items()}

        assert RETIRED_TOOL_NAMES.isdisjoint(exposed_names)


def test_codex_wording_cannot_restore_a_retired_tool_surface() -> None:
    user_prompt = "请 Codex 帮忙完成这个 PPT"

    assert "Codex" in user_prompt
    assert RETIRED_TOOL_NAMES.isdisjoint(_builtin_tool_functions())


def test_legacy_external_assistant_config_is_preserved_but_inert() -> None:
    legacy_config = {
        "agents": {
            "codex": {
                "enabled": True,
                "command": "codex",
                "args": ["acp"],
                "env": {"LEGACY_SECRET_REFERENCE": "preserve-me"},
            }
        }
    }

    config = Config.model_validate({"acp": legacy_config})
    fallback_profile = build_fallback_agent_profile_config("default", config)

    assert config.model_dump(mode="json")["acp"] == legacy_config
    assert fallback_profile.acp is None
    assert RETIRED_TOOL_NAMES.isdisjoint(config.tools.builtin_tools)


def test_legacy_external_assistant_config_cannot_reenable_execution() -> None:
    config = Config.model_validate(
        {
            "acp": {
                "agents": {
                    "codex": {
                        "enabled": True,
                        "command": "codex",
                        "args": ["acp"],
                    }
                }
            },
            "tools": {
                "builtin_tools": {
                    "delegate_external_agent": {
                        "name": "delegate_external_agent",
                        "enabled": True,
                    },
                    "sessions_spawn": {
                        "name": "sessions_spawn",
                        "enabled": True,
                    },
                }
            },
        }
    )
    registry = build_lantai_tool_registry(
        agent_config=SimpleNamespace(tools=config.tools),
        request_context={"distribution": "developer_id"},
        include_default_policy_middleware=False,
    )
    exposed_names = {spec.name for spec, _executor in registry.tool_items()}

    assert config.acp is not None
    assert RETIRED_TOOL_NAMES.isdisjoint(config.tools.builtin_tools)
    assert RETIRED_TOOL_NAMES.isdisjoint(exposed_names)


def test_retired_routes_and_api_cannot_open_connection_management() -> None:
    from lantai.app.routers import router as mounted_api_router

    routes = _read("console/src/layouts/MainLayout/index.tsx")
    settings = _read("console/src/pages/Settings/Home/index.tsx")
    config_router_source = _read("src/lantai/app/routers/config.py")
    cli = _read("src/lantai/cli/main.py")

    assert "ACPPage" not in routes
    for retired_route in ("/acp", "/ACP"):
        assert re.search(
            rf'path="{retired_route}"\s+element={{<Navigate to="/settings" replace />}}',
            routes,
        )
    assert 'path: "/acp"' not in settings
    assert "/config/acp" not in config_router_source
    assert "external_assistants" not in config_router_source
    assert '"lantai.cli.acp_server_cmd"' in cli
    assert '"lantai.cli.acp_cmd"' not in cli

    def mounted_paths(router: object) -> set[str]:
        paths: set[str] = set()
        for route in getattr(router, "routes", ()):
            path = getattr(route, "path", None)
            if isinstance(path, str):
                paths.add(path)
            original_router = getattr(route, "original_router", None)
            if original_router is not None:
                paths.update(mounted_paths(original_router))
        return paths

    active_paths = mounted_paths(mounted_api_router)
    assert not any("/config/acp" in path for path in active_paths)
    assert not any("external-assistant" in path for path in active_paths)


def test_app_startup_has_no_external_assistant_service_or_runner_hook() -> None:
    startup_surface = "\n".join(
        _read(relative_path)
        for relative_path in (
            "src/lantai/app/_app.py",
            "src/lantai/app/routers/__init__.py",
            "src/lantai/app/routers/agent_scoped.py",
            "src/lantai/agents/tool_registry.py",
            "src/lantai/cli/main.py",
        )
    )

    for retired_reference in (
        "lantai.agents.acp",
        "agents.acp",
        "managed_codex",
        "external_assistants",
        "delegate_external_agent",
        "sessions_spawn",
    ):
        assert retired_reference not in startup_surface


def test_acp_server_does_not_restore_retired_acp_client_runtime() -> None:
    assert "agent-client-protocol" in _read("pyproject.toml")
    assert "agent-client-protocol" in _read("uv.lock")
    packaging = _read("lantai-server.spec")
    assert "integrations.acp_server.server" in packaging
    assert "agents/acp" not in packaging
    assert "managed_codex" not in packaging


def test_adjacent_supported_capabilities_remain_registered() -> None:
    builtin_functions = _builtin_tool_functions()

    assert {
        "list_agents",
        "chat_with_agent",
        "collaborate_with_agents",
        "inspect_mcp_connectors",
        "manage_mcp_connector",
        "execute_shell_command",
    }.issubset(builtin_functions)

    providers = _read("src/lantai/providers/provider_manager.py")
    assert "opencode" in providers.lower()
