"""Formal preparation keeps release gates and never deploys or uploads.

Exact shell functions run on synthetic files with finite OS/authority doubles.
The real byte and metadata admission runs; no build or signing service runs.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import re

import pytest


spec = importlib.util.spec_from_file_location(
    "preparation_shell_baseline",
    Path(__file__).with_name("test_release_chain_boundaries.py"),
)
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)

BUILD_STEPS = (
    "build_console", "write_embedded_release_build_identity",
    "validate_console_product_contract", "sync_console_static", "build_backend",
    "build_mac_use_helper", "prepare_console_assets", "build_electron",
    "sign_packaged_macho_leaves_for_distribution", "validate_packaged_mac_use_helper",
    "run_packaged_mac_use_helper_smoke", "adhoc_sign_local_release_bundle_and_artifacts",
    "run_packaged_skill_runtime_smoke",
)
PACKAGE_GATES = (
    "validate_packaged_product_contract", "validate_packaged_version_coherence",
    "validate_skip_build_manifest", "validate_release_bundle_signature",
    "run_packaged_app_lifecycle_smoke", "run_packaged_app_rapid_lifecycle_smoke",
    "run_packaged_customization_upgrade_smoke", "run_packaged_image_parser_mitigation",
    "run_packaged_upgrade_rollback_smoke", "notarize_release_bundle_and_artifacts",
    "write_supply_chain_manifest", "write_artifact_manifest",
    "run_persona_and_office_release_gates", "write_runtime_migration_dry_run_report",
    "write_runtime_migration_backup_restore_evidence", "run_release_evidence_readiness_gate",
    "run_agent_reliability_release_gate",
)
DEPLOYMENT_STEPS = (
    "begin_deployment", "deploy_release_artifacts", "deploy_app", "assess_gatekeeper",
    "launch_app", "finish_deployment", "clean_deployed_launchservices", "publish_github_release",
)


def option_script(args):
    source = baseline.SCRIPT.read_text(encoding="utf-8")
    start = source.index("\nwhile [[ $# -gt 0 ]]")
    end = source.index("\nacquire_release_build_lock\n", start)
    names = (
        "PREPARE_RELEASE", "ARTIFACT_ONLY", "PUBLISH_GITHUB_RELEASE", "SKIP_BUILD",
        "NO_LAUNCH", "RELEASE_CANDIDATE_ID", "ALLOW_UNVERIFIED_LOCAL_BUILD",
        "ALLOW_DIRTY_RELEASE_BUILD", "FORCE_ADHOC_LOCAL_BUILD", "DEFER_AGENT_RELIABILITY_SOAK",
    )
    initial = "\n".join(
        line for line in source[:start].splitlines()
        if re.match(r"^(?:" + "|".join(names) + r")=", line)
    )
    return (
        "set -- " + " ".join(baseline.quote(arg) for arg in args) + "\n"
        + baseline.source_function("usage") + initial + "\n" + source[start:end]
        + '\nprintf "PARSED:%s:%s:%s:%s\\n" "${PREPARE_RELEASE:-missing}"'
        + ' "$ARTIFACT_ONLY" "$PUBLISH_GITHUB_RELEASE" "$SKIP_BUILD"\n'
    )


@pytest.mark.parametrize("args,skip", [
    (["--prepare-release"], "0"),
    (["--prepare-release", "--skip-build", "--no-launch"], "1"),
    (["--skip-build", "--prepare-release"], "1"),
])
def test_preparation_option_is_recognized_before_build_lock(tmp_path, args, skip):
    result = baseline.run_shell(tmp_path, option_script(args))
    assert result.returncode == 0, result.stderr
    assert f"PARSED:1:0:0:{skip}" in result.stdout
    assert not (tmp_path / "build").exists()


@pytest.mark.parametrize("args,env", [
    (["--prepare-release", "--publish-github-release"], {}),
    (["--publish-github-release", "--prepare-release"], {}),
    (["--prepare-release", "--artifact-only"], {
        "LANTAI_ALLOW_UNVERIFIED_LOCAL_BUILD": "1", "LANTAI_RELEASE_CANDIDATE_ID": "isolated",
    }),
    (["--prepare-release"], {"LANTAI_RELEASE_CANDIDATE_ID": "isolated"}),
    *[(["--prepare-release"], {key: value}) for key, value in (
        ("LANTAI_ALLOW_UNVERIFIED_LOCAL_BUILD", "1"),
        ("LANTAI_ALLOW_UNVERIFIED_LOCAL_BUILD", "2"),
        ("LANTAI_ALLOW_DIRTY_RELEASE_BUILD", "1"),
        ("LANTAI_FORCE_ADHOC_LOCAL_BUILD", "1"),
        ("LANTAI_FORCE_ADHOC_LOCAL_BUILD", "true"),
        ("LANTAI_FORCE_ADHOC_LOCAL_BUILD", "yes"),
        ("LANTAI_FORCE_ADHOC_LOCAL_BUILD", "on"),
        ("LANTAI_DEFER_AGENT_RELIABILITY_SOAK", "1"),
        ("LANTAI_DEFER_AGENT_RELIABILITY_SOAK", "true"),
        ("LANTAI_SKIP_NOTARIZATION", "1"),
        ("LANTAI_SKIP_NOTARIZATION", "true"),
        ("LANTAI_ALLOW_UNSIGNED_RELEASE_TAG", "1"),
        ("LANTAI_ALLOW_SAME_VERSION_REBUILD", "1"),
    )],
])
def test_preparation_rejects_local_candidate_publish_and_bypasses(tmp_path, args, env):
    result = baseline.run_shell(tmp_path, option_script(args), env)
    assert result.returncode != 0
    assert "Error:" in result.stderr and "Unknown option:" not in result.stderr
    assert "PARSED:" not in result.stdout
    assert not (tmp_path / "build").exists()


def main_trace(tmp_path, *, prepare=0, artifact_only=0, skip=1, fault=""):
    names = BUILD_STEPS + PACKAGE_GATES + DEPLOYMENT_STEPS + ("prepare_formal_release",)
    doubles = "".join(
        f'{name}() {{ printf "%s\\n" {name} >> "$TRACE"; '
        f'[[ "$FAULT" != {name} ]] || return 71; }}\n'
        for name in names
    )
    return baseline.run_shell(
        tmp_path, baseline.source_function("main") + doubles + "main\n",
        {"ROOT_DIR": tmp_path, "PRODUCT_NAME": "Synthetic", "VERSION": "1.2.3",
         "RELEASE_DIR": tmp_path / "release", "PREPARED_RELEASE_DIR": tmp_path / "prepared",
         "DOWNLOAD_APP": tmp_path / "Downloads/Synthetic.app",
         "DOWNLOAD_DMG": tmp_path / "Downloads/Synthetic.dmg",
         "DOWNLOAD_ZIP": tmp_path / "Downloads/Synthetic.zip",
         "PREPARE_RELEASE": str(prepare), "ARTIFACT_ONLY": str(artifact_only),
         "SKIP_BUILD": str(skip), "FAULT": fault, "TRACE": tmp_path / "trace"},
    )


@pytest.mark.parametrize("skip", [0, 1])
def test_preparation_runs_all_package_gates_and_stops_before_deployment(tmp_path, skip):
    result = main_trace(tmp_path, prepare=1, skip=skip)
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "trace").read_text().splitlines()
    assert calls == list((BUILD_STEPS if not skip else ()) + PACKAGE_GATES + ("prepare_formal_release",))
    assert "Downloads deployment: skipped (--prepare-release)" in result.stdout
    assert "Deployed app:" not in result.stdout
    assert not (tmp_path / "Downloads").exists()


@pytest.mark.parametrize("fault", PACKAGE_GATES + ("prepare_formal_release",))
def test_failed_gate_cannot_be_reported_as_prepared_or_deployed(tmp_path, fault):
    result = main_trace(tmp_path, prepare=1, fault=fault)
    assert result.returncode != 0
    calls = (tmp_path / "trace").read_text().splitlines()
    assert calls[-1] == fault
    assert not set(calls).intersection(DEPLOYMENT_STEPS)
    assert "Done." not in result.stdout
    assert "Downloads deployment: skipped" not in result.stdout


@pytest.mark.parametrize("artifact_only", [0, 1])
def test_existing_deployment_and_local_candidate_main_paths_are_preserved(tmp_path, artifact_only):
    result = main_trace(tmp_path, artifact_only=artifact_only)
    assert result.returncode == 0, result.stderr
    expected = PACKAGE_GATES + (() if artifact_only else DEPLOYMENT_STEPS)
    assert (tmp_path / "trace").read_text().splitlines() == list(expected)


def test_preparation_uses_canonical_authority_without_candidate_exemption(tmp_path):
    recorder = tmp_path / "record-authority"
    recorder.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$RECORDED"\n')
    recorder.chmod(0o700)
    result = baseline.run_shell(
        tmp_path, baseline.source_function("validate_release_source_authority")
        + "validate_release_source_authority\n",
        {"ROOT_DIR": tmp_path, "ARTIFACT_ONLY": "0", "PREPARE_RELEASE": "1",
         "VENV_PYTHON": recorder, "RELEASE_SOURCE_AUTHORITY_SCRIPT": "canonical-check",
         "RELEASE_SOURCE_AUTHORITY_REPORT": tmp_path / "authority.json",
         "RECORDED": tmp_path / "argv"},
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "argv").read_text().splitlines() == [
        "canonical-check", "--root", str(tmp_path), "--output", str(tmp_path / "authority.json"),
    ]


@pytest.mark.parametrize("fault", [
    "none", "modified-dmg", "missing-notary", "notary-binding", "local-candidate",
    "noncanonical", "stale-sha", "authority-rejected", "authority-changed", "signature", "mutated-snapshot",
])
def test_preparation_admits_exact_private_snapshot_with_fresh_authority(tmp_path, fault):
    root = tmp_path / "release"
    env = baseline.release_fixture(root)
    manifest = json.loads(Path(env["ARTIFACT_MANIFEST"]).read_text())
    manifest["signing"]["identityClass"] = "Developer ID Application"
    for item in manifest["notarization"]["requestIds"]:
        role = item["label"].split("-")[-1]
        item.update(status="Accepted", requestIdSHA256=hashlib.sha256(("synthetic-" + role).encode()).hexdigest())
    if fault == "notary-binding":
        manifest["notarization"]["requestIds"][0]["requestIdSHA256"] = "f" * 64
    elif fault == "local-candidate":
        manifest["release"].update(artifactOnly=True, candidateId="isolated")
        manifest["buildIdentity"].update(artifactOnly=True, candidateId="isolated")
    elif fault == "noncanonical":
        manifest["source"].update(sourceBranch="codex/isolated", canonicalMain=False)
        manifest["buildIdentity"].update(sourceBranch="codex/isolated", canonicalMain=False)
    from scripts.release_build_identity import identity_digest
    manifest["buildIdentitySha256"] = identity_digest(manifest["buildIdentity"])
    baseline.write_json(Path(env["ARTIFACT_MANIFEST"]), manifest)
    if fault == "modified-dmg":
        next(root.glob("*.dmg")).write_bytes(b"modified after evidence")
    elif fault == "missing-notary":
        (Path(env["NOTARY_RECORD_DIR"]) / "notary-app.json").unlink()
    elif fault == "stale-sha":
        env["SOURCE_SHA"] = "b" * 40
    (root / "unchecked.blockmap").write_bytes(b"must not enter the prepared snapshot")
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    for name in ("Synthetic.app", "Synthetic.dmg", "Synthetic.zip"):
        (downloads / name).write_bytes(b"previous install")
    before = {p.name: p.read_bytes() for p in downloads.iterdir()}
    names = (
        "skip_notarization_requested", "assert_accepted_notary_record",
        "assert_public_release_prerequisites", "verify_public_notary_binding", "prepare_formal_release",
    )
    functions = "".join(baseline.source_function(name) for name in names)
    forbidden = "".join(
        f'{name}() {{ printf "%s\\n" {name} >> "$FORBIDDEN"; return 91; }}\n'
        for name in DEPLOYMENT_STEPS + ("resolve_github_cli", "audit_public_release_version_lineage", "gh")
    )
    script = functions + forbidden + '''
revalidate_deployment_source_authority() {
  printf 'authority\n' >> "$CHECKS"
  [[ "$FAULT" != authority-rejected ]] || return 71
  [[ "$FAULT" != authority-changed || $(wc -l < "$CHECKS") -lt 3 ]] || return 72
}
verify_public_upload_signatures() {
  printf 'signature\n' >> "$CHECKS"
  [[ "$FAULT" != signature ]] || return 73
  if [[ "$FAULT" == mutated-snapshot ]]; then
    printf 'changed after signature check' > "$1/Lantai-${VERSION}-${RELEASE_ARCH}.zip"
  fi
}
prepare_formal_release
printf 'PREPARED=%s\n' "$PREPARED_RELEASE_DIR"
'''
    env.update(
        PREPARE_RELEASE="1", PREPARED_RELEASE_DIR="", FAULT=fault,
        CHECKS=tmp_path / "authority-checks", FORBIDDEN=tmp_path / "forbidden",
        RELEASE_DIR=root, RELEASE_ARCH="arm64", PRODUCT_NAME="Synthetic",
        DOWNLOADS_DIR=downloads, RELEASE_UPLOAD_SCRIPT=baseline.W / "scripts/check_release_upload.py",
    )
    result = baseline.run_shell(tmp_path, script, env)
    assert {p.name: p.read_bytes() for p in downloads.iterdir()} == before
    assert not Path(env["FORBIDDEN"]).exists(), "preparation reached deployment or public repository operations"
    if fault != "none":
        assert result.returncode != 0, f"prepared {fault}"
        assert "PREPARED=" not in result.stdout
        return
    assert result.returncode == 0, result.stderr
    assert Path(env["CHECKS"]).read_text().splitlines() == ["authority", "signature", "authority"]
    prepared = Path(next(line.removeprefix("PREPARED=") for line in result.stdout.splitlines() if line.startswith("PREPARED=")))
    paths = list(prepared.iterdir())
    assert len(paths) == 5
    assert prepared.is_relative_to(root / "release-evidence")
    assert {p.name for p in paths} == {
        "Lantai-1.2.3-arm64.zip", "Lantai-1.2.3-arm64.dmg", "lantai-artifact-manifest.json",
        "lantai-supply-chain-manifest.json", "lantai-sbom.cdx.json",
    }
    assert all(p.read_bytes() == (root / p.name).read_bytes() for p in paths)
