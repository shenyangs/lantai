# -*- coding: utf-8 -*-
# pylint:disable=too-many-nested-blocks
"""Central plugin registry."""

from typing import Any, Callable, Dict, Iterable, List, Optional, Type
from dataclasses import dataclass, field
import asyncio
import copy
import inspect
import logging
import re

from ..runtime.events import RuntimeEvent
from ..runtime.effects import RuntimeEffectRecoveryCapability
from ..runtime.sanitizer import sanitize_runtime_payload
from ..runtime.tools import ToolOutputSemantics, ToolSpec
from .architecture import RESERVED_PLUGIN_TOOL_METADATA

logger = logging.getLogger(__name__)


@dataclass
class ProviderRegistration:
    """Provider registration record."""

    plugin_id: str
    provider_id: str
    provider_class: Type
    label: str
    base_url: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HookRegistration:
    """Hook registration record."""

    plugin_id: str
    hook_name: str
    callback: Callable
    priority: int = 100


@dataclass
class ControlCommandRegistration:
    """Control command registration record."""

    plugin_id: str
    handler: Any  # BaseControlCommandHandler
    priority_level: int = 10


@dataclass
class ToolRegistration:
    """One plugin tool that must still enter the Runtime policy chain."""

    plugin_id: str
    spec: ToolSpec
    executor: Callable[..., Any]


@dataclass
class RuntimeEventObserverRegistration:
    """Read-only observer for sanitized Runtime events."""

    plugin_id: str
    observer_name: str
    callback: Callable[..., Any]
    event_types: frozenset[str] = frozenset()
    priority: int = 100


class PluginRegistry:  # pylint: disable=too-many-public-methods
    """Central plugin registry (Singleton).

    This registry manages all plugin registrations and provides
    a centralized way to access plugin capabilities.
    """

    _instance = None
    MAX_PENDING_RUNTIME_EVENT_TASKS = 256

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        # Initialize _initialized first to avoid pylint error
        if not hasattr(self, "_initialized"):
            self._initialized = False

        if self._initialized:
            return

        self._providers: Dict[str, ProviderRegistration] = {}
        self._startup_hooks: List[HookRegistration] = []
        self._shutdown_hooks: List[HookRegistration] = []
        self._control_commands: List[ControlCommandRegistration] = []
        self._tools: Dict[str, ToolRegistration] = {}
        self._runtime_event_observers: List[
            RuntimeEventObserverRegistration
        ] = []
        self._runtime_event_tasks_by_plugin: Dict[
            str,
            set[asyncio.Task[None]],
        ] = {}
        self._runtime_event_tails_by_plugin: Dict[
            str,
            asyncio.Task[None],
        ] = {}
        self._closed_runtime_event_plugins: set[str] = set()
        self._quarantined_runtime_event_plugins: set[str] = set()
        self._runtime_event_drop_count = 0
        self._runtime_helpers = None
        self._plugin_manifests: Dict[str, Dict[str, Any]] = {}

        self._initialized = True

    def register_provider(
        self,
        plugin_id: str,
        provider_id: str,
        provider_class: Type,
        label: str,
        base_url: str,
        metadata: Dict[str, Any],
    ):
        """Register a provider.

        Args:
            plugin_id: Plugin identifier
            provider_id: Provider identifier
            provider_class: Provider class
            label: Display label
            base_url: API base URL
            metadata: Additional metadata

        Raises:
            ValueError: If provider_id already registered
        """
        if provider_id in self._providers:
            existing = self._providers[provider_id]
            raise ValueError(
                f"Provider '{provider_id}' already registered "
                f"by plugin '{existing.plugin_id}'",
            )

        self._providers[provider_id] = ProviderRegistration(
            plugin_id=plugin_id,
            provider_id=provider_id,
            provider_class=provider_class,
            label=label,
            base_url=base_url,
            metadata=metadata,
        )
        logger.info(
            f"Registered provider '{provider_id}' from plugin '{plugin_id}'",
        )

    def get_provider(self, provider_id: str) -> Optional[ProviderRegistration]:
        """Get provider registration.

        Args:
            provider_id: Provider identifier

        Returns:
            ProviderRegistration or None if not found
        """
        return self._providers.get(provider_id)

    def get_all_providers(self) -> Dict[str, ProviderRegistration]:
        """Get all provider registrations.

        Returns:
            Dictionary of provider_id -> ProviderRegistration
        """
        return self._providers.copy()

    def set_runtime_helpers(self, helpers):
        """Set runtime helpers.

        Args:
            helpers: RuntimeHelpers instance
        """
        self._runtime_helpers = helpers

    def get_runtime_helpers(self):
        """Get runtime helpers.

        Returns:
            RuntimeHelpers instance or None
        """
        return self._runtime_helpers

    def register_startup_hook(
        self,
        plugin_id: str,
        hook_name: str,
        callback: Callable,
        priority: int = 100,
    ):
        """Register a startup hook.

        Args:
            plugin_id: Plugin identifier
            hook_name: Hook name
            callback: Callback function
            priority: Priority (lower = earlier execution)
        """
        hook = HookRegistration(
            plugin_id=plugin_id,
            hook_name=hook_name,
            callback=callback,
            priority=priority,
        )
        self._startup_hooks.append(hook)
        # Sort by priority (lower = earlier)
        self._startup_hooks.sort(key=lambda h: h.priority)
        logger.info(
            f"Registered startup hook '{hook_name}' from plugin '{plugin_id}' "
            f"(priority={priority})",
        )

    def register_shutdown_hook(
        self,
        plugin_id: str,
        hook_name: str,
        callback: Callable,
        priority: int = 100,
    ):
        """Register a shutdown hook.

        Args:
            plugin_id: Plugin identifier
            hook_name: Hook name
            callback: Callback function
            priority: Priority (lower = earlier execution)
        """
        hook = HookRegistration(
            plugin_id=plugin_id,
            hook_name=hook_name,
            callback=callback,
            priority=priority,
        )
        self._shutdown_hooks.append(hook)
        # Sort by priority (lower = earlier)
        self._shutdown_hooks.sort(key=lambda h: h.priority)
        logger.info(
            f"Registered shutdown hook '{hook_name}' from plugin "
            f"'{plugin_id}' (priority={priority})",
        )

    def get_startup_hooks(self) -> List[HookRegistration]:
        """Get all startup hooks sorted by priority.

        Returns:
            List of HookRegistration
        """
        return self._startup_hooks.copy()

    def get_shutdown_hooks(self) -> List[HookRegistration]:
        """Get all shutdown hooks sorted by priority.

        Returns:
            List of HookRegistration
        """
        return self._shutdown_hooks.copy()

    def register_control_command(
        self,
        plugin_id: str,
        handler: Any,
        priority_level: int = 10,
    ):
        """Register a control command handler.

        Args:
            plugin_id: Plugin identifier
            handler: Control command handler instance
            priority_level: Command priority (default: 10 = high)
        """
        cmd_reg = ControlCommandRegistration(
            plugin_id=plugin_id,
            handler=handler,
            priority_level=priority_level,
        )
        self._control_commands.append(cmd_reg)
        logger.info(
            f"Registered control command '{handler.command_name}' "
            f"from plugin '{plugin_id}' (priority={priority_level})",
        )

    def get_control_commands(self) -> List[ControlCommandRegistration]:
        """Get all registered control command handlers.

        Returns:
            List of ControlCommandRegistration
        """
        return self._control_commands.copy()

    def register_tool(
        self,
        *,
        plugin_id: str,
        spec: ToolSpec,
        executor: Callable[..., Any],
    ) -> None:
        """Register one namespaced capability without overriding another."""

        from ..agents.tool_registry import authoritative_reserved_tool_names

        namespace = re.sub(r"[^a-z0-9]+", "_", plugin_id.lower()).strip("_")
        if not namespace or not spec.name.startswith(f"{namespace}_"):
            raise ValueError(
                f"Plugin tool names must use the '{namespace}_' namespace",
            )
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

        if spec.name in self._tools:
            existing = self._tools[spec.name]
            raise ValueError(
                f"Tool '{spec.name}' already registered by plugin "
                f"'{existing.plugin_id}'",
            )
        effectful = bool(spec.destructive or not spec.read_only)
        recovery = (
            RuntimeEffectRecoveryCapability()
            if not effectful
            else RuntimeEffectRecoveryCapability(
                external_side_effect=True,
                idempotency="not_idempotent",
                probe="none",
                compensation="none",
                on_ambiguity="manual_disambiguation",
                notes=(
                    "Conservative plugin boundary: advanced recovery "
                    "capabilities require a separately audited permission."
                ),
            )
        )
        normalized = spec.model_copy(
            deep=True,
            update={
                "origin": "plugin",
                "destructive": effectful,
                "read_only": not effectful,
                "concurrency_safe": bool(
                    spec.concurrency_safe and not effectful,
                ),
                "effect_recovery": recovery,
                "output_semantics": ToolOutputSemantics(),
                "max_result_size": (
                    spec.max_result_size
                    if spec.max_result_size is not None
                    else 1_048_576
                    if spec.read_only
                    else None
                ),
            },
        )
        self._tools[spec.name] = ToolRegistration(
            plugin_id=plugin_id,
            spec=normalized,
            executor=executor,
        )

    def get_tool(self, tool_name: str) -> Optional[ToolRegistration]:
        return self._tools.get(tool_name)

    def get_all_tools(self) -> Dict[str, ToolRegistration]:
        return self._tools.copy()

    def register_runtime_event_observer(
        self,
        *,
        plugin_id: str,
        observer_name: str,
        callback: Callable[..., Any],
        event_types: Iterable[str] = (),
        priority: int = 100,
    ) -> None:
        if plugin_id in self._closed_runtime_event_plugins:
            raise ValueError(
                f"Runtime observers for plugin '{plugin_id}' are closed",
            )
        key = (plugin_id, observer_name)
        if any(
            (observer.plugin_id, observer.observer_name) == key
            for observer in self._runtime_event_observers
        ):
            raise ValueError(
                f"Runtime observer '{observer_name}' already registered "
                f"by plugin '{plugin_id}'",
            )
        registration = RuntimeEventObserverRegistration(
            plugin_id=plugin_id,
            observer_name=observer_name,
            callback=callback,
            event_types=frozenset(event_types),
            priority=priority,
        )
        self._runtime_event_observers.append(registration)
        self._runtime_event_observers.sort(key=lambda item: item.priority)

    def get_runtime_event_observers(
        self,
    ) -> List[RuntimeEventObserverRegistration]:
        return self._runtime_event_observers.copy()

    async def notify_runtime_event(self, event: RuntimeEvent) -> None:
        """Deliver isolated sanitized copies without changing Runtime state."""

        await self._notify_runtime_event_observers(
            event,
            tuple(self.get_runtime_event_observers()),
        )

    async def _notify_runtime_event_observers(
        self,
        event: RuntimeEvent,
        observers: tuple[RuntimeEventObserverRegistration, ...],
    ) -> None:
        """Deliver one already accepted event to a fixed observer snapshot."""

        safe_payload = sanitize_runtime_payload(dict(event.payload or {}))
        for observer in observers:
            if observer.event_types and event.type not in observer.event_types:
                continue
            safe_event = event.model_copy(
                deep=True,
                update={"payload": copy.deepcopy(safe_payload)},
            )
            try:
                if inspect.iscoroutinefunction(observer.callback):
                    result = await observer.callback(safe_event)
                else:
                    result = await asyncio.to_thread(
                        observer.callback,
                        safe_event,
                    )
                if inspect.isawaitable(result):
                    await result
            except Exception:  # pylint: disable=broad-except
                logger.warning(
                    "Runtime event observer '%s' from plugin '%s' failed",
                    observer.observer_name,
                    observer.plugin_id,
                    exc_info=True,
                )

    def _runtime_event_observer_groups(
        self,
    ) -> Dict[str, tuple[RuntimeEventObserverRegistration, ...]]:
        grouped: Dict[str, list[RuntimeEventObserverRegistration]] = {}
        for observer in tuple(self._runtime_event_observers):
            if observer.plugin_id not in self._closed_runtime_event_plugins:
                grouped.setdefault(observer.plugin_id, []).append(observer)
        return {
            plugin_id: tuple(observers)
            for plugin_id, observers in grouped.items()
        }

    async def _deliver_runtime_event_in_order(
        self,
        *,
        previous: asyncio.Task[None] | None,
        event: RuntimeEvent,
        observers: tuple[RuntimeEventObserverRegistration, ...],
    ) -> None:
        if previous is not None:
            try:
                await asyncio.shield(previous)
            except (asyncio.CancelledError, Exception):
                pass
        await self._notify_runtime_event_observers(event, observers)

    def _track_runtime_event_task(
        self,
        plugin_id: str,
        task: asyncio.Task[None],
    ) -> None:
        tasks = self._runtime_event_tasks_by_plugin.setdefault(
            plugin_id,
            set(),
        )
        tasks.add(task)
        self._runtime_event_tails_by_plugin[plugin_id] = task

        def settled(done: asyncio.Task[None]) -> None:
            owner_tasks = self._runtime_event_tasks_by_plugin.get(plugin_id)
            if owner_tasks is not None:
                owner_tasks.discard(done)
                if not owner_tasks:
                    self._runtime_event_tasks_by_plugin.pop(plugin_id, None)
            if self._runtime_event_tails_by_plugin.get(plugin_id) is done:
                self._runtime_event_tails_by_plugin.pop(plugin_id, None)
            try:
                done.result()
            except asyncio.CancelledError:
                return
            except Exception:  # pylint: disable=broad-except
                logger.warning(
                    "Runtime event observer delivery failed",
                    exc_info=True,
                )

        task.add_done_callback(settled)

    def _schedule_runtime_event_for_plugin(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        plugin_id: str,
        event: RuntimeEvent,
        observers: tuple[RuntimeEventObserverRegistration, ...],
    ) -> None:
        coroutine = self._deliver_runtime_event_in_order(
            previous=self._runtime_event_tails_by_plugin.get(plugin_id),
            event=event,
            observers=observers,
        )
        try:
            task = loop.create_task(
                coroutine,
                name=(
                    f"plugin-runtime-event:{plugin_id}:"
                    f"{event.type}:{event.id}"
                ),
            )
        except Exception:  # pylint: disable=broad-except
            coroutine.close()
            self._runtime_event_drop_count += 1
            logger.warning(
                "Dropped plugin Runtime event because scheduling failed",
                exc_info=True,
            )
            return
        self._track_runtime_event_task(plugin_id, task)

    def publish_runtime_event(self, event: RuntimeEvent) -> None:
        """Schedule observer delivery without delaying the Runtime stream."""

        grouped = self._runtime_event_observer_groups()
        if not grouped:
            return
        pending_count = sum(
            len(tasks)
            for tasks in self._runtime_event_tasks_by_plugin.values()
        )
        if pending_count + len(grouped) > self.MAX_PENDING_RUNTIME_EVENT_TASKS:
            self._runtime_event_drop_count += 1
            logger.warning(
                "Dropped plugin Runtime event because observers are behind",
            )
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(
                "Runtime event observers require an active event loop",
            )
            return
        try:
            event_snapshot = event.model_copy(deep=True)
        except Exception:  # pylint: disable=broad-except
            self._runtime_event_drop_count += 1
            logger.warning(
                "Dropped plugin Runtime event because snapshotting failed",
                exc_info=True,
            )
            return

        for plugin_id, plugin_observers in grouped.items():
            self._schedule_runtime_event_for_plugin(
                loop=loop,
                plugin_id=plugin_id,
                event=event_snapshot,
                observers=plugin_observers,
            )

    @property
    def runtime_event_drop_count(self) -> int:
        return self._runtime_event_drop_count

    def disable_runtime_event_observers(self, plugin_id: str) -> int:
        """Atomically close new observer admission for one plugin."""

        self._closed_runtime_event_plugins.add(plugin_id)
        before = len(self._runtime_event_observers)
        self._runtime_event_observers = [
            observer
            for observer in self._runtime_event_observers
            if observer.plugin_id != plugin_id
        ]
        return before - len(self._runtime_event_observers)

    def is_runtime_event_plugin_quarantined(self, plugin_id: str) -> bool:
        """Return whether unload timed out with callbacks still in flight."""

        return plugin_id in self._quarantined_runtime_event_plugins

    async def drain_runtime_event_observers(
        self,
        *,
        plugin_id: str | None = None,
        timeout_seconds: float = 5.0,
    ) -> bool:
        """Wait for accepted deliveries without pretending threads stopped."""

        selected = (
            {
                plugin_id: self._runtime_event_tasks_by_plugin.get(
                    plugin_id,
                    set(),
                ),
            }
            if plugin_id is not None
            else self._runtime_event_tasks_by_plugin
        )
        pending = tuple(
            task for tasks in selected.values() for task in tuple(tasks)
        )
        if pending:
            _done, still_pending = await asyncio.wait(
                pending,
                timeout=max(0.0, timeout_seconds),
            )
            if still_pending:
                quarantined = {
                    owner
                    for owner, tasks in selected.items()
                    if any(task in still_pending for task in tasks)
                }
                self._quarantined_runtime_event_plugins.update(quarantined)
                logger.error(
                    "Plugin Runtime observer drain timed out; quarantined: %s",
                    ", ".join(sorted(quarantined)),
                )
                return False
        if plugin_id is not None:
            self._quarantined_runtime_event_plugins.discard(plugin_id)
        elif not self._runtime_event_tasks_by_plugin:
            self._quarantined_runtime_event_plugins.clear()
        return True

    def register_plugin_manifest(
        self,
        plugin_id: str,
        manifest: Dict[str, Any],
    ):
        """Register plugin manifest.

        Args:
            plugin_id: Plugin identifier
            manifest: Plugin manifest dictionary
        """
        self._plugin_manifests[plugin_id] = manifest
        logger.debug(f"Registered manifest for plugin '{plugin_id}'")

    def get_all_plugin_manifests(self) -> Dict[str, Dict[str, Any]]:
        """Get all plugin manifests.

        Returns:
            Dictionary of plugin_id -> manifest
        """
        return self._plugin_manifests.copy()

    def snapshot(self) -> Dict[str, Any]:
        """Return a shallow transaction snapshot of registry state."""
        return {
            "providers": self._providers.copy(),
            "startup_hooks": self._startup_hooks.copy(),
            "shutdown_hooks": self._shutdown_hooks.copy(),
            "control_commands": self._control_commands.copy(),
            "tools": self._tools.copy(),
            "runtime_event_observers": self._runtime_event_observers.copy(),
            "closed_runtime_event_plugins": set(
                self._closed_runtime_event_plugins,
            ),
            "quarantined_runtime_event_plugins": set(
                self._quarantined_runtime_event_plugins,
            ),
            "plugin_manifests": self._plugin_manifests.copy(),
        }

    def restore(self, snapshot: Dict[str, Any]) -> None:
        """Restore registry state from a transaction snapshot."""
        self._providers = snapshot["providers"].copy()
        self._startup_hooks = snapshot["startup_hooks"].copy()
        self._shutdown_hooks = snapshot["shutdown_hooks"].copy()
        self._control_commands = snapshot["control_commands"].copy()
        self._tools = snapshot.get("tools", {}).copy()
        self._runtime_event_observers = snapshot.get(
            "runtime_event_observers",
            [],
        ).copy()
        self._closed_runtime_event_plugins = set(
            snapshot.get("closed_runtime_event_plugins", set()),
        )
        self._quarantined_runtime_event_plugins = set(
            snapshot.get("quarantined_runtime_event_plugins", set()),
        )
        self._plugin_manifests = snapshot["plugin_manifests"].copy()

    def unregister_plugin(self, plugin_id: str) -> None:
        """Remove every registration owned by one plugin."""

        if self._runtime_event_tasks_by_plugin.get(plugin_id):
            raise RuntimeError(
                f"Plugin '{plugin_id}' still has Runtime observers in flight",
            )

        self._providers = {
            provider_id: registration
            for provider_id, registration in self._providers.items()
            if registration.plugin_id != plugin_id
        }
        self._startup_hooks = [
            hook for hook in self._startup_hooks if hook.plugin_id != plugin_id
        ]
        self._shutdown_hooks = [
            hook
            for hook in self._shutdown_hooks
            if hook.plugin_id != plugin_id
        ]
        self._control_commands = [
            command
            for command in self._control_commands
            if command.plugin_id != plugin_id
        ]
        self._tools = {
            name: registration
            for name, registration in self._tools.items()
            if registration.plugin_id != plugin_id
        }
        self._runtime_event_observers = [
            observer
            for observer in self._runtime_event_observers
            if observer.plugin_id != plugin_id
        ]
        self._runtime_event_tails_by_plugin.pop(plugin_id, None)
        self._closed_runtime_event_plugins.discard(plugin_id)
        self._quarantined_runtime_event_plugins.discard(plugin_id)
        self._plugin_manifests.pop(plugin_id, None)

    def get_plugin_manifest(
        self,
        plugin_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Get plugin manifest.

        Args:
            plugin_id: Plugin identifier

        Returns:
            Plugin manifest dict or None
        """
        return self._plugin_manifests.get(plugin_id)

    def get_plugin_id_for_tool(self, tool_name: str) -> Optional[str]:
        """Get plugin ID that provides a specific tool.

        Args:
            tool_name: Tool function name

        Returns:
            Plugin ID or None
        """
        from ..agents.tool_registry import authoritative_reserved_tool_names

        if tool_name in authoritative_reserved_tool_names():
            return None
        registration = self._tools.get(tool_name)
        if registration is not None:
            return registration.plugin_id
        for plugin_id, manifest in self._plugin_manifests.items():
            meta = manifest.get("meta", {})
            if meta.get("tool_name") == tool_name:
                return plugin_id
        return None

    def get_tool_config(
        self,
        tool_name: str,
        agent_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Get tool configuration for a specific agent.

        Args:
            tool_name: Tool function name
            agent_id: Agent identifier

        Returns:
            Tool configuration dict or None
        """
        try:
            from ..config.config import load_agent_config

            agent_config = load_agent_config(agent_id)
            if (
                not agent_config.tools
                or tool_name not in agent_config.tools.builtin_tools
            ):
                return None

            tool_config = agent_config.tools.builtin_tools[tool_name]
            return tool_config.config if tool_config.config else None
        except Exception as e:
            logger.error(f"Failed to load tool config: {e}")
            return None

    def set_tool_config(
        self,
        tool_name: str,
        agent_id: str,
        config: Dict[str, Any],
    ) -> None:
        """Save tool configuration for a specific agent.

        Args:
            tool_name: Tool function name
            agent_id: Agent identifier
            config: Configuration data
        """
        try:
            from ..config.config import (
                load_agent_config,
                save_agent_config,
            )

            agent_config = load_agent_config(agent_id)
            if (
                not agent_config.tools
                or tool_name not in agent_config.tools.builtin_tools
            ):
                raise ValueError(f"Tool '{tool_name}' not found in agent")

            # Update tool config
            agent_config.tools.builtin_tools[tool_name].config = config

            # Save agent config
            save_agent_config(agent_id, agent_config)

            logger.info(
                f"Saved config for tool '{tool_name}' in agent '{agent_id}'",
            )
        except Exception as e:
            logger.error(f"Failed to save tool config: {e}")
            raise
