#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Audit non-secret official-release evidence for runtime migration verification.

This helper is intentionally read-only. It does not build, notarize, upload, or
launch an app. The maintained product is the official Developer ID edition.
Archived Mac App Store evidence is available only through an explicit
compatibility flag and never participates in the default release gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

try:
    from scripts.packaged_runtime_evidence import load_and_validate
except ModuleNotFoundError:  # direct script execution from scripts/
    from packaged_runtime_evidence import load_and_validate


SCHEMA = "com.lantai.release-evidence-readiness.v1"
PACKAGED_LIFECYCLE_SMOKE_SCHEMA = "com.lantai.packaged-app-lifecycle-smoke.v1"
PACKAGED_RAPID_LIFECYCLE_SMOKE_SCHEMA = (
    "com.lantai.packaged-app-rapid-lifecycle-smoke.v1"
)
PACKAGED_CUSTOMIZATION_UPGRADE_SMOKE_SCHEMA = (
    "com.lantai.packaged-customization-upgrade-smoke.v1"
)
MAS_BOOKMARK_SMOKE_SCHEMA = "com.lantai.mas-security-scoped-bookmark-smoke.v1"
RUNTIME_MIGRATION_BACKUP_RESTORE_SMOKE_SCHEMA = (
    "com.lantai.runtime-migration-backup-restore-smoke.v1"
)
LEGACY_WRITER_OBSERVATION_SUMMARY_SCHEMA = (
    "com.lantai.runtime-legacy-writer-observation-summary.v1"
)
LEGACY_BOUNDARY_OBSERVATION_SUMMARY_SCHEMA = (
    "com.lantai.runtime-legacy-boundary-observation-summary.v1"
)
RUNTIME_MIGRATION_REPORT_SCHEMA_VERSION = 2
PACKAGED_RUNTIME_COMPLETENESS_CHECK_ID = (
    "official.packaged_runtime_completeness.evidence"
)
REQUIRED_LIFECYCLE_CHECKS = {
    "desktop_readiness",
    "distribution_tools",
    "browser_capabilities",
    "packaged_customization_integrity",
    "reminder_delivery_worker",
    "legacy_writer_observation_summary",
    "legacy_boundary_observation_summary",
    "runtime_restart_decision_projection",
    "loop_trial_recovery_export",
    "browser_content_trial_handoff",
    "browser_content_renderer_export",
    "browser_preflight_failure_runtime_settlement",
    "browser_pause_resume",
    "task_center_multi_window_stale_control",
    "console_model_success_path",
    "runtime_event_sse_reconnect",
    "chat_renderer_stream_reconnect",
    "chat_renderer_basic_ui_send",
    "chat_renderer_provider_stream_ui",
    "chat_renderer_rapid_queue_ui",
    "chat_renderer_stop_resend_ui",
    "chat_renderer_stream_route_switch_ui",
    "chat_renderer_backend_model_roundtrip",
    "chat_renderer_basic_experience_matrix",
    "terminal_interrupt_noop",
    "canonical_runtime_controls",
    "backend_sigkill_restart",
    "post_restart_customization_integrity",
    "post_restart_reminder_delivery_dedupe",
    "post_restart_runtime_projection",
    "post_restart_loop_export",
    "post_restart_browser_content_renderer_export",
    "post_restart_chat_stream_reconnect",
    "post_restart_chat_basic_ui_send",
    "post_restart_chat_provider_stream_ui",
    "post_restart_chat_rapid_queue_ui",
    "post_restart_chat_stop_resend_ui",
    "post_restart_chat_stream_route_switch_ui",
    "post_restart_chat_backend_model_roundtrip",
    "post_restart_chat_basic_experience_matrix",
    "post_restart_canonical_runtime_controls",
    "electron_app_relaunch",
    "post_app_relaunch_customization_integrity",
    "post_app_relaunch_chat_history",
    "post_app_relaunch_terminal_interrupt_noop",
    "post_app_relaunch_canonical_runtime_controls",
    "post_app_relaunch_chat_basic_ui_send",
    "post_app_relaunch_chat_provider_stream_ui",
}
REQUIRED_APP_RELAUNCH_RUNTIME_CONTROL_FIELDS = {
    "terminal_interrupt_noop",
    "canonical_runtime_controls",
    "archive_delete_cleanup",
}
REQUIRED_BASIC_EXPERIENCE_CHECKS = {
    "initial_empty_chat_dom",
    "initial_empty_chat_backend",
    "first_user_feedback_within_budget",
    "first_assistant_feedback_within_budget",
    "second_user_feedback_within_budget",
    "second_assistant_feedback_within_budget",
    "two_primary_answers_with_optional_persona_lines",
    "no_empty_assistant_bubbles",
    "no_duplicate_assistant_chrome",
    "textarea_cleared_after_submit",
    "backend_state_consistent_with_submission_mode",
    "backend_pending_cleared",
    "route_isolation_empty_dom",
    "route_isolation_empty_backend",
    "route_restore_preserved_dom",
    "route_restore_preserved_backend",
    "final_dom_stable",
}
REQUIRED_BOOKMARK_CHECKS = {
    "picker_security_scoped_bookmark",
    "safe_storage_encrypted",
    "no_raw_bookmark_or_path",
    "restore_after_restart",
    "upload_after_restore",
    "stale_bookmark_rejected",
}
REQUIRED_RUNTIME_MIGRATION_WRITER_IDS = {
    "legacy_runtime_event_ledger_writer",
    "legacy_task_run_snapshot_writer",
    "legacy_evaluation_report_writer",
    "legacy_mission_state_writer",
    "legacy_browser_task_state_writer",
}
REQUIRED_RUNTIME_MIGRATION_BACKUP_RESTORE_CHECKS = {
    "fixture_legacy_sources_seeded",
    "dry_run_report_side_effect_free",
    "legacy_sources_backed_up_to_scratch",
    "legacy_sources_restored_from_scratch_backup",
    "restored_report_matches_original_signatures",
    "downgrade_readers_parse_restored_legacy_sources",
}
REQUIRED_LEGACY_WRITER_OBSERVATION_EDITIONS = {"official", "mas"}
REQUIRED_APP_ARTIFACT_HASH_IDS = {
    "main_executable",
    "app_asar",
    "backend_executable",
}
LEGACY_WRITER_OBSERVATION_EVIDENCE_FILES = (
    "runtime-legacy-writer-observation-summary.json",
    "legacy-writer-observation-summary.json",
    "close01-runtime-legacy-writer-observation-summary.json",
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    missing: bool = False

    @property
    def combined_output(self) -> str:
        return "\n".join(part for part in (self.stdout, self.stderr) if part)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _run_command(args: list[str], *, timeout: float = 20.0) -> CommandResult:
    executable = shutil.which(args[0]) if args else None
    if not executable:
        return CommandResult(127, stderr=f"missing command: {args[0]}", missing=True)
    try:
        proc = subprocess.run(
            [executable, *args[1:]],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            124,
            stdout=exc.stdout or "",
            stderr=exc.stderr or f"timed out after {timeout}s",
        )
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def _check(
    check_id: str,
    *,
    status: str,
    summary: str,
    required_for_final: bool = True,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "status": status,
        "required_for_final": required_for_final,
        "summary": summary,
        "details": details or {},
    }


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _check_packaged_runtime_completeness_evidence(
    path: Path | None,
    *,
    expected_source_sha: str,
    expected_version: str,
    app: Path,
) -> dict[str, Any]:
    if path is None or not path.is_file():
        return _check(
            PACKAGED_RUNTIME_COMPLETENESS_CHECK_ID,
            status="missing",
            summary="Packaged Runtime completeness evidence is missing",
            details={"path": str(path) if path else None},
        )
    if not expected_source_sha:
        return _check(
            PACKAGED_RUNTIME_COMPLETENESS_CHECK_ID,
            status="failed",
            summary="Packaged Runtime evidence has no expected source SHA",
            details={"path": str(path)},
        )
    try:
        payload = load_and_validate(
            path,
            expected_source_sha=expected_source_sha,
            expected_version=expected_version,
            app=app,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return _check(
            PACKAGED_RUNTIME_COMPLETENESS_CHECK_ID,
            status="failed",
            summary="Packaged Runtime completeness evidence is invalid",
            details={"path": str(path), "error": str(exc)},
        )
    return _check(
        PACKAGED_RUNTIME_COMPLETENESS_CHECK_ID,
        status="passed",
        summary="Packaged Runtime completeness evidence is bound and complete",
        details={
            "path": str(path),
            "schema": payload.get("schema"),
            "source_sha": payload.get("source", {}).get("git_sha"),
            "artifact_hashes": payload.get("artifacts"),
        },
    )


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _load_package(root: Path) -> dict[str, str]:
    package_path = root / "console" / "package.json"
    package = _read_json(package_path)
    build = package.get("build", {}) if isinstance(package, dict) else {}
    return {
        "name": str(package.get("name") or ""),
        "version": str(package.get("version") or ""),
        "product_name": str(build.get("productName") or "澜台"),
    }


def _latest_existing_path(paths: Iterable[Path]) -> Path | None:
    candidates: list[tuple[int, str, Path]] = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        if path.is_file():
            candidates.append((stat.st_mtime_ns, str(path), path))
    if not candidates:
        return None
    return max(candidates)[2]


def _evidence_directories(root: Path) -> list[Path]:
    directories = [root / "build/release-evidence"]
    release_builds = root / "release/builds"
    if release_builds.is_dir():
        directories.extend(
            sorted(
                path
                for path in release_builds.glob("*/release-evidence")
                if path.is_dir()
            )
        )
    return directories


def _find_latest_evidence(root: Path, file_names: Iterable[str]) -> Path | None:
    names = tuple(file_names)
    if not names:
        return None
    candidates = (
        directory / name
        for directory in _evidence_directories(root)
        for name in names
    )
    return _latest_existing_path(candidates)


def _explicit_evidence_paths(value: Any) -> list[Path]:
    if value is None:
        return []
    if isinstance(value, Path):
        return [value]
    if isinstance(value, str):
        return [Path(value)]
    return [Path(item) for item in value]


def _default_arch() -> str:
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    if machine in {"x86_64", "amd64"}:
        return "x64"
    return machine or "unknown"


def _extract_authorities(output: str) -> list[str]:
    return [
        match.group(1).strip()
        for match in re.finditer(r"^Authority=(.+)$", output, flags=re.MULTILINE)
    ]


def _extract_codesign_field(output: str, field: str) -> str | None:
    match = re.search(rf"^{re.escape(field)}=(.+)$", output, flags=re.MULTILINE)
    return match.group(1).strip() if match else None


def _read_app_info(app_path: Path) -> dict[str, Any] | None:
    info_path = app_path / "Contents" / "Info.plist"
    if not info_path.is_file():
        return None
    with info_path.open("rb") as handle:
        info = plistlib.load(handle)
    return info if isinstance(info, dict) else None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _app_artifact_sha256(app_path: Path) -> dict[str, str | None]:
    info = _read_app_info(app_path) or {}
    executable_name = str(info.get("CFBundleExecutable") or "")
    artifact_paths = {
        "main_executable": app_path / "Contents" / "MacOS" / executable_name,
        "app_asar": app_path / "Contents" / "Resources" / "app.asar",
        "backend_executable": (
            app_path
            / "Contents"
            / "Resources"
            / "lantai-server"
            / "Lantai Backend.app"
            / "Contents"
            / "MacOS"
            / "lantai-server"
        ),
    }
    result: dict[str, str | None] = {}
    for artifact_id, artifact_path in artifact_paths.items():
        if artifact_path.is_symlink() or not artifact_path.is_file():
            result[artifact_id] = None
            continue
        result[artifact_id] = _sha256_file(artifact_path)
    return result


def _parse_entitlements(output: str) -> dict[str, Any]:
    start = output.find("<?xml")
    if start < 0:
        start = output.find("<plist")
    if start < 0:
        return {}
    end = output.find("</plist>", start)
    if end < 0:
        return {}
    plist_text = output[start : end + len("</plist>")]
    try:
        parsed = plistlib.loads(plist_text.encode("utf-8"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _summarize_spctl(result: CommandResult) -> dict[str, Any]:
    output = result.combined_output
    source_match = re.search(r"^source=(.+)$", output, flags=re.MULTILINE)
    origin_match = re.search(r"^origin=(.+)$", output, flags=re.MULTILINE)
    return {
        "returncode": result.returncode,
        "accepted": result.returncode == 0 and "accepted" in output,
        "source": source_match.group(1).strip() if source_match else None,
        "origin": origin_match.group(1).strip() if origin_match else None,
    }


def _check_app(
    check_id: str,
    app_path: Path,
    *,
    expected_bundle_id: str,
    authority_prefixes: tuple[str, ...],
    require_notarized_gatekeeper: bool = False,
    require_sandbox: bool = False,
    required_for_final: bool = True,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    if not app_path.exists():
        return [
            _check(
                check_id,
                status="missing",
                summary=f"App bundle not found: {app_path}",
                required_for_final=required_for_final,
            )
        ]
    if not app_path.is_dir():
        return [
            _check(
                check_id,
                status="failed",
                summary=f"Path is not an app bundle directory: {app_path}",
                required_for_final=required_for_final,
            )
        ]

    info = _read_app_info(app_path)
    bundle_id = str((info or {}).get("CFBundleIdentifier") or "")
    if bundle_id != expected_bundle_id:
        checks.append(
            _check(
                f"{check_id}.bundle_id",
                status="failed",
                summary=f"Expected bundle id {expected_bundle_id}, got {bundle_id or '<missing>'}",
                required_for_final=required_for_final,
                details={"path": str(app_path), "bundle_id": bundle_id},
            )
        )
    else:
        checks.append(
            _check(
                f"{check_id}.bundle_id",
                status="passed",
                summary=f"Bundle id is {bundle_id}",
                required_for_final=required_for_final,
                details={"path": str(app_path), "bundle_id": bundle_id},
            )
        )

    verify = _run_command(["codesign", "--verify", "--deep", "--strict", str(app_path)])
    describe = _run_command(["codesign", "-dv", "--verbose=4", str(app_path)])
    output = describe.combined_output
    authorities = _extract_authorities(output)
    expected_authority = any(
        authority.startswith(prefix)
        for authority in authorities
        for prefix in authority_prefixes
    )
    signature_ok = verify.returncode == 0 and expected_authority
    checks.append(
        _check(
            f"{check_id}.signature",
            status="passed" if signature_ok else "failed",
            summary=(
                "Signature matches expected authority"
                if signature_ok
                else "Signature does not match expected authority"
            ),
            required_for_final=required_for_final,
            details={
                "path": str(app_path),
                "verify_returncode": verify.returncode,
                "authorities": authorities,
                "team_identifier": _extract_codesign_field(output, "TeamIdentifier"),
                "notarization_ticket": _extract_codesign_field(
                    output,
                    "Notarization Ticket",
                ),
            },
        )
    )

    if require_notarized_gatekeeper:
        spctl = _run_command(
            ["spctl", "--assess", "--type", "execute", "--verbose=4", str(app_path)]
        )
        spctl_summary = _summarize_spctl(spctl)
        notarized = (
            spctl_summary["accepted"]
            and spctl_summary.get("source") == "Notarized Developer ID"
        )
        checks.append(
            _check(
                f"{check_id}.gatekeeper_notarized",
                status="passed" if notarized else "failed",
                summary=(
                    "Gatekeeper accepts the app as Notarized Developer ID"
                    if notarized
                    else "Gatekeeper does not accept the app as Notarized Developer ID"
                ),
                required_for_final=required_for_final,
                details=spctl_summary,
            )
        )

    if require_sandbox:
        entitlements_result = _run_command(
            ["codesign", "-d", "--entitlements", ":-", str(app_path)]
        )
        entitlements = _parse_entitlements(entitlements_result.combined_output)
        sandboxed = entitlements.get("com.apple.security.app-sandbox") is True
        checks.append(
            _check(
                f"{check_id}.sandbox",
                status="passed" if sandboxed else "failed",
                summary=(
                    "App Sandbox entitlement is present"
                    if sandboxed
                    else "App Sandbox entitlement is missing"
                ),
                required_for_final=required_for_final,
                details={
                    "path": str(app_path),
                    "entitlements_returncode": entitlements_result.returncode,
                    "has_application_groups": bool(
                        entitlements.get("com.apple.security.application-groups")
                    ),
                    "has_security_scoped_user_selected_files": (
                        entitlements.get("com.apple.security.files.user-selected.read-write")
                        is True
                        or entitlements.get(
                            "com.apple.security.files.user-selected.read-only"
                        )
                        is True
                    ),
                },
            )
        )

    return checks


def _check_paths_present(
    check_id: str,
    paths: Iterable[Path],
    *,
    required_for_final: bool = True,
) -> dict[str, Any]:
    details = {
        str(path): {
            "exists": path.exists(),
            "is_file": path.is_file(),
            "is_dir": path.is_dir(),
            "bytes": path.stat().st_size if path.is_file() else None,
        }
        for path in paths
    }
    passed = all(item["exists"] for item in details.values())
    return _check(
        check_id,
        status="passed" if passed else "missing",
        summary="All expected artifacts are present" if passed else "Some artifacts are missing",
        required_for_final=required_for_final,
        details=details,
    )


def _check_pkg_signature(pkg_path: Path) -> dict[str, Any]:
    if not pkg_path.exists():
        return _check(
            "mas.download_pkg.signature",
            status="missing",
            summary=f"MAS pkg not found: {pkg_path}",
            details={"path": str(pkg_path)},
        )
    result = _run_command(["pkgutil", "--check-signature", str(pkg_path)])
    output = result.combined_output
    signed = result.returncode == 0 and "3rd Party Mac Developer Installer" in output
    return _check(
        "mas.download_pkg.signature",
        status="passed" if signed else "failed",
        summary=(
            "MAS pkg is signed by a 3rd Party Mac Developer Installer identity"
            if signed
            else "MAS pkg is not signed by the expected installer identity"
        ),
        details={
            "path": str(pkg_path),
            "returncode": result.returncode,
            "has_installer_authority": "3rd Party Mac Developer Installer" in output,
        },
    )


def _notary_credential_mode(env: dict[str, str]) -> tuple[str | None, list[str]]:
    if env.get("LANTAI_NOTARY_KEYCHAIN_PROFILE") or env.get("APPLE_KEYCHAIN_PROFILE"):
        names = [
            name
            for name in ("LANTAI_NOTARY_KEYCHAIN_PROFILE", "APPLE_KEYCHAIN_PROFILE")
            if env.get(name)
        ]
        if env.get("LANTAI_NOTARY_KEYCHAIN") or env.get("APPLE_KEYCHAIN"):
            names.extend(
                name
                for name in ("LANTAI_NOTARY_KEYCHAIN", "APPLE_KEYCHAIN")
                if env.get(name)
            )
        return "keychain_profile", names
    apple_id_names = ("APPLE_ID", "APPLE_APP_SPECIFIC_PASSWORD", "APPLE_TEAM_ID")
    if all(env.get(name) for name in apple_id_names):
        return "apple_id_password", list(apple_id_names)
    api_names = ("APPLE_API_KEY", "APPLE_API_KEY_ID", "APPLE_API_ISSUER")
    if all(env.get(name) for name in api_names):
        return "app_store_connect_api_key", list(api_names)
    return None, []


_NOTARY_REDACT_ENV_NAMES = (
    "LANTAI_NOTARY_KEYCHAIN_PROFILE",
    "APPLE_KEYCHAIN_PROFILE",
    "LANTAI_NOTARY_KEYCHAIN",
    "APPLE_KEYCHAIN",
    "APPLE_ID",
    "APPLE_APP_SPECIFIC_PASSWORD",
    "APPLE_TEAM_ID",
    "APPLE_API_KEY",
    "APPLE_API_KEY_ID",
    "APPLE_API_ISSUER",
)


def _redact_notary_output(text: str, env: dict[str, str]) -> str:
    redacted = text
    for name in _NOTARY_REDACT_ENV_NAMES:
        value = env.get(name)
        if value and len(value) >= 3:
            redacted = redacted.replace(value, f"<redacted:{name}>")
    return redacted


def _notarytool_history_command(env: dict[str, str], mode: str) -> list[str]:
    command = ["xcrun", "notarytool", "history"]
    if mode == "keychain_profile":
        keychain = env.get("LANTAI_NOTARY_KEYCHAIN") or env.get("APPLE_KEYCHAIN")
        if keychain:
            command.extend(["--keychain", keychain])
        profile = env.get("LANTAI_NOTARY_KEYCHAIN_PROFILE") or env.get(
            "APPLE_KEYCHAIN_PROFILE",
        )
        command.extend(["--keychain-profile", str(profile)])
        return command
    if mode == "apple_id_password":
        command.extend(
            [
                "--apple-id",
                str(env.get("APPLE_ID")),
                "--password",
                str(env.get("APPLE_APP_SPECIFIC_PASSWORD")),
                "--team-id",
                str(env.get("APPLE_TEAM_ID")),
            ],
        )
        return command
    if mode == "app_store_connect_api_key":
        command.extend(
            [
                "--key",
                str(env.get("APPLE_API_KEY")),
                "--key-id",
                str(env.get("APPLE_API_KEY_ID")),
                "--issuer",
                str(env.get("APPLE_API_ISSUER")),
            ],
        )
        return command
    raise ValueError(f"Unsupported notary credential mode: {mode}")


def _classify_notarytool_issue(result: CommandResult) -> str:
    output = result.combined_output.lower()
    if result.missing:
        return "notarytool_missing"
    if result.returncode == 124:
        return "notarytool_timeout"
    if "required agreement" in output or (
        "agreement" in output and "expired" in output
    ):
        return "apple_agreement_missing_or_expired"
    if "http status code: 403" in output:
        return "apple_notary_forbidden"
    if "http status code: 401" in output or "authentication" in output:
        return "notary_authentication_failed"
    return "notarytool_history_failed"


def _check_notary_credentials(
    env: dict[str, str],
    *,
    verify_live: bool = False,
) -> dict[str, Any]:
    mode, present_names = _notary_credential_mode(env)
    details: dict[str, Any] = {
        "mode": mode,
        "present_environment_names": present_names,
        "live_verification": {"enabled": bool(verify_live)},
    }
    if not mode:
        return _check(
            "official.notary_credentials.available",
            status="missing",
            summary="No notary credential environment group is present",
            required_for_final=False,
            details=details,
        )
    if not verify_live:
        return _check(
            "official.notary_credentials.available",
            status="passed",
            summary=f"Notary credentials are available via {mode}",
            required_for_final=False,
            details=details,
        )

    command = _notarytool_history_command(env, mode)
    result = _run_command(command, timeout=30.0)
    verified = result.returncode == 0
    redacted_output = _redact_notary_output(result.combined_output, env).strip()
    details["live_verification"] = {
        "enabled": True,
        "verified": verified,
        "returncode": result.returncode,
        "issue": None if verified else _classify_notarytool_issue(result),
        "redacted_output": redacted_output[:1000],
    }
    return _check(
        "official.notary_credentials.available",
        status="passed" if verified else "failed",
        summary=(
            "Notary credentials passed live validation"
            if verified
            else "Notary credentials failed live validation"
        ),
        required_for_final=False,
        details=details,
    )


def _check_codesigning_identities() -> dict[str, Any]:
    result = _run_command(["security", "find-identity", "-v"])
    output = result.combined_output
    classes = {
        "developer_id_application": output.count("Developer ID Application:"),
        "apple_distribution": output.count("Apple Distribution:"),
        "apple_development": output.count("Apple Development:")
        + output.count("Mac Developer:"),
        "third_party_mac_developer_installer": output.count(
            "3rd Party Mac Developer Installer:"
        ),
    }
    available = any(count > 0 for count in classes.values())
    return _check(
        "local.codesigning_identities.available",
        status="passed" if available else "missing",
        summary=(
            "At least one relevant signing identity is available"
            if available
            else "No relevant signing identity was found"
        ),
        required_for_final=False,
        details={"returncode": result.returncode, "identity_class_counts": classes},
    )


def _find_installed_mas_app(explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit
    result = _run_command(
        ["mdfind", 'kMDItemCFBundleIdentifier == "com.lantai.appstore"'],
        timeout=10,
    )
    if result.returncode != 0:
        return None
    for raw in result.stdout.splitlines():
        candidate = Path(raw.strip())
        if candidate.is_dir() and candidate.suffix == ".app" and str(candidate).startswith(
            "/Applications/"
        ):
            return candidate
    return None


def _check_mas_installed_app_receipt(app_path: Path) -> dict[str, Any]:
    receipt_path = app_path / "Contents" / "_MASReceipt" / "receipt"
    is_symlink = receipt_path.is_symlink()
    is_file = receipt_path.is_file() and not is_symlink
    size = receipt_path.stat().st_size if is_file else 0
    passed = is_file and size > 0
    return _check(
        "mas.installed_app.app_store_receipt",
        status="passed" if passed else "failed",
        summary=(
            "Installed MAS app contains a non-empty App Store receipt"
            if passed
            else "Installed MAS app does not contain a valid App Store receipt"
        ),
        details={
            "app_path": str(app_path),
            "receipt_path": str(receipt_path),
            "exists": receipt_path.exists(),
            "is_file": is_file,
            "is_symlink": is_symlink,
            "bytes": size,
            "requirement": (
                "Mac TestFlight apps always contain an App Store receipt; a copied "
                "distribution bundle is not installed-app evidence"
            ),
        },
    )


def _check_bookmark_evidence(path: Path | None) -> dict[str, Any]:
    if path is None:
        return _check(
            "mas.security_scoped_bookmark_smoke.evidence",
            status="missing",
            summary="No MAS security-scoped bookmark smoke evidence file was provided",
            details={
                "expected_fields": [
                    f"schema={MAS_BOOKMARK_SMOKE_SCHEMA}",
                    "distribution=mas",
                    "passed=true",
                    "bookmark_restored_after_restart=true",
                    "stale_bookmark_rejected=true",
                    "sensitive_fields_present=false",
                ],
                "required_checks": sorted(REQUIRED_BOOKMARK_CHECKS),
            },
        )
    if not path.is_file():
        return _check(
            "mas.security_scoped_bookmark_smoke.evidence",
            status="missing",
            summary=f"MAS bookmark smoke evidence file not found: {path}",
            details={"path": str(path)},
        )
    try:
        payload = _read_json(path)
    except Exception as exc:
        return _check(
            "mas.security_scoped_bookmark_smoke.evidence",
            status="failed",
            summary=f"MAS bookmark smoke evidence is not valid JSON: {exc}",
            details={"path": str(path)},
        )
    checks = set(payload.get("checks") or []) if isinstance(payload, dict) else set()
    missing_checks = sorted(REQUIRED_BOOKMARK_CHECKS - checks)
    security_scope_ref = (
        str(payload.get("security_scope_ref") or "") if isinstance(payload, dict) else ""
    )
    bookmark_hash = (
        str(payload.get("security_scope_bookmark_sha256") or "")
        if isinstance(payload, dict)
        else ""
    )
    passed = False
    if isinstance(payload, dict):
        passed = (
            payload.get("schema") == MAS_BOOKMARK_SMOKE_SCHEMA
            and payload.get("passed") is True
            and payload.get("distribution") == "mas"
            and payload.get("bookmark_restored_after_restart") is True
            and payload.get("stale_bookmark_rejected") is True
            and not missing_checks
            and security_scope_ref.startswith("browser-security-scope:")
            and bookmark_hash.startswith("sha256:")
            and payload.get("sensitive_fields_present") is False
        )
    return _check(
        "mas.security_scoped_bookmark_smoke.evidence",
        status="passed" if passed else "failed",
        summary=(
            "MAS bookmark restore/stale evidence is present"
            if passed
            else "MAS bookmark evidence does not prove restore and stale rejection"
        ),
        details={
            "path": str(path),
            "expected_schema": MAS_BOOKMARK_SMOKE_SCHEMA,
            "schema": payload.get("schema") if isinstance(payload, dict) else None,
            "passed": payload.get("passed") if isinstance(payload, dict) else None,
            "distribution": payload.get("distribution") if isinstance(payload, dict) else None,
            "bookmark_restored_after_restart": (
                payload.get("bookmark_restored_after_restart")
                if isinstance(payload, dict)
                else None
            ),
            "stale_bookmark_rejected": (
                payload.get("stale_bookmark_rejected") if isinstance(payload, dict) else None
            ),
            "missing_checks": missing_checks,
            "has_security_scope_ref": security_scope_ref.startswith("browser-security-scope:"),
            "has_bookmark_hash": bookmark_hash.startswith("sha256:"),
            "sensitive_fields_present": (
                payload.get("sensitive_fields_present") if isinstance(payload, dict) else None
            ),
        },
    )


def _check_runtime_migration_report(path: Path | None) -> dict[str, Any]:
    if path is None:
        return _check(
            "close01.runtime_migration_dry_run.evidence",
            status="missing",
            summary="No Runtime migration dry-run report evidence file was provided",
            details={
                "expected_schema_version": RUNTIME_MIGRATION_REPORT_SCHEMA_VERSION,
                "required_writer_ids": sorted(REQUIRED_RUNTIME_MIGRATION_WRITER_IDS),
                "expected_files": [
                    "runtime-migration-dry-run-report.json",
                    "runtime-migration-report.json",
                    "close01-runtime-migration-dry-run-report.json",
                ],
            },
        )
    if not path.is_file():
        return _check(
            "close01.runtime_migration_dry_run.evidence",
            status="missing",
            summary=f"Runtime migration dry-run report not found: {path}",
            details={"path": str(path)},
        )
    try:
        payload = _read_json(path)
    except Exception as exc:
        return _check(
            "close01.runtime_migration_dry_run.evidence",
            status="failed",
            summary=f"Runtime migration dry-run report is not valid JSON: {exc}",
            details={"path": str(path)},
        )
    if not isinstance(payload, dict):
        return _check(
            "close01.runtime_migration_dry_run.evidence",
            status="failed",
            summary="Runtime migration dry-run report is not a JSON object",
            details={"path": str(path)},
        )

    inventory = payload.get("writer_inventory")
    writer_rows = [row for row in inventory or [] if isinstance(row, dict)]
    writer_id_values = [str(row.get("writer_id") or "") for row in writer_rows]
    duplicate_writer_ids = sorted(
        {
            writer_id
            for writer_id in writer_id_values
            if writer_id and writer_id_values.count(writer_id) > 1
        }
    )
    writers_by_id = {writer_id: row for writer_id, row in zip(writer_id_values, writer_rows)}
    observed_writer_ids = set(writers_by_id) - {""}
    missing_writer_ids = sorted(
        REQUIRED_RUNTIME_MIGRATION_WRITER_IDS - observed_writer_ids
    )
    active_writer_ids = sorted(
        writer_id
        for writer_id, row in writers_by_id.items()
        if row.get("legacy_write_allowed") is not False
    )
    not_ready_writer_ids = sorted(
        writer_id
        for writer_id, row in writers_by_id.items()
        if (
            row.get("can_stop_writes") is not True
            or row.get("migration_status")
            != "legacy_write_stopped_read_compatibility"
            or bool(row.get("stop_writes_blockers") or [])
        )
    )
    writer_issue_ids = sorted(
        writer_id
        for writer_id, row in writers_by_id.items()
        if _safe_int(row.get("observed_issue_count")) > 0
    )
    passed = (
        payload.get("schema_version") == RUNTIME_MIGRATION_REPORT_SCHEMA_VERSION
        and payload.get("dry_run") is True
        and payload.get("side_effect_free") is True
        and payload.get("side_effects_executed") == 0
        and not missing_writer_ids
        and not duplicate_writer_ids
        and not active_writer_ids
        and not not_ready_writer_ids
        and not writer_issue_ids
    )
    return _check(
        "close01.runtime_migration_dry_run.evidence",
        status="passed" if passed else "failed",
        summary=(
            "Runtime migration dry-run proves legacy writers are stopped"
            if passed
            else "Runtime migration dry-run does not prove legacy writer shutdown"
        ),
        details={
            "path": str(path),
            "schema_version": payload.get("schema_version"),
            "expected_schema_version": RUNTIME_MIGRATION_REPORT_SCHEMA_VERSION,
            "dry_run": payload.get("dry_run"),
            "side_effect_free": payload.get("side_effect_free"),
            "side_effects_executed": payload.get("side_effects_executed"),
            "observed_writer_ids": sorted(observed_writer_ids),
            "missing_writer_ids": missing_writer_ids,
            "duplicate_writer_ids": duplicate_writer_ids,
            "active_writer_ids": active_writer_ids,
            "not_ready_writer_ids": not_ready_writer_ids,
            "writer_issue_ids": writer_issue_ids,
        },
    )


def _check_runtime_migration_backup_restore_evidence(
    path: Path | None,
) -> dict[str, Any]:
    check_id = "close01.runtime_migration_backup_restore.evidence"
    if path is None:
        return _check(
            check_id,
            status="missing",
            summary="No Runtime migration backup/restore evidence file was provided",
            details={
                "expected_schema": RUNTIME_MIGRATION_BACKUP_RESTORE_SMOKE_SCHEMA,
                "required_checks": sorted(
                    REQUIRED_RUNTIME_MIGRATION_BACKUP_RESTORE_CHECKS
                ),
                "expected_files": [
                    "runtime-migration-backup-restore-smoke.json",
                    "close01-runtime-migration-backup-restore-smoke.json",
                ],
            },
        )
    if not path.is_file():
        return _check(
            check_id,
            status="missing",
            summary=f"Runtime migration backup/restore evidence not found: {path}",
            details={"path": str(path)},
        )
    try:
        payload = _read_json(path)
    except Exception as exc:
        return _check(
            check_id,
            status="failed",
            summary=f"Runtime migration backup/restore evidence is not valid JSON: {exc}",
            details={"path": str(path)},
        )
    if not isinstance(payload, dict):
        return _check(
            check_id,
            status="failed",
            summary="Runtime migration backup/restore evidence is not a JSON object",
            details={"path": str(path)},
        )

    checks = {
        str(item)
        for item in payload.get("checks") or []
        if isinstance(item, str) and item
    }
    missing_checks = sorted(REQUIRED_RUNTIME_MIGRATION_BACKUP_RESTORE_CHECKS - checks)
    downgrade_readers = (
        payload.get("downgrade_readers")
        if isinstance(payload.get("downgrade_readers"), dict)
        else {}
    )
    passed = (
        payload.get("schema") == RUNTIME_MIGRATION_BACKUP_RESTORE_SMOKE_SCHEMA
        and payload.get("passed") is True
        and payload.get("source_workspace_side_effect_free") is True
        and _safe_int(payload.get("source_count")) >= 5
        and payload.get("backup_entry_count") == payload.get("source_count")
        and payload.get("restored_source_count") == payload.get("source_count")
        and payload.get("source_tree_restored_exactly") is True
        and payload.get("signatures_match") is True
        and not missing_checks
        and _safe_int(downgrade_readers.get("experience_runtime_event_count")) >= 1
        and _safe_int(downgrade_readers.get("task_run_contract_count")) >= 1
        and _safe_int(downgrade_readers.get("evaluation_report_count")) >= 1
        and downgrade_readers.get("mission_config_read") is True
        and downgrade_readers.get("mission_prd_read") is True
        and _safe_int(downgrade_readers.get("browser_task_record_count")) >= 1
    )
    return _check(
        check_id,
        status="passed" if passed else "failed",
        summary=(
            "Runtime migration backup/restore and downgrade-read smoke passed"
            if passed
            else "Runtime migration backup/restore evidence is incomplete"
        ),
        details={
            "path": str(path),
            "expected_schema": RUNTIME_MIGRATION_BACKUP_RESTORE_SMOKE_SCHEMA,
            "schema": payload.get("schema"),
            "passed": payload.get("passed"),
            "release_edition": payload.get("release_edition"),
            "source_workspace_side_effect_free": payload.get(
                "source_workspace_side_effect_free"
            ),
            "source_count": payload.get("source_count"),
            "backup_entry_count": payload.get("backup_entry_count"),
            "restored_source_count": payload.get("restored_source_count"),
            "source_tree_restored_exactly": payload.get(
                "source_tree_restored_exactly"
            ),
            "signatures_match": payload.get("signatures_match"),
            "missing_checks": missing_checks,
            "downgrade_readers": downgrade_readers,
        },
    )


def _legacy_writer_observation_summaries_from_lifecycle(
    *,
    source: str,
    path: Path | None,
    expected_distribution: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if path is None:
        return [], [{"source": source, "issue": "lifecycle_evidence_missing"}]
    if not path.is_file():
        return [], [
            {
                "source": source,
                "path": str(path),
                "issue": "lifecycle_evidence_file_missing",
            }
        ]
    try:
        payload = _read_json(path)
    except Exception as exc:
        return [], [
            {
                "source": source,
                "path": str(path),
                "issue": "lifecycle_evidence_invalid_json",
                "error": str(exc),
            }
        ]
    if not isinstance(payload, dict):
        return [], [
            {
                "source": source,
                "path": str(path),
                "issue": "lifecycle_evidence_not_object",
            }
        ]

    checks = {
        str(item)
        for item in payload.get("checks") or []
        if isinstance(item, str) and item
    }
    smoke = payload.get("smoke") if isinstance(payload.get("smoke"), dict) else {}
    summary = smoke.get("legacy_writer_observation")
    if summary is None:
        return [], [
            {
                "source": source,
                "path": str(path),
                "issue": "legacy_writer_observation_summary_missing",
                "expected_distribution": expected_distribution,
                "has_lifecycle_check": "legacy_writer_observation_summary" in checks,
            }
        ]
    return [
        {
            "source": source,
            "path": str(path),
            "expected_release_edition": expected_distribution,
            "has_lifecycle_check": "legacy_writer_observation_summary" in checks,
            "summary": summary,
        }
    ], []


def _legacy_writer_observation_summaries_from_files(
    paths: Iterable[Path],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    summaries: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            issues.append(
                {
                    "source": "standalone_summary",
                    "path": str(path),
                    "issue": "summary_file_missing",
                }
            )
            continue
        try:
            payload = _read_json(path)
        except Exception as exc:
            issues.append(
                {
                    "source": "standalone_summary",
                    "path": str(path),
                    "issue": "summary_file_invalid_json",
                    "error": str(exc),
                }
            )
            continue
        summaries.append(
            {
                "source": "standalone_summary",
                "path": str(path),
                "summary": payload,
            }
        )
    return summaries, issues


def _legacy_writer_observation_summary_details(
    source_summary: dict[str, Any],
) -> dict[str, Any]:
    summary = source_summary.get("summary")
    if not isinstance(summary, dict):
        return {
            "source": source_summary.get("source"),
            "path": source_summary.get("path"),
            "release_edition": None,
            "issues": ["summary_not_object"],
        }

    release_edition = str(summary.get("release_edition") or "")
    expected_release_edition = source_summary.get("expected_release_edition")
    invalid_writer_ids = summary.get("invalid_writer_ids")
    issues: list[str] = []
    if summary.get("schema") != LEGACY_WRITER_OBSERVATION_SUMMARY_SCHEMA:
        issues.append("schema_mismatch")
    if release_edition not in REQUIRED_LEGACY_WRITER_OBSERVATION_EDITIONS:
        issues.append("unsupported_release_edition")
    if expected_release_edition and release_edition != expected_release_edition:
        issues.append("release_edition_mismatch")
    if source_summary.get("has_lifecycle_check") is False:
        issues.append("lifecycle_check_missing")
    if _safe_int(summary.get("fallback_write_count")) != 0:
        issues.append("fallback_writes_observed")
    if summary.get("zero_fallback_writes") is not True:
        issues.append("zero_fallback_writes_not_true")
    if _safe_int(summary.get("malformed_count")) != 0:
        issues.append("malformed_observations")
    if _safe_int(summary.get("unknown_schema_count")) != 0:
        issues.append("unknown_observation_schemas")
    if invalid_writer_ids:
        issues.append("invalid_writer_ids")
    if invalid_writer_ids is not None and not isinstance(invalid_writer_ids, list):
        issues.append("invalid_writer_ids_not_list")

    return {
        "source": source_summary.get("source"),
        "path": source_summary.get("path"),
        "release_edition": release_edition or None,
        "schema": summary.get("schema"),
        "expected_schema": LEGACY_WRITER_OBSERVATION_SUMMARY_SCHEMA,
        "event_count": summary.get("event_count"),
        "fallback_write_count": summary.get("fallback_write_count"),
        "blocked_attempt_count": summary.get("blocked_attempt_count"),
        "zero_observations": summary.get("zero_observations"),
        "zero_fallback_writes": summary.get("zero_fallback_writes"),
        "malformed_count": summary.get("malformed_count"),
        "unknown_schema_count": summary.get("unknown_schema_count"),
        "invalid_writer_ids": invalid_writer_ids,
        "has_lifecycle_check": source_summary.get("has_lifecycle_check"),
        "issues": issues,
    }


def _check_legacy_writer_observation_evidence(
    *,
    official_lifecycle_evidence: Path | None,
    mas_lifecycle_evidence: Path | None,
    standalone_evidence_paths: Iterable[Path] = (),
) -> dict[str, Any]:
    check_id = "close01.legacy_writer_observation.evidence"
    summaries: list[dict[str, Any]] = []
    source_issues: list[dict[str, Any]] = []
    for source, path, distribution in (
        ("official_lifecycle", official_lifecycle_evidence, "official"),
        ("mas_lifecycle", mas_lifecycle_evidence, "mas"),
    ):
        source_summaries, issues = _legacy_writer_observation_summaries_from_lifecycle(
            source=source,
            path=path,
            expected_distribution=distribution,
        )
        summaries.extend(source_summaries)
        source_issues.extend(issues)
    source_summaries, issues = _legacy_writer_observation_summaries_from_files(
        standalone_evidence_paths,
    )
    summaries.extend(source_summaries)
    source_issues.extend(issues)

    summary_details = [
        _legacy_writer_observation_summary_details(summary) for summary in summaries
    ]
    valid_summary_details = [
        item
        for item in summary_details
        if not item["issues"]
        and item["release_edition"] in REQUIRED_LEGACY_WRITER_OBSERVATION_EDITIONS
    ]
    observed_editions = sorted(
        {
            str(item["release_edition"])
            for item in valid_summary_details
            if item.get("release_edition")
        }
    )
    missing_editions = sorted(
        REQUIRED_LEGACY_WRITER_OBSERVATION_EDITIONS - set(observed_editions)
    )
    invalid_sources = [item for item in summary_details if item["issues"]]
    blocking_source_issues = [
        item for item in source_issues if item.get("source") == "standalone_summary"
    ]
    if not summary_details:
        status = "missing"
    elif invalid_sources or blocking_source_issues or missing_editions:
        status = "failed"
    else:
        status = "passed"

    return _check(
        check_id,
        status=status,
        summary=(
            "Legacy Runtime writer observation evidence proves zero active fallback writes"
            if status == "passed"
            else "Legacy Runtime writer observation evidence is incomplete"
        ),
        details={
            "expected_schema": LEGACY_WRITER_OBSERVATION_SUMMARY_SCHEMA,
            "required_release_editions": sorted(
                REQUIRED_LEGACY_WRITER_OBSERVATION_EDITIONS
            ),
            "observed_release_editions": observed_editions,
            "missing_release_editions": missing_editions,
            "expected_files": list(LEGACY_WRITER_OBSERVATION_EVIDENCE_FILES),
            "source_issues": source_issues,
            "blocking_source_issues": blocking_source_issues,
            "summaries": summary_details,
        },
    )


def _check_legacy_writer_observation_edition_evidence(
    check_id: str,
    *,
    release_edition: str,
    lifecycle_evidence: Path | None,
    lifecycle_source: str,
    standalone_evidence_paths: Iterable[Path] = (),
) -> dict[str, Any]:
    summaries, source_issues = _legacy_writer_observation_summaries_from_lifecycle(
        source=lifecycle_source,
        path=lifecycle_evidence,
        expected_distribution=release_edition,
    )
    source_summaries, issues = _legacy_writer_observation_summaries_from_files(
        standalone_evidence_paths,
    )
    summaries.extend(source_summaries)
    source_issues.extend(issues)

    summary_details = [
        _legacy_writer_observation_summary_details(summary) for summary in summaries
    ]
    matching_details = [
        item for item in summary_details if item.get("release_edition") == release_edition
    ]
    invalid_matching = [item for item in matching_details if item["issues"]]
    valid_matching = [item for item in matching_details if not item["issues"]]
    blocking_source_issues = [
        item for item in source_issues if item.get("source") == "standalone_summary"
    ]
    if valid_matching and not invalid_matching and not blocking_source_issues:
        status = "passed"
    elif not summary_details:
        status = "missing"
    elif not matching_details:
        status = "missing"
    else:
        status = "failed"

    return _check(
        check_id,
        status=status,
        summary=(
            f"{release_edition} legacy Runtime writer observation proves zero "
            "active fallback writes"
            if status == "passed"
            else (
                f"{release_edition} legacy Runtime writer observation evidence "
                "is incomplete"
            )
        ),
        details={
            "expected_schema": LEGACY_WRITER_OBSERVATION_SUMMARY_SCHEMA,
            "release_edition": release_edition,
            "expected_files": list(LEGACY_WRITER_OBSERVATION_EVIDENCE_FILES),
            "source_issues": source_issues,
            "blocking_source_issues": blocking_source_issues,
            "summaries": summary_details,
        },
    )


def _check_lifecycle_evidence(
    check_id: str,
    path: Path | None,
    *,
    expected_distribution: str,
    expected_app_paths: Iterable[Path] = (),
    require_expected_app_path: bool = False,
) -> dict[str, Any]:
    expected_app_paths = tuple(expected_app_paths)
    if path is None:
        return _check(
            check_id,
            status="missing",
            summary="No packaged lifecycle smoke evidence file was provided",
            details={
                "expected_schema": PACKAGED_LIFECYCLE_SMOKE_SCHEMA,
                "expected_distribution": expected_distribution,
                "required_checks": sorted(REQUIRED_LIFECYCLE_CHECKS),
            },
        )
    if not path.is_file():
        return _check(
            check_id,
            status="missing",
            summary=f"Packaged lifecycle smoke evidence file not found: {path}",
            details={"path": str(path)},
        )
    try:
        payload = _read_json(path)
    except Exception as exc:
        return _check(
            check_id,
            status="failed",
            summary=f"Packaged lifecycle smoke evidence is not valid JSON: {exc}",
            details={"path": str(path)},
        )

    if not isinstance(payload, dict):
        return _check(
            check_id,
            status="failed",
            summary="Packaged lifecycle smoke evidence is not a JSON object",
            details={"path": str(path)},
        )

    checks = set(payload.get("checks") or [])
    missing_checks = sorted(REQUIRED_LIFECYCLE_CHECKS - checks)
    smoke = payload.get("smoke") if isinstance(payload.get("smoke"), dict) else {}
    app_relaunch = (
        smoke.get("app_relaunch") if isinstance(smoke.get("app_relaunch"), dict) else None
    )
    app_relaunch_issues: list[str] = []
    if payload.get("app_relaunch_cycle") is not True:
        app_relaunch_issues.append("app_relaunch_cycle_not_recorded")
    if not isinstance(app_relaunch, dict):
        app_relaunch_issues.append("app_relaunch_summary_missing")
    else:
        if not str(app_relaunch.get("backend_pid") or "").strip():
            app_relaunch_issues.append("app_relaunch_backend_pid_missing")
        if not str(app_relaunch.get("chat_id") or "").strip():
            app_relaunch_issues.append("app_relaunch_chat_history_missing")
        runtime_controls = app_relaunch.get("runtime_controls")
        if not isinstance(runtime_controls, dict):
            app_relaunch_issues.append("app_relaunch_runtime_controls_missing")
        else:
            for field in sorted(REQUIRED_APP_RELAUNCH_RUNTIME_CONTROL_FIELDS):
                if runtime_controls.get(field) is not True:
                    app_relaunch_issues.append(
                        f"app_relaunch_runtime_control_missing:{field}"
                    )
    model_success_path = (
        smoke.get("model_success_path") if isinstance(smoke, dict) else None
    )
    model_success_issues: list[str] = []
    if not isinstance(model_success_path, dict):
        model_success_issues.append("model_success_path_missing")
    else:
        if model_success_path.get("configured") is not True:
            model_success_issues.append("model_success_path_not_configured")
        try:
            request_count = int(model_success_path.get("request_count") or 0)
        except (TypeError, ValueError):
            request_count = 0
        if request_count < 1:
            model_success_issues.append("model_success_path_no_model_requests")
    legacy_boundary = (
        smoke.get("legacy_boundary_observation")
        if isinstance(smoke.get("legacy_boundary_observation"), dict)
        else None
    )
    legacy_boundary_issues: list[str] = []
    if not isinstance(legacy_boundary, dict):
        legacy_boundary_issues.append("legacy_boundary_observation_missing")
    else:
        if legacy_boundary.get("schema") != LEGACY_BOUNDARY_OBSERVATION_SUMMARY_SCHEMA:
            legacy_boundary_issues.append("legacy_boundary_schema_invalid")
        if legacy_boundary.get("release_edition") != expected_distribution:
            legacy_boundary_issues.append("legacy_boundary_distribution_mismatch")
        if legacy_boundary.get("observation_files_exist") is not True:
            legacy_boundary_issues.append("legacy_boundary_files_missing")
        if legacy_boundary.get("evidence_valid") is not True:
            legacy_boundary_issues.append("legacy_boundary_evidence_invalid")
        if legacy_boundary.get("zero_forbidden_legacy_activity") is not True:
            legacy_boundary_issues.append("legacy_boundary_forbidden_activity")
    missing_basic_experience_checks: dict[str, list[str]] = {}
    failed_basic_experience_checks: dict[str, list[str]] = {}
    failed_backend_model_roundtrip: dict[str, list[str]] = {}
    failed_provider_stream_ui: dict[str, list[str]] = {}

    def _as_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    basic_evidence_by_key = {
        "chat_renderer_basic_ui_send": smoke.get("chat_renderer_basic_ui_send"),
        "post_restart_chat_renderer_basic_ui_send": smoke.get(
            "post_restart_chat_renderer_basic_ui_send"
        ),
        "app_relaunch.chat_renderer_basic_ui_send": (
            app_relaunch.get("chat_renderer_basic_ui_send")
            if isinstance(app_relaunch, dict)
            else None
        ),
    }
    for smoke_key, basic in basic_evidence_by_key.items():
        matrix = (
            basic.get("experience_matrix")
            if isinstance(basic, dict)
            and isinstance(basic.get("experience_matrix"), list)
            else []
        )
        matrix_by_id = {
            str(item.get("id")): item
            for item in matrix
            if isinstance(item, dict) and item.get("id")
        }
        missing = sorted(REQUIRED_BASIC_EXPERIENCE_CHECKS - set(matrix_by_id))
        failed = sorted(
            check_id
            for check_id, item in matrix_by_id.items()
            if check_id in REQUIRED_BASIC_EXPERIENCE_CHECKS
            and item.get("passed") is not True
        )
        if missing:
            missing_basic_experience_checks[smoke_key] = missing
        if failed:
            failed_basic_experience_checks[smoke_key] = failed
        backend_issues: list[str] = []
        if not isinstance(basic, dict):
            backend_issues.append("missing_basic_ui_send_evidence")
        else:
            if basic.get("backend_turn_observed") is not True:
                backend_issues.append("backend_turn_not_observed")
            if _as_int(basic.get("console_chat_post_count")) != 2:
                backend_issues.append("console_chat_post_count_not_two")
            final_backend = (
                basic.get("final_backend")
                if isinstance(basic.get("final_backend"), dict)
                else {}
            )
            restored_backend = (
                basic.get("restored_backend")
                if isinstance(basic.get("restored_backend"), dict)
                else {}
            )
            if _as_int(final_backend.get("user_message_count")) != 2:
                backend_issues.append("final_backend_user_count_not_two")
            if _as_int(final_backend.get("assistant_message_count")) < 2:
                backend_issues.append("final_backend_assistant_count_below_two")
            if str(final_backend.get("pending_user_message") or ""):
                backend_issues.append("final_backend_pending_user_message")
            if _as_int(restored_backend.get("user_message_count")) != 2:
                backend_issues.append("restored_backend_user_count_not_two")
            if _as_int(restored_backend.get("assistant_message_count")) < 2:
                backend_issues.append("restored_backend_assistant_count_below_two")
        if backend_issues:
            failed_backend_model_roundtrip[smoke_key] = backend_issues

    provider_stream_evidence_by_key = {
        "chat_renderer_provider_stream_ui": smoke.get(
            "chat_renderer_provider_stream_ui"
        ),
        "post_restart_chat_renderer_provider_stream_ui": smoke.get(
            "post_restart_chat_renderer_provider_stream_ui"
        ),
        "app_relaunch.chat_renderer_provider_stream_ui": (
            app_relaunch.get("chat_renderer_provider_stream_ui")
            if isinstance(app_relaunch, dict)
            else None
        ),
    }
    for smoke_key, provider in provider_stream_evidence_by_key.items():
        issues: list[str] = []
        if not isinstance(provider, dict):
            issues.append("provider_stream_ui_evidence_missing")
        else:
            if provider.get("ok") is not True:
                issues.append("provider_stream_ui_not_ok")
            thinking_ms = _as_int(provider.get("thinking_visible_ms"))
            partial_ms = _as_int(provider.get("partial_visible_ms"))
            final_ms = _as_int(provider.get("final_visible_ms"))
            if not (0 < thinking_ms < partial_ms < final_ms):
                issues.append("provider_stream_phase_order_invalid")
            thinking = (
                provider.get("thinking")
                if isinstance(provider.get("thinking"), dict)
                else {}
            )
            partial = (
                provider.get("partial")
                if isinstance(provider.get("partial"), dict)
                else {}
            )
            final = (
                provider.get("final")
                if isinstance(provider.get("final"), dict)
                else {}
            )
            if thinking.get("thinking_visible") is not True:
                issues.append("thinking_phase_missing")
            if partial.get("partial_visible") is not True:
                issues.append("partial_phase_missing")
            if final.get("redirected_answer_visible") is not True:
                issues.append("redirected_answer_phase_missing")
            if final.get("old_full_answer_visible") is not False:
                issues.append("retired_full_answer_visible")
            if _as_int(final.get("assistant_bubble_count")) != 2:
                issues.append("redirected_final_assistant_count_not_two")
            rapid_queue = (
                provider.get("rapid_queue")
                if isinstance(provider.get("rapid_queue"), dict)
                else {}
            )
            rapid_final = (
                rapid_queue.get("final")
                if isinstance(rapid_queue.get("final"), dict)
                else {}
            )
            if _as_int(rapid_queue.get("queued_submission_count")) != 2:
                issues.append("rapid_queue_submission_count_not_two")
            if rapid_queue.get("queued_before_first_answer") is not True:
                issues.append("rapid_queue_not_before_first_answer")
            queued_drafts = rapid_queue.get("queued_drafts")
            if not isinstance(queued_drafts, list) or len(queued_drafts) != 2:
                issues.append("rapid_queue_drafts_missing")
            if rapid_queue.get("submission_intents") != ["supplement", "redirect"]:
                issues.append("rapid_queue_submission_intents_invalid")
            admitted_statuses = rapid_queue.get("admitted_statuses")
            if not isinstance(admitted_statuses, dict) or any(
                not isinstance(admitted_statuses.get(intent), dict)
                or admitted_statuses[intent].get("status") != "admitted"
                for intent in ("supplement", "redirect")
            ):
                issues.append("rapid_queue_admitted_receipts_missing")
            if _as_int(rapid_final.get("user_bubble_count")) != 3:
                issues.append("rapid_queue_user_bubble_count_not_three")
            if _as_int(rapid_final.get("assistant_bubble_count")) != 2:
                issues.append("rapid_queue_assistant_bubble_count_not_two")
            if _as_int(rapid_final.get("empty_assistant_count")) != 0:
                issues.append("rapid_queue_empty_assistant_present")
            if rapid_final.get("user_texts_in_fifo_order") is not True:
                issues.append("rapid_queue_dom_order_invalid")
            for field, issue in (
                (
                    "assistant_texts_in_fifo_order",
                    "rapid_queue_assistant_order_invalid",
                ),
                ("old_partial_preserved", "rapid_queue_old_partial_missing"),
                (
                    "old_full_answer_absent",
                    "rapid_queue_retired_full_answer_present",
                ),
                (
                    "superseded_answer_absent",
                    "rapid_queue_superseded_answer_present",
                ),
                ("redirect_answer_present", "rapid_queue_redirect_answer_missing"),
                (
                    "conversation_items_preserve_fifo_order",
                    "rapid_queue_conversation_order_invalid",
                ),
            ):
                if rapid_final.get(field) is not True:
                    issues.append(issue)
            supplement_disposition = rapid_final.get("supplement_disposition")
            if (
                not isinstance(supplement_disposition, dict)
                or supplement_disposition.get("status") != "superseded"
            ):
                issues.append("rapid_queue_supplement_disposition_not_superseded")
            redirect_disposition = rapid_final.get("redirect_disposition")
            if (
                not isinstance(redirect_disposition, dict)
                or redirect_disposition.get("status") != "applied"
            ):
                issues.append("rapid_queue_redirect_disposition_not_applied")
            backend = (
                provider.get("backend")
                if isinstance(provider.get("backend"), dict)
                else {}
            )
            if _as_int(backend.get("user_message_count")) != 3:
                issues.append("rapid_queue_backend_user_count_not_three")
            if _as_int(backend.get("assistant_message_count")) != 2:
                issues.append("rapid_queue_backend_assistant_count_not_two")
            if str(backend.get("pending_user_message") or ""):
                issues.append("rapid_queue_backend_pending_user_message")
            if backend.get("user_texts_in_fifo_order") is not True:
                issues.append("rapid_queue_backend_order_invalid")
            stop_resend = (
                provider.get("stop_resend")
                if isinstance(provider.get("stop_resend"), dict)
                else {}
            )
            stop_final = (
                stop_resend.get("final")
                if isinstance(stop_resend.get("final"), dict)
                else {}
            )
            stop_backend = (
                stop_resend.get("backend")
                if isinstance(stop_resend.get("backend"), dict)
                else {}
            )
            if stop_resend.get("ok") is not True:
                issues.append("stop_resend_not_ok")
            if stop_resend.get("stop_terminal") not in {"interrupted", "cancelled"}:
                issues.append("stop_resend_terminal_invalid")
            if stop_resend.get("full_stopped_answer_absent") is not True:
                issues.append("stop_resend_stopped_answer_leaked")
            if _as_int(stop_final.get("user_bubble_count")) != 2:
                issues.append("stop_resend_user_bubble_count_not_two")
            if _as_int(stop_final.get("empty_assistant_count")) != 0:
                issues.append("stop_resend_empty_assistant_present")
            stop_assistant_count = _as_int(stop_final.get("assistant_bubble_count"))
            if _as_int(stop_final.get("response_avatar_count")) > stop_assistant_count:
                issues.append("stop_resend_duplicate_avatar")
            if _as_int(stop_final.get("action_bar_count")) > stop_assistant_count:
                issues.append("stop_resend_duplicate_action_bar")
            if _as_int(stop_backend.get("user_message_count")) != 2:
                issues.append("stop_resend_backend_user_count_not_two")
            if _as_int(stop_backend.get("assistant_message_count")) < 1:
                issues.append("stop_resend_backend_assistant_missing")
            if str(stop_backend.get("pending_user_message") or ""):
                issues.append("stop_resend_backend_pending_user_message")

            route_switch = (
                provider.get("route_switch")
                if isinstance(provider.get("route_switch"), dict)
                else {}
            )
            switch_isolation = (
                route_switch.get("isolation")
                if isinstance(route_switch.get("isolation"), dict)
                else {}
            )
            switch_restored = (
                route_switch.get("restored")
                if isinstance(route_switch.get("restored"), dict)
                else {}
            )
            switch_backend = (
                route_switch.get("backend")
                if isinstance(route_switch.get("backend"), dict)
                else {}
            )
            if route_switch.get("ok") is not True:
                issues.append("stream_route_switch_not_ok")
            if _as_int(switch_isolation.get("user_bubble_count")) != 0:
                issues.append("stream_route_switch_isolation_user_leak")
            if _as_int(switch_isolation.get("assistant_bubble_count")) != 0:
                issues.append("stream_route_switch_isolation_assistant_leak")
            if _as_int(switch_restored.get("user_bubble_count")) != 1:
                issues.append("stream_route_switch_restored_user_count_not_one")
            switch_assistant_count = _as_int(
                switch_restored.get("assistant_bubble_count")
            )
            if switch_assistant_count != 1:
                issues.append("stream_route_switch_restored_assistant_count_not_one")
            if _as_int(switch_restored.get("empty_assistant_count")) != 0:
                issues.append("stream_route_switch_empty_assistant_present")
            if (
                _as_int(switch_restored.get("response_avatar_count"))
                > switch_assistant_count
            ):
                issues.append("stream_route_switch_duplicate_avatar")
            if (
                _as_int(switch_restored.get("action_bar_count"))
                > switch_assistant_count
            ):
                issues.append("stream_route_switch_duplicate_action_bar")
            if _as_int(switch_backend.get("user_message_count")) != 1:
                issues.append("stream_route_switch_backend_user_count_not_one")
            switch_backend_assistant_count = _as_int(
                switch_backend.get("assistant_message_count")
            )
            if not 1 <= switch_backend_assistant_count <= 2:
                issues.append("stream_route_switch_backend_projection_count_invalid")
            if _as_int(switch_backend.get("thinking_occurrence_count")) != 1:
                issues.append("stream_route_switch_backend_thinking_not_exactly_once")
            if _as_int(switch_backend.get("answer_occurrence_count")) != 1:
                issues.append("stream_route_switch_backend_answer_not_exactly_once")
            if str(switch_backend.get("pending_user_message") or ""):
                issues.append("stream_route_switch_backend_pending_user_message")
        if issues:
            failed_provider_stream_ui[smoke_key] = issues
    app_payload = payload.get("app") if isinstance(payload.get("app"), dict) else {}
    evidence_app_path = str(app_payload.get("path") or "")
    evidence_artifact_sha256 = (
        app_payload.get("artifact_sha256")
        if isinstance(app_payload.get("artifact_sha256"), dict)
        else {}
    )
    expected_path_strings = {
        str(expected.expanduser().resolve()) for expected in expected_app_paths
    }
    path_matches = (
        evidence_app_path in expected_path_strings
        if require_expected_app_path
        else not expected_path_strings or evidence_app_path in expected_path_strings
    )
    matching_app_path = next(
        (
            expected.expanduser().resolve()
            for expected in expected_app_paths
            if str(expected.expanduser().resolve()) == evidence_app_path
        ),
        None,
    )
    expected_artifact_sha256 = (
        _app_artifact_sha256(matching_app_path)
        if matching_app_path is not None
        else {}
    )
    artifact_hash_issues: list[str] = []
    if matching_app_path is not None:
        for artifact_id in sorted(REQUIRED_APP_ARTIFACT_HASH_IDS):
            observed = str(evidence_artifact_sha256.get(artifact_id) or "")
            expected = expected_artifact_sha256.get(artifact_id)
            if expected is None:
                artifact_hash_issues.append(f"{artifact_id}:current_artifact_missing")
            elif not observed.startswith("sha256:"):
                artifact_hash_issues.append(f"{artifact_id}:evidence_hash_missing")
            elif observed != expected:
                artifact_hash_issues.append(f"{artifact_id}:hash_mismatch")
    passed = (
        payload.get("schema") == PACKAGED_LIFECYCLE_SMOKE_SCHEMA
        and payload.get("passed") is True
        and payload.get("distribution") == expected_distribution
        and payload.get("restart_cycle") is True
        and not app_relaunch_issues
        and not missing_checks
        and not model_success_issues
        and not legacy_boundary_issues
        and not missing_basic_experience_checks
        and not failed_basic_experience_checks
        and not failed_backend_model_roundtrip
        and not failed_provider_stream_ui
        and path_matches
        and not artifact_hash_issues
    )
    return _check(
        check_id,
        status="passed" if passed else "failed",
        summary=(
            "Packaged lifecycle smoke evidence proves restart-cycle runtime coverage"
            if passed
            else "Packaged lifecycle smoke evidence is missing required proof"
        ),
        details={
            "path": str(path),
            "schema": payload.get("schema"),
            "passed": payload.get("passed"),
            "distribution": payload.get("distribution"),
            "restart_cycle": payload.get("restart_cycle"),
            "app_relaunch_cycle": payload.get("app_relaunch_cycle"),
            "app_relaunch_issues": app_relaunch_issues,
            "app_path": evidence_app_path,
            "expected_app_paths": sorted(expected_path_strings),
            "require_expected_app_path": require_expected_app_path,
            "app_path_matches": path_matches,
            "artifact_hash_issues": artifact_hash_issues,
            "evidence_artifact_sha256": evidence_artifact_sha256,
            "expected_artifact_sha256": expected_artifact_sha256,
            "missing_checks": missing_checks,
            "model_success_issues": model_success_issues,
            "legacy_boundary_issues": legacy_boundary_issues,
            "legacy_boundary_observation": legacy_boundary,
            "missing_basic_experience_checks": missing_basic_experience_checks,
            "failed_basic_experience_checks": failed_basic_experience_checks,
            "failed_backend_model_roundtrip": failed_backend_model_roundtrip,
            "failed_provider_stream_ui": failed_provider_stream_ui,
        },
    )


def _check_rapid_lifecycle_evidence(
    path: Path | None,
    *,
    expected_app_path: Path,
    minimum_cycles: int = 100,
) -> dict[str, Any]:
    check_id = "official.packaged_rapid_lifecycle_smoke.evidence"
    if path is None or not path.is_file():
        return _check(
            check_id,
            status="missing",
            summary=(
                "No packaged process-ownership evidence meeting the configured "
                f"{minimum_cycles}-cycle minimum was found"
            ),
            details={
                "path": str(path) if path is not None else None,
                "minimum_cycles": minimum_cycles,
                "expected_schema": PACKAGED_RAPID_LIFECYCLE_SMOKE_SCHEMA,
            },
        )
    try:
        payload = _read_json(path)
    except Exception as exc:
        return _check(
            check_id,
            status="failed",
            summary=f"Rapid lifecycle evidence is not valid JSON: {exc}",
            details={"path": str(path)},
        )
    if not isinstance(payload, dict):
        return _check(
            check_id,
            status="failed",
            summary="Rapid lifecycle evidence is not a JSON object",
            details={"path": str(path)},
        )

    requested = _safe_int(payload.get("requested_cycles"))
    completed = _safe_int(payload.get("completed_cycles"))
    unique_pids = _safe_int(payload.get("unique_backend_pid_count"))
    pid_reuse_count = _safe_int(payload.get("backend_pid_reuse_count"))
    duplicate_backend_count = _safe_int(
        payload.get("duplicate_backend_process_count"), default=-1
    )
    maximum_simultaneous_backends = _safe_int(
        payload.get("maximum_simultaneous_backend_processes"), default=-1
    )
    unique_nonces = _safe_int(payload.get("unique_desktop_instance_nonce_count"))
    orphan_count = _safe_int(payload.get("orphan_process_count"), default=-1)
    app_payload = payload.get("app") if isinstance(payload.get("app"), dict) else {}
    evidence_app_path = str(app_payload.get("path") or "")
    expected_app_path = expected_app_path.expanduser().resolve()
    expected_hashes = _app_artifact_sha256(expected_app_path)
    evidence_hashes = (
        app_payload.get("artifact_sha256")
        if isinstance(app_payload.get("artifact_sha256"), dict)
        else {}
    )
    hash_issues = [
        artifact_id
        for artifact_id in sorted(REQUIRED_APP_ARTIFACT_HASH_IDS)
        if evidence_hashes.get(artifact_id) != expected_hashes.get(artifact_id)
    ]
    issues: list[str] = []
    if payload.get("schema") != PACKAGED_RAPID_LIFECYCLE_SMOKE_SCHEMA:
        issues.append("schema_mismatch")
    if payload.get("passed") is not True:
        issues.append("evidence_not_passed")
    if payload.get("distribution") != "official":
        issues.append("distribution_mismatch")
    if requested < minimum_cycles:
        issues.append("requested_cycle_count_below_minimum")
    if completed != requested:
        issues.append("completed_cycle_count_mismatch")
    if unique_pids + pid_reuse_count != completed:
        issues.append("backend_pid_accounting_mismatch")
    if duplicate_backend_count != 0:
        issues.append("duplicate_backend_processes_detected")
    if maximum_simultaneous_backends != 1:
        issues.append("simultaneous_backend_process_count_mismatch")
    if unique_nonces != completed:
        issues.append("desktop_nonce_uniqueness_mismatch")
    if orphan_count != 0:
        issues.append("orphan_processes_detected")
    if evidence_app_path != str(expected_app_path):
        issues.append("app_path_mismatch")
    if hash_issues:
        issues.append("app_artifact_hash_mismatch")

    return _check(
        check_id,
        status="passed" if not issues else "failed",
        summary=(
            f"Packaged app completed {completed} clean start/stop cycles"
            if not issues
            else "Packaged rapid lifecycle evidence is missing required proof"
        ),
        details={
            "path": str(path),
            "minimum_cycles": minimum_cycles,
            "requested_cycles": requested,
            "completed_cycles": completed,
            "unique_backend_pid_count": unique_pids,
            "backend_pid_reuse_count": pid_reuse_count,
            "duplicate_backend_process_count": duplicate_backend_count,
            "maximum_simultaneous_backend_processes": maximum_simultaneous_backends,
            "unique_desktop_instance_nonce_count": unique_nonces,
            "orphan_process_count": orphan_count,
            "app_path": evidence_app_path,
            "expected_app_path": str(expected_app_path),
            "artifact_hash_issues": hash_issues,
            "issues": issues,
        },
    )


def _check_customization_upgrade_evidence(
    path: Path | None,
    *,
    expected_app_path: Path,
) -> dict[str, Any]:
    check_id = "official.packaged_customization_upgrade_smoke.evidence"
    if path is None or not path.is_file():
        return _check(
            check_id,
            status="missing",
            summary="No packaged 1.x personalization upgrade evidence was found",
            details={
                "path": str(path) if path is not None else None,
                "expected_schema": PACKAGED_CUSTOMIZATION_UPGRADE_SMOKE_SCHEMA,
            },
        )
    try:
        payload = _read_json(path)
    except Exception as exc:
        return _check(
            check_id,
            status="failed",
            summary=f"Customization upgrade evidence is not valid JSON: {exc}",
            details={"path": str(path)},
        )
    if not isinstance(payload, dict):
        return _check(
            check_id,
            status="failed",
            summary="Customization upgrade evidence is not a JSON object",
            details={"path": str(path)},
        )

    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    app = payload.get("app") if isinstance(payload.get("app"), dict) else {}
    expected_app_path = expected_app_path.expanduser().resolve()
    expected_hashes = _app_artifact_sha256(expected_app_path)
    evidence_hashes = (
        app.get("artifact_sha256")
        if isinstance(app.get("artifact_sha256"), dict)
        else {}
    )
    expected_values = {
        "appearance.palette": "warm",
        "appearance.composer_size": "spacious",
        "conversation.show_thinking": True,
        "persona.presence": "quiet",
    }
    issues: list[str] = []
    if payload.get("schema") != PACKAGED_CUSTOMIZATION_UPGRADE_SMOKE_SCHEMA:
        issues.append("schema_mismatch")
    if payload.get("passed") is not True:
        issues.append("evidence_not_passed")
    if app.get("path") != str(expected_app_path):
        issues.append("app_path_mismatch")
    if any(
        evidence_hashes.get(artifact_id) != expected_hashes.get(artifact_id)
        for artifact_id in REQUIRED_APP_ARTIFACT_HASH_IDS
    ):
        issues.append("app_artifact_hash_mismatch")
    if summary.get("profile_id") != "profile_v1_upgrade_fixture":
        issues.append("profile_identity_changed")
    if _safe_int(summary.get("profile_revision")) != 3:
        issues.append("profile_revision_changed")
    if summary.get("active_profile_id_before") != summary.get(
        "active_profile_id_after"
    ):
        issues.append("active_profile_pointer_changed")
    if summary.get("last_known_good_after") != {"profile_v1_upgrade_fixture": 3}:
        issues.append("last_known_good_not_promoted")
    if summary.get("pending_boot_validation_after") != {}:
        issues.append("pending_boot_validation_not_cleared")
    if summary.get("effective_values") != expected_values:
        issues.append("effective_values_changed")
    if summary.get("changed_files"):
        issues.append("protected_files_changed")
    if summary.get("unexpected_files"):
        issues.append("unexpected_customization_files")

    return _check(
        check_id,
        status="passed" if not issues else "failed",
        summary=(
            "Packaged app preserved and validated the protected 1.x personalization state"
            if not issues
            else "Packaged personalization upgrade evidence is missing required proof"
        ),
        details={
            "path": str(path),
            "app_path": app.get("path"),
            "profile_id": summary.get("profile_id"),
            "profile_revision": summary.get("profile_revision"),
            "effective_values": summary.get("effective_values"),
            "issues": issues,
        },
    )


def _derive_overall(checks: list[dict[str, Any]]) -> str:
    final_checks = [check for check in checks if check.get("required_for_final")]
    if final_checks and all(check["status"] == "passed" for check in final_checks):
        return "verified"
    if any(check["status"] == "failed" for check in final_checks):
        return "failed"
    return "blocked_environment"


def audit_release_evidence(args: argparse.Namespace) -> dict[str, Any]:
    root = args.root.expanduser().resolve()
    package = _load_package(root)
    version = args.version or package["version"]
    product_name = args.product_name or package["product_name"]
    arch = args.arch or _default_arch()
    downloads_dir = args.downloads_dir.expanduser().resolve()
    include_archived_mas_evidence = bool(
        getattr(args, "include_archived_mas_evidence", False)
    )

    official_app = args.official_app or downloads_dir / f"{product_name}.app"
    official_dmg = (
        args.official_dmg
        or downloads_dir / f"{product_name}-{version}-{arch}-internal.dmg"
    )
    official_zip = (
        args.official_zip
        or downloads_dir / f"{product_name}-{version}-{arch}-internal.zip"
    )
    mas_root = (
        args.mas_root.expanduser().resolve()
        if include_archived_mas_evidence and args.mas_root
        else root.parent / "lantai_App Store"
    )
    mas_app = (
        args.mas_app or downloads_dir / f"{product_name}-MAS.app"
        if include_archived_mas_evidence
        else None
    )
    mas_pkg = (
        args.mas_pkg or downloads_dir / f"{product_name}-MAS-{version}-{arch}.pkg"
        if include_archived_mas_evidence
        else None
    )
    mas_dev_app = (
        args.mas_dev_app or downloads_dir / f"{product_name}-MAS-dev.app"
        if include_archived_mas_evidence
        else None
    )
    mas_dev_profile = (
        args.mas_dev_profile
        or mas_root / "console/build/embedded-dev.provisionprofile"
        if include_archived_mas_evidence
        else None
    )
    installed_mas_app = (
        _find_installed_mas_app(args.mas_installed_app)
        if include_archived_mas_evidence
        else None
    )
    mas_lifecycle_expected_paths = (
        [installed_mas_app]
        if include_archived_mas_evidence and installed_mas_app is not None
        else []
    )
    official_lifecycle_evidence = args.official_lifecycle_evidence or _find_latest_evidence(
        root,
        (
            "official-packaged-lifecycle-smoke.json",
            "official-local-packaged-lifecycle-smoke.json",
        ),
    )
    packaged_runtime_evidence = getattr(
        args,
        "packaged_runtime_evidence",
        None,
    ) or _find_latest_evidence(
        root,
        ("official-packaged-runtime-completeness-smoke.json",),
    )
    official_rapid_lifecycle_evidence = getattr(
        args, "official_rapid_lifecycle_evidence", None
    ) or _find_latest_evidence(
        root,
        ("official-packaged-rapid-lifecycle-smoke.json",),
    )
    official_customization_upgrade_evidence = getattr(
        args, "official_customization_upgrade_evidence", None
    ) or _find_latest_evidence(
        root,
        ("official-packaged-customization-upgrade-smoke.json",),
    )
    mas_lifecycle_evidence = (
        args.mas_lifecycle_evidence
        or _find_latest_evidence(
            mas_root,
            (
                "mas-packaged-lifecycle-smoke.json",
                "mas-dev-packaged-lifecycle-smoke.json",
            ),
        )
        if include_archived_mas_evidence
        else None
    )
    mas_bookmark_evidence = (
        args.mas_bookmark_evidence
        or _find_latest_evidence(
            mas_root,
            (
                "mas-security-scoped-bookmark-smoke.json",
                "mas-bookmark-smoke.json",
                "security-scoped-bookmark-smoke.json",
            ),
        )
        if include_archived_mas_evidence
        else None
    )
    runtime_migration_report = args.runtime_migration_report or _find_latest_evidence(
        root,
        (
            "runtime-migration-dry-run-report.json",
            "runtime-migration-report.json",
            "close01-runtime-migration-dry-run-report.json",
        ),
    )
    runtime_migration_backup_restore_evidence = (
        args.runtime_migration_backup_restore_evidence
        or _find_latest_evidence(
            root,
            (
                "runtime-migration-backup-restore-smoke.json",
                "close01-runtime-migration-backup-restore-smoke.json",
            ),
        )
    )
    explicit_legacy_writer_observation_evidence = _explicit_evidence_paths(
        getattr(args, "legacy_writer_observation_evidence", None),
    )
    if explicit_legacy_writer_observation_evidence:
        legacy_writer_observation_evidence_paths = (
            explicit_legacy_writer_observation_evidence
        )
    else:
        evidence_roots = [root]
        if include_archived_mas_evidence:
            evidence_roots.append(mas_root)
        legacy_writer_observation_evidence_paths = [
            path
            for evidence_root in evidence_roots
            if (
                path := _find_latest_evidence(
                    evidence_root,
                    LEGACY_WRITER_OBSERVATION_EVIDENCE_FILES,
                )
            )
            is not None
        ]

    checks: list[dict[str, Any]] = []
    checks.append(_check_codesigning_identities())
    checks.append(
        _check_notary_credentials(
            dict(os.environ),
            verify_live=bool(getattr(args, "verify_notary_credentials", False)),
        ),
    )
    checks.extend(
        _check_app(
            "official.download_app",
            official_app,
            expected_bundle_id="com.lantai.app",
            authority_prefixes=("Developer ID Application:",),
            require_notarized_gatekeeper=True,
        )
    )
    checks.append(
        _check_paths_present(
            "official.download_bundles.present",
            (official_dmg, official_zip),
        )
    )
    checks.append(
        _check_lifecycle_evidence(
            "official.packaged_lifecycle_smoke.evidence",
            official_lifecycle_evidence,
            expected_distribution="official",
            expected_app_paths=(official_app,),
        )
    )
    if packaged_runtime_evidence is not None or str(
        getattr(args, "source_sha", "") or ""
    ):
        checks.append(
            _check_packaged_runtime_completeness_evidence(
                packaged_runtime_evidence,
                expected_source_sha=str(getattr(args, "source_sha", "") or ""),
                expected_version=version,
                app=official_app,
            )
        )
    checks.append(
        _check_rapid_lifecycle_evidence(
            official_rapid_lifecycle_evidence,
            expected_app_path=official_app,
            minimum_cycles=getattr(
                args,
                "official_rapid_lifecycle_minimum_cycles",
                100,
            ),
        )
    )
    checks.append(
        _check_customization_upgrade_evidence(
            official_customization_upgrade_evidence,
            expected_app_path=official_app,
        )
    )
    checks.append(
        _check_legacy_writer_observation_edition_evidence(
            "official.legacy_writer_observation.evidence",
            release_edition="official",
            lifecycle_evidence=official_lifecycle_evidence,
            lifecycle_source="official_lifecycle",
            standalone_evidence_paths=legacy_writer_observation_evidence_paths,
        )
    )
    if include_archived_mas_evidence:
        assert mas_app is not None
        assert mas_pkg is not None
        assert mas_dev_app is not None
        assert mas_dev_profile is not None
        checks.extend(
            _check_app(
                "mas.download_app",
                mas_app,
                expected_bundle_id="com.lantai.appstore",
                authority_prefixes=(
                    "Apple Distribution:",
                    "3rd Party Mac Developer Application:",
                ),
                require_sandbox=True,
                required_for_final=False,
            )
        )
        checks.append(_check_pkg_signature(mas_pkg))
        checks.append(
            _check_paths_present(
                "mas.dev_profile.present",
                (mas_dev_profile,),
                required_for_final=False,
            )
        )
        checks.extend(
            _check_app(
                "mas.dev_download_app",
                mas_dev_app,
                expected_bundle_id="com.lantai.appstore",
                authority_prefixes=(
                    "Apple Development:",
                    "Mac Developer:",
                ),
                require_sandbox=True,
                required_for_final=False,
            )
        )
        checks.append(
            _check_lifecycle_evidence(
                "mas.packaged_lifecycle_smoke.evidence",
                mas_lifecycle_evidence,
                expected_distribution="mas",
                expected_app_paths=mas_lifecycle_expected_paths,
                require_expected_app_path=True,
            )
        )
        checks.append(
            _check_legacy_writer_observation_edition_evidence(
                "mas.legacy_writer_observation.evidence",
                release_edition="mas",
                lifecycle_evidence=mas_lifecycle_evidence,
                lifecycle_source="mas_lifecycle",
                standalone_evidence_paths=legacy_writer_observation_evidence_paths,
            )
        )
        if installed_mas_app is None:
            checks.append(
                _check(
                    "mas.installed_app.signature_sandbox",
                    status="missing",
                    summary=(
                        "No TestFlight/App Store installed MAS app was found "
                        "under /Applications"
                    ),
                    details={"bundle_id": "com.lantai.appstore"},
                )
            )
        else:
            checks.extend(
                _check_app(
                    "mas.installed_app",
                    installed_mas_app,
                    expected_bundle_id="com.lantai.appstore",
                    authority_prefixes=(
                        "Apple Distribution:",
                        "3rd Party Mac Developer Application:",
                    ),
                    require_sandbox=True,
                )
            )
            checks.append(_check_mas_installed_app_receipt(installed_mas_app))
        checks.append(_check_bookmark_evidence(mas_bookmark_evidence))
    checks.append(_check_runtime_migration_report(runtime_migration_report))
    checks.append(
        _check_runtime_migration_backup_restore_evidence(
            runtime_migration_backup_restore_evidence,
        )
    )
    if include_archived_mas_evidence:
        checks.append(
            _check_legacy_writer_observation_evidence(
                official_lifecycle_evidence=official_lifecycle_evidence,
                mas_lifecycle_evidence=mas_lifecycle_evidence,
                standalone_evidence_paths=legacy_writer_observation_evidence_paths,
            )
        )

    overall = _derive_overall(checks)
    report = {
        "schema": SCHEMA,
        "overall": overall,
        "release": {
            "root": str(root),
            "distribution": "official",
            "archived_mas_evidence_included": include_archived_mas_evidence,
            "version": version,
            "arch": arch,
            "product_name": product_name,
            "downloads_dir": str(downloads_dir),
            "official_rapid_lifecycle_minimum_cycles": getattr(
                args,
                "official_rapid_lifecycle_minimum_cycles",
                100,
            ),
        },
        "paths": {
            "official_app": str(official_app),
            "official_dmg": str(official_dmg),
            "official_zip": str(official_zip),
            "official_lifecycle_evidence": (
                str(official_lifecycle_evidence)
                if official_lifecycle_evidence
                else None
            ),
            "official_rapid_lifecycle_evidence": (
                str(official_rapid_lifecycle_evidence)
                if official_rapid_lifecycle_evidence
                else None
            ),
            "official_customization_upgrade_evidence": (
                str(official_customization_upgrade_evidence)
                if official_customization_upgrade_evidence
                else None
            ),
            "runtime_migration_report": (
                str(runtime_migration_report) if runtime_migration_report else None
            ),
            "runtime_migration_backup_restore_evidence": (
                str(runtime_migration_backup_restore_evidence)
                if runtime_migration_backup_restore_evidence
                else None
            ),
            "legacy_writer_observation_evidence": [
                str(path) for path in legacy_writer_observation_evidence_paths
            ],
            "official_lifecycle_evidence_source": (
                "explicit" if args.official_lifecycle_evidence else "auto"
            ),
            "official_rapid_lifecycle_evidence_source": (
                "explicit"
                if getattr(args, "official_rapid_lifecycle_evidence", None)
                else "auto"
            ),
            "official_customization_upgrade_evidence_source": (
                "explicit"
                if getattr(args, "official_customization_upgrade_evidence", None)
                else "auto"
            ),
            "runtime_migration_report_source": (
                "explicit" if args.runtime_migration_report else "auto"
            ),
            "runtime_migration_backup_restore_evidence_source": (
                "explicit"
                if args.runtime_migration_backup_restore_evidence
                else "auto"
            ),
            "legacy_writer_observation_evidence_source": (
                "explicit"
                if explicit_legacy_writer_observation_evidence
                else "auto"
            ),
        },
        "checks": checks,
    }
    if include_archived_mas_evidence:
        report["release"]["mas_root"] = str(mas_root)
        report["paths"].update(
            {
                "mas_app": str(mas_app),
                "mas_pkg": str(mas_pkg),
                "mas_dev_app": str(mas_dev_app),
                "mas_dev_profile": str(mas_dev_profile),
                "mas_installed_app": (
                    str(installed_mas_app) if installed_mas_app else None
                ),
                "mas_lifecycle_evidence": (
                    str(mas_lifecycle_evidence) if mas_lifecycle_evidence else None
                ),
                "mas_bookmark_evidence": (
                    str(mas_bookmark_evidence) if mas_bookmark_evidence else None
                ),
                "mas_lifecycle_evidence_source": (
                    "explicit" if args.mas_lifecycle_evidence else "auto"
                ),
                "mas_bookmark_evidence_source": (
                    "explicit" if args.mas_bookmark_evidence else "auto"
                ),
            }
        )
    return report


def _print_text(report: dict[str, Any]) -> None:
    print(f"Release evidence readiness: {report['overall']}")
    for check in report["checks"]:
        required = "final" if check["required_for_final"] else "preflight"
        print(f"- [{check['status']}] {check['id']} ({required}): {check['summary']}")


def _normalize_required_check_ids(values: Iterable[str] | None) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        for raw in str(value).split(","):
            check_id = raw.strip()
            if check_id and check_id not in seen:
                seen.add(check_id)
                ids.append(check_id)
    return ids


def _required_check_failures(
    report: dict[str, Any],
    required_check_ids: Iterable[str],
) -> list[dict[str, Any]]:
    checks_by_id = {check["id"]: check for check in report.get("checks", [])}
    failures: list[dict[str, Any]] = []
    for check_id in required_check_ids:
        check = checks_by_id.get(check_id)
        if check is None:
            failures.append(
                {
                    "id": check_id,
                    "status": "missing_check",
                    "summary": "Required check id is not present in the readiness report",
                }
            )
        elif check.get("status") != "passed":
            failures.append(check)
    return failures


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--mas-root", type=Path)
    parser.add_argument("--downloads-dir", type=Path, default=Path("~/Downloads"))
    parser.add_argument("--version")
    parser.add_argument("--arch")
    parser.add_argument("--product-name")
    parser.add_argument("--official-app", type=Path)
    parser.add_argument("--official-dmg", type=Path)
    parser.add_argument("--official-zip", type=Path)
    parser.add_argument("--mas-app", type=Path)
    parser.add_argument("--mas-pkg", type=Path)
    parser.add_argument("--mas-dev-app", type=Path)
    parser.add_argument("--mas-dev-profile", type=Path)
    parser.add_argument("--mas-installed-app", type=Path)
    parser.add_argument("--official-lifecycle-evidence", type=Path)
    parser.add_argument("--official-rapid-lifecycle-evidence", type=Path)
    parser.add_argument(
        "--official-rapid-lifecycle-minimum-cycles",
        type=_positive_int,
        default=100,
        metavar="CYCLES",
        help=(
            "Minimum completed rapid lifecycle cycles required by the evidence "
            "gate (default: 100)."
        ),
    )
    parser.add_argument("--official-customization-upgrade-evidence", type=Path)
    parser.add_argument("--packaged-runtime-evidence", type=Path)
    parser.add_argument("--source-sha", default="")
    parser.add_argument("--mas-lifecycle-evidence", type=Path)
    parser.add_argument("--mas-bookmark-evidence", type=Path)
    parser.add_argument(
        "--include-archived-mas-evidence",
        action="store_true",
        help=(
            "Audit archived Mac App Store evidence for historical compatibility. "
            "The maintained official release never enables this flag."
        ),
    )
    parser.add_argument("--runtime-migration-report", type=Path)
    parser.add_argument("--runtime-migration-backup-restore-evidence", type=Path)
    parser.add_argument(
        "--legacy-writer-observation-evidence",
        action="append",
        type=Path,
        default=[],
        help=(
            "Runtime legacy-writer observation summary JSON. May be repeated "
            "for official and MAS evidence."
        ),
    )
    parser.add_argument(
        "--verify-notary-credentials",
        action="store_true",
        help=(
            "Run a live notarytool history request using the configured notary "
            "credentials. Disabled by default so ordinary readiness audits stay "
            "offline/read-only."
        ),
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format.",
    )
    parser.add_argument(
        "--require-final",
        action="store_true",
        help="Return non-zero unless all final evidence checks pass.",
    )
    parser.add_argument(
        "--require-check",
        action="append",
        default=[],
        help=(
            "Return non-zero unless the named check id passes. May be repeated "
            "or comma-separated for stage-specific release gates."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        report = audit_release_evidence(args)
    except Exception as exc:  # noqa: BLE001 - command-line diagnostics
        print(f"Release evidence readiness audit failed: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_text(report)

    required_check_ids = _normalize_required_check_ids(args.require_check)
    required_failures = _required_check_failures(report, required_check_ids)
    if required_failures:
        print(
            "Required release evidence checks did not pass: "
            + ", ".join(
                f"{check['id']}={check.get('status', '<unknown>')}"
                for check in required_failures
            ),
            file=sys.stderr,
        )
        return 1

    if args.require_final and report["overall"] != "verified":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
