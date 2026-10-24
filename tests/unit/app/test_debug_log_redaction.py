# -*- coding: utf-8 -*-
"""Tests for local debug-log redaction."""

from __future__ import annotations

import pytest
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_backend_debug_logs_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lantai.app.routers import console as console_router

    monkeypatch.delenv("LANTAI_ENABLE_DEBUG_LOG_ENDPOINT", raising=False)

    with pytest.raises(HTTPException) as exc_info:
        await console_router.get_backend_debug_logs()

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_backend_debug_logs_redact_content_and_absolute_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from lantai.app.routers import console as console_router

    log_path = tmp_path / "backend.log"
    log_path.write_text(
        (
            "Authorization: Bearer sk-secret1234567890abcdef "
            "email=sam@example.com path=/Users/example/private/file.txt?token=abc"
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LANTAI_ENABLE_DEBUG_LOG_ENDPOINT", "1")
    monkeypatch.setattr(console_router, "LOG_FILE_PATH", log_path)

    result = await console_router.get_backend_debug_logs(lines=20)

    assert result["path"] == "backend.log"
    assert "[redacted" in result["content"]
    assert "sk-secret1234567890abcdef" not in result["content"]
    assert "sam@example.com" not in result["content"]
    assert "/Users/sam" not in result["content"]
    assert "token=abc" not in result["content"]


@pytest.mark.asyncio
async def test_desktop_debug_logs_use_electron_owned_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from lantai.app.routers import console as console_router

    log_path = tmp_path / "backend.log"
    log_path.write_text("desktop lifecycle log", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LANTAI_ENABLE_DEBUG_LOG_ENDPOINT", "1")
    monkeypatch.setenv("LANTAI_DESKTOP_APP", "1")
    monkeypatch.setenv("LANTAI_DESKTOP_LOG_PATH", str(log_path))

    result = await console_router.get_backend_debug_logs(lines=20)

    assert result["path"] == "backend.log"
    assert result["content"] == "desktop lifecycle log"


def test_desktop_startup_skips_duplicate_python_file_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lantai.app import _app

    calls = []
    monkeypatch.setenv("LANTAI_DESKTOP_APP", "1")
    monkeypatch.setattr(
        _app,
        "add_project_file_handler",
        lambda path: calls.append(path),
    )

    assert _app._configure_project_file_logging() is False
    assert calls == []
