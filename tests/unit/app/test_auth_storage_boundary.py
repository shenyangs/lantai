"""A failed auth store cannot become a fresh, unprotected installation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import click
import httpx
import pytest
from click.testing import CliRunner
from fastapi import FastAPI

from lantai.app import auth
from lantai.app.routers import auth as auth_router
from lantai.cli import app_cmd, auth_cmd
from lantai.cli import doctor_cmd
from lantai.security.secret_store import SecretDecryptionError


def _user(name: str) -> dict[str, str]:
    return {
        "username": name,
        "password_hash": "a" * 64,
        "password_salt": "b" * 32,
    }


def _app() -> tuple[FastAPI, list[str]]:
    app = FastAPI()
    app.add_middleware(auth.AuthMiddleware)
    app.include_router(auth_router.router, prefix="/api")
    reached: list[str] = []

    @app.get("/api/protected")
    async def protected() -> dict[str, bool]:
        reached.append("protected")
        return {"reached": True}

    return app, reached


def _request(app: FastAPI, method: str, path: str, **kwargs) -> httpx.Response:
    async def run() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=app, client=("203.0.113.9", 1234),
            ),
            base_url="http://audit.example",
        ) as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(run())


@pytest.fixture
def auth_file(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "auth.json"
    monkeypatch.setattr(auth, "AUTH_FILE", path)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth_router, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "encrypt_dict_fields", lambda data, _fields: dict(data))
    monkeypatch.setattr(auth, "decrypt_dict_fields", lambda data, _fields: dict(data))
    monkeypatch.setattr(
        auth_router.EnvVarLoader,
        "get_str",
        lambda key, default="": "true" if key == "LANTAI_AUTH_ENABLED" else default,
    )
    return path


def test_corrupt_auth_file_never_reaches_protected_handler_or_registration(
    auth_file: Path,
) -> None:
    original = b'{"user":'
    auth_file.write_bytes(original)
    app, reached = _app()

    protected = _request(app, "GET", "/api/protected")
    registration = _request(
        app,
        "POST",
        "/api/auth/register",
        json={"username": "replacement", "password": "new-password"},
    )

    assert protected.status_code == 503
    assert registration.status_code == 503
    assert reached == []
    assert auth_file.read_bytes() == original


@pytest.mark.parametrize(
    "bad_bytes",
    [b"[]", b"null", b'{"user":42}', b"{}", b'{"jwt_secret":"orphan"}'],
)
def test_incompatible_auth_structure_fails_closed(
    auth_file: Path,
    bad_bytes: bytes,
) -> None:
    auth_file.write_bytes(bad_bytes)
    app, reached = _app()

    response = _request(app, "GET", "/api/protected")
    registration = _request(
        app,
        "POST",
        "/api/auth/register",
        json={"username": "replacement", "password": "new-password"},
    )

    assert response.status_code == 503
    assert registration.status_code == 503
    assert reached == []
    assert auth_file.read_bytes() == bad_bytes


def test_decryption_failure_fails_closed(
    auth_file: Path,
    monkeypatch,
) -> None:
    auth_file.write_text(json.dumps({"user": _user("sam"), "jwt_secret": "ENC:x"}))

    def fail_decrypt(*_args):
        raise SecretDecryptionError("synthetic decryption failure")

    monkeypatch.setattr(auth, "decrypt_dict_fields", fail_decrypt)
    app, reached = _app()

    response = _request(app, "GET", "/api/protected")

    assert response.status_code == 503
    assert reached == []


def test_unreadable_auth_file_fails_closed(auth_file: Path, monkeypatch) -> None:
    auth_file.write_text(json.dumps({"user": _user("sam")}))
    real_open = open

    def fail_auth_open(file, *args, **kwargs):
        if str(file) == str(auth_file):
            raise OSError("synthetic read failure")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fail_auth_open)
    app, reached = _app()

    response = _request(app, "GET", "/api/protected")

    assert response.status_code == 503
    assert reached == []


def test_missing_auth_file_with_existing_backup_requires_recovery(
    auth_file: Path,
) -> None:
    backup = auth_file.with_name("auth.json.bak")
    backup.write_text(json.dumps({"user": _user("sam")}))
    app, reached = _app()

    protected = _request(app, "GET", "/api/protected")
    registration = _request(
        app,
        "POST",
        "/api/auth/register",
        json={"username": "replacement", "password": "new-password"},
    )

    assert protected.status_code == 503
    assert registration.status_code == 503
    assert reached == []
    assert not auth_file.exists()
    assert json.loads(backup.read_text())["user"]["username"] == "sam"


def test_first_registration_and_existing_account_still_enforce_auth(
    auth_file: Path,
) -> None:
    app, reached = _app()
    first_run = _request(app, "GET", "/api/protected")
    assert first_run.status_code == 403
    assert _request(app, "GET", "/api/auth/status").status_code == 200

    registration = _request(
        app,
        "POST",
        "/api/auth/register",
        json={"username": "sam", "password": "correct horse battery staple"},
    )
    assert registration.status_code == 200
    assert json.loads(auth_file.read_text())["user"]["username"] == "sam"

    protected = _request(app, "GET", "/api/protected")
    assert protected.status_code == 401
    login = _request(
        app,
        "POST",
        "/api/auth/login",
        json={"username": "sam", "password": "correct horse battery staple"},
    )
    assert login.status_code == 200
    authorized = _request(
        app,
        "GET",
        "/api/protected",
        headers={"Authorization": f"Bearer {login.json()['token']}"},
    )
    assert authorized.status_code == 200
    assert reached == ["protected"]


def test_auth_disabled_development_mode_remains_open(
    auth_file: Path,
    monkeypatch,
) -> None:
    auth_file.write_bytes(b"broken")
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)
    app, reached = _app()

    response = _request(app, "GET", "/api/protected")

    assert response.status_code == 200
    assert reached == ["protected"]


def test_interrupted_auth_save_retains_last_good_credentials(
    auth_file: Path,
    monkeypatch,
) -> None:
    previous = {"user": _user("sam"), "jwt_secret": "old-secret"}
    auth._save_auth_data(previous)
    original = auth_file.read_bytes()
    real_dump = json.dump

    def interrupted_dump(value, file, *args, **kwargs):
        file.write("{")
        raise OSError("synthetic interrupted write")

    monkeypatch.setattr(auth.json, "dump", interrupted_dump)
    with pytest.raises(OSError, match="synthetic interrupted write"):
        auth._save_auth_data({"user": _user("replacement")})
    monkeypatch.setattr(auth.json, "dump", real_dump)

    assert auth_file.read_bytes() == original
    assert auth._load_auth_data()["user"]["username"] == "sam"


def test_save_preserves_verifiable_previous_credentials_as_backup(
    auth_file: Path,
) -> None:
    auth._save_auth_data({"user": _user("sam")})
    original = auth_file.read_bytes()

    auth._save_auth_data({"user": _user("next")})

    backup = auth_file.with_name("auth.json.bak")
    assert backup.read_bytes() == original
    assert json.loads(backup.read_text())["user"]["username"] == "sam"
    assert auth._load_auth_data()["user"]["username"] == "next"


def test_first_account_save_leaves_recovery_evidence_if_main_goes_missing(
    auth_file: Path,
) -> None:
    auth._save_auth_data({"user": _user("sam")})
    saved = auth_file.read_bytes()
    backup = auth_file.with_name("auth.json.bak")
    assert backup.read_bytes() == saved

    auth_file.unlink()
    app, reached = _app()
    response = _request(app, "GET", "/api/protected")
    assert response.status_code == 503
    assert reached == []


def test_failed_atomic_replace_keeps_original_and_recoverable_backup(
    auth_file: Path,
    monkeypatch,
) -> None:
    auth._save_auth_data({"user": _user("sam")})
    original = auth_file.read_bytes()
    real_replace = auth.os.replace

    def fail_main_replace(source, destination):
        if str(destination) == str(auth_file):
            raise OSError("synthetic replacement failure")
        return real_replace(source, destination)

    monkeypatch.setattr(auth.os, "replace", fail_main_replace)
    with pytest.raises(OSError, match="synthetic replacement failure"):
        auth._save_auth_data({"user": _user("next")})

    assert auth_file.read_bytes() == original
    assert auth_file.with_name("auth.json.bak").read_bytes() == original


def test_cli_password_reset_reports_corrupt_auth_without_rewriting(
    auth_file: Path,
    monkeypatch,
) -> None:
    original = b"{broken"
    auth_file.write_bytes(original)
    monkeypatch.setattr(auth_cmd, "is_auth_enabled", lambda: True)

    result = CliRunner().invoke(auth_cmd.auth_group, ["reset-password"])

    assert result.exit_code != 0
    assert "Preserve auth.json" in result.output
    assert auth_file.read_bytes() == original


def test_doctor_reports_auth_storage_failure_as_actionable_state(
    auth_file: Path,
    monkeypatch,
) -> None:
    auth_file.write_bytes(b"{broken")
    monkeypatch.setattr(doctor_cmd, "is_auth_enabled", lambda: True)

    healthy, detail = doctor_cmd._check_web_auth("http://audit.example")

    assert not healthy
    assert "auth.json" in detail
    assert "backup" in detail


def test_remote_bind_explains_corrupt_auth_store_before_starting(
    auth_file: Path,
    monkeypatch,
) -> None:
    auth_file.write_bytes(b"{broken")
    monkeypatch.setattr(app_cmd, "_auth_enabled_from_env", lambda: True)
    monkeypatch.setattr(app_cmd.uvicorn, "run", lambda *_args, **_kwargs: pytest.fail("server started"))

    with pytest.raises(click.ClickException, match="auth.json"):
        app_cmd._run_app("0.0.0.0", 9876, False, None, "info", ())
