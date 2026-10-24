# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from lantai.app.kdocs_connection import (
    KdocsConnectionError,
    KdocsConnectionService,
    find_kdocs_cli,
)


class FakeKdocsCli:
    def __init__(self, executable: Path) -> None:
        self.executable = executable
        self.authenticated = False

    def run(
        self,
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        assert command[0] == str(self.executable)
        if command[1:] == ["version"]:
            return subprocess.CompletedProcess(command, 0, "2.5.7\n", "")
        if command[1:3] == ["auth", "set-token"]:
            self.authenticated = True
            return subprocess.CompletedProcess(command, 0, "saved\n", "")
        if command[1:] == ["auth", "status"]:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "authenticated": self.authenticated,
                        "token": "masked-token-value",
                    },
                ),
                "",
            )
        if command[1:] == ["auth", "logout"]:
            self.authenticated = False
            return subprocess.CompletedProcess(command, 0, "logged out\n", "")
        if command[1:3] == ["drive", "list-my-files"]:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps({"code": 0, "msg": "ok", "data": {"items": []}}),
                "",
            )
        raise AssertionError(f"Unexpected command: {command!r}")


class InvalidTokenFakeKdocsCli(FakeKdocsCli):
    def run(
        self,
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        if command[1:3] == ["drive", "list-my-files"]:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps({"code": 400006, "data": {}}),
                "authentication failed",
            )
        return super().run(command, **kwargs)


class InstallingFakeKdocsCli(FakeKdocsCli):
    def __init__(self, executable: Path, setup_script: Path) -> None:
        super().__init__(executable)
        self.setup_script = setup_script
        self.installed = False

    def find(self) -> Path | None:
        return self.executable if self.installed else None

    def run(
        self,
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        if command == ["/bin/sh", str(self.setup_script)]:
            self.installed = True
            return subprocess.CompletedProcess(command, 0, "installed\n", "")
        return super().run(command, **kwargs)


class InstallingWindowsFakeKdocsCli(FakeKdocsCli):
    def __init__(self, executable: Path, setup_script: Path) -> None:
        super().__init__(executable)
        self.setup_script = setup_script
        self.installed = False

    def find(self) -> Path | None:
        return self.executable if self.installed else None

    def run(
        self,
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        if Path(command[0]).name in {"powershell", "pwsh"} and command[1:] == [
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self.setup_script),
        ]:
            self.installed = True
            return subprocess.CompletedProcess(command, 0, "installed\n", "")
        return super().run(command, **kwargs)


def test_connect_returns_verified_status_without_exposing_token(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "kdocs-cli"
    executable.touch()
    fake_cli = FakeKdocsCli(executable)
    service = KdocsConnectionService(
        skill_dir=tmp_path,
        command_runner=fake_cli.run,
        cli_finder=lambda: executable,
    )
    token = "private-kdocs-token-value"

    status = service.connect(token)

    assert status.installed is True
    assert status.authenticated is True
    assert status.cli_version == "2.5.7"
    assert token not in repr(status)
    assert "masked-token-value" not in repr(status)


def test_connect_rejects_token_that_cannot_read_cloud_documents(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "kdocs-cli"
    executable.touch()
    fake_cli = InvalidTokenFakeKdocsCli(executable)
    service = KdocsConnectionService(
        skill_dir=tmp_path,
        command_runner=fake_cli.run,
        cli_finder=lambda: executable,
    )

    with pytest.raises(KdocsConnectionError, match="cloud document access"):
        service.connect("present-but-invalid-token")


def test_status_does_not_report_connected_for_an_invalid_saved_token(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "kdocs-cli"
    executable.touch()
    fake_cli = InvalidTokenFakeKdocsCli(executable)
    fake_cli.authenticated = True
    service = KdocsConnectionService(
        skill_dir=tmp_path,
        command_runner=fake_cli.run,
        cli_finder=lambda: executable,
    )

    status = service.status()

    assert status.installed is True
    assert status.authenticated is False


def test_connect_installs_official_cli_before_binding(tmp_path: Path) -> None:
    setup_script = tmp_path / "scripts" / "setup.sh"
    setup_script.parent.mkdir()
    setup_script.touch()
    executable = tmp_path / "installed" / "kdocs-cli"
    fake_cli = InstallingFakeKdocsCli(executable, setup_script)
    service = KdocsConnectionService(
        skill_dir=tmp_path,
        command_runner=fake_cli.run,
        cli_finder=fake_cli.find,
        platform_name="darwin",
    )

    status = service.connect("private-kdocs-token-value")

    assert status.installed is True
    assert status.authenticated is True


def test_connect_installs_official_cli_on_windows(tmp_path: Path) -> None:
    setup_script = tmp_path / "scripts" / "setup.ps1"
    setup_script.parent.mkdir()
    setup_script.touch()
    executable = tmp_path / "installed" / "kdocs-cli.exe"
    fake_cli = InstallingWindowsFakeKdocsCli(executable, setup_script)
    service = KdocsConnectionService(
        skill_dir=tmp_path,
        command_runner=fake_cli.run,
        cli_finder=fake_cli.find,
        platform_name="win32",
    )

    status = service.connect("private-kdocs-token-value")

    assert status.installed is True
    assert status.authenticated is True


def test_disconnect_removes_cli_authentication(tmp_path: Path) -> None:
    executable = tmp_path / "kdocs-cli"
    executable.touch()
    fake_cli = FakeKdocsCli(executable)
    fake_cli.authenticated = True
    service = KdocsConnectionService(
        skill_dir=tmp_path,
        command_runner=fake_cli.run,
        cli_finder=lambda: executable,
    )

    status = service.disconnect()

    assert status.installed is True
    assert status.authenticated is False


def test_connect_rejects_empty_token_without_echoing_input(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "kdocs-cli"
    executable.touch()
    fake_cli = FakeKdocsCli(executable)
    service = KdocsConnectionService(
        skill_dir=tmp_path,
        command_runner=fake_cli.run,
        cli_finder=lambda: executable,
    )

    with pytest.raises(KdocsConnectionError) as exc_info:
        service.connect("   ")

    assert "token" in str(exc_info.value).lower()
    assert "   " not in str(exc_info.value)


def test_connect_hides_token_when_cli_times_out(tmp_path: Path) -> None:
    executable = tmp_path / "kdocs-cli"
    executable.touch()
    token = "private-kdocs-token-value"

    def timeout_runner(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, timeout=30)

    service = KdocsConnectionService(
        skill_dir=tmp_path,
        command_runner=timeout_runner,
        cli_finder=lambda: executable,
    )

    with pytest.raises(KdocsConnectionError) as exc_info:
        service.connect(token)

    assert token not in str(exc_info.value)


def test_configured_cli_directory_takes_precedence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configured_dir = tmp_path / "configured"
    configured_dir.mkdir()
    executable = configured_dir / "kdocs-cli"
    executable.touch()
    monkeypatch.setenv("KDOCS_CLI_DIR", str(configured_dir))

    assert find_kdocs_cli() == executable.resolve()
