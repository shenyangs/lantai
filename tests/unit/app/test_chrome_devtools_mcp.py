from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lantai.app.mcp import chrome_devtools
from lantai.app.routers import mcp as mcp_router
from lantai.config.config import MCPConfig


def _fake_runtime(tmp_path: Path, *, version: str = "1.8.0") -> Path:
    modules = tmp_path / "node_modules"
    package = modules / "chrome-devtools-mcp"
    entry = package / "build" / "src" / "bin" / "chrome-devtools-mcp.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("// fixture\n", encoding="utf-8")
    (package / "package.json").write_text(
        json.dumps({"name": "chrome-devtools-mcp", "version": version}),
        encoding="utf-8",
    )
    electron = modules / "electron"
    executable = electron / "dist" / "electron-fixture"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    (electron / "path.txt").write_text("electron-fixture\n", encoding="utf-8")
    return modules


def test_runtime_resolves_only_the_pinned_bundled_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modules = _fake_runtime(tmp_path)
    monkeypatch.delenv("PRESENTATION_NODE_EXECUTABLE", raising=False)
    monkeypatch.setattr(chrome_devtools, "_ffmpeg_path", lambda: None)
    monkeypatch.setattr(
        chrome_devtools,
        "_candidate_modules_roots",
        lambda: ((modules, "bundled"),),
    )

    runtime = chrome_devtools.resolve_chrome_devtools_runtime()

    assert runtime.available is True
    assert runtime.source == "bundled"
    assert runtime.package_version == "1.8.0"
    assert runtime.command.endswith("electron-fixture")
    assert runtime.entrypoint.endswith("chrome-devtools-mcp.js")


def test_runtime_rejects_an_unpinned_package_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modules = _fake_runtime(tmp_path, version="1.8.1")
    monkeypatch.setattr(
        chrome_devtools,
        "_candidate_modules_roots",
        lambda: ((modules, "source"),),
    )

    runtime = chrome_devtools.resolve_chrome_devtools_runtime()

    assert runtime.available is False
    assert runtime.source == "missing"
    assert runtime.package_version == "1.8.1"
    assert "不匹配" in runtime.message


def test_client_config_is_offline_persistent_and_full_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modules = _fake_runtime(tmp_path)
    monkeypatch.delenv("PRESENTATION_NODE_EXECUTABLE", raising=False)
    monkeypatch.setattr(chrome_devtools, "_ffmpeg_path", lambda: None)
    monkeypatch.setattr(
        chrome_devtools,
        "_candidate_modules_roots",
        lambda: ((modules, "source"),),
    )

    config = chrome_devtools.build_chrome_devtools_client_config(enabled=True)

    assert config.enabled is True
    assert config.command.endswith("electron-fixture")
    assert config.args[0].endswith("chrome-devtools-mcp.js")
    assert "--isolated" not in config.args
    assert "--page-id-routing" in config.args
    assert "--memory-debugging" in config.args
    assert "--category-extensions" in config.args
    assert "--category-pwa" in config.args
    assert "--category-experimental-third-party" in config.args
    assert "--category-experimental-webmcp" in config.args
    assert "--experimental-devtools" in config.args
    assert "--experimental-vision" in config.args
    assert "--experimental-structured-content" in config.args
    assert "--experimental-include-all-pages" in config.args
    assert "--allow-unrestricted-paths" in config.args
    assert any(item.startswith("--user-data-dir=") for item in config.args)
    assert "--no-usage-statistics" in config.args
    assert "--no-performance-crux" not in config.args
    assert "--redact-network-headers" not in config.args
    assert not any("latest" in item or item == "npx" for item in config.args)
    assert config.env["ELECTRON_RUN_AS_NODE"] == "1"
    assert config.env["CHROME_DEVTOOLS_MCP_NO_UPDATE_CHECKS"] == "1"
    assert config.env["CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS"] == "1"
    assert "upload_file" in config.allowed_tools
    assert "take_heapsnapshot" in config.allowed_tools
    assert "install_extension" in config.allowed_tools
    assert "install_pwa" in config.allowed_tools
    assert len(config.allowed_tools) == 55
    assert "performance_start_trace" in config.allowed_tools
    assert "evaluate_script" in config.allowed_tools


def test_default_is_disabled_and_preserves_an_explicit_enable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modules = _fake_runtime(tmp_path)
    monkeypatch.delenv("PRESENTATION_NODE_EXECUTABLE", raising=False)
    monkeypatch.setattr(chrome_devtools, "_ffmpeg_path", lambda: None)
    monkeypatch.setattr(
        chrome_devtools,
        "_candidate_modules_roots",
        lambda: ((modules, "source"),),
    )

    defaulted = chrome_devtools.ensure_chrome_devtools_default(None)
    assert (
        defaulted.clients[chrome_devtools.CHROME_DEVTOOLS_CLIENT_KEY].enabled is False
    )

    existing = MCPConfig(
        clients={
            chrome_devtools.CHROME_DEVTOOLS_CLIENT_KEY: (
                chrome_devtools.build_chrome_devtools_client_config(enabled=True)
            ),
        },
    )
    merged = chrome_devtools.ensure_chrome_devtools_default(existing)
    assert merged.clients[chrome_devtools.CHROME_DEVTOOLS_CLIENT_KEY].enabled is True


def test_native_client_is_ordered_before_custom_tool_name_collisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modules = _fake_runtime(tmp_path)
    monkeypatch.delenv("PRESENTATION_NODE_EXECUTABLE", raising=False)
    monkeypatch.setattr(chrome_devtools, "_ffmpeg_path", lambda: None)
    monkeypatch.setattr(
        chrome_devtools,
        "_candidate_modules_roots",
        lambda: ((modules, "source"),),
    )
    custom = chrome_devtools.build_chrome_devtools_client_config(enabled=False)
    custom.name = "Custom Browser MCP"
    existing = MCPConfig(clients={"custom-browser": custom})

    merged = chrome_devtools.ensure_chrome_devtools_default(existing)

    assert next(iter(merged.clients)) == chrome_devtools.CHROME_DEVTOOLS_CLIENT_KEY
    assert list(merged.clients)[1] == "custom-browser"


@pytest.mark.asyncio
async def test_status_uses_live_mcp_handshake_truth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modules = _fake_runtime(tmp_path)
    monkeypatch.delenv("PRESENTATION_NODE_EXECUTABLE", raising=False)
    monkeypatch.setattr(chrome_devtools, "_ffmpeg_path", lambda: None)
    monkeypatch.setattr(
        chrome_devtools,
        "_candidate_modules_roots",
        lambda: ((modules, "bundled"),),
    )
    config = chrome_devtools.build_chrome_devtools_client_config(enabled=True)
    state = SimpleNamespace(
        status="ready",
        attempt_count=1,
        error_code=None,
        updated_at=123.0,
    )

    class Manager:
        async def get_runtime_state(self, key: str):
            assert key == chrome_devtools.CHROME_DEVTOOLS_CLIENT_KEY
            return state

        async def get_client(self, key: str):
            assert key == chrome_devtools.CHROME_DEVTOOLS_CLIENT_KEY
            return SimpleNamespace(is_connected=True)

    agent = SimpleNamespace(
        config=SimpleNamespace(mcp=MCPConfig(clients={config.name: config})),
        mcp_manager=Manager(),
    )
    # Config keys are product identities, not display names.
    agent.config.mcp.clients = {
        chrome_devtools.CHROME_DEVTOOLS_CLIENT_KEY: config,
    }

    info = await mcp_router._build_chrome_devtools_info(agent)

    assert info.capability_status == "ready"
    assert info.connected is True
    assert info.runtime_attempt_count == 1
    assert info.runtime_source == "bundled"
    assert info.attaches_existing_chrome is False
    assert info.usage_statistics_enabled is False
    assert info.performance_crux_enabled is True
    assert info.network_headers_redacted is False
    assert info.unrestricted_paths_enabled is True
    assert info.filesystem_access == "host_authorized"


@pytest.mark.asyncio
async def test_reserved_toggle_persists_only_canonical_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modules = _fake_runtime(tmp_path)
    monkeypatch.delenv("PRESENTATION_NODE_EXECUTABLE", raising=False)
    monkeypatch.setattr(chrome_devtools, "_ffmpeg_path", lambda: None)
    monkeypatch.setattr(
        chrome_devtools,
        "_candidate_modules_roots",
        lambda: ((modules, "source"),),
    )
    agent = SimpleNamespace(
        agent_id="default",
        config=SimpleNamespace(mcp=MCPConfig(clients={})),
        mcp_manager=None,
    )

    async def get_agent(_request):
        return agent

    saved: list[tuple[str, object]] = []
    monkeypatch.setattr(
        "lantai.app.agent_context.get_agent_for_request",
        get_agent,
    )
    monkeypatch.setattr(
        "lantai.config.config.save_agent_config",
        lambda agent_id, config: saved.append((agent_id, config)),
    )
    monkeypatch.setattr(mcp_router, "schedule_agent_reload", lambda *_args: None)

    info = await mcp_router.set_chrome_devtools_mcp(
        SimpleNamespace(),
        mcp_router.ChromeDevToolsToggleRequest(enabled=True),
    )

    stored = agent.config.mcp.clients[chrome_devtools.CHROME_DEVTOOLS_CLIENT_KEY]
    assert stored.enabled is True
    assert stored.command.endswith("electron-fixture")
    assert all(
        item in stored.args[1:] for item in chrome_devtools.CHROME_DEVTOOLS_SERVER_ARGS
    )
    assert any(item.startswith("--user-data-dir=") for item in stored.args[1:])
    assert saved and saved[0][0] == "default"
    assert info.enabled is True
    assert info.capability_status == "starting"


def test_tool_classification_is_total_disjoint_and_fixed() -> None:
    groups = (
        chrome_devtools.CHROME_DEVTOOLS_OBSERVE_TOOLS,
        chrome_devtools.CHROME_DEVTOOLS_LOCAL_STATE_TOOLS,
        chrome_devtools.CHROME_DEVTOOLS_NAVIGATION_TOOLS,
        chrome_devtools.CHROME_DEVTOOLS_INTERACTIVE_TOOLS,
        chrome_devtools.CHROME_DEVTOOLS_SENSITIVE_TOOLS,
    )
    complete = set(chrome_devtools.CHROME_DEVTOOLS_ALLOWED_TOOLS) | set(
        chrome_devtools.CHROME_DEVTOOLS_SCREENCAST_TOOLS,
    )

    assert len(complete) == 57
    assert set().union(*groups) == complete
    for index, group in enumerate(groups):
        for other in groups[index + 1 :]:
            assert set(group).isdisjoint(other)


def test_optional_ffmpeg_adds_only_screencast_tools_and_flags() -> None:
    runtime = chrome_devtools.ChromeDevToolsRuntime(
        available=True,
        source="source",
        ffmpeg_path="/opt/example/bin/ffmpeg",
    )

    assert chrome_devtools.chrome_devtools_allowed_tools(runtime)[-2:] == (
        "screencast_start",
        "screencast_stop",
    )
    args = chrome_devtools.chrome_devtools_server_args(
        runtime,
        agent_id="default",
    )
    assert "--experimental-screencast" in args
    assert "--experimental-ffmpeg-path=/opt/example/bin/ffmpeg" in args


def test_persistent_profiles_are_isolated_per_agent() -> None:
    runtime = chrome_devtools.ChromeDevToolsRuntime(
        available=True,
        source="source",
    )

    first = chrome_devtools.chrome_devtools_server_args(runtime, agent_id="alpha")
    second = chrome_devtools.chrome_devtools_server_args(runtime, agent_id="beta")
    first_profile = next(item for item in first if item.startswith("--user-data-dir="))
    second_profile = next(
        item for item in second if item.startswith("--user-data-dir=")
    )

    assert first_profile != second_profile
    assert "--auto-connect" not in first
    assert "--browser-url" not in first
    assert "--ws-endpoint" not in first
