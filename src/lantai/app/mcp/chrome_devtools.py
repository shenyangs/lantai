# -*- coding: utf-8 -*-
"""Bundled Chrome DevTools MCP runtime and product policy metadata.

The desktop app executes the pinned upstream package with Electron's embedded
Node runtime.  No ``npx`` lookup, package installation, update check, or user
Chrome profile attachment is part of this integration.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Literal

from ...config.config import MCPClientConfig, MCPConfig
from ...constant import WORKING_DIR
from ...security.storage_keys import safe_storage_key


CHROME_DEVTOOLS_CLIENT_KEY = "chrome-devtools"
CHROME_DEVTOOLS_MCP_NAME = "Chrome DevTools"
CHROME_DEVTOOLS_MCP_DESCRIPTION = (
    "使用 Chrome 官方 DevTools MCP 检查页面控制台、网络、性能、"
    "Lighthouse、内存、扩展、PWA 与设备模拟；启用后开放完整工具面。"
)
CHROME_DEVTOOLS_PACKAGE = "chrome-devtools-mcp"
CHROME_DEVTOOLS_PACKAGE_VERSION = "1.8.0"
CHROME_DEVTOOLS_REPOSITORY_URL = "https://github.com/ChromeDevTools/chrome-devtools-mcp"
CHROME_DEVTOOLS_DOCS_URL = CHROME_DEVTOOLS_REPOSITORY_URL
CHROME_DEVTOOLS_LICENSE = "Apache-2.0"
CHROME_DEVTOOLS_INTEGRATION_POLICY_VERSION = "lantai-chrome-devtools/1"

# The full upstream 1.8.0 surface is available after the user explicitly turns
# the module on. The Lantai host still owns path grants and approval decisions;
# an upstream tool name or annotation is never treated as authorization.
CHROME_DEVTOOLS_ALLOWED_TOOLS: tuple[str, ...] = (
    "click",
    "click_at",
    "close_heapsnapshot",
    "close_page",
    "compare_heapsnapshots",
    "drag",
    "emulate",
    "evaluate_script",
    "execute_3p_developer_tool",
    "execute_webmcp_tool",
    "fill",
    "fill_form",
    "get_console_message",
    "get_heapsnapshot_class_nodes",
    "get_heapsnapshot_details",
    "get_heapsnapshot_dominators",
    "get_heapsnapshot_duplicate_strings",
    "get_heapsnapshot_edges",
    "get_heapsnapshot_object_details",
    "get_heapsnapshot_retainers",
    "get_heapsnapshot_retaining_paths",
    "get_heapsnapshot_summary",
    "get_network_request",
    "get_os_app_state",
    "handle_dialog",
    "hover",
    "install_extension",
    "install_pwa",
    "launch_pwa",
    "lighthouse_audit",
    "list_3p_developer_tools",
    "list_console_messages",
    "list_extensions",
    "list_network_requests",
    "list_pages",
    "list_webmcp_tools",
    "navigate_page",
    "new_page",
    "performance_analyze_insight",
    "performance_start_trace",
    "performance_stop_trace",
    "press_key",
    "query_heapsnapshot_objects",
    "reload_extension",
    "resize_page",
    "select_page",
    "take_heapsnapshot",
    "take_screenshot",
    "take_snapshot",
    "trigger_extension_action",
    "type_text",
    "uninstall_extension",
    "uninstall_pwa",
    "upload_file",
    "wait_for",
)
CHROME_DEVTOOLS_SCREENCAST_TOOLS: tuple[str, ...] = (
    "screencast_start",
    "screencast_stop",
)

CHROME_DEVTOOLS_OBSERVE_TOOLS: frozenset[str] = frozenset(
    {
        "get_console_message",
        "compare_heapsnapshots",
        "get_heapsnapshot_class_nodes",
        "get_heapsnapshot_details",
        "get_heapsnapshot_dominators",
        "get_heapsnapshot_duplicate_strings",
        "get_heapsnapshot_edges",
        "get_heapsnapshot_object_details",
        "get_heapsnapshot_retainers",
        "get_heapsnapshot_retaining_paths",
        "get_heapsnapshot_summary",
        "get_network_request",
        "get_os_app_state",
        "lighthouse_audit",
        "list_3p_developer_tools",
        "list_console_messages",
        "list_extensions",
        "list_network_requests",
        "list_pages",
        "list_webmcp_tools",
        "performance_analyze_insight",
        "query_heapsnapshot_objects",
        "take_screenshot",
        "take_snapshot",
        "wait_for",
    }
)
CHROME_DEVTOOLS_LOCAL_STATE_TOOLS: frozenset[str] = frozenset(
    {
        "close_page",
        "close_heapsnapshot",
        "emulate",
        "performance_start_trace",
        "performance_stop_trace",
        "resize_page",
        "screencast_start",
        "screencast_stop",
        "select_page",
        "take_heapsnapshot",
    }
)
CHROME_DEVTOOLS_NAVIGATION_TOOLS: frozenset[str] = frozenset(
    {"launch_pwa", "navigate_page", "new_page"}
)
CHROME_DEVTOOLS_INTERACTIVE_TOOLS: frozenset[str] = frozenset(
    {
        "click",
        "click_at",
        "drag",
        "execute_3p_developer_tool",
        "execute_webmcp_tool",
        "fill",
        "fill_form",
        "handle_dialog",
        "hover",
        "install_extension",
        "install_pwa",
        "press_key",
        "reload_extension",
        "type_text",
        "trigger_extension_action",
        "uninstall_extension",
        "uninstall_pwa",
    }
)
CHROME_DEVTOOLS_SENSITIVE_TOOLS: frozenset[str] = frozenset(
    {"evaluate_script", "upload_file"}
)

CHROME_DEVTOOLS_PATH_FIELDS: dict[str, dict[str, str]] = {
    "close_heapsnapshot": {"filePath": "read_file"},
    "compare_heapsnapshots": {
        "baseFilePath": "read_file",
        "currentFilePath": "read_file",
    },
    "evaluate_script": {"filePath": "write_file"},
    "get_heapsnapshot_class_nodes": {"filePath": "read_file"},
    "get_heapsnapshot_details": {"filePath": "read_file"},
    "get_heapsnapshot_dominators": {"filePath": "read_file"},
    "get_heapsnapshot_duplicate_strings": {"filePath": "read_file"},
    "get_heapsnapshot_edges": {"filePath": "read_file"},
    "get_heapsnapshot_object_details": {"filePath": "read_file"},
    "get_heapsnapshot_retainers": {"filePath": "read_file"},
    "get_heapsnapshot_retaining_paths": {"filePath": "read_file"},
    "get_heapsnapshot_summary": {"filePath": "read_file"},
    "get_network_request": {
        "requestFilePath": "write_file",
        "responseFilePath": "write_file",
    },
    "install_extension": {"path": "read_dir"},
    "lighthouse_audit": {"outputDirPath": "write_dir"},
    "performance_start_trace": {"filePath": "write_file"},
    "performance_stop_trace": {"filePath": "write_file"},
    "query_heapsnapshot_objects": {"filePath": "read_file"},
    "screencast_start": {"filePath": "write_file"},
    "take_heapsnapshot": {"filePath": "write_file"},
    "take_screenshot": {"filePath": "write_file"},
    "take_snapshot": {"filePath": "write_file"},
    "upload_file": {"filePaths": "upload_files"},
}


@dataclass(frozen=True, slots=True)
class ChromeDevToolsToolEffect:
    tool_name: str
    category: Literal[
        "observe",
        "local_state",
        "navigation",
        "interactive",
        "sensitive",
        "unknown",
    ]
    read_only: bool
    destructive: bool
    external_side_effect: bool
    approval: Literal["auto", "medium", "high", "critical", "forbidden"]
    idempotency: Literal[
        "read_only",
        "natural_key",
        "not_idempotent",
    ]
    probe: Literal["not_required", "tool_specific", "browser_observation", "manual"]
    compensation: Literal["not_required", "manual_or_domain_specific", "none"]
    on_ambiguity: Literal["safe_to_retry", "probe_required", "manual_disambiguation"]
    populated_path_fields: tuple[str, ...] = ()


def is_official_chrome_devtools_metadata(metadata: dict[str, object]) -> bool:
    """Recognize only host-authored, versioned integration metadata."""

    return (
        metadata.get("official_integration") == "chrome-devtools"
        and metadata.get("integration_policy_version")
        == CHROME_DEVTOOLS_INTEGRATION_POLICY_VERSION
    )


def chrome_devtools_tool_effect(
    tool_name: str,
    arguments: dict[str, object] | None = None,
) -> ChromeDevToolsToolEffect:
    """Return the exact host effect contract for one pinned upstream tool."""

    name = str(tool_name or "").strip()
    args = arguments or {}
    path_modes = CHROME_DEVTOOLS_PATH_FIELDS.get(name, {})
    populated_paths = tuple(
        field for field in path_modes if args.get(field) not in (None, "", [])
    )
    writes_file = any(
        path_modes[field].startswith("write_") for field in populated_paths
    )

    if name in CHROME_DEVTOOLS_OBSERVE_TOOLS:
        if name == "lighthouse_audit":
            return ChromeDevToolsToolEffect(
                name,
                "observe",
                read_only=False,
                destructive=False,
                external_side_effect=True,
                approval="auto",
                idempotency="not_idempotent",
                probe="browser_observation",
                compensation="none",
                on_ambiguity="probe_required",
                populated_path_fields=populated_paths,
            )
        if writes_file:
            return ChromeDevToolsToolEffect(
                name,
                "observe",
                read_only=False,
                destructive=False,
                external_side_effect=False,
                approval="auto",
                idempotency="natural_key",
                probe="tool_specific",
                compensation="not_required",
                on_ambiguity="probe_required",
                populated_path_fields=populated_paths,
            )
        return ChromeDevToolsToolEffect(
            name,
            "observe",
            read_only=True,
            destructive=False,
            external_side_effect=False,
            approval="auto",
            idempotency="read_only",
            probe="not_required",
            compensation="not_required",
            on_ambiguity="safe_to_retry",
            populated_path_fields=populated_paths,
        )

    if name in CHROME_DEVTOOLS_LOCAL_STATE_TOOLS:
        naturally_idempotent = name in {"emulate", "resize_page", "select_page"}
        performance_trace = name in {
            "performance_start_trace",
            "performance_stop_trace",
        }
        return ChromeDevToolsToolEffect(
            name,
            "local_state",
            read_only=False,
            destructive=False,
            external_side_effect=performance_trace,
            approval="auto",
            idempotency="natural_key" if naturally_idempotent else "not_idempotent",
            probe=("browser_observation" if not writes_file else "tool_specific"),
            compensation="none" if performance_trace else "not_required",
            on_ambiguity=(
                "safe_to_retry" if naturally_idempotent else "probe_required"
            ),
            populated_path_fields=populated_paths,
        )

    if name in CHROME_DEVTOOLS_NAVIGATION_TOOLS:
        return ChromeDevToolsToolEffect(
            name,
            "navigation",
            read_only=False,
            destructive=False,
            external_side_effect=True,
            approval="medium" if name == "launch_pwa" else "auto",
            idempotency="not_idempotent",
            probe="browser_observation",
            compensation="none",
            on_ambiguity="probe_required",
        )

    if name in CHROME_DEVTOOLS_INTERACTIVE_TOOLS:
        critical = name in {
            "execute_3p_developer_tool",
            "execute_webmcp_tool",
            "trigger_extension_action",
        }
        high = name in {
            "click_at",
            "handle_dialog",
            "install_extension",
            "install_pwa",
            "reload_extension",
            "uninstall_extension",
            "uninstall_pwa",
        }
        probe = (
            "tool_specific"
            if name
            in {
                "install_extension",
                "install_pwa",
                "reload_extension",
                "uninstall_extension",
                "uninstall_pwa",
            }
            else "manual"
            if critical
            else "browser_observation"
        )
        return ChromeDevToolsToolEffect(
            name,
            "interactive",
            read_only=False,
            destructive=True,
            external_side_effect=True,
            approval="critical" if critical else "high" if high else "medium",
            idempotency="not_idempotent",
            probe=probe,
            compensation="manual_or_domain_specific",
            on_ambiguity=("manual_disambiguation" if critical else "probe_required"),
            populated_path_fields=populated_paths,
        )

    if name in CHROME_DEVTOOLS_SENSITIVE_TOOLS:
        return ChromeDevToolsToolEffect(
            name,
            "sensitive",
            read_only=False,
            destructive=True,
            external_side_effect=True,
            approval="critical",
            idempotency="not_idempotent",
            probe="manual",
            compensation="manual_or_domain_specific",
            on_ambiguity="manual_disambiguation",
            populated_path_fields=populated_paths,
        )

    return ChromeDevToolsToolEffect(
        name,
        "unknown",
        read_only=False,
        destructive=True,
        external_side_effect=True,
        approval="forbidden",
        idempotency="not_idempotent",
        probe="manual",
        compensation="none",
        on_ambiguity="manual_disambiguation",
        populated_path_fields=populated_paths,
    )


CHROME_DEVTOOLS_SERVER_ARGS: tuple[str, ...] = (
    "--page-id-routing",
    "--memory-debugging",
    "--category-extensions",
    "--category-pwa",
    "--category-experimental-third-party",
    "--category-experimental-webmcp",
    "--experimental-devtools",
    "--experimental-vision",
    "--experimental-structured-content",
    "--experimental-include-all-pages",
    "--allow-unrestricted-paths",
    "--chrome-arg=--enable-features=WebMCP",
    "--no-usage-statistics",
)
CHROME_DEVTOOLS_SERVER_ENV: dict[str, str] = {
    "ELECTRON_RUN_AS_NODE": "1",
    "CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS": "1",
    "CHROME_DEVTOOLS_MCP_NO_UPDATE_CHECKS": "1",
}


ChromeDevToolsRuntimeSource = Literal["bundled", "source", "missing"]


@dataclass(frozen=True, slots=True)
class ChromeDevToolsRuntime:
    available: bool
    source: ChromeDevToolsRuntimeSource
    command: str = ""
    entrypoint: str = ""
    package_version: str | None = None
    ffmpeg_path: str | None = None
    message: str = ""


def _source_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _candidate_modules_roots() -> tuple[tuple[Path, ChromeDevToolsRuntimeSource], ...]:
    candidates: list[tuple[Path, ChromeDevToolsRuntimeSource]] = []
    source_modules = _source_root() / "console" / "node_modules"
    configured = str(os.getenv("PRESENTATION_NODE_MODULES") or "").strip()
    if configured:
        configured_path = Path(configured).expanduser()
        configured_source: ChromeDevToolsRuntimeSource = (
            "source"
            if configured_path.resolve(strict=False)
            == source_modules.resolve(strict=False)
            else "bundled"
        )
        candidates.append((configured_path, configured_source))
    candidates.append((source_modules, "source"))

    unique: list[tuple[Path, ChromeDevToolsRuntimeSource]] = []
    seen: set[str] = set()
    for path, source in candidates:
        key = str(path.resolve(strict=False))
        if key not in seen:
            seen.add(key)
            unique.append((path, source))
    return tuple(unique)


def _electron_executable(modules_root: Path) -> Path | None:
    configured = str(os.getenv("PRESENTATION_NODE_EXECUTABLE") or "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()

    electron_root = modules_root / "electron"
    path_file = electron_root / "path.txt"
    try:
        relative = path_file.read_text(encoding="utf-8").strip()
    except OSError:
        relative = ""
    if relative:
        candidate = electron_root / "dist" / relative
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    return None


def _package_version(package_json: Path) -> str | None:
    try:
        payload = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    version = str(payload.get("version") or "").strip()
    return version or None


def _ffmpeg_path() -> str | None:
    candidates = [
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/usr/bin/ffmpeg",
    ]
    for raw in candidates:
        if not raw:
            continue
        candidate = Path(raw).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    return None


def chrome_devtools_allowed_tools(
    runtime: ChromeDevToolsRuntime,
) -> tuple[str, ...]:
    return (
        (*CHROME_DEVTOOLS_ALLOWED_TOOLS, *CHROME_DEVTOOLS_SCREENCAST_TOOLS)
        if runtime.ffmpeg_path
        else CHROME_DEVTOOLS_ALLOWED_TOOLS
    )


def chrome_devtools_server_args(
    runtime: ChromeDevToolsRuntime,
    *,
    agent_id: str,
) -> tuple[str, ...]:
    profile_key = safe_storage_key(
        "chrome-devtools-agent",
        agent_id or "default",
    )
    profile_dir = WORKING_DIR / "chrome-devtools" / profile_key / "profile"
    args = [
        *CHROME_DEVTOOLS_SERVER_ARGS,
        f"--user-data-dir={profile_dir}",
    ]
    if runtime.ffmpeg_path:
        args.extend(
            (
                "--experimental-screencast",
                f"--experimental-ffmpeg-path={runtime.ffmpeg_path}",
            ),
        )
    return tuple(args)


def resolve_chrome_devtools_runtime() -> ChromeDevToolsRuntime:
    """Resolve only the pinned app-owned package and Node runtime."""

    version_mismatches: list[str] = []
    for modules_root, source in _candidate_modules_roots():
        package_root = modules_root / CHROME_DEVTOOLS_PACKAGE
        package_json = package_root / "package.json"
        entrypoint = package_root / "build" / "src" / "bin" / ("chrome-devtools-mcp.js")
        version = _package_version(package_json)
        if version and version != CHROME_DEVTOOLS_PACKAGE_VERSION:
            version_mismatches.append(version)
            continue
        if version != CHROME_DEVTOOLS_PACKAGE_VERSION or not entrypoint.is_file():
            continue
        command = _electron_executable(modules_root)
        if command is None:
            continue
        return ChromeDevToolsRuntime(
            available=True,
            source=source,
            command=str(command),
            entrypoint=str(entrypoint.resolve()),
            package_version=version,
            ffmpeg_path=_ffmpeg_path(),
            message=(
                f"Chrome DevTools MCP {version} 已随澜台就绪；"
                "启用后使用独立持久 Chrome 资料并开放完整调试能力。"
            ),
        )

    mismatch = ", ".join(sorted(set(version_mismatches)))
    return ChromeDevToolsRuntime(
        available=False,
        source="missing",
        package_version=mismatch or None,
        message=(
            "检测到不匹配的 Chrome DevTools MCP 版本；请更新澜台。"
            if mismatch
            else "当前安装包缺少 Chrome DevTools MCP 运行时；请更新澜台。"
        ),
    )


def build_chrome_devtools_client_config(
    *,
    enabled: bool = False,
    agent_id: str = "default",
) -> MCPClientConfig:
    """Build the reserved, full-capability stdio MCP client config."""

    runtime = resolve_chrome_devtools_runtime()
    return MCPClientConfig(
        name=CHROME_DEVTOOLS_MCP_NAME,
        description=CHROME_DEVTOOLS_MCP_DESCRIPTION,
        enabled=bool(enabled and runtime.available),
        transport="stdio",
        command=runtime.command or "chrome-devtools-mcp-unavailable",
        args=(
            [
                runtime.entrypoint,
                *chrome_devtools_server_args(runtime, agent_id=agent_id),
            ]
            if runtime.entrypoint
            else []
        ),
        env=dict(CHROME_DEVTOOLS_SERVER_ENV),
        cwd="",
        allowed_tools=list(chrome_devtools_allowed_tools(runtime)),
    )


def ensure_chrome_devtools_default(
    config: MCPConfig | None,
    *,
    agent_id: str = "default",
) -> MCPConfig:
    """Add the bundled module disabled by default and preserve user choice."""

    merged = MCPConfig(clients={}) if config is None else config.model_copy(deep=True)
    existing = (merged.clients or {}).get(CHROME_DEVTOOLS_CLIENT_KEY)
    canonical = build_chrome_devtools_client_config(
        enabled=bool(existing.enabled) if existing is not None else False,
        agent_id=agent_id,
    )
    # When explicitly enabled, the native module must win generic MCP tool-name
    # collisions. Disabled clients expose no tools, so the same deterministic
    # ordering is harmless while keeping reload behavior stable.
    merged.clients = {
        CHROME_DEVTOOLS_CLIENT_KEY: canonical,
        **{
            key: value
            for key, value in merged.clients.items()
            if key != CHROME_DEVTOOLS_CLIENT_KEY
        },
    }
    return merged


def is_official_chrome_devtools_client(client: object) -> bool:
    return (
        str(getattr(client, "_lantai_client_key", "") or "").strip()
        == CHROME_DEVTOOLS_CLIENT_KEY
    )


__all__ = [
    "CHROME_DEVTOOLS_ALLOWED_TOOLS",
    "CHROME_DEVTOOLS_CLIENT_KEY",
    "CHROME_DEVTOOLS_DOCS_URL",
    "CHROME_DEVTOOLS_INTEGRATION_POLICY_VERSION",
    "CHROME_DEVTOOLS_INTERACTIVE_TOOLS",
    "CHROME_DEVTOOLS_LICENSE",
    "CHROME_DEVTOOLS_LOCAL_STATE_TOOLS",
    "CHROME_DEVTOOLS_MCP_DESCRIPTION",
    "CHROME_DEVTOOLS_MCP_NAME",
    "CHROME_DEVTOOLS_NAVIGATION_TOOLS",
    "CHROME_DEVTOOLS_OBSERVE_TOOLS",
    "CHROME_DEVTOOLS_PACKAGE_VERSION",
    "CHROME_DEVTOOLS_PATH_FIELDS",
    "CHROME_DEVTOOLS_REPOSITORY_URL",
    "CHROME_DEVTOOLS_SCREENCAST_TOOLS",
    "CHROME_DEVTOOLS_SERVER_ARGS",
    "CHROME_DEVTOOLS_SERVER_ENV",
    "CHROME_DEVTOOLS_SENSITIVE_TOOLS",
    "ChromeDevToolsRuntime",
    "ChromeDevToolsToolEffect",
    "build_chrome_devtools_client_config",
    "chrome_devtools_allowed_tools",
    "chrome_devtools_server_args",
    "chrome_devtools_tool_effect",
    "ensure_chrome_devtools_default",
    "is_official_chrome_devtools_client",
    "is_official_chrome_devtools_metadata",
    "resolve_chrome_devtools_runtime",
]
