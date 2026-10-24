"""Only temporary synthetic sources; source checksums must bind extracted data."""
from __future__ import annotations

import hashlib

import pytest

from lantai.presentation import material_extraction
from lantai.presentation.material_extraction import PresentationMaterialError, read_material
from lantai.presentation.models import InputMaterial


def test_declared_inline_checksum_cannot_be_silently_ignored():
    old = "预算上限27万元。禁止上传外部云盘。"
    new = "预算上限83万元。允许上传外部云盘。"
    declared = hashlib.sha256(old.encode()).hexdigest()
    material = InputMaterial(id="inline", name="scope.md", content=new, sha256=declared)
    with pytest.raises(PresentationMaterialError):
        read_material(material)


def test_path_change_after_digest_is_rejected_or_uses_the_attested_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "scope.md"
    old = "预算上限27万元。禁止上传外部云盘。"
    new = "预算上限83万元。允许上传外部云盘。"
    path.write_text(old, encoding="utf-8")
    declared = hashlib.sha256(path.read_bytes()).hexdigest()
    real_digest = material_extraction._digest
    changed = False

    def digest_then_writer(source):
        nonlocal changed
        result = real_digest(source)
        if source == path and not changed:
            changed = True
            replacement = tmp_path / "atomic-new.md"
            replacement.write_text(new, encoding="utf-8")
            replacement.replace(path)
        return result

    monkeypatch.setattr(material_extraction, "_digest", digest_then_writer)
    material = InputMaterial(id="scope", name=path.name, path=str(path), sha256=declared)
    try:
        text, coverage = read_material(material)
    except PresentationMaterialError:
        return
    assert text == old, "read newer unverified bytes after accepting the declared old checksum"
    assert coverage.source_sha256 == hashlib.sha256(text.encode()).hexdigest()
