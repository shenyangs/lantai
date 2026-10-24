# -*- coding: utf-8 -*-
"""Keep daily cloud Mac candidates isolated, source-bound and honest."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/cloud-macos.yml"
SIGNING_KEYS = (
    "CSC_LINK",
    "CSC_KEY_PASSWORD",
    "APPLE_ID",
    "APPLE_APP_SPECIFIC_PASSWORD",
    "APPLE_TEAM_ID",
)


@pytest.fixture(name="workflow")
def cloud_macos_workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _step(workflow, name):
    return next(
        step
        for step in workflow["jobs"]["candidate"]["steps"]
        if step.get("name") == name
    )


def test_daily_candidates_use_existing_sync_channel_without_publication(
    workflow,
):
    events = workflow.get("on", workflow.get(True))
    assert events["push"]["branches"] == ["codex/cloud-sync"]
    assert "pull_request_target" not in events
    assert (
        events["workflow_dispatch"]["inputs"]["signing"]["default"]
        == "runner-only"
    )
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is True
    job = workflow["jobs"]["candidate"]
    assert job["runs-on"] == "macos-14"
    assert 60 <= job["timeout-minutes"] <= 180
    for step in job["steps"]:
        assert not step.get("continue-on-error", False)
        assert "git push" not in step.get("run", "")
        assert "gh release" not in step.get("run", "")


def test_candidate_builds_keep_full_package_smokes_and_source_boundary(
    workflow,
):
    job = workflow["jobs"]["candidate"]
    env = job["env"]
    assert env["LANTAI_RUN_PACKAGED_APP_LIFECYCLE_SMOKE"] == "1"
    assert env["LANTAI_PACKAGED_APP_RAPID_LIFECYCLE_CYCLES"] == "100"
    assert env["LANTAI_REQUIRE_BUNDLED_OFFICECLI"] == "1"
    checkout = next(
        step
        for step in job["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    )
    assert checkout["with"]["ref"] == "${{ github.sha }}"
    identity = _step(workflow, "Bind source and runner identity")["run"]
    assert 'test "$(git rev-parse HEAD)" = "$GITHUB_SHA"' in identity
    assert "--require-clean --require-no-active-lease" in identity
    builds = [
        step
        for step in job["steps"]
        if "./build_and_deploy.sh" in step.get("run", "")
    ]
    assert len(builds) == 2
    assert all(
        step["run"].strip()
        == "./build_and_deploy.sh --artifact-only --no-launch"
        for step in builds
    )
    runner = _step(workflow, "Build and test on runner only")
    assert runner["env"]["LANTAI_FORCE_ADHOC_LOCAL_BUILD"] == "1"
    assert runner["env"]["LANTAI_SKIP_NOTARIZATION"] == "1"
    assert "secrets." not in str(runner)
    signed = _step(workflow, "Build notarized acceptance candidate")
    assert "LANTAI_SKIP_NOTARIZATION" not in signed["env"]
    assert all(key in signed["env"] for key in SIGNING_KEYS)
    assert "secrets." not in str(job["env"])


@pytest.mark.parametrize("missing", SIGNING_KEYS)
def test_signed_mode_rejects_incomplete_credentials_without_disclosing_values(
    workflow,
    missing,
):
    script = _step(workflow, "Validate signing setup")["run"]
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in SIGNING_KEYS
    }
    env.update({key: "test-private-value" for key in SIGNING_KEYS})
    env.update(BUILD_REF="main", CANDIDATE_SIGNING="developer-id")
    env[missing] = ""
    result = subprocess.run(
        ["bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert missing in result.stderr
    assert "test-private-value" not in result.stdout + result.stderr


def test_signing_rejects_feature_refs_before_build(workflow):
    env = {
        **os.environ,
        "BUILD_REF": "codex/unreviewed",
        "CANDIDATE_SIGNING": "developer-id",
    }
    result = subprocess.run(
        ["bash", "-c", _step(workflow, "Validate signing setup")["run"]],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "main" in result.stderr


def test_candidate_validation_precedes_diagnostic_artifact_upload(workflow):
    steps = workflow["jobs"]["candidate"]["steps"]
    verify = _step(workflow, "Verify candidate provenance")["run"]
    assert (
        "check_release_artifact_provenance.py validate-skip-manifest" in verify
    )
    assert '--source-sha "$GITHUB_SHA"' in verify
    assert '--version "$VERSION" --arch arm64' in verify
    assert "lantai-supply-chain-manifest.json" in verify
    assert "lantai-sbom.cdx.json" in verify
    upload = next(
        step
        for step in steps
        if step.get("uses", "").startswith("actions/upload-artifact@")
    )
    assert steps.index(
        _step(workflow, "Verify candidate provenance"),
    ) < steps.index(upload)
    assert upload["if"] == "always()"
    assert "release/builds/**/*.dmg" in upload["with"]["path"]
    assert "release/builds/**/release-evidence/**" in upload["with"]["path"]
    assert "*.secret" not in upload["with"]["path"]


def test_all_workflow_shell_blocks_parse(workflow):
    for step in workflow["jobs"]["candidate"]["steps"]:
        if "run" in step:
            result = subprocess.run(
                ["bash", "-n"],
                input=step["run"],
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode == 0, (step["name"], result.stderr)
