# -*- coding: utf-8 -*-
"""Release evidence readiness helper tests."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import plistlib
import sys
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[3] / "scripts" / "check_release_evidence_readiness.py"
)
_RUNTIME_MIGRATION_WRITER_IDS = [
    "legacy_runtime_event_ledger_writer",
    "legacy_task_run_snapshot_writer",
    "legacy_evaluation_report_writer",
    "legacy_mission_state_writer",
    "legacy_browser_task_state_writer",
]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "check_release_evidence_readiness",
        SCRIPT_PATH,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _make_root(tmp_path: Path) -> Path:
    root = tmp_path / "lantai3"
    console = root / "console"
    console.mkdir(parents=True)
    (console / "package.json").write_text(
        json.dumps(
            {
                "name": "lantai-console",
                "version": "1.2.3",
                "build": {"productName": "澜台"},
            },
        ),
        encoding="utf-8",
    )
    mas_root = tmp_path / "lantai_App Store"
    (mas_root / "console" / "build").mkdir(parents=True)
    return root


def _make_app(path: Path, bundle_id: str) -> None:
    contents = path / "Contents"
    macos = contents / "MacOS"
    macos.mkdir(parents=True)
    (macos / "澜台").write_text("#!/bin/sh\n", encoding="utf-8")
    resources = contents / "Resources"
    backend = (
        resources
        / "lantai-server"
        / "Lantai Backend.app"
        / "Contents"
        / "MacOS"
        / "lantai-server"
    )
    backend.parent.mkdir(parents=True)
    (resources / "app.asar").write_bytes(b"test-app-asar")
    backend.write_bytes(b"test-frozen-backend")
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleExecutable": "澜台",
                "CFBundleIdentifier": bundle_id,
                "CFBundleShortVersionString": "1.2.3",
                "CFBundleVersion": "1.2.3",
            },
            handle,
        )


def _sha256_tag(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _app_artifact_sha256(app_path: Path) -> dict[str, str]:
    return {
        "main_executable": _sha256_tag(app_path / "Contents" / "MacOS" / "澜台"),
        "app_asar": _sha256_tag(app_path / "Contents" / "Resources" / "app.asar"),
        "backend_executable": _sha256_tag(
            app_path
            / "Contents"
            / "Resources"
            / "lantai-server"
            / "Lantai Backend.app"
            / "Contents"
            / "MacOS"
            / "lantai-server",
        ),
    }


def _write_app_store_receipt(app_path: Path) -> Path:
    receipt = app_path / "Contents" / "_MASReceipt" / "receipt"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_bytes(b"test-app-store-receipt")
    return receipt


def test_installed_mas_app_requires_nonempty_regular_app_store_receipt(
    tmp_path: Path,
) -> None:
    module = _load_module()
    app_path = tmp_path / "Applications" / "澜台.app"
    _make_app(app_path, "com.lantai.appstore")

    missing = module._check_mas_installed_app_receipt(app_path)
    assert missing["status"] == "failed"

    receipt = _write_app_store_receipt(app_path)
    receipt.write_bytes(b"")
    empty = module._check_mas_installed_app_receipt(app_path)
    assert empty["status"] == "failed"

    receipt.write_bytes(b"test-app-store-receipt")
    present = module._check_mas_installed_app_receipt(app_path)
    assert present["status"] == "passed"


def test_mas_lifecycle_must_reference_the_installed_app(tmp_path: Path) -> None:
    module = _load_module()
    downloads_app = tmp_path / "Downloads" / "澜台-MAS.app"
    installed_app = tmp_path / "Applications" / "澜台.app"
    _make_app(downloads_app, "com.lantai.appstore")
    _make_app(installed_app, "com.lantai.appstore")
    evidence = tmp_path / "mas-packaged-lifecycle-smoke.json"
    _write_lifecycle_evidence(
        evidence,
        distribution="mas",
        app_path=downloads_app,
    )

    copied_bundle_evidence = module._check_lifecycle_evidence(
        "mas.packaged_lifecycle_smoke.evidence",
        evidence,
        expected_distribution="mas",
        expected_app_paths=(installed_app,),
        require_expected_app_path=True,
    )
    assert copied_bundle_evidence["status"] == "failed"

    _write_lifecycle_evidence(
        evidence,
        distribution="mas",
        app_path=installed_app,
    )
    tampered_payload = json.loads(evidence.read_text(encoding="utf-8"))
    tampered_payload["app"]["artifact_sha256"]["app_asar"] = "sha256:wrong"
    evidence.write_text(json.dumps(tampered_payload), encoding="utf-8")
    tampered_bundle_evidence = module._check_lifecycle_evidence(
        "mas.packaged_lifecycle_smoke.evidence",
        evidence,
        expected_distribution="mas",
        expected_app_paths=(installed_app,),
        require_expected_app_path=True,
    )
    assert tampered_bundle_evidence["status"] == "failed"

    _write_lifecycle_evidence(
        evidence,
        distribution="mas",
        app_path=installed_app,
    )
    installed_bundle_evidence = module._check_lifecycle_evidence(
        "mas.packaged_lifecycle_smoke.evidence",
        evidence,
        expected_distribution="mas",
        expected_app_paths=(installed_app,),
        require_expected_app_path=True,
    )
    assert installed_bundle_evidence["status"] == "passed"


def _args(root: Path, downloads: Path, **overrides):
    defaults = {
        "root": root,
        "mas_root": root.parent / "lantai_App Store",
        "downloads_dir": downloads,
        "version": "1.2.3",
        "arch": "arm64",
        "product_name": "澜台",
        "official_app": None,
        "official_dmg": None,
        "official_zip": None,
        "mas_app": None,
        "mas_pkg": None,
        "mas_dev_app": None,
        "mas_dev_profile": None,
        "mas_installed_app": None,
        "official_lifecycle_evidence": None,
        "official_rapid_lifecycle_evidence": None,
        "official_rapid_lifecycle_minimum_cycles": 100,
        "official_customization_upgrade_evidence": None,
        "mas_lifecycle_evidence": None,
        "mas_bookmark_evidence": None,
        "include_archived_mas_evidence": False,
        "runtime_migration_report": None,
        "runtime_migration_backup_restore_evidence": None,
        "legacy_writer_observation_evidence": [],
        "verify_notary_credentials": False,
        "format": "json",
        "require_final": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _write_rapid_lifecycle_evidence(
    path: Path,
    *,
    app_path: Path,
    cycles: int = 100,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "com.lantai.packaged-app-rapid-lifecycle-smoke.v1",
                "passed": True,
                "distribution": "official",
                "requested_cycles": cycles,
                "completed_cycles": cycles,
                "unique_backend_pid_count": cycles,
                "backend_pid_reuse_count": 0,
                "duplicate_backend_process_count": 0,
                "maximum_simultaneous_backend_processes": 1,
                "unique_desktop_instance_nonce_count": cycles,
                "orphan_process_count": 0,
                "app": {
                    "path": str(app_path.resolve()),
                    "artifact_sha256": _app_artifact_sha256(app_path),
                },
            }
        ),
        encoding="utf-8",
    )


def _write_customization_upgrade_evidence(path: Path, *, app_path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "com.lantai.packaged-customization-upgrade-smoke.v1",
                "passed": True,
                "app": {
                    "path": str(app_path.resolve()),
                    "artifact_sha256": _app_artifact_sha256(app_path),
                },
                "summary": {
                    "profile_id": "profile_v1_upgrade_fixture",
                    "profile_revision": 3,
                    "active_profile_id_before": "profile_v1_upgrade_fixture",
                    "active_profile_id_after": "profile_v1_upgrade_fixture",
                    "last_known_good_after": {"profile_v1_upgrade_fixture": 3},
                    "pending_boot_validation_after": {},
                    "effective_values": {
                        "appearance.palette": "warm",
                        "appearance.composer_size": "spacious",
                        "conversation.show_thinking": True,
                        "persona.presence": "quiet",
                    },
                    "changed_files": [],
                    "unexpected_files": [],
                },
            }
        ),
        encoding="utf-8",
    )


def test_rapid_lifecycle_evidence_requires_100_clean_single_backend_cycles(
    tmp_path: Path,
) -> None:
    module = _load_module()
    app_path = tmp_path / "澜台.app"
    _make_app(app_path, "com.lantai.app")
    evidence = tmp_path / "rapid.json"
    _write_rapid_lifecycle_evidence(evidence, app_path=app_path)

    passed = module._check_rapid_lifecycle_evidence(
        evidence,
        expected_app_path=app_path,
    )
    assert passed["status"] == "passed"

    payload = json.loads(evidence.read_text(encoding="utf-8"))
    payload["completed_cycles"] = 99
    payload["orphan_process_count"] = 1
    evidence.write_text(json.dumps(payload), encoding="utf-8")
    failed = module._check_rapid_lifecycle_evidence(
        evidence,
        expected_app_path=app_path,
    )
    assert failed["status"] == "failed"
    assert "completed_cycle_count_mismatch" in failed["details"]["issues"]
    assert "orphan_processes_detected" in failed["details"]["issues"]


def test_rapid_lifecycle_evidence_allows_sequential_backend_pid_reuse(
    tmp_path: Path,
) -> None:
    module = _load_module()
    app_path = tmp_path / "澜台.app"
    _make_app(app_path, "com.lantai.app")
    evidence = tmp_path / "rapid.json"
    _write_rapid_lifecycle_evidence(evidence, app_path=app_path)
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    payload["unique_backend_pid_count"] = 98
    payload["backend_pid_reuse_count"] = 2
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    result = module._check_rapid_lifecycle_evidence(
        evidence,
        expected_app_path=app_path,
    )

    assert result["status"] == "passed"


def test_rapid_lifecycle_evidence_honors_explicit_lower_minimum(
    tmp_path: Path,
) -> None:
    module = _load_module()
    app_path = tmp_path / "澜台.app"
    _make_app(app_path, "com.lantai.app")
    evidence = tmp_path / "rapid.json"
    _write_rapid_lifecycle_evidence(evidence, app_path=app_path, cycles=1)

    default_result = module._check_rapid_lifecycle_evidence(
        evidence,
        expected_app_path=app_path,
    )
    override_result = module._check_rapid_lifecycle_evidence(
        evidence,
        expected_app_path=app_path,
        minimum_cycles=1,
    )

    assert default_result["status"] == "failed"
    assert "requested_cycle_count_below_minimum" in default_result["details"][
        "issues"
    ]
    assert override_result["status"] == "passed"
    assert override_result["details"]["minimum_cycles"] == 1


def test_customization_upgrade_evidence_rejects_effective_value_regression(
    tmp_path: Path,
) -> None:
    module = _load_module()
    app_path = tmp_path / "澜台.app"
    _make_app(app_path, "com.lantai.app")
    evidence = tmp_path / "customization-upgrade.json"
    _write_customization_upgrade_evidence(evidence, app_path=app_path)

    passed = module._check_customization_upgrade_evidence(
        evidence,
        expected_app_path=app_path,
    )
    assert passed["status"] == "passed"

    payload = json.loads(evidence.read_text(encoding="utf-8"))
    payload["summary"]["effective_values"]["appearance.palette"] = "lantai"
    evidence.write_text(json.dumps(payload), encoding="utf-8")
    failed = module._check_customization_upgrade_evidence(
        evidence,
        expected_app_path=app_path,
    )
    assert failed["status"] == "failed"
    assert "effective_values_changed" in failed["details"]["issues"]


def _legacy_writer_observation_summary(
    *,
    distribution: str,
    fallback_write_count: int = 0,
    malformed_count: int = 0,
    unknown_schema_count: int = 0,
    invalid_writer_ids: list[str] | None = None,
) -> dict[str, object]:
    invalid_ids = invalid_writer_ids or []
    return {
        "schema": "com.lantai.runtime-legacy-writer-observation-summary.v1",
        "generated_at": "2026-07-18T00:00:00Z",
        "release_edition": distribution,
        "observation_file": "/tmp/legacy-writer-observations.jsonl",
        "observation_file_exists": True,
        "event_count": fallback_write_count,
        "fallback_write_count": fallback_write_count,
        "blocked_attempt_count": 0,
        "malformed_count": malformed_count,
        "unknown_schema_count": unknown_schema_count,
        "invalid_writer_ids": invalid_ids,
        "counts_by_writer": {
            writer_id: 0 for writer_id in _RUNTIME_MIGRATION_WRITER_IDS
        },
        "counts_by_outcome": {
            "allowed": fallback_write_count,
            "blocked": 0,
        },
        "writer_ids_without_observations": list(_RUNTIME_MIGRATION_WRITER_IDS),
        "zero_observations": (
            fallback_write_count == 0
            and malformed_count == 0
            and unknown_schema_count == 0
            and not invalid_ids
        ),
        "zero_fallback_writes": (
            fallback_write_count == 0
            and malformed_count == 0
            and unknown_schema_count == 0
            and not invalid_ids
        ),
    }


def _legacy_boundary_observation_summary(
    *,
    distribution: str,
    forbidden_activity_count: int = 0,
) -> dict[str, object]:
    return {
        "schema": "com.lantai.runtime-legacy-boundary-observation-summary.v1",
        "generated_at": "2026-08-30T00:00:00Z",
        "release_edition": distribution,
        "counts_by_kind": {
            "legacy_agent_constructed": 1,
            "legacy_loop_invoked": forbidden_activity_count,
            "legacy_runtime_adapter_selected": 0,
            "legacy_writer_attempted": 0,
        },
        "forbidden_activity_count": forbidden_activity_count,
        "evidence_valid": True,
        "observation_files_exist": True,
        "zero_forbidden_legacy_activity": forbidden_activity_count == 0,
        "construction_only": forbidden_activity_count == 0,
    }


def _write_lifecycle_evidence(
    path: Path,
    *,
    distribution: str,
    app_path: Path,
    include_legacy_writer_observation: bool = True,
    fallback_write_count: int = 0,
    checks_override: list[str] | None = None,
) -> None:
    checks = [
        "desktop_readiness",
        "distribution_tools",
        "browser_capabilities",
        "packaged_customization_integrity",
        "reminder_delivery_worker",
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
    ]
    basic_experience_matrix = [
        {"id": "initial_empty_chat_dom", "passed": True},
        {"id": "initial_empty_chat_backend", "passed": True},
        {"id": "first_user_feedback_within_budget", "passed": True},
        {"id": "first_assistant_feedback_within_budget", "passed": True},
        {"id": "second_user_feedback_within_budget", "passed": True},
        {"id": "second_assistant_feedback_within_budget", "passed": True},
        {"id": "two_primary_answers_with_optional_persona_lines", "passed": True},
        {"id": "no_empty_assistant_bubbles", "passed": True},
        {"id": "no_duplicate_assistant_chrome", "passed": True},
        {"id": "textarea_cleared_after_submit", "passed": True},
        {"id": "backend_state_consistent_with_submission_mode", "passed": True},
        {"id": "backend_pending_cleared", "passed": True},
        {"id": "route_isolation_empty_dom", "passed": True},
        {"id": "route_isolation_empty_backend", "passed": True},
        {"id": "route_restore_preserved_dom", "passed": True},
        {"id": "route_restore_preserved_backend", "passed": True},
        {"id": "final_dom_stable", "passed": True},
    ]
    smoke: dict[str, object] = {}
    smoke["model_success_path"] = {
        "provider_id": "lantai-smoke-openai",
        "model": "lantai-smoke-model",
        "configured": True,
        "request_count": 4,
    }
    if include_legacy_writer_observation:
        checks.append("legacy_writer_observation_summary")
        smoke["legacy_writer_observation"] = _legacy_writer_observation_summary(
            distribution=distribution,
            fallback_write_count=fallback_write_count,
        )
    checks.append("legacy_boundary_observation_summary")
    smoke["legacy_boundary_observation"] = _legacy_boundary_observation_summary(
        distribution=distribution,
    )
    basic_roundtrip = {
        "backend_turn_observed": True,
        "console_chat_post_count": 2,
        "experience_matrix": basic_experience_matrix,
        "final_backend": {
            "pending_user_message": "",
            "message_count": 4,
            "user_message_count": 2,
            "assistant_message_count": 2,
        },
        "restored_backend": {
            "pending_user_message": "",
            "message_count": 4,
            "user_message_count": 2,
            "assistant_message_count": 2,
        },
    }
    smoke["chat_renderer_basic_ui_send"] = dict(basic_roundtrip)
    smoke["post_restart_chat_renderer_basic_ui_send"] = dict(basic_roundtrip)
    provider_stream = {
        "ok": True,
        "thinking_visible_ms": 100,
        "partial_visible_ms": 200,
        "final_visible_ms": 300,
        "thinking": {"thinking_visible": True},
        "partial": {"partial_visible": True},
        "final": {
            "redirected_answer_visible": True,
            "old_full_answer_visible": False,
            "assistant_bubble_count": 2,
        },
        "rapid_queue": {
            "queued_submission_count": 2,
            "queued_before_first_answer": True,
            "queued_drafts": ["SECOND", "THIRD"],
            "submission_intents": ["supplement", "redirect"],
            "admitted_statuses": {
                "supplement": {"status": "admitted"},
                "redirect": {"status": "admitted"},
            },
            "final": {
                "user_bubble_count": 3,
                "assistant_bubble_count": 2,
                "empty_assistant_count": 0,
                "user_texts_in_fifo_order": True,
                "assistant_texts_in_fifo_order": True,
                "old_partial_preserved": True,
                "old_full_answer_absent": True,
                "superseded_answer_absent": True,
                "redirect_answer_present": True,
                "conversation_items_preserve_fifo_order": True,
                "supplement_disposition": {"status": "superseded"},
                "redirect_disposition": {"status": "applied"},
            },
        },
        "backend": {
            "pending_user_message": "",
            "message_count": 5,
            "user_message_count": 3,
            "assistant_message_count": 2,
            "user_texts_in_fifo_order": True,
        },
        "stop_resend": {
            "ok": True,
            "stop_terminal": "interrupted",
            "full_stopped_answer_absent": True,
            "final": {
                "user_bubble_count": 2,
                "assistant_bubble_count": 2,
                "empty_assistant_count": 0,
                "response_avatar_count": 2,
                "action_bar_count": 2,
            },
            "backend": {
                "user_message_count": 2,
                "assistant_message_count": 2,
                "pending_user_message": "",
            },
        },
        "route_switch": {
            "ok": True,
            "isolation": {
                "user_bubble_count": 0,
                "assistant_bubble_count": 0,
            },
            "restored": {
                "user_bubble_count": 1,
                "assistant_bubble_count": 1,
                "empty_assistant_count": 0,
                "response_avatar_count": 1,
                "action_bar_count": 1,
            },
            "backend": {
                "user_message_count": 1,
                "assistant_message_count": 2,
                "thinking_occurrence_count": 1,
                "answer_occurrence_count": 1,
                "pending_user_message": "",
            },
        },
    }
    smoke["chat_renderer_provider_stream_ui"] = dict(provider_stream)
    smoke["post_restart_chat_renderer_provider_stream_ui"] = dict(
        provider_stream
    )
    smoke["app_relaunch"] = {
        "backend_pid": 12345,
        "chat_id": "relaunch-chat",
        "chat_renderer_basic_ui_send": dict(basic_roundtrip),
        "chat_renderer_provider_stream_ui": dict(provider_stream),
        "runtime_controls": {
            "terminal_interrupt_noop": True,
            "canonical_runtime_controls": True,
            "archive_delete_cleanup": True,
        },
    }
    path.write_text(
        json.dumps(
            {
                "schema": "com.lantai.packaged-app-lifecycle-smoke.v1",
                "passed": True,
                "distribution": distribution,
                "restart_cycle": True,
                "app_relaunch_cycle": True,
                "app": {
                    "path": str(app_path.resolve()),
                    "artifact_sha256": _app_artifact_sha256(app_path),
                },
                "smoke": smoke,
                "checks": checks_override if checks_override is not None else checks,
            },
        ),
        encoding="utf-8",
    )


def _write_bookmark_evidence(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema": "com.lantai.mas-security-scoped-bookmark-smoke.v1",
                "passed": True,
                "distribution": "mas",
                "bookmark_restored_after_restart": True,
                "stale_bookmark_rejected": True,
                "security_scope_ref": "browser-security-scope:test",
                "security_scope_bookmark_sha256": "sha256:test",
                "sensitive_fields_present": False,
                "checks": [
                    "picker_security_scoped_bookmark",
                    "safe_storage_encrypted",
                    "no_raw_bookmark_or_path",
                    "restore_after_restart",
                    "upload_after_restore",
                    "stale_bookmark_rejected",
                ],
            },
        ),
        encoding="utf-8",
    )


def _write_runtime_migration_report(path: Path, *, stopped: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "writer_version": "lantai-runtime-migration-report/1",
                "dry_run": True,
                "side_effect_free": True,
                "side_effects_executed": 0,
                "workspace_dir": "/tmp/lantai-workspace",
                "release_edition": "official",
                "totals": {
                    "source_count": 0,
                    "bytes": 0,
                    "record_count": 0,
                    "migratable_count": 0,
                    "skipped_count": 0,
                    "conflict_count": 0,
                    "issue_count": 0,
                },
                "backup_targets": [],
                "sources": [],
                "writer_inventory": [
                    {
                        "writer_id": writer_id,
                        "source_type": "experience_ledger",
                        "legacy_writer": f"legacy.{writer_id}",
                        "legacy_path_glob": "/tmp/lantai-workspace/legacy/*",
                        "canonical_writer": "lantai.runtime.store.RuntimeRunStore",
                        "last_dependent_readers": ["legacy.reader"],
                        "migration_status": (
                            "legacy_write_stopped_read_compatibility"
                            if stopped
                            else "legacy_write_active"
                        ),
                        "legacy_write_allowed": not stopped,
                        "can_stop_writes": stopped,
                        "stop_writes_blockers": []
                        if stopped
                        else ["two_official_release_cycles_without_fallback_write"],
                        "delete_preconditions": ["read_only_legacy_adapter_retained"],
                        "verification": ["tests/unit/app/test_runtime_migration_report.py"],
                        "observed_source_count": 0,
                        "observed_record_count": 0,
                        "observed_issue_count": 0,
                        "observed_paths": [],
                    }
                    for writer_id in _RUNTIME_MIGRATION_WRITER_IDS
                ],
            },
        ),
        encoding="utf-8",
    )


def _write_runtime_migration_backup_restore_evidence(
    path: Path,
    *,
    passed: bool = True,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    checks = [
        "fixture_legacy_sources_seeded",
        "dry_run_report_side_effect_free",
        "legacy_sources_backed_up_to_scratch",
        "legacy_sources_restored_from_scratch_backup",
        "restored_report_matches_original_signatures",
        "downgrade_readers_parse_restored_legacy_sources",
    ]
    path.write_text(
        json.dumps(
            {
                "schema": "com.lantai.runtime-migration-backup-restore-smoke.v1",
                "passed": passed,
                "release_edition": "official",
                "fixture_seeded": True,
                "source_workspace_side_effect_free": True,
                "source_count": 5,
                "backup_entry_count": 5,
                "restored_source_count": 5,
                "source_tree_restored_exactly": True,
                "signatures_match": True,
                "checks": checks if passed else checks[:-1],
                "downgrade_readers": {
                    "experience_runtime_event_count": 1,
                    "task_run_contract_count": 1,
                    "evaluation_report_count": 1,
                    "mission_config_read": True,
                    "mission_prd_read": True,
                    "browser_task_record_count": 1,
                },
            },
        ),
        encoding="utf-8",
    )


def test_notary_credentials_report_names_without_secret_values(monkeypatch) -> None:
    module = _load_module()
    monkeypatch.setenv("APPLE_ID", "person@example.com")
    monkeypatch.setenv("APPLE_APP_SPECIFIC_PASSWORD", "secret-password")
    monkeypatch.setenv("APPLE_TEAM_ID", "TEAMID123")

    check = module._check_notary_credentials(dict(module.os.environ))

    assert check["status"] == "passed"
    assert check["details"]["mode"] == "apple_id_password"
    assert check["details"]["present_environment_names"] == [
        "APPLE_ID",
        "APPLE_APP_SPECIFIC_PASSWORD",
        "APPLE_TEAM_ID",
    ]
    assert "secret-password" not in json.dumps(check, ensure_ascii=False)
    assert "person@example.com" not in json.dumps(check, ensure_ascii=False)


def test_live_notary_credentials_report_apple_agreement_block_without_secrets(
    monkeypatch,
) -> None:
    module = _load_module()
    monkeypatch.setenv("LANTAI_NOTARY_KEYCHAIN_PROFILE", "lantai-notary")
    monkeypatch.setenv("APPLE_ID", "person@example.com")
    monkeypatch.setenv("APPLE_APP_SPECIFIC_PASSWORD", "secret-password")
    monkeypatch.setenv("APPLE_TEAM_ID", "TEAMID123")
    calls = []

    def fake_run(command, *, timeout=20.0):
        calls.append((command, timeout))
        return module.CommandResult(
            1,
            stderr=(
                "Error: HTTP status code: 403. A required agreement is missing "
                "or has expired for person@example.com using secret-password "
                "and team TEAMID123 via lantai-notary."
            ),
        )

    monkeypatch.setattr(module, "_run_command", fake_run)

    check = module._check_notary_credentials(
        dict(module.os.environ),
        verify_live=True,
    )

    assert check["status"] == "failed"
    assert check["summary"] == "Notary credentials failed live validation"
    assert check["details"]["mode"] == "keychain_profile"
    assert check["details"]["live_verification"]["issue"] == (
        "apple_agreement_missing_or_expired"
    )
    assert calls == [
        (
            [
                "xcrun",
                "notarytool",
                "history",
                "--keychain-profile",
                "lantai-notary",
            ],
            30.0,
        )
    ]
    serialized = json.dumps(check, ensure_ascii=False)
    assert "person@example.com" not in serialized
    assert "secret-password" not in serialized
    assert "TEAMID123" not in serialized
    assert "lantai-notary" not in serialized


def test_live_notary_credentials_pass_with_keychain_profile(monkeypatch) -> None:
    module = _load_module()
    monkeypatch.setenv("LANTAI_NOTARY_KEYCHAIN_PROFILE", "lantai-notary")

    def fake_run(command, *, timeout=20.0):
        if command[:3] == ["xcrun", "notarytool", "history"]:
            return module.CommandResult(0, stdout="Successfully received history.")
        return module.CommandResult(1, stderr=f"unexpected command: {command}")

    monkeypatch.setattr(module, "_run_command", fake_run)

    check = module._check_notary_credentials(
        dict(module.os.environ),
        verify_live=True,
    )

    assert check["status"] == "passed"
    assert check["summary"] == "Notary credentials passed live validation"
    assert check["details"]["live_verification"]["verified"] is True


def test_bookmark_evidence_rejects_legacy_or_secret_bearing_payload(
    tmp_path: Path,
) -> None:
    module = _load_module()
    evidence = tmp_path / "mas-security-scoped-bookmark-smoke.json"
    evidence.write_text(
        json.dumps(
            {
                "distribution": "mas",
                "bookmark_restored_after_restart": True,
                "stale_bookmark_rejected": True,
                "sensitive_fields_present": True,
            },
        ),
        encoding="utf-8",
    )

    check = module._check_bookmark_evidence(evidence)

    assert check["status"] == "failed"
    assert check["details"]["expected_schema"] == (
        "com.lantai.mas-security-scoped-bookmark-smoke.v1"
    )
    assert "picker_security_scoped_bookmark" in check["details"]["missing_checks"]
    assert check["details"]["sensitive_fields_present"] is True


def test_runtime_migration_report_rejects_active_legacy_writers(
    tmp_path: Path,
) -> None:
    module = _load_module()
    evidence = tmp_path / "runtime-migration-dry-run-report.json"
    _write_runtime_migration_report(evidence, stopped=False)

    check = module._check_runtime_migration_report(evidence)

    assert check["status"] == "failed"
    assert check["details"]["schema_version"] == 2
    assert "legacy_task_run_snapshot_writer" in check["details"]["active_writer_ids"]
    assert "legacy_task_run_snapshot_writer" in check["details"]["not_ready_writer_ids"]


def test_runtime_migration_backup_restore_evidence_requires_full_smoke(
    tmp_path: Path,
) -> None:
    module = _load_module()
    evidence = tmp_path / "runtime-migration-backup-restore-smoke.json"
    _write_runtime_migration_backup_restore_evidence(evidence, passed=False)

    check = module._check_runtime_migration_backup_restore_evidence(evidence)

    assert check["status"] == "failed"
    assert check["details"]["expected_schema"] == (
        "com.lantai.runtime-migration-backup-restore-smoke.v1"
    )
    assert "downgrade_readers_parse_restored_legacy_sources" in check["details"][
        "missing_checks"
    ]


def test_runtime_migration_backup_restore_evidence_passes_complete_smoke(
    tmp_path: Path,
) -> None:
    module = _load_module()
    evidence = tmp_path / "runtime-migration-backup-restore-smoke.json"
    _write_runtime_migration_backup_restore_evidence(evidence)

    check = module._check_runtime_migration_backup_restore_evidence(evidence)

    assert check["status"] == "passed"
    assert check["details"]["source_count"] == 5
    assert check["details"]["downgrade_readers"]["browser_task_record_count"] == 1


def test_lifecycle_evidence_requires_current_packaged_reliability_smoke(
    tmp_path: Path,
) -> None:
    module = _load_module()
    app_path = tmp_path / "澜台.app"
    _make_app(app_path, "com.lantai.app")
    evidence = tmp_path / "official-packaged-lifecycle-smoke.json"
    legacy_checks = [
        "desktop_readiness",
        "distribution_tools",
        "browser_capabilities",
        "legacy_writer_observation_summary",
        "runtime_restart_decision_projection",
        "loop_trial_recovery_export",
        "browser_content_trial_handoff",
        "browser_content_renderer_export",
        "runtime_event_sse_reconnect",
        "chat_renderer_stream_reconnect",
        "chat_renderer_live_native_delta",
        "backend_sigkill_restart",
        "post_restart_runtime_projection",
        "post_restart_browser_content_renderer_export",
        "post_restart_chat_stream_reconnect",
    ]
    _write_lifecycle_evidence(
        evidence,
        distribution="official",
        app_path=app_path,
        checks_override=legacy_checks,
    )

    check = module._check_lifecycle_evidence(
        "official.packaged_lifecycle_smoke.evidence",
        evidence,
        expected_distribution="official",
        expected_app_paths=(app_path,),
    )

    assert check["status"] == "failed"
    assert "console_model_success_path" in check["details"]["missing_checks"]
    assert "chat_renderer_basic_ui_send" in check["details"]["missing_checks"]
    assert "chat_renderer_provider_stream_ui" in check["details"]["missing_checks"]
    assert "chat_renderer_rapid_queue_ui" in check["details"]["missing_checks"]
    assert (
        "chat_renderer_backend_model_roundtrip"
        in check["details"]["missing_checks"]
    )
    assert (
        "chat_renderer_basic_experience_matrix"
        in check["details"]["missing_checks"]
    )
    assert "post_restart_chat_basic_ui_send" in check["details"]["missing_checks"]
    assert (
        "post_restart_chat_provider_stream_ui"
        in check["details"]["missing_checks"]
    )
    assert "post_restart_chat_rapid_queue_ui" in check["details"]["missing_checks"]
    assert (
        "post_restart_chat_basic_experience_matrix"
        in check["details"]["missing_checks"]
    )
    assert "canonical_runtime_controls" in check["details"]["missing_checks"]
    assert "post_restart_reminder_delivery_dedupe" in check["details"]["missing_checks"]
    assert "electron_app_relaunch" in check["details"]["missing_checks"]
    assert (
        "post_app_relaunch_chat_provider_stream_ui"
        in check["details"]["missing_checks"]
    )


def test_lifecycle_evidence_rejects_forbidden_legacy_boundary_activity(
    tmp_path: Path,
) -> None:
    module = _load_module()
    app_path = tmp_path / "澜台.app"
    _make_app(app_path, "com.lantai.app")
    evidence = tmp_path / "official-packaged-lifecycle-smoke.json"
    _write_lifecycle_evidence(
        evidence,
        distribution="official",
        app_path=app_path,
    )
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    payload["smoke"]["legacy_boundary_observation"] = (
        _legacy_boundary_observation_summary(
            distribution="official",
            forbidden_activity_count=1,
        )
    )
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    check = module._check_lifecycle_evidence(
        "official.packaged_lifecycle_smoke.evidence",
        evidence,
        expected_distribution="official",
        expected_app_paths=(app_path,),
    )

    assert check["status"] == "failed"
    assert check["details"]["legacy_boundary_issues"] == [
        "legacy_boundary_forbidden_activity"
    ]


def test_lifecycle_evidence_requires_structured_electron_relaunch_proof(
    tmp_path: Path,
) -> None:
    module = _load_module()
    app_path = tmp_path / "澜台.app"
    _make_app(app_path, "com.lantai.app")
    evidence = tmp_path / "official-packaged-lifecycle-smoke.json"
    _write_lifecycle_evidence(
        evidence,
        distribution="official",
        app_path=app_path,
    )
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    payload["smoke"]["app_relaunch"]["runtime_controls"][
        "archive_delete_cleanup"
    ] = False
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    check = module._check_lifecycle_evidence(
        "official.packaged_lifecycle_smoke.evidence",
        evidence,
        expected_distribution="official",
        expected_app_paths=(app_path,),
    )

    assert check["status"] == "failed"
    assert check["details"]["app_relaunch_cycle"] is True
    assert check["details"]["app_relaunch_issues"] == [
        "app_relaunch_runtime_control_missing:archive_delete_cleanup"
    ]


def test_lifecycle_evidence_requires_basic_experience_matrix_details(
    tmp_path: Path,
) -> None:
    module = _load_module()
    app_path = tmp_path / "澜台.app"
    _make_app(app_path, "com.lantai.app")
    evidence = tmp_path / "official-packaged-lifecycle-smoke.json"
    _write_lifecycle_evidence(
        evidence,
        distribution="official",
        app_path=app_path,
    )
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    payload["smoke"]["chat_renderer_basic_ui_send"]["experience_matrix"] = [
        {"id": "initial_empty_chat_dom", "passed": True},
        {"id": "initial_empty_chat_backend", "passed": False},
    ]
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    check = module._check_lifecycle_evidence(
        "official.packaged_lifecycle_smoke.evidence",
        evidence,
        expected_distribution="official",
        expected_app_paths=(app_path,),
    )

    assert check["status"] == "failed"
    assert check["details"]["failed_basic_experience_checks"] == {
        "chat_renderer_basic_ui_send": ["initial_empty_chat_backend"]
    }
    assert "backend_pending_cleared" in check["details"][
        "missing_basic_experience_checks"
    ]["chat_renderer_basic_ui_send"]


def test_lifecycle_evidence_requires_backend_model_roundtrip(
    tmp_path: Path,
) -> None:
    module = _load_module()
    app_path = tmp_path / "澜台.app"
    _make_app(app_path, "com.lantai.app")
    evidence = tmp_path / "official-packaged-lifecycle-smoke.json"
    _write_lifecycle_evidence(
        evidence,
        distribution="official",
        app_path=app_path,
    )
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    payload["smoke"]["chat_renderer_basic_ui_send"]["backend_turn_observed"] = False
    payload["smoke"]["chat_renderer_basic_ui_send"]["console_chat_post_count"] = 0
    payload["smoke"]["chat_renderer_basic_ui_send"]["final_backend"] = {
        "pending_user_message": "",
        "message_count": 0,
        "user_message_count": 0,
        "assistant_message_count": 0,
    }
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    check = module._check_lifecycle_evidence(
        "official.packaged_lifecycle_smoke.evidence",
        evidence,
        expected_distribution="official",
        expected_app_paths=(app_path,),
    )

    assert check["status"] == "failed"
    assert check["details"]["failed_backend_model_roundtrip"][
        "chat_renderer_basic_ui_send"
    ] == [
        "backend_turn_not_observed",
        "console_chat_post_count_not_two",
        "final_backend_user_count_not_two",
        "final_backend_assistant_count_below_two",
    ]


def test_lifecycle_evidence_requires_provider_rapid_queue_details(
    tmp_path: Path,
) -> None:
    module = _load_module()
    app_path = tmp_path / "澜台.app"
    _make_app(app_path, "com.lantai.app")
    evidence = tmp_path / "official-packaged-lifecycle-smoke.json"
    _write_lifecycle_evidence(
        evidence,
        distribution="official",
        app_path=app_path,
    )
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    provider = payload["smoke"]["chat_renderer_provider_stream_ui"]
    provider["rapid_queue"]["queued_before_first_answer"] = False
    provider["rapid_queue"]["final"]["assistant_bubble_count"] = 3
    provider["backend"]["pending_user_message"] = "THIRD"
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    check = module._check_lifecycle_evidence(
        "official.packaged_lifecycle_smoke.evidence",
        evidence,
        expected_distribution="official",
        expected_app_paths=(app_path,),
    )

    assert check["status"] == "failed"
    assert check["details"]["failed_provider_stream_ui"][
        "chat_renderer_provider_stream_ui"
    ] == [
        "rapid_queue_not_before_first_answer",
        "rapid_queue_assistant_bubble_count_not_two",
        "rapid_queue_backend_pending_user_message",
    ]


def test_lifecycle_evidence_accepts_redirected_successor_without_retired_answer(
    tmp_path: Path,
) -> None:
    module = _load_module()
    app_path = tmp_path / "澜台.app"
    _make_app(app_path, "com.lantai.app")
    evidence = tmp_path / "official-packaged-lifecycle-smoke.json"
    _write_lifecycle_evidence(evidence, distribution="official", app_path=app_path)

    check = module._check_lifecycle_evidence(
        "official.packaged_lifecycle_smoke.evidence",
        evidence,
        expected_distribution="official",
        expected_app_paths=(app_path,),
    )

    assert check["status"] == "passed"


def test_lifecycle_evidence_rejects_retired_answer_or_missing_redirect(
    tmp_path: Path,
) -> None:
    module = _load_module()
    app_path = tmp_path / "澜台.app"
    _make_app(app_path, "com.lantai.app")
    evidence = tmp_path / "official-packaged-lifecycle-smoke.json"
    _write_lifecycle_evidence(evidence, distribution="official", app_path=app_path)
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    provider = payload["smoke"]["chat_renderer_provider_stream_ui"]
    provider["final"]["old_full_answer_visible"] = True
    provider["rapid_queue"]["final"]["redirect_answer_present"] = False
    provider["rapid_queue"]["final"]["supplement_disposition"]["status"] = (
        "applied"
    )
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    check = module._check_lifecycle_evidence(
        "official.packaged_lifecycle_smoke.evidence",
        evidence,
        expected_distribution="official",
        expected_app_paths=(app_path,),
    )

    assert check["status"] == "failed"
    assert check["details"]["failed_provider_stream_ui"][
        "chat_renderer_provider_stream_ui"
    ] == [
        "retired_full_answer_visible",
        "rapid_queue_redirect_answer_missing",
        "rapid_queue_supplement_disposition_not_superseded",
    ]


def test_legacy_writer_observation_evidence_requires_both_release_editions() -> None:
    module = _load_module()

    check = module._check_legacy_writer_observation_evidence(
        official_lifecycle_evidence=None,
        mas_lifecycle_evidence=None,
        standalone_evidence_paths=[],
    )

    assert check["status"] == "missing"
    assert check["details"]["missing_release_editions"] == ["mas", "official"]


def test_legacy_writer_observation_evidence_passes_lifecycle_summaries(
    tmp_path: Path,
) -> None:
    module = _load_module()
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    official_app = downloads / "澜台.app"
    mas_app = downloads / "澜台-MAS.app"
    _make_app(official_app, "com.lantai.app")
    _make_app(mas_app, "com.lantai.appstore")
    official_lifecycle = tmp_path / "official-packaged-lifecycle-smoke.json"
    mas_lifecycle = tmp_path / "mas-packaged-lifecycle-smoke.json"
    _write_lifecycle_evidence(
        official_lifecycle,
        distribution="official",
        app_path=official_app,
    )
    _write_lifecycle_evidence(
        mas_lifecycle,
        distribution="mas",
        app_path=mas_app,
    )

    check = module._check_legacy_writer_observation_evidence(
        official_lifecycle_evidence=official_lifecycle,
        mas_lifecycle_evidence=mas_lifecycle,
        standalone_evidence_paths=[],
    )

    assert check["status"] == "passed"
    assert check["details"]["observed_release_editions"] == ["mas", "official"]
    assert not check["details"]["missing_release_editions"]


def test_legacy_writer_observation_evidence_rejects_fallback_writes(
    tmp_path: Path,
) -> None:
    module = _load_module()
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    official_app = downloads / "澜台.app"
    mas_app = downloads / "澜台-MAS.app"
    _make_app(official_app, "com.lantai.app")
    _make_app(mas_app, "com.lantai.appstore")
    official_lifecycle = tmp_path / "official-packaged-lifecycle-smoke.json"
    mas_lifecycle = tmp_path / "mas-packaged-lifecycle-smoke.json"
    _write_lifecycle_evidence(
        official_lifecycle,
        distribution="official",
        app_path=official_app,
    )
    _write_lifecycle_evidence(
        mas_lifecycle,
        distribution="mas",
        app_path=mas_app,
        fallback_write_count=1,
    )

    check = module._check_legacy_writer_observation_evidence(
        official_lifecycle_evidence=official_lifecycle,
        mas_lifecycle_evidence=mas_lifecycle,
        standalone_evidence_paths=[],
    )

    assert check["status"] == "failed"
    mas_summary = next(
        item
        for item in check["details"]["summaries"]
        if item["release_edition"] == "mas"
    )
    assert "fallback_writes_observed" in mas_summary["issues"]


def test_legacy_writer_observation_edition_evidence_rejects_fallback_writes(
    tmp_path: Path,
) -> None:
    module = _load_module()
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    official_app = downloads / "澜台.app"
    _make_app(official_app, "com.lantai.app")
    official_lifecycle = tmp_path / "official-packaged-lifecycle-smoke.json"
    _write_lifecycle_evidence(
        official_lifecycle,
        distribution="official",
        app_path=official_app,
        fallback_write_count=1,
    )

    check = module._check_legacy_writer_observation_edition_evidence(
        "official.legacy_writer_observation.evidence",
        release_edition="official",
        lifecycle_evidence=official_lifecycle,
        lifecycle_source="official_lifecycle",
        standalone_evidence_paths=[],
    )

    assert check["status"] == "failed"
    assert check["details"]["release_edition"] == "official"
    assert "fallback_writes_observed" in check["details"]["summaries"][0]["issues"]


def test_parse_entitlements_ignores_codesign_stderr_trailer() -> None:
    module = _load_module()
    output = (
        "Executable=/Users/example/Downloads/澜台-MAS.app/Contents/MacOS/澜台\n"
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        "<plist version=\"1.0\"><dict>"
        "<key>com.apple.security.app-sandbox</key><true/>"
        "<key>com.apple.security.files.user-selected.read-write</key><true/>"
        "</dict></plist>\n"
        "warning: Specifying ':' in the path is deprecated and will not work\n"
    )

    entitlements = module._parse_entitlements(output)

    assert entitlements["com.apple.security.app-sandbox"] is True
    assert entitlements["com.apple.security.files.user-selected.read-write"] is True


def test_required_check_ids_are_stage_specific() -> None:
    module = _load_module()
    report = {
        "overall": "failed",
        "checks": [
            {"id": "official.download_app.signature", "status": "passed"},
            {"id": "official.download_app.gatekeeper_notarized", "status": "failed"},
            {"id": "mas.download_app.sandbox", "status": "passed"},
        ],
    }

    required = module._normalize_required_check_ids(
        ["official.download_app.signature, mas.download_app.sandbox"]
    )
    assert required == ["official.download_app.signature", "mas.download_app.sandbox"]
    assert module._required_check_failures(report, required) == []

    failures = module._required_check_failures(
        report,
        [
            "official.download_app.gatekeeper_notarized",
            "mas.download_pkg.signature",
        ],
    )
    assert [failure["id"] for failure in failures] == [
        "official.download_app.gatekeeper_notarized",
        "mas.download_pkg.signature",
    ]
    assert failures[0]["status"] == "failed"
    assert failures[1]["status"] == "missing_check"


def test_audit_marks_unnotarized_official_download_app_failed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    root = _make_root(tmp_path)
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    _make_app(downloads / "澜台.app", "com.lantai.app")
    (downloads / "澜台-1.2.3-arm64-internal.dmg").write_bytes(b"dmg")
    (downloads / "澜台-1.2.3-arm64-internal.zip").write_bytes(b"zip")

    def fake_run(command, *, timeout=20.0):
        if command[:2] == ["security", "find-identity"]:
            return module.CommandResult(
                0,
                stdout='1) ABC "Developer ID Application: YANG SHEN (766F8BQ9T3)"',
            )
        if command[:3] == ["codesign", "--verify", "--deep"]:
            return module.CommandResult(0)
        if command[:2] == ["codesign", "-dv"]:
            return module.CommandResult(
                0,
                stderr=(
                    "Authority=Developer ID Application: YANG SHEN (766F8BQ9T3)\n"
                    "TeamIdentifier=766F8BQ9T3\n"
                ),
            )
        if command[:2] == ["spctl", "--assess"]:
            return module.CommandResult(
                3,
                stderr=(
                    f"{downloads / '澜台.app'}: rejected\n"
                    "source=Unnotarized Developer ID\n"
                ),
            )
        if command[:2] == ["mdfind", 'kMDItemCFBundleIdentifier == "com.lantai.appstore"']:
            return module.CommandResult(0, stdout="")
        return module.CommandResult(1, stderr="unexpected command")

    monkeypatch.setattr(module, "_run_command", fake_run)

    report = module.audit_release_evidence(_args(root, downloads))
    check_by_id = {check["id"]: check for check in report["checks"]}

    assert report["overall"] == "failed"
    assert check_by_id["official.download_app.gatekeeper_notarized"]["status"] == "failed"
    assert check_by_id["official.download_bundles.present"]["status"] == "passed"


def test_audit_auto_discovers_latest_local_official_lifecycle_evidence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    root = _make_root(tmp_path)
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    official_app = downloads / "澜台.app"
    _make_app(official_app, "com.lantai.app")
    (downloads / "澜台-1.2.3-arm64-internal.dmg").write_bytes(b"dmg")
    (downloads / "澜台-1.2.3-arm64-internal.zip").write_bytes(b"zip")
    evidence_dir = root / "build/release-evidence"
    evidence_dir.mkdir(parents=True)
    official_lifecycle = evidence_dir / "official-local-packaged-lifecycle-smoke.json"
    _write_lifecycle_evidence(
        official_lifecycle,
        distribution="official",
        app_path=official_app,
    )

    def fake_run(command, *, timeout=20.0):
        if command[:2] == ["security", "find-identity"]:
            return module.CommandResult(
                0,
                stdout='1) ABC "Developer ID Application: YANG SHEN (766F8BQ9T3)"',
            )
        if command[:3] == ["codesign", "--verify", "--deep"]:
            return module.CommandResult(0)
        if command[:2] == ["codesign", "-dv"]:
            return module.CommandResult(
                0,
                stderr=(
                    "Authority=Developer ID Application: YANG SHEN (766F8BQ9T3)\n"
                    "TeamIdentifier=766F8BQ9T3\n"
                ),
            )
        if command[:2] == ["spctl", "--assess"]:
            return module.CommandResult(3, stderr="source=Unnotarized Developer ID\n")
        if command[:2] == ["mdfind", 'kMDItemCFBundleIdentifier == "com.lantai.appstore"']:
            return module.CommandResult(0, stdout="")
        return module.CommandResult(1, stderr=f"unexpected command: {command}")

    monkeypatch.setattr(module, "_run_command", fake_run)

    report = module.audit_release_evidence(_args(root, downloads))
    check_by_id = {check["id"]: check for check in report["checks"]}

    assert check_by_id["official.packaged_lifecycle_smoke.evidence"]["status"] == "passed"
    assert report["paths"]["official_lifecycle_evidence"] == str(official_lifecycle)
    assert report["paths"]["official_lifecycle_evidence_source"] == "auto"


def test_audit_can_verify_final_release_evidence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    root = _make_root(tmp_path)
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    official_app = downloads / "澜台.app"
    mas_app = downloads / "澜台-MAS.app"
    mas_dev_app = downloads / "澜台-MAS-dev.app"
    installed_mas_app = tmp_path / "Applications" / "澜台.app"
    _make_app(official_app, "com.lantai.app")
    _make_app(mas_app, "com.lantai.appstore")
    _make_app(mas_dev_app, "com.lantai.appstore")
    _make_app(installed_mas_app, "com.lantai.appstore")
    _write_app_store_receipt(installed_mas_app)
    (downloads / "澜台-1.2.3-arm64-internal.dmg").write_bytes(b"dmg")
    (downloads / "澜台-1.2.3-arm64-internal.zip").write_bytes(b"zip")
    (downloads / "澜台-MAS-1.2.3-arm64.pkg").write_bytes(b"pkg")
    bookmark_dir = root.parent / "lantai_App Store" / "build/release-evidence"
    bookmark_dir.mkdir(parents=True)
    bookmark_evidence = bookmark_dir / "mas-security-scoped-bookmark-smoke.json"
    _write_bookmark_evidence(bookmark_evidence)
    runtime_migration_report = (
        root / "build/release-evidence/runtime-migration-dry-run-report.json"
    )
    _write_runtime_migration_report(runtime_migration_report, stopped=True)
    runtime_migration_backup_restore = (
        root / "build/release-evidence/runtime-migration-backup-restore-smoke.json"
    )
    _write_runtime_migration_backup_restore_evidence(
        runtime_migration_backup_restore,
    )
    official_lifecycle = (
        root
        / "release/builds/1.2.3-auto-arm64/release-evidence/official-packaged-lifecycle-smoke.json"
    )
    mas_lifecycle = (
        root.parent
        / "lantai_App Store/release/builds/1.2.3-auto-arm64/release-evidence/mas-packaged-lifecycle-smoke.json"
    )
    official_lifecycle.parent.mkdir(parents=True)
    mas_lifecycle.parent.mkdir(parents=True)
    _write_lifecycle_evidence(
        official_lifecycle,
        distribution="official",
        app_path=official_app,
    )
    _write_rapid_lifecycle_evidence(
        official_lifecycle.parent / "official-packaged-rapid-lifecycle-smoke.json",
        app_path=official_app,
    )
    _write_customization_upgrade_evidence(
        official_lifecycle.parent
        / "official-packaged-customization-upgrade-smoke.json",
        app_path=official_app,
    )
    _write_lifecycle_evidence(
        mas_lifecycle,
        distribution="mas",
        app_path=installed_mas_app,
    )

    def fake_run(command, *, timeout=20.0):
        joined = " ".join(command)
        if command[:2] == ["security", "find-identity"]:
            return module.CommandResult(
                0,
                stdout="\n".join(
                    [
                        '1) ABC "Developer ID Application: YANG SHEN (766F8BQ9T3)"',
                        '2) DEF "Apple Distribution: YANG SHEN (766F8BQ9T3)"',
                        '3) GHI "Apple Development: YANG SHEN (BLD7W5XJ3W)"',
                        '4) JKL "3rd Party Mac Developer Installer: YANG SHEN (766F8BQ9T3)"',
                    ],
                ),
            )
        if command[:3] == ["codesign", "--verify", "--deep"]:
            return module.CommandResult(0)
        if command[:2] == ["codesign", "-dv"]:
            authority = (
                "Developer ID Application: YANG SHEN (766F8BQ9T3)"
                if "澜台.app" in joined and "Applications" not in joined and "-MAS" not in joined
                else "Apple Development: YANG SHEN (BLD7W5XJ3W)"
                if "MAS-dev" in joined
                else "Apple Distribution: YANG SHEN (766F8BQ9T3)"
            )
            return module.CommandResult(
                0,
                stderr=(
                    f"Authority={authority}\n"
                    "TeamIdentifier=766F8BQ9T3\n"
                    "Notarization Ticket=stapled\n"
                ),
            )
        if command[:3] == ["codesign", "-d", "--entitlements"]:
            return module.CommandResult(
                0,
                stdout=(
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<plist version="1.0"><dict>'
                    "<key>com.apple.security.app-sandbox</key><true/>"
                    "<key>com.apple.security.files.user-selected.read-write</key><true/>"
                    "</dict></plist>"
                ),
            )
        if command[:2] == ["spctl", "--assess"]:
            return module.CommandResult(
                0,
                stderr=f"{official_app}: accepted\nsource=Notarized Developer ID\n",
            )
        if command[:2] == ["pkgutil", "--check-signature"]:
            return module.CommandResult(
                0,
                stdout="1. 3rd Party Mac Developer Installer: YANG SHEN (766F8BQ9T3)",
            )
        if command[:2] == ["mdfind", 'kMDItemCFBundleIdentifier == "com.lantai.appstore"']:
            return module.CommandResult(0, stdout=str(installed_mas_app))
        return module.CommandResult(1, stderr=f"unexpected command: {command}")

    monkeypatch.setattr(module, "_run_command", fake_run)

    report = module.audit_release_evidence(
        _args(
            root,
            downloads,
            mas_installed_app=installed_mas_app,
            include_archived_mas_evidence=True,
        ),
    )
    check_by_id = {check["id"]: check for check in report["checks"]}

    assert report["overall"] == "verified"
    assert check_by_id["official.download_app.gatekeeper_notarized"]["status"] == "passed"
    assert check_by_id["official.packaged_lifecycle_smoke.evidence"]["status"] == "passed"
    assert check_by_id["official.legacy_writer_observation.evidence"]["status"] == "passed"
    assert check_by_id["mas.download_pkg.signature"]["status"] == "passed"
    assert check_by_id["mas.packaged_lifecycle_smoke.evidence"]["status"] == "passed"
    assert check_by_id["mas.legacy_writer_observation.evidence"]["status"] == "passed"
    assert check_by_id["mas.installed_app.signature"]["status"] == "passed"
    assert check_by_id["mas.installed_app.sandbox"]["status"] == "passed"
    assert check_by_id["mas.installed_app.app_store_receipt"]["status"] == "passed"
    assert check_by_id["mas.security_scoped_bookmark_smoke.evidence"]["status"] == "passed"
    assert check_by_id["close01.runtime_migration_dry_run.evidence"]["status"] == "passed"
    assert check_by_id["close01.runtime_migration_backup_restore.evidence"][
        "status"
    ] == "passed"
    assert check_by_id["close01.legacy_writer_observation.evidence"][
        "status"
    ] == "passed"
    assert report["paths"]["official_lifecycle_evidence"] == str(official_lifecycle)
    assert report["paths"]["mas_lifecycle_evidence"] == str(mas_lifecycle)
    assert report["paths"]["mas_bookmark_evidence"] == str(bookmark_evidence)
    assert report["paths"]["runtime_migration_report"] == str(runtime_migration_report)
    assert report["paths"]["runtime_migration_backup_restore_evidence"] == str(
        runtime_migration_backup_restore
    )
    assert report["paths"]["legacy_writer_observation_evidence"] == []
    assert report["paths"]["official_lifecycle_evidence_source"] == "auto"
    assert report["paths"]["mas_lifecycle_evidence_source"] == "auto"
    assert report["paths"]["mas_bookmark_evidence_source"] == "auto"
    assert report["paths"]["runtime_migration_report_source"] == "auto"
    assert report["paths"]["runtime_migration_backup_restore_evidence_source"] == "auto"
    assert report["paths"]["legacy_writer_observation_evidence_source"] == "auto"


def test_default_audit_is_official_only(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    root = _make_root(tmp_path)
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    official_app = downloads / "澜台.app"
    _make_app(official_app, "com.lantai.app")
    (downloads / "澜台-1.2.3-arm64-internal.dmg").write_bytes(b"dmg")
    (downloads / "澜台-1.2.3-arm64-internal.zip").write_bytes(b"zip")

    def fake_run(command, *, timeout=20.0):
        if command[:2] == ["security", "find-identity"]:
            return module.CommandResult(
                0,
                stdout='1) ABC "Developer ID Application: YANG SHEN (766F8BQ9T3)"',
            )
        if command[:3] == ["codesign", "--verify", "--deep"]:
            return module.CommandResult(0)
        if command[:2] == ["codesign", "-dv"]:
            return module.CommandResult(
                0,
                stderr=(
                    "Authority=Developer ID Application: YANG SHEN (766F8BQ9T3)\n"
                    "TeamIdentifier=766F8BQ9T3\n"
                ),
            )
        if command[:2] == ["spctl", "--assess"]:
            return module.CommandResult(
                0,
                stderr=f"{official_app}: accepted\nsource=Notarized Developer ID\n",
            )
        return module.CommandResult(1, stderr=f"unexpected command: {command}")

    monkeypatch.setattr(module, "_run_command", fake_run)

    report = module.audit_release_evidence(_args(root, downloads))

    assert report["release"]["distribution"] == "official"
    assert report["release"]["archived_mas_evidence_included"] is False
    assert all(not check["id"].startswith("mas.") for check in report["checks"])
    assert "mas_root" not in report["release"]
    assert all(not key.startswith("mas_") for key in report["paths"])


def test_explicit_lifecycle_evidence_overrides_auto_discovery(
    tmp_path: Path,
) -> None:
    module = _load_module()
    root = _make_root(tmp_path)
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    official_app = downloads / "澜台.app"
    _make_app(official_app, "com.lantai.app")
    auto_lifecycle = (
        root
        / "release/builds/1.2.3-auto-arm64/release-evidence/official-packaged-lifecycle-smoke.json"
    )
    explicit_lifecycle = tmp_path / "explicit-official-lifecycle.json"
    auto_lifecycle.parent.mkdir(parents=True)
    _write_lifecycle_evidence(
        auto_lifecycle,
        distribution="official",
        app_path=official_app,
    )
    _write_lifecycle_evidence(
        explicit_lifecycle,
        distribution="mas",
        app_path=official_app,
    )

    check = module._check_lifecycle_evidence(
        "official.packaged_lifecycle_smoke.evidence",
        _args(
            root,
            downloads,
            official_lifecycle_evidence=explicit_lifecycle,
        ).official_lifecycle_evidence
        or module._find_latest_evidence(
            root,
            (
                "official-packaged-lifecycle-smoke.json",
                "official-local-packaged-lifecycle-smoke.json",
            ),
        ),
        expected_distribution="official",
        expected_app_paths=(official_app,),
    )

    assert check["status"] == "failed"
    assert check["details"]["path"] == str(explicit_lifecycle)
