# -*- coding: utf-8 -*-
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .models import Capability, Pipeline

CapabilityDistribution = Literal["developer_id", "app_store"]
CapabilityMode = Literal["enabled", "read_only", "interactive_only", "disabled"]
CapabilityGateUse = Literal["automatic", "interactive"]


class CapabilityMatrixEntry(BaseModel):
    """One machine-readable capability decision shared across app layers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    mode: CapabilityMode
    reason_code: str
    label: str
    description: str = ""


class CapabilityMatrix(BaseModel):
    """Distribution-specific capability matrix for runtime policy surfaces."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    distribution: CapabilityDistribution
    entries: dict[str, CapabilityMatrixEntry] = Field(default_factory=dict)

    def entry(self, capability_id: str) -> CapabilityMatrixEntry:
        return self.entries[capability_id]


def _normalize_distribution(value: str | None = None) -> CapabilityDistribution:
    raw = str(value or "").strip().lower().replace("-", "_")
    if raw in {"app_store", "appstore", "mas", "testflight"}:
        return "app_store"
    if raw in {"developer_id", "developerid", "official", "dev_id"}:
        return "developer_id"
    return "app_store" if is_app_store_distribution() else "developer_id"


def _entry(
    capability_id: str,
    mode: CapabilityMode,
    reason_code: str,
    label: str,
    description: str = "",
) -> CapabilityMatrixEntry:
    return CapabilityMatrixEntry(
        id=capability_id,
        mode=mode,
        reason_code=reason_code,
        label=label,
        description=description,
    )


_DEVELOPER_ID_ENTRIES: tuple[CapabilityMatrixEntry, ...] = (
    _entry("chat_runtime", "enabled", "developer_id_allowed", "Chat runtime"),
    _entry("filesystem_read", "enabled", "developer_id_allowed", "Filesystem read"),
    _entry("filesystem_write", "enabled", "developer_id_allowed", "Filesystem write"),
    _entry(
        "filesystem_external_grant",
        "enabled",
        "developer_id_allowed",
        "External file grant",
    ),
    _entry(
        "browser_read_navigation",
        "enabled",
        "developer_id_allowed",
        "Browser read and navigation",
    ),
    _entry(
        "browser_write",
        "interactive_only",
        "developer_id_browser_write_confirmation_required",
        "Browser write automation",
        "Browser writes require profile policy and confirmation when configured.",
    ),
    _entry(
        "browser_upload_download",
        "interactive_only",
        "developer_id_browser_file_grant_required",
        "Browser upload and download",
        "File transfer requires explicit grants and broker validation.",
    ),
    _entry(
        "shell_python_subprocess",
        "enabled",
        "developer_id_allowed",
        "Shell, Python, and subprocess tools",
    ),
    _entry(
        "mcp_plugin_network",
        "enabled",
        "developer_id_allowed",
        "MCP, plugin, and network tools",
    ),
    _entry(
        "background_scheduled_execution",
        "enabled",
        "developer_id_allowed",
        "Background and scheduled execution",
    ),
    _entry(
        "persistent_resume",
        "enabled",
        "developer_id_allowed",
        "Persistent resume",
    ),
)


_APP_STORE_ENTRIES: tuple[CapabilityMatrixEntry, ...] = (
    _entry("chat_runtime", "enabled", "mas_chat_runtime_allowed", "Chat runtime"),
    _entry(
        "filesystem_read",
        "read_only",
        "mas_filesystem_read_container_only",
        "Filesystem read",
        "Reads are limited to app container data or user-granted locations.",
    ),
    _entry(
        "filesystem_write",
        "interactive_only",
        "mas_filesystem_write_requires_user_grant",
        "Filesystem write",
        "Writes outside the container require an explicit user grant.",
    ),
    _entry(
        "filesystem_external_grant",
        "interactive_only",
        "mas_security_scoped_bookmark_required",
        "External file grant",
        "External file access must be restored through a security-scoped bookmark.",
    ),
    _entry(
        "browser_read_navigation",
        "read_only",
        "mas_browser_read_only",
        "Browser read and navigation",
        "MAS exposes only audited read-only browser surfaces.",
    ),
    _entry(
        "browser_write",
        "disabled",
        "mas_browser_write_disabled",
        "Browser write automation",
        "MAS disables agentic browser writes.",
    ),
    _entry(
        "browser_upload_download",
        "interactive_only",
        "mas_security_scoped_bookmark_required",
        "Browser upload and download",
        "MAS browser file transfer requires an active picker-backed security-scoped bookmark grant.",
    ),
    _entry(
        "shell_python_subprocess",
        "disabled",
        "mas_subprocess_disabled",
        "Shell, Python, and subprocess tools",
    ),
    _entry(
        "mcp_plugin_network",
        "interactive_only",
        "mas_mcp_plugin_network_limited",
        "MCP, plugin, and network tools",
        "MAS network/plugin access must stay user-visible and policy-gated.",
    ),
    _entry(
        "background_scheduled_execution",
        "interactive_only",
        "mas_background_execution_limited",
        "Background and scheduled execution",
        "MAS scheduled execution must degrade to visible, user-controlled work.",
    ),
    _entry(
        "persistent_resume",
        "interactive_only",
        "mas_persistent_resume_requires_bookmark_restore",
        "Persistent resume",
        "Persistent resume depends on restoring user grants after restart.",
    ),
)


def is_app_store_distribution() -> bool:
    raw = (
        os.getenv("LANTAI_RELEASE_CHANNEL")
        or os.getenv("LANTAI_DISTRIBUTION")
        or ""
    ).lower()
    if raw in {"app_store", "appstore", "mas"}:
        return True
    if os.getenv("LANTAI_APP_STORE", "").lower() in {"1", "true", "yes"}:
        return True
    return any("lantai_app store" == part.lower() for part in Path(__file__).parts)


def build_capability_matrix(
    *,
    distribution: str | None = None,
) -> CapabilityMatrix:
    normalized = _normalize_distribution(distribution)
    entries = _APP_STORE_ENTRIES if normalized == "app_store" else _DEVELOPER_ID_ENTRIES
    return CapabilityMatrix(
        distribution=normalized,
        entries={entry.id: entry for entry in entries},
    )


def capability_matrix_entry(
    capability_id: str,
    *,
    distribution: str | None = None,
) -> CapabilityMatrixEntry:
    return build_capability_matrix(distribution=distribution).entry(capability_id)


def capability_matrix_allows(
    capability_id: str,
    *,
    distribution: str | None = None,
    use: CapabilityGateUse = "automatic",
) -> bool:
    """Return whether a capability can be used for the requested context."""

    entry = capability_matrix_entry(capability_id, distribution=distribution)
    if entry.mode == "enabled":
        return True
    if entry.mode == "interactive_only":
        return use == "interactive"
    return False


def capability_has_gui_steps(capability: Capability) -> bool:
    return any(step.kind == "gui_prompt" for step in capability.steps)


def capability_has_browser_steps(capability: Capability) -> bool:
    return any(step.kind == "browser_prompt" for step in capability.steps)


def capability_has_interactive_steps(capability: Capability) -> bool:
    return capability_has_gui_steps(capability) or capability_has_browser_steps(capability)


def pipeline_has_gui_steps(
    pipeline: Pipeline,
    capabilities: dict[str, Capability],
) -> bool:
    for node in pipeline.nodes:
        capability = capabilities.get(node.capability_id)
        if capability is not None and capability_has_gui_steps(capability):
            return True
    return False


def pipeline_has_interactive_steps(
    pipeline: Pipeline,
    capabilities: dict[str, Capability],
) -> bool:
    for node in pipeline.nodes:
        capability = capabilities.get(node.capability_id)
        if capability is not None and capability_has_interactive_steps(capability):
            return True
    return False


def ensure_capability_allowed(capability: Capability) -> None:
    if is_app_store_distribution() and capability_has_interactive_steps(capability):
        raise PermissionError("App Store 版不支持创建或运行 GUI/浏览器自动操作能力。")
