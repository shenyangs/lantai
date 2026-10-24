#!/usr/bin/env python3
"""Fail when an officially bundled Skill lacks distributable provenance."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable


REQUIRED_FIELDS = {
    "skill",
    "source_name",
    "origin",
    "upstream",
    "upstream_sha",
    "license",
    "bundled",
    "modified",
    "redistribution_status",
}
VALID_REDISTRIBUTION_STATUSES = {"allowed", "blocked", "review"}
RESTRICTED_LICENSE_WORDS = ("proprietary", "source-available", "source available")
FORBIDDEN_BUNDLED_MARKERS = (
    "© 2025 Anthropic",
    "Anthropic, PBC",
    "ADDITIONAL RESTRICTIONS",
    "license: Proprietary",
)


def _load_manifest(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"missing provenance manifest: {path}") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid provenance manifest {path}: {exc}") from exc
    if payload.get("schema_version") != 1:
        raise ValueError("skill provenance schema_version must be 1")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ValueError("skill provenance entries must be a list")
    return entries


def _builtin_source_names(skills_root: Path) -> set[str]:
    return {
        skill_file.parent.name
        for skill_file in skills_root.glob("*/SKILL.md")
        if skill_file.is_file()
    }


def _text_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            yield path


def validate_manifest(manifest: Path, skills_root: Path) -> list[str]:
    errors: list[str] = []
    try:
        entries = _load_manifest(manifest)
    except ValueError as exc:
        return [str(exc)]

    by_source: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(f"entry {index} is not an object")
            continue
        missing = sorted(REQUIRED_FIELDS - entry.keys())
        if missing:
            errors.append(f"entry {index} missing fields: {', '.join(missing)}")
            continue
        source_name = str(entry["source_name"]).strip()
        if not source_name:
            errors.append(f"entry {index} has an empty source_name")
            continue
        if source_name in by_source:
            errors.append(f"duplicate provenance entry: {source_name}")
            continue
        by_source[source_name] = entry

        if entry["bundled"] is not True:
            errors.append(f"{source_name}: builtin entry must set bundled=true")
        if not isinstance(entry["modified"], bool):
            errors.append(f"{source_name}: modified must be boolean")
        license_name = str(entry["license"]).strip()
        if not license_name or license_name.lower() in {"unknown", "tbd", "n/a"}:
            errors.append(f"{source_name}: bundled skill license is unknown")
        status = str(entry["redistribution_status"]).strip().lower()
        if status not in VALID_REDISTRIBUTION_STATUSES:
            errors.append(f"{source_name}: invalid redistribution_status {status!r}")
        restricted = any(word in license_name.lower() for word in RESTRICTED_LICENSE_WORDS)
        if restricted and status != "allowed":
            errors.append(
                f"{source_name}: restricted license cannot be bundled with status={status}"
            )
        if restricted and status == "allowed" and not str(
            entry.get("authorization_reference") or ""
        ).strip():
            errors.append(
                f"{source_name}: restricted license requires an authorization_reference"
            )

    actual = _builtin_source_names(skills_root)
    missing_entries = sorted(actual - by_source.keys())
    stale_entries = sorted(by_source.keys() - actual)
    errors.extend(f"missing provenance for builtin skill: {name}" for name in missing_entries)
    errors.extend(f"manifest entry has no builtin Skill: {name}" for name in stale_entries)

    for path in _text_files(skills_root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for marker in FORBIDDEN_BUNDLED_MARKERS:
            if marker in text:
                errors.append(
                    f"forbidden proprietary marker {marker!r} in {path.as_posix()}"
                )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest", type=Path, default=Path("third_party/skill-provenance.json")
    )
    parser.add_argument(
        "--skills-root", type=Path, default=Path("src/lantai/agents/skills")
    )
    args = parser.parse_args()
    errors = validate_manifest(args.manifest, args.skills_root)
    if errors:
        print("Builtin Skill license gate failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Builtin Skill license gate passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
