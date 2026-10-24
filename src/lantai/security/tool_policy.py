# -*- coding: utf-8 -*-
"""Lightweight tool permission policy for runtime auditing.

This policy is deliberately smaller than the existing tool_guard subsystem:
tool_guard detects risky parameters and drives approvals, while ToolPolicy
answers a product question: what permission profile would this tool call need,
and why?
"""
from __future__ import annotations

import os
import re
import shlex
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from lantai.security.shell_effects import extract_shell_effects


ToolPolicyAction = Literal["allow", "prompt", "forbidden"]
ToolPolicyRisk = Literal["low", "medium", "high", "critical"]
ToolPermissionProfileName = Literal["observe", "workspace", "full"]
ToolDistribution = Literal["developer_id", "app_store"]
ToolPermissionMode = Literal[
    "default",
    "plan",
    "accept_edits",
    "quiet",
    "full_auto",
]


class ToolPermissionProfile(BaseModel):
    """A user-facing permission profile."""

    id: ToolPermissionProfileName
    name: str
    description: str
    examples: list[str] = Field(default_factory=list)


class ToolPolicyDecision(BaseModel):
    """Decision returned by the built-in tool policy."""

    decision: ToolPolicyAction
    reason: str
    profile: ToolPermissionProfileName
    matched_rule_id: str | None = None
    risk: ToolPolicyRisk = "low"
    matched_rule_ids: list[str] = Field(default_factory=list)
    requires_approval: bool = False
    workspace_escape: bool = False
    suggested_approval_message: str | None = None
    distribution: ToolDistribution = "developer_id"
    risk_level: ToolPolicyRisk = "low"
    matched_rules: list[str] = Field(default_factory=list)
    approval_copy: str | None = None
    permission_scope_suggestion: ToolPermissionProfileName = "workspace"
    rememberable: bool = False
    permission_mode: ToolPermissionMode = "quiet"
    command_category: str | None = None
    command_head: str | None = None
    decision_source: str | None = None
    effective_rule_id: str | None = None
    hard_denied: bool = False
    command_pattern_suggestion: str | None = None
    path_pattern_suggestion: str | None = None


class ToolExecutionContext(BaseModel):
    """Context carried into every runtime tool policy decision."""

    agent_id: str | None = None
    chat_id: str | None = None
    turn_id: str | None = None
    workspace_dir: str | None = None
    permission_profile: ToolPermissionProfileName = "workspace"
    permission_mode: ToolPermissionMode | None = None
    permission_mode_source: str | None = None
    command_rules: list[dict[str, Any]] = Field(default_factory=list)
    distribution: ToolDistribution | None = None


PERMISSION_PROFILES: tuple[ToolPermissionProfile, ...] = (
    ToolPermissionProfile(
        id="observe",
        name="观察",
        description="只允许读取、搜索、浏览、截图等观察动作，不允许写入或修改系统。",
        examples=["rg", "ls", "cat", "sed -n", "git status"],
    ),
    ToolPermissionProfile(
        id="workspace",
        name="工作区",
        description="默认档位。允许工作区内读写；安装、删除、改权限等高风险操作需要确认。",
        examples=["pytest", "npm run build", "apply_patch", "git diff"],
    ),
    ToolPermissionProfile(
        id="full",
        name="完全访问",
        description="高级档位。本次任务不再重复询问，但硬拒绝规则仍然有效。",
        examples=["codesign", "xcodebuild", "hdiutil"],
    ),
)

_PROFILE_ORDER: dict[ToolPermissionProfileName, int] = {
    "observe": 0,
    "workspace": 1,
    "full": 2,
}

_FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "shell.rm_root",
        re.compile(r"(^|[;&|]\s*)rm\s+-[^\n;|&]*r[^\n;|&]*\s+/(?:\s|$)"),
        "禁止递归删除根目录。",
    ),
    (
        "shell.rm_home",
        re.compile(
            r"(^|[;&|]\s*)rm\s+-[^\n;|&]*r[^\n;|&]*\s+(?:~|\$HOME)(?:/|\s|$)"
        ),
        "禁止无确认清理用户主目录。",
    ),
    (
        "shell.git_dir_destructive",
        re.compile(r"(^|[;&|]\s*)rm\s+-[^\n;|&]*r[^\n;|&]*\s+\.git(?:/|\s|$)"),
        "禁止破坏 .git 目录。",
    ),
    (
        "shell.git_reset_hard",
        re.compile(r"(^|[;&|]\s*)git\s+reset\s+--hard(?:\s|$)"),
        "禁止直接执行 git reset --hard。",
    ),
    (
        "shell.git_clean_force",
        re.compile(r"(^|[;&|]\s*)git\s+clean\s+-(?:[^\n;|&]*f[^\n;|&]*d|[^\n;|&]*d[^\n;|&]*f)"),
        "禁止直接执行强制清理工作区。",
    ),
)

_PROMPT_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "shell.install_dependency",
        re.compile(
            r"(^|[;&|]\s*)(?:npm|pnpm|yarn|bun|pip|pip3|uv|poetry|brew|cargo)\s+"
            r"(?:add|install|i|pip\s+install|sync|update|upgrade)"
        ),
        "安装或更新依赖需要用户确认。",
    ),
    (
        "shell.network_pipe",
        re.compile(r"(?:curl|wget)\b[^\n;|&]*(?:\||>\s*/|\bbash\b|\bsh\b)"),
        "网络下载并执行或写入敏感位置需要用户确认。",
    ),
    (
        "shell.privilege_escalation",
        re.compile(r"(^|[;&|]\s*)sudo\b"),
        "提权操作需要用户确认。",
    ),
    (
        "shell.permission_change",
        re.compile(r"(^|[;&|]\s*)(?:chmod|chown)\b"),
        "修改权限或所有者需要用户确认。",
    ),
    (
        "shell.delete_recursive",
        re.compile(r"(^|[;&|]\s*)rm\s+-[^\n;|&]*r"),
        "递归删除需要用户确认。",
    ),
)

_SENSITIVE_SYSTEM_ROOTS = tuple(
    Path(path)
    for path in (
        "/Applications",
        "/Library",
        "/System",
        "/usr/local",
        "/opt/homebrew",
        "/etc",
    )
)
_WRITE_HEADS = {"cp", "mv", "rm", "mkdir", "touch", "tee", "chmod", "chown"}
_MAS_DISTRIBUTION_CHANNELS = {"mas", "appstore", "app-store", "testflight"}

_READ_ONLY_HEADS = {
    "awk",
    "cat",
    "date",
    "du",
    "echo",
    "find",
    "git",
    "grep",
    "head",
    "jq",
    "ls",
    "md5",
    "md5sum",
    "pwd",
    "rg",
    "sed",
    "shasum",
    "stat",
    "tail",
    "tree",
    "wc",
    "which",
}

_READ_ONLY_GIT_SUBCOMMANDS = {
    "branch",
    "diff",
    "log",
    "show",
    "status",
    "rev-parse",
    "ls-files",
}

_WORKSPACE_ALLOWED_HEADS = _READ_ONLY_HEADS | {
    "cargo",
    "go",
    "make",
    "node",
    "npm",
    "pnpm",
    "pytest",
    "python",
    "python3",
    "swift",
    "uv",
    "xcodebuild",
}

_FILE_READ_TOOLS = {
    "read_file",
    "view_text_file",
    "desktop_screenshot",
    "view_image",
}
_FILE_WRITE_TOOLS = {
    "write_file",
    "edit_file",
    "append_file",
    "write_text_file",
    "copy_skill_resource",
    "export_analysis_evidence",
}
_SHELL_TOOLS = {
    "execute_shell_command",
    "execute_python_code",
    "run_skill_script",
}
_FILE_SEND_TOOLS = {
    "send_file_to_user", "send_file",
    "create_analysis_report", "create_analysis_chart", "create_analysis_presentation",
}
_ANALYSIS_LOCAL_TOOLS = {
    "import_data_table", "analyze_data_table", "refresh_data_table", "refresh_analysis_result",
}
_RESEARCH_WORKSPACE_TOOLS = {
    "research_project", "research_read", "research_report", "research_review",
    "research_library", "research_export",
}
_SIDE_EFFECT_TOOLS = (
    _FILE_WRITE_TOOLS
    | _SHELL_TOOLS
    | _FILE_SEND_TOOLS
    | {"mac_act", "mac_session"}
)
_READ_ONLY_TOOL_NAMES = _FILE_READ_TOOLS | {
    "get_analysis_result",
    "get_analysis_delivery_status",
    "read_skill",
    "list_skill_resources",
    "grep_search",
    "glob_search",
    "get_current_time",
    "get_token_usage",
    "list_agents",
    "check_agent_task",
    "view_video",
    "wiki_search",
    "wiki_read",
    "wiki_graph_neighbors",
    "wiki_ingest_status",
    "web_search",
    "web_fetch",
    "inspect_lantai_customization",
    "mac_inspect",
    "mac_observe",
    "mac_verify",
}
_LOW_RISK_INTERNAL_TOOLS = {
    "chat_with_agent",
    "collaborate_with_agents",
    "prepare_scheduled_task",
    "prepare_lantai_customization",
}
_PROMPT_ONLY_TOOLS = {
    "set_user_timezone",
    "submit_to_agent",
    "clear_analysis_data",
}
_BROWSER_TOOLS = {
    "browser_find",
    "browser_task",
    "browser_use",
    "browser_workbench",
}
_MAC_USE_TOOLS = {
    "mac_inspect",
    "mac_observe",
    "mac_act",
    "mac_verify",
    "mac_session",
}
_UNKNOWN_HARD_DENY_TOKENS = (
    "bash",
    "code",
    "exec",
    "python",
    "shell",
)
_BROWSER_READ_ACTIONS = {
    "status",
    "navigate",
    "open",
    "new_tab",
    "select_tab",
    "close_tab",
    "close_task_group",
    "tabs",
    "find",
    "snapshot",
    "snapshot_ax",
    "snapshot_dom",
    "screenshot",
    "take_screenshot",
    "network_list",
    "network_get",
    "network_requests",
    "console_list",
    "console_messages",
    "performance_trace",
    "screencast_start",
    "screencast_stop",
    "pdf",
    "hover",
    "wait_for",
}
_BROWSER_WRITE_ACTIONS = {
    "click",
    "click_at",
    "type",
    "fill_form",
    "select",
    "select_option",
    "drag",
    "upload_file",
    "download_wait",
    "press_key",
    "cookies_get",
    "cookies_set",
    "cookies_clear",
    "clear_browser_cache",
}

_BROWSER_READ_TASK_OPERATIONS = {
    "artifacts",
    "cancel",
    "create",
    "events",
    "get",
    "list",
    "pause",
    "resume",
    "run",
    "start",
    "status",
}

_BROWSER_READ_NAVIGATION_KEYS = {
    "arrowdown",
    "arrowleft",
    "arrowright",
    "arrowup",
    "end",
    "escape",
    "home",
    "pagedown",
    "pageup",
    "space",
    "spacebar",
}


def _browser_tool_is_read_only(tool_name: str, params: dict[str, Any]) -> bool:
    if tool_name == "browser_find":
        return True
    action = str(params.get("action") or "").strip().lower()
    if tool_name == "browser_task":
        operation = str(params.get("operation") or "create").strip().lower()
        access = str(params.get("access") or "read").strip().lower()
        return operation in _BROWSER_READ_TASK_OPERATIONS and (
            operation not in {"create", "run", "start"} or access == "read"
        )
    if action in _BROWSER_READ_ACTIONS:
        return True
    if action != "press_key":
        return False
    raw_key = str(params.get("key") or params.get("text") or "")
    key = re.sub(
        r"[\s_+-]+",
        "",
        raw_key.strip().lower(),
    )
    if not key and raw_key:
        key = "space"
    return key in _BROWSER_READ_NAVIGATION_KEYS


_VALID_PERMISSION_MODES: set[str] = {
    "default",
    "plan",
    "accept_edits",
    "quiet",
    "full_auto",
}
_VALID_RULE_EFFECTS = {"allow", "ask", "deny"}

_REMEMBERABLE_PROMPT_RULES = {
    "shell.install_dependency",
    "profile.workspace_unknown_shell",
    "profile.observe_requires_prompt",
    "profile.observe_write_tool",
}


def _detect_distribution(distribution: str | None = None) -> ToolDistribution:
    raw = distribution
    normalized = str(raw or "").strip().lower()
    if normalized:
        if normalized in _MAS_DISTRIBUTION_CHANNELS or normalized in {
            "app_store",
            "app-store",
        }:
            return "app_store"
        return "developer_id"

    raw = (
        os.environ.get("LANTAI_DISTRIBUTION_CHANNEL", "")
        or os.environ.get("LANTAI_RELEASE_CHANNEL", "")
        or os.environ.get("LANTAI_DISTRIBUTION", "")
    )
    normalized = str(raw or "").strip().lower()
    app_store_flag = os.environ.get("LANTAI_APP_STORE", "").strip().lower()
    if (
        normalized in _MAS_DISTRIBUTION_CHANNELS
        or normalized
        in {
            "app_store",
            "app-store",
        }
        or app_store_flag in {"1", "true", "yes"}
    ):
        return "app_store"
    if any(
        "lantai_app store" == part.lower() for part in Path(__file__).parts):
        return "app_store"
    return "developer_id"


def _load_tool_guard_policy_defaults() -> dict[str, Any]:
    """Load optional permission-policy defaults from config.json."""

    try:
        from ..config.utils import load_config

        guard = load_config().security.tool_guard
        return {
            "permission_mode": str(getattr(guard, "permission_mode", "quiet") or "quiet"),
            "default_workspace_dir": str(
                getattr(guard, "default_workspace_dir", "") or "",
            ),
            "command_rules": [
                rule.model_dump(mode="json")
                if hasattr(rule, "model_dump")
                else dict(rule)
                for rule in list(getattr(guard, "command_rules", []) or [])
            ],
        }
    except Exception:
        return {
            "permission_mode": "quiet",
            "default_workspace_dir": "",
            "command_rules": [],
        }


def _normalise_permission_mode(
    mode: str | None,
    *,
    distribution: str | None = None,
) -> tuple[ToolPermissionMode, str | None]:
    requested = str(mode or "").strip().lower() or "quiet"
    if requested not in _VALID_PERMISSION_MODES:
        requested = "quiet"
    active_distribution = _detect_distribution(distribution)
    if requested == "full_auto" and active_distribution == "app_store":
        return "quiet", "mode.full_auto_downgraded"
    return requested, None  # type: ignore[return-value]


def list_permission_profiles(
    distribution: str | None = None,
) -> list[ToolPermissionProfile]:
    """Return built-in permission profiles."""

    active_distribution = _detect_distribution(distribution)
    return [
        profile.model_copy()
        for profile in PERMISSION_PROFILES
        if active_distribution != "app_store" or profile.id != "full"
    ]


def _normalise_profile(
    profile: str | None,
    *,
    distribution: str | None = None,
) -> ToolPermissionProfileName:
    active_distribution = _detect_distribution(distribution)
    if profile in _PROFILE_ORDER:
        if profile == "full" and active_distribution == "app_store":
            return "workspace"
        return profile  # type: ignore[return-value]
    return "workspace"


def _normalise_context(
    context: ToolExecutionContext | dict[str, Any] | None,
    *,
    profile: str | None = None,
) -> ToolExecutionContext:
    if isinstance(context, ToolExecutionContext):
        data = context.model_dump()
    elif isinstance(context, dict):
        data = dict(context)
    else:
        data = {}
    defaults = _load_tool_guard_policy_defaults()
    distribution = _detect_distribution(data.get("distribution") or None)
    requested_profile = profile or str(data.get("permission_profile") or "")
    requested_mode = str(data.get("permission_mode") or "").strip() or str(
        defaults.get("permission_mode") or "quiet"
    )
    permission_mode, mode_source = _normalise_permission_mode(
        requested_mode,
        distribution=distribution,
    )
    data["distribution"] = distribution
    data["permission_profile"] = _normalise_profile(
        requested_profile,
        distribution=distribution,
    )
    data["permission_mode"] = permission_mode
    if not data.get("workspace_dir") and defaults.get("default_workspace_dir"):
        data["workspace_dir"] = defaults.get("default_workspace_dir")
    if not data.get("command_rules"):
        data["command_rules"] = defaults.get("command_rules") or []
    if mode_source:
        data["permission_mode_source"] = mode_source
    return ToolExecutionContext.model_validate(data)


def _is_full_access_mode(
    profile: ToolPermissionProfileName,
    permission_mode: ToolPermissionMode,
    distribution: ToolDistribution,
) -> bool:
    """Return whether the user selected chat-scoped complete access.

    Complete access is intentionally a paired setting.  The chat composer
    sends both ``permission_profile=full`` and ``permission_mode=full_auto``;
    requiring both prevents a stale global mode from silently broadening a
    workspace-scoped request.  App Store builds never expose this pair.
    """

    return (
        profile == "full"
        and permission_mode == "full_auto"
        and distribution != "app_store"
    )


def _normalise_command(command: str) -> str:
    return " ".join((command or "").strip().split())


def _command_parts(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _command_head(command: str) -> str:
    parts = _command_parts(command)
    if not parts:
        return ""
    return Path(parts[0]).name


def _command_pattern_suggestion(command: str) -> str | None:
    head = _command_head(command)
    if not head:
        return None
    if head == "git":
        parts = _command_parts(command)
        if len(parts) >= 2:
            return rf"^git\s+{re.escape(parts[1])}\b"
    return rf"^{re.escape(head)}\b"


def _path_pattern_suggestion(context: ToolExecutionContext) -> str | None:
    workspace_dir = str(context.workspace_dir or "").strip()
    if not workspace_dir:
        return None
    try:
        return str(Path(workspace_dir).expanduser().resolve() / "**")
    except Exception:
        return str(Path(workspace_dir).expanduser() / "**")


def _paths_from_tool_input(tool_input: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    for key in ("path", "file_path", "filepath", "target_path", "output_path"):
        raw = tool_input.get(key)
        if isinstance(raw, str) and raw.strip():
            paths.append(Path(raw.strip()))
    for key in ("paths", "files", "file_paths"):
        raw_list = tool_input.get(key)
        if isinstance(raw_list, list):
            for raw in raw_list:
                if isinstance(raw, str) and raw.strip():
                    paths.append(Path(raw.strip()))
    return paths


def _tool_path_workspace_escape(
    tool_input: dict[str, Any],
    context: ToolExecutionContext,
) -> tuple[str, str] | None:
    workspace_dir = (context.workspace_dir or "").strip()
    if not workspace_dir:
        return None
    root = Path(workspace_dir).expanduser()
    for path in _paths_from_tool_input(tool_input):
        candidate = path.expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        if not _path_is_under(candidate, root):
            return (
                "tool.workspace_escape",
                "工具会写入或修改工作区之外的路径，需要用户确认。",
            )
    return None


def _rule_text(rule: dict[str, Any], key: str) -> str:
    return str(rule.get(key) or "").strip()


def _rule_matches_path_pattern(
    path_pattern: str,
    *,
    command: str,
    tool_input: dict[str, Any],
) -> bool:
    if not path_pattern:
        return True
    effects = extract_shell_effects(command)
    candidates = [
        raw
        for raw in (*effects.read_paths, *effects.write_paths)
        if Path(raw).expanduser().is_absolute()
    ]
    candidates.extend(str(path) for path in _paths_from_tool_input(tool_input))
    if not candidates:
        return False
    return any(fnmatch(candidate, path_pattern) for candidate in candidates)


def _matching_command_rule(
    *,
    tool_name: str,
    command: str,
    tool_input: dict[str, Any],
    context: ToolExecutionContext,
    effect: str | None = None,
) -> dict[str, Any] | None:
    for rule in context.command_rules or []:
        if not isinstance(rule, dict) or rule.get("enabled") is False:
            continue
        raw_effect = _rule_text(rule, "effect").lower()
        if raw_effect not in _VALID_RULE_EFFECTS:
            continue
        if effect and raw_effect != effect:
            continue
        rule_tool = _rule_text(rule, "tool_name")
        if rule_tool and rule_tool not in {"*", tool_name}:
            continue
        command_pattern = _rule_text(rule, "command_pattern")
        if command_pattern:
            try:
                if not re.search(command_pattern, command):
                    continue
            except re.error:
                continue
        path_pattern = _rule_text(rule, "path_pattern")
        if not _rule_matches_path_pattern(
            path_pattern,
            command=command,
            tool_input=tool_input,
        ):
            continue
        return rule
    return None


def _is_read_only_git(command: str) -> bool:
    parts = _command_parts(command)
    if len(parts) < 2 or Path(parts[0]).name != "git":
        return False
    return parts[1] in _READ_ONLY_GIT_SUBCOMMANDS


def _is_obviously_read_only(command: str) -> bool:
    head = _command_head(command)
    if not head:
        return True
    if head == "git":
        return _is_read_only_git(command)
    if head not in _READ_ONLY_HEADS:
        return False
    if re.search(
        r"(^|[;&|]\s*)(?:rm|mv|cp|mkdir|touch|tee|chmod|chown)\b", command
    ):
        return False
    if extract_shell_effects(command).write_paths:
        return False
    return True


def _match_rule(
    command: str,
    rules: tuple[tuple[str, re.Pattern[str], str], ...],
) -> tuple[str, str] | None:
    for rule_id, pattern, reason in rules:
        if pattern.search(command):
            return rule_id, reason
    return None


def _looks_like_sensitive_alias(tool_name: str) -> bool:
    lowered = str(tool_name or "").strip().lower()
    return any(token in lowered for token in _UNKNOWN_HARD_DENY_TOKENS)


def _decision(
    decision: ToolPolicyAction,
    reason: str,
    profile: ToolPermissionProfileName,
    matched_rule_id: str | None = None,
    *,
    risk: ToolPolicyRisk = "low",
    workspace_escape: bool = False,
    distribution: ToolDistribution = "developer_id",
    suggested_approval_message: str | None = None,
    permission_mode: ToolPermissionMode = "quiet",
    command: str | None = None,
    command_category: str | None = None,
    decision_source: str | None = None,
    effective_rule_id: str | None = None,
    hard_denied: bool = False,
    command_pattern_suggestion: str | None = None,
    path_pattern_suggestion: str | None = None,
    rememberable: bool | None = None,
) -> ToolPolicyDecision:
    matched_rules = [matched_rule_id] if matched_rule_id else []
    approval_copy = suggested_approval_message or (
        reason if decision == "prompt" else None
    )
    can_remember = (
        decision == "prompt"
        and matched_rule_id in _REMEMBERABLE_PROMPT_RULES
        and not workspace_escape
    )
    if rememberable is not None:
        can_remember = rememberable
    command_head = (
        _command_head(command or "") if command is not None else None
    )
    return ToolPolicyDecision(
        decision=decision,
        reason=reason,
        profile=profile,
        matched_rule_id=matched_rule_id,
        risk=risk,
        matched_rule_ids=matched_rules,
        requires_approval=decision == "prompt",
        workspace_escape=workspace_escape,
        suggested_approval_message=(
            suggested_approval_message
            or (reason if decision == "prompt" else None)
        ),
        distribution=distribution,
        risk_level=risk,
        matched_rules=matched_rules,
        approval_copy=approval_copy,
        permission_scope_suggestion=profile,
        rememberable=can_remember,
        permission_mode=permission_mode,
        command_category=command_category,
        command_head=command_head,
        decision_source=decision_source,
        effective_rule_id=effective_rule_id,
        hard_denied=hard_denied,
        command_pattern_suggestion=(
            command_pattern_suggestion
            or (_command_pattern_suggestion(command or "") if command else None)
        ),
        path_pattern_suggestion=path_pattern_suggestion,
    )


def _decision_from_command_rule(
    rule: dict[str, Any],
    *,
    profile: ToolPermissionProfileName,
    permission_mode: ToolPermissionMode,
    distribution: ToolDistribution,
    command: str = "",
    tool_name: str,
    risk: ToolPolicyRisk = "medium",
) -> ToolPolicyDecision:
    effect = _rule_text(rule, "effect").lower()
    rule_id = _rule_text(rule, "id") or "custom"
    description = _rule_text(rule, "description")
    source = f"user_rule.{rule_id}"
    if effect == "deny":
        return _decision(
            "forbidden",
            description or f"权限规则 {rule_id} 拒绝 {tool_name}。",
            profile,
            source,
            risk="high",
            distribution=distribution,
            permission_mode=permission_mode,
            command=command,
            command_category="user_rule",
            decision_source="user_rule",
            effective_rule_id=rule_id,
        )
    if effect == "ask":
        return _decision(
            "prompt",
            description or f"权限规则 {rule_id} 要求确认 {tool_name}。",
            profile,
            source,
            risk=risk,
            distribution=distribution,
            permission_mode=permission_mode,
            command=command,
            command_category="user_rule",
            decision_source="user_rule",
            effective_rule_id=rule_id,
            rememberable=False,
        )
    return _decision(
        "allow",
        description or f"权限规则 {rule_id} 允许 {tool_name}。",
        profile,
        source,
        risk="low",
        distribution=distribution,
        permission_mode=permission_mode,
        command=command,
        command_category="user_rule",
        decision_source="user_rule",
        effective_rule_id=rule_id,
    )


def _path_is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def _writes_sensitive_system_path(command: str) -> bool:
    """Match real system roots without confusing ``~/Library`` with ``/Library``."""

    return any(
        any(
            _path_is_under(Path(raw).expanduser(), root)
            for root in _SENSITIVE_SYSTEM_ROOTS
        )
        for raw in extract_shell_effects(command).write_paths
    )


def _write_target_paths_from_command(command: str, root: Path) -> list[Path]:
    paths: list[Path] = []
    for token in extract_shell_effects(command).write_paths:
        path = Path(token).expanduser()
        if not path.is_absolute():
            path = root / path
        paths.append(path)
    return paths


def _workspace_escape_rule(
    command: str,
    context: ToolExecutionContext,
) -> tuple[str, str] | None:
    workspace_dir = (context.workspace_dir or "").strip()
    if not workspace_dir:
        return None
    if not extract_shell_effects(command).write_paths:
        return None
    root = Path(workspace_dir).expanduser()
    for path in _write_target_paths_from_command(command, root):
        if not _path_is_under(path.expanduser(), root):
            return (
                "shell.workspace_escape",
                "命令会写入或修改工作区之外的路径，需要用户确认。",
            )
    return None


def evaluate_shell_command(
    command: str,
    *,
    cwd: str | Path | None = None,
    profile: str | None = None,
    context: ToolExecutionContext | dict[str, Any] | None = None,
) -> ToolPolicyDecision:
    """Evaluate a shell command under a permission profile."""

    active_context = _normalise_context(context, profile=profile)
    if cwd is not None and not active_context.workspace_dir:
        active_context.workspace_dir = str(cwd)
    active_profile = active_context.permission_profile
    permission_mode = active_context.permission_mode or "quiet"
    distribution = active_context.distribution
    full_access = _is_full_access_mode(
        active_profile,
        permission_mode,
        distribution,
    )
    normalized = _normalise_command(command)
    if not normalized:
        return _decision(
            "allow",
            "空命令不会产生系统修改。",
            active_profile,
            distribution=distribution,
            permission_mode=permission_mode,
            command=normalized,
            command_category="empty",
            decision_source="mode",
        )

    forbidden = _match_rule(normalized, _FORBIDDEN_PATTERNS)
    if forbidden:
        rule_id, reason = forbidden
        return _decision(
            "forbidden",
            reason,
            active_profile,
            rule_id,
            risk="critical",
            distribution=distribution,
            permission_mode=permission_mode,
            command=normalized,
            command_category="hard_deny",
            decision_source="builtin_hard_deny",
            effective_rule_id=rule_id,
            hard_denied=True,
        )

    if _writes_sensitive_system_path(normalized):
        return _decision(
            "forbidden",
            "禁止直接写入系统敏感路径。",
            active_profile,
            "shell.system_path_write",
            risk="critical",
            distribution=distribution,
            permission_mode=permission_mode,
            command=normalized,
            command_category="hard_deny",
            decision_source="builtin_hard_deny",
            effective_rule_id="shell.system_path_write",
            hard_denied=True,
        )

    rule_effects = (
        ("deny", "allow") if full_access else ("deny", "allow", "ask")
    )
    for effect in rule_effects:
        matched_rule = _matching_command_rule(
            tool_name="execute_shell_command",
            command=normalized,
            tool_input={"command": normalized, "cwd": str(cwd or "")},
            context=active_context,
            effect=effect,
        )
        if matched_rule is not None:
            return _decision_from_command_rule(
                matched_rule,
                profile=active_profile,
                permission_mode=permission_mode,
                distribution=distribution,
                command=normalized,
                tool_name="execute_shell_command",
            )

    if full_access:
        return _decision(
            "allow",
            "用户已为本次任务选择完全访问；该命令无需重复授权。",
            active_profile,
            distribution=distribution,
            permission_mode=permission_mode,
            command=normalized,
            command_category="full_access",
            decision_source="chat_permission",
        )

    if permission_mode == "plan":
        return _decision(
            "forbidden",
            "Plan 模式不执行 Bash 命令。",
            active_profile,
            "mode.plan_shell_blocked",
            risk="medium",
            distribution=distribution,
            permission_mode=permission_mode,
            command=normalized,
            command_category="mode_block",
            decision_source="mode",
            effective_rule_id="mode.plan",
        )

    workspace_escape = _workspace_escape_rule(normalized, active_context)
    if workspace_escape:
        rule_id, reason = workspace_escape
        return _decision(
            "prompt",
            reason,
            active_profile,
            rule_id,
            risk="high",
            workspace_escape=True,
            distribution=distribution,
            permission_mode=permission_mode,
            command=normalized,
            command_category="workspace_escape",
            decision_source="builtin_prompt",
            effective_rule_id=rule_id,
            path_pattern_suggestion=_path_pattern_suggestion(active_context),
        )

    if permission_mode == "full_auto" and active_profile != "observe":
        return _decision(
            "allow",
            "完全自动模式允许该命令，硬拒绝规则仍会拦截危险操作。",
            active_profile,
            distribution=distribution,
            permission_mode=permission_mode,
            command=normalized,
            command_category="full_auto",
            decision_source="mode",
        )

    prompt = _match_rule(normalized, _PROMPT_PATTERNS)
    if prompt:
        rule_id, reason = prompt
        return _decision(
            "prompt",
            reason,
            active_profile,
            rule_id,
            risk="high",
            distribution=distribution,
            permission_mode=permission_mode,
            command=normalized,
            command_category=rule_id,
            decision_source="builtin_prompt",
            effective_rule_id=rule_id,
        )

    if active_profile == "observe":
        if _is_obviously_read_only(normalized):
            return _decision(
                "allow",
                "观察档位允许明确只读命令。",
                active_profile,
                distribution=distribution,
                permission_mode=permission_mode,
                command=normalized,
                command_category="read_only",
                decision_source="profile",
            )
        return _decision(
            "prompt",
            "观察档位只自动允许读取、搜索和浏览类命令。",
            active_profile,
            "profile.observe_requires_prompt",
            risk="medium",
            distribution=distribution,
            permission_mode=permission_mode,
            command=normalized,
            command_category="profile_prompt",
            decision_source="profile",
            effective_rule_id="profile.observe_requires_prompt",
        )

    head = _command_head(normalized)
    if permission_mode in {"default", "accept_edits"}:
        if _is_obviously_read_only(normalized):
            return _decision(
                "allow",
                "该权限模式允许明确只读命令。",
                active_profile,
                distribution=distribution,
                permission_mode=permission_mode,
                command=normalized,
                command_category="read_only",
                decision_source="mode",
            )
        return _decision(
            "prompt",
            "该权限模式下 Bash 副作用命令需要确认。",
            active_profile,
            f"mode.{permission_mode}_shell_prompt",
            risk="medium",
            distribution=distribution,
            permission_mode=permission_mode,
            command=normalized,
            command_category="mode_prompt",
            decision_source="mode",
            effective_rule_id=f"mode.{permission_mode}",
            rememberable=True,
        )

    if active_profile == "workspace":
        if head in _WORKSPACE_ALLOWED_HEADS:
            return _decision(
                "allow",
                "工作区档位允许该类常规命令。",
                active_profile,
                distribution=distribution,
                permission_mode=permission_mode,
                command=normalized,
                command_category=(
                    "read_only" if _is_obviously_read_only(normalized) else "workspace_safe"
                ),
                decision_source="mode",
            )
        return _decision(
            "prompt",
            "未知 shell 命令需要用户确认后执行。",
            active_profile,
            "profile.workspace_unknown_shell",
            risk="medium",
            distribution=distribution,
            permission_mode=permission_mode,
            command=normalized,
            command_category="unknown_shell",
            decision_source="mode",
            effective_rule_id="profile.workspace_unknown_shell",
        )

    return _decision(
        "allow",
        "完全访问档位允许该命令。",
        active_profile,
        distribution=distribution,
        permission_mode=permission_mode,
        command=normalized,
        command_category="full_profile",
        decision_source="profile",
    )


def evaluate_tool_policy(
    tool_name: str,
    params: Any | None = None,
    *,
    profile: str | None = None,
    context: ToolExecutionContext | dict[str, Any] | None = None,
) -> ToolPolicyDecision:
    """Evaluate a tool call under the built-in permission policy."""

    active_context = _normalise_context(context, profile=profile)
    active_profile = active_context.permission_profile
    permission_mode = active_context.permission_mode or "quiet"
    distribution = active_context.distribution
    full_access = _is_full_access_mode(
        active_profile,
        permission_mode,
        distribution,
    )
    if params is None:
        params = {}
    elif not isinstance(params, dict):
        return _decision(
            "forbidden",
            "工具参数必须是对象；未知参数形态已被拒绝。",
            active_profile,
            "tool.invalid_params",
            risk="critical",
            distribution=distribution,
            permission_mode=permission_mode,
            command_category="invalid_params",
            decision_source="builtin_hard_deny",
            effective_rule_id="tool.invalid_params",
            hard_denied=True,
        )
    if tool_name == "execute_shell_command":
        return evaluate_shell_command(
            str(params.get("command") or ""),
            cwd=params.get("cwd"),
            profile=active_profile,
            context=active_context,
        )
    if tool_name == "execute_python_code" and distribution == "app_store":
        return _decision(
            "forbidden",
            "App Store 版不允许运行模型提供的 Python 代码。",
            active_profile,
            "app_store.code_execution_blocked",
            risk="critical",
            distribution=distribution,
            permission_mode=permission_mode,
            command_category="code_execution",
            decision_source="distribution",
            effective_rule_id="app_store.code_execution_blocked",
            hard_denied=True,
        )

    rule_effects = (
        ("deny", "allow") if full_access else ("deny", "allow", "ask")
    )
    for effect in rule_effects:
        matched_rule = _matching_command_rule(
            tool_name=tool_name,
            command="",
            tool_input=params,
            context=active_context,
            effect=effect,
        )
        if matched_rule is not None:
            return _decision_from_command_rule(
                matched_rule,
                profile=active_profile,
                permission_mode=permission_mode,
                distribution=distribution,
                tool_name=tool_name,
            )

    if tool_name == "run_skill_script":
        if distribution == "app_store":
            return _decision(
                "forbidden",
                "App Store 版不允许运行技能脚本。",
                active_profile,
                "app_store.skill_script_blocked",
                risk="critical",
                distribution=distribution,
                permission_mode=permission_mode,
                command_category="skill_script",
                decision_source="distribution",
                effective_rule_id="app_store.skill_script_blocked",
                hard_denied=True,
            )
        if full_access:
            return _decision(
                "allow",
                "用户已为本次任务选择完全访问；技能脚本无需重复授权。",
                active_profile,
                distribution=distribution,
                permission_mode=permission_mode,
                command_category="skill_script",
                decision_source="chat_permission",
            )
        return _decision(
            "prompt",
            "运行技能自带脚本需要用户确认。",
            active_profile,
            "skill_script.confirmation_required",
            risk="high",
            distribution=distribution,
            permission_mode=permission_mode,
            command_category="skill_script",
            decision_source="builtin_prompt",
            effective_rule_id="skill_script.confirmation_required",
            rememberable=False,
        )

    if tool_name == "execute_python_code":
        if full_access:
            return _decision(
                "allow",
                "用户已为本次任务选择完全访问；代码执行无需重复授权。",
                active_profile,
                distribution=distribution,
                permission_mode=permission_mode,
                command_category="full_access",
                decision_source="chat_permission",
            )
        return _decision(
            "prompt",
            "运行 Python/代码执行工具需要用户确认。",
            active_profile,
            "code_execution.confirmation_required",
            risk="high",
            distribution=distribution,
            permission_mode=permission_mode,
            command_category="code_execution",
            decision_source="builtin_prompt",
            effective_rule_id="code_execution.confirmation_required",
            rememberable=False,
        )

    if permission_mode == "plan":
        if tool_name in {
            "create_plan",
            "revise_current_plan",
            "update_subtask_state",
            "finish_subtask",
            "finish_plan",
        }:
            return _decision(
                "allow",
                "Plan 模式允许更新内部计划状态。",
                active_profile,
                distribution=distribution,
                permission_mode=permission_mode,
                command_category="plan_state_control",
                decision_source="mode",
            )
        if tool_name in _FILE_READ_TOOLS:
            return _decision(
                "allow",
                "Plan 模式允许读取上下文。",
                active_profile,
                distribution=distribution,
                permission_mode=permission_mode,
                command_category="read_only_tool",
                decision_source="mode",
            )
        return _decision(
            "forbidden",
            "Plan 模式不执行会修改环境、联网或运行命令的工具。",
            active_profile,
            "mode.plan_tool_blocked",
            risk="medium",
            distribution=distribution,
            permission_mode=permission_mode,
            command_category="mode_block",
            decision_source="mode",
            effective_rule_id="mode.plan",
        )

    if tool_name in _RESEARCH_WORKSPACE_TOOLS:
        # These executors keep all records and exported files in the Agent's
        # managed state root. Remote inference/search is separately constrained
        # by the versioned project policy; arguments cannot grant new roots.
        return _decision(
            "prompt" if active_profile == "observe" else "allow",
            "在已授权 Agent 工作区内保存研究记录；观察档位须确认。",
            active_profile,
            "profile.observe_research_tool" if active_profile == "observe" else None,
            risk="low",
            distribution=distribution,
            permission_mode=permission_mode,
            command_category="research_workspace",
            decision_source="profile" if active_profile == "observe" else "builtin_allow",
        )

    if tool_name in _ANALYSIS_LOCAL_TOOLS:
        if active_profile == "observe":
            return _decision(
                "prompt",
                "观察档位不自动建立分析快照或计算记录。",
                active_profile,
                "profile.observe_analysis_tool",
                risk="medium",
                distribution=distribution,
                permission_mode=permission_mode,
                command_category="analysis_local",
                decision_source="profile",
                effective_rule_id="profile.observe_analysis_tool",
            )
        return _decision(
            "allow",
            "在已授权工作区内创建会话范围的分析记录。",
            active_profile,
            distribution=distribution,
            permission_mode=permission_mode,
            command_category="analysis_local",
            decision_source="builtin_allow",
        )

    if tool_name in _MAC_USE_TOOLS:
        if distribution == "app_store":
            return _decision(
                "forbidden",
                "App Store 版不提供系统级 Mac 应用操作。",
                active_profile,
                "app_store.mac_use_blocked",
                risk="high",
                distribution=distribution,
                permission_mode=permission_mode,
                command_category="mac_use",
                decision_source="distribution",
                effective_rule_id="app_store.mac_use_blocked",
                hard_denied=True,
            )
        if tool_name in {"mac_inspect", "mac_observe", "mac_verify"}:
            return _decision(
                "allow",
                "只读取用户选定的 Mac 应用目标。",
                active_profile,
                distribution=distribution,
                permission_mode=permission_mode,
                command_category="mac_use_read",
                decision_source="builtin_allow",
            )
        if tool_name == "mac_session":
            return _decision(
                "allow",
                "Mac 应用操作的停止、暂停和恢复不增加桌面写入权限。",
                active_profile,
                distribution=distribution,
                permission_mode=permission_mode,
                command_category="mac_use_session_control",
                decision_source="builtin_allow",
            )
        if full_access:
            return _decision(
                "allow",
                "用户已为本次任务选择完全访问；Mac 应用写操作仍受目标和观察收据约束。",
                active_profile,
                distribution=distribution,
                permission_mode=permission_mode,
                command_category="mac_use_write",
                decision_source="chat_permission",
            )
        return _decision(
            "prompt",
            "操作选定的 Mac 应用可能改变应用或云端状态，需要本次任务授权。",
            active_profile,
            "mode.default_mac_use_prompt",
            risk="high",
            distribution=distribution,
            permission_mode=permission_mode,
            command_category="mac_use_write",
            decision_source="mode",
            effective_rule_id="mode.default_mac_use_prompt",
            rememberable=True,
        )

    if tool_name in _BROWSER_TOOLS:
        action = str(params.get("action") or "").strip().lower()
        if distribution == "app_store":
            return _decision(
                "forbidden",
                "App Store 版不允许 Agent 自动控制浏览器。",
                active_profile,
                "app_store.browser_automation_blocked",
                risk="high",
                distribution=distribution,
                permission_mode=permission_mode,
                command_category="browser_automation",
                decision_source="distribution",
                effective_rule_id="app_store.browser_automation_blocked",
                hard_denied=True,
            )
        if full_access:
            return _decision(
                "allow",
                "用户已为本次任务选择完全访问；浏览器操作无需重复授权。",
                active_profile,
                distribution=distribution,
                permission_mode=permission_mode,
                command_category="full_access",
                decision_source="chat_permission",
            )
        if _browser_tool_is_read_only(tool_name, params):
            return _decision(
                "allow",
                "只读浏览任务中的读取、导航和页面内交互允许静默执行。",
                active_profile,
                distribution=distribution,
                permission_mode=permission_mode,
                command_category="browser_read",
                decision_source="browser_policy",
            )
        if action in _BROWSER_WRITE_ACTIONS or not action:
            return _decision(
                "prompt",
                "浏览器写入、点击、输入、上传、下载或标签状态变更需要用户确认。",
                active_profile,
                "browser.write_confirmation_required",
                risk="high",
                distribution=distribution,
                permission_mode=permission_mode,
                command_category="browser_write",
                decision_source="browser_policy",
                effective_rule_id="browser.write_confirmation_required",
                rememberable=False,
            )
        return _decision(
            "prompt",
            "未知浏览器动作需要用户确认。",
            active_profile,
            "browser.unknown_action",
            risk="high",
            distribution=distribution,
            permission_mode=permission_mode,
            command_category="browser_unknown",
            decision_source="browser_policy",
            effective_rule_id="browser.unknown_action",
            rememberable=False,
        )

    if active_profile == "observe" and (
        tool_name in _FILE_WRITE_TOOLS or tool_name in _FILE_SEND_TOOLS
    ):
        return _decision(
            "prompt",
            "观察档位不自动允许写入或发送文件。",
            active_profile,
            "profile.observe_write_tool",
            risk="medium",
            distribution=distribution,
            permission_mode=permission_mode,
            command_category="file_write",
            decision_source="profile",
            effective_rule_id="profile.observe_write_tool",
        )

    if tool_name in _FILE_SEND_TOOLS:
        if full_access:
            return _decision(
                "allow",
                "用户已为本次任务选择完全访问；文件交付无需重复授权。",
                active_profile,
                distribution=distribution,
                permission_mode=permission_mode,
                command_category="full_access",
                decision_source="chat_permission",
            )
        workspace_escape = _tool_path_workspace_escape(params, active_context)
        if workspace_escape:
            rule_id, reason = workspace_escape
            return _decision(
                "prompt",
                reason,
                active_profile,
                rule_id,
                risk="high",
                workspace_escape=True,
                distribution=distribution,
                permission_mode=permission_mode,
                command_category="workspace_escape",
                decision_source="builtin_prompt",
                effective_rule_id=rule_id,
                path_pattern_suggestion=_path_pattern_suggestion(active_context),
            )
        return _decision(
            "prompt",
            "发送或外传文件需要用户确认。",
            active_profile,
            "file_send.confirmation_required",
            risk="high",
            distribution=distribution,
            permission_mode=permission_mode,
            command_category="file_send",
            decision_source="builtin_prompt",
            effective_rule_id="file_send.confirmation_required",
            rememberable=False,
        )

    if tool_name in _FILE_WRITE_TOOLS:
        if full_access:
            return _decision(
                "allow",
                "用户已为本次任务选择完全访问；文件修改无需重复授权。",
                active_profile,
                distribution=distribution,
                permission_mode=permission_mode,
                command_category="full_access",
                decision_source="chat_permission",
            )
        workspace_escape = _tool_path_workspace_escape(params, active_context)
        if workspace_escape:
            rule_id, reason = workspace_escape
            return _decision(
                "prompt",
                reason,
                active_profile,
                rule_id,
                risk="high",
                workspace_escape=True,
                distribution=distribution,
                permission_mode=permission_mode,
                command_category="workspace_escape",
                decision_source="builtin_prompt",
                effective_rule_id=rule_id,
                path_pattern_suggestion=_path_pattern_suggestion(active_context),
            )
        if permission_mode in {"accept_edits", "quiet", "full_auto"}:
            return _decision(
                "allow",
                "当前权限模式允许工作区内文件编辑。",
                active_profile,
                distribution=distribution,
                permission_mode=permission_mode,
                command_category="file_edit",
                decision_source="mode",
                path_pattern_suggestion=_path_pattern_suggestion(active_context),
            )
        return _decision(
            "prompt",
            "默认模式下文件写入需要确认。",
            active_profile,
            "mode.default_file_write_prompt",
            risk="medium",
            distribution=distribution,
            permission_mode=permission_mode,
            command_category="file_edit",
            decision_source="mode",
            effective_rule_id="mode.default_file_write_prompt",
            path_pattern_suggestion=_path_pattern_suggestion(active_context),
            rememberable=True,
        )

    if tool_name in _READ_ONLY_TOOL_NAMES:
        return _decision(
            "allow",
            "该工具调用属于只读或观察类工具。",
            active_profile,
            distribution=distribution,
            permission_mode=permission_mode,
            command_category="read_only_tool",
            decision_source="builtin_allow",
        )

    if tool_name in _LOW_RISK_INTERNAL_TOOLS:
        return _decision(
            "allow",
            "该内部协调工具不直接修改本地系统。",
            active_profile,
            distribution=distribution,
            permission_mode=permission_mode,
            command_category="internal_coordination",
            decision_source="builtin_allow",
        )

    if tool_name in _PROMPT_ONLY_TOOLS:
        if full_access and tool_name != "clear_analysis_data":
            return _decision(
                "allow",
                "用户已为本次任务选择完全访问；该工具无需重复授权。",
                active_profile,
                distribution=distribution,
                permission_mode=permission_mode,
                command_category="full_access",
                decision_source="chat_permission",
            )
        return _decision(
            "prompt",
            "该工具会修改设置或提交需确认的后台任务，需要用户确认。",
            active_profile,
            f"{tool_name}.confirmation_required",
            risk="high" if tool_name == "clear_analysis_data" else "medium",
            distribution=distribution,
            permission_mode=permission_mode,
            command_category="side_effect_tool",
            decision_source="builtin_prompt",
            effective_rule_id=f"{tool_name}.confirmation_required",
            rememberable=False,
        )

    if full_access:
        if _looks_like_sensitive_alias(tool_name):
            return _decision(
                "forbidden",
                "未知敏感工具别名已被拒绝。",
                active_profile,
                "tool.unknown_sensitive_alias",
                risk="critical",
                distribution=distribution,
                permission_mode=permission_mode,
                command_category="unknown_tool",
                decision_source="builtin_hard_deny",
                effective_rule_id="tool.unknown_sensitive_alias",
                hard_denied=True,
            )
        return _decision(
            "allow",
            "用户已为本次任务选择完全访问；该工具无需重复授权。",
            active_profile,
            distribution=distribution,
            permission_mode=permission_mode,
            command_category="full_access",
            decision_source="chat_permission",
        )

    if _looks_like_sensitive_alias(tool_name):
        return _decision(
            "forbidden",
            "未知敏感工具别名已被拒绝。",
            active_profile,
            "tool.unknown_sensitive_alias",
            risk="critical",
            distribution=distribution,
            permission_mode=permission_mode,
            command_category="unknown_tool",
            decision_source="builtin_hard_deny",
            effective_rule_id="tool.unknown_sensitive_alias",
            hard_denied=True,
        )

    return _decision(
        "prompt",
        "未知工具需要用户确认后执行。",
        active_profile,
        "tool.unknown",
        risk="medium",
        distribution=distribution,
        permission_mode=permission_mode,
        command_category="unknown_tool",
        decision_source="builtin_prompt",
        effective_rule_id="tool.unknown",
        rememberable=False,
    )
