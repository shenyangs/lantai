# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from types import SimpleNamespace

from lantai.agents.tools import customization
from lantai.agents.tool_registry import _input_schema_from_callable
from lantai.agents import tool_registry
from lantai.customization.store import CustomizationStore


def _payload(response):
    block = response.content[0]
    return json.loads(block["text"] if isinstance(block, dict) else block.text)


async def test_chat_tool_only_prepares_plan_and_does_not_activate(monkeypatch, tmp_path):
    monkeypatch.setattr(customization, "WORKING_DIR", tmp_path)

    response = await customization.prepare_lantai_customization(
        intent="隐藏记忆入口",
        operations=[
            {
                "op": "set",
                "path": "navigation.sidebar.memory.visible",
                "value": False,
            }
        ],
    )

    payload = _payload(response)
    store = CustomizationStore(tmp_path / "customization")
    assert payload["type"] == "lantai_customization_draft"
    assert payload["preview"]["changed"]
    assert store.get_state().mode == "factory"
    assert store.get_state().active_profile_id is None
    assert store.get_profile(payload["profile"]["id"]).revision == 0


async def test_catalog_tool_excludes_immutable_core(monkeypatch, tmp_path):
    monkeypatch.setattr(customization, "WORKING_DIR", tmp_path)

    response = await customization.inspect_lantai_customization("颜色")

    payload = _payload(response)
    assert payload["type"] == "lantai_customization_catalog"
    assert any(item["id"] == "appearance.palette" for item in payload["items"])
    assert all(not item["id"].startswith("core.") for item in payload["items"])


def test_prepare_tool_exposes_operations_as_an_array_schema():
    schema = _input_schema_from_callable(
        customization.prepare_lantai_customization
    )

    assert schema["properties"]["operations"]["type"] == "array"
    assert schema["properties"]["operations"]["items"]["type"] == "object"


def test_safe_recovery_mode_registers_only_read_only_builtin_tools(monkeypatch):
    monkeypatch.setenv("LANTAI_RECOVERY_SAFE_MODE", "1")
    monkeypatch.setattr(tool_registry, "_customization_allowed_tools", lambda: None)
    registry = tool_registry.build_lantai_tool_registry(
        agent_config=SimpleNamespace(),
        include_default_policy_middleware=False,
    )
    names = {spec.name for spec, _executor in registry.tool_items()}

    assert "read_file" in names
    assert "inspect_lantai_customization" in names
    assert "write_file" not in names
    assert "prepare_lantai_customization" not in names
