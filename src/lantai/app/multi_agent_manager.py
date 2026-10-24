# -*- coding: utf-8 -*-
"""MultiAgentManager: Manages multiple agent workspaces with lazy loading.

Provides centralized management for multiple Workspace objects,
including lazy loading, lifecycle management, and hot reloading.
"""
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Dict, Set

from lantai.exceptions import ConfigurationException
from lantai.utils.async_settlement import await_settled

from .beta_access.admission import BetaAccessAdmissionGuard
from .concurrency import get_runtime_concurrency_governor
from .workspace import Workspace
from .workspace_access import should_defer_desktop_workspace_io
from ..config.utils import load_config

logger = logging.getLogger(__name__)


class MultiAgentManager:
    """Manages multiple agent workspaces.

    Features:
    - Lazy loading: Workspaces are created only when first requested
    - Lifecycle management: Start, stop, reload workspaces
    - Thread-safe: Uses async lock for concurrent access
    - Hot reload: Reload individual workspaces without affecting others
    - Parallel startup: Multiple agents start concurrently via
      fine-grained locking (lock released during slow workspace init)
    """

    def __init__(
        self,
        *,
        beta_access_admission_guard: BetaAccessAdmissionGuard | None = None,
    ):
        """Initialize multi-agent manager."""
        self.agents: Dict[str, Workspace] = {}
        self._beta_access_admission_guard = beta_access_admission_guard
        self._lock = asyncio.Lock()
        self._pending_starts: Dict[str, asyncio.Event] = {}
        self._cleanup_tasks: Set[asyncio.Task] = set()
        self._startup_tasks: Dict[str, asyncio.Task] = {}
        self._reload_tasks: Dict[str, asyncio.Task] = {}
        self._agent_stops: Dict[str, asyncio.Task] = {}
        self._instance_stops: Dict[int, asyncio.Task] = {}
        self._retired_instances: Dict[int, tuple[Workspace, str, bool]] = {}
        self._failed_stops: Set[str] = set()
        self._stopping = False
        self._shutdown_task: asyncio.Task | None = None
        logger.debug("MultiAgentManager initialized")

    async def get_agent(self, agent_id: str) -> Workspace:
        """Load once, retaining constructor/start ownership across cancellation."""
        async with self._lock:
            if self._stopping:
                raise ConfigurationException(
                    config_key="agent", message="Agent manager is shutting down",
                )
            if agent_id in self._agent_stops or agent_id in self._failed_stops:
                raise ConfigurationException(
                    config_key="agent",
                    message=f"Agent '{agent_id}' still has unsettled stop resources",
                )
            if agent_id in self.agents:
                return self.agents[agent_id]
            if any(
                owned_agent == agent_id and final
                for _, owned_agent, final in self._retired_instances.values()
            ):
                raise ConfigurationException(
                    config_key="agent",
                    message=f"Agent '{agent_id}' still has unsettled startup resources",
                )
            task = self._startup_tasks.get(agent_id)
            if task is None:
                config = load_config()
                if agent_id not in config.agents.profiles:
                    raise ConfigurationException(
                        config_key="agent",
                        message=(
                            f"Agent '{agent_id}' not found in configuration. "
                            f"Available agents: {list(config.agents.profiles.keys())}"
                        ),
                    )
                agent_ref = config.agents.profiles[agent_id]
                if should_defer_desktop_workspace_io(agent_ref.workspace_dir):
                    raise ConfigurationException(
                        config_key="workspace_access_required",
                        message=(
                            f"Agent '{agent_id}' uses an external workspace "
                            "that must be restored from the desktop app before it can run."
                        ),
                    )
                event = asyncio.Event()
                self._pending_starts[agent_id] = event
                task = asyncio.create_task(
                    self._start_agent(agent_id, agent_ref.workspace_dir, event),
                    name=f"workspace-start:{agent_id}",
                )
                self._startup_tasks[agent_id] = task
        return await await_settled(task)

    async def _start_agent(self, agent_id, workspace_dir, event) -> Workspace:
        t0 = time.perf_counter()
        try:
            instance = await asyncio.to_thread(
                Workspace, agent_id, workspace_dir,
                beta_access_admission_guard=self._beta_access_admission_guard,
            )
            if self._stopping:
                self._retain_retired(instance, agent_id, final=True)
                await self._stop_retired(instance)
                raise ConfigurationException(
                    config_key="agent", message="Agent manager is shutting down",
                )
            try:
                await instance.start()
            except (Exception, asyncio.CancelledError) as exc:
                logger.exception("Failed to start workspace %s", agent_id)
                if getattr(instance, "_startup_cleanup_pending", False):
                    self._retain_retired(instance, agent_id, final=True)
                    raise
                if isinstance(exc, asyncio.CancelledError):
                    raise
                # Keep the established degraded configuration access when
                # startup failed but partial resources have been settled.
            instance.set_manager(self)
            async with self._lock:
                if not self._stopping:
                    self.agents[agent_id] = instance
                    logger.debug(
                        "Workspace ready: %s (%.3fs)",
                        agent_id, time.perf_counter() - t0,
                    )
                    return instance
                self._retain_retired(instance, agent_id, final=True)
            await self._stop_retired(instance)
            raise ConfigurationException(
                config_key="agent", message="Agent manager is shutting down",
            )
        finally:
            # Construction itself is inside the owned task and this finally.
            async with self._lock:
                self._pending_starts.pop(agent_id, None)
                self._startup_tasks.pop(agent_id, None)
            event.set()

    def _retain_retired(self, instance, agent_id, *, final: bool) -> None:
        self._retired_instances[id(instance)] = (instance, agent_id, final)

    async def _stop_instance(self, instance, *, final: bool) -> None:
        key = id(instance)
        task = self._instance_stops.get(key)
        if task is None:
            task = asyncio.create_task(
                instance.stop(final=final), name=f"workspace-stop:{instance.agent_id}",
            )
            self._instance_stops[key] = task
            task.add_done_callback(
                lambda finished: self._instance_stops.pop(key, None)
                if self._instance_stops.get(key) is finished else None,
            )
        await await_settled(task)

    async def _stop_retired(self, instance) -> None:
        record = self._retired_instances.get(id(instance))
        if record is None:
            return
        await self._stop_instance(instance, final=record[2])
        self._retired_instances.pop(id(instance), None)

    async def _graceful_stop_old_instance(self, old_instance, agent_id) -> None:
        """Keep the retired owner until both its runtime and stop settle."""
        self._retain_retired(old_instance, agent_id, final=False)
        has_unsettled = await old_instance.task_tracker.has_unsettled_tasks()
        if not has_unsettled:
            try:
                await self._stop_retired(old_instance)
            except Exception:
                logger.exception("Old workspace stop remains unsettled: %s", agent_id)
            return

        async def delayed_cleanup():
            completed = await old_instance.task_tracker.wait_all_done(timeout=60.0)
            if not completed:
                logger.warning("Runtime settlement timed out for old workspace %s", agent_id)
                return
            await self._stop_retired(old_instance)

        task = asyncio.create_task(delayed_cleanup(), name=f"workspace-retire:{agent_id}")
        self._cleanup_tasks.add(task)

        def cleanup_done(finished):
            self._cleanup_tasks.discard(finished)
            if not finished.cancelled() and finished.exception() is not None:
                logger.warning("Old workspace cleanup remains unsettled: %s", agent_id)

        task.add_done_callback(cleanup_done)

    async def stop_agent(self, agent_id: str) -> bool:
        async with self._lock:
            task = self._agent_stops.get(agent_id)
            if task is None:
                instance = self.agents.get(agent_id)
                if instance is None:
                    return False
                task = asyncio.create_task(self._stop_agent(agent_id, instance))
                self._agent_stops[agent_id] = task
        return await await_settled(task)

    async def _stop_agent(self, agent_id, instance) -> bool:
        try:
            # A replacement which started before this stop owns resources too.
            # Settle borrowed consumers before closing the shared owner.
            reloader = self._reload_tasks.get(agent_id)
            if reloader is not None:
                await self._join_tasks([reloader])
            await self._stop_retired_for_agent(agent_id)
            await self._stop_instance(instance, final=True)
            async with self._lock:
                if self.agents.get(agent_id) is instance:
                    self.agents.pop(agent_id)
                self._failed_stops.discard(agent_id)
            return True
        except BaseException:
            self._failed_stops.add(agent_id)
            raise
        finally:
            self._agent_stops.pop(agent_id, None)

    async def _stop_retired_for_agent(self, agent_id) -> None:
        records = [
            record for record in self._retired_instances.values()
            if record[1] == agent_id
        ]
        # Old sessions borrow reusable services from the current workspace.
        # A failed consumer stop must retain that shared owner's resources.
        for instance, _, _ in sorted(records, key=lambda record: record[2]):
            await self._stop_retired(instance)

    async def reload_agent(self, agent_id: str) -> bool:
        """Own the replacement until it is published or fully stopped."""
        async with self._lock:
            if self._stopping or agent_id in self._agent_stops or agent_id in self._failed_stops:
                return False
            old_instance = self.agents.get(agent_id)
            if old_instance is None:
                return False
            task = self._reload_tasks.get(agent_id)
            if task is None:
                task = asyncio.create_task(
                    self._reload_agent(agent_id, old_instance),
                    name=f"workspace-reload:{agent_id}",
                )
                self._reload_tasks[agent_id] = task
        return await await_settled(task)

    async def _reload_agent(self, agent_id, old_instance) -> bool:
        new_instance = None
        try:
            try:
                old_watcher = old_instance._service_manager.services.get("agent_config_watcher")
                if old_watcher is not None:
                    await old_watcher.stop()
            except Exception:
                logger.exception("Failed to stop old config watcher for %s", agent_id)
            config = load_config()
            if agent_id not in config.agents.profiles:
                return False
            agent_ref = config.agents.profiles[agent_id]
            if should_defer_desktop_workspace_io(agent_ref.workspace_dir):
                raise ConfigurationException(
                    config_key="workspace_access_required",
                    message=(
                        f"Agent '{agent_id}' uses an external workspace "
                        "that must be restored from the desktop app "
                        "before it can run."
                    ),
                )
            new_instance = await asyncio.to_thread(
                Workspace, agent_id, agent_ref.workspace_dir,
                beta_access_admission_guard=self._beta_access_admission_guard,
            )
            self._retain_retired(new_instance, agent_id, final=True)
            if (
                self._stopping or agent_id in self._agent_stops
                or agent_id in self._failed_stops
                or self.agents.get(agent_id) is not old_instance
            ):
                await self._stop_retired(new_instance)
                return False
            reusable = old_instance._service_manager.get_reusable_services()
            if reusable:
                await new_instance.set_reusable_components(reusable)
            try:
                await new_instance.start()
                new_instance.set_manager(self)
            except Exception:
                logger.exception("Failed to start replacement workspace %s", agent_id)
                try:
                    await self._stop_retired(new_instance)
                except Exception:
                    logger.exception("Replacement cleanup remains unsettled: %s", agent_id)
                return False
            async with self._lock:
                if (
                    not self._stopping and agent_id not in self._agent_stops
                    and agent_id not in self._failed_stops
                    and self.agents.get(agent_id) is old_instance
                ):
                    claim = getattr(new_instance, "claim_reusable_components", None)
                    if claim is not None:
                        claim()
                    self.agents[agent_id] = new_instance
                    self._retired_instances.pop(id(new_instance), None)
                    self._retain_retired(old_instance, agent_id, final=False)
                    published = True
                else:
                    published = False
            if not published:
                await self._stop_retired(new_instance)
                return False
            await self._graceful_stop_old_instance(old_instance, agent_id)
            return True
        finally:
            self._reload_tasks.pop(agent_id, None)

    async def cancel_all_cleanup_tasks(self) -> None:
        """Cancel waiters while retaining every retired workspace owner."""
        tasks = list(self._cleanup_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await await_settled(asyncio.create_task(self._join_tasks(tasks)))

    @staticmethod
    async def _join_tasks(tasks):
        return await asyncio.gather(*tasks, return_exceptions=True)

    async def stop_all(self):
        """Fence new admission and settle all owned workspaces truthfully."""
        async with self._lock:
            self._stopping = True
            if self._shutdown_task is None or self._shutdown_task.done():
                self._shutdown_task = asyncio.create_task(self._stop_all(), name="workspace-shutdown")
            task = self._shutdown_task
        await await_settled(task)

    async def _stop_all(self):
        pending = set(self._startup_tasks.values()) | set(self._reload_tasks.values()) | set(self._agent_stops.values())
        if pending:
            await self._join_tasks(pending)
        await self.cancel_all_cleanup_tasks()
        failures = []
        owned_agents = list(dict.fromkeys([
            *self.agents,
            *(record[1] for record in self._retired_instances.values()),
        ]))
        for agent_id in owned_agents:
            instance = self.agents.get(agent_id)
            try:
                await self._stop_retired_for_agent(agent_id)
                if instance is not None:
                    await self._stop_instance(instance, final=True)
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                self._failed_stops.add(agent_id)
                failures.append(exc)
                logger.error("Workspace stop remains unsettled: %s", agent_id)
            else:
                if instance is not None and self.agents.get(agent_id) is instance:
                    self.agents.pop(agent_id)
                self._failed_stops.discard(agent_id)
        if failures:
            raise RuntimeError(f"Failed to settle {len(failures)} workspace(s)") from failures[0]
        logger.info("All owned workspaces stopped")

    def list_loaded_agents(self) -> list[str]:
        """List currently loaded agent IDs.

        Returns:
            list[str]: List of loaded agent IDs
        """
        return list(self.agents.keys())

    def is_agent_loaded(self, agent_id: str) -> bool:
        """Check if agent is currently loaded.

        Args:
            agent_id: Agent ID to check

        Returns:
            bool: True if agent is loaded and running
        """
        return agent_id in self.agents

    async def preload_agent(self, agent_id: str) -> bool:
        """Preload an agent instance during startup.

        Args:
            agent_id: Agent ID to preload

        Returns:
            bool: True if successfully preloaded, False if failed
        """
        try:
            await self.get_agent(agent_id)
            logger.info(f"Successfully preloaded agent: {agent_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to preload agent {agent_id}: {e}")
            return False

    async def start_all_configured_agents(self) -> dict[str, bool]:
        """Preload the active/background agents defined in configuration.

        Only agents with enabled=True are eligible. UI-only persona agents are
        still lazy-loaded on first use; agents that own background work
        (non-console channels, heartbeat, or scheduled jobs) are preloaded so
        their background services keep running.

        Agents are started truly in parallel: get_agent() only holds the
        manager lock briefly for dict checks, releasing it during the slow
        workspace initialization.

        Returns:
            dict[str, bool]: Mapping of agent_id to success status
        """
        config = load_config()
        # Filter only enabled agents
        enabled_agents = {
            agent_id: ref
            for agent_id, ref in config.agents.profiles.items()
            if getattr(ref, "enabled", True)
        }
        active_agent = getattr(config.agents, "active_agent", "default")
        if active_agent not in enabled_agents:
            active_agent = (
                "default"
                if "default" in enabled_agents
                else next(iter(enabled_agents), "")
            )

        agent_ids = [
            agent_id
            for agent_id, ref in enabled_agents.items()
            if (
                agent_id == active_agent
                and not should_defer_desktop_workspace_io(ref.workspace_dir)
            )
            or self._agent_needs_background_preload(ref.workspace_dir)
        ]

        if not enabled_agents:
            logger.warning("No enabled agents configured in config")
            return {}
        if not agent_ids:
            logger.warning("No agents selected for startup preload")
            return {}

        total_agents = len(config.agents.profiles)
        disabled_count = total_agents - len(enabled_agents)
        deferred_count = len(enabled_agents) - len(agent_ids)
        logger.debug(
            f"Preloading {len(agent_ids)} agent(s) "
            f"({deferred_count} lazy, {disabled_count} disabled)",
        )

        async def start_single_agent(agent_id: str) -> tuple[str, bool]:
            """Start a single agent with error handling."""
            try:
                logger.debug(f"Starting agent: {agent_id}")
                async with get_runtime_concurrency_governor().slot(
                    category="background",
                    owner_key=f"agent-start:{agent_id}",
                ):
                    await self.get_agent(agent_id)
                logger.debug(f"Agent started successfully: {agent_id}")
                return (agent_id, True)
            except Exception as e:
                logger.error(
                    f"Failed to start agent {agent_id}: {e}. "
                    f"Continuing with other agents...",
                )
                return (agent_id, False)

        # Truly parallel: get_agent releases lock during workspace startup
        results = await asyncio.gather(
            *[start_single_agent(agent_id) for agent_id in agent_ids],
            return_exceptions=False,
        )

        # Build result mapping
        result_map = dict(results)
        success_count = sum(1 for success in result_map.values() if success)
        logger.info(
            f"Agent startup complete: {success_count}/{len(agent_ids)} "
            f"agents preloaded successfully, {deferred_count} lazy, "
            f"{disabled_count} disabled",
        )

        return result_map

    @staticmethod
    def _agent_needs_background_preload(workspace_dir: str) -> bool:
        """Return true when an agent owns work that must start at boot."""

        if should_defer_desktop_workspace_io(workspace_dir):
            return False

        workspace = Path(workspace_dir).expanduser()
        agent_json = workspace / "agent.json"
        jobs_json = workspace / "jobs.json"

        try:
            if jobs_json.is_file():
                with open(jobs_json, "r", encoding="utf-8") as fh:
                    jobs_data = json.load(fh)
                jobs = jobs_data.get("jobs", [])
                if isinstance(jobs, list) and any(
                    isinstance(job, dict)
                    and job.get("enabled", True)
                    and job.get("status", "active").lower() != "disabled"
                    for job in jobs
                ):
                    return True
        except Exception:
            logger.debug(
                "Failed to inspect cron jobs for startup preload: %s",
                jobs_json,
                exc_info=True,
            )
            return True

        try:
            if not agent_json.is_file():
                return False
            with open(agent_json, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            logger.debug(
                "Failed to inspect agent config for startup preload: %s",
                agent_json,
                exc_info=True,
            )
            return True

        heartbeat = data.get("heartbeat")
        if isinstance(heartbeat, dict) and heartbeat.get("enabled"):
            return True

        channels = data.get("channels")
        if isinstance(channels, dict):
            for channel_key, channel_config in channels.items():
                if channel_key == "console":
                    continue
                if (
                    isinstance(channel_config, dict)
                    and channel_config.get("enabled")
                ):
                    return True

        return False

    def __repr__(self) -> str:
        """String representation of manager."""
        loaded = list(self.agents.keys())
        return f"MultiAgentManager(loaded_agents={loaded})"
