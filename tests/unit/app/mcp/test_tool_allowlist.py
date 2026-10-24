# -*- coding: utf-8 -*-
"""MCP tool authorization tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lantai.app.mcp.stateful_client import LantaiStatefulClientBase


class _Session:
    async def list_tools(self):
        return SimpleNamespace(
            tools=[SimpleNamespace(name="read"), SimpleNamespace(name="delete")],
        )

    async def call_tool(self, name, arguments):
        return {"name": name, "arguments": arguments}


class _Client(LantaiStatefulClientBase):
    async def list_tools(self):
        self._validate_connection()
        result = await self.session.list_tools()
        self._cached_tools = self._filter_allowed_tools(result.tools)
        return self._cached_tools

    async def call_tool(self, name, arguments=None):
        self._validate_connection()
        self._validate_tool_allowed(name)
        return await self.session.call_tool(name, arguments or {})

    def _validate_connection(self):
        if not self.is_connected or self.session is None:
            raise RuntimeError("not connected")


@pytest.mark.asyncio
async def test_allowlist_filters_discovery_and_blocks_direct_call():
    client = _Client("test")
    client.is_connected = True
    client.session = _Session()
    client.allowed_tools = frozenset({"read"})

    assert [tool.name for tool in await client.list_tools()] == ["read"]
    assert await client.call_tool("read", {"path": "a"}) == {
        "name": "read",
        "arguments": {"path": "a"},
    }
    with pytest.raises(PermissionError, match="not permitted"):
        await client.call_tool("delete", {})


@pytest.mark.asyncio
async def test_empty_allowlist_preserves_legacy_allow_all_behavior():
    client = _Client("test")
    client.is_connected = True
    client.session = _Session()

    assert [tool.name for tool in await client.list_tools()] == ["read", "delete"]

