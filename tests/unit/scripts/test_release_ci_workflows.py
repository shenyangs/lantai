# -*- coding: utf-8 -*-
"""Release CI workflow guard tests."""

from __future__ import annotations

import shlex
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/desktop-release.yml"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_desktop_release_workflow_is_valid_yaml():
    data = yaml.safe_load(_workflow_text())

    assert data["name"] == "Desktop Release"
    assert "build-macos-electron" in data["jobs"]
    assert "publish-public-release" in data["jobs"]


def test_desktop_release_ci_uses_locked_uv_backend_environments():
    text = _workflow_text()

    assert "astral-sh/setup-uv@v5" in text
    assert "uv sync --frozen --extra dev" in text
    assert "uv sync --frozen --extra full --extra dev" in text
    assert "UV_PYTHON_DOWNLOADS: never" in text
    assert 'pip install -e ".[dev]"' not in text
    assert 'pip install -e ".[full]"' not in text
    assert "pip install --upgrade" not in text
    assert "pip install --upgrade pip wheel pyinstaller" not in text


def test_desktop_release_ci_uploads_nested_release_manifests_and_sbom():
    text = _workflow_text()

    expected_patterns = [
        "release/builds/**/*.dmg",
        "release/builds/**/*.zip",
        "release/builds/**/lantai-artifact-manifest.json",
        "release/builds/**/lantai-supply-chain-manifest.json",
        "release/builds/**/lantai-sbom.cdx.json",
    ]
    for pattern in expected_patterns:
        assert pattern in text

    assert "release/*.dmg" not in text
    assert "release/*.zip" not in text
    assert "latest-mac.yml" not in text
    assert "updater artifacts" not in text


def test_desktop_release_ci_revalidates_artifact_manifest_before_upload():
    text = _workflow_text()

    assert "Verify release provenance manifests" in text
    assert (
        "scripts/check_release_artifact_provenance.py validate-skip-manifest"
        in text
    )
    assert "release/builds" in text
    assert "lantai-artifact-manifest.json" in text
    assert "lantai-supply-chain-manifest.json" in text
    assert "lantai-sbom.cdx.json" in text
    assert "SOURCE_SHA" in text
    assert "VERSION" in text
    assert "ARCH" in text


def test_public_release_publishes_release_provenance_files():
    text = _workflow_text()

    assert "find . -mindepth 2 -type f" in text
    assert "-maxdepth 3" not in text
    assert "-name 'lantai-artifact-manifest.json'" in text
    assert "-name 'lantai-supply-chain-manifest.json'" in text
    assert "-name 'lantai-sbom.cdx.json'" in text
    assert "lantai-artifact-manifest.json" in text
    assert "lantai-supply-chain-manifest.json" in text
    assert "lantai-sbom.cdx.json" in text


def test_windows_checkouts_enable_long_paths_before_fetching_evidence():
    for name in ("tests.yml", "desktop-release.yml"):
        workflow = yaml.safe_load((WORKFLOW.parent / name).read_text())
        for job in workflow["jobs"].values():
            matrix = job.get("strategy", {}).get("matrix", {})
            platforms = set(matrix.get("os", []))
            platforms.update(
                entry.get("os") for entry in matrix.get("include", [])
            )
            if (
                "windows-latest" not in platforms
                and job["runs-on"] != "windows-latest"
            ):
                continue
            steps = job["steps"]
            checkout = next(
                i
                for i, step in enumerate(steps)
                if step.get("uses", "").startswith("actions/checkout@")
            )
            settings = [
                step
                for step in steps[:checkout]
                if "git config --global core.longpaths true"
                in step.get("run", "")
            ]
            assert settings, "Windows must enable long paths before checkout"
            assert all(
                step.get("if") == "runner.os == 'Windows'" for step in settings
            )


def test_python_gates_resolve_the_installed_locked_pptx_compiler():
    expected = "${{ github.workspace }}/console/node_modules/pptxgenjs"
    for name in (
        "foundation-experience-gate.yml",
        "vibe-coding-preflight.yml",
        "desktop-release.yml",
    ):
        workflow = yaml.safe_load((WORKFLOW.parent / name).read_text())
        assert (
            workflow.get("env", {}).get("LANTAI_PPTXGENJS_MODULE") == expected
        )
        steps = next(iter(workflow["jobs"].values()))["steps"]
        install = next(
            i
            for i, step in enumerate(steps)
            if step.get("working-directory") == "console"
            and "npm ci" in step.get("run", "")
        )
        gate = next(
            i
            for i, step in enumerate(steps)
            if "scripts/check_foundation_experience_gate.py"
            in step.get("run", "")
            or "scripts/vibe_coding_preflight.py" in step.get("run", "")
        )
        assert install < gate


def test_invocation_gate_executes_existing_acp_lifecycle_locks():
    workflow = yaml.safe_load((WORKFLOW.parent / "unit-tests.yml").read_text())
    steps = workflow["jobs"]["unit-tests"]["steps"]
    gate = next(
        step
        for step in steps
        if step.get("name", "").startswith("Invocation lifecycle regressions")
    )
    selected = {
        word
        for word in shlex.split(gate["run"].replace("\\\n", " "))
        if word.startswith("tests/")
    }
    assert selected and all((ROOT / path).is_file() for path in selected)
    assert {
        "tests/unit/integrations/test_acp_sdk_lifecycle.py",
        "tests/unit/integrations/test_acp_submission_lifecycle.py",
    } <= selected


def test_desktop_release_preflight_uses_the_locked_backend_python():
    workflow = yaml.safe_load(_workflow_text())
    steps = workflow["jobs"]["vibe-coding-preflight"]["steps"]
    gate = next(
        step
        for step in steps
        if "scripts/vibe_coding_preflight.py" in step.get("run", "")
    )
    assert shlex.split(gate["run"]) == [
        "uv",
        "run",
        "--frozen",
        "--extra",
        "dev",
        "python",
        "scripts/vibe_coding_preflight.py",
        "--full",
    ]
