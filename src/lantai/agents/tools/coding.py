# -*- coding: utf-8 -*-
"""Optional Coding Mode tools.

These tools are registered only for explicit Coding Mode turns, and only when
their local dependencies are already present.  They are deliberately read-only:
AST search shells out to ast-grep, while LSP diagnostics starts a language
server over stdio and opens a file without writing to the workspace.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ...config.context import get_current_workspace_dir
from ...constant import WORKING_DIR
from ...runtime.tool_response import TextBlock, ToolResponse
from ..coding_mode import is_coding_mode_enabled


AST_GREP_BINARIES = ("ast-grep", "sg")
MAX_CODING_TOOL_OUTPUT_CHARS = 50_000
MAX_LSP_FILE_BYTES = 2 * 1024 * 1024
DEFAULT_AST_TIMEOUT_SECONDS = 20.0
DEFAULT_LSP_TIMEOUT_SECONDS = 4.0

_APP_STORE_DISTRIBUTIONS = {"app_store", "app-store", "appstore", "mas", "testflight"}
_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "release",
    "electron-dist",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
_SENSITIVE_FILE_PATTERNS = {
    ".env",
    ".env.*",
    "*.key",
    "*.pem",
    "*.p12",
    "*.pfx",
    "id_rsa",
    "id_ed25519",
}


@dataclass(frozen=True)
class CodingToolCapability:
    available: bool
    command: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class _LanguageServer:
    name: str
    binary: str
    args: tuple[str, ...]
    extensions: frozenset[str]
    language_id: str


_LSP_SERVERS = (
    _LanguageServer(
        name="pyright",
        binary="pyright-langserver",
        args=("--stdio",),
        extensions=frozenset({".py"}),
        language_id="python",
    ),
    _LanguageServer(
        name="pylsp",
        binary="pylsp",
        args=(),
        extensions=frozenset({".py"}),
        language_id="python",
    ),
    _LanguageServer(
        name="typescript",
        binary="typescript-language-server",
        args=("--stdio",),
        extensions=frozenset({".ts", ".tsx", ".js", ".jsx"}),
        language_id="typescript",
    ),
)


def _text_response(text: str) -> ToolResponse:
    return ToolResponse(content=[TextBlock(type="text", text=text)])


def _normalise_distribution(distribution: str | None = None) -> str:
    raw = (
        distribution
        or os.environ.get("LANTAI_DISTRIBUTION_CHANNEL")
        or os.environ.get("LANTAI_RELEASE_CHANNEL")
        or os.environ.get("LANTAI_DISTRIBUTION")
        or ""
    )
    normalized = str(raw).strip().lower()
    app_store_flag = os.environ.get("LANTAI_APP_STORE", "").strip().lower()
    if normalized in _APP_STORE_DISTRIBUTIONS or app_store_flag in {
        "1",
        "true",
        "yes",
    }:
        return "app_store"
    if any("lantai_app store" == part.lower() for part in Path(__file__).parts):
        return "app_store"
    return "developer_id"


def _first_binary(names: tuple[str, ...] | list[str]) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def detect_coding_tool_capabilities(
    *,
    distribution: str | None = None,
) -> dict[str, CodingToolCapability]:
    """Probe optional Coding Mode tools without installing anything."""

    if _normalise_distribution(distribution) == "app_store":
        reason = "disabled in App Store distribution"
        return {
            "ast": CodingToolCapability(False, reason=reason),
            "lsp": CodingToolCapability(False, reason=reason),
        }

    ast_binary = _first_binary(AST_GREP_BINARIES)
    lsp_binary = _first_binary([server.binary for server in _LSP_SERVERS])
    return {
        "ast": CodingToolCapability(
            available=bool(ast_binary),
            command=ast_binary,
            reason=None if ast_binary else "ast-grep or sg was not found",
        ),
        "lsp": CodingToolCapability(
            available=bool(lsp_binary),
            command=lsp_binary,
            reason=None if lsp_binary else "no supported language server was found",
        ),
    }


def coding_tool_prompt_hint(request_context: dict[str, Any] | None = None) -> str:
    """Return one compact prompt line describing optional tool availability."""

    distribution = str((request_context or {}).get("distribution") or "") or None
    capabilities = detect_coding_tool_capabilities(distribution=distribution)
    ast_cap = capabilities["ast"]
    lsp_cap = capabilities["lsp"]
    available: list[str] = []
    unavailable: list[str] = []
    if ast_cap.available:
        available.append("`coding_ast_search`")
    else:
        unavailable.append(f"AST ({ast_cap.reason})")
    if lsp_cap.available:
        available.append("`coding_lsp_diagnostics`")
    else:
        unavailable.append(f"LSP ({lsp_cap.reason})")
    if available:
        suffix = f" Available helpers: {', '.join(available)}."
    else:
        suffix = ""
    if unavailable:
        suffix += f" Unavailable: {'; '.join(unavailable)}."
    return "- Coding helpers are dependency-gated and read-only." + suffix


def get_coding_mode_tool_functions(
    request_context: dict[str, Any] | None = None,
) -> dict[str, Callable[..., Any]]:
    """Return optional Coding Mode tools that are safe to register now."""

    if not is_coding_mode_enabled(request_context):
        return {}
    distribution = str((request_context or {}).get("distribution") or "") or None
    capabilities = detect_coding_tool_capabilities(distribution=distribution)
    tools: dict[str, Callable[..., Any]] = {}
    if capabilities["ast"].available:
        tools["coding_ast_search"] = coding_ast_search
    if capabilities["lsp"].available:
        tools["coding_lsp_diagnostics"] = coding_lsp_diagnostics
    return tools


def _workspace_root() -> Path:
    return Path(get_current_workspace_dir() or WORKING_DIR).expanduser().resolve()


def _is_sensitive_file(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in _SENSITIVE_FILE_PATTERNS)


def _safe_workspace_path(
    raw_path: str | None,
    *,
    root: Path,
    require_file: bool = False,
) -> Path | ToolResponse:
    candidate = (raw_path or ".").strip() or "."
    path = Path(candidate).expanduser()
    target = path if path.is_absolute() else root / path
    try:
        resolved = target.resolve(strict=False)
        resolved.relative_to(root)
    except Exception:
        return _text_response("Error: path escapes the active Coding workspace.")

    try:
        relative = resolved.relative_to(root)
    except ValueError:
        return _text_response("Error: path escapes the active Coding workspace.")
    if any(part in _SKIP_DIRS for part in relative.parts):
        return _text_response("Error: path is hidden from Coding Mode tools.")
    if _is_sensitive_file(resolved.name):
        return _text_response("Error: sensitive files are hidden from Coding Mode tools.")
    if not resolved.exists():
        return _text_response(f"Error: path does not exist: {relative.as_posix()}")
    if require_file and not resolved.is_file():
        return _text_response(f"Error: path is not a file: {relative.as_posix()}")
    return resolved


def _truncate(text: str) -> str:
    if len(text) <= MAX_CODING_TOOL_OUTPUT_CHARS:
        return text
    return (
        text[:MAX_CODING_TOOL_OUTPUT_CHARS]
        + "\n\n[truncated: Coding tool output exceeded 50000 characters]"
    )


async def coding_ast_search(
    pattern: str,
    path: str | None = None,
    language: str | None = None,
    timeout_seconds: float = DEFAULT_AST_TIMEOUT_SECONDS,
) -> ToolResponse:
    """Search code structurally with ast-grep.

    Args:
        pattern: ast-grep pattern, for example ``def $NAME($$$): $$$``.
        path: File or directory under the active Coding workspace. Defaults to
            the workspace root.
        language: Optional ast-grep language id, such as ``python`` or ``ts``.
        timeout_seconds: Search timeout in seconds, capped to 60.
    """

    pattern = (pattern or "").strip()
    if not pattern:
        return _text_response("Error: pattern is required.")

    capability = detect_coding_tool_capabilities()["ast"]
    if not capability.available or not capability.command:
        return _text_response(
            f"Error: coding_ast_search is unavailable ({capability.reason}).",
        )

    root = _workspace_root()
    target = _safe_workspace_path(path, root=root)
    if isinstance(target, ToolResponse):
        return target

    timeout = max(1.0, min(float(timeout_seconds or DEFAULT_AST_TIMEOUT_SECONDS), 60.0))
    command = [capability.command, "run", "--pattern", pattern]
    if language and str(language).strip():
        command.extend(["--lang", str(language).strip()])
    command.append(str(target))

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            command,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _text_response(f"Error: ast-grep timed out after {timeout:.1f}s.")
    except OSError as exc:
        return _text_response(f"Error: failed to run ast-grep: {exc}")

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    rel_target = target.relative_to(root).as_posix() if target != root else "."
    if result.returncode != 0 and not stdout:
        return _text_response(
            "Error: ast-grep failed"
            f" (exit {result.returncode}) while searching {rel_target}.\n"
            + _truncate(stderr or "No error output."),
        )
    if not stdout:
        return _text_response(f"No AST matches found in {rel_target}.")

    detail = f"AST matches in {rel_target}"
    if stderr:
        detail += f"\n\nstderr:\n{stderr}"
    return _text_response(detail + "\n\n" + _truncate(stdout))


def _server_for_file(path: Path) -> tuple[_LanguageServer, str] | None:
    suffix = path.suffix.lower()
    for server in _LSP_SERVERS:
        if suffix not in server.extensions:
            continue
        binary = shutil.which(server.binary)
        if binary:
            return server, binary
    return None


async def _write_lsp_message(proc: asyncio.subprocess.Process, payload: dict[str, Any]) -> None:
    if proc.stdin is None:
        raise RuntimeError("LSP stdin is unavailable")
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    proc.stdin.write(header + body)
    await proc.stdin.drain()


async def _read_lsp_message(proc: asyncio.subprocess.Process, timeout: float) -> dict[str, Any]:
    if proc.stdout is None:
        raise RuntimeError("LSP stdout is unavailable")
    header_bytes = await asyncio.wait_for(proc.stdout.readuntil(b"\r\n\r\n"), timeout)
    headers = header_bytes.decode("ascii", errors="replace").split("\r\n")
    content_length = 0
    for line in headers:
        if line.lower().startswith("content-length:"):
            content_length = int(line.split(":", 1)[1].strip())
            break
    if content_length <= 0:
        raise RuntimeError("LSP response omitted Content-Length")
    body = await asyncio.wait_for(proc.stdout.readexactly(content_length), timeout)
    message = json.loads(body.decode("utf-8"))
    return message if isinstance(message, dict) else {}


def _format_lsp_diagnostics(server_name: str, relative_path: str, diagnostics: list[dict[str, Any]]) -> str:
    if not diagnostics:
        return (
            f"{server_name} returned no diagnostics for {relative_path}. "
            "If the file imports project-wide symbols, rerun after dependencies are installed."
        )
    lines = [f"{server_name} diagnostics for {relative_path}:"]
    severity_names = {1: "error", 2: "warning", 3: "information", 4: "hint"}
    for item in diagnostics[:100]:
        rng = item.get("range") if isinstance(item, dict) else {}
        start = rng.get("start", {}) if isinstance(rng, dict) else {}
        line = int(start.get("line", 0)) + 1 if isinstance(start, dict) else 1
        character = int(start.get("character", 0)) + 1 if isinstance(start, dict) else 1
        severity = severity_names.get(item.get("severity"), "diagnostic")
        message = str(item.get("message") or "").strip()
        source = str(item.get("source") or server_name).strip()
        lines.append(f"- {line}:{character} [{severity}] {source}: {message}")
    if len(diagnostics) > 100:
        lines.append(f"- truncated: {len(diagnostics) - 100} more diagnostics")
    return "\n".join(lines)


async def coding_lsp_diagnostics(
    file_path: str,
    timeout_seconds: float = DEFAULT_LSP_TIMEOUT_SECONDS,
) -> ToolResponse:
    """Run read-only LSP diagnostics for a file when a language server exists.

    Args:
        file_path: File under the active Coding workspace.
        timeout_seconds: Total LSP wait time in seconds, capped to 15.
    """

    capability = detect_coding_tool_capabilities()["lsp"]
    if not capability.available:
        return _text_response(
            f"Error: coding_lsp_diagnostics is unavailable ({capability.reason}).",
        )

    root = _workspace_root()
    target = _safe_workspace_path(file_path, root=root, require_file=True)
    if isinstance(target, ToolResponse):
        return target
    if target.stat().st_size > MAX_LSP_FILE_BYTES:
        return _text_response("Error: file is too large for LSP diagnostics.")

    selected = _server_for_file(target)
    if selected is None:
        return _text_response(
            "Error: no supported language server is available for "
            f"{target.suffix or 'this file type'}.",
        )
    server, binary = selected
    timeout = max(1.0, min(float(timeout_seconds or DEFAULT_LSP_TIMEOUT_SECONDS), 15.0))
    relative_path = target.relative_to(root).as_posix()
    file_uri = target.as_uri()
    root_uri = root.as_uri()
    text = target.read_text(encoding="utf-8", errors="replace")
    proc: asyncio.subprocess.Process | None = None

    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            *server.args,
            cwd=str(root),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        init_request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "processId": os.getpid(),
                "rootUri": root_uri,
                "capabilities": {},
                "workspaceFolders": [
                    {"uri": root_uri, "name": root.name or "workspace"},
                ],
            },
        }
        await _write_lsp_message(proc, init_request)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        initialized = False
        diagnostics: list[dict[str, Any]] = []
        while loop.time() < deadline:
            message = await _read_lsp_message(proc, max(0.1, deadline - loop.time()))
            if message.get("id") == 1:
                initialized = True
                break
        if not initialized:
            return _text_response(f"Error: {server.name} did not initialize in time.")

        await _write_lsp_message(
            proc,
            {"jsonrpc": "2.0", "method": "initialized", "params": {}},
        )
        await _write_lsp_message(
            proc,
            {
                "jsonrpc": "2.0",
                "method": "textDocument/didOpen",
                "params": {
                    "textDocument": {
                        "uri": file_uri,
                        "languageId": server.language_id,
                        "version": 1,
                        "text": text,
                    },
                },
            },
        )

        while loop.time() < deadline:
            try:
                message = await _read_lsp_message(
                    proc,
                    max(0.1, deadline - loop.time()),
                )
            except asyncio.TimeoutError:
                break
            if message.get("method") != "textDocument/publishDiagnostics":
                continue
            params = message.get("params") or {}
            if not isinstance(params, dict) or params.get("uri") != file_uri:
                continue
            raw = params.get("diagnostics") or []
            diagnostics = [item for item in raw if isinstance(item, dict)]

        return _text_response(
            _truncate(_format_lsp_diagnostics(server.name, relative_path, diagnostics)),
        )
    except asyncio.TimeoutError:
        return _text_response(f"Error: {server.name} timed out after {timeout:.1f}s.")
    except Exception as exc:  # pylint: disable=broad-except
        return _text_response(f"Error: failed to run {server.name}: {exc}")
    finally:
        if proc is not None and proc.returncode is None:
            try:
                await _write_lsp_message(
                    proc,
                    {"jsonrpc": "2.0", "id": 2, "method": "shutdown", "params": None},
                )
                await _write_lsp_message(
                    proc,
                    {"jsonrpc": "2.0", "method": "exit", "params": None},
                )
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except Exception:
                proc.terminate()
