#!/usr/bin/env python3
"""Verify a packaged app can start its bundled Chrome DevTools MCP server.

This smoke test performs only MCP initialization and ``tools/list``. It does
not call a browser tool, launch Chrome, navigate to a page, or use the network.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import subprocess

from lantai.app.mcp.chrome_devtools import (
    CHROME_DEVTOOLS_ALLOWED_TOOLS,
    CHROME_DEVTOOLS_PACKAGE_VERSION,
    CHROME_DEVTOOLS_SERVER_ENV,
    ChromeDevToolsRuntime,
    chrome_devtools_server_args,
)
from lantai.app.mcp.stateful_client import StdIOStatefulClient


def _required_file(path: Path, label: str) -> Path:
    if not path.is_file():
        raise RuntimeError(f"Packaged {label} is missing: {path}")
    return path


def _supported_node_version(value: str) -> bool:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.\d+", value.strip())
    if match is None:
        return False
    major, minor = (int(part) for part in match.groups())
    return (
        major >= 23
        or major == 22
        and minor >= 12
        or major == 20
        and minor >= 19
    )


async def run_smoke(app: Path, *, timeout: int) -> dict[str, object]:
    app = app.expanduser().resolve()
    resources = app / "Contents" / "Resources"
    modules = resources / "app.asar.unpacked" / "node_modules"
    package = modules / "chrome-devtools-mcp"
    manifest = _required_file(package / "package.json", "Chrome MCP manifest")
    entrypoint = _required_file(
        package / "build" / "src" / "bin" / "chrome-devtools-mcp.js",
        "Chrome MCP entrypoint",
    )
    executable = _required_file(
        app / "Contents" / "MacOS" / app.stem,
        "Electron executable",
    )
    package_version = str(
        json.loads(manifest.read_text(encoding="utf-8")).get("version") or "",
    )
    if package_version != CHROME_DEVTOOLS_PACKAGE_VERSION:
        raise RuntimeError(
            "Packaged Chrome DevTools MCP version mismatch: "
            f"expected {CHROME_DEVTOOLS_PACKAGE_VERSION}, got {package_version}",
        )

    node_probe = subprocess.run(
        [str(executable), "-p", "process.version"],
        env={**os.environ, **CHROME_DEVTOOLS_SERVER_ENV},
        capture_output=True,
        text=True,
        timeout=max(5, timeout),
        check=False,
    )
    node_version = node_probe.stdout.strip()
    if node_probe.returncode != 0 or not _supported_node_version(node_version):
        raise RuntimeError(
            "Packaged Electron Node runtime does not satisfy "
            f"chrome-devtools-mcp@{package_version}: {node_version or 'unknown'}",
        )

    runtime = ChromeDevToolsRuntime(
        available=True,
        source="bundled",
        command=str(executable),
        entrypoint=str(entrypoint),
        package_version=package_version,
        # Keep the release handshake deterministic. Screencast is an optional
        # host enhancement and is covered separately when ffmpeg is present.
        ffmpeg_path=None,
    )
    server_args = chrome_devtools_server_args(
        runtime,
        agent_id="packaged-smoke",
    )

    client = StdIOStatefulClient(
        name="packaged-chrome-devtools-smoke",
        command=str(executable),
        args=[str(entrypoint), *server_args],
        env=dict(CHROME_DEVTOOLS_SERVER_ENV),
        cwd=str(resources),
        read_timeout_seconds=float(timeout),
    )
    try:
        await client.connect(timeout=float(timeout))
        tools = await client.list_tools()
    finally:
        await client.close()

    names = sorted(str(getattr(tool, "name", "") or "") for tool in tools)
    missing = sorted(set(CHROME_DEVTOOLS_ALLOWED_TOOLS).difference(names))
    if missing:
        raise RuntimeError(
            "Packaged Chrome DevTools MCP is missing allowed tools: "
            + ", ".join(missing),
        )
    return {
        "status": "ok",
        "app": app.name,
        "package": "chrome-devtools-mcp",
        "package_version": package_version,
        "mcp_connected": True,
        "node_version": node_version,
        "server_tool_count": len(names),
        "allowed_tool_count": len(CHROME_DEVTOOLS_ALLOWED_TOOLS),
        "chrome_launched": False,
        "network_used": False,
        "server_args": list(server_args),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(run_smoke(args.app, timeout=max(5, args.timeout))),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
