from __future__ import annotations

from lantai.security import tool_execution_context
from lantai.security.tool_execution_context import build_sandboxed_tool_env


def test_tools_inherit_parent_process_owner_without_desktop_credentials(monkeypatch):
    nonce = "0123456789abcdef" * 4
    monkeypatch.setenv("LANTAI_DESKTOP_INSTANCE_NONCE", nonce)
    monkeypatch.setenv("LANTAI_DESKTOP_TOKEN", "must-not-leak")
    env = build_sandboxed_tool_env(base_env={
        "PATH": "/usr/bin",
        "LANTAI_DESKTOP_INSTANCE_NONCE": "foreign-owner",
        "LANTAI_DESKTOP_TOKEN": "must-not-leak",
    })
    assert env["LANTAI_DESKTOP_INSTANCE_NONCE"] == nonce
    assert "LANTAI_DESKTOP_TOKEN" not in env


def test_tool_configuration_cannot_create_a_process_owner(monkeypatch):
    monkeypatch.delenv("LANTAI_DESKTOP_INSTANCE_NONCE", raising=False)
    env = build_sandboxed_tool_env(base_env={
        "PATH": "/usr/bin",
        "LANTAI_DESKTOP_INSTANCE_NONCE": "0123456789abcdef" * 4,
    })
    assert "LANTAI_DESKTOP_INSTANCE_NONCE" not in env


def test_presentation_runtime_paths_survive_tool_environment_sandbox() -> None:
    env = build_sandboxed_tool_env(
        base_env={
            "PATH": "/usr/bin:/bin",
            "PRESENTATION_NODE_EXECUTABLE": "/Applications/澜台.app/Contents/MacOS/澜台",
            "PRESENTATION_NODE_MODULES": (
                "/Applications/澜台.app/Contents/Resources/app.asar/node_modules"
            ),
            "PRESENTATION_NODE_MODE": "electron",
            "LANTAI_DESKTOP_TOKEN": "must-not-leak",
        },
        include_python_dir=False,
    )

    assert env["PRESENTATION_NODE_EXECUTABLE"].endswith("/澜台")
    assert env["PRESENTATION_NODE_MODULES"].endswith("/node_modules")
    assert env["PRESENTATION_NODE_MODE"] == "electron"
    assert "LANTAI_DESKTOP_TOKEN" not in env


def test_packaged_backend_bin_is_available_to_shell_tools(
    tmp_path,
    monkeypatch,
) -> None:
    backend_root = tmp_path / "lantai-server"
    backend_executable = backend_root / "lantai-server"
    bundled_bin = backend_root / "bin"
    bundled_bin.mkdir(parents=True)
    backend_executable.touch()
    (bundled_bin / "officecli").touch()
    monkeypatch.setattr(
        tool_execution_context.sys,
        "executable",
        str(backend_executable),
    )

    env = build_sandboxed_tool_env(
        base_env={
            "PATH": "/usr/bin:/bin",
            "OFFICECLI_PATH": str(bundled_bin / "officecli"),
            "OFFICECLI_SKIP_UPDATE": "1",
        },
    )

    assert str(bundled_bin) in env["PATH"].split(":")
    assert env["OFFICECLI_PATH"] == str(bundled_bin / "officecli")
    assert env["OFFICECLI_SKIP_UPDATE"] == "1"
