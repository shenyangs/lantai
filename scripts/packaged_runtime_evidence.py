"""Shared validation for packaged Runtime completeness evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

SCHEMA = "com.lantai.packaged-runtime-completeness-smoke.v2"
REQUIRED_ARTIFACTS = (
    "app_executable",
    "app_asar",
    "backend_executable",
)
REQUIRED_STABLE_PRESENTATIONS = ("pptx-zh", "pptx-en")
REQUIRED_PDF_RENDERERS = ("pdf-zh", "pdf-en")
REQUIRED_REMOVED_ENV = ("NODE_PATH", "PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_payload(
    payload: dict[str, Any],
    *,
    expected_source_sha: str,
    expected_version: str,
    app: Path,
) -> list[str]:
    issues: list[str] = []
    if payload.get("schema") != SCHEMA:
        issues.append("schema_mismatch")
    if payload.get("status") != "ok":
        issues.append("status_not_ok")
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    if source.get("git_sha") != expected_source_sha:
        issues.append("source_sha_mismatch")
    app_evidence = payload.get("app") if isinstance(payload.get("app"), dict) else {}
    if app_evidence.get("version") != expected_version:
        issues.append("version_mismatch")

    artifacts = (
        payload.get("artifacts") if isinstance(payload.get("artifacts"), dict) else {}
    )
    for artifact_id in REQUIRED_ARTIFACTS:
        entry = artifacts.get(artifact_id)
        if not isinstance(entry, dict):
            issues.append(f"artifact_missing:{artifact_id}")
            continue
        relative = Path(str(entry.get("path") or ""))
        if relative.is_absolute() or ".." in relative.parts:
            issues.append(f"artifact_path_invalid:{artifact_id}")
            continue
        target = app / relative
        if not target.is_file():
            issues.append(f"artifact_file_missing:{artifact_id}")
        elif entry.get("sha256") != _sha256_file(target):
            issues.append(f"artifact_hash_mismatch:{artifact_id}")

    for probe_name in (
        "runtime_import_probe",
        "skill_import_probe",
        "node_import_probe",
    ):
        probe = payload.get(probe_name)
        if not isinstance(probe, dict) or probe.get("failed_count") != 0:
            issues.append(f"probe_failed:{probe_name}")
    runtime_probe = payload.get("runtime_import_probe") or {}
    if (
        int(runtime_probe.get("module_count") or 0) <= 0
        or int(runtime_probe.get("cli_help_count") or 0) <= 0
    ):
        issues.append("lazy_cli_help_missing")
    skill_probe = payload.get("skill_import_probe") or {}
    if (
        int(skill_probe.get("script_count") or 0) <= 0
        or int(skill_probe.get("import_count") or 0) <= 0
    ):
        issues.append("skill_import_inventory_empty")
    node_probe = payload.get("node_import_probe") or {}
    if int(node_probe.get("script_count") or 0) <= 0:
        issues.append("node_import_inventory_empty")
    if int(payload.get("shell_syntax_count") or 0) <= 0:
        issues.append("shell_syntax_inventory_empty")

    parity = payload.get("skill_resource_parity")
    if not isinstance(parity, dict):
        issues.append("skill_resource_parity_missing")
    elif (
        int(parity.get("source_count") or 0) <= 0
        or parity.get("source_count") != parity.get("packaged_count")
        or not parity.get("source_manifest_sha256")
        or not parity.get("packaged_manifest_sha256")
        or parity.get("bundle_unchanged_after_probes") is not True
    ):
        issues.append("skill_resource_parity_invalid")

    stable = (
        payload.get("stable_presentations")
        if isinstance(payload.get("stable_presentations"), dict)
        else {}
    )
    for locale in REQUIRED_STABLE_PRESENTATIONS:
        entry = stable.get(locale)
        if (
            not isinstance(entry, dict)
            or int(entry.get("slide_count") or 0) <= 0
            or not entry.get("sha256")
        ):
            issues.append(f"stable_presentation_missing:{locale}")

    pdf_renderers = (
        payload.get("pdf_renderers")
        if isinstance(payload.get("pdf_renderers"), dict)
        else {}
    )
    for locale in REQUIRED_PDF_RENDERERS:
        entry = pdf_renderers.get(locale)
        if (
            not isinstance(entry, dict)
            or int(entry.get("page_count") or 0) <= 0
            or int(entry.get("width") or 0) <= 0
            or int(entry.get("height") or 0) <= 0
            or not entry.get("sha256")
        ):
            issues.append(f"pdf_renderer_missing:{locale}")

    sanitized = (
        payload.get("sanitized_environment")
        if isinstance(payload.get("sanitized_environment"), dict)
        else {}
    )
    removed = set(sanitized.get("removed") or [])
    if (
        sanitized.get("clean_path") is not True
        or sanitized.get("dont_write_bytecode") is not True
        or sanitized.get("isolated_probe_cwd") is not True
        or not set(REQUIRED_REMOVED_ENV).issubset(removed)
    ):
        issues.append("sanitized_environment_invalid")
    return issues


def load_and_validate(
    path: Path,
    *,
    expected_source_sha: str,
    expected_version: str,
    app: Path,
) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"Packaged Runtime evidence is missing: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("Packaged Runtime evidence must be a JSON object")
    issues = validate_payload(
        payload,
        expected_source_sha=expected_source_sha,
        expected_version=expected_version,
        app=app,
    )
    if issues:
        raise ValueError("Packaged Runtime evidence invalid: " + ", ".join(issues))
    return payload
