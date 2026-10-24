# -*- coding: utf-8 -*-
"""The desktop can reach login/recovery without weakening business auth."""

from __future__ import annotations

import asyncio
import secrets
from pathlib import Path

import httpx
import pytest

from lantai.app import auth
from lantai.app import _app as app_module
from lantai.app.routers import auth as auth_router
from lantai.config import load_config
from lantai.security import secret_store


@pytest.fixture(name="desktop_auth")
def _desktop_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    auth_file = tmp_path / "auth.json"
    monkeypatch.setattr(auth, "AUTH_FILE", auth_file)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth_router, "is_auth_enabled", lambda: True)
    monkeypatch.setenv("LANTAI_DISABLE_KEYRING", "1")
    monkeypatch.setattr(secret_store, "_get_secret_dir", lambda: tmp_path)
    monkeypatch.setattr(secret_store, "_cached_master_key", None)
    monkeypatch.setattr(secret_store, "_cached_fernet", None)
    config = load_config().model_copy(deep=True)
    config.security.allow_no_auth_hosts = []
    monkeypatch.setattr("lantai.config.load_config", lambda: config)
    token, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    monkeypatch.setenv("LANTAI_DESKTOP_TOKEN", token)
    monkeypatch.setenv("LANTAI_DESKTOP_INSTANCE_NONCE", nonce)
    return auth_file, {"X-Lantai-Desktop-Token": token}, nonce


def _request(method: str, path: str, headers=None):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=app_module.app,
                client=("127.0.0.1", 17423),
            ),
            base_url="http://127.0.0.1",
        ) as client:
            return await client.request(method, path, headers=headers)

    return asyncio.run(run())


def _registered_store():
    auth.register_user("isolated-desktop-auth", secrets.token_urlsafe(32))


@pytest.mark.parametrize("account_state", ["missing", "registered", "corrupt"])
def test_managed_desktop_probe_precedes_login_or_recovery(
    desktop_auth,
    account_state,
):
    auth_file, headers, nonce = desktop_auth
    if account_state == "registered":
        _registered_store()
    elif account_state == "corrupt":
        auth_file.write_bytes(b'{"user":')
    backup = auth_file.with_name(auth_file.name + ".bak")
    before = tuple(
        p.read_bytes() if p.exists() else None for p in (auth_file, backup)
    )

    response = _request("GET", "/api/desktop/readiness", headers)

    assert response.status_code == 200
    proof = response.json()
    assert proof["status"] == "ok" and proof["desktop"] is True
    assert proof["instance_nonce"] == nonce
    assert isinstance(proof["pid"], int) and proof["pid"] > 0
    assert (
        tuple(
            p.read_bytes() if p.exists() else None for p in (auth_file, backup)
        )
        == before
    )


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/desktop/readiness"),
        ("HEAD", "/api/desktop/readiness"),
        ("GET", "/api/desktop/readiness/extra"),
        ("GET", "/api/agents"),
    ],
)
def test_desktop_capability_is_not_a_business_login(
    desktop_auth,
    method,
    path,
):
    _, headers, _ = desktop_auth
    _registered_store()
    assert _request(method, path, headers).status_code == 401


@pytest.mark.parametrize(
    "invalid_probe",
    [
        "missing-header",
        "wrong-header",
        "query-only",
        "missing-instance",
        "missing-managed-token",
    ],
)
def test_startup_exception_requires_exact_managed_header(
    desktop_auth,
    monkeypatch,
    invalid_probe,
):
    _, headers, _ = desktop_auth
    _registered_store()
    path = "/api/desktop/readiness"
    if invalid_probe == "missing-header":
        headers = None
    elif invalid_probe == "wrong-header":
        headers = {"X-Lantai-Desktop-Token": secrets.token_urlsafe(32)}
    elif invalid_probe == "query-only":
        path += "?desktop_token=" + headers["X-Lantai-Desktop-Token"]
        headers = None
    elif invalid_probe == "missing-instance":
        monkeypatch.delenv("LANTAI_DESKTOP_INSTANCE_NONCE")
    else:
        monkeypatch.delenv("LANTAI_DESKTOP_TOKEN")
    assert _request("GET", path, headers).status_code == 401


@pytest.mark.parametrize("path", ["/", "/health"])
def test_corrupt_auth_store_leaves_login_shell_reachable(desktop_auth, path):
    auth_file, _, _ = desktop_auth
    original = b'{"user":'
    auth_file.write_bytes(original)
    assert _request("GET", path).status_code == 200
    assert auth_file.read_bytes() == original


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/auth/status"),
        ("POST", "/api/auth/register"),
        ("GET", "/api/agents"),
        ("POST", "/api/desktop/readiness"),
    ],
)
def test_corrupt_store_still_blocks_account_and_business_access(
    desktop_auth,
    method,
    path,
):
    auth_file, headers, _ = desktop_auth
    original = b'{"user":'
    auth_file.write_bytes(original)
    assert _request(method, path, headers).status_code == 503
    assert auth_file.read_bytes() == original


def test_managed_probe_does_not_open_first_run_business_api(desktop_auth):
    _, headers, _ = desktop_auth
    assert _request("GET", "/api/agents", headers).status_code == 403
