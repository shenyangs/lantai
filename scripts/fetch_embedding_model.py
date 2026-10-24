#!/usr/bin/env python3
"""Fetch the pinned local embedding asset and verify every file."""

from __future__ import annotations

import hashlib
import json
import tempfile
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "src/lantai/embedding_models/bge-small-zh-v1.5"
REVISION = "9507db33464b5da99a532ac26b2a251767cbc62b"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    manifest = json.loads((DESTINATION / "manifest.json").read_text(encoding="utf-8"))
    checksums = dict(manifest["sha256"])
    with tempfile.TemporaryDirectory(prefix="lantai-embedding-") as temporary:
        staging = Path(temporary)
        for relative, expected in checksums.items():
            current = DESTINATION / relative
            if current.is_file() and _sha256(current) == expected:
                continue
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            url = (
                "https://huggingface.co/onnx-community/"
                f"bge-small-zh-v1.5-ONNX/resolve/{REVISION}/{relative}?download=true"
            )
            print(f"Downloading {relative}")
            urllib.request.urlretrieve(url, target)
            actual = _sha256(target)
            if actual != expected:
                raise RuntimeError(
                    f"checksum mismatch for {relative}: expected {expected}, got {actual}",
                )
            current.parent.mkdir(parents=True, exist_ok=True)
            target.replace(current)
    print(f"Embedding model ready: {DESTINATION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

