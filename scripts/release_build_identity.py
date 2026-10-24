"""Bind release metadata to the identity contained in signed payload bytes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re


def identity_digest(identity):
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def validate_identity(identity, source_sha, version, *, public=False):
    if not isinstance(identity, dict) or identity.get("gitSha") != source_sha or identity.get("version") != version:
        raise ValueError("Signed payload build identity differs from expected source/version")
    if not isinstance(identity.get("sourceBranch"), str) or type(identity.get("canonicalMain")) is not bool:
        raise ValueError("Signed payload lacks source authority identity")
    if public and (identity.get("sourceBranch") != "main" or identity.get("canonicalMain") is not True
                   or identity.get("artifactOnly") is not False or identity.get("candidateId")):
        raise ValueError("A signed candidate payload cannot become a public release")
    return identity


def load_app_identity(app, source_sha, version, *, public=False):
    path = Path(app) / "Contents/Resources/console/dist/release-provenance.json"
    if not path.is_file() or path.is_symlink():
        raise ValueError("Signed App build identity is missing or is a symlink")
    return validate_identity(json.loads(path.read_text(encoding="utf-8")), source_sha, version, public=public)


def validate_public_app(app, manifest, source_sha, version, arch):
    # These checks run after the OS signature gate has verified this exact App.
    try:
        from scripts.check_release_artifact_provenance import _validate_app
    except ModuleNotFoundError:
        from check_release_artifact_provenance import _validate_app
    _validate_app(Path(app), version, arch)
    identity = load_app_identity(app, source_sha, version, public=True)
    if manifest.get("buildIdentity") != identity or manifest.get("buildIdentitySha256") != identity_digest(identity):
        raise ValueError("Signed App identity differs from the transported bound manifest")
    return identity
