from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from lantai.app.mcp.chrome_devtools import (
    CHROME_DEVTOOLS_ALLOWED_TOOLS,
    CHROME_DEVTOOLS_INTEGRATION_POLICY_VERSION,
    CHROME_DEVTOOLS_SCREENCAST_TOOLS,
    chrome_devtools_tool_effect,
)
from lantai.runtime.mcp_tools import project_mcp_tool_spec
from lantai.runtime.tool_policy import RuntimeToolPolicyMiddleware
from lantai.runtime.tools import McpClientSpec, ToolCall, ToolPolicyContext, ToolSpec


def _chrome_spec(name: str, *, enabled: bool = True) -> ToolSpec:
    return project_mcp_tool_spec(
        SimpleNamespace(
            name=name,
            description=name,
            inputSchema={"type": "object", "properties": {}},
        ),
        McpClientSpec(
            name="Chrome DevTools",
            client=object(),
            enabled=enabled,
            metadata={
                "official_integration": "chrome-devtools",
                "integration_policy_version": (
                    CHROME_DEVTOOLS_INTEGRATION_POLICY_VERSION
                ),
            },
        ),
    )


def _context(
    workspace: Path,
    *,
    access_mode: str = "read_write",
    permission_profile: str = "workspace",
    permission_mode: str = "quiet",
    workspace_grant_id: str | None = "host-workspace-grant",
    request_context: dict | None = None,
) -> ToolPolicyContext:
    return ToolPolicyContext(
        session_id="session-1",
        root_session_id="session-1",
        workspace_dir=str(workspace),
        workspace_access_mode=access_mode,
        workspace_grant_id=workspace_grant_id,
        permission_profile=permission_profile,
        permission_mode=permission_mode,
        approval_level="off",
        distribution="developer_id",
        request_context=request_context or {},
    )


async def _evaluate(
    name: str,
    workspace: Path,
    *,
    arguments: dict | None = None,
    spec: ToolSpec | None = None,
    context: ToolPolicyContext | None = None,
):
    spec = spec or _chrome_spec(name)
    outcome = await RuntimeToolPolicyMiddleware().evaluate(
        ToolCall(id=f"call-{name}", name=name, arguments=arguments or {}),
        spec,
        context or _context(workspace),
    )
    return outcome, spec


@pytest.mark.asyncio
async def test_all_57_tools_follow_the_exact_host_approval_matrix(
    tmp_path: Path,
) -> None:
    inventory = (*CHROME_DEVTOOLS_ALLOWED_TOOLS, *CHROME_DEVTOOLS_SCREENCAST_TOOLS)
    assert len(inventory) == 57

    for name in inventory:
        effect = chrome_devtools_tool_effect(name)
        outcome, spec = await _evaluate(name, tmp_path)
        expected = "allow" if effect.approval == "auto" else "needs_approval"
        assert outcome.action == expected, name
        assert outcome.policy_payload["decision_source"] == (
            "official_chrome_devtools"
        ), name
        assert spec.read_only is effect.read_only, name
        assert spec.destructive is effect.destructive, name
        assert spec.effect_recovery.external_side_effect is (
            effect.external_side_effect
        ), name
        assert spec.effect_recovery.idempotency == effect.idempotency, name
        assert spec.effect_recovery.probe == effect.probe, name
        assert spec.effect_recovery.on_ambiguity == effect.on_ambiguity, name


@pytest.mark.asyncio
async def test_optional_output_path_dynamically_changes_write_and_retry_contract(
    tmp_path: Path,
) -> None:
    inline_spec = _chrome_spec("take_screenshot")
    inline, inline_spec = await _evaluate(
        "take_screenshot",
        tmp_path,
        spec=inline_spec,
        context=_context(tmp_path, access_mode="read_only"),
    )
    assert inline.action == "allow"
    assert inline_spec.read_only is True
    assert inline_spec.effect_recovery.idempotency == "read_only"
    assert inline_spec.effect_recovery.on_ambiguity == "safe_to_retry"

    output_spec = _chrome_spec("take_screenshot")
    denied, output_spec = await _evaluate(
        "take_screenshot",
        tmp_path,
        arguments={"filePath": "screens/page.png"},
        spec=output_spec,
        context=_context(tmp_path, access_mode="read_only"),
    )
    assert denied.action == "deny"
    assert output_spec.read_only is False
    assert output_spec.effect_recovery.idempotency == "natural_key"
    assert output_spec.effect_recovery.probe == "tool_specific"
    assert output_spec.effect_recovery.on_ambiguity == "probe_required"

    allowed, output_spec = await _evaluate(
        "take_screenshot",
        tmp_path,
        arguments={"filePath": "screens/page.png"},
        spec=_chrome_spec("take_screenshot"),
    )
    assert allowed.action == "allow"
    assert output_spec.metadata["chrome_devtools_effect"]["populated_path_fields"] == [
        "filePath"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "arguments", "probe", "ambiguity"),
    [
        (
            "navigate_page",
            {"url": "https://example.com"},
            "browser_observation",
            "probe_required",
        ),
        ("click", {"pageId": 1, "uid": "1_2"}, "browser_observation", "probe_required"),
        ("evaluate_script", {"function": "() => 1"}, "manual", "manual_disambiguation"),
        ("upload_file", {"filePaths": []}, "manual", "manual_disambiguation"),
        ("performance_stop_trace", {}, "browser_observation", "probe_required"),
        ("lighthouse_audit", {}, "browser_observation", "probe_required"),
    ],
)
async def test_external_or_ambiguous_tools_never_gain_blind_retry(
    tmp_path: Path,
    name: str,
    arguments: dict,
    probe: str,
    ambiguity: str,
) -> None:
    outcome, spec = await _evaluate(name, tmp_path, arguments=arguments)

    assert outcome.action in {"allow", "needs_approval", "deny"}
    assert spec.effect_recovery.idempotency == "not_idempotent"
    assert spec.effect_recovery.probe == probe
    assert spec.effect_recovery.on_ambiguity == ambiguity


@pytest.mark.asyncio
async def test_public_navigation_is_allowed_and_private_target_requires_approval(
    tmp_path: Path,
) -> None:
    public, _ = await _evaluate(
        "new_page",
        tmp_path,
        arguments={"url": "https://example.com"},
    )
    private, _ = await _evaluate(
        "new_page",
        tmp_path,
        arguments={"url": "http://127.0.0.1:3000"},
    )
    file_url, _ = await _evaluate(
        "new_page",
        tmp_path,
        arguments={"url": "file:///etc/passwd"},
    )

    assert public.action == "allow"
    assert private.action == "needs_approval"
    assert private.policy_payload["matched_rule_id"] == (
        "chrome_devtools_private_network_prompt"
    )
    assert file_url.action == "deny"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"alternatePath": "result.txt"},
        {"filePath": "bad\x00path"},
        {"filePath": "../escape.txt"},
        {"nested": {"output_file": "secret.txt"}},
        {"workspace_grant_id": "model-invented"},
    ],
)
async def test_path_alias_escape_and_fake_grant_are_hard_denied(
    tmp_path: Path,
    arguments: dict,
) -> None:
    outcome, _ = await _evaluate(
        "take_snapshot",
        tmp_path,
        arguments=arguments,
    )
    assert outcome.action == "deny"
    assert outcome.policy_payload["matched_rule_id"] == (
        "chrome_devtools_path_forbidden"
    )


@pytest.mark.asyncio
async def test_symlink_escape_is_hard_denied(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = tmp_path / "linked.txt"
    link.symlink_to(outside)

    outcome, _ = await _evaluate(
        "get_heapsnapshot_summary",
        tmp_path,
        arguments={"filePath": str(link)},
    )

    assert outcome.action == "deny"


@pytest.mark.asyncio
async def test_upload_requires_host_grant_and_then_still_requires_approval(
    tmp_path: Path,
) -> None:
    upload = tmp_path / "upload.txt"
    upload.write_text("upload", encoding="utf-8")
    missing_grant, _ = await _evaluate(
        "upload_file",
        tmp_path,
        arguments={"filePaths": [str(upload)]},
        context=_context(tmp_path, workspace_grant_id=None),
    )
    granted, _ = await _evaluate(
        "upload_file",
        tmp_path,
        arguments={"filePaths": [str(upload)]},
    )

    assert missing_grant.action == "deny"
    assert granted.action == "needs_approval"
    assert granted.risk == "critical"


@pytest.mark.asyncio
async def test_read_only_workspace_blocks_local_or_external_mutation(
    tmp_path: Path,
) -> None:
    for name in ("click", "close_page", "navigate_page", "upload_file"):
        outcome, _ = await _evaluate(
            name,
            tmp_path,
            context=_context(tmp_path, access_mode="read_only"),
        )
        assert outcome.action == "deny", name


@pytest.mark.asyncio
async def test_full_access_suppresses_prompt_but_not_hard_boundaries(
    tmp_path: Path,
) -> None:
    full = _context(
        tmp_path,
        permission_profile="full",
        permission_mode="full_auto",
    )
    click, _ = await _evaluate("click", tmp_path, context=full)
    escaped, _ = await _evaluate(
        "take_snapshot",
        tmp_path,
        arguments={"filePath": "../escape.txt"},
        context=full,
    )

    assert click.action == "allow"
    assert escaped.action == "deny"


@pytest.mark.asyncio
async def test_runtime_block_and_disabled_tool_win_before_full_access(
    tmp_path: Path,
) -> None:
    full = _context(
        tmp_path,
        permission_profile="full",
        permission_mode="full_auto",
        request_context={"runtime_blocked_tool_names": ["click"]},
    )
    blocked, _ = await _evaluate("click", tmp_path, context=full)
    disabled, _ = await _evaluate(
        "list_pages",
        tmp_path,
        spec=_chrome_spec("list_pages", enabled=False),
    )

    assert blocked.action == "deny"
    assert blocked.metadata["source"] == "runtime_phase_tool_block"
    assert disabled.action == "deny"
    assert disabled.policy_payload["matched_rule_id"] == (
        "chrome_devtools_tool_disabled"
    )


@pytest.mark.asyncio
async def test_read_only_collaborators_get_observation_but_not_interaction(
    tmp_path: Path,
) -> None:
    collaboration = _context(
        tmp_path,
        request_context={"collaboration_read_only": True},
    )
    observed, _ = await _evaluate("list_pages", tmp_path, context=collaboration)
    clicked, _ = await _evaluate("click", tmp_path, context=collaboration)

    assert observed.action == "allow"
    assert clicked.action == "deny"
    assert clicked.metadata["source"] == "collaboration_read_only"


@pytest.mark.asyncio
async def test_execution_member_tool_scope_is_exact_and_fail_closed(
    tmp_path: Path,
) -> None:
    execution = _context(
        tmp_path,
        permission_profile="full",
        permission_mode="full_auto",
        request_context={
            "execution_team_mode": "scoped_execute",
            "delegation_root_run_id": "run-root",
            "delegation_parent_run_id": "run-parent",
            "delegation_parent_attempt_id": "attempt-parent",
            "delegation_parent_effect_id": "effect-parent",
            "delegation_task_id": "team:member-1",
            "delegation_attempt": 1,
            "delegation_allowed_tools": ["click"],
            "delegation_nested_allowed": False,
        },
    )

    allowed, _ = await _evaluate("click", tmp_path, context=execution)
    denied, _ = await _evaluate("list_pages", tmp_path, context=execution)

    assert allowed.action == "allow"
    assert denied.action == "deny"
    assert denied.metadata["source"] == "delegation_tool_scope"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("delegation_root_run_id", None),
        ("delegation_parent_run_id", ""),
        ("delegation_parent_attempt_id", None),
        ("delegation_parent_effect_id", " "),
        ("delegation_task_id", None),
        ("delegation_attempt", 0),
    ],
)
async def test_execution_member_identity_is_complete_and_fail_closed(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    request_context = {
        "execution_team_mode": "scoped_execute",
        "delegation_root_run_id": "run-root",
        "delegation_parent_run_id": "run-parent",
        "delegation_parent_attempt_id": "attempt-parent",
        "delegation_parent_effect_id": "effect-parent",
        "delegation_task_id": "team:member-1",
        "delegation_attempt": 1,
        "delegation_allowed_tools": ["click"],
        "delegation_nested_allowed": False,
    }
    request_context[field] = value
    execution = _context(
        tmp_path,
        permission_profile="full",
        permission_mode="full_auto",
        request_context=request_context,
    )

    outcome, _ = await _evaluate("click", tmp_path, context=execution)

    assert outcome.action == "deny"
    assert outcome.metadata["source"] == "delegation_identity_scope"


@pytest.mark.asyncio
async def test_execution_member_cannot_delegate_even_if_scope_is_forged(
    tmp_path: Path,
) -> None:
    spec = ToolSpec(
        name="collaborate_with_agents",
        origin="builtin",
        read_only=False,
        destructive=False,
    )
    execution = _context(
        tmp_path,
        permission_profile="full",
        permission_mode="full_auto",
        request_context={
            "execution_team_mode": "scoped_execute",
            "delegation_root_run_id": "run-root",
            "delegation_parent_run_id": "run-parent",
            "delegation_parent_attempt_id": "attempt-parent",
            "delegation_parent_effect_id": "effect-parent",
            "delegation_task_id": "team:member-1",
            "delegation_attempt": 1,
            "delegation_allowed_tools": ["collaborate_with_agents"],
            "delegation_nested_allowed": False,
        },
    )

    outcome, _ = await _evaluate(
        "collaborate_with_agents",
        tmp_path,
        spec=spec,
        context=execution,
    )

    assert outcome.action == "deny"
    assert outcome.metadata["source"] == "delegation_nested_denied"


@pytest.mark.asyncio
async def test_forged_metadata_and_unknown_name_do_not_receive_chrome_authority(
    tmp_path: Path,
) -> None:
    forged = ToolSpec(
        name="list_pages",
        origin="mcp",
        read_only=False,
        destructive=True,
        metadata={"official_integration": "chrome-devtools"},
    )
    forged_outcome, forged = await _evaluate(
        "list_pages",
        tmp_path,
        spec=forged,
    )
    unknown, _ = await _evaluate(
        "future_super_tool",
        tmp_path,
        spec=_chrome_spec("future_super_tool"),
    )

    assert forged_outcome.metadata["source"] != "official_chrome_devtools"
    assert forged.metadata.get("chrome_devtools_effect") is None
    assert unknown.action == "deny"
    assert unknown.policy_payload["matched_rule_id"] == (
        "chrome_devtools_tool_unreviewed"
    )
