"""Deterministic, public-safe diagnostics for Office workflows."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from importlib import metadata
from typing import Any


_PYTHON_DEPENDENCIES = {
    "docx": ("python-docx", "DOCX structure and editing"),
    "openpyxl": ("openpyxl", "XLSX formulas and structure"),
    "pptx": ("python-pptx", "PPTX structure and editing"),
    "fitz": ("PyMuPDF", "PDF rendering and text extraction"),
    "pypdf": ("pypdf", "PDF text extraction fallback"),
    "pdfplumber": ("pdfplumber", "PDF layout-aware extraction"),
    "markitdown": ("markitdown", "document conversion fallback"),
}


def _dependency_status(
    module_name: str, distribution: str, purpose: str
) -> dict[str, Any]:
    available = importlib.util.find_spec(module_name) is not None
    try:
        version = metadata.version(distribution) if available else None
    except metadata.PackageNotFoundError:
        version = None
    return {
        "available": available,
        "version": version,
        "purpose": purpose,
    }


def _command_path(*names: str) -> str | None:
    return next((path for name in names if (path := shutil.which(name))), None)


def _kdocs_auth_status(command: str | None) -> tuple[bool, str]:
    """Probe keychain-backed auth state without exposing command output."""

    if command is None:
        return False, "command_unavailable"
    if os.environ.get("KDOCS_ACCESS_TOKEN"):
        return True, "environment"
    try:
        completed = subprocess.run(
            [command, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env={**os.environ, "NO_COLOR": "1"},
        )
    except (OSError, subprocess.SubprocessError):
        return False, "status_unavailable"

    raw = (completed.stdout or "").strip()
    diagnostic = f"{raw}\n{completed.stderr or ''}".casefold()
    try:
        payload = json.loads(raw) if raw.startswith(("{", "[")) else {}
    except (TypeError, ValueError):
        payload = {}
    status = (
        str(payload.get("status", "")).casefold()
        if isinstance(payload, dict)
        else ""
    )
    explicit = (
        payload.get("authenticated") if isinstance(payload, dict) else None
    )
    negative_markers = (
        "not authenticated",
        "not logged",
        "unauthenticated",
        "failed to decrypt",
        "credentials are reconfigured",
        "未登录",
        "未认证",
    )
    positive_markers = ("authenticated", "logged in", "已登录", "已认证")
    has_negative_diagnostic = any(
        marker in diagnostic for marker in negative_markers
    )
    authenticated = completed.returncode == 0 and (
        not has_negative_diagnostic
        and (
            explicit is True
            or status in {"authenticated", "ready", "ok"}
            or any(marker in diagnostic for marker in positive_markers)
        )
    )
    return (
        authenticated,
        "keychain_status" if authenticated else "not_authenticated",
    )


def collect_office_capabilities() -> dict[str, Any]:
    """Return feature readiness without leaking host paths or tokens."""

    dependencies = {
        distribution: _dependency_status(module, distribution, purpose)
        for module, (distribution, purpose) in _PYTHON_DEPENDENCIES.items()
    }
    libreoffice = _command_path("soffice") is not None or os.path.isfile(
        "/Applications/LibreOffice.app/Contents/MacOS/soffice"
    )
    officecli = _command_path("officecli") is not None
    kdocs_command = _command_path("kdocs-cli")
    kdocs_auth, kdocs_auth_source = _kdocs_auth_status(kdocs_command)
    features = {
        "docx_structured_read": dependencies["python-docx"]["available"],
        "pptx_structured_read": dependencies["python-pptx"]["available"],
        "xlsx_formula_read": dependencies["openpyxl"]["available"],
        "xlsx_formula_recalculation": libreoffice,
        "office_visual_conversion": libreoffice or officecli,
        "pdf_text_extraction": any(
            dependencies[name]["available"]
            for name in ("PyMuPDF", "pypdf", "pdfplumber", "markitdown")
        ),
        "kdocs_cli": kdocs_command is not None and kdocs_auth,
    }
    unavailable = sorted(name for name, ready in features.items() if not ready)
    return {
        "schema_version": "lantai_office_capabilities_v1",
        "status": "ready" if not unavailable else "degraded",
        "dependencies": dependencies,
        "converters": {
            "libreoffice": {"available": libreoffice},
            "officecli": {"available": officecli},
        },
        "connectors": {
            "kdocs": {
                "mode": "cli",
                "command_available": kdocs_command is not None,
                "authenticated": kdocs_auth,
                "auth_source": kdocs_auth_source,
                "ready": kdocs_command is not None and kdocs_auth,
            },
        },
        "features": features,
        "unavailable_features": unavailable,
    }
