# -*- coding: utf-8 -*-
"""Tool permission profile endpoint tests."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from lantai.app.routers import tools as tools_router
from lantai.app.tool_catalog import get_tool_catalog_metadata
from lantai.config.config import BuiltinToolConfig
from lantai.security.tool_policy import list_permission_profiles

app = FastAPI()
app.include_router(tools_router.router, prefix="/api")


async def test_get_tool_permission_profiles():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/tools/permission-profiles")

    assert resp.status_code == 200
    body = resp.json()
    expected_ids = [profile.id for profile in list_permission_profiles()]
    assert [item["id"] for item in body] == expected_ids
    assert all(item["description"] for item in body)


async def test_app_store_tool_list_hides_shell_and_python(
    monkeypatch,
) -> None:
    async def _workspace_for_request(_request):
        return SimpleNamespace(agent_id="agent-1")

    agent_config = SimpleNamespace(
        tools=SimpleNamespace(
            builtin_tools={
                "execute_shell_command": BuiltinToolConfig(
                    name="execute_shell_command",
                    enabled=True,
                ),
                "execute_python_code": BuiltinToolConfig(
                    name="execute_python_code",
                    enabled=True,
                ),
                "read_file": BuiltinToolConfig(name="read_file", enabled=True),
            },
        ),
    )

    monkeypatch.setenv("LANTAI_DISTRIBUTION_CHANNEL", "app_store")
    monkeypatch.setattr(
        "lantai.app.agent_context.get_agent_for_request",
        _workspace_for_request,
    )
    monkeypatch.setattr(
        "lantai.config.config.load_agent_config",
        lambda _agent_id: agent_config,
    )
    monkeypatch.setattr(
        "lantai.plugins.registry.PluginRegistry",
        lambda: SimpleNamespace(
            get_all_plugin_manifests=lambda: {},
            get_all_tools=lambda: {},
        ),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/tools")

    assert resp.status_code == 200
    names = {item["name"] for item in resp.json()}
    assert "execute_shell_command" not in names
    assert "execute_python_code" not in names
    assert "read_file" in names
    read_file = next(
        item for item in resp.json() if item["name"] == "read_file"
    )
    assert read_file["display_name"] == "读取文件"
    assert read_file["category"] == "files"
    assert read_file["locked"] is True
    assert read_file["risk_hint"] == "read_only"


async def test_tool_list_hides_stale_unloaded_plugin_config(
    monkeypatch,
) -> None:
    async def _workspace_for_request(_request):
        return SimpleNamespace(agent_id="agent-1")

    agent_config = SimpleNamespace(
        tools=SimpleNamespace(
            builtin_tools={
                "read_file": BuiltinToolConfig(
                    name="read_file",
                    enabled=True,
                ),
                "retired_inspect": BuiltinToolConfig(
                    name="retired_inspect",
                    enabled=True,
                ),
            },
        ),
    )
    empty_registry = SimpleNamespace(
        get_all_plugin_manifests=lambda: {},
        get_all_tools=lambda: {},
        get_plugin_id_for_tool=lambda _name: None,
    )
    monkeypatch.setattr(
        "lantai.app.agent_context.get_agent_for_request",
        _workspace_for_request,
    )
    monkeypatch.setattr(
        "lantai.config.config.load_agent_config",
        lambda _agent_id: agent_config,
    )
    monkeypatch.setattr(
        "lantai.plugins.registry.PluginRegistry",
        lambda: empty_registry,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        resp = await client.get("/api/tools")

    assert resp.status_code == 200
    assert [item["name"] for item in resp.json()] == ["read_file"]


async def test_core_tool_cannot_be_disabled(
    monkeypatch,
) -> None:
    async def _workspace_for_request(_request):
        return SimpleNamespace(agent_id="agent-1")

    agent_config = SimpleNamespace(
        tools=SimpleNamespace(
            builtin_tools={
                "read_file": BuiltinToolConfig(
                    name="read_file",
                    enabled=True,
                ),
            },
        ),
    )
    saved = []

    monkeypatch.setattr(
        "lantai.app.agent_context.get_agent_for_request",
        _workspace_for_request,
    )
    monkeypatch.setattr(
        "lantai.config.config.load_agent_config",
        lambda _agent_id: agent_config,
    )
    monkeypatch.setattr(
        "lantai.config.config.save_agent_config",
        lambda *args: saved.append(args),
    )
    monkeypatch.setattr(
        "lantai.plugins.registry.PluginRegistry",
        lambda: SimpleNamespace(get_plugin_id_for_tool=lambda _name: None),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch("/api/tools/read_file/toggle")

    assert resp.status_code == 409
    assert agent_config.tools.builtin_tools["read_file"].enabled is True
    assert saved == []


async def test_legacy_disabled_core_tool_can_be_restored(
    monkeypatch,
) -> None:
    async def _workspace_for_request(_request):
        return SimpleNamespace(agent_id="agent-1")

    agent_config = SimpleNamespace(
        tools=SimpleNamespace(
            builtin_tools={
                "read_file": BuiltinToolConfig(
                    name="read_file",
                    enabled=False,
                    description="Read file contents",
                    icon="📄",
                ),
            },
        ),
    )
    saved = []

    monkeypatch.setattr(
        "lantai.app.agent_context.get_agent_for_request",
        _workspace_for_request,
    )
    monkeypatch.setattr(
        "lantai.config.config.load_agent_config",
        lambda _agent_id: agent_config,
    )
    monkeypatch.setattr(
        "lantai.config.config.save_agent_config",
        lambda *args: saved.append(args),
    )
    monkeypatch.setattr(
        "lantai.plugins.registry.PluginRegistry",
        lambda: SimpleNamespace(get_plugin_id_for_tool=lambda _name: None),
    )
    monkeypatch.setattr(
        tools_router,
        "schedule_agent_reload",
        lambda *_args: None,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch("/api/tools/read_file/toggle")

    assert resp.status_code == 200
    assert resp.json()["enabled"] is True
    assert resp.json()["locked"] is True
    assert len(saved) == 1


def test_plugin_tools_use_extension_catalogue_metadata() -> None:
    metadata = get_tool_catalog_metadata(
        "custom_connector",
        origin="plugin",
    )

    assert metadata["category"] == "extensions"
    assert metadata["source"] == "plugin"
    assert metadata["source_label"] == "插件"
    assert metadata["locked"] is False
    assert metadata["risk_hint"] == "external_action"
