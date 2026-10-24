# -*- coding: utf-8 -*-
"""Behavior locks for Lantai runtime tool registry."""

# pylint: disable=consider-using-from-import,protected-access,redefined-builtin
# pylint: disable=unused-argument,use-implicit-booleaness-not-comparison

from __future__ import annotations

import builtins
from dataclasses import dataclass, field
from time import time_ns
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from mcp import types as mcp_types
import pytest

import lantai.agents.tools.compat_builtin as compat_builtin
import lantai.agents.tool_registry as tool_registry_module
from lantai.agents.tool_registry import (
    build_lantai_tool_registry,
    build_mcp_client_registry,
    build_tool_policy_context,
    is_tool_registration_allowed,
    mcp_client_spec_from_client,
    tool_registration_matrix_entry,
)
from lantai.runtime.tools import (
    LantaiToolRegistry,
    SkillSpec,
    ToolCall,
    ToolPolicyContext,
    ToolPolicyOutcome,
    ToolSpec,
)
from lantai.runtime.tool_response import TextBlock, ToolResponse
from lantai.runtime.adapters.legacy_toolkit_adapter import LegacyToolkitAdapter
from lantai.runtime.adapters.legacy_toolkit_components import (
    legacy_mcp_content_to_blocks,
)
from lantai.security.tool_policy import _detect_distribution
from lantai.security.tool_guard.models import ToolGuardResult


@dataclass
class _FakeLegacyToolResponse:
    content: list[dict[str, Any]]
    metadata: dict[str, Any] | None = None
    stream: bool = False
    is_last: bool = True
    is_interrupted: bool = False
    id: str = field(default_factory=lambda: str(time_ns()))


class _ToolConfig:
    def __init__(
        self,
        enabled: bool = True,
        async_execution: bool = False,
    ) -> None:
        self.enabled = enabled
        self.async_execution = async_execution


def _agent_config(builtin_tools: dict[str, _ToolConfig] | None = None):
    return SimpleNamespace(
        tools=SimpleNamespace(builtin_tools=builtin_tools or {}),
    )


def _developer_id_context() -> dict[str, str]:
    return {"distribution": "developer_id"}


def _skip_developer_id_registry_when_app_store_runtime() -> None:
    if _detect_distribution() == "app_store":
        pytest.skip(
            "App Store runtime intentionally removes shell/Python tools",
        )


class _FakeToolkit:
    tool_response_type = _FakeLegacyToolResponse

    def __init__(self) -> None:
        self.registered = []

    def register_tool_function(self, func, **kwargs):
        self.registered.append((func, kwargs))


def test_registry_keeps_tool_skill_and_async_metadata() -> None:
    async def demo_tool() -> str:
        return "ok"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="demo_tool",
            origin="builtin",
            async_execution=True,
        ),
        demo_tool,
    )
    registry.register_skill(SkillSpec(name="alpha", path="/tmp/alpha"))

    tool_spec, executor = registry.tool_items()[0]

    assert tool_spec.name == "demo_tool"
    assert tool_spec.async_execution is True
    assert executor is demo_tool
    assert registry.has_async_tools() is True
    assert registry.skill_specs()[0].path == "/tmp/alpha"


def test_registry_blocked_tools_stay_hidden_across_late_registration() -> None:
    registry = LantaiToolRegistry()
    registry.block_tools({"edit_file"})

    registry.register_tool(ToolSpec(name="edit_file"), lambda: "blocked")
    registry.register_tool(ToolSpec(name="read_file"), lambda: "visible")

    assert registry.blocked_tool_names() == frozenset({"edit_file"})
    assert registry.get_tool("edit_file") is None
    assert [spec.name for spec, _executor in registry.tool_items()] == [
        "read_file",
    ]


def test_runtime_context_blocks_builtin_tool_registration() -> None:
    registry = build_lantai_tool_registry(
        agent_config=_agent_config(
            {
                "edit_file": _ToolConfig(enabled=True),
                "read_file": _ToolConfig(enabled=True),
            },
        ),
        request_context={
            "distribution": "developer_id",
            "runtime_blocked_tool_names": ["edit_file"],
        },
    )

    assert registry.get_tool("edit_file") is None
    assert registry.get_tool("read_file") is not None


@pytest.mark.asyncio
async def test_exact_knowledge_scope_replaces_search_and_read_executors(
    monkeypatch,
) -> None:
    search = AsyncMock(return_value=ToolResponse(content=[]))
    read = AsyncMock(return_value=ToolResponse(content=[]))
    monkeypatch.setattr(tool_registry_module, "wiki_search_for_sources", search)
    monkeypatch.setattr(tool_registry_module, "wiki_read_for_sources", read)
    source_paths = [".wiki/raw/sources/alpha.txt"]
    registry = build_lantai_tool_registry(
        agent_config=_agent_config(
            {
                "wiki_search": _ToolConfig(enabled=True),
                "wiki_read": _ToolConfig(enabled=True),
            }
        ),
        request_context={
            "knowledge_sources": [
                {"path": source_paths[0], "filename": "alpha.txt"}
            ],
            "knowledge_scope_mode": "exact",
        },
    )

    _, search_executor = registry.get_tool("wiki_search")
    _, read_executor = registry.get_tool("wiki_read")
    await search_executor("alpha", 4)
    await read_executor(source_paths[0])

    search.assert_awaited_once_with("alpha", source_paths, 4)
    read.assert_awaited_once_with(source_paths[0], source_paths)


def test_expanded_knowledge_scope_keeps_normal_wiki_executors() -> None:
    registry = build_lantai_tool_registry(
        agent_config=_agent_config(
            {
                "wiki_search": _ToolConfig(enabled=True),
                "wiki_read": _ToolConfig(enabled=True),
            }
        ),
        request_context={
            "knowledge_sources": [
                {"path": ".wiki/raw/sources/alpha.txt", "filename": "alpha.txt"}
            ],
            "knowledge_scope_mode": "expanded",
        },
    )

    assert registry.get_tool("wiki_search")[1] is tool_registry_module.wiki_search
    assert registry.get_tool("wiki_read")[1] is tool_registry_module.wiki_read


def test_registry_keeps_policy_middleware_and_mcp_metadata() -> None:
    _skip_developer_id_registry_when_app_store_runtime()

    class _Policy:
        async def evaluate(self, tool_call, spec, context):
            return ToolPolicyOutcome(action="allow")

    client = SimpleNamespace(name="fs", read_timeout_seconds=9.0)
    registry = LantaiToolRegistry()
    registry.register_policy_middleware(_Policy())
    registry.extend(
        build_mcp_client_registry(
            [client],
            request_context=_developer_id_context(),
        ),
    )

    assert len(registry.policy_middlewares()) == 1
    assert registry.mcp_client_specs()[0].name == "fs"
    assert registry.mcp_client_specs()[0].execution_timeout == 9.0
    assert (
        registry.mcp_client_specs()[0].metadata["capability_matrix_id"]
        == "mcp_plugin_network"
    )


def test_reserved_guanlan_client_carries_unspoofable_official_provenance() -> (
    None
):
    official = SimpleNamespace(
        name="观澜 / Guanlan",
        read_timeout_seconds=300.0,
        _lantai_client_key="guanlan",
    )
    namesake = SimpleNamespace(
        name="guanlan",
        read_timeout_seconds=300.0,
    )

    official_spec = mcp_client_spec_from_client(official)
    namesake_spec = mcp_client_spec_from_client(namesake)

    assert official_spec.metadata["official_integration"] == "guanlan"
    assert official_spec.metadata["integration_policy_version"] == (
        "guanlan-native-v1"
    )
    assert "official_integration" not in namesake_spec.metadata


def test_reserved_chrome_client_carries_unspoofable_official_provenance() -> None:
    official = SimpleNamespace(
        name="Chrome DevTools",
        read_timeout_seconds=300.0,
        _lantai_client_key="chrome-devtools",
    )
    namesake = SimpleNamespace(
        name="Chrome DevTools",
        read_timeout_seconds=300.0,
    )
    wrong_key = SimpleNamespace(
        name="Chrome DevTools",
        read_timeout_seconds=300.0,
        _lantai_client_key="custom-chrome",
    )

    official_spec = mcp_client_spec_from_client(official)
    namesake_spec = mcp_client_spec_from_client(namesake)
    wrong_key_spec = mcp_client_spec_from_client(wrong_key)

    assert official_spec.metadata["official_integration"] == "chrome-devtools"
    assert official_spec.metadata["integration_policy_version"] == (
        "lantai-chrome-devtools/1"
    )
    assert "official_integration" not in namesake_spec.metadata
    assert "official_integration" not in wrong_key_spec.metadata


def test_build_tool_registry_preserves_builtin_plugin_and_async_rules() -> (
    None
):
    _skip_developer_id_registry_when_app_store_runtime()

    registry = build_lantai_tool_registry(
        agent_config=_agent_config(
            {
                "execute_shell_command": _ToolConfig(
                    enabled=True,
                    async_execution=True,
                ),
                "read_file": _ToolConfig(enabled=False),
                # Retired config entries are ignored instead of resurrecting
                # removed plugin implementations.
                "generate_image_with_provider": _ToolConfig(enabled=True),
            },
        ),
        request_context=_developer_id_context(),
    )

    tools = {spec.name: spec for spec, _executor in registry.tool_items()}

    assert tools["execute_shell_command"].origin == "builtin"
    assert tools["execute_shell_command"].async_execution is True
    assert tools["execute_shell_command"].input_schema["type"] == "object"
    assert (
        "command" in tools["execute_shell_command"].input_schema["properties"]
    )
    assert "read_file" not in tools
    assert tools["write_file"].origin == "builtin"
    assert tools["browser_task"].origin == "builtin"
    assert tools["prepare_scheduled_task"].origin == "builtin"
    assert "generate_image_with_provider" not in tools
    assert "append_file" not in tools
    assert registry.policy_middlewares()


def test_builtin_file_tools_declare_versioned_artifact_semantics() -> None:
    registry = build_lantai_tool_registry(
        agent_config=_agent_config(),
        request_context=_developer_id_context(),
    )

    write_spec = registry.get_tool("write_file")[0]
    edit_spec = registry.get_tool("edit_file")[0]
    delivery_spec = registry.get_tool("send_file_to_user")[0]
    presentation_spec = registry.get_tool("create_presentation")[0]
    presentation_status_spec = registry.get_tool(
        "get_presentation_generation_status"
    )[0]
    presentation_retry_spec = registry.get_tool(
        "retry_presentation_generation"
    )[0]
    presentation_finalize_spec = registry.get_tool(
        "finalize_presentation_generation"
    )[0]

    assert write_spec.output_semantics.schema_version == 1
    assert write_spec.output_semantics.artifact_role == "candidate"
    assert write_spec.output_semantics.argument_path_fields == ("file_path",)
    assert write_spec.output_semantics.source_adapter == "legacy_tool_adapter"
    assert edit_spec.output_semantics == write_spec.output_semantics
    assert delivery_spec.output_semantics.artifact_role == "delivery"
    assert delivery_spec.output_semantics.argument_path_fields == (
        "file_path",
    )
    assert (
        delivery_spec.output_semantics.source_adapter == "legacy_tool_adapter"
    )
    assert set(delivery_spec.input_schema["properties"]) >= {
        "file_path",
        "title",
    }
    assert "file_path" in delivery_spec.input_schema["required"]
    assert "title" not in delivery_spec.input_schema["required"]
    assert presentation_spec.destructive is True
    assert presentation_spec.metadata["runtime_owned"] is True
    assert presentation_spec.metadata["trusted_effect_settlement"] is True
    assert presentation_spec.output_semantics.artifact_role == "delivery"
    assert presentation_spec.output_semantics.argument_path_fields == (
        "output_filename",
    )
    assert presentation_spec.output_semantics.source_adapter == "declared"
    assert presentation_spec.output_semantics.content_media_types == ("file",)
    assert (
        registry.get_tool("create_analysis_chart")[0]
        .output_semantics.content_media_types
        == ("image",)
    )
    assert presentation_status_spec.destructive is False
    assert presentation_status_spec.output_semantics.artifact_role == "none"
    assert presentation_status_spec.input_schema["required"] == ["job_id"]
    assert presentation_retry_spec.destructive is True
    assert presentation_retry_spec.output_semantics.artifact_role == "none"
    assert presentation_retry_spec.input_schema["required"] == ["job_id"]
    assert presentation_retry_spec.effect_recovery.idempotency == "natural_key"
    assert presentation_retry_spec.effect_recovery.probe == "tool_specific"
    assert presentation_finalize_spec.destructive is True
    assert presentation_finalize_spec.output_semantics.artifact_role == "delivery"
    assert presentation_finalize_spec.output_semantics.argument_path_fields == (
        "output_filename",
    )
    slide_schema = presentation_spec.input_schema["properties"]["slides"]
    assert slide_schema["minItems"] == 1
    assert slide_schema["maxItems"] == 60
    layout_schemas = slide_schema["items"]["oneOf"]
    assert layout_schemas
    assert all(schema["additionalProperties"] is False for schema in layout_schemas)
    freeform_schema = next(
        schema
        for schema in layout_schemas
        if schema["properties"]["layout_id"]["const"] == "freeform"
    )
    object_variants = freeform_schema["properties"]["objects"]["items"]["oneOf"]
    assert len(object_variants) == 6
    assert any(
        variant["properties"].get("kind", {}).get("const") == "shape"
        for variant in object_variants
    )
    chart_variant = next(
        variant
        for variant in object_variants
        if variant["properties"].get("kind", {}).get("const") == "chart"
    )
    assert {
        "unit",
        "number_format",
        "derivation",
        "source_refs",
    } <= chart_variant["properties"].keys()
    assert {"unit", "derivation", "source_refs"} <= set(
        chart_variant["required"]
    )
    assert presentation_spec.input_schema["examples"] == [
        {
            "title": "季度复盘",
            "slides": [
                {
                    "layout_id": "cover",
                    "composition_authority": "design_system",
                    "objects": [
                        {
                            "kind": "text",
                            "role": "title",
                            "text": "季度复盘",
                            "slot": "title",
                        },
                    ],
                },
            ],
        },
    ]


def test_chat_analysis_tools_are_discoverable_and_bound_to_delivery_semantics() -> None:
    registry = build_lantai_tool_registry(
        agent_config=_agent_config(), request_context=_developer_id_context(),
    )
    names = {spec.name for spec, _executor in registry.tool_items()}
    assert {"import_data_table", "analyze_data_table", "get_analysis_result", "refresh_data_table", "refresh_analysis_result", "create_analysis_chart", "create_analysis_report", "create_analysis_presentation", "clear_analysis_data"} <= names
    assert registry.get_tool("get_analysis_result")[0].destructive is False
    assert "analysis_id" not in registry.get_tool("get_analysis_result")[0].input_schema.get("required", [])
    assert registry.get_tool("analyze_data_table")[0].input_schema["properties"]["plan"]["additionalProperties"] is False
    assert "pivot" in registry.get_tool("analyze_data_table")[0].input_schema["properties"]["plan"]["properties"]
    assert registry.get_tool("clear_analysis_data")[0].destructive is True
    presentation_schema = registry.get_tool("create_analysis_presentation")[0].input_schema
    assert "bindings" in presentation_schema["properties"]
    assert set(presentation_schema["required"]) == {"analysis_id", "title"}
    assert "standard" in presentation_schema["properties"]["slides"]["description"].lower()
    report_schema = registry.get_tool("create_analysis_report")[0].input_schema
    assert report_schema["required"] == ["analysis_id"]
    assert "standard" in report_schema["properties"]["sections"]["description"].lower()
    assert "omit" in report_schema["properties"]["sections"]["description"].lower()
    for name in ("create_analysis_chart", "create_analysis_report", "create_analysis_presentation"):
        spec = registry.get_tool(name)[0]
        assert spec.destructive is True
        assert spec.output_semantics.artifact_role == "delivery"


def test_generic_presentation_tool_routes_sealed_analysis_to_dedicated_tools() -> None:
    registry = build_lantai_tool_registry(
        agent_config=_agent_config(), request_context=_developer_id_context(),
    )

    description = registry.get_tool("create_presentation")[0].description

    assert "create_analysis_presentation" in description
    assert "get_presentation_generation_status" in description


def test_build_tool_registry_exposes_browser_find_as_read_only_tool() -> None:
    registry = build_lantai_tool_registry(
        agent_config=_agent_config(),
        request_context=_developer_id_context(),
    )

    registered = registry.get_tool("browser_find")
    assert registered is not None
    spec, executor = registered
    assert callable(executor)
    assert spec.origin == "builtin"
    assert spec.read_only is True
    assert spec.concurrency_safe is True
    assert spec.destructive is False
    assert set(spec.input_schema["properties"]) == {
        "text",
        "regex",
        "page_id",
        "profile_id",
        "max_results",
        "context_chars",
    }


def test_build_tool_registry_separates_mac_observation_from_side_effects() -> None:
    registry = build_lantai_tool_registry(
        agent_config=_agent_config(),
        request_context=_developer_id_context(),
    )

    inspect_spec, _ = registry.get_tool("mac_inspect") or (None, None)
    act_spec, _ = registry.get_tool("mac_act") or (None, None)
    session_spec, _ = registry.get_tool("mac_session") or (None, None)

    assert inspect_spec is not None
    assert inspect_spec.read_only is True
    assert inspect_spec.concurrency_safe is True
    assert act_spec is not None
    assert act_spec.read_only is False
    assert act_spec.destructive is True
    assert act_spec.effect_recovery.external_side_effect is True
    assert act_spec.effect_recovery.idempotency == "not_idempotent"
    assert act_spec.effect_recovery.on_ambiguity == "probe_required"
    assert session_spec is not None
    assert session_spec.effect_recovery.idempotency == "natural_key"


def test_builtin_analysis_chart_declares_trusted_assistant_projection() -> None:
    registry = build_lantai_tool_registry(
        agent_config=_agent_config(),
        request_context=_developer_id_context(),
    )

    spec, _executor = registry.get_tool("create_analysis_chart") or (None, None)

    assert spec is not None
    assert spec.origin == "builtin"
    assert spec.metadata == {
        "assistant_artifact_projection": "analysis_chart_reference",
    }


def test_build_tool_registry_registers_progressive_collaboration_batch() -> (
    None
):
    registry = build_lantai_tool_registry(
        agent_config=_agent_config(),
        request_context=_developer_id_context(),
    )

    registered = registry.get_tool("collaborate_with_agents")

    assert registered is not None
    spec, _executor = registered
    assert spec.origin == "builtin"
    assert spec.metadata["progressive_tool"] == "collaboration"
    assert spec.metadata["progressive_timeout_seconds"] > 600
    assert spec.input_schema["required"] == ["tasks"]
    assert spec.input_schema["properties"]["tasks"]["type"] == "array"
    task_items = spec.input_schema["properties"]["tasks"]["items"]
    assert spec.input_schema["properties"]["tasks"]["minItems"] == 1
    assert spec.input_schema["properties"]["tasks"]["maxItems"] == 4
    assert spec.input_schema["allOf"][-1] == {
        "if": {
            "properties": {"mode": {"const": "execution"}},
            "required": ["mode"],
        },
        "then": {"properties": {"tasks": {"minItems": 1}}},
        "else": {"properties": {"tasks": {"minItems": 2}}},
    }
    assert task_items["required"] == ["to_agent", "text", "label"]
    assert set(task_items["properties"]) == {
        "to_agent",
        "text",
        "label",
        "depends_on",
        "minimum_output_chars",
        "required_output_markers",
        "allowed_tools",
        "resource_claims",
        "expected_artifacts",
    }
    assert task_items["properties"]["allowed_tools"]["minItems"] == 1
    artifact = task_items["properties"]["expected_artifacts"]["items"]
    assert artifact["required"] == ["path"]
    assert artifact["additionalProperties"] is False
    assert spec.input_schema["properties"]["mode"]["enum"] == [
        "consultation",
        "execution",
    ]
    assert spec.input_schema["properties"]["team_id"]["maxLength"] == 160
    assert spec.input_schema["properties"]["max_member_attempts"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 8,
        "description": (
            "Root attempt budget shared by every member and retry."
        ),
    }
    assert spec.input_schema["properties"]["max_team_wall_seconds"][
        "maximum"
    ] == 14400
    assert spec.input_schema["properties"]["max_artifact_bytes"][
        "maximum"
    ] == 2000000000


@pytest.mark.parametrize("enabled", [True, False])
def test_unified_web_tools_replace_standalone_quick_search(monkeypatch, enabled):
    from lantai.agents.tools import web
    monkeypatch.setattr(web, "get_web_search_store", lambda: SimpleNamespace(
        status=lambda: SimpleNamespace(enabled=enabled),
    ))
    registry = build_lantai_tool_registry(
        agent_config=_agent_config(), request_context=_developer_id_context(),
    )
    assert registry.get_tool("deepseek_quick_search") is None
    for name in ("web_search", "web_fetch"):
        spec, _executor = registry.get_tool(name)
        assert spec.read_only is True
        assert spec.concurrency_safe is True
        assert spec.origin == "builtin"


def test_build_tool_registry_registers_logical_skill_resource_tools() -> None:
    registry = build_lantai_tool_registry(
        agent_config=_agent_config(),
        request_context=_developer_id_context(),
    )

    list_spec, _list_executor = registry.get_tool("list_skill_resources")
    copy_spec, _copy_executor = registry.get_tool("copy_skill_resource")
    run_spec, _run_executor = registry.get_tool("run_skill_script")

    assert list_spec.read_only is True
    assert list_spec.metadata["progress_policy"] == "first_tool_success"
    assert list_spec.input_schema["required"] == ["skill_name"]
    assert copy_spec.destructive is True
    assert copy_spec.input_schema["required"] == [
        "skill_name",
        "resource_path",
        "destination_path",
    ]
    assert run_spec.destructive is True
    assert run_spec.input_schema["required"] == [
        "skill_name",
        "script_path",
        "arguments",
    ]
    assert run_spec.input_schema["properties"]["arguments"] == {
        "type": "array",
        "items": {"type": "string"},
    }


def test_developer_id_registry_can_register_python_when_explicitly_enabled() -> (  # noqa: E501
    None
):
    _skip_developer_id_registry_when_app_store_runtime()

    registry = build_lantai_tool_registry(
        agent_config=_agent_config(
            {
                "execute_python_code": _ToolConfig(enabled=True),
            },
        ),
        request_context=_developer_id_context(),
    )

    spec, _executor = registry.get_tool("execute_python_code")

    assert spec.name == "execute_python_code"
    assert spec.origin == "plugin"


def test_app_store_registry_removes_shell_and_python_even_when_enabled() -> (
    None
):
    registry = build_lantai_tool_registry(
        agent_config=_agent_config(
            {
                "execute_shell_command": _ToolConfig(enabled=True),
                "execute_python_code": _ToolConfig(enabled=True),
            },
        ),
        request_context={"distribution": "app_store"},
    )

    assert registry.get_tool("execute_shell_command") is None
    assert registry.get_tool("execute_python_code") is None


def test_app_store_registry_uses_capability_matrix_for_plugin_tools() -> None:
    entry = tool_registration_matrix_entry(
        "plugin_network_action",
        {"distribution": "app_store"},
        origin="plugin",
    )

    assert entry is not None
    assert entry.id == "mcp_plugin_network"
    assert entry.reason_code == "mas_mcp_plugin_network_limited"
    assert not is_tool_registration_allowed(
        "plugin_network_action",
        {"distribution": "app_store"},
        origin="plugin",
    )

    registry = build_lantai_tool_registry(
        agent_config=_agent_config(
            {
                "plugin_network_action": _ToolConfig(enabled=True),
            },
        ),
        request_context={"distribution": "app_store"},
    )

    assert registry.get_tool("plugin_network_action") is None


def test_app_store_mcp_client_registry_skips_automatic_clients() -> None:
    client = SimpleNamespace(name="external-search", read_timeout_seconds=3.0)

    registry = build_mcp_client_registry(
        [client],
        request_context={"distribution": "app_store"},
    )

    assert registry.mcp_client_specs() == []


def test_app_store_ambient_registry_cannot_be_downgraded_by_request_context(
    monkeypatch,
) -> None:
    monkeypatch.setenv("LANTAI_APP_STORE", "1")

    registry = build_lantai_tool_registry(
        agent_config=_agent_config(
            {
                "execute_shell_command": _ToolConfig(enabled=True),
                "execute_python_code": _ToolConfig(enabled=True),
            },
        ),
        request_context=_developer_id_context(),
    )

    assert registry.get_tool("execute_shell_command") is None
    assert registry.get_tool("execute_python_code") is None


def test_frozen_registry_removes_python_tool(monkeypatch) -> None:
    _skip_developer_id_registry_when_app_store_runtime()

    monkeypatch.setattr(compat_builtin.sys, "frozen", True, raising=False)

    registry = build_lantai_tool_registry(
        agent_config=_agent_config(
            {
                "execute_shell_command": _ToolConfig(enabled=True),
                "execute_python_code": _ToolConfig(enabled=True),
            },
        ),
        request_context=_developer_id_context(),
    )

    assert registry.get_tool("execute_shell_command") is not None
    assert registry.get_tool("execute_python_code") is None


def test_build_tool_registry_adds_coding_tools_only_when_enabled() -> None:
    _skip_developer_id_registry_when_app_store_runtime()

    async def coding_ast_search() -> None:
        return None

    normal_registry = build_lantai_tool_registry(
        agent_config=_agent_config(),
        request_context=_developer_id_context(),
        is_coding_mode_enabled_fn=lambda _ctx: False,
        coding_tool_functions_factory=lambda _ctx: {
            "coding_ast_search": coding_ast_search,
        },
    )
    coding_registry = build_lantai_tool_registry(
        agent_config=_agent_config(),
        request_context={
            "coding_mode": "true",
            "distribution": "developer_id",
        },
        is_coding_mode_enabled_fn=lambda _ctx: True,
        coding_tool_functions_factory=lambda _ctx: {
            "coding_ast_search": coding_ast_search,
        },
    )

    assert normal_registry.get_tool("coding_ast_search") is None
    coding_tool = coding_registry.get_tool("coding_ast_search")
    assert coding_tool is not None
    assert coding_tool[0].origin == "coding"


def test_coding_presentation_cannot_inherit_builtin_effect_trust() -> None:
    _skip_developer_id_registry_when_app_store_runtime()

    async def coding_presentation() -> None:
        return None

    registry = build_lantai_tool_registry(
        agent_config=_agent_config(),
        request_context=_developer_id_context(),
        is_coding_mode_enabled_fn=lambda _ctx: True,
        coding_tool_functions_factory=lambda _ctx: {
            "create_presentation": coding_presentation,
        },
    )

    spec = registry.get_tool("create_presentation")[0]
    assert spec.origin == "coding"
    assert not spec.metadata.get("runtime_owned")
    assert not spec.metadata.get("trusted_effect_settlement")
    shell_spec = registry.get_tool("execute_shell_command")[0]
    assert not shell_spec.metadata.get("trusted_effect_settlement")


def test_tool_policy_context_preserves_request_and_agent_state() -> None:
    context = build_tool_policy_context(
        agent_config=SimpleNamespace(approval_level="STRICT"),
        request_context={
            "agent_id": "assistant",
            "session_id": "session-1",
            "root_session_id": "root-1",
            "permission_profile": "observe",
            "permission_mode": "plan",
            "distribution": "app_store",
            "workspace_grant_id": "wsg-context",
            "workspace_access_mode": "read_only",
            "workspace_trust_state": "trusted",
        },
        workspace_dir="/workspace",
    )

    assert context.agent_id == "assistant"
    assert context.session_id == "session-1"
    assert context.root_session_id == "root-1"
    assert context.workspace_dir == "/workspace"
    assert context.workspace_grant_id == "wsg-context"
    assert context.workspace_access_mode == "read_only"
    assert context.workspace_trust_state == "trusted"
    assert context.permission_profile == "observe"
    assert context.permission_mode == "plan"
    assert context.distribution == "app_store"
    assert context.approval_level == "STRICT"


@pytest.mark.asyncio
async def test_runtime_tool_policy_middleware_denies_forbidden_shell() -> None:
    _skip_developer_id_registry_when_app_store_runtime()

    registry = build_lantai_tool_registry(
        agent_config=_agent_config(
            {
                "execute_shell_command": _ToolConfig(enabled=True),
            },
        ),
        request_context=_developer_id_context(),
    )
    spec, _executor = registry.get_tool("execute_shell_command")
    middleware = registry.policy_middlewares()[0]

    outcome = await middleware.evaluate(
        ToolCall(
            id="call-1",
            name="execute_shell_command",
            arguments={"command": "git reset --hard"},
        ),
        spec,
        ToolPolicyContext(workspace_dir="/workspace"),
    )

    assert outcome.action == "deny"
    assert outcome.metadata["source"] == "runtime_policy"
    assert outcome.policy_payload["decision"] == "forbidden"


@pytest.mark.asyncio
async def test_collab_read_only_denies_mutation_and_allows_reads() -> None:
    registry = build_lantai_tool_registry(
        agent_config=_agent_config(
            {
                "read_file": _ToolConfig(enabled=True),
                "write_file": _ToolConfig(enabled=True),
                "browser_use": _ToolConfig(enabled=True),
            },
        ),
        request_context={
            "distribution": "developer_id",
            "collaboration_read_only": True,
        },
    )
    context = build_tool_policy_context(
        request_context={
            "distribution": "developer_id",
            "collaboration_read_only": True,
        },
        workspace_dir="/workspace",
    )

    middleware = registry.policy_middlewares()[0]
    read_spec, _ = registry.get_tool("read_file")
    write_spec, _ = registry.get_tool("write_file")
    browser_spec, _ = registry.get_tool("browser_use")

    read_outcome = await middleware.evaluate(
        ToolCall(
            id="call-read",
            name="read_file",
            arguments={"file_path": "/workspace/source.md"},
        ),
        read_spec,
        context,
    )
    write_outcome = await middleware.evaluate(
        ToolCall(
            id="call-write",
            name="write_file",
            arguments={"file_path": "/workspace/out.md", "content": "x"},
        ),
        write_spec,
        context,
    )
    browser_outcome = await middleware.evaluate(
        ToolCall(
            id="call-browser",
            name="browser_use",
            arguments={"operation": "click"},
        ),
        browser_spec,
        context,
    )

    assert read_outcome.action == "allow"
    assert write_outcome.action == "deny"
    assert browser_outcome.action == "deny"
    assert write_outcome.metadata["source"] == "collaboration_read_only"


@pytest.mark.asyncio
async def test_runtime_phase_policy_denies_context_blocked_tool() -> None:
    registry = build_lantai_tool_registry(
        agent_config=_agent_config({}),
        request_context={"distribution": "developer_id"},
    )
    middleware = registry.policy_middlewares()[0]
    outcome = await middleware.evaluate(
        ToolCall(id="call-edit", name="edit_file"),
        ToolSpec(name="edit_file"),
        ToolPolicyContext(
            request_context={
                "runtime_blocked_tool_names": ["edit_file"],
            },
        ),
    )

    assert outcome.action == "deny"
    assert outcome.metadata["source"] == "runtime_phase_tool_block"


def test_tool_specs_use_conservative_typed_risk_defaults() -> None:
    spec = ToolSpec(name="mystery_tool")

    assert spec.read_only is False
    assert spec.concurrency_safe is False
    assert spec.destructive is False
    assert spec.max_result_size is None


def test_lantai_web_baseline_exists_without_any_mcp_client() -> None:
    registry = build_lantai_tool_registry(
        agent_config=_agent_config({}),
        request_context=_developer_id_context(),
    )

    search_spec, search_executor = registry.get_tool("web_search")
    fetch_spec, fetch_executor = registry.get_tool("web_fetch")

    assert search_spec.origin == "builtin"
    assert search_spec.read_only is True
    assert search_spec.concurrency_safe is True
    assert fetch_spec.origin == "builtin"
    assert fetch_spec.read_only is True
    assert fetch_spec.concurrency_safe is True
    assert callable(search_executor)
    assert callable(fetch_executor)


def test_lantai_web_baseline_remains_subject_to_network_capability_policy() -> (  # noqa: E501
    None
):
    search_entry = tool_registration_matrix_entry(
        "web_search",
        origin="builtin",
    )
    fetch_entry = tool_registration_matrix_entry(
        "web_fetch",
        origin="builtin",
    )

    assert search_entry is not None
    assert search_entry.id == "mcp_plugin_network"
    assert fetch_entry is not None
    assert fetch_entry.id == "mcp_plugin_network"


def test_builtin_registry_declares_tool_risk_instead_of_guessing_from_name() -> (  # noqa: E501
    None
):
    registry = build_lantai_tool_registry(
        agent_config=_agent_config(
            {
                "read_file": _ToolConfig(enabled=True),
                "write_file": _ToolConfig(enabled=True),
            },
        ),
        request_context={"distribution": "developer_id"},
    )

    read_spec, _ = registry.get_tool("read_file")
    write_spec, _ = registry.get_tool("write_file")

    assert read_spec.read_only is True
    assert read_spec.concurrency_safe is True
    assert read_spec.destructive is False
    assert write_spec.read_only is False
    assert write_spec.concurrency_safe is False
    assert write_spec.destructive is True


def test_extension_management_declares_read_and_write_boundaries() -> None:
    registry = build_lantai_tool_registry(
        agent_config=_agent_config(
            {
                "inspect_mcp_connectors": _ToolConfig(enabled=True),
                "install_agent_skill": _ToolConfig(enabled=True),
                "manage_mcp_connector": _ToolConfig(enabled=True),
            },
        ),
        request_context={"distribution": "developer_id"},
    )

    inspect_spec, _ = registry.get_tool("inspect_mcp_connectors")
    install_spec, _ = registry.get_tool("install_agent_skill")
    manage_spec, _ = registry.get_tool("manage_mcp_connector")

    assert inspect_spec.read_only is True
    assert inspect_spec.concurrency_safe is True
    assert inspect_spec.destructive is False
    assert install_spec.read_only is False
    assert install_spec.destructive is True
    assert manage_spec.read_only is False
    assert manage_spec.destructive is True


@pytest.mark.asyncio
async def test_collaboration_policy_trusts_declared_risk_not_tool_name(
    monkeypatch,
) -> None:
    from lantai.runtime.tool_policy import RuntimeToolPolicyMiddleware

    class _AllowDecision:
        decision = "allow"
        reason = ""
        risk = "low"

        def model_dump(self, **_kwargs):
            return {"decision": "allow", "reason": "", "risk": "low"}

    middleware = RuntimeToolPolicyMiddleware()
    monkeypatch.setattr(
        middleware,
        "_evaluate_builtin_policy",
        lambda *_args, **_kwargs: _AllowDecision(),
    )
    context = ToolPolicyContext(
        request_context={"collaboration_read_only": True},
    )

    declared_safe = ToolSpec(
        name="calculate_summary",
        read_only=True,
        concurrency_safe=True,
    )
    misleading_name = ToolSpec(name="read_and_delete", read_only=False)

    safe_outcome = await middleware.evaluate(
        ToolCall(id="safe", name=declared_safe.name),
        declared_safe,
        context,
    )
    unsafe_outcome = await middleware.evaluate(
        ToolCall(id="unsafe", name=misleading_name.name),
        misleading_name,
        context,
    )

    assert safe_outcome.action == "allow"
    assert unsafe_outcome.action == "deny"


@pytest.mark.asyncio
async def test_official_guanlan_search_is_allowlisted_but_namesake_is_not() -> None:  # noqa: E501
    from lantai.runtime.tool_policy import RuntimeToolPolicyMiddleware

    middleware = RuntimeToolPolicyMiddleware()
    context = ToolPolicyContext(
        session_id="session-1",
        workspace_dir="/workspace",
        permission_profile="workspace",
        permission_mode="quiet",
        distribution="developer_id",
    )
    call = ToolCall(
        id="search-1",
        name="guanlan_search",
        arguments={"query": "最近的 AI 政策", "limit": 80},
    )

    official_outcome = await middleware.evaluate(
        call,
        ToolSpec(
            name="guanlan_search",
            origin="mcp",
            metadata={"official_integration": "guanlan"},
        ),
        context,
    )
    namesake_outcome = await middleware.evaluate(
        call,
        ToolSpec(name="guanlan_search", origin="mcp"),
        context,
    )

    assert official_outcome.action == "allow"
    assert official_outcome.metadata["source"] == (
        "official_guanlan_allowlist"
    )
    assert namesake_outcome.action == "needs_approval"


@pytest.mark.asyncio
async def test_runtime_owned_artifact_contract_tools_do_not_leak_unknown_approval(  # noqa: E501
    monkeypatch,
) -> None:
    from lantai.runtime.tool_policy import RuntimeToolPolicyMiddleware

    middleware = RuntimeToolPolicyMiddleware()
    monkeypatch.setattr(
        middleware,
        "_evaluate_tool_guard",
        lambda _name, _input, _context: None,
    )
    context = ToolPolicyContext(
        session_id="session-1",
        workspace_dir="/workspace",
        permission_profile="workspace",
        permission_mode="quiet",
        distribution="developer_id",
    )

    for name in ("declare_deliverable_contract", "finalize_deliverable"):
        outcome = await middleware.evaluate(
            ToolCall(id=f"call-{name}", name=name),
            ToolSpec(
                name=name,
                origin="builtin",
                metadata={"runtime_owned": True},
            ),
            context,
        )
        assert outcome.action == "allow"
        assert outcome.metadata["source"] == "runtime_owned_internal"

        namesake = await middleware.evaluate(
            ToolCall(id=f"call-namesake-{name}", name=name),
            ToolSpec(name=name, origin="plugin"),
            context,
        )
        assert namesake.action == "needs_approval"


@pytest.mark.asyncio
async def test_effect_record_auto_delivery_skips_only_the_redundant_send_prompt(  # noqa: E501
    monkeypatch,
) -> None:
    from lantai.runtime.tool_policy import RuntimeToolPolicyMiddleware

    middleware = RuntimeToolPolicyMiddleware()
    monkeypatch.setattr(
        middleware,
        "_evaluate_tool_guard",
        lambda _name, _input, _context: None,
    )
    context = ToolPolicyContext(
        session_id="session-1",
        workspace_dir="/workspace",
        permission_profile="workspace",
        permission_mode="quiet",
        distribution="developer_id",
    )
    spec = ToolSpec(name="send_file_to_user", origin="builtin")

    committed = await middleware.evaluate(
        ToolCall(
            id="call-effect-delivery",
            name=spec.name,
            arguments={"file_path": "/workspace/report.txt"},
            metadata={
                "auto_delivery": True,
                "idempotency_key": "artifact-delivery:artifact:sha256:abc123",
            },
        ),
        spec,
        context,
    )
    ordinary = await middleware.evaluate(
        ToolCall(
            id="call-ordinary-send",
            name=spec.name,
            arguments={"file_path": "/workspace/report.txt"},
        ),
        spec,
        context,
    )
    legacy_auto = await middleware.evaluate(
        ToolCall(
            id="call-legacy-auto-send",
            name=spec.name,
            arguments={"file_path": "/workspace/report.txt"},
            metadata={
                "auto_delivery": True,
                "idempotency_key": (
                    "legacy-file-delivery:/workspace/report.txt"
                ),
            },
        ),
        spec,
        context,
    )
    workspace_escape = await middleware.evaluate(
        ToolCall(
            id="call-effect-delivery-outside-workspace",
            name=spec.name,
            arguments={"file_path": "/outside/report.txt"},
            metadata={
                "auto_delivery": True,
                "idempotency_key": "artifact-delivery:artifact:sha256:abc123",
            },
        ),
        spec,
        context,
    )
    namesake = await middleware.evaluate(
        ToolCall(
            id="call-effect-delivery-namesake",
            name=spec.name,
            arguments={"file_path": "/workspace/report.txt"},
            metadata={
                "auto_delivery": True,
                "idempotency_key": "artifact-delivery:artifact:sha256:abc123",
            },
        ),
        ToolSpec(name=spec.name, origin="plugin"),
        context,
    )

    assert committed.action == "allow"
    assert committed.metadata["source"] == "artifact_effect_auto_delivery"
    assert ordinary.action == "needs_approval"
    assert legacy_auto.action == "needs_approval"
    assert workspace_escape.action == "needs_approval"
    assert namesake.action == "needs_approval"


@pytest.mark.asyncio
async def test_runtime_tool_policy_middleware_requests_approval_for_prompt() -> None:  # noqa: E501
    _skip_developer_id_registry_when_app_store_runtime()

    registry = build_lantai_tool_registry(
        agent_config=_agent_config(
            {
                "execute_shell_command": _ToolConfig(enabled=True),
            },
        ),
        request_context=_developer_id_context(),
    )
    spec, _executor = registry.get_tool("execute_shell_command")
    middleware = registry.policy_middlewares()[0]

    outcome = await middleware.evaluate(
        ToolCall(
            id="call-2",
            name="execute_shell_command",
            arguments={"command": "npm install"},
        ),
        spec,
        ToolPolicyContext(session_id="session-1", workspace_dir="/workspace"),
    )

    assert outcome.action == "needs_approval"
    assert outcome.requires_approval is True
    assert outcome.approval_copy


@pytest.mark.asyncio
async def test_runtime_policy_honors_request_permission_mode() -> None:
    _skip_developer_id_registry_when_app_store_runtime()

    registry = build_lantai_tool_registry(
        agent_config=_agent_config(
            {
                "execute_shell_command": _ToolConfig(enabled=True),
            },
        ),
        request_context=_developer_id_context(),
    )
    spec, _executor = registry.get_tool("execute_shell_command")
    middleware = registry.policy_middlewares()[0]

    outcome = await middleware.evaluate(
        ToolCall(
            id="call-full-auto",
            name="execute_shell_command",
            arguments={"command": "npm install lodash"},
        ),
        spec,
        ToolPolicyContext(
            session_id="session-1",
            workspace_dir="/workspace",
            permission_profile="full",
            permission_mode="full_auto",
            distribution="developer_id",
        ),
    )

    assert outcome.action == "allow"
    assert outcome.policy_payload["permission_mode"] == "full_auto"


@pytest.mark.asyncio
async def test_default_runtime_policy_receives_live_approval_service() -> None:
    from lantai.app.approvals.service import ApprovalService

    service = ApprovalService()
    registry = build_lantai_tool_registry(
        agent_config=_agent_config(
            {
                "execute_shell_command": _ToolConfig(enabled=True),
            },
        ),
        request_context=_developer_id_context(),
        approval_service=service,
    )

    middleware = registry.policy_middlewares()[0]
    assert middleware._approval_service is service


@pytest.mark.asyncio
async def test_runtime_policy_uses_permission_changed_after_turn_started(
    monkeypatch,
) -> None:
    from lantai.app.approvals.service import ApprovalService
    from lantai.runtime.tool_policy import RuntimeToolPolicyMiddleware

    service = ApprovalService()
    middleware = RuntimeToolPolicyMiddleware(service)
    captured_contexts: list[ToolPolicyContext] = []
    policy_payload = {
        "decision": "prompt",
        "reason": "Approval required.",
        "risk": "medium",
    }

    def capture_policy_context(
        _name: str,
        _input: dict[str, Any],
        context: ToolPolicyContext,
    ) -> SimpleNamespace:
        captured_contexts.append(context)
        return SimpleNamespace(
            **policy_payload,
            model_dump=lambda mode=None: dict(policy_payload),
        )

    monkeypatch.setattr(
        middleware,
        "_evaluate_builtin_policy",
        capture_policy_context,
    )
    monkeypatch.setattr(
        middleware,
        "_evaluate_tool_guard",
        lambda _name, _input, _context: None,
    )
    await service.set_session_permission_override(
        root_session_id="root-session",
        user_id="owner-user",
        permission_mode="full_auto",
        permission_profile="full",
    )

    outcome = await middleware.evaluate(
        ToolCall(
            id="call-live-full-access",
            name="execute_shell_command",
            arguments={"command": "npm test"},
        ),
        ToolSpec(name="execute_shell_command"),
        ToolPolicyContext(
            session_id="child-session",
            root_session_id="root-session",
            permission_mode="default",
            permission_profile="workspace",
            request_context={
                "user_id": "owner-user",
                "permission_snapshot_at": 0.0,
            },
        ),
    )

    assert outcome.action == "allow"
    assert captured_contexts[0].permission_mode == "full_auto"
    assert captured_contexts[0].permission_profile == "full"


@pytest.mark.asyncio
async def test_full_access_uses_turn_context_when_policy_omits_echo_fields(
    monkeypatch,
) -> None:
    from lantai.runtime.tool_policy import RuntimeToolPolicyMiddleware

    middleware = RuntimeToolPolicyMiddleware()
    policy_payload = {
        "decision": "prompt",
        "reason": "Unknown tool requires approval.",
        "risk": "medium",
        "approval_copy": "Approve?",
    }
    monkeypatch.setattr(
        middleware,
        "_evaluate_builtin_policy",
        lambda _name, _input, _context: SimpleNamespace(
            **policy_payload,
            model_dump=lambda mode=None: dict(policy_payload),
        ),
    )
    monkeypatch.setattr(
        middleware,
        "_evaluate_tool_guard",
        lambda _name, _input, _context: None,
    )

    outcome = await middleware.evaluate(
        ToolCall(
            id="call-full-context",
            name="guanlan_read",
            arguments={"url": "https://example.com"},
        ),
        ToolSpec(name="guanlan_read", origin="plugin"),
        ToolPolicyContext(
            session_id="session-1",
            workspace_dir="/workspace",
            permission_profile="full",
            permission_mode="full_auto",
            distribution="developer_id",
        ),
    )

    assert outcome.action == "allow"
    assert outcome.metadata["source"] == "runtime_policy"


@pytest.mark.asyncio
async def test_session_approval_suppresses_guard_prompt_but_not_guard_scan(
    monkeypatch,
) -> None:
    from lantai.runtime.tool_policy import RuntimeToolPolicyMiddleware

    middleware = RuntimeToolPolicyMiddleware()
    policy_payload = {
        "decision": "allow",
        "reason": "Built-in policy allows this tool.",
        "risk": "medium",
    }
    remembered = {
        "id": "session-access",
        "rule_kind": "session_access",
    }
    monkeypatch.setattr(
        middleware,
        "_evaluate_builtin_policy",
        lambda _name, _input, _context: SimpleNamespace(
            **policy_payload,
            model_dump=lambda mode=None: dict(policy_payload),
        ),
    )
    monkeypatch.setattr(
        middleware,
        "_remembered_policy_approval",
        AsyncMock(return_value=remembered),
    )
    guard = MagicMock(
        return_value=ToolPolicyOutcome(
            action="needs_approval",
            reason="ToolGuard would normally ask again.",
            risk="high",
            guard_payload={"findings": [{"rule_id": "review_me"}]},
            metadata={"source": "tool_guard"},
        ),
    )
    monkeypatch.setattr(middleware, "_evaluate_tool_guard", guard)

    outcome = await middleware.evaluate(
        ToolCall(
            id="call-session-access",
            name="execute_shell_command",
            arguments={"command": "npm install lodash"},
        ),
        ToolSpec(name="execute_shell_command", origin="builtin"),
        ToolPolicyContext(
            session_id="child-session",
            root_session_id="root-session",
            workspace_dir="/workspace",
        ),
    )

    assert guard.called
    assert outcome.action == "allow"
    assert outcome.metadata["source"] == "remembered_approval"
    assert outcome.remembered_approval == remembered


@pytest.mark.asyncio
async def test_session_approval_does_not_override_guard_hard_deny(
    monkeypatch,
) -> None:
    from lantai.runtime.tool_policy import RuntimeToolPolicyMiddleware

    middleware = RuntimeToolPolicyMiddleware()
    policy_payload = {
        "decision": "prompt",
        "reason": "Approval required.",
        "risk": "high",
    }
    monkeypatch.setattr(
        middleware,
        "_evaluate_builtin_policy",
        lambda _name, _input, _context: SimpleNamespace(
            **policy_payload,
            model_dump=lambda mode=None: dict(policy_payload),
        ),
    )
    monkeypatch.setattr(
        middleware,
        "_remembered_policy_approval",
        AsyncMock(
            return_value={
                "id": "session-access",
                "rule_kind": "session_access",
            },
        ),
    )
    monkeypatch.setattr(
        middleware,
        "_evaluate_tool_guard",
        lambda _name, _input, _context: ToolPolicyOutcome(
            action="deny",
            reason="Hard-denied by ToolGuard.",
            risk="critical",
            guard_payload={"hard_denied": True},
            metadata={"source": "tool_guard"},
        ),
    )

    outcome = await middleware.evaluate(
        ToolCall(
            id="call-session-hard-deny",
            name="execute_shell_command",
            arguments={"command": "git reset --hard"},
        ),
        ToolSpec(name="execute_shell_command", origin="builtin"),
        ToolPolicyContext(
            session_id="child-session",
            root_session_id="root-session",
            workspace_dir="/workspace",
        ),
    )

    assert outcome.action == "deny"
    assert outcome.metadata["source"] == "tool_guard"


@pytest.mark.asyncio
async def test_runtime_policy_denies_prompt_without_approval_route() -> None:
    _skip_developer_id_registry_when_app_store_runtime()

    registry = build_lantai_tool_registry(
        agent_config=_agent_config(
            {
                "execute_shell_command": _ToolConfig(enabled=True),
            },
        ),
        request_context=_developer_id_context(),
    )
    spec, _executor = registry.get_tool("execute_shell_command")
    middleware = registry.policy_middlewares()[0]

    outcome = await middleware.evaluate(
        ToolCall(
            id="call-no-approval",
            name="execute_shell_command",
            arguments={"command": "npm install lodash"},
        ),
        spec,
        ToolPolicyContext(workspace_dir="/workspace"),
    )

    assert outcome.action == "deny"
    assert outcome.reason
    assert outcome.metadata["source"] == "approval_unavailable"


@pytest.mark.asyncio
async def test_runtime_tool_policy_middleware_denies_builtin_policy_exception(
    monkeypatch,
) -> None:
    _skip_developer_id_registry_when_app_store_runtime()

    registry = build_lantai_tool_registry(
        agent_config=_agent_config(
            {
                "execute_shell_command": _ToolConfig(enabled=True),
            },
        ),
        request_context=_developer_id_context(),
    )
    spec, _executor = registry.get_tool("execute_shell_command")
    middleware = registry.policy_middlewares()[0]

    def _raise_policy(*_args, **_kwargs):
        raise RuntimeError("policy unavailable")

    monkeypatch.setattr(
        "lantai.security.tool_policy.evaluate_tool_policy",
        _raise_policy,
    )

    outcome = await middleware.evaluate(
        ToolCall(
            id="call-policy-error",
            name="execute_shell_command",
            arguments={"command": "rg RuntimeEvent src"},
        ),
        spec,
        ToolPolicyContext(workspace_dir="/workspace"),
    )

    assert outcome.action == "deny"
    assert outcome.reason
    assert outcome.metadata["source"] == "policy_engine_error"
    assert outcome.policy_payload["matched_rule_id"] == "policy_engine_error"


@pytest.mark.asyncio
async def test_read_only_workspace_denies_mutation_even_in_full_access() -> None:  # noqa: E501
    from lantai.runtime.tool_policy import RuntimeToolPolicyMiddleware

    middleware = RuntimeToolPolicyMiddleware()
    outcome = await middleware.evaluate(
        ToolCall(
            id="call-read-only-write",
            name="write_file",
            arguments={"path": "report.md", "content": "changed"},
        ),
        ToolSpec(name="write_file", read_only=False),
        ToolPolicyContext(
            agent_id="default",
            session_id="session-1",
            workspace_dir="/workspace",
            workspace_access_mode="read_only",
            permission_profile="full",
            permission_mode="full_auto",
        ),
    )

    assert outcome.action == "deny"
    assert outcome.metadata["source"] == "workspace_read_only"


@pytest.mark.asyncio
async def test_guardian_failure_denies_tool_instead_of_reporting_safe(
    monkeypatch,
) -> None:
    from lantai.runtime.tool_policy import RuntimeToolPolicyMiddleware

    result = ToolGuardResult(
        tool_name="execute_shell_command",
        params={"command": "pwd"},
        guardians_failed=[{"name": "path_guard", "error": "unavailable"}],
    )
    engine = SimpleNamespace(
        enabled=True,
        is_denied=lambda _name: False,
        is_guarded=lambda _name: True,
        guard=lambda *_args, **_kwargs: result,
        should_auto_deny_result=lambda _result: False,
    )
    monkeypatch.setattr(
        "lantai.security.tool_guard.engine.get_guard_engine",
        lambda: engine,
    )
    middleware = RuntimeToolPolicyMiddleware()
    monkeypatch.setattr(
        middleware,
        "_evaluate_builtin_policy",
        lambda _name, _input, _context: SimpleNamespace(
            decision="allow",
            reason="allowed",
            risk="low",
            model_dump=lambda mode=None: {
                "decision": "allow",
                "reason": "allowed",
                "risk": "low",
            },
        ),
    )

    outcome = await middleware.evaluate(
        ToolCall(
            id="call-guardian-failed",
            name="execute_shell_command",
            arguments={"command": "pwd"},
        ),
        ToolSpec(name="execute_shell_command"),
        ToolPolicyContext(
            agent_id="default",
            session_id="session-1",
            workspace_dir="/workspace",
        ),
    )

    assert result.is_safe is False
    assert outcome.action == "deny"
    assert outcome.metadata["source"] == "policy_engine_error"
    assert outcome.guard_payload["matched_rule_id"] == "policy_engine_error"


@pytest.mark.asyncio
async def test_runtime_tool_policy_middleware_denies_guard_import_failure(
    monkeypatch,
) -> None:
    _skip_developer_id_registry_when_app_store_runtime()

    registry = build_lantai_tool_registry(
        agent_config=_agent_config(
            {
                "execute_shell_command": _ToolConfig(enabled=True),
            },
        ),
        request_context=_developer_id_context(),
    )
    spec, _executor = registry.get_tool("execute_shell_command")
    middleware = registry.policy_middlewares()[0]
    real_import = builtins.__import__

    def _raise_guard_import(
        name,
        globals=None,
        locals=None,
        fromlist=(),
        level=0,
    ):
        if name == "lantai.security.tool_guard.engine":
            raise ImportError("guard unavailable")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _raise_guard_import)

    outcome = await middleware.evaluate(
        ToolCall(
            id="call-guard-error",
            name="execute_shell_command",
            arguments={"command": "rg RuntimeEvent src"},
        ),
        spec,
        ToolPolicyContext(workspace_dir="/workspace"),
    )

    assert outcome.action == "deny"
    assert outcome.reason
    assert outcome.metadata["source"] == "policy_engine_error"
    assert outcome.guard_payload["matched_rule_id"] == "policy_engine_error"


@pytest.mark.asyncio
async def test_legacy_toolkit_adapter_converts_lantai_tool_response() -> None:
    async def demo_tool(value: str) -> ToolResponse:
        return ToolResponse(content=[TextBlock(text=value)])

    registry = LantaiToolRegistry()
    registry.register_tool(ToolSpec(name="demo_tool"), demo_tool)
    toolkit = _FakeToolkit()

    LegacyToolkitAdapter(toolkit).register_tools(registry)

    wrapped, _kwargs = toolkit.registered[0]
    response = await wrapped(value="ok")

    assert isinstance(response, _FakeLegacyToolResponse)
    assert response.content == [{"type": "text", "text": "ok"}]


@pytest.mark.asyncio
async def test_legacy_toolkit_adapter_converts_lantai_tool_stream() -> None:
    async def demo_stream() -> ToolResponse:
        yield ToolResponse(content=[TextBlock(text="part")], stream=True)

    registry = LantaiToolRegistry()
    registry.register_tool(ToolSpec(name="demo_stream"), demo_stream)
    toolkit = _FakeToolkit()

    LegacyToolkitAdapter(toolkit).register_tools(registry)

    wrapped, _kwargs = toolkit.registered[0]
    responses = [item async for item in wrapped()]

    assert isinstance(responses[0], _FakeLegacyToolResponse)
    assert responses[0].content == [{"type": "text", "text": "part"}]
    assert responses[0].stream is True


def test_legacy_toolkit_adapter_can_register_dormant_progressive_tools() -> (
    None
):
    from lantai.runtime.adapters.legacy_toolkit_components import LegacyToolkit

    def browser_tool() -> ToolResponse:
        return ToolResponse(content=[TextBlock(text="ok")])

    registry = LantaiToolRegistry()
    registry.register_tool(ToolSpec(name="browser_tool"), browser_tool)
    toolkit = LegacyToolkit()
    toolkit.create_tool_group("browser", "浏览器自动化")

    LegacyToolkitAdapter(toolkit).register_tools(
        registry,
        group_name_by_tool=lambda _name: "browser",
    )

    assert toolkit.get_json_schemas() == []
    toolkit.update_tool_groups(["browser"], active=True)
    assert [
        schema["function"]["name"] for schema in toolkit.get_json_schemas()
    ] == ["browser_tool"]


def test_legacy_mcp_content_to_blocks_is_lantai_owned() -> None:
    blocks = legacy_mcp_content_to_blocks(
        [
            mcp_types.TextContent(type="text", text="hello"),
            mcp_types.ImageContent(
                type="image",
                data="image-base64",
                mimeType="image/png",
            ),
            mcp_types.AudioContent(
                type="audio",
                data="audio-base64",
                mimeType="audio/mpeg",
            ),
            mcp_types.EmbeddedResource(
                type="resource",
                resource=mcp_types.TextResourceContents(
                    uri="file:///tmp/demo.txt",
                    mimeType="text/plain",
                    text="resource body",
                ),
            ),
        ],
    )

    assert blocks[0] == {"type": "text", "text": "hello"}
    assert blocks[1] == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": "image-base64",
        },
    }
    assert blocks[2] == {
        "type": "audio",
        "source": {
            "type": "base64",
            "media_type": "audio/mpeg",
            "data": "audio-base64",
        },
    }
    assert blocks[3]["type"] == "text"
    assert "resource body" in blocks[3]["text"]
