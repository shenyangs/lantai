# -*- coding: utf-8 -*-
"""Versioned Prompt Bundle staging, rollout, diff and rollback governance."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .prompt_system import PromptBuild, PromptSection

_PLATFORM_SOURCE_KINDS = {"platform_canon", "platform_policy"}
_ROLLOUTS = {"candidate", "canary", "stable"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


class PromptBundleRegistry:
    """Persist platform prompt versions without copying mutable user overlays."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.builds_dir = self.root / "builds"
        self.state_path = self.root / "state.json"

    def _build_path(self, build_id: str) -> Path:
        return self.builds_dir / f"{build_id}.json"

    def _load_build(self, build_id: str) -> dict[str, Any]:
        path = self._build_path(build_id)
        if not path.is_file():
            raise FileNotFoundError(f"unknown prompt build id: {build_id}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("prompt_build_id") != build_id:
            raise ValueError("prompt bundle build id mismatch")
        return payload

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {
                "schema_version": "1",
                "active_build_id": None,
                "previous_build_id": None,
                "rollout": "candidate",
                "history": [],
            }
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def stage(
        self,
        build: PromptBuild,
        sections: Iterable[PromptSection] | None = None,
        *,
        gate_evidence: dict[str, Any],
    ) -> Path:
        """Stage an immutable candidate only after its gate passed."""
        if gate_evidence.get("status") != "pass":
            raise ValueError("prompt bundle requires passed gate evidence")
        build_id = str(build.manifest.get("prompt_build_id") or "")
        if not build_id:
            raise ValueError("prompt bundle is missing prompt_build_id")
        selected_sections = tuple(sections) if sections is not None else build.sections
        manifest_rows = {
            str(row.get("id")): row for row in build.manifest.get("sections", [])
        }
        for section in selected_sections:
            row = manifest_rows.get(section.id)
            if row is None:
                raise ValueError(f"prompt bundle section missing from manifest: {section.id}")
            if row.get("sha256") != hashlib.sha256(
                section.content.encode("utf-8")
            ).hexdigest():
                raise ValueError(f"prompt bundle section hash mismatch: {section.id}")
        platform_sections = [
            {
                "id": section.id,
                "owner": section.owner,
                "version": section.version,
                "source_kind": section.source_kind,
                "content": section.content,
            }
            for section in selected_sections
            if section.source_kind in _PLATFORM_SOURCE_KINDS
        ]
        payload = {
            "schema_version": "1",
            "prompt_build_id": build_id,
            "staged_at": _now(),
            "gate": {
                "status": "pass",
                "evidence_id": str(gate_evidence.get("evidence_id") or ""),
            },
            "manifest": build.manifest,
            "platform_sections": platform_sections,
            "user_overlay_persisted": False,
        }
        path = self._build_path(build_id)
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing.get("manifest") != build.manifest:
                raise ValueError("immutable prompt build id already exists")
            return path
        _write_private_json(path, payload)
        return path

    def promote(
        self,
        build_id: str,
        *,
        rollout: str,
        gate_evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Promote a passed candidate to candidate/canary/stable."""
        if rollout not in _ROLLOUTS:
            raise ValueError(f"unsupported prompt rollout: {rollout}")
        bundle = self._load_build(build_id)
        if bundle.get("gate", {}).get("status") != "pass":
            raise ValueError("cannot promote a failed prompt bundle")
        release_gate: dict[str, str] | None = None
        if rollout == "stable":
            evidence = gate_evidence or {}
            evidence_id = str(evidence.get("evidence_id") or "").strip()
            if evidence.get("status") != "pass" or not evidence_id:
                raise ValueError(
                    "stable promotion requires fresh passed release gate evidence"
                )
            release_gate = {"status": "pass", "evidence_id": evidence_id}
        state = self._load_state()
        active = state.get("active_build_id")
        if active == build_id:
            # A canary -> stable rollout change for identical immutable content
            # is not a build transition. Preserve the last distinct build (and
            # repair older self-referential state) so rollback metadata remains
            # truthful.
            previous = state.get("previous_build_id")
            if previous == build_id:
                previous = None
        else:
            previous = active
        state.update(
            {
                "active_build_id": build_id,
                "previous_build_id": previous,
                "rollout": rollout,
            }
        )
        state.setdefault("history", []).append(
            {
                "action": "promote",
                "build_id": build_id,
                "previous_build_id": previous,
                "rollout": rollout,
                "at": _now(),
                "gate_evidence_id": (
                    release_gate["evidence_id"] if release_gate else None
                ),
            }
        )
        if release_gate is not None:
            state["release_gate"] = release_gate
        _write_private_json(self.state_path, state)
        return state

    def resolve_active_platform_sections(self) -> tuple[PromptSection, ...]:
        """Load and verify the exact platform sections selected by rollout state."""
        state = self._load_state()
        build_id = str(state.get("active_build_id") or "")
        if not build_id:
            raise ValueError("prompt bundle has no active build id")
        bundle = self._load_build(build_id)
        if bundle.get("gate", {}).get("status") != "pass":
            raise ValueError("active prompt bundle has no passed gate")
        manifest_rows = {
            str(row.get("id")): row
            for row in bundle.get("manifest", {}).get("sections", [])
        }
        sections: list[PromptSection] = []
        for persisted in bundle.get("platform_sections", []):
            section_id = str(persisted.get("id") or "")
            row = manifest_rows.get(section_id)
            if row is None:
                raise ValueError(
                    f"active prompt bundle section missing from manifest: {section_id}"
                )
            content = str(persisted.get("content") or "")
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if digest != row.get("sha256"):
                raise ValueError(
                    f"active prompt bundle section hash mismatch: {section_id}"
                )
            source_kind = str(persisted.get("source_kind") or "")
            if source_kind not in _PLATFORM_SOURCE_KINDS:
                raise ValueError(
                    f"active prompt bundle contains non-platform section: {section_id}"
                )
            sections.append(
                PromptSection(
                    id=section_id,
                    content=content,
                    owner=str(persisted.get("owner") or row.get("owner") or "platform"),
                    version=str(persisted.get("version") or row.get("version") or "1"),
                    required=bool(row.get("required")),
                    dynamic=bool(row.get("dynamic")),
                    cache_tier=row.get("cache_tier") or "stable",
                    authority=str(row.get("authority") or "trusted_system"),
                    source=str(row.get("source") or ""),
                    source_kind=source_kind,
                    metadata=dict(row.get("metadata") or {}),
                )
            )
        if not sections:
            raise ValueError("active prompt bundle has no platform sections")
        return tuple(sections)

    def rollback(self, build_id: str) -> dict[str, Any]:
        """Select an exact earlier passed build id and retain audit history."""
        self._load_build(build_id)
        state = self._load_state()
        previous = state.get("active_build_id")
        state.update(
            {
                "active_build_id": build_id,
                "previous_build_id": previous,
                "rollout": "stable",
            }
        )
        state.setdefault("history", []).append(
            {
                "action": "rollback",
                "build_id": build_id,
                "previous_build_id": previous,
                "at": _now(),
            }
        )
        _write_private_json(self.state_path, state)
        return state

    def diff(self, left_build_id: str, right_build_id: str) -> dict[str, Any]:
        """Compare manifests without exposing prompt content."""
        left = self._load_build(left_build_id)["manifest"]
        right = self._load_build(right_build_id)["manifest"]
        left_rows = {row["id"]: row for row in left.get("sections", [])}
        right_rows = {row["id"]: row for row in right.get("sections", [])}
        added = sorted(set(right_rows) - set(left_rows))
        removed = sorted(set(left_rows) - set(right_rows))
        changed = [
            {
                "id": section_id,
                "left_sha256": left_rows[section_id].get("sha256"),
                "right_sha256": right_rows[section_id].get("sha256"),
                "left_chars": left_rows[section_id].get("chars"),
                "right_chars": right_rows[section_id].get("chars"),
            }
            for section_id in sorted(set(left_rows) & set(right_rows))
            if left_rows[section_id].get("sha256")
            != right_rows[section_id].get("sha256")
        ]
        return {
            "schema_version": "1",
            "left_build_id": left_build_id,
            "right_build_id": right_build_id,
            "added": added,
            "removed": removed,
            "changed": changed,
        }


__all__ = ["PromptBundleRegistry"]
