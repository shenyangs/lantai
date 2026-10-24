"""Upload admission uses downloaded bytes and independently expected identity."""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
from pathlib import Path

import pytest

SHA = "a" * 40
VERSION = "1.2.3"


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _record(path):
    return {"name": path.name, "path": "/builder/" + path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size": path.stat().st_size}


def _release(root):
    root.mkdir(parents=True)
    files = {}
    for suffix in ("dmg", "zip"):
        p = root / f"Lantai-{VERSION}-arm64.{suffix}"
        p.write_bytes(b"synthetic " + suffix.encode())
        files[suffix] = _record(p)
    sbom = root / "lantai-sbom.cdx.json"
    _write(sbom, {"bomFormat": "CycloneDX", "specVersion": "1.6", "components": []})
    supply = {"schema": "com.lantai.release-supply-chain-manifest.v1",
              "source": {"sha": SHA}, "release": {"version": VERSION, "arch": "arm64"},
              "sbom": _record(sbom), "binarySources": [{"name": "backend", "path": "/builder/backend",
              "version": VERSION, "arch": "arm64", "source": {"type": "git"}, "sha256": "c" * 64,
              "license": "MIT", "releasable": True}]}
    _write(root / "lantai-supply-chain-manifest.json", supply)
    manifest = {"schema": "com.lantai.release-artifact-manifest.v1",
                "source": {"sha": SHA, "dirty": False, "tag": "v" + VERSION,
                           "sourceBranch": "main", "canonicalMain": True},
                "release": {"version": VERSION, "arch": "arm64", "artifactOnly": False, "candidateId": None},
                "signing": {"mode": "developer-id", "identityClass": "Developer ID Application"},
                "notarization": {"requestIds": [{"label": "notary-" + label, "status": "Accepted",
                "requestIdRedacted": label + "...safe", "requestIdSHA256": ("d" if label == "app" else "e") * 64}
                for label in ("app", "dmg")]}, "supplyChainManifest": supply, "artifacts": files}
    from scripts.release_build_identity import identity_digest
    manifest['buildIdentity'] = {'schemaVersion': 1, 'gitSha': SHA, 'version': VERSION, 'sourceBranch': 'main',
                                 'canonicalMain': True, 'artifactOnly': False, 'candidateId': None}
    manifest['buildIdentitySha256'] = identity_digest(manifest['buildIdentity'])
    _write(root / "lantai-artifact-manifest.json", manifest)
    return manifest


def _module():
    return importlib.import_module("scripts.check_release_upload")


def _admit(root):
    return _module().admit_macos_release(root, source_sha=SHA, version=VERSION, arch="arm64", public=True)


def test_relocated_complete_verified_bundle_has_exact_five_assets(tmp_path):
    root = tmp_path / "download"
    _release(root)
    paths = _admit(root)
    assert {p.name for p in paths} == {"Lantai-1.2.3-arm64.dmg", "Lantai-1.2.3-arm64.zip",
          "lantai-artifact-manifest.json", "lantai-supply-chain-manifest.json", "lantai-sbom.cdx.json"}
    assert all(p.parent == root for p in paths)


def test_real_supply_chain_producer_record_is_admitted_after_relocation(tmp_path):
    # Use the actual producer, whose SBOM file record has path/bytes rather
    # than the artifact producer's name/path/size shape.
    from scripts.check_release_supply_chain import _file_metadata
    root = tmp_path / "download"
    manifest = _release(root)
    supply = manifest["supplyChainManifest"]
    supply["sbom"] = _file_metadata(root / "lantai-sbom.cdx.json")
    supply["sbom"]["path"] = "/builder/lantai-sbom.cdx.json"
    _write(root / "lantai-supply-chain-manifest.json", supply)
    _write(root / "lantai-artifact-manifest.json", manifest)
    assert len(_admit(root)) == 5


@pytest.mark.parametrize("fault", ["different-bytes", "different-name"])
def test_mixed_file_record_fields_must_agree(tmp_path, fault):
    root = tmp_path / "download"
    manifest = _release(root)
    record = manifest["artifacts"]["dmg"]
    if fault == "different-bytes":
        record["bytes"] = record["size"] + 1
    else:
        record["path"] = "/builder/other.dmg"
    _write(root / "lantai-artifact-manifest.json", manifest)
    with pytest.raises(ValueError):
        _admit(root)


@pytest.mark.parametrize("fault", ["tamper", "sha", "version", "dirty", "tag", "adhoc", "identity",
    "pending", "missing-id", "duplicate-label", "missing-zip", "missing-sbom", "sbom-hash", "supply-drift",
    "unreleasable", "symlink", "path-name", "size", "hash"])
def test_upload_admission_rejects_invalid_downloaded_bundle(tmp_path, fault):
    root = tmp_path / "download"
    manifest = _release(root)
    if fault == "tamper": (root / "Lantai-1.2.3-arm64.dmg").write_bytes(b"tampered")
    elif fault == "sha": manifest["source"]["sha"] = "b" * 40
    elif fault == "version": manifest["release"]["version"] = "9.9.9"
    elif fault == "dirty": manifest["source"]["dirty"] = True
    elif fault == "tag": manifest["source"]["tag"] = "v9.9.9"
    elif fault == "adhoc": manifest["signing"]["mode"] = "unsigned"
    elif fault == "identity": manifest["signing"]["identityClass"] = "Apple Distribution"
    elif fault == "pending": manifest["notarization"]["requestIds"][0]["status"] = "In Progress"
    elif fault == "missing-id": manifest["notarization"]["requestIds"][0].pop("requestIdSHA256")
    elif fault == "duplicate-label": manifest["notarization"]["requestIds"][1] = copy.deepcopy(manifest["notarization"]["requestIds"][0])
    elif fault == "missing-zip": (root / "Lantai-1.2.3-arm64.zip").unlink()
    elif fault == "missing-sbom": (root / "lantai-sbom.cdx.json").unlink()
    elif fault == "sbom-hash": manifest["supplyChainManifest"]["sbom"]["sha256"] = "0" * 64
    elif fault == "supply-drift": manifest["supplyChainManifest"]["source"]["sha"] = "b" * 40
    elif fault == "unreleasable":
        manifest["supplyChainManifest"]["binarySources"][0]["releasable"] = False
        _write(root / "lantai-supply-chain-manifest.json", manifest["supplyChainManifest"])
    elif fault == "symlink":
        asset = root / "Lantai-1.2.3-arm64.dmg"
        outside = tmp_path / "outside.dmg"
        asset.rename(outside)
        asset.symlink_to(outside)
    elif fault == "path-name": manifest["artifacts"]["dmg"]["path"] = "/untrusted/different.dmg"
    elif fault == "size": manifest["artifacts"]["dmg"]["size"] += 1
    elif fault == "hash": manifest["artifacts"]["dmg"]["sha256"] = "x" * 64
    _write(root / "lantai-artifact-manifest.json", manifest)
    with pytest.raises((ValueError, OSError)):
        _admit(root)


def test_stage_revalidates_copied_bytes_and_emits_only_admitted_files(tmp_path):
    root = tmp_path / "incoming"
    _release(root / "macos")
    (root / "unrelated.zip").write_bytes(b"never selected")
    output = tmp_path / "verified-upload"
    paths = _module().stage_upload(root, output, source_sha=SHA, version=VERSION, arch="arm64", public=True)
    assert len(paths) == 5
    assert all(p.parent == output for p in paths)
    assert not (output / "unrelated.zip").exists()
    assert len(_admit(output)) == 5


def test_duplicate_basename_is_rejected_before_any_input_move(tmp_path):
    root = tmp_path / "incoming"
    _release(root / "first")
    _release(root / "second")
    output = tmp_path / "verified-upload"
    with pytest.raises(ValueError):
        _module().stage_upload(root, output, source_sha=SHA, version=VERSION, arch="arm64", public=True)
    assert (root / "first/Lantai-1.2.3-arm64.dmg").exists()
    assert (root / "second/Lantai-1.2.3-arm64.dmg").exists()
    assert not output.exists()


def test_stage_copy_fault_preserves_downloads_and_has_no_upload_directory(tmp_path, monkeypatch):
    root = tmp_path / "incoming"
    _release(root / "macos")
    module = _module()
    def fail_copy(*_args, **_kwargs): raise OSError("synthetic disk failure")
    monkeypatch.setattr(module.shutil, "copy2", fail_copy)
    output = tmp_path / "verified-upload"
    with pytest.raises(OSError):
        module.stage_upload(root, output, source_sha=SHA, version=VERSION, arch="arm64", public=True)
    assert not output.exists()
    assert len(list((root / "macos").iterdir())) == 5


@pytest.mark.parametrize("fault", ["artifact-only", "candidate-id", "detached", "noncanonical", "missing-identity"])
def test_candidate_source_cannot_be_admitted_as_a_public_macos_release(tmp_path, fault):
    root = tmp_path / "download"
    manifest = _release(root)
    manifest["source"].update(sourceBranch="main", canonicalMain=True)
    manifest["release"].update(artifactOnly=False, candidateId=None)
    if fault == "artifact-only": manifest["release"]["artifactOnly"] = True
    elif fault == "candidate-id": manifest["release"]["candidateId"] = "synthetic-candidate"
    elif fault == "detached": manifest["source"]["sourceBranch"] = "DETACHED"
    elif fault == "noncanonical": manifest["source"]["canonicalMain"] = False
    else: manifest["source"].pop("canonicalMain")
    _write(root / "lantai-artifact-manifest.json", manifest)
    with pytest.raises(ValueError):
        _admit(root)
