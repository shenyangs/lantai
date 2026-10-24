# -*- coding: utf-8 -*-
"""Behavior locks for Agent-native Skill and MCP management."""

from __future__ import annotations

import json

import httpx
import pytest

from lantai.agents.tools import extension_management
from lantai.agents.tools.read_skill import read_skill
from lantai.config.context import (
    WorkspaceContext,
    set_current_workspace_context,
)


class _FakeResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.request = httpx.Request("GET", "http://test/api/mcp")

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "request failed",
                request=self.request,
                response=httpx.Response(
                    self.status_code,
                    request=self.request,
                ),
            )


class _FakeClient:
    def __init__(self) -> None:
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def post(self, path, **kwargs):
        self.calls.append(("POST", path, kwargs))
        return _FakeResponse(
            {
                "key": "coffee",
                "name": "Coffee MCP",
                "enabled": True,
                "transport": "stdio",
            },
            status_code=201,
        )

    def get(self, path, **kwargs):
        self.calls.append(("GET", path, kwargs))
        if path == "/mcp/guanlan":
            return _FakeResponse(
                {
                    "client_key": "guanlan",
                    "name": "观澜 / Guanlan",
                    "enabled": True,
                    "capability_status": "error",
                    "connected": False,
                    "runtime_message": "观澜系统能力暂时不可用。",
                },
            )
        if path == "/mcp/chrome-devtools":
            return _FakeResponse(
                {
                    "client_key": "chrome-devtools",
                    "name": "Chrome DevTools",
                    "enabled": False,
                    "available": True,
                    "capability_status": "disabled",
                    "connected": False,
                    "runtime_message": "已内置，当前关闭。",
                },
            )
        return _FakeResponse(
            [
                {
                    "key": "coffee",
                    "name": "Coffee MCP",
                    "enabled": True,
                    "transport": "stdio",
                },
            ],
        )


@pytest.mark.asyncio
async def test_agent_installs_and_reads_local_skill_from_topic_workspace(
    tmp_path,
) -> None:
    agent_home = tmp_path / "agent"
    topic = agent_home / "topic-workspaces" / "chat-1"
    source = topic / "downloads" / "coffee"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\n"
        "name: coffee\n"
        "description: Use the coffee CLI.\n"
        "---\n"
        "Run coffee status.\n",
        encoding="utf-8",
    )
    set_current_workspace_context(
        WorkspaceContext(
            agent_home=agent_home,
            state_root=topic,
            project_root=topic,
            trust_state="trusted",
        ),
    )
    try:
        installed = await extension_management.install_agent_skill(
            "downloads/coffee",
        )
        loaded = await read_skill("coffee")
    finally:
        set_current_workspace_context(None)

    assert installed.is_error is False
    receipt = json.loads(installed.content[0]["text"])
    assert receipt["name"] == "coffee"
    assert receipt["enabled"] is True
    assert receipt["immediately_readable"] is True
    assert loaded.is_error is False
    assert "Use the coffee CLI" in loaded.content[0]["text"]


@pytest.mark.asyncio
async def test_agent_adds_mcp_through_canonical_control_plane(
    tmp_path,
    monkeypatch,
) -> None:
    agent_home = tmp_path / "agent"
    topic = tmp_path / "topic"
    topic.mkdir()
    config_file = topic / "coffee-mcp.json"
    config_file.write_text(
        json.dumps(
            {
                "name": "Coffee MCP",
                "transport": "stdio",
                "command": "coffee-mcp",
                "args": [],
                "enabled": True,
            },
        ),
        encoding="utf-8",
    )
    fake_client = _FakeClient()
    monkeypatch.setattr(
        extension_management,
        "create_agent_api_client",
        lambda _base_url: fake_client,
    )
    monkeypatch.setattr(
        extension_management,
        "get_current_agent_id",
        lambda: "default",
    )
    set_current_workspace_context(
        WorkspaceContext(
            agent_home=agent_home,
            state_root=topic,
            project_root=topic,
            trust_state="trusted",
        ),
    )
    try:
        response = await extension_management.manage_mcp_connector(
            "add",
            connector_key="coffee",
            config_file="coffee-mcp.json",
        )
    finally:
        set_current_workspace_context(None)

    assert response.is_error is False
    method, path, kwargs = fake_client.calls[0]
    assert method == "POST"
    assert path == "/mcp"
    assert kwargs["headers"]["X-Agent-Id"] == "default"
    assert kwargs["json"]["client_key"] == "coffee"
    assert kwargs["json"]["client"]["command"] == "coffee-mcp"


@pytest.mark.asyncio
async def test_agent_can_inspect_mcp_from_read_only_workspace(
    tmp_path,
    monkeypatch,
) -> None:
    agent_home = tmp_path / "agent"
    topic = tmp_path / "topic"
    topic.mkdir()
    fake_client = _FakeClient()
    monkeypatch.setattr(
        extension_management,
        "create_agent_api_client",
        lambda _base_url: fake_client,
    )
    monkeypatch.setattr(
        extension_management,
        "get_current_agent_id",
        lambda: "default",
    )
    set_current_workspace_context(
        WorkspaceContext(
            agent_home=agent_home,
            state_root=topic,
            project_root=topic,
            access_mode="read_only",
            trust_state="trusted",
        ),
    )
    try:
        response = await extension_management.inspect_mcp_connectors()
    finally:
        set_current_workspace_context(None)

    assert response.is_error is False
    assert fake_client.calls[0][0:2] == ("GET", "/mcp")
    assert fake_client.calls[1][0:2] == ("GET", "/mcp/guanlan")
    assert fake_client.calls[2][0:2] == ("GET", "/mcp/chrome-devtools")
    assert fake_client.calls[0][2]["headers"]["X-Agent-Id"] == "default"
    payload = json.loads(response.content[0]["text"])
    assert payload["connectors"][0]["key"] == "coffee"
    assert payload["native_capabilities"][0]["client_key"] == "guanlan"
    assert payload["native_capabilities"][0]["capability_status"] == "error"
    assert payload["native_capabilities"][1]["client_key"] == "chrome-devtools"
    assert payload["native_capabilities"][1]["enabled"] is False
    assert payload["native_capabilities"][1]["available"] is True
    assert payload["native_capabilities"][1]["capability_status"] == "disabled"
    assert "install" not in payload["native_capabilities"][0]


def test_mcp_inspection_keeps_custom_connectors_when_native_status_is_unavailable(
    monkeypatch,
) -> None:
    class Client(_FakeClient):
        def get(self, path, **kwargs):
            if path == "/mcp/guanlan":
                return _FakeResponse({"detail": "failed"}, status_code=500)
            return super().get(path, **kwargs)

    monkeypatch.setattr(
        extension_management,
        "create_agent_api_client",
        lambda _base_url: Client(),
    )

    payload = extension_management._mcp_request(
        action="list",
        connector_key="",
        config=None,
        agent_id="default",
        base_url="",
    )

    assert payload["connectors"][0]["key"] == "coffee"
    assert payload["native_capabilities"][0] == (
        {
            "client_key": "guanlan",
            "name": "观澜 / Guanlan",
            "capability_status": "error",
            "connected": False,
            "runtime_message": "Native capability status is temporarily unavailable.",
        }
    )
    assert payload["native_capabilities"][1]["client_key"] == "chrome-devtools"
    assert payload["native_capabilities"][1]["capability_status"] == "disabled"


def test_mcp_inspection_reports_chrome_status_failure_independently(
    monkeypatch,
) -> None:
    class Client(_FakeClient):
        def get(self, path, **kwargs):
            if path == "/mcp/chrome-devtools":
                return _FakeResponse({"detail": "failed"}, status_code=500)
            return super().get(path, **kwargs)

    monkeypatch.setattr(
        extension_management,
        "create_agent_api_client",
        lambda _base_url: Client(),
    )

    payload = extension_management._mcp_request(
        action="list",
        connector_key="",
        config=None,
        agent_id="default",
        base_url="",
    )

    assert payload["connectors"][0]["key"] == "coffee"
    assert payload["native_capabilities"][0]["client_key"] == "guanlan"
    chrome = payload["native_capabilities"][1]
    assert chrome["client_key"] == "chrome-devtools"
    assert chrome["enabled"] is False
    assert chrome["available"] is False
    assert chrome["capability_status"] == "error"
    assert chrome["connected"] is False
