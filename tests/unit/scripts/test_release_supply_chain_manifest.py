# -*- coding: utf-8 -*-
# pylint: disable=line-too-long
"""Release SBOM and binary source manifest guard tests."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import plistlib
import stat
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _release_roots() -> list[Path]:
    roots = [ROOT]
    mas_root = ROOT.parent / "lantai_App Store"
    if ROOT.name != "lantai_App Store" and mas_root.exists():
        roots.append(mas_root)
    return roots


def _script_names(root: Path) -> list[str]:
    names = ["build_and_deploy.sh"]
    if root.name == "lantai_App Store":
        names.extend(["build_app_store.sh", "build_app_store_dev.sh"])
    return names


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha512_integrity(data: bytes) -> str:
    digest = base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")
    return f"sha512-{digest}"


def _codesign_env(tmp_path: Path) -> dict[str, str]:
    """Provide a deterministic unsigned codesign probe on non-macOS CI."""

    bin_dir = tmp_path / "test-bin"
    bin_dir.mkdir(exist_ok=True)
    codesign = bin_dir / "codesign"
    codesign.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    codesign.chmod(codesign.stat().st_mode | stat.S_IXUSR)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    return env


def _write_minimal_release_root(
    root: Path,
    *,
    officecli_license: str = "Apache-2.0",
) -> Path:
    (root / "console").mkdir(parents=True)
    (root / "build").mkdir()
    package = {
        "name": "lantai-console",
        "version": "1.2.3",
        "build": {"productName": "Lantai"},
        "dependencies": {"react": "^18.2.0"},
        "devDependencies": {"electron": "^42.0.1"},
    }
    (root / "console/package.json").write_text(
        json.dumps(package, indent=2) + "\n",
        encoding="utf-8",
    )
    lock = {
        "name": "lantai-console",
        "version": "1.2.3",
        "lockfileVersion": 3,
        "packages": {
            "": {
                "name": "lantai-console",
                "version": "1.2.3",
                "dependencies": package["dependencies"],
                "devDependencies": package["devDependencies"],
            },
            "node_modules/electron": {
                "version": "42.0.1",
                "resolved": "https://registry.npmjs.org/electron/-/electron-42.0.1.tgz",  # noqa: E501
                "integrity": _sha512_integrity(b"electron package"),
                "license": "MIT",
                "dev": True,
            },
            "node_modules/react": {
                "version": "18.2.0",
                "resolved": "https://registry.npmjs.org/react/-/react-18.2.0.tgz",  # noqa: E501
                "integrity": _sha512_integrity(b"react package"),
                "license": "MIT",
            },
        },
    }
    (root / "console/package-lock.json").write_text(
        json.dumps(lock, indent=2) + "\n",
        encoding="utf-8",
    )
    guanlan_sdist_hash = "a" * 64
    guanlan_wheel_hash = "b" * 64
    (root / "uv.lock").write_text(
        "\n".join(
            [
                "version = 1",
                "",
                "[[package]]",
                'name = "guanlan"',
                'version = "0.6.13"',
                'source = { registry = "https://pypi.org/simple" }',
                (
                    'sdist = { url = "https://files.pythonhosted.org/packages/'
                    'guanlan-0.6.13.tar.gz", hash = '
                    f'"sha256:{guanlan_sdist_hash}", size = 123 }}'
                ),
                "wheels = [",
                (
                    '    { url = "https://files.pythonhosted.org/packages/'
                    'guanlan-0.6.13-py3-none-any.whl", hash = '
                    f'"sha256:{guanlan_wheel_hash}", size = 456 }},'
                ),
                "]",
                "",
            ],
        ),
        encoding="utf-8",
    )
    (root / "build/release-dependency-manifest.json").write_text(
        json.dumps(
            {
                "schema": "com.lantai.release-dependency-manifest.v1",
                "source": {"gitHead": "f" * 40},
            },
        )
        + "\n",
        encoding="utf-8",
    )

    app = root / "release/Lantai.app"
    macos = app / "Contents/MacOS"
    framework = app / "Contents/Frameworks/Electron Framework.framework"
    backend = app / "Contents/Resources/lantai-server"
    backend_executable = (
        backend / "Lantai Backend.app/Contents/MacOS/lantai-server"
    )
    mac_use_helper = (
        app
        / "Contents/Resources/mac-use"
        / "Lantai Mac Use Helper.app/Contents/MacOS/lantai-mac-use-helper"
    )
    officecli_bin = backend / "bin"
    for directory in (
        macos,
        framework,
        backend_executable.parent,
        mac_use_helper.parent,
        officecli_bin,
    ):
        directory.mkdir(parents=True)
    with (app / "Contents/Info.plist").open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleExecutable": "Lantai",
                "CFBundleShortVersionString": "1.2.3",
                "CFBundleVersion": "1.2.3",
            },
            handle,
        )

    for path, data in (
        (macos / "Lantai", b"app launcher"),
        (framework / "Electron Framework", b"electron framework"),
        (backend_executable, b"backend executable"),
        (mac_use_helper, b"mac use helper"),
        (officecli_bin / "officecli", b"officecli executable"),
    ):
        path.write_bytes(data)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    officecli_provenance = {
        "schema": "com.lantai.officecli-bundle-provenance.v1",
        "source_kind": "vendored:darwin-arm64",
        "source_url": "https://github.com/iOfficeAI/OfficeCLI/releases/download/v1.0.102/officecli-mac-arm64",  # noqa: E501
        "sha256": _sha256_bytes(b"officecli executable"),
        "version": "officecli 1.0.102",
        "expected_version": "1.0.102",
        "expected_arch": "arm64",
        "license": officecli_license,
        "license_file": "officecli-LICENSE.txt",
        "notice_file": "officecli-NOTICE.txt",
        "license_sha256": _sha256_bytes(b"Apache License\nVersion 2.0\n"),
        "notice_sha256": _sha256_bytes(b"Copyright 2026 OfficeCLI\n"),
        "releasable": True,
        "signature_status": "Authority=Developer ID Application",
        "team_identifier": "TEAMID1234",
        "spctl_status": "accepted",
    }
    (officecli_bin / "officecli-LICENSE.txt").write_bytes(
        b"Apache License\nVersion 2.0\n",
    )
    (officecli_bin / "officecli-NOTICE.txt").write_bytes(
        b"Copyright 2026 OfficeCLI\n",
    )
    (officecli_bin / "officecli.provenance.json").write_text(
        json.dumps(officecli_provenance, indent=2) + "\n",
        encoding="utf-8",
    )
    return app


def test_release_scripts_generate_supply_chain_manifest_and_attach_it():
    for root in _release_roots():
        helper = root / "scripts/check_release_supply_chain.py"
        artifact_helper = root / "scripts/check_release_artifact_provenance.py"
        assert helper.is_file()
        assert "--supply-chain-manifest" in artifact_helper.read_text(
            encoding="utf-8",
        )

        for script_name in _script_names(root):
            text = (root / script_name).read_text(encoding="utf-8")
            assert "SUPPLY_CHAIN_MANIFEST" in text
            assert "SBOM_PATH" in text
            assert "check_release_supply_chain.py" in text
            assert "write_supply_chain_manifest" in text
            assert "--supply-chain-manifest" in text


def test_supply_chain_helper_writes_cyclonedx_sbom_and_binary_manifest(
    tmp_path,
):
    helper = ROOT / "scripts/check_release_supply_chain.py"
    release_root = tmp_path / "repo"
    app = _write_minimal_release_root(release_root)
    manifest_path = release_root / "release/lantai-supply-chain-manifest.json"
    sbom_path = release_root / "release/lantai-sbom.cdx.json"

    subprocess.run(
        [
            sys.executable,
            str(helper),
            "write-manifest",
            "--root",
            str(release_root),
            "--release-dir",
            str(release_root / "release"),
            "--output",
            str(manifest_path),
            "--sbom-output",
            str(sbom_path),
            "--source-sha",
            "f" * 40,
            "--version",
            "1.2.3",
            "--arch",
            "arm64",
            "--app",
            str(app),
            "--dependency-manifest",
            str(release_root / "build/release-dependency-manifest.json"),
        ],
        check=True,
        env=_codesign_env(tmp_path),
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
    binary_names = {entry["name"] for entry in manifest["binarySources"]}
    component_names = {entry["name"] for entry in sbom["components"]}

    assert manifest["schema"] == "com.lantai.release-supply-chain-manifest.v1"
    assert sbom["bomFormat"] == "CycloneDX"
    assert {
        "mac-use-helper",
        "electron-runtime",
        "officecli",
        "lantai-server",
    } <= binary_names
    assert {"electron", "react", "guanlan"} <= component_names
    assert manifest["sbom"]["sha256"] == _sha256_bytes(sbom_path.read_bytes())
    assert manifest["source"]["sha"] == "f" * 40


def test_supply_chain_helper_allows_missing_optional_officecli_only_for_local_build(  # noqa: E501
    tmp_path,
):
    helper = ROOT / "scripts/check_release_supply_chain.py"
    release_root = tmp_path / "repo"
    app = _write_minimal_release_root(release_root)
    officecli_dir = app / "Contents/Resources/lantai-server/bin"
    (officecli_dir / "officecli").unlink()
    (officecli_dir / "officecli.provenance.json").unlink()
    manifest_path = release_root / "release/lantai-supply-chain-manifest.json"
    sbom_path = release_root / "release/lantai-sbom.cdx.json"
    command = [
        sys.executable,
        str(helper),
        "write-manifest",
        "--root",
        str(release_root),
        "--release-dir",
        str(release_root / "release"),
        "--output",
        str(manifest_path),
        "--sbom-output",
        str(sbom_path),
        "--source-sha",
        "f" * 40,
        "--version",
        "1.2.3",
        "--arch",
        "arm64",
        "--app",
        str(app),
        "--dependency-manifest",
        str(release_root / "build/release-dependency-manifest.json"),
    ]

    strict = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    assert strict.returncode != 0
    assert "missing bundled officecli" in strict.stderr.lower()

    subprocess.run(
        [*command, "--allow-unreleasable-binaries"],
        check=True,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "officecli" not in {
        entry["name"] for entry in manifest["binarySources"]
    }


def test_supply_chain_helper_records_signed_officecli_bundle_hash(tmp_path):
    helper = ROOT / "scripts/check_release_supply_chain.py"
    release_root = tmp_path / "repo"
    app = _write_minimal_release_root(release_root)
    officecli = app / "Contents/Resources/lantai-server/bin/officecli"
    original_sha = _sha256_bytes(b"officecli executable")
    signed_bytes = b"officecli executable after codesign"
    officecli.write_bytes(signed_bytes)
    manifest_path = release_root / "release/lantai-supply-chain-manifest.json"
    sbom_path = release_root / "release/lantai-sbom.cdx.json"

    subprocess.run(
        [
            sys.executable,
            str(helper),
            "write-manifest",
            "--root",
            str(release_root),
            "--release-dir",
            str(release_root / "release"),
            "--output",
            str(manifest_path),
            "--sbom-output",
            str(sbom_path),
            "--source-sha",
            "f" * 40,
            "--version",
            "1.2.3",
            "--arch",
            "arm64",
            "--app",
            str(app),
            "--dependency-manifest",
            str(release_root / "build/release-dependency-manifest.json"),
        ],
        check=True,
        env=_codesign_env(tmp_path),
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    officecli_entry = next(
        entry
        for entry in manifest["binarySources"]
        if entry["name"] == "officecli"
    )
    assert officecli_entry["sourceSha256"] == original_sha
    assert officecli_entry["bundleSha256"] == _sha256_bytes(signed_bytes)
    assert officecli_entry["sha256"] == officecli_entry["bundleSha256"]
    assert officecli_entry["source"]["sha256"] == original_sha
    assert officecli_entry["signature"]["status"] == "not-signed"
    assert (
        officecli_entry["provenanceSignature"]["status"]
        == "Authority=Developer ID Application"
    )


def test_supply_chain_helper_rejects_binary_without_license_source(tmp_path):
    helper = ROOT / "scripts/check_release_supply_chain.py"
    release_root = tmp_path / "repo"
    app = _write_minimal_release_root(release_root, officecli_license="")

    result = subprocess.run(
        [
            sys.executable,
            str(helper),
            "write-manifest",
            "--root",
            str(release_root),
            "--release-dir",
            str(release_root / "release"),
            "--output",
            str(release_root / "release/lantai-supply-chain-manifest.json"),
            "--sbom-output",
            str(release_root / "release/lantai-sbom.cdx.json"),
            "--source-sha",
            "f" * 40,
            "--version",
            "1.2.3",
            "--arch",
            "arm64",
            "--app",
            str(app),
            "--dependency-manifest",
            str(release_root / "build/release-dependency-manifest.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "license" in result.stderr.lower()


def test_artifact_skip_manifest_validates_embedded_supply_chain_sources(
    tmp_path,
):
    supply_helper = ROOT / "scripts/check_release_supply_chain.py"
    artifact_helper = ROOT / "scripts/check_release_artifact_provenance.py"
    release_root = tmp_path / "repo"
    app = _write_minimal_release_root(release_root)
    release_dir = release_root / "release"
    supply_manifest_path = release_dir / "lantai-supply-chain-manifest.json"
    sbom_path = release_dir / "lantai-sbom.cdx.json"

    subprocess.run(
        [
            sys.executable,
            str(supply_helper),
            "write-manifest",
            "--root",
            str(release_root),
            "--release-dir",
            str(release_dir),
            "--output",
            str(supply_manifest_path),
            "--sbom-output",
            str(sbom_path),
            "--source-sha",
            "f" * 40,
            "--version",
            "1.2.3",
            "--arch",
            "arm64",
            "--app",
            str(app),
            "--dependency-manifest",
            str(release_root / "build/release-dependency-manifest.json"),
        ],
        check=True,
        env=_codesign_env(tmp_path),
    )

    supply_manifest = json.loads(
        supply_manifest_path.read_text(encoding="utf-8"),
    )
    artifact_manifest_path = release_dir / "lantai-artifact-manifest.json"
    artifact_manifest = {
        "schema": "com.lantai.release-artifact-manifest.v1",
        "source": {"sha": "f" * 40},
        "release": {"version": "1.2.3", "arch": "arm64"},
        "artifacts": {},
        "supplyChainManifest": supply_manifest,
    }
    artifact_manifest_path.write_text(
        json.dumps(artifact_manifest, indent=2) + "\n",
        encoding="utf-8",
    )

    subprocess.run(
        [
            sys.executable,
            str(artifact_helper),
            "validate-skip-manifest",
            "--manifest",
            str(artifact_manifest_path),
            "--release-dir",
            str(release_dir),
            "--source-sha",
            "f" * 40,
            "--version",
            "1.2.3",
            "--arch",
            "arm64",
        ],
        check=True,
    )

    broken = json.loads(json.dumps(artifact_manifest))
    broken["supplyChainManifest"]["binarySources"][0]["source"]["url"] = ""
    artifact_manifest_path.write_text(
        json.dumps(broken, indent=2) + "\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(artifact_helper),
            "validate-skip-manifest",
            "--manifest",
            str(artifact_manifest_path),
            "--release-dir",
            str(release_dir),
            "--source-sha",
            "f" * 40,
            "--version",
            "1.2.3",
            "--arch",
            "arm64",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "source URL" in result.stderr
