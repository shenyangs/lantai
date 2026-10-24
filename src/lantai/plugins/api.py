# -*- coding: utf-8 -*-
"""Plugin API for plugin developers."""

from typing import Any, Callable, Dict, Iterable, Type
import logging
import re

from ..runtime.tools import ToolSpec
from .architecture import RESERVED_PLUGIN_TOOL_METADATA

logger = logging.getLogger(__name__)


class PluginApi:
    """Plugin API - Interface for plugin developers.

    This class provides the API that plugins use to register their
    capabilities.
    """

    def __init__(
        self,
        plugin_id: str,
        config: Dict[str, Any],
        manifest: Dict[str, Any] = None,
    ):
        """Initialize plugin API.

        Args:
            plugin_id: Unique plugin identifier
            config: Plugin configuration dictionary
            manifest: Plugin manifest dictionary (from plugin.json)
        """
        self.plugin_id = plugin_id
        self.config = config
        self.manifest = manifest or {}
        self.permissions = frozenset(self.manifest.get("permissions", []))
        self._registry = None

    def _require_permission(self, permission: str) -> None:
        if permission not in self.permissions:
            raise PermissionError(
                f"Plugin '{self.plugin_id}' requires permission "
                f"'{permission}'",
            )

    def set_registry(self, registry):
        """Set registry reference (called by loader).

        Args:
            registry: PluginRegistry instance
        """
        self._registry = registry

    def register_provider(
        self,
        provider_id: str,
        provider_class: Type,
        label: str = "",
        base_url: str = "",
        **metadata,
    ):
        """Register a custom LLM Provider.

        Args:
            provider_id: Unique provider identifier
            provider_class: Provider class (inherits from BaseProvider)
            label: Display name for the provider
            base_url: API base URL
            **metadata: Additional metadata (chat_model, require_api_key, etc.)

        Example:
            >>> api.register_provider(
            ...     provider_id="my-provider",
            ...     provider_class=MyProvider,
            ...     label="My Custom Provider",
            ...     base_url="https://api.example.com/v1",
            ...     chat_model="OpenAIChatModel",
            ...     require_api_key=True,
            ... )
        """
        self._require_permission("runtime:provider")
        if self._registry:
            # Merge plugin manifest meta with provider metadata
            merged_metadata = dict(metadata)
            if "meta" in self.manifest:
                merged_metadata["meta"] = self.manifest["meta"]

            self._registry.register_provider(
                plugin_id=self.plugin_id,
                provider_id=provider_id,
                provider_class=provider_class,
                label=label or provider_id,
                base_url=base_url,
                metadata=merged_metadata,
            )
            logger.info(
                f"Plugin '{self.plugin_id}' registered provider "
                f"'{provider_id}'",
            )

    def register_startup_hook(
        self,
        hook_name: str,
        callback: Callable,
        priority: int = 100,
    ):
        """Register a startup hook.

        Args:
            hook_name: Unique hook identifier
            callback: Async or sync function to call on startup
            priority: Execution priority (lower = earlier, default=100)

        Example:
            >>> api.register_startup_hook(
            ...     hook_name="init_sdk",
            ...     callback=self.on_startup,
            ...     priority=0,  # Execute first
            ... )
        """
        self._require_permission("runtime:lifecycle")
        if self._registry:
            self._registry.register_startup_hook(
                plugin_id=self.plugin_id,
                hook_name=hook_name,
                callback=callback,
                priority=priority,
            )
            logger.info(
                f"Plugin '{self.plugin_id}' registered startup hook "
                f"'{hook_name}' (priority={priority})",
            )

    def register_shutdown_hook(
        self,
        hook_name: str,
        callback: Callable,
        priority: int = 100,
    ):
        """Register a shutdown hook.

        Args:
            hook_name: Unique hook identifier
            callback: Async or sync function to call on shutdown
            priority: Execution priority (lower = earlier, default=100)

        Example:
            >>> api.register_shutdown_hook(
            ...     hook_name="cleanup_sdk",
            ...     callback=self.on_shutdown,
            ...     priority=100,
            ... )
        """
        self._require_permission("runtime:lifecycle")
        if self._registry:
            self._registry.register_shutdown_hook(
                plugin_id=self.plugin_id,
                hook_name=hook_name,
                callback=callback,
                priority=priority,
            )
            logger.info(
                f"Plugin '{self.plugin_id}' registered shutdown hook "
                f"'{hook_name}' (priority={priority})",
            )

    def register_control_command(
        self,
        handler: Any,
        priority_level: int = 10,
    ):
        """Register a control command handler.

        Args:
            handler: Control command handler instance
                (BaseControlCommandHandler)
            priority_level: Command priority (default: 10 = high)
        """
        self._require_permission("runtime:command")
        if self._registry:
            self._registry.register_control_command(
                plugin_id=self.plugin_id,
                handler=handler,
                priority_level=priority_level,
            )
            logger.info(
                f"Plugin '{self.plugin_id}' registered control command "
                f"'{handler.command_name}' (priority={priority_level})",
            )

    def register_tool(
        self,
        spec: ToolSpec,
        executor: Callable[..., Any],
    ) -> None:
        """Register a tool that remains subject to Lantai Runtime policy."""

        if not isinstance(spec, ToolSpec):
            raise TypeError("Plugin tool spec must be a ToolSpec")
        if not callable(executor):
            raise TypeError("Plugin tool executor must be callable")
        if re.fullmatch(r"[a-z][a-z0-9_]{1,63}", spec.name) is None:
            raise ValueError(
                "Plugin tool names must use 2-64 lowercase letters, "
                "numbers, or underscores",
            )
        namespace = re.sub(r"[^a-z0-9]+", "_", self.plugin_id.lower()).strip(
            "_",
        )
        if not namespace or not spec.name.startswith(f"{namespace}_"):
            raise ValueError(
                f"Plugin tool names must use the '{namespace}_' namespace",
            )
        from ..agents.tool_registry import authoritative_reserved_tool_names

        if spec.name in authoritative_reserved_tool_names():
            raise ValueError(
                f"Plugin tool name '{spec.name}' is reserved by "
                "Lantai Runtime",
            )
        reserved_metadata = sorted(
            key
            for key in spec.metadata
            if key in RESERVED_PLUGIN_TOOL_METADATA
            or key.startswith("_native_")
        )
        if reserved_metadata:
            raise ValueError(
                "Plugin tool metadata uses Runtime-owned fields: "
                + ", ".join(reserved_metadata),
            )
        effectful = bool(spec.destructive or not spec.read_only)
        self._require_permission(
            "runtime:tool:write" if effectful else "runtime:tool:read",
        )
        if self._registry:
            self._registry.register_tool(
                plugin_id=self.plugin_id,
                spec=spec,
                executor=executor,
            )

    def register_runtime_event_observer(
        self,
        observer_name: str,
        callback: Callable[..., Any],
        *,
        event_types: Iterable[str] = (),
        priority: int = 100,
    ) -> None:
        """Observe sanitized events without intercepting Runtime behavior."""

        self._require_permission("runtime:event:read")
        if not callable(callback):
            raise TypeError("Runtime event observer must be callable")
        normalized_types = frozenset(
            event_type.strip()
            for event_type in event_types
            if isinstance(event_type, str) and event_type.strip()
        )
        if self._registry:
            self._registry.register_runtime_event_observer(
                plugin_id=self.plugin_id,
                observer_name=observer_name,
                callback=callback,
                event_types=normalized_types,
                priority=priority,
            )

    @property
    def runtime(self):
        """Access runtime helper functions.

        Returns:
            RuntimeHelpers instance or None
        """
        self._require_permission("runtime:helpers")
        if self._registry:
            return self._registry.get_runtime_helpers()
        return None

    def get_tool_config(self, tool_name: str, agent_id: str) -> dict:
        """Get tool configuration from registry.

        Args:
            tool_name: Tool function name
            agent_id: Agent identifier

        Returns:
            Tool configuration dictionary (empty if not configured)
        """
        self._require_permission("config:read")
        if self._registry:
            config = self._registry.get_tool_config(tool_name, agent_id)
            return config if config else {}
        return {}

    def set_tool_config(
        self,
        tool_name: str,
        agent_id: str,
        config: dict,
    ) -> None:
        """Save tool configuration to registry.

        Args:
            tool_name: Tool function name
            agent_id: Agent identifier
            config: Configuration dictionary
        """
        self._require_permission("config:write")
        if self._registry:
            self._registry.set_tool_config(tool_name, agent_id, config)
