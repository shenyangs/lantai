# -*- coding: utf-8 -*-
"""Remembered approval helpers for runtime tool policy prompts."""
from __future__ import annotations

import hashlib
import json
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from ..config.config import ToolGuardRememberedApprovalConfig
from ..config.utils import load_config, save_config
from ..runtime.sanitizer import sanitize_runtime_payload

ApprovalDecisionScope = Literal["once", "session", "workspace"]
_APPROVAL_MEMORY_POLICY_VERSION = 2

_REMEMBERABLE_POLICY_RULES = frozenset(
    {
        "shell.install_dependency",
        "profile.workspace_unknown_shell",
        "profile.observe_requires_prompt",
        "profile.observe_write_tool",
    }
)


def normalise_decision_scope(raw: str | None) -> ApprovalDecisionScope:
    """Return the supported approval-memory scope."""

    value = str(raw or "").strip().lower()
    if value in {"session", "allow_session"}:
        return "session"
    if value in {"workspace", "allow_workspace"}:
        return "workspace"
    return "once"


def policy_to_dict(policy_decision: Any) -> dict[str, Any]:
    """Convert a policy object/dict into plain JSON-like data."""

    if policy_decision is None:
        return {}
    if isinstance(policy_decision, dict):
        return dict(policy_decision)
    if hasattr(policy_decision, "model_dump"):
        try:
            return policy_decision.model_dump(mode="json")
        except Exception:
            return {}
    return {}


def policy_rule_ids(policy_decision: Any) -> list[str]:
    """Return stable matched policy rule ids, if available."""

    policy = policy_to_dict(policy_decision)
    raw_rules = policy.get("matched_rules") or policy.get("matched_rule_ids")
    rules: list[str] = []
    if isinstance(raw_rules, list):
        rules = [str(item).strip() for item in raw_rules if str(item).strip()]
    raw_single = policy.get("matched_rule_id")
    if raw_single:
        rules.append(str(raw_single).strip())
    return sorted(set(rules))


def is_policy_rememberable(policy_decision: Any) -> bool:
    """Whether a prompt is safe enough to remember beyond this one call."""

    policy = policy_to_dict(policy_decision)
    if policy.get("decision") != "prompt":
        return False
    if policy.get("workspace_escape"):
        return False
    rules = set(policy_rule_ids(policy))
    return bool(rules and rules <= _REMEMBERABLE_POLICY_RULES)


def _normalise_workspace_dir(workspace_dir: str | None) -> str:
    if not workspace_dir:
        return ""
    try:
        return str(Path(workspace_dir).expanduser().resolve())
    except Exception:
        return str(Path(workspace_dir).expanduser())


def _approval_archive_dir(workspace_dir: str) -> Path:
    return (
        Path(workspace_dir).expanduser()
        / ".lantai"
        / "approvals"
        / "approval-memory-archives"
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _session_policy_fingerprint(policy_decision: Any) -> str:
    """Return a stable, non-secret fingerprint for one prompted policy class."""

    policy = policy_to_dict(policy_decision)
    fingerprint_payload = {
        "decision": str(policy.get("decision") or "prompt"),
        "profile": str(policy.get("profile") or ""),
        "permission_mode": str(policy.get("permission_mode") or ""),
        "matched_rule_ids": policy_rule_ids(policy),
        "command_category": str(policy.get("command_category") or ""),
        "decision_source": str(policy.get("decision_source") or ""),
        "effective_rule_id": str(policy.get("effective_rule_id") or ""),
        "risk": str(policy.get("risk_level") or policy.get("risk") or ""),
        "workspace_escape": bool(policy.get("workspace_escape")),
        "hard_denied": bool(policy.get("hard_denied")),
    }
    encoded = json.dumps(
        fingerprint_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256_text(encoded)


def is_policy_session_rememberable(policy_decision: Any) -> bool:
    """Whether an explicit approval may be reused in the current chat only."""

    policy = policy_to_dict(policy_decision)
    return not bool(policy.get("workspace_escape") or policy.get("hard_denied"))


def build_session_approval_rule(
    *,
    workspace_dir: str,
    tool_name: str,
    policy_decision: Any,
    source_agent_id: str = "",
) -> dict[str, Any] | None:
    """Build a broad in-memory grant that dies with the current root session."""

    if not tool_name or not is_policy_session_rememberable(policy_decision):
        return None
    policy = policy_to_dict(policy_decision)
    normalised_workspace = _normalise_workspace_dir(workspace_dir)
    fingerprint = _session_policy_fingerprint(policy)
    now = datetime.now(timezone.utc).isoformat()
    rule_source = "|".join(
        [
            "session",
            normalised_workspace,
            tool_name,
            fingerprint,
            str(source_agent_id or ""),
        ]
    )
    return {
        "id": f"session-approval-{_sha256_text(rule_source)[:16]}",
        "scope": "session",
        "decision_scope": "session",
        "rule_kind": "session_access",
        "workspace_dir": normalised_workspace,
        "tool_name": "*",
        "approved_from_tool_name": tool_name,
        "permission_profile": str(policy.get("profile") or ""),
        "permission_mode": str(policy.get("permission_mode") or ""),
        "policy_fingerprint": fingerprint,
        "source_agent_id": str(source_agent_id or ""),
        "created_at": now,
        "updated_at": now,
        "enabled": True,
    }


def session_approval_rule_matches(
    rule: dict[str, Any],
    *,
    workspace_dir: str,
    tool_name: str,
    policy_decision: Any,
    source_agent_id: str = "",
) -> bool:
    """Return true when a chat-wide grant covers the current tool call."""

    if rule.get("rule_kind") != "session_access":
        return False
    if not bool(rule.get("enabled", True)):
        return False
    if not is_policy_session_rememberable(policy_decision):
        return False
    if str(rule.get("source_agent_id") or "") != str(source_agent_id or ""):
        return False
    return True


def _approval_matches_lifecycle_scope(
    approval: ToolGuardRememberedApprovalConfig,
    *,
    workspace_dir: str,
    source_agent_id: str,
) -> bool:
    if approval.workspace_dir != workspace_dir:
        return False
    if source_agent_id and approval.source_agent_id != source_agent_id:
        return False
    return True


def approval_memory_lifecycle_cascade(
    *,
    workspace_dir: str,
    source_agent_id: str = "",
    operation: Literal["archive", "delete"] = "archive",
    dry_run: bool = True,
) -> dict[str, Any]:
    """Archive and optionally remove remembered approvals for a workspace."""

    if operation not in {"archive", "delete"}:
        raise ValueError(f"unsupported approval memory lifecycle operation: {operation}")

    normalised_workspace = _normalise_workspace_dir(workspace_dir)
    source_agent = str(source_agent_id or "").strip()
    config = load_config()
    remembered = list(config.security.tool_guard.remembered_approvals)
    matching = [
        approval
        for approval in remembered
        if _approval_matches_lifecycle_scope(
            approval,
            workspace_dir=normalised_workspace,
            source_agent_id=source_agent,
        )
    ]
    matching_ids = [approval.id for approval in matching]
    report: dict[str, Any] = {
        "kind": "approval_memory_lifecycle",
        "scope": "workspace",
        "operation": operation,
        "dry_run": bool(dry_run),
        "workspace_dir": normalised_workspace,
        "source_agent_id": source_agent,
        "matched_count": len(matching),
        "matched_rule_ids": matching_ids,
        "archive_path": None,
        "archive_sha256": None,
        "removed_count": 0,
        "remaining_count": len(remembered),
    }
    if not matching:
        return report

    if not dry_run:
        archive_payload = sanitize_runtime_payload(
            {
                "kind": "approval_memory_archive",
                "workspace_dir": normalised_workspace,
                "source_agent_id": source_agent,
                "operation": operation,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "approval_count": len(matching),
                "approvals": [
                    approval.model_dump(mode="json")
                    for approval in matching
                ],
            },
        )
        archive_text = json.dumps(
            archive_payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        archive_dir = _approval_archive_dir(normalised_workspace)
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / (
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-"
            f"{_sha256_text('|'.join(matching_ids))[:12]}.json"
        )
        archive_path.write_text(archive_text, encoding="utf-8")
        report["archive_path"] = str(archive_path)
        report["archive_sha256"] = f"sha256:{_sha256_text(archive_text)}"

    if operation == "delete" and not dry_run:
        kept = [
            approval
            for approval in remembered
            if not _approval_matches_lifecycle_scope(
                approval,
                workspace_dir=normalised_workspace,
                source_agent_id=source_agent,
            )
        ]
        config.security.tool_guard.remembered_approvals = kept
        save_config(config)
        report["removed_count"] = len(remembered) - len(kept)
        report["remaining_count"] = len(kept)

    return report


def _command_head(tool_name: str, tool_input: dict[str, Any]) -> str:
    if tool_name != "execute_shell_command":
        return ""
    command = str(tool_input.get("command") or "").strip()
    if not command:
        return ""
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    if not parts:
        return ""
    return Path(parts[0]).name


def _command_text(tool_name: str, tool_input: dict[str, Any]) -> str:
    if tool_name != "execute_shell_command":
        return ""
    return str(tool_input.get("command") or "").strip()


def _paths_from_tool_input(tool_input: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in ("path", "file_path", "filepath", "target_path", "output_path"):
        raw = tool_input.get(key)
        if isinstance(raw, str) and raw.strip():
            paths.append(raw.strip())
    return paths


def build_approval_memory_rule(
    *,
    scope: Literal["workspace"],
    workspace_dir: str,
    tool_name: str,
    tool_input: dict[str, Any],
    policy_decision: Any,
    source_agent_id: str = "",
    workspace_grant_id: str = "",
) -> ToolGuardRememberedApprovalConfig | None:
    """Build a persisted workspace approval rule for a remembered prompt."""

    if scope != "workspace":
        return None
    if not is_policy_rememberable(policy_decision):
        return None
    policy = policy_to_dict(policy_decision)
    matched_rule_ids = policy_rule_ids(policy)
    normalised_workspace = _normalise_workspace_dir(workspace_dir)
    if not normalised_workspace or not tool_name or not matched_rule_ids:
        return None
    try:
        workspace_stat = Path(normalised_workspace).stat()
    except OSError:
        return None
    now = datetime.now(timezone.utc).isoformat()
    command_head = _command_head(tool_name, tool_input)
    command_pattern = str(policy.get("command_pattern_suggestion") or "")
    path_pattern = str(policy.get("path_pattern_suggestion") or "")
    rule_id_source = "|".join(
        [
            "workspace",
            normalised_workspace,
            str(workspace_grant_id or ""),
            str(workspace_stat.st_dev),
            str(workspace_stat.st_ino),
            str(_APPROVAL_MEMORY_POLICY_VERSION),
            tool_name,
            ",".join(matched_rule_ids),
            str(policy.get("profile") or ""),
            command_head,
            command_pattern,
            path_pattern,
            str(source_agent_id or ""),
        ]
    )
    rule_id = hashlib.sha256(rule_id_source.encode("utf-8")).hexdigest()[:16]
    return ToolGuardRememberedApprovalConfig(
        id=f"approval-{rule_id}",
        scope=scope,
        workspace_dir=normalised_workspace,
        workspace_grant_id=str(workspace_grant_id or ""),
        workspace_device=workspace_stat.st_dev,
        workspace_inode=workspace_stat.st_ino,
        policy_version=_APPROVAL_MEMORY_POLICY_VERSION,
        tool_name=tool_name,
        permission_profile=str(policy.get("profile") or "workspace"),
        matched_rule_ids=matched_rule_ids,
        command_head=command_head,
        command_pattern=command_pattern,
        path_pattern=path_pattern,
        risk_level=str(policy.get("risk_level") or policy.get("risk") or "medium"),
        approval_copy=str(
            policy.get("approval_copy")
            or policy.get("suggested_approval_message")
            or policy.get("reason")
            or ""
        ),
        policy_decision=policy,
        source_agent_id=source_agent_id,
        created_at=now,
        updated_at=now,
    )


def approval_rule_matches(
    rule: ToolGuardRememberedApprovalConfig | dict[str, Any],
    *,
    workspace_dir: str,
    tool_name: str,
    tool_input: dict[str, Any],
    policy_decision: Any,
    source_agent_id: str = "",
    workspace_grant_id: str = "",
) -> bool:
    """Return true when a remembered rule covers this policy prompt."""

    if isinstance(rule, dict):
        try:
            rule = ToolGuardRememberedApprovalConfig.model_validate(rule)
        except Exception:
            return False
    if not rule.enabled:
        return False
    if rule.scope != "workspace":
        return False
    if rule.tool_name != tool_name:
        return False
    if rule.workspace_dir != _normalise_workspace_dir(workspace_dir):
        return False
    if rule.policy_version != _APPROVAL_MEMORY_POLICY_VERSION:
        return False
    if rule.source_agent_id != str(source_agent_id or ""):
        return False
    if rule.workspace_grant_id != str(workspace_grant_id or ""):
        return False
    try:
        workspace_stat = Path(rule.workspace_dir).stat()
    except OSError:
        return False
    if (
        rule.workspace_device != workspace_stat.st_dev
        or rule.workspace_inode != workspace_stat.st_ino
    ):
        return False
    policy = policy_to_dict(policy_decision)
    if str(rule.permission_profile or "") != str(policy.get("profile") or ""):
        return False
    if sorted(rule.matched_rule_ids) != policy_rule_ids(policy):
        return False
    command_pattern = str(getattr(rule, "command_pattern", "") or "").strip()
    if command_pattern:
        import re

        try:
            if not re.search(command_pattern, _command_text(tool_name, tool_input)):
                return False
        except re.error:
            return False
    elif rule.command_head and rule.command_head != _command_head(tool_name, tool_input):
        return False
    path_pattern = str(getattr(rule, "path_pattern", "") or "").strip()
    if path_pattern:
        from fnmatch import fnmatch

        candidates = _paths_from_tool_input(tool_input)
        if not candidates or not any(fnmatch(path, path_pattern) for path in candidates):
            return False
    return True


def workspace_approval_matches(
    *,
    workspace_dir: str,
    tool_name: str,
    tool_input: dict[str, Any],
    policy_decision: Any,
    source_agent_id: str = "",
    workspace_grant_id: str = "",
) -> ToolGuardRememberedApprovalConfig | None:
    """Find a persisted workspace approval rule that covers this prompt."""

    if not is_policy_rememberable(policy_decision):
        return None
    config = load_config()
    remembered = config.security.tool_guard.remembered_approvals
    for rule in remembered:
        if approval_rule_matches(
            rule,
            workspace_dir=workspace_dir,
            tool_name=tool_name,
            tool_input=tool_input,
            policy_decision=policy_decision,
            source_agent_id=source_agent_id,
            workspace_grant_id=workspace_grant_id,
        ):
            return rule
    return None


def remember_workspace_approval(
    *,
    workspace_dir: str,
    tool_name: str,
    tool_input: dict[str, Any],
    policy_decision: Any,
    source_agent_id: str = "",
    workspace_grant_id: str = "",
) -> ToolGuardRememberedApprovalConfig | None:
    """Persist or refresh a workspace-scoped approval rule in config.json."""

    rule = build_approval_memory_rule(
        scope="workspace",
        workspace_dir=workspace_dir,
        tool_name=tool_name,
        tool_input=tool_input,
        policy_decision=policy_decision,
        source_agent_id=source_agent_id,
        workspace_grant_id=workspace_grant_id,
    )
    if rule is None:
        return None

    config = load_config()
    remembered = list(config.security.tool_guard.remembered_approvals)
    for index, existing in enumerate(remembered):
        if existing.id == rule.id:
            rule.created_at = existing.created_at or rule.created_at
            remembered[index] = rule
            break
    else:
        remembered.append(rule)
    config.security.tool_guard.remembered_approvals = remembered
    save_config(config)
    return rule
