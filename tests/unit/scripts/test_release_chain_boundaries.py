# -*- coding: utf-8 -*-
"""Finite read-only release-chain locks; no build, real OS action or upload.

Only exact functions/steps read from the source checkout execute. They execute
against synthetic files under this evidence directory, with external systems
replaced by finite doubles. The full release script is never sourced.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

W = Path(__file__).resolve().parents[3]
P = Path(__file__).resolve().parent
SCRIPT = W / "build_and_deploy.sh"
WORKFLOW = W / ".github/workflows/desktop-release.yml"
EXPECTED_SHA = "a" * 40
VERSION = "1.2.3"


@pytest.fixture(name="fixture_dir")
def fixture_dir_factory(tmp_path):
    return tmp_path


def source_function(name: str) -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index(f"{name}() {{\n")
    end = text.index("\n}\n", start) + 3
    return text[start:end] + "\n"


def workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def step(job: str, name: str):
    return next(
        x for x in workflow()["jobs"][job]["steps"] if x.get("name") == name
    )


def run_shell(root: Path, script: str, env=None):
    # Do not inherit credentials, live profile, shell startup or user config.
    clean_env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(root),
        "TMPDIR": str(root),
        "PYTHONDONTWRITEBYTECODE": "1",
        "LC_ALL": "C",
    }
    clean_env.update({str(k): str(v) for k, v in (env or {}).items()})
    return subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            "set -euo pipefail\n" + script,
        ],
        cwd=root,
        env=clean_env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def quote(value):
    return shlex.quote(str(value))


def test_source_validation_profile_does_not_leak_into_packaged_checks(tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json, os, sys\n"
        "keys = ['LANTAI_WORKING_DIR', 'LANTAI_SECRET_DIR', 'LANTAI_PPTXGENJS_MODULE']\n"
        "print(json.dumps({'env': {k: os.environ.get(k) for k in keys}, 'args': sys.argv[1:]}))\n",
        encoding="utf-8",
    )
    script_dir = tmp_path / "scripts"
    script_dir.mkdir()
    (script_dir / "check_foundation_experience_gate.py").write_text(probe.read_text())
    result = run_shell(
        tmp_path,
        source_function("validate_foundation_experience_gate")
        + f"ROOT_DIR={quote(tmp_path)}\nVENV_PYTHON={quote(sys.executable)}\n"
        + "validate_foundation_experience_gate\n"
        + f"{quote(sys.executable)} {quote(probe)} packaged-check\n",
        env={
            "LANTAI_SOURCE_VALIDATION_WORKING_DIR": tmp_path / "source-working",
            "LANTAI_SOURCE_VALIDATION_SECRET_DIR": tmp_path / "source-secret",
            "LANTAI_SOURCE_VALIDATION_PPTXGENJS_MODULE": tmp_path / "source-module",
        },
    )
    assert result.returncode == 0, result.stderr
    records = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    assert records[0]["env"] == {
        "LANTAI_WORKING_DIR": str(tmp_path / "source-working"),
        "LANTAI_SECRET_DIR": str(tmp_path / "source-secret"),
        "LANTAI_PPTXGENJS_MODULE": str(tmp_path / "source-module"),
    }
    assert records[0]["args"][-1] == "--run-verifications"
    assert records[1]["env"] == dict.fromkeys(records[0]["env"])


@pytest.mark.parametrize("key", ["LANTAI_SOURCE_VALIDATION_WORKING_DIR", "LANTAI_SOURCE_VALIDATION_SECRET_DIR"])
def test_source_validation_rejects_incomplete_profile_pair(tmp_path, key):
    result = run_shell(
        tmp_path,
        source_function("validate_foundation_experience_gate")
        + "VENV_PYTHON=/usr/bin/false\nROOT_DIR=/unused\nvalidate_foundation_experience_gate\n",
        env={key: tmp_path / "isolated"},
    )
    assert result.returncode != 0
    assert "must be set together" in result.stderr


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def file_record(path: Path):
    data = path.read_bytes()
    return {
        "name": path.name,
        "path": str(path),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }


def release_fixture(root: Path, *, sha=EXPECTED_SHA, version=VERSION):
    root.mkdir(parents=True, exist_ok=True)
    dmg = root / f"Lantai-{version}-arm64.dmg"
    zip_path = root / f"Lantai-{version}-arm64.zip"
    dmg.write_bytes(b"synthetic notarized DMG")
    zip_path.write_bytes(b"synthetic notarized ZIP")
    binary = root / "synthetic-backend"
    binary.write_bytes(b"synthetic backend -- never executed")
    sbom = root / "lantai-sbom.cdx.json"
    write_json(
        sbom,
        {
            "bomFormat": "CycloneDX",
            "specVersion": "1.6",
            "version": 1,
            "components": [],
        },
    )
    supply = {
        "schema": "com.lantai.release-supply-chain-manifest.v1",
        "source": {"sha": sha},
        "release": {"version": version, "arch": "arm64"},
        "sbom": file_record(sbom),
        "binarySources": [
            {
                "name": "synthetic-backend",
                "path": str(binary),
                "sha256": file_record(binary)["sha256"],
                "version": version,
                "arch": "arm64",
                "source": {"type": "git"},
                "license": "MIT",
                "releasable": True,
            },
        ],
    }
    supply_path = root / "lantai-supply-chain-manifest.json"
    write_json(supply_path, supply)
    manifest = root / "lantai-artifact-manifest.json"
    payload = {
        "schema": "com.lantai.release-artifact-manifest.v1",
        "source": {
            "sha": sha,
            "tag": "v" + version,
            "dirty": False,
            "sourceBranch": "main",
            "canonicalMain": True,
        },
        "release": {
            "version": version,
            "arch": "arm64",
            "artifactOnly": False,
            "candidateId": None,
        },
        "signing": {"mode": "developer-id"},
        "notarization": {
            "requestIds": [
                {"label": "notary-app", "requestIdRedacted": "fake...app"},
                {"label": "notary-dmg", "requestIdRedacted": "fake...dmg"},
            ],
        },
        "supplyChainManifest": supply,
        "artifacts": {"zip": file_record(zip_path), "dmg": file_record(dmg)},
    }
    from scripts.release_build_identity import identity_digest

    payload["buildIdentity"] = {
        "schemaVersion": 1,
        "gitSha": sha,
        "version": version,
        "sourceBranch": "main",
        "canonicalMain": True,
        "artifactOnly": False,
        "candidateId": None,
    }
    payload["buildIdentitySha256"] = identity_digest(payload["buildIdentity"])
    write_json(manifest, payload)
    notary = root / "notary"
    write_json(
        notary / "notary-app.json",
        {"id": "synthetic-app", "status": "Accepted"},
    )
    write_json(
        notary / "notary-dmg.json",
        {"id": "synthetic-dmg", "status": "Accepted"},
    )
    return {
        "ARTIFACT_MANIFEST": manifest,
        "SUPPLY_CHAIN_MANIFEST": supply_path,
        "SBOM_PATH": sbom,
        "NOTARY_RECORD_DIR": notary,
        "SOURCE_DIRTY": "0",
        "ALLOW_UNVERIFIED_LOCAL_BUILD": "0",
        "ALLOW_DIRTY_RELEASE_BUILD": "0",
        "FORCE_ADHOC_LOCAL_BUILD": "0",
        "MAC_SIGNING_MODE": "developer-id",
        "RELEASE_TAG": "v" + version,
        "VERSION": version,
        "SOURCE_SHA": sha,
        "VENV_PYTHON": sys.executable,
    }


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, False),
        ("0", False),
        ("false", False),
        ("off", False),
        ("", False),
        ("1", True),
        ("true", True),
        ("yes", True),
        ("on", True),
    ],
)
def test_skip_notarization_boolean_preserved(fixture_dir, value, expected):
    env = {} if value is None else {"LANTAI_SKIP_NOTARIZATION": value}
    result = run_shell(
        fixture_dir,
        source_function("skip_notarization_requested")
        + "if skip_notarization_requested; then exit 0; else exit 7; fi\n",
        env,
    )
    assert result.returncode == (0 if expected else 7), result.stderr


@pytest.mark.parametrize("value", [None, "", "0", "false", "off"])
def test_public_prerequisites_allow_fully_verified_release(fixture_dir, value):
    env = release_fixture(fixture_dir / "release")
    if value is not None:
        env["LANTAI_SKIP_NOTARIZATION"] = value
    result = run_shell(
        fixture_dir,
        source_function("skip_notarization_requested")
        + source_function("assert_accepted_notary_record")
        + source_function("assert_public_release_prerequisites")
        + "assert_public_release_prerequisites\n",
        env,
    )
    assert (
        result.returncode == 0
    ), f"normal signed/notarized release blocked: {result.stderr.strip()}"


@pytest.mark.parametrize(
    "field,value",
    [
        ("ALLOW_UNVERIFIED_LOCAL_BUILD", "1"),
        ("ALLOW_DIRTY_RELEASE_BUILD", "1"),
        ("LANTAI_ALLOW_UNSIGNED_RELEASE_TAG", "1"),
        *[
            ("FORCE_ADHOC_LOCAL_BUILD", value)
            for value in ("1", "true", "yes", "on")
        ],
        *[
            ("LANTAI_SKIP_NOTARIZATION", value)
            for value in ("1", "true", "yes", "on")
        ],
    ],
)
def test_public_prerequisites_keep_bypasses_blocked(fixture_dir, field, value):
    env = release_fixture(fixture_dir / "release")
    env[field] = value
    result = run_shell(
        fixture_dir,
        source_function("skip_notarization_requested")
        + source_function("assert_accepted_notary_record")
        + source_function("assert_public_release_prerequisites")
        + "assert_public_release_prerequisites\n",
        env,
    )
    assert result.returncode != 0


@pytest.mark.parametrize(
    "fault",
    [
        "source-dirty",
        "signing",
        "tag",
        "manifest",
        "supply",
        "sbom",
        "app-notary",
        "dmg-notary",
    ],
)
def test_public_prerequisites_keep_each_required_evidence_gate(
    fixture_dir,
    fault,
):
    env = release_fixture(fixture_dir / "release")
    fields = {
        "source-dirty": ("SOURCE_DIRTY", "1"),
        "signing": ("MAC_SIGNING_MODE", "unsigned"),
        "tag": ("RELEASE_TAG", "v9.9.9"),
    }
    if fault in fields:
        field, value = fields[fault]
        env[field] = value
    elif fault.endswith("notary"):
        (
            env["NOTARY_RECORD_DIR"]
            / ("notary-" + fault.split("-")[0] + ".json")
        ).unlink()
    else:
        env[
            {
                "manifest": "ARTIFACT_MANIFEST",
                "supply": "SUPPLY_CHAIN_MANIFEST",
                "sbom": "SBOM_PATH",
            }[fault]
        ].unlink()
    script = (
        source_function("skip_notarization_requested")
        + source_function("assert_accepted_notary_record")
        + source_function("assert_public_release_prerequisites")
        + "assert_public_release_prerequisites\n"
    )
    result = run_shell(fixture_dir, script, env)
    assert result.returncode != 0


@pytest.mark.parametrize(
    "authority,allowed",
    [
        ("Developer ID Application: Synthetic", True),
        ("Apple Distribution: Synthetic", False),
        ("Apple Development: Synthetic", False),
        ("", False),
    ],
)
def test_direct_distribution_signature_identity_preserved(
    fixture_dir,
    authority,
    allowed,
):
    script = (
        source_function("validate_direct_distribution_signature")
        + 'codesign_leaf_authority() { printf "%s\\n" "$FAKE_AUTHORITY";'
        " }\nvalidate_direct_distribution_signature synthetic-app app\n"
    )
    result = run_shell(
        fixture_dir,
        script,
        {"MAC_SIGNING_MODE": "developer-id", "FAKE_AUTHORITY": authority},
    )
    assert (result.returncode == 0) is allowed


@pytest.mark.parametrize(
    "candidate,publish,allowed",
    [
        ("independent-source-only", "0", True),
        ("", "0", False),
        ("independent-source-only", "1", False),
    ],
)
def test_artifact_only_isolated_guard_preserved(
    fixture_dir,
    candidate,
    publish,
    allowed,
):
    env = {
        "ARTIFACT_ONLY": "1",
        "ALLOW_UNVERIFIED_LOCAL_BUILD": "1",
        "PUBLISH_GITHUB_RELEASE": publish,
        "RELEASE_CANDIDATE_ID": candidate,
    }
    result = run_shell(
        fixture_dir,
        source_function("validate_artifact_only_request")
        + "validate_artifact_only_request\n",
        env,
    )
    assert (result.returncode == 0) is allowed, result.stderr


def test_ci_build_uses_artifact_only_candidate_mode(fixture_dir):
    # Execute the exact current build step against a recording-only script.
    recorder = fixture_dir / "build_and_deploy.sh"
    recorder.write_text(
        (
            '#!/bin/bash\nprintf "%s\\n" "$@" >'
            ' recorded-build-argv.txt\nprintf "%s\\n"'
            ' "${LANTAI_RELEASE_CANDIDATE_ID:-}" > recorded-candidate-id.txt\n'
        ),
        encoding="utf-8",
    )
    recorder.chmod(0o700)
    result = run_shell(
        fixture_dir,
        step("build-macos-electron", "Build Electron release")["run"],
    )
    assert result.returncode == 0
    argv = (fixture_dir / "recorded-build-argv.txt").read_text().splitlines()
    assert (
        "--artifact-only" in argv
    ), f"CI invokes canonical Downloads deployment: {argv}"
    assert "LANTAI_RELEASE_CANDIDATE_ID" in step(
        "build-macos-electron",
        "Build Electron release",
    ).get("env", {}), (
        "CI candidate needs the builder's existing unique candidate-ID"
        " environment interface"
    )


def _load_authority():
    spec = importlib.util.spec_from_file_location(
        "independent_release_authority",
        W / "scripts/check_release_source_authority.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def mock_source_layout(monkeypatch, module, root, branch, *, has_main=True):
    canonical = root if branch == "main" else root.parent / "synthetic-main"

    def git_output(_root, *args):
        if args == ("rev-parse", "--show-toplevel"):
            return str(root)
        if args == ("rev-parse", "HEAD"):
            return EXPECTED_SHA
        if args == ("rev-parse", "refs/heads/main"):
            if not has_main:
                raise subprocess.CalledProcessError(128, ["synthetic-git"])
            return EXPECTED_SHA
        if args == ("symbolic-ref", "--short", "HEAD"):
            if branch == "DETACHED":
                raise subprocess.CalledProcessError(1, ["synthetic-git"])
            return branch
        if args == ("worktree", "list", "--porcelain"):
            return (
                f"worktree {canonical}\nHEAD {EXPECTED_SHA}\nbranch"
                f" refs/heads/main\n\nworktree {root}\nHEAD"
                f" {EXPECTED_SHA}\ndetached\n"
                if canonical != root
                else (
                    f"worktree {root}\nHEAD {EXPECTED_SHA}\nbranch"
                    " refs/heads/main\n"
                )
            )
        raise AssertionError("unexpected finite Git query")

    monkeypatch.setattr(module, "run_git", git_output)


@pytest.mark.parametrize(
    "branch,allowed",
    [("main", True), ("codex/synthetic", False), ("DETACHED", False)],
)
def test_unique_main_deployment_authority_preserved(
    fixture_dir,
    monkeypatch,
    branch,
    allowed,
):
    module = _load_authority()
    mock_source_layout(monkeypatch, module, fixture_dir, branch)
    assert (
        module.audit_release_source(fixture_dir, artifact_only=False)["ok"]
        is allowed
    )


def test_ci_detached_without_main_can_make_isolated_candidate(
    fixture_dir,
    monkeypatch,
):
    # Actions tag checkout normally has no local refs/heads/main. This must
    # never authorize deployment, but an explicitly isolated candidate is
    # useful.
    module = _load_authority()
    mock_source_layout(
        monkeypatch,
        module,
        fixture_dir,
        "DETACHED",
        has_main=False,
    )
    try:
        report = module.audit_release_source(fixture_dir, artifact_only=True)
    except subprocess.CalledProcessError:
        pytest.fail(
            "explicit artifact-only candidate still requires local"
            " refs/heads/main",
        )
    assert report["ok"] is True and report["canonicalMain"] is False


@pytest.mark.parametrize("stale_field", ["sha", "version"])
def test_ci_provenance_uses_independently_expected_identity(
    fixture_dir,
    stale_field,
):
    sha = "b" * 40 if stale_field == "sha" else EXPECTED_SHA
    version = "9.9.9" if stale_field == "version" else VERSION
    release_fixture(
        fixture_dir / "release/builds/synthetic",
        sha=sha,
        version=version,
    )
    (fixture_dir / ".venv/bin").mkdir(parents=True)
    (fixture_dir / ".venv/bin/python").symlink_to(sys.executable)
    scripts = fixture_dir / "scripts"
    scripts.mkdir()
    (scripts / "check_release_artifact_provenance.py").write_text(
        "import runpy,sys\nsys.path.insert(0,"
        + repr(str(W))
        + ")\nrunpy.run_path("
        + repr(str(W / "scripts/check_release_artifact_provenance.py"))
        + ",run_name='__main__')\n",
        encoding="utf-8",
    )
    result = run_shell(
        fixture_dir,
        step("build-macos-electron", "Verify release provenance manifests")[
            "run"
        ],
        {"GITHUB_SHA": EXPECTED_SHA, "EXPECTED_VERSION": VERSION},
    )
    assert result.returncode != 0, (
        f"CI accepted stale {stale_field} by deriving expectations from the"
        " artifact manifest"
    )


def _record_public_job(root):
    """Execute exact current shell steps after download; upload is file
    selection only.
    """
    steps = workflow()["jobs"]["publish-public-release"]["steps"]
    start = (
        next(
            i
            for i, x in enumerate(steps)
            if x.get("uses", "").startswith("actions/download-artifact@")
        )
        + 1
    )
    selected = []
    for entry in steps[start:]:
        if "run" in entry:
            result = run_shell(
                root,
                entry["run"],
                {"GITHUB_SHA": EXPECTED_SHA, "EXPECTED_VERSION": VERSION},
            )
            if result.returncode:
                return result, selected
        elif entry.get("uses", "").startswith("softprops/action-gh-release@"):
            for pattern in entry["with"]["files"].splitlines():
                selected.extend(
                    p for p in root.glob(pattern.strip()) if p.is_file()
                )
        else:
            raise AssertionError(
                "unexpected external action in finite upload harness",
            )
    return None, selected


@pytest.mark.parametrize(
    "fault",
    ["tampered-dmg", "different-source", "missing-sbom"],
)
def test_public_download_boundary_blocks_invalid_artifacts(fixture_dir, fault):
    release = fixture_dir / "Lantai-macOS-release"
    env = release_fixture(
        release,
        sha="b" * 40 if fault == "different-source" else EXPECTED_SHA,
    )
    if fault == "tampered-dmg":
        next(release.glob("*.dmg")).write_bytes(
            b"modified after builder attestation",
        )
    elif fault == "missing-sbom":
        Path(env["SBOM_PATH"]).unlink()
    failure, selected = _record_public_job(fixture_dir)
    assert failure is not None or not selected, (
        f"upload double selected invalid downloaded artifacts ({fault}); no"
        " downstream validation ran"
    )


def test_public_download_boundary_rejects_duplicate_basenames(fixture_dir):
    first = fixture_dir / "first"
    second = fixture_dir / "second"
    first.mkdir()
    second.mkdir()
    (first / "Lantai-1.2.3-arm64.dmg").write_bytes(b"verified first artifact")
    (second / "Lantai-1.2.3-arm64.dmg").write_bytes(
        b"conflicting second artifact",
    )
    failure, selected = _record_public_job(fixture_dir)
    assert failure is not None or not selected, (
        "duplicate artifact basename was silently flattened and selected for"
        " upload"
    )
    assert (first / "Lantai-1.2.3-arm64.dmg").exists() and (
        second / "Lantai-1.2.3-arm64.dmg"
    ).exists()


@pytest.mark.parametrize("fault_target", ["dmg", "zip"])
def test_failed_artifact_replacement_preserves_previous_pair(
    fixture_dir,
    fault_target,
):
    source = fixture_dir / "source"
    destination = fixture_dir / "synthetic-downloads"
    source.mkdir()
    destination.mkdir()
    for suffix in ("dmg", "zip"):
        (source / f"Lantai-{VERSION}-arm64.{suffix}").write_bytes(
            b"new-" + suffix.encode(),
        )
        (destination / f"Lantai.{suffix}").write_bytes(
            b"old-" + suffix.encode(),
        )
    script = (
        source_function("deploy_release_artifacts")
        + """
find_release_file() { printf '%s/%s\\n' "$SYNTHETIC_SOURCE" "$1"; }
ditto() {
  if [[ "$1" == *".$FAULT_TARGET" ]]; then printf partial > "$2"; return 73; fi
  cp "$1" "$2"
}
xattr() { return 0; }
deploy_release_artifacts
"""
    )
    env = {
        "SYNTHETIC_SOURCE": source,
        "FAULT_TARGET": fault_target,
        "VERSION": VERSION,
        "RELEASE_ARCH": "arm64",
        "RELEASE_DIR": source,
        "DOWNLOAD_DMG": destination / "Lantai.dmg",
        "DOWNLOAD_ZIP": destination / "Lantai.zip",
    }
    result = run_shell(fixture_dir, script, env)
    assert result.returncode == 73
    for suffix in ("dmg", "zip"):
        path = destination / f"Lantai.{suffix}"
        assert (
            path.is_file() and path.read_bytes() == b"old-" + suffix.encode()
        ), f"copy failure destroyed previous {suffix}"


def test_failed_app_copy_preserves_previous_app(fixture_dir):
    source = fixture_dir / "new.app"
    target = fixture_dir / "synthetic-downloads/old.app"
    source.mkdir()
    target.mkdir(parents=True)
    (source / "marker").write_bytes(b"new app")
    (target / "marker").write_bytes(b"old app")
    script = (
        source_function("deploy_app")
        + """
find_release_app() { printf '%s\\n' "$SYNTHETIC_SOURCE"; }
request_app_quit() { return 0; }
terminate_matching_processes() { return 0; }
sleep() { return 0; }
ditto() { mkdir -p "$2"; printf partial > "$2/marker"; return 73; }
deploy_app
"""
    )
    result = run_shell(
        fixture_dir,
        script,
        {
            "SYNTHETIC_SOURCE": source,
            "DOWNLOAD_APP": target,
            "PRODUCT_NAME": "Synthetic Lantai",
            "RELEASE_DIR": source,
        },
    )
    assert (
        result.returncode == 73
    ), "fault must stop before any actual LaunchServices or signature command"
    assert (
        target / "marker"
    ).read_bytes() == b"old app", (
        "copy failure deleted old App and its rollback directory"
    )


@pytest.mark.parametrize(
    "status,allowed",
    [("Accepted", True), ("Invalid", False), ("In Progress", False)],
)
def test_notary_exit_zero_requires_accepted_status(
    fixture_dir,
    status,
    allowed,
):
    response = fixture_dir / "notary-response.json"
    write_json(response, {"id": "synthetic-submission", "status": status})
    output = fixture_dir / "notary-record.json"
    script = (
        source_function("notarize_file_with_notarytool")
        + """
build_notarytool_auth_args() {
  NOTARYTOOL_AUTH_ARGS=(--keychain-profile synthetic-only)
}
xcrun() { cat "$SYNTHETIC_RESPONSE"; return 0; }
sleep() { return 0; }
notarize_file_with_notarytool synthetic-archive.zip app "$SYNTHETIC_OUTPUT"
"""
    )
    result = run_shell(
        fixture_dir,
        script,
        {"SYNTHETIC_RESPONSE": response, "SYNTHETIC_OUTPUT": output},
    )
    assert (
        result.returncode == 0
    ) is allowed, f"notary transport exit zero treated {status} as accepted"
