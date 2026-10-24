from __future__ import annotations

from click.testing import CliRunner

from lantai.app import auth as auth_module
from lantai.cli import app_cmd as app_cmd_module
from lantai.cli.main import cli


def test_run_command_is_registered_with_self_host_options() -> None:
    result = CliRunner().invoke(cli, ["run", "--help"])

    assert result.exit_code == 0
    assert "Start Console, agents, channels and scheduled tasks" in result.output
    assert "--host" in result.output
    assert "--port" in result.output


def test_run_command_uses_same_app_implementation(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_app(
        host: str,
        port: int,
        reload: bool,
        workers: int | None,
        log_level: str,
        hide_access_paths: tuple[str, ...],
    ) -> None:
        captured.update(
            host=host,
            port=port,
            reload=reload,
            workers=workers,
            log_level=log_level,
            hide_access_paths=hide_access_paths,
        )

    monkeypatch.setattr(app_cmd_module, "_run_app", fake_run_app)
    result = CliRunner().invoke(
        cli,
        ["run", "--host", "0.0.0.0", "--port", "9000", "--log-level", "debug"],
    )

    assert result.exit_code == 0
    assert captured == {
        "host": "0.0.0.0",
        "port": 9000,
        "reload": False,
        "workers": None,
        "log_level": "debug",
        "hide_access_paths": ("/console/push-messages",),
    }


def test_run_rejects_non_loopback_bind_when_auth_is_disabled(
    monkeypatch,
) -> None:
    monkeypatch.setenv("LANTAI_AUTH_ENABLED", "false")
    monkeypatch.setattr(app_cmd_module.uvicorn, "run", lambda *_args, **_kwargs: None)

    result = CliRunner().invoke(
        cli,
        ["run", "--host", "0.0.0.0", "--port", "9000"],
    )

    assert result.exit_code != 0
    assert "non-loopback" in result.output
    assert "LANTAI_AUTH_ENABLED=true" in result.output


def test_run_rejects_non_loopback_bind_without_bootstrapped_account(
    monkeypatch,
) -> None:
    monkeypatch.setenv("LANTAI_AUTH_ENABLED", "true")
    monkeypatch.delenv("LANTAI_AUTH_USERNAME", raising=False)
    monkeypatch.delenv("LANTAI_AUTH_PASSWORD", raising=False)
    monkeypatch.setattr(auth_module, "has_registered_users", lambda: False)
    monkeypatch.setattr(app_cmd_module.uvicorn, "run", lambda *_args, **_kwargs: None)

    result = CliRunner().invoke(
        cli,
        ["run", "--host", "0.0.0.0", "--port", "9000"],
    )

    assert result.exit_code != 0
    assert "registered account" in result.output
    assert "LANTAI_AUTH_USERNAME" in result.output
