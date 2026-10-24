# -*- coding: utf-8 -*-
"""macOS computer permission helpers for the settings UI."""
from __future__ import annotations

import ctypes
import platform
import subprocess
import sys
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

PermissionKey = Literal[
    "accessibility",
    "full_disk_access",
    "screen_recording",
    "automation",
]
PermissionStatus = Literal["granted", "denied", "unknown", "unsupported"]


class ComputerPermissionItem(BaseModel):
    key: PermissionKey
    status: PermissionStatus
    can_check: bool
    can_request: bool
    settings_url: str | None = None


class ComputerPermissionStatus(BaseModel):
    platform: str
    is_macos: bool
    app_path: str | None = None
    permissions: list[ComputerPermissionItem]


class ComputerPermissionActionResult(BaseModel):
    ok: bool
    key: PermissionKey
    message: str


router = APIRouter(
    prefix="/settings/computer-permissions",
    tags=["computer-permissions"],
)

_MACOS_SETTINGS_URLS: dict[PermissionKey, str] = {
    "accessibility": (
        "x-apple.systempreferences:"
        "com.apple.preference.security?Privacy_Accessibility"
    ),
    "full_disk_access": (
        "x-apple.systempreferences:"
        "com.apple.preference.security?Privacy_AllFiles"
    ),
    "screen_recording": (
        "x-apple.systempreferences:"
        "com.apple.preference.security?Privacy_ScreenCapture"
    ),
    "automation": (
        "x-apple.systempreferences:"
        "com.apple.preference.security?Privacy_Automation"
    ),
}


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _find_app_bundle_path() -> str | None:
    """Return the containing .app bundle when running from packaged macOS app."""
    candidates = [Path(sys.executable)]
    try:
        candidates.append(Path(__file__).resolve())
    except OSError:
        pass

    for candidate in candidates:
        for parent in (candidate, *candidate.parents):
            if parent.suffix == ".app" and parent.is_dir():
                return str(parent)
    return None


def _check_accessibility() -> PermissionStatus:
    """Check Accessibility trust for the current process when possible.

    AXIsProcessTrusted only verifies the process that is serving this API.
    In development and helper-process launches, that process can differ from
    the app bundle the user toggled in System Settings. A false result is
    therefore not a reliable proof that the user denied the app permission.
    """
    if not _is_macos():
        return "unsupported"

    try:
        framework = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/"
            "ApplicationServices",
        )
        framework.AXIsProcessTrusted.restype = ctypes.c_bool
        return "granted" if bool(framework.AXIsProcessTrusted()) else "unknown"
    except Exception:
        return "unknown"


def _status_for_permission(key: PermissionKey) -> tuple[PermissionStatus, bool]:
    if not _is_macos():
        return "unsupported", False
    if key == "accessibility":
        status = _check_accessibility()
        return status, status == "granted"
    return "unknown", False


def _permission_item(key: PermissionKey) -> ComputerPermissionItem:
    status, can_check = _status_for_permission(key)
    return ComputerPermissionItem(
        key=key,
        status=status,
        can_check=can_check,
        can_request=_is_macos(),
        settings_url=_MACOS_SETTINGS_URLS.get(key) if _is_macos() else None,
    )


def _validate_key(key: str) -> PermissionKey:
    if key not in _MACOS_SETTINGS_URLS:
        raise HTTPException(status_code=404, detail="Unknown permission key")
    return key  # type: ignore[return-value]


def _open_settings_for_permission(key: PermissionKey) -> None:
    if not _is_macos():
        raise HTTPException(
            status_code=400,
            detail="Computer permission settings are only available on macOS.",
        )

    url = _MACOS_SETTINGS_URLS[key]
    try:
        subprocess.run(["open", url], check=True, timeout=8)
    except subprocess.SubprocessError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to open System Settings: {exc}",
        ) from exc


@router.get("", response_model=ComputerPermissionStatus)
async def get_computer_permissions() -> ComputerPermissionStatus:
    keys: list[PermissionKey] = [
        "accessibility",
        "full_disk_access",
        "screen_recording",
        "automation",
    ]
    return ComputerPermissionStatus(
        platform=platform.system() or sys.platform,
        is_macos=_is_macos(),
        app_path=_find_app_bundle_path(),
        permissions=[_permission_item(key) for key in keys],
    )


@router.post("/{key}/open", response_model=ComputerPermissionActionResult)
async def open_computer_permission_settings(
    key: str,
) -> ComputerPermissionActionResult:
    permission_key = _validate_key(key)
    _open_settings_for_permission(permission_key)
    return ComputerPermissionActionResult(
        ok=True,
        key=permission_key,
        message="System Settings opened.",
    )


@router.post("/{key}/request", response_model=ComputerPermissionActionResult)
async def request_computer_permission(
    key: str,
) -> ComputerPermissionActionResult:
    permission_key = _validate_key(key)
    _open_settings_for_permission(permission_key)
    return ComputerPermissionActionResult(
        ok=True,
        key=permission_key,
        message="Permission request opened.",
    )
