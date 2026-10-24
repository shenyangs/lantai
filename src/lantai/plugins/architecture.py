# -*- coding: utf-8 -*-
"""Plugin architecture definitions."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from pathlib import Path


ALLOWED_PLUGIN_PERMISSIONS = frozenset(
    {
        "runtime:provider",
        "runtime:lifecycle",
        "runtime:command",
        "runtime:helpers",
        "runtime:tool:read",
        "runtime:tool:write",
        "runtime:event:read",
        "config:read",
        "config:write",
        "ui:route",
        "ui:tool-renderer",
        "api:read",
        "api:write",
    },
)

RESERVED_PLUGIN_TOOL_METADATA = frozenset(
    {
        "runtime_owned",
        "progressive_tool",
        "progressive_tool_loader",
        "progressive_timeout_seconds",
        "progress_policy",
        "deliverable_contract",
        "official_integration",
        "trust_tool_annotations",
        "chrome_devtools_effect",
    },
)


@dataclass
class PluginEntryPoints:
    """Plugin entry points for frontend and backend."""

    frontend: Optional[str] = None
    backend: Optional[str] = None


@dataclass
class PluginManifest:
    """Plugin manifest definition."""

    id: str
    name: str
    version: str
    description: str = ""
    author: str = ""
    entry: PluginEntryPoints = field(default_factory=PluginEntryPoints)
    dependencies: List[str] = field(default_factory=list)
    permissions: List[str] = field(default_factory=list)
    permissions_declared: bool = False
    min_version: str = "0.1.0"
    meta: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PluginManifest":
        """Create manifest from dictionary.

        Args:
            data: Manifest data dictionary

        Returns:
            PluginManifest instance
        """
        entry_data = data.get("entry", {})
        legacy_entry_point = data.get("entry_point", "plugin.py")
        entry = PluginEntryPoints(
            frontend=entry_data.get("frontend"),
            backend=entry_data.get("backend") or legacy_entry_point,
        )

        raw_permissions = data.get("permissions", [])
        if not isinstance(raw_permissions, list) or any(
            not isinstance(item, str) for item in raw_permissions
        ):
            raise ValueError("plugin permissions must be a list of strings")
        permissions = list(
            dict.fromkeys(item.strip() for item in raw_permissions),
        )
        unknown_permissions = sorted(
            permission
            for permission in permissions
            if permission not in ALLOWED_PLUGIN_PERMISSIONS
        )
        if unknown_permissions:
            raise ValueError(
                "unknown plugin permissions: "
                + ", ".join(unknown_permissions),
            )

        return cls(
            id=data["id"],
            name=data["name"],
            version=data["version"],
            description=data.get("description", ""),
            author=data.get("author", ""),
            entry=entry,
            dependencies=data.get("dependencies", []),
            permissions=permissions,
            permissions_declared="permissions" in data,
            min_version=data.get("min_version", "0.1.0"),
            meta=data.get("meta", {}),
        )


@dataclass
class PluginRecord:
    """Plugin record for loaded plugins."""

    manifest: PluginManifest
    source_path: Path
    enabled: bool
    instance: Optional[Any] = None
    diagnostics: List[str] = field(default_factory=list)
