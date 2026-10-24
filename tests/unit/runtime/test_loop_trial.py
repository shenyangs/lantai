# -*- coding: utf-8 -*-
"""Behavior locks for a recoverable LOOP-05 long-running trial."""

from __future__ import annotations

import json
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from lantai.runtime import RuntimeRunStore
from lantai.runtime.loop_trial import (
    inspect_loop_interruption_matrix_trial,
    run_browser_content_loop_trial,
    run_loop_interruption_matrix_trial,
    run_loop_trial,
    run_release_verification_loop_trial,
    validate_browser_content_draft_payload,
    validate_release_verification_manifests,
)


ROOT = Path(__file__).resolve().parents[3]
TRIAL_SMOKE = ROOT / "scripts" / "run_loop_trial_smoke.py"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_file(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _artifact_entry(path: Path) -> dict[str, object]:
    return {
        "name": path.name,
        "path": str(path),
        "sha256": _sha256(path),
        "size": path.stat().st_size,
    }


def _write_release_fixture(root: Path, distribution: str) -> Path:
    release_dir = root / distribution / "release-build"
    app_dir = release_dir / ("mac-arm64" if distribution == "official" else "mas-arm64") / "澜台.app"
    _write_file(app_dir / "Contents" / "Info.plist", b"plist")
    source_sha = "a" * 40 if distribution == "official" else "b" * 40
    sbom = _write_file(release_dir / "lantai-sbom.cdx.json", b'{"bomFormat":"CycloneDX"}')
    artifacts: dict[str, object] = {
        "app": {
            "name": "澜台.app",
            "path": str(app_dir),
        }
    }
    if distribution == "official":
        dmg = _write_file(release_dir / "Lantai-1.2.3-arm64.dmg", b"dmg")
        zip_path = _write_file(release_dir / "Lantai-1.2.3-arm64.zip", b"zip")
        latest = _write_file(release_dir / "latest-mac.yml", b"version: 1.2.3\n")
        artifacts.update(
            {
                "dmg": _artifact_entry(dmg),
                "zip": _artifact_entry(zip_path),
                "latestMacYml": _artifact_entry(latest),
            }
        )

    manifest = {
        "schema": "com.lantai.release-artifact-manifest.v1",
        "release": {
            "version": "1.2.3",
            "arch": "arm64",
            "productName": "澜台",
            "directory": str(release_dir),
        },
        "source": {
            "sha": source_sha,
            "dirty": True,
            "tag": None,
        },
        "signing": {
            "mode": "developer-id" if distribution == "official" else "mas",
            "identityClass": "developer-id" if distribution == "official" else "mas",
        },
        "notarization": {"requestIds": []},
        "artifacts": artifacts,
        "supplyChainManifest": {
            "schema": "com.lantai.release-supply-chain-manifest.v1",
            "release": {"version": "1.2.3", "arch": "arm64"},
            "source": {"sha": source_sha},
            "binarySources": [
                {
                    "name": "electron-runtime",
                    "version": "42.0.1",
                    "path": str(app_dir / "Contents" / "Frameworks" / "Electron Framework.framework"),
                    "sha256": "c" * 64,
                    "bytes": 1,
                }
            ],
            "sbom": {
                "format": "CycloneDX",
                "specVersion": "1.5",
                "path": str(sbom),
                "sha256": _sha256(sbom),
                "bytes": sbom.stat().st_size,
            },
        },
    }
    manifest_path = release_dir / "lantai-artifact-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return manifest_path


def _browser_content_payload() -> dict[str, object]:
    return {
        "draft": {
            "id": "draft-1",
            "platform": "local_markdown",
            "title": "Runtime migration field notes",
            "body": "Draft body prepared from browser research evidence.",
            "summary": "A local draft prepared for review.",
            "status": "ready_for_review",
            "preview_url": "preview://draft-1",
        },
        "preview": {
            "url": "preview://draft-1",
            "status": "ready",
        },
        "queue_item": {
            "id": "queue-1",
            "draft_id": "draft-1",
            "platform": "wechat_official_account",
            "status": "needs_confirmation",
            "requires_confirmation": True,
        },
        "publish_action": {
            "action": "publish",
            "page_kind": "publish",
            "requires_human_gate": True,
            "requires_manual_takeover": True,
            "executed": False,
        },
    }


def _write_browser_content_payload(path: Path) -> Path:
    payload = _browser_content_payload()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


@pytest.mark.asyncio
async def test_loop_trial_recovers_progress_sidecar_and_settles(tmp_path: Path) -> None:
    first = await run_loop_trial(
        tmp_path,
        run_id="loop-trial-1",
        max_iterations=1,
    )
    store_after_first = RuntimeRunStore(tmp_path)
    run_after_first = await store_after_first.load_contract("loop-trial-1")
    progress_after_first = await store_after_first.load_loop_progress_window(
        "loop-trial-1",
        expected_attempt_id="loop-trial-1:attempt-1",
    )

    assert first.status == "running"
    assert run_after_first is not None
    assert run_after_first.status == "running"
    assert progress_after_first is not None
    assert progress_after_first.consecutive_no_progress_iterations == 1

    second = await run_loop_trial(
        tmp_path,
        run_id="loop-trial-1",
        max_iterations=5,
    )
    store_after_second = RuntimeRunStore(tmp_path)
    run_after_second = await store_after_second.load_contract("loop-trial-1")
    progress_after_second = await store_after_second.load_loop_progress_window(
        "loop-trial-1",
        expected_attempt_id="loop-trial-1:attempt-1",
    )
    latest_report = await store_after_second.load_latest_loop_evaluation_report(
        "loop-trial-1",
        expected_attempt_id="loop-trial-1:attempt-1",
    )

    assert second.status == "stalled"
    assert run_after_second is not None
    assert run_after_second.status == "stalled"
    assert run_after_second.error is not None
    assert run_after_second.error.code == "loop_no_progress_exhausted"
    assert progress_after_second is not None
    assert progress_after_second.consecutive_no_progress_iterations == 2
    assert latest_report is not None
    assert latest_report.report.report_id == "loop-trial-1-eval-2"


def test_loop_trial_smoke_recovers_across_processes(tmp_path: Path) -> None:
    workspace = tmp_path / "loop-trial-smoke"
    completed = subprocess.run(
        [
            sys.executable,
            str(TRIAL_SMOKE),
            "--workspace",
            str(workspace),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["ok"] is True
    assert payload["first_process"] != payload["second_process"]
    assert payload["second_status"] == "stalled"
    assert payload["error_code"] == "loop_no_progress_exhausted"
    assert payload["progress"]["consecutive_no_progress_iterations"] == 2


def test_release_verification_trial_smoke_runs_across_processes(tmp_path: Path) -> None:
    official = _write_release_fixture(tmp_path, "official")
    mas = _write_release_fixture(tmp_path, "mas")
    workspace = tmp_path / "release-verification-smoke"
    completed = subprocess.run(
        [
            sys.executable,
            str(TRIAL_SMOKE),
            "--workspace",
            str(workspace),
            "--scenario",
            "release-verification",
            "--release-official-artifact-manifest",
            str(official),
            "--release-mas-artifact-manifest",
            str(mas),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["ok"] is True
    assert payload["first_process"] != payload["inspect_process"]
    assert payload["final_status"] == "succeeded"
    assert payload["latest_report_conclusion"] == "pass"
    assert payload["issues"] == []


def test_browser_content_trial_smoke_runs_human_gate_across_processes(
    tmp_path: Path,
) -> None:
    payload_path = _write_browser_content_payload(tmp_path / "browser-content.json")
    workspace = tmp_path / "browser-content-smoke"
    completed = subprocess.run(
        [
            sys.executable,
            str(TRIAL_SMOKE),
            "--workspace",
            str(workspace),
            "--scenario",
            "browser-content",
            "--browser-content-payload",
            str(payload_path),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["ok"] is True
    assert payload["pending_process"] != payload["approved_process"]
    assert payload["pending_status"] == "waiting_approval"
    assert payload["final_status"] == "succeeded"
    assert payload["latest_report_conclusion"] == "pass"
    assert payload["human_gate_decision"] == "approved"
    assert payload["issues"] == []


def test_loop_interruption_matrix_trial_smoke_runs_across_processes(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "interruption-matrix-smoke"
    completed = subprocess.run(
        [
            sys.executable,
            str(TRIAL_SMOKE),
            "--workspace",
            str(workspace),
            "--scenario",
            "interruption-matrix",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["ok"] is True
    assert payload["first_process"] != payload["inspect_process"]
    assert payload["issues"] == []
    assert payload["scenario_statuses"]["transport_disconnect"] == "running"
    assert payload["scenario_statuses"]["process_kill_recovery"] == "stalled"
    assert payload["scenario_statuses"]["pause_resume"] == "running"
    assert payload["scenario_statuses"]["failure_retry"] == "running"
    assert payload["scenario_statuses"]["budget_exhaustion"] == "stalled"
    assert payload["scenario_statuses"]["user_stop"] == "cancelled"
    assert payload["scenario_statuses"]["browser_human_gate"] == "succeeded"
    assert payload["error_codes"]["process_kill_recovery"] == (
        "loop_no_progress_exhausted"
    )
    assert payload["error_codes"]["budget_exhaustion"] == "loop_step_budget_exhausted"
    assert payload["stop_reason_codes"]["user_stop"] == "loop_human_stop_requested"


@pytest.mark.asyncio
async def test_loop_interruption_matrix_trial_covers_source_level_outcomes(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    result = await run_loop_interruption_matrix_trial(
        workspace,
        run_id_prefix="matrix",
    )
    inspected = await inspect_loop_interruption_matrix_trial(
        workspace,
        run_id_prefix="matrix",
    )

    assert result.ok is True
    assert result.issues == ()
    assert inspected.ok is True
    assert inspected.scenario_statuses == result.scenario_statuses
    assert result.scenario_statuses == {
        "transport_disconnect": "running",
        "process_kill_recovery": "stalled",
        "pause_resume": "running",
        "failure_retry": "running",
        "budget_exhaustion": "stalled",
        "user_stop": "cancelled",
        "browser_human_gate": "succeeded",
    }
    assert result.error_codes["transport_disconnect"] is None
    assert result.error_codes["process_kill_recovery"] == "loop_no_progress_exhausted"
    assert result.error_codes["budget_exhaustion"] == "loop_step_budget_exhausted"
    assert result.stop_reason_codes["user_stop"] == "loop_human_stop_requested"
    assert result.attempt_ids["failure_retry"] == (
        "matrix-failure-retry:attempt-2"
    )
    assert (result.details or {})["failure_retry"]["previous_attempt_id"] == (
        "matrix-failure-retry:attempt-1"
    )
    assert (result.details or {})["browser_human_gate"]["pending_status"] == (
        "waiting_approval"
    )
    assert (result.details or {})["transport_disconnect"]["suspected_stalled"] is True


@pytest.mark.asyncio
async def test_browser_content_trial_waits_for_human_gate_then_succeeds(
    tmp_path: Path,
) -> None:
    payload = _browser_content_payload()
    workspace = tmp_path / "workspace"

    pending = await run_browser_content_loop_trial(
        workspace,
        payload=payload,
        run_id="browser-content-1",
        human_gate_decision="pending",
    )
    store = RuntimeRunStore(workspace)
    pending_run = await store.load_contract("browser-content-1")
    pending_report = await store.load_latest_loop_evaluation_report(
        "browser-content-1",
        expected_attempt_id="browser-content-1:attempt-1",
    )

    assert pending.status == "waiting_approval"
    assert pending.error_code is None
    assert pending.issues == ()
    assert pending.progress.consecutive_no_progress_iterations == 0
    assert pending_run is not None
    assert pending_run.status == "waiting_approval"
    assert pending_run.current_step is not None
    assert pending_run.current_step.kind == "approval"
    assert pending_report is not None
    assert pending_report.report.conclusion == "waiting_human"
    assert pending_report.report.human_gate_decision == "pending"

    approved = await run_browser_content_loop_trial(
        workspace,
        payload=payload,
        run_id="browser-content-1",
        human_gate_decision="approved",
    )
    approved_run = await store.load_contract("browser-content-1")
    approved_report = await store.load_latest_loop_evaluation_report(
        "browser-content-1",
        expected_attempt_id="browser-content-1:attempt-1",
    )

    assert approved.status == "succeeded"
    assert approved.error_code is None
    assert approved_run is not None
    assert approved_run.status == "succeeded"
    assert approved_report is not None
    assert approved_report.report.conclusion == "pass"
    assert approved_report.report.human_gate_decision == "approved"


@pytest.mark.asyncio
async def test_browser_content_trial_blocks_publish_receipt(tmp_path: Path) -> None:
    payload = _browser_content_payload()
    payload["publish"] = {"receipt": "already-published"}

    result = await run_browser_content_loop_trial(
        tmp_path / "workspace",
        payload=payload,
        run_id="browser-content-publish-receipt",
        human_gate_decision="pending",
    )
    store = RuntimeRunStore(tmp_path / "workspace")
    run = await store.load_contract("browser-content-publish-receipt")
    latest_report = await store.load_latest_loop_evaluation_report(
        "browser-content-publish-receipt",
        expected_attempt_id="browser-content-publish-receipt:attempt-1",
    )

    assert result.status == "stalled"
    assert result.error_code == "loop_deterministic_gate_blocked"
    assert any(issue.startswith("no-publish:") for issue in result.issues)
    assert run is not None
    assert run.status == "stalled"
    assert latest_report is not None
    assert latest_report.report.conclusion == "blocked"


@pytest.mark.asyncio
async def test_browser_content_trial_rejected_gate_cancels_run(tmp_path: Path) -> None:
    payload = _browser_content_payload()

    result = await run_browser_content_loop_trial(
        tmp_path / "workspace",
        payload=payload,
        run_id="browser-content-rejected",
        human_gate_decision="rejected",
    )
    store = RuntimeRunStore(tmp_path / "workspace")
    run = await store.load_contract("browser-content-rejected")
    latest_report = await store.load_latest_loop_evaluation_report(
        "browser-content-rejected",
        expected_attempt_id="browser-content-rejected:attempt-1",
    )

    assert result.status == "cancelled"
    assert run is not None
    assert run.status == "cancelled"
    assert run.stop_reason is not None
    assert run.stop_reason.code == "browser_publish_gate_rejected"
    assert latest_report is not None
    assert latest_report.report.conclusion == "blocked"
    assert latest_report.report.human_gate_decision == "rejected"


def test_browser_content_payload_requires_preview_safe_state() -> None:
    payload = _browser_content_payload()
    payload["draft"]["status"] = "saved_to_platform"  # type: ignore[index]
    payload["queue_item"]["requires_confirmation"] = False  # type: ignore[index]
    evidence = validate_browser_content_draft_payload(payload)

    assert any("draft status is not preview-safe" in issue for issue in evidence.issues)
    assert "browser-content:queue does not require confirmation" in evidence.issues


@pytest.mark.asyncio
async def test_release_verification_loop_trial_succeeds_without_publish(tmp_path: Path) -> None:
    official = _write_release_fixture(tmp_path, "official")
    mas = _write_release_fixture(tmp_path, "mas")

    result = await run_release_verification_loop_trial(
        tmp_path / "workspace",
        manifests={"official": official, "mas": mas},
        run_id="release-verification-1",
    )
    store = RuntimeRunStore(tmp_path / "workspace")
    run = await store.load_contract("release-verification-1")
    latest_report = await store.load_latest_loop_evaluation_report(
        "release-verification-1",
        expected_attempt_id="release-verification-1:attempt-1",
    )

    assert result.status == "succeeded"
    assert result.error_code is None
    assert result.issues == ()
    assert result.iterations_executed == 1
    assert result.progress.consecutive_no_progress_iterations == 0
    assert run is not None
    assert run.status == "succeeded"
    assert latest_report is not None
    assert latest_report.report.conclusion == "pass"
    assert latest_report.report.weighted_score == 5


@pytest.mark.asyncio
async def test_release_verification_loop_trial_blocks_publish_receipt(tmp_path: Path) -> None:
    official = _write_release_fixture(tmp_path, "official")
    mas = _write_release_fixture(tmp_path, "mas")
    payload = json.loads(official.read_text(encoding="utf-8"))
    payload["publish"] = {"githubRelease": "created"}
    official.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    result = await run_release_verification_loop_trial(
        tmp_path / "workspace",
        manifests={"official": official, "mas": mas},
        run_id="release-verification-publish-receipt",
    )
    store = RuntimeRunStore(tmp_path / "workspace")
    run = await store.load_contract("release-verification-publish-receipt")
    latest_report = await store.load_latest_loop_evaluation_report(
        "release-verification-publish-receipt",
        expected_attempt_id="release-verification-publish-receipt:attempt-1",
    )

    assert result.status == "stalled"
    assert result.error_code == "loop_deterministic_gate_blocked"
    assert any(issue.startswith("no-publish:") for issue in result.issues)
    assert result.progress.consecutive_failures == 1
    assert run is not None
    assert run.status == "stalled"
    assert latest_report is not None
    assert latest_report.report.conclusion == "blocked"


@pytest.mark.asyncio
async def test_release_verification_loop_trial_rejects_changed_evidence_for_same_run_id(
    tmp_path: Path,
) -> None:
    official = _write_release_fixture(tmp_path, "official")
    mas = _write_release_fixture(tmp_path, "mas")
    workspace = tmp_path / "workspace"

    await run_release_verification_loop_trial(
        workspace,
        manifests={"official": official, "mas": mas},
        run_id="release-verification-evidence-fence",
    )
    payload = json.loads(official.read_text(encoding="utf-8"))
    payload["artifacts"]["zip"]["sha256"] = "0" * 64
    official.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="different contract hash"):
        await run_release_verification_loop_trial(
            workspace,
            manifests={"official": official, "mas": mas},
            run_id="release-verification-evidence-fence",
        )


def test_release_verification_requires_both_distribution_manifests(tmp_path: Path) -> None:
    official = _write_release_fixture(tmp_path, "official")
    evidence = validate_release_verification_manifests({"official": official})

    assert {item.distribution for item in evidence} == {"official", "mas"}
    assert any(
        issue == "manifest:mas missing required distribution manifest"
        for item in evidence
        for issue in item.issues
    )
