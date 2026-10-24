# -*- coding: utf-8 -*-
"""Behavior locks for MCP tool projection and result contracts."""

from types import SimpleNamespace

from lantai.app.mcp.chrome_devtools import (
    CHROME_DEVTOOLS_ALLOWED_TOOLS,
    CHROME_DEVTOOLS_INTEGRATION_POLICY_VERSION,
    CHROME_DEVTOOLS_SCREENCAST_TOOLS,
    chrome_devtools_tool_effect,
)
from lantai.runtime.mcp_tools import (
    mcp_structured_result_metadata,
    project_mcp_tool_spec,
)
from lantai.runtime.tools import McpClientSpec


def _client_spec(**metadata):
    return McpClientSpec(
        name="third-party",
        client=object(),
        metadata=metadata,
    )


def _tool(**overrides):
    payload = {
        "name": "publish_record",
        "description": "Publish a record",
        "inputSchema": {"type": "object", "properties": {}},
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def test_untrusted_mcp_tool_defaults_to_external_non_idempotent_effect() -> None:
    raw = _tool(
        annotations=SimpleNamespace(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
        ),
    )

    spec = project_mcp_tool_spec(raw, _client_spec())

    assert spec.read_only is False
    assert spec.destructive is True
    assert spec.concurrency_safe is False
    assert spec.effect_recovery.external_side_effect is True
    assert spec.effect_recovery.idempotency == "not_idempotent"
    assert spec.effect_recovery.probe == "none"
    assert spec.effect_recovery.on_ambiguity == "manual_disambiguation"
    assert spec.metadata["annotation_trust"] == "untrusted_hint_only"


def test_trusted_read_only_annotation_projects_safe_recovery() -> None:
    raw = _tool(
        annotations=SimpleNamespace(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
            title="Read record",
        ),
    )

    spec = project_mcp_tool_spec(
        raw,
        _client_spec(official_integration="guanlan"),
    )

    assert spec.read_only is True
    assert spec.destructive is False
    assert spec.concurrency_safe is True
    assert spec.effect_recovery.external_side_effect is False
    assert spec.effect_recovery.idempotency == "read_only"
    assert spec.effect_recovery.on_ambiguity == "safe_to_retry"
    assert spec.metadata["annotation_trust"] == "trusted"
    assert spec.metadata["annotations"]["title"] == "Read record"


def test_trusted_idempotent_mutation_uses_natural_key_recovery() -> None:
    raw = _tool(
        outputSchema={"type": "object", "required": ["id"]},
        annotations=SimpleNamespace(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
        ),
        execution=SimpleNamespace(taskSupport="optional"),
    )

    spec = project_mcp_tool_spec(
        raw,
        _client_spec(trust_tool_annotations=True),
    )

    assert spec.read_only is False
    assert spec.destructive is False
    assert spec.effect_recovery.external_side_effect is True
    assert spec.effect_recovery.idempotency == "natural_key"
    assert spec.effect_recovery.on_ambiguity == "safe_to_retry"
    assert spec.metadata["output_schema"] == {
        "type": "object",
        "required": ["id"],
    }
    assert spec.metadata["task_support"] == "optional"


def test_required_tasks_tool_is_fail_closed_until_runtime_support_exists() -> None:
    spec = project_mcp_tool_spec(
        _tool(execution=SimpleNamespace(taskSupport="required")),
        _client_spec(),
    )

    assert spec.enabled is False
    assert spec.metadata["unavailable_reason"] == "mcp_tasks_extension_unsupported"


def _chrome_client_spec(*, enabled: bool = True, valid: bool = True):
    metadata = {"official_integration": "chrome-devtools"}
    if valid:
        metadata["integration_policy_version"] = (
            CHROME_DEVTOOLS_INTEGRATION_POLICY_VERSION
        )
    return McpClientSpec(
        name="Chrome DevTools",
        client=object(),
        enabled=enabled,
        metadata=metadata,
    )


def test_exact_chrome_inventory_uses_host_effects_not_server_annotations() -> None:
    inventory = (*CHROME_DEVTOOLS_ALLOWED_TOOLS, *CHROME_DEVTOOLS_SCREENCAST_TOOLS)
    assert len(inventory) == 57

    for name in inventory:
        raw = _tool(
            name=name,
            annotations=SimpleNamespace(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
            ),
        )
        effect = chrome_devtools_tool_effect(name)

        spec = project_mcp_tool_spec(raw, _chrome_client_spec())

        assert spec.enabled is True, name
        assert spec.read_only is effect.read_only, name
        assert spec.concurrency_safe is effect.read_only, name
        assert spec.destructive is effect.destructive, name
        assert spec.effect_recovery.external_side_effect is (
            effect.external_side_effect
        ), name
        assert spec.effect_recovery.idempotency == effect.idempotency, name
        assert spec.effect_recovery.probe == effect.probe, name
        assert spec.effect_recovery.compensation == effect.compensation, name
        assert spec.effect_recovery.on_ambiguity == effect.on_ambiguity, name
        assert spec.metadata["chrome_devtools_effect"]["category"] == (
            effect.category
        ), name
        assert spec.metadata["official_integration"] == "chrome-devtools"


def test_unreviewed_chrome_tool_is_disabled_even_with_safe_annotations() -> None:
    spec = project_mcp_tool_spec(
        _tool(
            name="future_delete_everything",
            annotations=SimpleNamespace(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
            ),
        ),
        _chrome_client_spec(),
    )

    assert spec.enabled is False
    assert spec.read_only is False
    assert spec.destructive is True
    assert spec.metadata["unavailable_reason"] == ("chrome_devtools_tool_unreviewed")


def test_forged_chrome_metadata_without_host_policy_version_stays_untrusted() -> None:
    raw = _tool(
        name="list_pages",
        official_integration="chrome-devtools",
        annotations=SimpleNamespace(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
        ),
    )

    spec = project_mcp_tool_spec(raw, _chrome_client_spec(valid=False))

    assert spec.enabled is True
    assert spec.read_only is False
    assert spec.destructive is True
    assert spec.metadata["annotation_trust"] == "untrusted_hint_only"
    assert "chrome_devtools_effect" not in spec.metadata


def test_disabled_official_chrome_client_cannot_project_enabled_tools() -> None:
    spec = project_mcp_tool_spec(
        _tool(name="list_pages"),
        _chrome_client_spec(enabled=False),
    )

    assert spec.enabled is False


def test_structured_result_is_preserved_without_raw_transport_object() -> None:
    result = SimpleNamespace(
        structuredContent={"count": 2, "items": ["a", "b"]},
        meta={"request_id": "req-1"},
    )

    metadata = mcp_structured_result_metadata(result)

    assert metadata == {
        "structured_content": {"count": 2, "items": ["a", "b"]},
        "mcp_meta": {"request_id": "req-1"},
    }
