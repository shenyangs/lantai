from __future__ import annotations

import json
from pathlib import Path

from scripts.check_builtin_skill_licenses import validate_manifest


def _entry(source_name: str, **overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "skill": source_name.removesuffix("-en").removesuffix("-zh"),
        "source_name": source_name,
        "origin": "Lantai clean-room implementation",
        "upstream": None,
        "upstream_sha": None,
        "license": "Apache-2.0",
        "bundled": True,
        "modified": True,
        "redistribution_status": "allowed",
    }
    entry.update(overrides)
    return entry


def _write_manifest(path: Path, entries: list[dict[str, object]]) -> None:
    path.write_text(
        json.dumps({"schema_version": 1, "entries": entries}), encoding="utf-8"
    )


def test_gate_accepts_complete_open_source_manifest(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    (skills / "pptx-en").mkdir(parents=True)
    (skills / "pptx-en" / "SKILL.md").write_text("license: Apache-2.0\n")
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, [_entry("pptx-en")])

    assert validate_manifest(manifest, skills) == []


def test_gate_rejects_missing_entry_and_proprietary_marker(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    (skills / "docx-en").mkdir(parents=True)
    (skills / "docx-en" / "SKILL.md").write_text("license: Proprietary\n")
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, [])

    errors = validate_manifest(manifest, skills)

    assert any("missing provenance" in error for error in errors)
    assert any("forbidden proprietary marker" in error for error in errors)


def test_gate_requires_authorization_for_source_available_license(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    (skills / "xlsx-en").mkdir(parents=True)
    (skills / "xlsx-en" / "SKILL.md").write_text("license: Apache-2.0\n")
    manifest = tmp_path / "manifest.json"
    _write_manifest(
        manifest,
        [_entry("xlsx-en", license="source-available", redistribution_status="allowed")],
    )

    errors = validate_manifest(manifest, skills)

    assert any("authorization_reference" in error for error in errors)
