# -*- coding: utf-8 -*-
"""Bind enabled Skill executions to the exact workspace content selected.

This helper intentionally lives at the tool boundary.  The protected Skill
manager keeps its established routing behavior, while tools that execute or
expose resources can attach a reproducible identity to their results.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path

from ..skills_manager import (
    get_workspace_skills_dir,
    read_skill_manifest,
    resolve_effective_skills,
)


_IGNORED_SKILL_ARTIFACTS = frozenset(
    {"__pycache__", "__MACOSX", ".DS_Store", "Thumbs.db", "desktop.ini"},
)


@dataclass(frozen=True)
class ResolvedSkillIdentity:
    """Identity of one enabled workspace Skill selected by host routing."""

    name: str
    source: str
    resolution_priority: int
    content_sha256: str
    version_text: str = ""


def _skill_tree_fingerprint(skill_dir: Path) -> str:
    digest = hashlib.sha256()
    if not skill_dir.is_dir():
        return ""
    for path in sorted(skill_dir.rglob("*")):
        relative = path.relative_to(skill_dir)
        if any(part in _IGNORED_SKILL_ARTIFACTS for part in relative.parts):
            continue
        if not path.is_file() or path.is_symlink():
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            return ""
        digest.update(b"\0")
    return digest.hexdigest()


def resolve_effective_skill_identities(
    workspace_dir: Path,
    channel_name: str,
) -> list[ResolvedSkillIdentity]:
    """Return enabled Skills with deterministic source and content identity."""

    workspace_dir = Path(workspace_dir).expanduser()
    enabled = set(resolve_effective_skills(workspace_dir, channel_name))
    manifest = read_skill_manifest(workspace_dir)
    skills_dir = get_workspace_skills_dir(workspace_dir)
    identities: list[ResolvedSkillIdentity] = []
    for skill_name in sorted(enabled):
        entry = manifest.get("skills", {}).get(skill_name) or {}
        source = str(entry.get("source") or "customized").strip()
        metadata = entry.get("metadata") or {}
        version_text = str(
            metadata.get("version_text") or entry.get("version_text") or "",
        )
        identities.append(
            ResolvedSkillIdentity(
                name=skill_name,
                source=source,
                resolution_priority=100 if source == "builtin" else 200,
                content_sha256=_skill_tree_fingerprint(
                    skills_dir / skill_name,
                ),
                version_text=version_text,
            ),
        )
    return identities
