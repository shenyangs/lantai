# -*- coding: utf-8 -*-
"""Typed prompt sections, deterministic compilation, and safe manifests."""

from __future__ import annotations

import hashlib
import json
from difflib import unified_diff
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

CacheTier = Literal["kernel", "stable", "session", "turn", "tool"]
SourceKind = Literal[
    "platform_canon",
    "platform_policy",
    "user_overlay",
    "runtime_projection",
    "skill_projection",
    "tool_projection",
]


@dataclass(frozen=True, slots=True)
class PromptSection:
    """One owned, versioned unit of model-visible system context."""

    id: str
    content: str
    owner: str
    version: str = "1"
    required: bool = False
    dynamic: bool = False
    cache_tier: CacheTier = "stable"
    authority: str = "trusted_system"
    source: str = ""
    source_kind: SourceKind = "platform_policy"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PromptBuild:
    """Compiled prompt plus a content-free observability manifest."""

    prompt: str
    manifest: dict[str, Any]
    sections: tuple[PromptSection, ...] = ()


class PromptRegistry:
    """Ordered prompt registry with strict section contracts."""

    def __init__(
        self,
        *,
        language: str = "zh",
        provider: str = "",
        model: str = "",
    ) -> None:
        self.language = language
        self.provider = provider
        self.model = model
        self._sections: list[PromptSection] = []
        self._ids: set[str] = set()

    def add(self, section: PromptSection) -> None:
        if section.id in self._ids:
            raise ValueError(f"duplicate prompt section: {section.id}")
        if not section.id.strip() or not section.owner.strip():
            raise ValueError("prompt section id and owner must be non-empty")
        self._ids.add(section.id)
        self._sections.append(section)

    def compile(
        self,
        *,
        active_tools: tuple[str, ...] | list[str] = (),
        context: dict[str, Any] | None = None,
        separator: str = "\n\n",
        token_budget: int | None = None,
    ) -> PromptBuild:
        missing = [
            section.id
            for section in self._sections
            if section.required and not section.content.strip()
        ]
        if missing:
            raise ValueError(
                "required prompt section is empty: " + ", ".join(missing)
            )

        included = [section for section in self._sections if section.content]
        prompt = separator.join(section.content for section in included)
        rows: list[dict[str, Any]] = []
        for order, section in enumerate(included):
            content_hash = hashlib.sha256(section.content.encode("utf-8")).hexdigest()
            rows.append(
                {
                    "id": section.id,
                    "order": order,
                    "owner": section.owner,
                    "version": section.version,
                    "required": section.required,
                    "dynamic": section.dynamic,
                    "cache_tier": section.cache_tier,
                    "authority": section.authority,
                    "source": section.source,
                    "source_kind": section.source_kind,
                    "sha256": content_hash,
                    "chars": len(section.content),
                    "estimated_tokens": max(1, (len(section.content) + 3) // 4),
                    "metadata": section.metadata,
                }
            )

        normalized_tools = sorted(set(active_tools))
        identity = {
            "schema_version": "1",
            "language": self.language,
            "provider": self.provider,
            "model": self.model,
            "active_tools": normalized_tools,
            "context": context or {},
            "sections": rows,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        }
        build_id = hashlib.sha256(
            json.dumps(
                identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        estimated_tokens = max(1, (len(prompt) + 3) // 4) if prompt else 0
        conflicts: list[dict[str, Any]] = []
        if token_budget is not None and estimated_tokens > token_budget:
            conflicts.append(
                {
                    "type": "token_budget_exceeded",
                    "budget": token_budget,
                    "estimated_tokens": estimated_tokens,
                }
            )
        manifest = {
            **identity,
            "prompt_build_id": build_id,
            "total_chars": len(prompt),
            "estimated_tokens": estimated_tokens,
            "token_budget": token_budget,
            "within_token_budget": (
                token_budget is None or estimated_tokens <= token_budget
            ),
            "missing_required_sections": [],
            "conflicts": conflicts,
        }
        return PromptBuild(
            prompt=prompt,
            manifest=manifest,
            sections=tuple(included),
        )


def build_workspace_template_drift_report(
    platform_dir: str | Path,
    workspace_dir: str | Path,
) -> dict[str, Any]:
    """Compare packaged templates with user overlays without mutating either."""
    platform = Path(platform_dir)
    workspace = Path(workspace_dir)
    rows: list[dict[str, Any]] = []
    names = sorted(
        {path.name for path in platform.glob("*.md")}
        | {path.name for path in workspace.glob("*.md")}
    )
    for name in names:
        packaged_path = platform / name
        overlay_path = workspace / name
        packaged = (
            packaged_path.read_text(encoding="utf-8")
            if packaged_path.is_file()
            else None
        )
        overlay = (
            overlay_path.read_text(encoding="utf-8")
            if overlay_path.is_file()
            else None
        )
        if packaged is None:
            status = "user_only"
        elif overlay is None:
            status = "platform_only"
        elif packaged == overlay:
            status = "in_sync"
        else:
            status = "user_modified"
        diff = ""
        if packaged is not None and overlay is not None and packaged != overlay:
            diff = "".join(
                unified_diff(
                    packaged.splitlines(keepends=True),
                    overlay.splitlines(keepends=True),
                    fromfile=f"platform/{name}",
                    tofile=f"workspace/{name}",
                )
            )
        rows.append(
            {
                "name": name,
                "status": status,
                "platform_sha256": (
                    hashlib.sha256(packaged.encode("utf-8")).hexdigest()
                    if packaged is not None
                    else None
                ),
                "workspace_sha256": (
                    hashlib.sha256(overlay.encode("utf-8")).hexdigest()
                    if overlay is not None
                    else None
                ),
                "diff": diff,
            }
        )
    return {"schema_version": "1", "files": rows}
