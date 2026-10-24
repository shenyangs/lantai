# -*- coding: utf-8 -*-
"""Guard the required Runtime migration CI gate."""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/runtime-migration.yml"

REQUIRED_JOBS = {
    "runtime-contracts": [
        "uv run pytest",
        "tests/unit/runtime/test_contracts.py",
        "tests/unit/runtime/test_loop_contracts.py",
        "tests/unit/runtime/test_loop_evaluator_executor.py",
        "tests/unit/runtime/test_loop_export.py",
        "tests/unit/runtime/test_loop_evaluation.py",
        "tests/unit/runtime/test_loop_runtime_bridge.py",
        "tests/unit/runtime/test_loop_stop_conditions.py",
        "tests/unit/runtime/test_loop_trial.py",
        "tests/unit/runtime/test_run_contracts.py",
        "tests/unit/runtime/test_run_reducer.py",
        "tests/unit/runtime/test_run_store.py",
        "tests/unit/runtime/test_effect_crash_matrix_smoke.py",
        "tests/unit/runtime/test_native_runtime_effect_crash_smoke.py",
        "tests/unit/runtime/test_fault_injection_helpers.py",
        "tests/unit/app/test_browser_task_effect_crash_smoke.py",
        "tests/unit/app/test_chat_runtime_run_adapter.py",
        "tests/unit/app/test_runtime_continue.py",
        "tests/unit/app/test_workspace_grants.py",
        "tests/unit/app/test_workspace_context_resolution.py",
        "tests/unit/app/test_coding_router.py",
        "tests/unit/config/test_workspace_context.py",
        "tests/unit/app/test_runtime_export.py",
        "tests/unit/app/test_cron_experience_events.py",
        "tests/unit/app/test_cron_runtime_run_adapter.py",
        "tests/unit/app/test_reminders.py",
        "tests/unit/app/test_legacy_runtime_compat.py",
        "tests/unit/app/test_legacy_runtime_writers.py",
        "tests/unit/app/test_legacy_runtime_execution_observations.py",
        "tests/unit/app/test_runtime_events.py",
        "tests/unit/app/test_experience_events.py",
        "tests/unit/app/test_runtime_stall_watchdog.py",
        "tests/unit/scripts/test_frozen_backend_console_smoke.py",
        "tests/unit/scripts/test_packaged_app_lifecycle_smoke.py",
        "tests/unit/scripts/test_pyinstaller_spec_policy.py",
        "tests/unit/scripts/test_release_dependency_locks.py",
        "tests/unit/scripts/test_release_evidence_readiness.py",
        "tests/unit/scripts/test_reminder_delivery_smoke.py",
        "tests/unit/scripts/test_runtime_migration_backup_restore_smoke.py",
        "tests/unit/scripts/test_write_runtime_migration_dry_run_report.py",
        (
            "tests/unit/scripts/test_write_runtime_legacy_writer_obse"
            "rvation_summary.py"
        ),
        (
            "tests/unit/scripts/test_write_runtime_legacy_boundary_ob"
            "servation_summary.py"
        ),
        "tests/unit/scripts/test_legacy_intelligence_assembly_baseline.py",
        "tests/unit/scripts/test_legacy_intelligence_interface.py",
        "tests/unit/runtime/test_legacy_execution_architecture_guard.py",
    ],
    "runtime-ownership": [
        "uv run pytest",
        "tests/unit/app/test_runtime_router_ownership.py",
        "tests/unit/app/test_console_chat_smoke.py",
        "duplicate_submit",
        "effect",
        "generation_fence",
    ],
    "runtime-materializer": [
        "uv run pytest",
        "tests/unit/app/test_capabilities.py",
        "task_run_materializer",
    ],
    "runtime-transport-frontend": [
        "npm ci",
        "npm run test:run --",
        "chat.test.ts",
        "runtime.test.ts",
        "RuntimeEventStreamBridge.test.tsx",
        "runtimeEventStream.test.ts",
        "TurnEventPreview.test.tsx",
        "ChatPage.test.tsx",
        "src/api/modules/workspaceGrants.test.ts",
        "src/api/authHeaders.test.ts",
        "src/api/request.test.ts",
        "src/pages/Control/TaskCenter/index.test.tsx",
    ],
    "browser-policy-python": [
        "uv run pytest",
        "tests/unit/app/test_browser_workbench.py",
        "tests/unit/agents/tools/test_browser_control.py",
        "final_url",
    ],
    "browser-policy-electron": [
        "npm ci",
        "node --test electron/browserWorkbenchController.test.cjs",
        "electron/desktopIpcController.test.cjs",
        "electron/workspaceGrantBookmarkStore.test.cjs",
        "electron/packageAllowlist.test.cjs",
    ],
}

REQUIRED_PATH_FILTERS = [
    "tests/fixtures/runtime_event_v1.json",
    "scripts/run_effect_crash_matrix_smoke.py",
    "scripts/run_native_runtime_effect_crash_smoke.py",
    "scripts/run_browser_task_effect_crash_smoke.py",
    "scripts/run_runtime_watchdog_restart_smoke.py",
    "scripts/run_loop_trial_smoke.py",
    "scripts/frozen_backend_console_smoke.py",
    "scripts/packaged_app_lifecycle_smoke.py",
    "scripts/check_release_evidence_readiness.py",
    "scripts/run_reminder_delivery_smoke.py",
    "scripts/write_runtime_migration_dry_run_report.py",
    "scripts/write_runtime_legacy_writer_observation_summary.py",
    "scripts/write_runtime_legacy_boundary_observation_summary.py",
    "scripts/check_legacy_intelligence_assembly_baseline.py",
    (
        "docs/remediation/evidence/legacy-intelligence-assembly-r"
        "1-source-baseline-2026-08-30.json"
    ),
    (
        "docs/remediation/evidence/legacy-intelligence-assembly-r"
        "eviewed-transitions-2026-08-31.json"
    ),
    (
        "docs/remediation/evidence/legacy-execution-retirement-r4"
        "-gate-2026-08-31.json"
    ),
    (
        "docs/remediation/evidence/runtime-r6-source-retirement-g"
        "ate-2026-08-31.json"
    ),
    "scripts/check_legacy_intelligence_interface.py",
    "scripts/run_runtime_migration_backup_restore_smoke.py",
    "console/src/pages/Control/TaskCenter/**",
    "console/electron/backendLifecycleController.cjs",
    "console/electron/backendLifecycleController.test.cjs",
    "console/electron/browserWorkbenchSmoke.cjs",
    "console/electron/main.cjs",
    "console/electron/desktopIpcController.cjs",
    "console/electron/workspaceGrantBookmarkStore.cjs",
    "console/electron/packageAllowlist.test.cjs",
    "console/electron/entitlements/developer-id.inherit.plist",
    "electron/build-python.py",
    "src/lantai/app/runner/**",
    "src/lantai/app/workspace/**",
    "src/lantai/app/workspace_context.py",
    "src/lantai/app/workspace_grants.py",
    "src/lantai/app/routers/coding.py",
    "src/lantai/app/crons/**",
    "src/lantai/app/reminders/**",
    "src/lantai/app/runtime_legacy_writers.py",
    "src/lantai/app/runtime_legacy_execution_observations.py",
    "src/lantai/app/runtime_export.py",
    "tests/unit/app/test_cron_experience_events.py",
    "tests/unit/app/test_cron_runtime_run_adapter.py",
    "tests/unit/app/test_reminders.py",
    "tests/unit/app/test_legacy_runtime_writers.py",
    "tests/unit/app/test_legacy_runtime_execution_observations.py",
    "tests/unit/app/test_runtime_stall_watchdog.py",
    "tests/unit/app/test_runtime_export.py",
    "tests/unit/scripts/test_frozen_backend_console_smoke.py",
    "tests/unit/scripts/test_packaged_app_lifecycle_smoke.py",
    "tests/unit/scripts/test_pyinstaller_spec_policy.py",
    "tests/unit/scripts/test_release_dependency_locks.py",
    "tests/unit/scripts/test_release_evidence_readiness.py",
    "tests/unit/scripts/test_reminder_delivery_smoke.py",
    "tests/unit/scripts/test_runtime_migration_backup_restore_smoke.py",
    "tests/unit/scripts/test_write_runtime_migration_dry_run_report.py",
    (
        "tests/unit/scripts/test_write_runtime_legacy_writer_obse"
        "rvation_summary.py"
    ),
    (
        "tests/unit/scripts/test_write_runtime_legacy_boundary_ob"
        "servation_summary.py"
    ),
    "tests/unit/scripts/test_legacy_intelligence_assembly_baseline.py",
    "tests/unit/scripts/test_legacy_intelligence_interface.py",
]


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_runtime_migration_workflow_exists() -> None:
    assert WORKFLOW.exists()


def test_runtime_migration_workflow_is_valid_required_gate() -> None:
    text = _workflow_text()
    data = yaml.safe_load(text)

    assert data["name"] == "Runtime Migration Gate"
    assert "workflow_dispatch:" in text
    assert "continue-on-error" not in text
    assert "|| true" not in text
    assert "pytest tests/unit \\" not in text
    assert "--ignore=tests/unit/channels" not in text
    assert "mas-contract-sync" not in data["jobs"]
    assert "check_mas_runtime_migration_sync.py" not in text
    assert set(REQUIRED_JOBS).issubset(data["jobs"])


def test_runtime_migration_workflow_runs_focused_gate_commands() -> None:
    text = _workflow_text()

    assert "uv sync --frozen --extra dev" in text
    for job_name, required_fragments in REQUIRED_JOBS.items():
        assert job_name in text
        for fragment in required_fragments:
            assert fragment in text


def test_runtime_migration_workflow_watches_shared_fixtures() -> None:
    text = _workflow_text()

    for fragment in REQUIRED_PATH_FILTERS:
        assert fragment in text


def test_runtime_script_contracts_include_repository_import_root() -> None:
    data = yaml.safe_load(_workflow_text())
    assert data.get("env", {}).get("PYTHONPATH") == (
        "${{ github.workspace }}:${{ github.workspace }}/src"
    )


def test_project_metadata_reproduction_is_mandatory_for_both_events() -> None:
    data = yaml.load(_workflow_text(), Loader=yaml.BaseLoader)
    for event in ("push", "pull_request"):
        watched = data["on"][event]["paths"]
        assert "lantai-server.spec" in watched
        assert "scripts/pyinstaller_project_metadata.py" in watched
        assert (
            "tests/unit/scripts/test_pyinstaller_project_metadata.py"
            in watched
        )

    commands = "\n".join(
        step.get("run", "")
        for step in data["jobs"]["runtime-contracts"]["steps"]
    )
    assert (
        "tests/unit/scripts/test_pyinstaller_project_metadata.py" in commands
    )
