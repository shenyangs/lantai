# -*- coding: utf-8 -*-
"""Built-in Guanlan MCP integration.

Guanlan is exposed as a reserved MCP client so the UI can present it as a
first-class, toggleable capability while the runtime still uses the standard
MCP client manager underneath.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Mapping

from mcp_types.version import LATEST_HANDSHAKE_VERSION, LATEST_MODERN_VERSION
from packaging.version import InvalidVersion, Version

from ...config.config import MCPClientConfig
from ...constant import WORKING_DIR

logger = logging.getLogger(__name__)

GUANLAN_CLIENT_KEY = "guanlan"
GUANLAN_PACKAGE = "guanlan"
GUANLAN_MCP_NAME = "观澜 / Guanlan"
GUANLAN_MCP_DESCRIPTION = (
    "中文互联网搜索增强：搜索、网页阅读、中文热榜、研究证据包；免费、无需 Key。"
)
GUANLAN_REPOSITORY_URL = "https://github.com/shenyangs/GuanLan"
GUANLAN_LICENSE = "MIT License"
GUANLAN_TOOLS = [
    {
        "name": "guanlan_search",
        "description": "中文搜索 — Baidu/Bing/DDG 多后端聚合，信源路由与分类",
    },
    {
        "name": "guanlan_read",
        "description": "网页阅读 — Jina Reader + 直连 HTML 降级链，中文网页质量检测",
    },
    {
        "name": "guanlan_hotnews",
        "description": "中文热榜 — 百度/微博/B站/IT之家/V2EX 多源聚合",
    },
    {
        "name": "guanlan_research",
        "description": "研究证据包 — 自动路由信源、拆分查询、多角色搜索",
    },
]


@dataclass
class GuanlanPackageState:
    """Best-effort package install/update status."""

    status: Literal["idle", "checking", "updating", "ready", "error"] = (
        "idle"
    )
    version: str | None = None
    message: str = ""
    updated_at: float | None = None
    candidate_version: str | None = None
    metadata_source: Literal["pypi", "github_tags", "unavailable"] = "pypi"
    metadata_degraded: bool = False
    update_available: bool = False
    bundled_version: str | None = None
    runtime_source: Literal["bundled", "user"] = "bundled"
    runtime_command: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class GuanlanArgumentRepair:
    """One safe, schema-backed repair applied to a read-only Guanlan call."""

    field: str
    requested: int | float
    applied: int | float
    minimum: int | float | None = None
    maximum: int | float | None = None


def repair_guanlan_tool_arguments(
    tool_name: str,
    arguments: Mapping[str, Any] | None,
    input_schema: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], tuple[GuanlanArgumentRepair, ...]]:
    """Clamp safe numeric arguments to Guanlan's advertised input schema.

    Guanlan's MCP surface is read-only.  A model occasionally misses a
    numeric bound such as ``read_top <= 2``; rejecting the whole call makes a
    harmless shape error look like a permission or tool-budget failure.  Keep
    the server schema authoritative while repairing only top-level numeric
    bounds.  Types, enums, required fields, and nested payloads still fail
    normally so semantic mistakes remain visible.
    """

    repaired = dict(arguments or {})
    schema = input_schema if isinstance(input_schema, Mapping) else {}
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return repaired, ()

    repairs: list[GuanlanArgumentRepair] = []
    for field, field_schema in properties.items():
        if field not in repaired or not isinstance(field_schema, Mapping):
            continue
        if field_schema.get("type") not in {"integer", "number"}:
            continue

        requested = repaired[field]
        if isinstance(requested, bool) or not isinstance(
            requested,
            (int, float),
        ):
            continue

        minimum = field_schema.get("minimum")
        maximum = field_schema.get("maximum")
        applied = requested
        if isinstance(minimum, (int, float)) and applied < minimum:
            applied = minimum
        if isinstance(maximum, (int, float)) and applied > maximum:
            applied = maximum
        if applied == requested:
            continue

        if field_schema.get("type") == "integer":
            applied = int(applied)
        repaired[str(field)] = applied
        repairs.append(
            GuanlanArgumentRepair(
                field=str(field),
                requested=requested,
                applied=applied,
                minimum=minimum if isinstance(minimum, (int, float)) else None,
                maximum=maximum if isinstance(maximum, (int, float)) else None,
            ),
        )

    return repaired, tuple(repairs)


def format_guanlan_argument_repair_notice(
    tool_name: str,
    repairs: tuple[GuanlanArgumentRepair, ...],
) -> str:
    """Return a model-readable explanation without implying a global limit."""

    details = ", ".join(
        f"{repair.field} requested {repair.requested} and was adjusted to "
        f"{repair.applied}"
        for repair in repairs
    )
    return (
        f"[Lantai repaired safe arguments for read-only tool {tool_name}: "
        f"{details}. The call continued using the tool's published input "
        "schema. This is not a permission or tool-call limit.]"
    )


_state = GuanlanPackageState()


@dataclass(frozen=True)
class GuanlanRuntime:
    """One verified Guanlan MCP runtime selected without mutating the host."""

    command: str
    args: tuple[str, ...]
    version: str | None
    source: Literal["bundled", "user"]


_GUANLAN_VERSION_PATTERN = re.compile(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)")
_EXTERNAL_GUANLAN_ENV = "LANTAI_GUANLAN_MCP_COMMAND"
_EXTERNAL_GUANLAN_PROBE_TIMEOUT_SECONDS = 2.0


def is_guanlan_package_update_disabled() -> bool:
    """Runtime updates are never supported for the bundled dependency."""

    return True


def _mark_guanlan_update_disabled() -> GuanlanPackageState:
    runtime = select_guanlan_runtime()
    bundled_version = get_guanlan_package_version()
    _state.version = runtime.version
    _state.bundled_version = bundled_version
    _state.runtime_source = runtime.source
    _state.runtime_command = runtime.command
    _state.updated_at = time.time()
    if runtime.version:
        _state.status = "ready"
        _state.message = (
            "User-installed Guanlan runtime is active"
            if runtime.source == "user"
            else "Built-in Guanlan runtime is ready and updates with Lantai"
        )
    else:
        _state.status = "error"
        _state.message = (
            "Built-in Guanlan runtime is unavailable; update the Lantai app "
            "to repair bundled dependencies"
        )
    return _state


def get_guanlan_package_dir() -> Path:
    """Return the historical package directory without activating it."""

    return (
        Path(WORKING_DIR)
        / "python-packages"
        / f"py{sys.version_info.major}.{sys.version_info.minor}"
        / "guanlan"
    )


def bootstrap_guanlan_import_path() -> Path:
    """Keep compatibility without overriding the bundled import path."""

    return get_guanlan_package_dir()


def _build_guanlan_env() -> dict[str, str]:
    return {}


def _is_frozen_runtime() -> bool:
    return bool(getattr(sys, "frozen", False))


def _command_supports_python_code(command: str) -> bool:
    name = Path(command).name.lower()
    return name.startswith("python") or name in {"python.exe", "pythonw.exe"}


def _guanlan_stdio_command() -> tuple[str, list[str]]:
    if _is_frozen_runtime() or not _command_supports_python_code(
        sys.executable,
    ):
        return sys.executable, ["guanlan-mcp"]

    return (
        sys.executable,
        [
            "-c",
            "from lantai.app.mcp.guanlan import run_guanlan_mcp; "
            "run_guanlan_mcp()",
        ],
    )


def _bundled_guanlan_runtime() -> GuanlanRuntime:
    command, args = _guanlan_stdio_command()
    return GuanlanRuntime(
        command=command,
        args=tuple(args),
        version=get_guanlan_package_version(),
        source="bundled",
    )


def _external_guanlan_mcp_candidates() -> tuple[Path, ...]:
    """Return GUI-safe, user-controlled MCP entry points in priority order."""

    raw_candidates: list[Path] = []
    explicit = os.environ.get(_EXTERNAL_GUANLAN_ENV, "").strip()
    if explicit:
        raw_candidates.append(Path(explicit).expanduser())

    home = Path.home()
    raw_candidates.extend(
        [
            home / ".local" / "bin" / "guanlan-mcp",
            Path("/opt/homebrew/bin/guanlan-mcp"),
            Path("/usr/local/bin/guanlan-mcp"),
        ],
    )
    discovered = shutil.which("guanlan-mcp")
    if discovered:
        raw_candidates.append(Path(discovered))

    candidates: list[Path] = []
    seen: set[str] = set()
    for candidate in raw_candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        key = str(resolved)
        if key in seen or not resolved.is_file() or not os.access(resolved, os.X_OK):
            continue
        seen.add(key)
        candidates.append(candidate.absolute())
    return tuple(candidates)


def _probe_external_guanlan_runtime(command: Path) -> GuanlanRuntime | None:
    """Verify an external entry point with a real MCP initialize handshake."""

    cli = command.with_name("guanlan")
    if not cli.is_file() or not os.access(cli, os.X_OK):
        return None
    try:
        version_probe = subprocess.run(
            [str(cli), "--version"],
            capture_output=True,
            check=False,
            text=True,
            timeout=_EXTERNAL_GUANLAN_PROBE_TIMEOUT_SECONDS,
        )
        match = _GUANLAN_VERSION_PATTERN.search(
            f"{version_probe.stdout}\n{version_probe.stderr}",
        )
        if version_probe.returncode != 0 or not match:
            return None
        version = str(Version(match.group(1)))
        client_info = {
            "name": "lantai-runtime-probe",
            "version": "1",
        }
        probe_requests = (
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "server/discover",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": (
                            LATEST_MODERN_VERSION
                        ),
                        "io.modelcontextprotocol/clientInfo": client_info,
                        "io.modelcontextprotocol/clientCapabilities": {},
                    }
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "initialize",
                "params": {
                    "protocolVersion": LATEST_HANDSHAKE_VERSION,
                    "capabilities": {},
                    "clientInfo": client_info,
                },
            },
        )
        initialize_probe = subprocess.run(
            [str(command)],
            capture_output=True,
            check=False,
            input="".join(
                f"{json.dumps(request)}\n" for request in probe_requests
            ),
            text=True,
            timeout=_EXTERNAL_GUANLAN_PROBE_TIMEOUT_SECONDS,
        )
        initialized = False
        for line in initialize_probe.stdout.splitlines():
            try:
                payload = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if (
                isinstance(payload, dict)
                and payload.get("id") in {1, 2}
                and isinstance(payload.get("result"), dict)
                and isinstance(payload["result"].get("serverInfo"), dict)
            ):
                initialized = True
                break
        if not initialized:
            return None
    except (OSError, subprocess.SubprocessError, InvalidVersion):
        logger.debug("Failed to probe external Guanlan runtime", exc_info=True)
        return None
    return GuanlanRuntime(
        command=str(command),
        args=(),
        version=version,
        source="user",
    )


@lru_cache(maxsize=1)
def select_guanlan_runtime() -> GuanlanRuntime:
    """Prefer a compatible user-installed Guanlan only when it is newer.

    Discovery is read-only and bounded.  Lantai never installs, upgrades, or
    rewrites PATH at runtime; the bundled dependency remains the deterministic
    fallback when an external entry point is absent, older, or cannot start.
    """

    bundled = _bundled_guanlan_runtime()
    try:
        bundled_version = Version(bundled.version or "0")
    except InvalidVersion:
        bundled_version = Version("0")
    verified = filter(
        None,
        (
            _probe_external_guanlan_runtime(candidate)
            for candidate in _external_guanlan_mcp_candidates()
        ),
    )
    external = max(
        verified,
        key=lambda runtime: Version(runtime.version or "0"),
        default=None,
    )
    if external and Version(external.version or "0") > bundled_version:
        return external
    return bundled


def build_guanlan_client_config(enabled: bool = True) -> MCPClientConfig:
    """Return the reserved MCP client config for Guanlan.

    In a PyInstaller desktop build ``sys.executable`` is the ``lantai-server``
    launcher rather than a normal Python binary, so packaged builds use the
    dedicated ``guanlan-mcp`` CLI command.
    """

    runtime = select_guanlan_runtime()
    return MCPClientConfig(
        name=GUANLAN_MCP_NAME,
        description=GUANLAN_MCP_DESCRIPTION,
        enabled=enabled,
        transport="stdio",
        command=runtime.command,
        args=list(runtime.args),
        env=_build_guanlan_env(),
        cwd="",
    )


def get_guanlan_package_version() -> str | None:
    """Return the installed Guanlan package version for this interpreter."""

    try:
        return importlib.metadata.version(GUANLAN_PACKAGE)
    except importlib.metadata.PackageNotFoundError:
        return None
    except Exception:
        logger.debug("Failed to read Guanlan package version", exc_info=True)
        return None


def get_guanlan_package_state() -> GuanlanPackageState:
    """Return current package status, refreshing installed version cheaply."""

    return _mark_guanlan_update_disabled()


async def ensure_guanlan_package(timeout: int = 300) -> GuanlanPackageState:
    """Report current Guanlan availability without doing a network update."""

    return _mark_guanlan_update_disabled()


def ensure_guanlan_default(config):
    """Return an MCPConfig with Guanlan present and normalized.

    Missing Guanlan means "default on"; an explicit disabled config means the
    user turned it off and is preserved.
    """

    from ...config.config import MCPConfig

    if config is None:
        config = MCPConfig(clients={})
    else:
        config = config.model_copy(deep=True)

    existing = (config.clients or {}).get(GUANLAN_CLIENT_KEY)
    enabled = True if existing is None else existing.enabled
    config.clients[GUANLAN_CLIENT_KEY] = build_guanlan_client_config(
        enabled=enabled,
    )
    return config


def _raw_config_has_enabled_guanlan(data: object) -> bool:
    if not isinstance(data, dict):
        return False
    clients = data.get("mcp", {})
    if isinstance(clients, dict):
        clients = clients.get("clients", {})
    if not isinstance(clients, dict):
        return False
    guanlan = clients.get(GUANLAN_CLIENT_KEY)
    if guanlan is None:
        return True
    return isinstance(guanlan, dict) and bool(guanlan.get("enabled"))


def should_prepare_guanlan_for_startup() -> bool:
    """Runtime package preparation is disabled for bundled Guanlan."""

    return False


def run_guanlan_mcp() -> None:
    """Entry point used by the packaged ``lantai-server guanlan-mcp``."""

    bootstrap_guanlan_import_path()
    try:
        import guanlan.integrations.mcp_server as guanlan_server
    except Exception as exc:
        print(f"Failed to import Guanlan MCP server: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    # Guanlan currently exposes an SDK v1 decorator Server interface. Lantai
    # owns a narrow v2 host wrapper so the bundled runtime shares our MCP SDK.
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import (
        CallToolResult,
        ListToolsResult,
        TextContent,
        Tool,
        ToolAnnotations,
    )
    from jsonschema.validators import validator_for

    required_upstream_symbols = (
        "_tool_definitions",
        "_run_tool",
        "_as_text",
        "_doctor_report",
        "format_user_error",
    )
    missing = [
        symbol
        for symbol in required_upstream_symbols
        if not callable(getattr(guanlan_server, symbol, None))
    ]
    if missing:
        print(
            "Guanlan MCP compatibility wrapper is out of date; missing: "
            + ", ".join(missing),
            file=sys.stderr,
        )
        raise SystemExit(1)

    definitions = {
        str(item.get("name") or ""): dict(item)
        for item in guanlan_server._tool_definitions()
        if str(item.get("name") or "")
    }
    validators = {}
    for name, definition in definitions.items():
        schema = definition.get("inputSchema") or {
            "type": "object",
            "properties": {},
        }
        validator_class = validator_for(schema)
        validator_class.check_schema(schema)
        validators[name] = validator_class(schema)

    async def list_tools(_context, _params):
        tools = []
        for definition in definitions.values():
            payload = dict(definition)
            payload["annotations"] = ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
            )
            tools.append(Tool(**payload))
        return ListToolsResult(tools=tools)

    async def call_tool(_context, params):
        try:
            target = "guanlan_status" if params.name == "get_status" else params.name
            if target not in definitions:
                raise ValueError(f"Unknown tool: {params.name}")
            arguments = dict(params.arguments or {})
            validators[target].validate(arguments)
            result = (
                guanlan_server._doctor_report()
                if params.name == "get_status"
                else guanlan_server._run_tool(
                    target,
                    arguments,
                )
            )
            text = guanlan_server._as_text(result)
            return CallToolResult(
                content=[TextContent(type="text", text=text)],
                structuredContent=result if isinstance(result, (dict, list)) else None,
            )
        except Exception as exc:  # Guanlan owns the user-safe formatter.
            formatted = guanlan_server.format_user_error(exc)
            return CallToolResult(
                content=[TextContent(type="text", text=f"Error: {formatted}")],
                isError=True,
            )

    async def main() -> None:
        server = Server(
            "guanlan",
            version=get_guanlan_package_version() or "0",
            description=GUANLAN_MCP_DESCRIPTION,
            on_list_tools=list_tools,
            on_call_tool=call_tool,
        )
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    asyncio.run(main())
