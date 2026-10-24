#!/usr/bin/env python3
"""Admit a precise release asset set from fresh local bytes before upload."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ARTIFACT_MANIFEST = "lantai-artifact-manifest.json"
SUPPLY_MANIFEST = "lantai-supply-chain-manifest.json"
SBOM = "lantai-sbom.cdx.json"
WINDOWS_NAMES = {
    "manifest": "lantai-windows-x64-artifact-manifest.json",
    "supply": "lantai-windows-x64-supply-chain-manifest.json",
    "sbom": "lantai-windows-x64-sbom.cdx.json",
    "signing": "lantai-windows-x64-signing-receipt.json",
}


def _real_path(path):
    path = Path(os.path.abspath(path))
    _require(all(not ancestor.is_symlink() for ancestor in (path, *path.parents)),
             f"Symlink in release path: {path}")
    return path


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _json(path):
    _require(path.is_file() and not path.is_symlink(), f"Missing regular sidecar: {path.name}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    _require(isinstance(payload, dict), f"Invalid sidecar object: {path.name}")
    return payload


def _sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file(root, record, name):
    _require(isinstance(record, dict), f"Missing file record: {name}")
    _require(record.get("name", name) == name and Path(str(record.get("path", ""))).name == name,
             f"File record name/path mismatch: {name}")
    # Builder paths are metadata. Downloads are always resolved inside this
    # admitted directory; an absolute builder path is never followed.
    path = root / name
    _require(path.is_file() and not path.is_symlink(), f"Missing regular artifact: {name}")
    sizes = [record[k] for k in ("size", "bytes") if k in record]
    _require(bool(sizes) and all(type(size) is int and size > 0 and size == path.stat().st_size for size in sizes),
             f"Artifact size mismatch: {name}")
    _require(isinstance(record.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", record["sha256"]),
             f"Invalid artifact digest: {name}")
    _require(_sha(path) == record["sha256"], f"Artifact digest mismatch: {name}")
    return path


def admit_macos_release(root: Path, *, source_sha: str, version: str, arch: str, public: bool) -> list[Path]:
    root = _real_path(root)
    _require(bool(re.fullmatch(r"[0-9a-f]{40}", source_sha)), "Expected source SHA must be independently supplied")
    _require(bool(re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version)), "Invalid expected version")
    _require(arch in {"arm64", "x64", "universal"}, "Invalid expected macOS architecture")
    manifest = _json(root / ARTIFACT_MANIFEST)
    _require(manifest.get("schema") == "com.lantai.release-artifact-manifest.v1", "Invalid artifact schema")
    source, release = manifest.get("source", {}), manifest.get("release", {})
    _require(source.get("sha") == source_sha, "Artifact source SHA mismatch")
    _require(release.get("version") == version and release.get("arch") == arch, "Artifact version/architecture mismatch")
    if public:
        _require(source.get("sourceBranch") == "main" and source.get("canonicalMain") is True,
                 "Public macOS source must originate from the unique main authority")
        _require(release.get("artifactOnly") is False and not release.get("candidateId"),
                 "macOS candidates cannot become official assets")
        try:
            from scripts.release_build_identity import validate_identity, identity_digest
        except ModuleNotFoundError:
            from release_build_identity import validate_identity, identity_digest
        bound_identity = validate_identity(manifest.get("buildIdentity"), source_sha, version, public=True)
        _require(manifest.get("buildIdentitySha256") == identity_digest(bound_identity),
                 "macOS artifact manifest does not bind its payload identity")
        _require(source.get("dirty") is False and source.get("tag") == "v" + version, "Public source must be clean and tagged")
        signing = manifest.get("signing", {})
        _require(signing.get("mode") == "developer-id" and signing.get("identityClass") == "Developer ID Application",
                 "Public macOS assets require Developer ID Application evidence")
        records = manifest.get("notarization", {}).get("requestIds", [])
        _require(isinstance(records, list), "Missing notary records")
        for label in ("notary-app", "notary-dmg"):
            matches = [r for r in records if isinstance(r, dict) and r.get("label") == label]
            _require(len(matches) == 1, f"Missing or duplicate notarization record: {label}")
            record = matches[0]
            _require(record.get("status") == "Accepted" and bool(record.get("requestIdRedacted"))
                     and isinstance(record.get("requestIdSHA256"), str)
                     and re.fullmatch(r"[0-9a-f]{64}", record["requestIdSHA256"]),
                     f"Notarization not settled with a valid identity: {label}")
    supply = _json(root / SUPPLY_MANIFEST)
    _require(supply == manifest.get("supplyChainManifest"), "Supply-chain sidecar differs from bound manifest")
    _require(supply.get("schema") == "com.lantai.release-supply-chain-manifest.v1", "Invalid supply-chain schema")
    _require(supply.get("source", {}).get("sha") == source_sha, "Supply-chain source SHA mismatch")
    _require(supply.get("release", {}).get("version") == version and supply.get("release", {}).get("arch") == arch,
             "Supply-chain version/architecture mismatch")
    sbom_path = _file(root, supply.get("sbom"), SBOM)
    _require(_json(sbom_path).get("bomFormat") == "CycloneDX", "Invalid CycloneDX SBOM")
    binaries = supply.get("binarySources")
    _require(isinstance(binaries, list) and bool(binaries), "Missing binary source evidence")
    for binary in binaries:
        _require(isinstance(binary, dict), "Invalid binary source evidence")
        _require(all(binary.get(k) for k in ("name", "version", "arch", "license", "sha256", "source")),
                 "Incomplete binary source evidence")
        origin = binary["source"]
        _require(isinstance(origin, dict) and bool(origin.get("type")), "Missing binary source type")
        _require(bool(re.fullmatch(r"[0-9a-f]{64}", str(binary["sha256"]))), "Invalid binary source digest")
        if public:
            _require(binary.get("releasable") is True, f"Binary redistribution is unverified: {binary['name']}")
            _require(origin.get("type") == "git" or bool(origin.get("url")), "Missing binary origin URL")
    assets = manifest.get("artifacts", {})
    admitted = [_file(root, assets.get(suffix), f"Lantai-{version}-{arch}.{suffix}") for suffix in ("dmg", "zip")]
    return admitted + [root / ARTIFACT_MANIFEST, root / SUPPLY_MANIFEST, sbom_path]


def admit_windows_release(root: Path, *, source_sha: str, version: str, public: bool,
                          authority_root: Path | None = None, signer_sha256: str | None = None) -> list[Path]:
    """Check transported bytes. Official admission also executes OS/authority gates.

    Candidate admission on Linux is metadata evidence only. A fixture receipt or
    a locally written status string can never replace the Windows trust verifier.
    """
    root = _real_path(root)
    _require(bool(re.fullmatch(r"[0-9a-f]{40}", source_sha)), "Invalid independently expected source SHA")
    _require(bool(re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version)), "Invalid expected version")
    manifest = _json(root / WINDOWS_NAMES["manifest"])
    _require(manifest.get("schema") == "com.lantai.release-artifact-manifest.v1", "Invalid Windows artifact schema")

    def identity(value, label):
        _require(value.get("source", {}).get("sha") == source_sha, f"Windows {label} source SHA mismatch")
        release = value.get("release", {})
        _require(release.get("version") == version and release.get("arch") == "x64"
                 and release.get("platform") == "win32", f"Windows {label} version/platform/architecture mismatch")

    identity(manifest, "artifact")
    supply = _json(root / WINDOWS_NAMES["supply"])
    _require(supply == manifest.get("supplyChainManifest"), "Windows supply-chain sidecar is not bound")
    _require(supply.get("schema") == "com.lantai.release-supply-chain-manifest.v1", "Invalid Windows supply-chain schema")
    identity(supply, "supply-chain")
    sbom = _file(root, supply.get("sbom"), WINDOWS_NAMES["sbom"])
    _require(_json(sbom).get("bomFormat") == "CycloneDX", "Invalid Windows CycloneDX SBOM")
    binaries = supply.get("binarySources")
    _require(isinstance(binaries, list) and bool(binaries), "Missing Windows binary sources")
    for binary in binaries:
        _require(isinstance(binary, dict) and all(binary.get(key) for key in
                 ("name", "version", "arch", "license", "sha256", "source")), "Incomplete Windows binary source")
        _require(bool(re.fullmatch(r"[0-9a-f]{64}", str(binary["sha256"]))), "Invalid Windows binary digest")
        _require(isinstance(binary["source"], dict) and bool(binary["source"].get("type")), "Missing Windows binary origin")
        if public:
            _require(binary.get("releasable") is True, f"Unverified Windows redistribution: {binary['name']}")
            _require(binary["source"].get("type") == "git" or bool(binary["source"].get("url")), "Missing Windows origin URL")
    installer = _file(root, manifest.get("artifacts", {}).get("installer"), f"Lantai-Setup-{version}.exe")
    signing = manifest.get("signing", {})
    _require(signing.get("mode") == "authenticode", "Windows installer is not Authenticode signed")
    receipt_path = _file(root, signing.get("evidence"), WINDOWS_NAMES["signing"])
    receipt = _json(receipt_path)
    _require(receipt.get("schema") == "com.lantai.windows-signing-evidence.v1", "Invalid Windows signing schema")
    identity(receipt, "signing")
    _file(root, receipt.get("artifact"), installer.name)
    _require(receipt.get("status") == "Valid" and type(receipt.get("verificationExitCode")) is int
             and receipt["verificationExitCode"] == 0 and receipt.get("timestampPresent") is True,
             "Windows signing verification/timestamp is not complete")
    _require(bool(re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("signerCertificateSha256", "")))),
             "Missing Windows signer identity")
    if public:
        _require(receipt.get("testDouble") is not True, "A signing test double is never public evidence")
        try:
            from scripts.release_build_identity import validate_identity, identity_digest
        except ModuleNotFoundError:
            from release_build_identity import validate_identity, identity_digest
        payload_identity = validate_identity(supply.get("provenance"), source_sha, version, public=True)
        payload_digest = identity_digest(payload_identity)
        _require(supply.get("provenanceSha256") == payload_digest
                 and receipt.get("buildIdentitySha256") == payload_digest,
                 "Windows signed payload identity is not bound to its evidence")
        _require(manifest.get("source", {}).get("sourceBranch") == "main"
                 and manifest.get("source", {}).get("canonicalMain") is True,
                 "Public Windows source lacks main authority identity")
        _require(manifest.get("source", {}).get("dirty") is False
                 and manifest.get("source", {}).get("tag") == "v" + version, "Public Windows source must be clean and tagged")
        _require(not manifest.get("release", {}).get("candidateId")
                 and manifest.get("release", {}).get("artifactOnly") is False, "Windows candidates cannot become official assets")
        _require(sys.platform == "win32" and authority_root is not None, "Official Windows intake requires the actual canonical-main Windows verifier")
        _require(bool(re.fullmatch(r"[0-9a-f]{64}", str(signer_sha256 or "")))
                 and signer_sha256 == receipt["signerCertificateSha256"], "Windows signer differs from independent approved identity")
        scripts = Path(__file__).resolve().parent
        subprocess.run([sys.executable, str(scripts / "check_release_source_authority.py"),
                        "--root", str(authority_root), "--expected-source-sha", source_sha,
                        "--require-clean"], check=True)
        subprocess.run(["pwsh", "-NoProfile", "-File", str(scripts / "verify_windows_release_signature.ps1"),
                        "-Installer", str(installer), "-ExpectedSourceSha", source_sha, "-ExpectedVersion", version,
                        "-ExpectedSignerSha256", signer_sha256, "-ExpectedBuildIdentitySha256", payload_digest,
                        "-Receipt", str(receipt_path)], check=True)
    return [installer, root / WINDOWS_NAMES["manifest"], root / WINDOWS_NAMES["supply"], sbom, receipt_path]


def _recognized(path):
    return (path.name in {ARTIFACT_MANIFEST, SUPPLY_MANIFEST, SBOM, *WINDOWS_NAMES.values()}
            or (path.name.startswith("Lantai-") and path.suffix in {".dmg", ".zip", ".exe"}))


def stage_upload(download_root: Path, output: Path, *, source_sha: str, version: str, arch: str, public: bool,
                 platforms=("macos",), flat_root=False, authority_root=None, signer_sha256=None) -> list[Path]:
    download_root, output = _real_path(download_root), _real_path(output)
    _require(not output.exists() and not output.is_symlink(), "Upload staging directory already exists")
    _require(bool(platforms) and len(platforms) == len(set(platforms))
             and set(platforms) <= {"macos", "windows"}, "Invalid required platform list")
    inventory = list(download_root.iterdir() if flat_root else download_root.rglob("*"))
    _require(not any(path.is_symlink() for path in inventory), "Symlink in downloaded release inputs")
    candidates = [p for p in inventory if _recognized(p)]
    _require(all(p.is_file() for p in candidates), "Release asset must be a regular file")
    names = [p.name for p in candidates]
    _require(len(names) == len(set(names)), "Duplicate downloaded artifact basename")
    def admit(root, platform):
        if platform == "macos":
            return admit_macos_release(root, source_sha=source_sha, version=version, arch=arch, public=public)
        return admit_windows_release(root, source_sha=source_sha, version=version, public=public,
                                     authority_root=authority_root, signer_sha256=signer_sha256)
    admitted = []
    for platform in platforms:
        name = ARTIFACT_MANIFEST if platform == "macos" else WINDOWS_NAMES["manifest"]
        manifests = [p for p in candidates if p.name == name]
        _require(len(manifests) == 1, f"Expected exactly one {platform} artifact manifest")
        admitted.extend(admit(manifests[0].parent, platform))
    _require(set(names) == {path.name for path in admitted}, "Unexpected or unbound downloaded release asset")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".lantai-upload-", dir=output.parent))
    try:
        stage.chmod(0o700)
        for path in admitted:
            shutil.copy2(path, stage / path.name, follow_symlinks=False)
        for platform in platforms:
            admit(stage, platform)
        os.rename(stage, output)
        return [output / p.name for p in admitted]
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "stage"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--arch", choices=("arm64", "x64", "universal"), required=True)
    parser.add_argument("--public", action="store_true")
    parser.add_argument("--platform", action="append", choices=("macos", "windows"))
    parser.add_argument("--flat-root", action="store_true")
    parser.add_argument("--authority-root", type=Path)
    parser.add_argument("--windows-signer-sha256")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    kwargs = dict(source_sha=args.source_sha, version=args.version, arch=args.arch, public=args.public)
    if args.command == "stage":
        _require(args.output is not None, "Stage requires an owned output directory")
        paths = stage_upload(args.root, args.output, **kwargs, platforms=args.platform or ("macos",),
                             flat_root=args.flat_root, authority_root=args.authority_root,
                             signer_sha256=args.windows_signer_sha256)
    else:
        paths = []
        for platform in args.platform or ("macos",):
            if platform == "macos":
                paths.extend(admit_macos_release(args.root, **kwargs))
            else:
                paths.extend(admit_windows_release(args.root, source_sha=args.source_sha, version=args.version,
                                                   public=args.public, authority_root=args.authority_root,
                                                   signer_sha256=args.windows_signer_sha256))
    if args.github_output:
        # All emitted paths were selected by the verifier. The action receives
        # this exact list, never an unconstrained current-directory glob.
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write("files<<LANTAI_VERIFIED_ASSETS\n")
            for path in paths:
                handle.write(str(path) + "\n")
            handle.write("LANTAI_VERIFIED_ASSETS\n")
    print(json.dumps({"sourceSha": args.source_sha, "version": args.version, "files": [str(p) for p in paths]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
