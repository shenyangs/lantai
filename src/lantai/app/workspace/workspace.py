# -*- coding: utf-8 -*-
"""Workspace: Encapsulates a complete independent agent runtime.

Each Workspace represents a standalone agent workspace with its own:
- Runner (request processing)
- ChannelManager (communication channels)
- BaseMemoryManager (conversation memory)
- MCPClientManager (MCP tool clients)
- CronManager (scheduled tasks)

All existing single-agent components are reused without modification.
"""
import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Optional

from lantai.config.timezone import normalize_tz
from lantai.config.utils import load_config
from lantai.utils.async_settlement import await_settled

from ..beta_access.admission import BetaAccessAdmissionGuard
from .service_manager import ServiceDescriptor, ServiceManager
from .service_factories import (
    create_mcp_service,
    create_chat_service,
    create_channel_service,
    create_agent_config_watcher,
    create_mcp_config_watcher,
)
from ..runner import AgentRunner
from ..runner.task_tracker import SessionRuntimeRegistry
from ..mcp import MCPClientManager
from ..crons.manager import CronManager
from ..crons.repo.json_repo import JsonJobRepository
from ..daily_brief.lifecycle import DailyBriefWorkspaceService
from ..reminders.lifecycle import ReminderDeliveryWorkspaceService
from ..background_semantics import BackgroundSemanticService
from ...config.config import load_agent_config

logger = logging.getLogger(__name__)

RUNTIME_WAKE_SCAN_INTERVAL_SECONDS = 30.0


class Workspace:
    """Single agent workspace with complete runtime components.

    Each Workspace is an independent agent instance with its own:
    - Runner: Processes agent requests
    - ChannelManager: Manages communication channels
    - BaseMemoryManager: Manages conversation memory
    - MCPClientManager: Manages MCP tool clients
    - CronManager: Manages scheduled tasks

    All components use existing single-agent code without modification.
    """

    def __init__(
        self,
        agent_id: str,
        workspace_dir: str,
        *,
        beta_access_admission_guard: BetaAccessAdmissionGuard | None = None,
    ):
        """Initialize agent instance.

        Args:
            agent_id: Unique agent identifier
            workspace_dir: Path to agent's workspace directory
        """
        self.agent_id = agent_id
        self.beta_access_admission_guard = beta_access_admission_guard
        self.workspace_dir = Path(workspace_dir).expanduser()
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

        # Service manager (unified component management)
        self._service_manager = ServiceManager(self)

        # Non-service state
        self._config = None  # Loaded before start()
        self._started = False
        self._manager = None  # Reference to MultiAgentManager
        self._task_tracker = SessionRuntimeRegistry()
        self._recovery_task: asyncio.Task | None = None
        self._stop_task: asyncio.Task | None = None
        self._startup_cleanup_pending = False
        self._owns_reused_services = True

        # Register all services
        self._register_services()

        logger.debug(
            f"Created Workspace: {agent_id} at {self.workspace_dir}",
        )

    # Service access via properties (delegates to ServiceManager)
    @property
    def runner(self) -> Optional[AgentRunner]:
        """Get runner instance from ServiceManager."""
        return self._service_manager.services.get("runner")

    @property
    def memory_manager(self):
        """Get memory manager instance from ServiceManager."""
        return self._service_manager.services.get("memory_manager")

    @property
    def context_manager(self):
        """Get context manager instance from ServiceManager."""
        return self._service_manager.services.get("context_manager")

    @property
    def mcp_manager(self):
        """Get MCP manager instance from ServiceManager."""
        return self._service_manager.services.get("mcp_manager")

    @property
    def chat_manager(self):
        """Get chat manager instance from ServiceManager."""
        return self._service_manager.services.get("chat_manager")

    @property
    def channel_manager(self):
        """Get channel manager instance from ServiceManager."""
        return self._service_manager.services.get("channel_manager")

    @property
    def cron_manager(self):
        """Get cron manager instance from ServiceManager."""
        return self._service_manager.services.get("cron_manager")

    @property
    def reminder_delivery(self):
        """Get reminder delivery service from ServiceManager."""
        return self._service_manager.services.get("reminder_delivery")

    @property
    def daily_brief(self):
        """Get the consent-gated isolated daily-brief lifecycle."""
        return self._service_manager.services.get("daily_brief")

    @property
    def background_semantics(self):
        """Get the durable post-answer semantic worker."""

        return self._service_manager.services.get("background_semantics")

    # Non-service state
    @property
    def task_tracker(self) -> SessionRuntimeRegistry:
        """Get task tracker for background chat and reconnect."""
        return self._task_tracker

    @property
    def config(self):
        """Get agent configuration."""
        self._config = load_agent_config(self.agent_id)
        return self._config

    def set_manager(self, manager) -> None:
        """Set reference to MultiAgentManager for /daemon restart.

        Args:
            manager: MultiAgentManager instance
        """
        self._manager = manager
        # Pass to runner for /daemon restart command
        if self.runner is not None:
            self.runner._manager = manager  # pylint: disable=protected-access

    def _register_services(  # pylint: disable=too-many-statements
        self,
    ) -> None:
        """Register all workspace services with ServiceManager.

        Uses declarative ServiceDescriptor configuration to replace
        hardcoded initialization logic.
        """
        # pylint: disable=protected-access
        from ...agents.memory.base_memory_manager import (
            get_memory_manager_backend,
        )
        from ...agents.context.base_context_manager import (
            get_context_manager_backend,
        )

        sm = self._service_manager

        # Priority 10: Runner
        sm.register(
            ServiceDescriptor(
                name="runner",
                service_class=AgentRunner,
                init_args=lambda ws: {
                    "agent_id": ws.agent_id,
                    "workspace_dir": ws.workspace_dir,
                    "task_tracker": ws._task_tracker,
                    "beta_access_admission_guard": (
                        ws.beta_access_admission_guard
                    ),
                },
                stop_method="stop",
                priority=10,
                concurrent_init=False,
            ),
        )

        # Priority 20: Core services (concurrent)
        sm.register(
            ServiceDescriptor(
                name="memory_manager",
                service_class=lambda ws: get_memory_manager_backend(
                    ws._config.running.memory_manager_backend,
                ),
                init_args=lambda ws: {
                    "working_dir": str(ws.workspace_dir),
                    "agent_id": ws.agent_id,
                },
                post_init=lambda ws, mm: setattr(
                    ws._service_manager.services["runner"],
                    "memory_manager",
                    mm,
                ),
                start_method="start",
                stop_method="close",
                reusable=True,
                priority=20,
                concurrent_init=True,
            ),
        )

        # Priority 22: durable post-answer semantic worker. It starts after
        # memory/chat creation and before the runner accepts channel traffic.
        sm.register(
            ServiceDescriptor(
                name="background_semantics",
                service_class=BackgroundSemanticService,
                init_args=lambda ws: {
                    "workspace_dir": ws.workspace_dir,
                    "agent_id": ws.agent_id,
                },
                post_init=self._configure_background_semantics,
                start_method="start",
                stop_method="stop",
                reusable=True,
                priority=22,
                concurrent_init=False,
            ),
        )

        sm.register(
            ServiceDescriptor(
                name="context_manager",
                service_class=lambda ws: get_context_manager_backend(
                    ws._config.running.context_manager_backend,
                ),
                init_args=lambda ws: {
                    "working_dir": str(ws.workspace_dir),
                    "agent_id": ws.agent_id,
                },
                post_init=lambda ws, cm: setattr(
                    ws._service_manager.services["runner"],
                    "context_manager",
                    cm,
                ),
                start_method="start",
                stop_method="close",
                reusable=True,
                priority=20,
                concurrent_init=True,
            ),
        )

        sm.register(
            ServiceDescriptor(
                name="mcp_manager",
                service_class=MCPClientManager,
                init_args=lambda ws: {"agent_id": ws.agent_id},
                post_init=create_mcp_service,
                stop_method="close_all",
                priority=20,
                concurrent_init=True,
            ),
        )

        sm.register(
            ServiceDescriptor(
                name="chat_manager",
                service_class=None,
                post_init=create_chat_service,
                reusable=True,
                priority=20,
                concurrent_init=True,
            ),
        )

        # Priority 25: Runner start
        sm.register(
            ServiceDescriptor(
                name="runner_start",
                service_class=None,
                post_init=lambda ws, _: ws._service_manager.services[
                    "runner"
                ].start(),
                priority=25,
                concurrent_init=False,
            ),
        )

        # Priority 30: Channel manager
        sm.register(
            ServiceDescriptor(
                name="channel_manager",
                service_class=None,
                post_init=create_channel_service,
                start_method="start_all",
                stop_method="stop_all",
                priority=30,
                concurrent_init=False,
            ),
        )

        # Priority 40: Cron manager
        sm.register(
            ServiceDescriptor(
                name="cron_manager",
                service_class=CronManager,
                init_args=lambda ws: {  # pylint: disable=protected-access
                    "repo": JsonJobRepository(
                        str(ws.workspace_dir / "jobs.json"),
                    ),
                    "runner": ws._service_manager.services["runner"],
                    "channel_manager": ws._service_manager.services.get(
                        "channel_manager",
                    ),
                    "timezone": normalize_tz(
                        load_config().user_timezone or "UTC",
                    )
                    or "UTC",
                    "agent_id": ws.agent_id,
                    "task_tracker": ws._task_tracker,
                },
                start_method="start",
                stop_method="stop",
                priority=40,
                concurrent_init=False,
            ),
        )

        # Priority 45: Optional reminder delivery worker
        sm.register(
            ServiceDescriptor(
                name="reminder_delivery",
                service_class=ReminderDeliveryWorkspaceService,
                init_args=lambda ws: {
                    "workspace_dir": ws.workspace_dir,
                    "agent_id": ws.agent_id,
                    "native_notifications": True,
                },
                start_method="start",
                stop_method="stop",
                priority=45,
                concurrent_init=False,
            ),
        )

        # Priority 46: Isolated daily-brief worker.  It is not the chat runner
        # or the cron agent, and remains inert without persisted consent plus
        # an active one-time cycle or daily subscription.
        sm.register(
            ServiceDescriptor(
                name="daily_brief",
                service_class=DailyBriefWorkspaceService,
                init_args=lambda ws: {
                    "workspace_dir": ws.workspace_dir,
                    "agent_id": ws.agent_id,
                    "mcp_manager": ws.mcp_manager,
                },
                start_method="start",
                stop_method="stop",
                priority=46,
                concurrent_init=False,
            ),
        )

        # Priority 50: Agent Config Watcher (conditional)
        sm.register(
            ServiceDescriptor(
                name="agent_config_watcher",
                service_class=None,
                post_init=create_agent_config_watcher,
                start_method="start",
                stop_method="stop",
                priority=50,
                concurrent_init=False,
            ),
        )

        # Priority 51: MCP Config Watcher (conditional)
        sm.register(
            ServiceDescriptor(
                name="mcp_config_watcher",
                service_class=None,
                post_init=create_mcp_config_watcher,
                start_method="start",
                stop_method="stop",
                priority=51,
                concurrent_init=False,
            ),
        )

    @staticmethod
    def _configure_background_semantics(ws, service):
        memory_manager = ws._service_manager.services.get("memory_manager")
        if memory_manager is not None:
            binder = getattr(
                memory_manager,
                "bind_background_semantic_service",
                None,
            )
            if callable(binder):
                binder(service)
            memory_handler = getattr(
                memory_manager,
                "handle_background_semantic_job",
                None,
            )
            if callable(memory_handler):
                service.register_handler("memory_capture", memory_handler)

        async def generate_title(payload):
            from ..runner.title_generator import generate_and_update_title

            await generate_and_update_title(
                workspace=ws,
                chat_id=str(payload.get("chat_id") or ""),
                user_message=str(payload.get("user_message") or ""),
                assistant_message=str(payload.get("assistant_message") or ""),
                placeholder_name=str(payload.get("placeholder_name") or ""),
            )

        service.register_handler("chat_title", generate_title)

        async def classify_artifacts(payload):
            from ..utility_intelligence import classify_text_artifacts

            response = await classify_text_artifacts(
                str(payload.get("text") or ""),
                agent_id=ws.agent_id,
            )
            return response.model_dump(mode="json")

        service.register_handler("artifact_classification", classify_artifacts)

        async def prepare_welcome_greeting(payload):
            from ..welcome_greetings import (
                WelcomeGenerationBudgetStore,
                WelcomeGenerationRequest,
                WelcomeGenerationResponse,
                generate_reviewed_welcome_candidates,
            )

            request = WelcomeGenerationRequest.model_validate(payload)
            encoded = json.dumps(
                request.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            )
            batch_key = hashlib.sha256(encoded.encode()).hexdigest()
            budget = WelcomeGenerationBudgetStore(
                service.store.path + ".welcome-budget.sqlite3",
            )
            try:
                reserved = budget.reserve(
                    ws.agent_id,
                    request.local_date.isoformat(),
                    batch_key,
                )
            finally:
                budget.close()
            if not reserved:
                return WelcomeGenerationResponse(
                    status="budget_exhausted",
                ).model_dump(mode="json")
            response = await generate_reviewed_welcome_candidates(
                request,
                agent_id=ws.agent_id,
            )
            return response.model_dump(mode="json")

        service.register_handler("welcome_greeting", prepare_welcome_greeting)
        return service

    async def set_reusable_components(self, components: dict) -> None:
        """Set components to reuse from previous instance.

        Must be called BEFORE start(). Allows reusing components that support
        hot-reload without recreating them. If a service has a reload_func,
        it will be called during this process.

        Args:
            components: Dict mapping component name to instance.
                Supported keys:
                - 'memory_manager': BaseMemoryManager instance
                - 'context_manager': BaseContextManager instance
                - 'chat_manager': ChatManager instance

        Example:
            new_ws = Workspace("default", workspace_dir)
            await new_ws.set_reusable_components({
                'memory_manager': old_ws.memory_manager,
                'chat_manager': old_ws.chat_manager,
            })
            await new_ws.start()
        """
        if self._started:
            logger.warning(
                f"Cannot set reusable components for already started "
                f"workspace: {self.agent_id}",
            )
            return

        # Delegate to ServiceManager
        for name, component in components.items():
            self._owns_reused_services = False
            await self._service_manager.set_reusable(name, component)

    def claim_reusable_components(self) -> None:
        """Commit reusable resource ownership with the manager's atomic swap."""
        self._owns_reused_services = True

    async def _stop_services(self, *, final: bool) -> None:
        if getattr(self, "_owns_reused_services", True):
            await self._service_manager.stop_all(final=final)
        else:
            await self._service_manager.stop_all(final=final, preserve_reused=True)

    async def start(self):
        """Start workspace and initialize all components."""
        if self._started:
            logger.debug(f"Workspace already started: {self.agent_id}")
            return
        if getattr(self, "_startup_cleanup_pending", False):
            raise RuntimeError("Previous workspace startup resources remain unsettled")

        logger.info(f"Starting workspace: {self.agent_id}")

        from ...agents.skills_manager import (
            ensure_skill_pool_initialized,
        )

        try:
            await await_settled(asyncio.create_task(
                asyncio.to_thread(ensure_skill_pool_initialized),
            ))
        except Exception as e:
            logger.warning(
                f"Skill pool initialization failed (non-fatal): {e}",
            )

        try:
            # 1. Load agent configuration
            self._config = await await_settled(asyncio.create_task(asyncio.to_thread(
                load_agent_config,
                self.agent_id,
            )))
            logger.debug(f"Loaded config for agent: {self.agent_id}")

            # 2. Run legacy weixin -> wechat data migrations BEFORE services
            # start so ChatManager / Runner see the canonical layout.
            await await_settled(asyncio.create_task(
                asyncio.to_thread(self._migrate_legacy_weixin_data),
            ))

            # 3. Start all services via ServiceManager
            await self._service_manager.start_all()

            self._started = True
            # Recovery is outside the interactive startup critical path. It
            # may dispatch only when the persisted run carries a current,
            # task-scoped user autonomy grant; ordinary runs remain passive.
            self._recovery_task = asyncio.create_task(
                self._run_runtime_recovery_loop(),
                name=f"runtime-recovery-loop:{self.agent_id}",
            )
            logger.info(f"Workspace started successfully: {self.agent_id}")

        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                logger.info(
                    "Workspace startup cancelled; stopping partial services: %s",
                    self.agent_id,
                )
            else:
                logger.error(
                    "Failed to start agent instance %s: %s",
                    self.agent_id,
                    exc,
                )
            # ``_started`` is assigned only after every service has started, so
            # ``stop()`` would return early here and leak services created by a
            # cancelled startup. Stop the registered service set directly.
            try:
                await self._stop_services(final=True)
            except BaseException as cleanup_exc:
                if isinstance(cleanup_exc, (KeyboardInterrupt, SystemExit)):
                    raise
                logger.warning(
                    "Failed to stop partial services for %s: %s",
                    self.agent_id,
                    cleanup_exc,
                )
                self._startup_cleanup_pending = True
            else:
                self._startup_cleanup_pending = False
            self._started = False
            raise

    async def _run_runtime_recovery_loop(self) -> None:
        """Reconcile task restart state once, then scan due wakes and cleanup outbox."""

        from ..recovery_coordinator import RecoveryCoordinator

        due_wakes_only = False
        while True:
            try:
                recovery_report = await asyncio.wait_for(
                    RecoveryCoordinator(self).recover(
                        auto_resume=True,
                        require_autonomy_grant=True,
                        due_wakes_only=due_wakes_only,
                        autonomy_resume_condition=(
                            "network_recovery" if due_wakes_only else "app_reopen"
                        ),
                    ),
                    timeout=30.0,
                )
                if recovery_report.scanned:
                    logger.info(
                        "Runtime recovery reconciliation: scanned=%s resumed=%s "
                        "stalled=%s skipped=%s due_wakes_only=%s",
                        recovery_report.scanned,
                        recovery_report.resumed,
                        recovery_report.stalled,
                        recovery_report.skipped,
                        due_wakes_only,
                    )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                logger.error(
                    "Runtime recovery reconciliation exceeded the 30 second SLO",
                )
            except Exception:
                logger.exception("Runtime recovery reconciliation failed")
            # Chat deletion records analysis retirement in the same
            # transaction. Drain that outbox independently of task recovery,
            # including later scans after a transient cleanup failure. This
            # remains outside the interactive startup path.
            try:
                chat_manager = getattr(self, "chat_manager", None)
                if chat_manager is not None:
                    from ..runner.api import _retry_pending_analysis_cleanup

                    failures = await _retry_pending_analysis_cleanup(
                        chat_manager, self,
                    )
                    if failures:
                        logger.warning(
                            "Analysis cleanup remains pending after workspace recovery: %s",
                            failures,
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Workspace analysis cleanup recovery failed")
            due_wakes_only = True
            await asyncio.sleep(RUNTIME_WAKE_SCAN_INTERVAL_SECONDS)

    def _migrate_legacy_weixin_data(self) -> None:
        """Eagerly migrate legacy weixin -> wechat data on workspace start.

        Each step is guarded so a failure logs a warning instead of
        blocking startup; affected files stay in their legacy state.
        """
        from ..crons.repo.json_repo import migrate_legacy_weixin_jobs_file
        from ..runner.repo.json_repo import migrate_legacy_weixin_chats_file
        from ..runner.session import migrate_legacy_weixin_session_files

        try:
            migrate_legacy_weixin_chats_file(
                self.workspace_dir / "chats.json",
            )
        except Exception as exc:
            logger.warning(
                "weixin->wechat chats.json migration failed for "
                "agent %s: %s",
                self.agent_id,
                exc,
            )

        try:
            migrate_legacy_weixin_jobs_file(
                self.workspace_dir / "jobs.json",
            )
        except Exception as exc:
            logger.warning(
                "weixin->wechat jobs.json migration failed for "
                "agent %s: %s",
                self.agent_id,
                exc,
            )

        try:
            migrate_legacy_weixin_session_files(
                str(self.workspace_dir / "sessions"),
            )
        except Exception as exc:
            logger.warning(
                "weixin->wechat sessions migration failed for agent %s: %s",
                self.agent_id,
                exc,
            )

    async def stop(self, final: bool = True):
        """Stop agent instance and clean up all resources.

        Args:
            final: If True (default), stop ALL services including reusable.
                   If False, skip reusable services (for reload scenario).
        """
        if not self._started and not getattr(self, "_startup_cleanup_pending", False):
            logger.debug(f"Workspace not started: {self.agent_id}")
            return

        task = getattr(self, "_stop_task", None)
        if task is None or task.done():
            task = asyncio.create_task(self._stop_resources(final=final))
            self._stop_task = task
        await await_settled(task)

    async def _stop_resources(self, *, final: bool) -> None:

        logger.info(
            f"Stopping agent instance: {self.agent_id} (final={final})",
        )

        recovery_task = self._recovery_task
        self._recovery_task = None
        if recovery_task is not None and not recovery_task.done():
            recovery_task.cancel()
            await asyncio.gather(recovery_task, return_exceptions=True)

        # Services (including memory and MCP) remain usable until the runtime
        # which consumes them has relinquished its actual resource ownership.
        tracker = getattr(self, "_task_tracker", None)
        if tracker is not None:
            await tracker.shutdown()

        # Stop all services via ServiceManager (handles reuse automatically)
        await self._stop_services(final=final)

        self._started = False
        self._startup_cleanup_pending = False
        logger.info(f"Workspace stopped: {self.agent_id}")

    def __repr__(self) -> str:
        """String representation of workspace."""
        status = "started" if self._started else "stopped"
        return (
            f"Workspace(id={self.agent_id}, "
            f"workspace={self.workspace_dir}, "
            f"status={status})"
        )
