# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name,unused-argument
import inspect
import asyncio
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from lantai.exceptions import AppBaseException

from ..config import load_config  # pylint: disable=no-name-in-module
from ..config.utils import get_config_path
from ..constant import (
    DOCS_ENABLED,
    LOG_LEVEL_ENV,
    CORS_ORIGINS,
    WORKING_DIR,
    PROJECT_NAME,
)
from ..__version__ import __version__
from ..utils.logging import (
    setup_logger,
    add_project_file_handler,
    LOG_FILE_PATH,
)
from ..utils.system_info import summarize_python_environment
from .auth import AuthMiddleware
from .browser_pairing import (
    compare_desktop_token,
    has_valid_browser_session,
    is_browser_pairing_path,
    is_public_browser_pairing_path,
)
from .routers import router as api_router, create_agent_scoped_router
from .routers.agent_scoped import AgentContextMiddleware
from .routers.approval import router as approval_router
from .routers.voice import voice_router
from ..envs import load_envs_into_environ
from ..providers.provider_manager import ProviderManager
from ..local_models.manager import LocalModelManager
from .multi_agent_manager import MultiAgentManager
from .migration import (
    migrate_legacy_workspace_to_default_agent,
    migrate_legacy_skills_to_skill_pool,
    ensure_default_agent_exists,
    ensure_qa_agent_exists,
    ensure_team_agents_exist,
)
from .mime_types import init_browser_mime_types
from ..agents.skills_manager import (
    import_builtin_skills,
    reconcile_pool_manifest,
)
from .agent_service import LantaiAgentService
from .channels.registry import register_custom_channel_routes
from .beta_access.bootstrap import build_beta_access_runtime
from .beta_access.wiring import register_beta_access_runtime

# Apply log level on load so reload child process gets same level as CLI.
logger = setup_logger(os.environ.get(LOG_LEVEL_ENV, "info"))

# Ensure static assets are served with browser-compatible MIME types across
# platforms. Use only the built-in table so sandboxed macOS builds do not try
# to read system mime.types files like /etc/apache2/mime.types at import time.
init_browser_mime_types()

# Load persisted env vars into os.environ at module import time
# so they are available before the lifespan starts.
load_envs_into_environ()
_BETA_ACCESS_RUNTIME = build_beta_access_runtime()


# Dynamic runner that selects the correct workspace runner based on request
class DynamicMultiAgentRunner:
    """Runner wrapper that dynamically routes to the correct workspace runner.

    This allows the local agent process API to work with multiple agents by inspecting
    the X-Agent-Id header on each request.
    """

    def __init__(self):
        self.framework_type = "lantai"
        self._multi_agent_manager = None

    def set_multi_agent_manager(self, manager):
        """Set the MultiAgentManager instance after initialization."""
        self._multi_agent_manager = manager

    async def _get_workspace(self, request):
        """Get the correct workspace based on request.

        Returns:
            Workspace: The workspace instance for the current agent.
        """
        from .agent_context import get_current_agent_id

        # Get agent_id from context (set by middleware or header)
        agent_id = get_current_agent_id()

        logger.debug(f"_get_workspace: agent_id={agent_id}")

        # Get the correct workspace
        if not self._multi_agent_manager:
            raise RuntimeError("MultiAgentManager not initialized")

        try:
            workspace = await self._multi_agent_manager.get_agent(agent_id)
            logger.debug(
                "Got workspace: %s, runner: %s",
                workspace.agent_id,
                workspace.runner,
            )
            return workspace
        except (ValueError, AppBaseException) as e:
            logger.error(f"Agent not found: {e}")
            raise
        except Exception as e:
            logger.error(
                f"Error getting workspace: {e}",
                exc_info=True,
            )
            raise

    async def _get_workspace_runner(self, request):
        """Get the correct workspace runner based on request."""
        workspace = await self._get_workspace(request)
        return workspace.runner

    async def stream_query(self, request, *args, **kwargs):
        """Dynamically route to the correct workspace runner.

        Registers the task with the workspace's TaskTracker so that
        graceful shutdown during agent reload can detect in-flight
        background tasks (fixes #3275).
        """
        logger.debug("DynamicMultiAgentRunner.stream_query called")
        workspace = None
        run_key = None
        try:
            workspace = await self._get_workspace(request)
            runner = workspace.runner
            logger.debug(f"Got runner: {runner}, type: {type(runner)}")

            # Register this task with the workspace's TaskTracker so
            # _graceful_stop_old_instance() can see it during reload.
            run_key = f"ext-{uuid.uuid4().hex}"
            await workspace.task_tracker.register_external_task(
                run_key, owner_task=asyncio.current_task(),
            )

            # Delegate to the actual runner's stream_query generator
            count = 0
            async for item in runner.stream_query(request, *args, **kwargs):
                count += 1
                logger.debug(f"Yielding item #{count}: {type(item)}")
                yield item
            logger.debug(f"stream_query completed, yielded {count} items")
        except Exception as e:
            logger.error(
                f"Error in stream_query: {e}",
                exc_info=True,
            )
            # Yield error message to client
            yield {
                "error": str(e),
                "type": "error",
            }
        finally:
            # Always unregister the task when done (success, error,
            # or cancellation).
            if workspace is not None and run_key is not None:
                await workspace.task_tracker.unregister_external_task(run_key)

    async def query_handler(self, request, *args, **kwargs):
        """Dynamically route to the correct workspace runner.

        Registers the task with the workspace's TaskTracker so that
        graceful shutdown during agent reload can detect in-flight
        requests (fixes #3275).
        """
        workspace = None
        run_key = None
        try:
            workspace = await self._get_workspace(request)
            runner = workspace.runner

            run_key = f"ext-{uuid.uuid4().hex}"
            await workspace.task_tracker.register_external_task(
                run_key, owner_task=asyncio.current_task(),
            )

            runner_request = kwargs.pop("request", None)
            if runner_request is None and not isinstance(request, list):
                runner_request = request
                msgs = list(getattr(request, "input", None) or [])
            else:
                msgs = request

            call_kwargs = dict(kwargs)
            if runner_request is not None:
                call_kwargs["request"] = runner_request

            async for item in runner.query_handler(
                msgs,
                *args,
                **call_kwargs,
            ):
                yield item
        finally:
            # Always unregister the task when done (success, error,
            # or cancellation).
            if workspace is not None and run_key is not None:
                await workspace.task_tracker.unregister_external_task(run_key)

    # Async context manager support for process API lifecycle
    async def __aenter__(self):
        """
        No-op context manager entry (workspaces manage their own runners).
        """
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """No-op context manager exit (workspaces manage their own runners)."""
        return None


# Use dynamic runner for the local agent process API.
runner = DynamicMultiAgentRunner()

agent_service = LantaiAgentService(
    app_name="Friday",
    app_description="A helpful assistant with background task support",
    runner=runner,
    stream_task_queue="stream_query",
    stream_task_timeout=900,
)


def _prepare_workspace_assets() -> None:
    """Repair non-core workspace assets outside the server startup loop."""
    try:
        migrate_legacy_workspace_to_default_agent()
    except Exception:
        logger.warning("Failed to migrate legacy workspace", exc_info=True)

    try:
        ensure_default_agent_exists()
    except Exception:
        logger.warning("Failed to repair default agent", exc_info=True)

    try:
        migrate_legacy_skills_to_skill_pool()
    except Exception:
        logger.warning("Failed to migrate legacy skills", exc_info=True)

    try:
        import_builtin_skills()
        reconcile_pool_manifest()
    except Exception:
        logger.warning("Failed to sync builtin skills to pool", exc_info=True)

    try:
        ensure_qa_agent_exists()
    except Exception:
        logger.warning("Failed to repair builtin QA agent", exc_info=True)

    try:
        ensure_team_agents_exist()
    except Exception:
        logger.warning("Failed to repair builtin team agents", exc_info=True)

def _configure_project_file_logging() -> bool:
    """Attach the CLI log file unless Electron already owns desktop logging."""
    if os.environ.get("LANTAI_DESKTOP_APP", "").strip() == "1":
        return False
    add_project_file_handler(LOG_FILE_PATH)
    return True


@asynccontextmanager
async def lifespan(  # pylint: disable=too-many-statements,too-many-branches
    app: FastAPI,
):
    startup_start_time = time.time()
    _configure_project_file_logging()

    # Settle the whole restore group before startup reads config, credentials,
    # or starts agents. The stable fence also excludes another live restorer.
    from ..utils.restore_fence import restore_read_fence

    with restore_read_fence():
        pass

    # Remove data left by retired telemetry builds before any background
    # startup work begins. This migration is local-only and idempotent.
    try:
        from ..utils.privacy import purge_legacy_telemetry_data

        purge_legacy_telemetry_data(WORKING_DIR)
    except Exception:
        logger.warning("Failed to purge legacy telemetry data", exc_info=True)

    # ================================================================
    # Phase 1: Fast synchronous setup (target < 100ms)
    # Everything here must be lightweight so the server starts quickly.
    # ================================================================

    from .auth import auto_register_from_env

    auto_register_from_env()

    # Create core managers (instant — no I/O)
    logger.debug("Initializing MultiAgentManager...")
    multi_agent_manager = MultiAgentManager(
        beta_access_admission_guard=_BETA_ACCESS_RUNTIME.admission_guard,
    )
    provider_manager = ProviderManager.get_instance()
    local_model_manager = LocalModelManager.get_instance()

    # Start token usage manager background tasks
    logger.debug("Starting TokenUsageManager background tasks...")
    from ..token_usage import get_token_usage_manager

    token_usage_manager = get_token_usage_manager()
    token_usage_manager.start(flush_interval=10)

    # Expose to endpoints (must be set before first request arrives)
    app.state.multi_agent_manager = multi_agent_manager
    app.state.provider_manager = provider_manager
    app.state.local_model_manager = local_model_manager
    app.state.plugin_loader = None
    app.state.plugin_registry = None

    if isinstance(runner, DynamicMultiAgentRunner):
        runner.set_multi_agent_manager(multi_agent_manager)

    async def _get_agent_by_id(agent_id: str = None):
        """Get agent instance by ID, or active agent if not specified."""
        if agent_id is None:
            config = load_config(get_config_path())
            agent_id = config.agents.active_agent or "default"
        return await multi_agent_manager.get_agent(agent_id)

    app.state.get_agent_by_id = _get_agent_by_id

    fast_elapsed = time.time() - startup_start_time
    logger.info(
        f"Server ready in {fast_elapsed:.3f}s "
        f"(agents loading in background)",
    )

    # ================================================================
    # Phase 2: Background heavy initialization
    # Agents, plugins, and services start in a background task so the
    # server can begin accepting HTTP requests immediately.
    # First API requests that need an agent will await its readiness
    # via MultiAgentManager.get_agent() lazy-loading / event wait.
    # ================================================================

    async def _background_startup():  # pylint: disable=too-many-statements
        try:
            try:
                from ..storage_retention import clean_storage_retention

                await asyncio.to_thread(clean_storage_retention, dry_run=False)
            except Exception:
                logger.warning("Failed to apply local storage retention", exc_info=True)

            asset_start = time.time()
            logger.info("Preparing workspace assets in a background thread")
            await asyncio.to_thread(_prepare_workspace_assets)
            logger.info(
                "Workspace assets prepared in %.3f seconds",
                time.time() - asset_start,
            )

            from .mcp.guanlan import (
                ensure_guanlan_package,
                should_prepare_guanlan_for_startup,
            )

            if should_prepare_guanlan_for_startup():
                await ensure_guanlan_package()

            # Start all configured agents (truly parallel now)
            await multi_agent_manager.start_all_configured_agents()

            provider_manager.start_local_model_resume(local_model_manager)

            # ---- Plugin System ----
            logger.debug("Initializing plugin system...")

            from ..plugins.loader import PluginLoader
            from ..plugins.runtime import RuntimeHelpers
            from ..config.utils import get_plugins_dir

            plugin_dirs = [
                get_plugins_dir(),
            ]

            plugin_loader = PluginLoader(plugin_dirs)

            config = load_config(get_config_path())
            plugin_configs = (
                config.plugins if hasattr(config, "plugins") else {}
            )
            logger.debug(
                f"Loading plugins with {len(plugin_configs)} config(s)",
            )

            loaded_plugins = await plugin_loader.load_all_plugins(
                configs=plugin_configs,
            )
            logger.debug(f"Loaded {len(loaded_plugins)} plugin(s)")

            runtime_helpers = RuntimeHelpers(
                provider_manager=provider_manager,
            )
            plugin_loader.registry.set_runtime_helpers(runtime_helpers)

            for (
                provider_id,
                provider_reg,
            ) in plugin_loader.registry.get_all_providers().items():
                provider_manager.register_plugin_provider(
                    provider_id=provider_id,
                    provider_class=provider_reg.provider_class,
                    label=provider_reg.label,
                    base_url=provider_reg.base_url,
                    metadata=provider_reg.metadata,
                )
                logger.debug(
                    f"Registered plugin provider: {provider_id}",
                )

            app.state.plugin_loader = plugin_loader
            app.state.plugin_registry = plugin_loader.registry

            # ---- Plugin Control Commands ----
            logger.debug("Registering plugin control commands...")
            from ..app.runner.control_commands import register_command
            from ..app.channels.command_registry import CommandRegistry

            command_registry = CommandRegistry()

            control_commands = plugin_loader.registry.get_control_commands()
            for cmd_reg in control_commands:
                try:
                    register_command(cmd_reg.handler)

                    command_registry.register_command(
                        f"/{cmd_reg.handler.command_name}",
                        priority_level=cmd_reg.priority_level,
                    )

                    logger.debug(
                        f"Registered plugin control command: "
                        f"/{cmd_reg.handler.command_name} "
                        f"from plugin '{cmd_reg.plugin_id}' (priority"
                        f"={cmd_reg.priority_level})",
                    )
                except Exception as e:
                    logger.error(
                        f"✗ Failed to register control command "
                        f"'{cmd_reg.handler.command_name}' "
                        f"from plugin '{cmd_reg.plugin_id}': {e}",
                        exc_info=True,
                    )

            # ---- Startup Hooks ----
            logger.debug("Executing plugin startup hooks...")
            startup_hooks = plugin_loader.registry.get_startup_hooks()
            for hook in startup_hooks:
                try:
                    logger.debug(
                        f"Executing startup hook '{hook.hook_name}' "
                        f"from plugin '{hook.plugin_id}' "
                        f"(priority={hook.priority})",
                    )

                    result = hook.callback()
                    if inspect.iscoroutine(
                        result,
                    ) or inspect.isawaitable(result):
                        await result

                    logger.debug(
                        f"Completed startup hook '{hook.hook_name}' "
                        f"from plugin '{hook.plugin_id}'",
                    )
                except Exception as e:
                    logger.error(
                        f"✗ Failed to execute startup hook "
                        f"'{hook.hook_name}' "
                        f"from plugin '{hook.plugin_id}': {e}",
                        exc_info=True,
                    )

            # ---- Approval Service ----
            try:
                default_agent = await multi_agent_manager.get_agent(
                    "default",
                )
                if default_agent.channel_manager:
                    from .approvals import get_approval_service

                    get_approval_service().set_channel_manager(
                        default_agent.channel_manager,
                    )
            except Exception as e:
                logger.warning(f"Approval service setup skipped: {e}")

            startup_elapsed = time.time() - startup_start_time
            logger.info(
                "Background startup completed in "
                f"{startup_elapsed:.3f} seconds",
            )

            # Print server URL again so it's visible after background logs
            from ..config.utils import read_last_api
            from ..utils.startup_display import print_ready_banner

            api_info = read_last_api()
            print_ready_banner(api_info, startup_elapsed)
        except Exception:
            logger.error(
                "Background startup encountered an error",
                exc_info=True,
            )

    _bg_task = asyncio.create_task(_background_startup())

    try:
        yield
    finally:
        # Cancel background startup if still in progress
        if not _bg_task.done():
            _bg_task.cancel()
            with suppress(asyncio.CancelledError):
                await _bg_task

        # BrowserSkill sessions may have been created by short-lived atomic
        # browser tool calls. They share one process client and must all be
        # released when the backend exits so borrowed tabs are returned.
        try:
            from .browser_workbench import get_browser_skill_client

            await get_browser_skill_client().stop_all()
        except Exception:
            logger.warning("Failed to stop BrowserSkill sessions", exc_info=True)

        # ==================== Execute Shutdown Hooks ====================
        plugin_registry = getattr(app.state, "plugin_registry", None)
        if plugin_registry is not None:
            logger.info("Executing plugin shutdown hooks...")
            shutdown_hooks = plugin_registry.get_shutdown_hooks()
            for hook in shutdown_hooks:
                try:
                    logger.info(
                        f"Executing shutdown hook '{hook.hook_name}' "
                        f"from plugin '{hook.plugin_id}' (priority"
                        f"={hook.priority})",
                    )

                    result = hook.callback()
                    if inspect.iscoroutine(result) or inspect.isawaitable(
                        result,
                    ):
                        await result

                    logger.info(
                        f"✓ Completed shutdown hook '{hook.hook_name}' "
                        f"from plugin '{hook.plugin_id}'",
                    )
                except Exception as e:
                    logger.error(
                        f"✗ Failed to execute shutdown hook "
                        f"'{hook.hook_name}' "
                        f"from plugin '{hook.plugin_id}': {e}",
                        exc_info=True,
                    )

        local_model_mgr = getattr(app.state, "local_model_manager", None)
        if local_model_mgr is not None:
            logger.info("Stopping local model server...")
            try:
                await local_model_mgr.shutdown_server()
            except Exception as exc:
                logger.error(
                    "Error shutting down local model server gracefully: %s",
                    exc,
                )
                with suppress(OSError, RuntimeError, ValueError):
                    local_model_mgr.shutdown_server_sync()

        # Stop multi-agent manager (stops all agents and their components)
        multi_agent_mgr = getattr(app.state, "multi_agent_manager", None)
        if multi_agent_mgr is not None:
            logger.info("Stopping MultiAgentManager...")
            try:
                await multi_agent_mgr.stop_all()
            except Exception as e:
                logger.error(f"Error stopping MultiAgentManager: {e}")

        # Stop token usage manager (drain queue and final flush)
        logger.info("Stopping TokenUsageManager...")
        try:
            await token_usage_manager.stop()
        except Exception as e:
            logger.error(f"Error stopping TokenUsageManager: {e}")

        logger.info("Application shutdown complete")


app = FastAPI(
    lifespan=lifespan,
    docs_url="/docs" if DOCS_ENABLED else None,
    redoc_url="/redoc" if DOCS_ENABLED else None,
    openapi_url="/openapi.json" if DOCS_ENABLED else None,
)


_DESKTOP_TOKEN_ENV = "LANTAI_DESKTOP_TOKEN"
_DESKTOP_TOKEN_HEADER = "X-Lantai-Desktop-Token"
_DESKTOP_INSTANCE_NONCE_ENV = "LANTAI_DESKTOP_INSTANCE_NONCE"


@app.middleware("http")
async def desktop_token_middleware(request, call_next):
    """Protect Electron's private loopback API from external local clients.

    Normal CLI/server deployments do not set LANTAI_DESKTOP_TOKEN, so this
    guard is inert outside the packaged desktop app.
    """
    expected_token = os.environ.get(_DESKTOP_TOKEN_ENV, "")
    path = request.url.path
    if (
        expected_token
        and path.startswith("/api/")
        and request.method != "OPTIONS"
    ):
        if (
            is_public_browser_pairing_path(path)
            or path == "/api/mcp/oauth/callback"
            or path == "/api/cloud-account/watcha/callback"
            or path == "/api/models/tokendance/oauth/callback"
            # Generated Office media uses a short-lived, unguessable grant in
            # the path. Editor image loaders cannot attach desktop headers;
            # the route validates the grant and its media-root confinement.
            or path.startswith("/api/console/office-media/")
        ):
            return await call_next(request)

        supplied_token = request.headers.get(
            _DESKTOP_TOKEN_HEADER,
            "",
        ) or request.query_params.get("desktop_token", "")
        has_desktop_token = compare_desktop_token(
            supplied_token,
            expected_token,
        )
        if not has_desktop_token and has_valid_browser_session(request):
            request.state.browser_pairing = True
            return await call_next(request)

        if not has_desktop_token:
            detail = (
                "Browser pairing required"
                if not is_browser_pairing_path(path)
                else "Desktop API token required"
            )
            return Response(
                content=(
                    '{"detail":"'
                    + detail
                    + '","code":"desktop_token_required"}'
                ),
                status_code=403,
                media_type="application/json",
            )

    return await call_next(request)


# Add agent context middleware for agent-scoped routes
app.add_middleware(AgentContextMiddleware)

app.add_middleware(AuthMiddleware)

# Apply CORS middleware if CORS_ORIGINS is set
if CORS_ORIGINS:
    origins = [o.strip() for o in CORS_ORIGINS.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition"],
    )


def _resolve_console_static_dir() -> str:
    """Compatibility facade for the shared console static resolver."""
    from ..utils.console_static import resolve_console_static_dir

    return resolve_console_static_dir()


_CONSOLE_STATIC_DIR = _resolve_console_static_dir()
_CONSOLE_INDEX = (
    Path(_CONSOLE_STATIC_DIR) / "index.html" if _CONSOLE_STATIC_DIR else None
)
logger.info(f"STATIC_DIR: {_CONSOLE_STATIC_DIR}")


@app.get("/")
def read_root():
    if _CONSOLE_INDEX and _CONSOLE_INDEX.exists():
        return FileResponse(_CONSOLE_INDEX)
    return {
        "message": (
            f"{PROJECT_NAME} web console is not available. "
            "If you installed the project from source code, please run "
            "`npm ci && npm run build` in the `console/` "
            f"directory, and restart {PROJECT_NAME} to enable the "
            "web console."
        ),
    }


@app.get("/api/version")
def get_version():
    """Return the current application version (public-safe payload)."""
    return {
        "version": __version__,
    }


@app.get("/health")
def health_check():
    """Return a minimal local readiness signal for desktop probes."""
    return {
        "status": "ok",
        "name": PROJECT_NAME,
        "version": __version__,
    }


@app.get("/api/desktop/readiness")
def desktop_readiness_check():
    """Return an authenticated proof for Electron's managed backend."""
    return {
        "status": "ok",
        "desktop": True,
        "name": PROJECT_NAME,
        "version": __version__,
        "pid": os.getpid(),
        "instance_nonce": os.environ.get(_DESKTOP_INSTANCE_NONCE_ENV, ""),
    }


@app.get("/api/doctor/runtime")
async def get_doctor_runtime():
    """Return server runtime diagnostics for authenticated troubleshooting."""
    from .concurrency import get_runtime_concurrency_governor
    from .office_capabilities import collect_office_capabilities

    return {
        "python_executable": sys.executable,
        "python_environment": summarize_python_environment(),
        "concurrency": get_runtime_concurrency_governor().snapshot(),
        "office_capabilities": collect_office_capabilities(),
    }


app.include_router(api_router, prefix="/api")

# Approval router: /api/approval/approve, /api/approval/deny, etc.
app.include_router(approval_router, prefix="/api")

# Agent-scoped router: /api/agents/{agentId}/chats, etc.
agent_scoped_router = create_agent_scoped_router()
app.include_router(agent_scoped_router, prefix="/api")


app.include_router(
    agent_service.router,
    prefix="/api/agent",
    tags=["agent"],
)

# Voice channel: Twilio-facing endpoints at root level (not under /api/).
# POST /voice/incoming, WS /voice/ws, POST /voice/status-callback
app.include_router(voice_router, tags=["voice"])

# Custom channel routes (before SPA catch-all to ensure route priority)
register_custom_channel_routes(app)

# Register activation and recovery before the SPA fallback. Ordinary release
# channels have no beta router, while their local admission is unrestricted.
register_beta_access_runtime(app, _BETA_ACCESS_RUNTIME)

# Console static files and SPA fallback
# Register these AFTER API routes to ensure proper routing priority
if os.path.isdir(_CONSOLE_STATIC_DIR):
    _console_path = Path(_CONSOLE_STATIC_DIR)

    def _serve_console_index():
        if _CONSOLE_INDEX and _CONSOLE_INDEX.exists():
            return FileResponse(_CONSOLE_INDEX)

        raise HTTPException(status_code=404, detail="Not Found")

    _assets_dir = _console_path / "assets"
    if _assets_dir.is_dir():
        app.mount(
            "/assets",
            StaticFiles(directory=str(_assets_dir)),
            name="assets",
        )

    @app.get("/console")
    @app.get("/console/")
    @app.get("/console/{full_path:path}")
    def _console_spa_alias(full_path: str = ""):
        _ = full_path
        return _serve_console_index()

    # SPA fallback: catch-all route for frontend routing
    # Must be registered AFTER all API routes to avoid conflicts
    @app.get("/{full_path:path}")
    def _console_spa(full_path: str):
        # Prevent catching common system/special paths
        if full_path in ("docs", "redoc", "openapi.json"):
            raise HTTPException(status_code=404, detail="Not Found")
        # Skip API routes (should already be matched due to registration order)
        if full_path.startswith("api/") or full_path == "api":
            raise HTTPException(status_code=404, detail="Not Found")

        # Serve static files from the console build directory (e.g. logo SVGs,
        # favicons, images placed in public/).  Only serve regular files whose
        # path does not escape the console directory.
        if full_path and ".." not in full_path:
            # Security: Reject absolute paths to prevent path traversal bypass
            if not Path(full_path).is_absolute():
                static_file = _console_path / full_path
                if static_file.is_file():
                    return FileResponse(static_file)

        return _serve_console_index()
