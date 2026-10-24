# -*- coding: utf-8 -*-
"""MCP client manager for hot-reloadable client lifecycle management.

This module provides centralized management of MCP clients with support
for runtime updates without restarting the application.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import logging
import os
import time
from typing import Any, Dict, List, Literal, TYPE_CHECKING

from .stateful_client import HttpStatefulClient, StdIOStatefulClient

if TYPE_CHECKING:
    from ...config.config import MCPClientConfig, MCPConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MCPRuntimeState:
    """Authoritative runtime state retained even when no client connected."""

    client_key: str
    status: Literal["disabled", "starting", "ready", "degraded", "error"]
    attempt_count: int = 0
    error_code: str | None = None
    diagnostic_message: str = ""
    updated_at: float = 0.0


class MCPClientManager:
    """Manages MCP clients with hot-reload support.

    This manager handles the lifecycle of MCP clients, including:
    - Initial loading from config
    - Runtime replacement when config changes
    - Cleanup on shutdown

    Design pattern mirrors ChannelManager for consistency.
    """

    def __init__(self, agent_id: str | None = None) -> None:
        """Initialize an empty MCP client manager."""
        self.agent_id = agent_id or ""
        self._clients: Dict[str, Any] = {}
        self._runtime_states: Dict[str, MCPRuntimeState] = {}
        self._lock = asyncio.Lock()
        self._initialization_task: asyncio.Task[None] | None = None

    def init_from_config_background(
        self,
        config: "MCPConfig",
    ) -> asyncio.Task[None]:
        """Start initial MCP client setup without blocking core chat startup."""
        if (
            self._initialization_task is not None
            and not self._initialization_task.done()
        ):
            self._initialization_task.cancel()

        task = asyncio.create_task(self.init_from_config(config))
        self._initialization_task = task

        def _log_result(done_task: asyncio.Task[None]) -> None:
            with suppress(asyncio.CancelledError):
                exc = done_task.exception()
                if exc is not None:
                    logger.warning(
                        "Background MCP initialization failed: %s",
                        exc,
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )

        task.add_done_callback(_log_result)
        return task

    async def init_from_config(self, config: "MCPConfig") -> None:
        """Initialize clients from configuration.

        Args:
            config: MCP configuration containing client definitions
        """
        logger.debug("Initializing MCP clients from config")
        for key, client_config in config.clients.items():
            if not client_config.enabled:
                await self._set_runtime_state(key, status="disabled")
                logger.debug(f"MCP client '{key}' is disabled, skipping")
                continue

            previous = await self.get_runtime_state(key)
            attempt_count = (previous.attempt_count if previous else 0) + 1
            await self._set_runtime_state(
                key,
                status="starting",
                attempt_count=attempt_count,
            )
            try:
                await self._add_client(key, client_config)
                await self._set_runtime_state(
                    key,
                    status="ready",
                    attempt_count=attempt_count,
                )
                logger.debug(f"MCP client '{key}' initialized successfully")
            except BaseException as e:
                if isinstance(
                    e,
                    (asyncio.CancelledError, KeyboardInterrupt, SystemExit),
                ):
                    raise
                await self._set_runtime_state(
                    key,
                    status="error",
                    attempt_count=attempt_count,
                    error_code=type(e).__name__,
                    diagnostic_message=str(e),
                )
                logger.warning(
                    f"Failed to initialize MCP client '{key}': {e}",
                    exc_info=True,
                )

    async def get_clients(self) -> List[Any]:
        """Get list of all active MCP clients.

        This method is called by the runner on each query to get
        the latest set of clients.

        Returns:
            List of connected MCP client instances
        """
        async with self._lock:
            return [
                client
                for client in self._clients.values()
                if client is not None
            ]

    async def get_client(self, key: str) -> Any | None:
        """Get a specific active MCP client by key.

        Args:
            key: Client identifier (from config)

        Returns:
            Connected MCP client instance, or None if not found
        """
        async with self._lock:
            return self._clients.get(key)

    async def get_runtime_state(self, key: str) -> MCPRuntimeState | None:
        """Return lifecycle truth even when a connector failed before publish."""

        async with self._lock:
            state = self._runtime_states.get(key)
            client = self._clients.get(key)
            if state is None or client is None:
                return state

            connected = bool(getattr(client, "is_connected", False))
            if state.status == "ready" and not connected:
                diagnostic_message = str(
                    getattr(client, "last_connection_error", "") or ""
                )
                live_state = MCPRuntimeState(
                    client_key=key,
                    status="degraded",
                    attempt_count=max(
                        state.attempt_count,
                        int(getattr(client, "reconnect_attempt", 0) or 0),
                    ),
                    error_code=(
                        "ConnectionError" if diagnostic_message else None
                    ),
                    diagnostic_message=diagnostic_message,
                    updated_at=time.time(),
                )
                self._runtime_states[key] = live_state
                return live_state
            if state.status == "degraded" and connected:
                live_state = MCPRuntimeState(
                    client_key=key,
                    status="ready",
                    attempt_count=state.attempt_count,
                    updated_at=time.time(),
                )
                self._runtime_states[key] = live_state
                return live_state
            return state

    async def _set_runtime_state(
        self,
        key: str,
        *,
        status: Literal["disabled", "starting", "ready", "degraded", "error"],
        attempt_count: int = 0,
        error_code: str | None = None,
        diagnostic_message: str = "",
    ) -> MCPRuntimeState:
        state = MCPRuntimeState(
            client_key=key,
            status=status,
            attempt_count=attempt_count,
            error_code=error_code,
            diagnostic_message=diagnostic_message,
            updated_at=time.time(),
        )
        async with self._lock:
            self._runtime_states[key] = state
        return state

    async def replace_client(
        self,
        key: str,
        client_config: "MCPClientConfig",
        timeout: float = 60.0,
    ) -> None:
        """Replace or add a client with new configuration.

        Flow: connect new (outside lock) → atomic swap (inside lock) → close
        old (outside lock). Readers can use the replacement immediately even
        when the retired client's shutdown is slow.

        Args:
            key: Client identifier (from config)
            client_config: New client configuration
            timeout: Connection timeout in seconds (default 60s)
        """
        # 1. Create and connect new client outside lock (may be slow)
        logger.debug(f"Connecting new MCP client: {key}")
        prepared_config = await self._prepare_client_config(key, client_config)
        new_client = self._build_client(prepared_config)
        setattr(new_client, "_lantai_client_key", key)

        previous_state = await self.get_runtime_state(key)
        attempt_count = (previous_state.attempt_count if previous_state else 0) + 1
        await self._set_runtime_state(
            key,
            status="starting",
            attempt_count=attempt_count,
        )

        try:
            # Add timeout to prevent indefinite blocking
            await asyncio.wait_for(
                new_client.connect(timeout=timeout),
                timeout=timeout + 5,
            )
        except BaseException as exc:
            await self._force_cleanup_client(new_client)
            existing = await self.get_client(key)
            await self._set_runtime_state(
                key,
                status=(
                    "degraded"
                    if existing is not None
                    and getattr(existing, "is_connected", False)
                    else "error"
                ),
                attempt_count=attempt_count,
                error_code=type(exc).__name__,
                diagnostic_message=str(exc),
            )
            raise

        # 2. Publish the replacement atomically. Closing the retired client is
        # deliberately outside the manager lock because transport teardown can
        # block on subprocesses or remote peers.
        async with self._lock:
            old_client = self._clients.get(key)
            self._clients[key] = new_client
            self._runtime_states[key] = MCPRuntimeState(
                client_key=key,
                status="ready",
                attempt_count=attempt_count,
                updated_at=time.time(),
            )

        if old_client is not None:
            logger.debug(f"Closing old MCP client: {key}")
            try:
                await old_client.close()
            except Exception as e:
                logger.warning(
                    f"Error closing old MCP client '{key}': {e}",
                )
        else:
            logger.debug(f"Added new MCP client: {key}")

    async def remove_client(self, key: str) -> None:
        """Remove and close a client.

        Args:
            key: Client identifier to remove
        """
        async with self._lock:
            old_client = self._clients.pop(key, None)
            previous = self._runtime_states.get(key)
            self._runtime_states[key] = MCPRuntimeState(
                client_key=key,
                status="disabled",
                attempt_count=previous.attempt_count if previous else 0,
                updated_at=time.time(),
            )

        if old_client is not None:
            logger.debug(f"Removing MCP client: {key}")
            try:
                await old_client.close()
            except Exception as e:
                logger.warning(f"Error closing MCP client '{key}': {e}")

    async def close_all(self) -> None:
        """Close all MCP clients.

        Called during application shutdown.
        """
        if (
            self._initialization_task is not None
            and not self._initialization_task.done()
        ):
            self._initialization_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._initialization_task
        self._initialization_task = None

        async with self._lock:
            clients_snapshot = list(self._clients.items())
            self._clients.clear()

        logger.debug("Closing all MCP clients")
        for key, client in clients_snapshot:
            if client is not None:
                try:
                    await client.close()
                except Exception as e:
                    logger.warning(f"Error closing MCP client '{key}': {e}")

    async def _add_client(
        self,
        key: str,
        client_config: "MCPClientConfig",
        timeout: float = 60.0,
    ) -> None:
        """Add a new client (used during initial setup).

        Args:
            key: Client identifier
            client_config: Client configuration
            timeout: Connection timeout in seconds (default 60s)
        """
        prepared_config = await self._prepare_client_config(key, client_config)
        client = self._build_client(prepared_config)
        setattr(client, "_lantai_client_key", key)

        try:
            await asyncio.wait_for(
                client.connect(timeout=timeout),
                timeout=timeout + 5,
            )
        except BaseException:
            await self._force_cleanup_client(client)
            raise

        async with self._lock:
            self._clients[key] = client

    @staticmethod
    async def _force_cleanup_client(client: Any) -> None:
        """Force-close a client whose ``connect()`` was interrupted.

        ``StatefulClientBase.close()`` refuses to run when
        ``is_connected`` is still ``False`` (which is the case when
        ``connect()`` times out or raises).  We bypass that guard by
        closing the ``AsyncExitStack`` directly — this triggers the
        ``stdio_client`` finally-block that sends SIGTERM/SIGKILL to
        the child process.

        The ``ClientSession`` is registered on the same stack via
        ``enter_async_context``, so ``stack.aclose()`` exits it in
        LIFO order — no separate session teardown is needed.
        """
        if client is None:
            return

        close = getattr(client, "close", None)
        if callable(close):
            try:
                await close()
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                logger.debug(
                    "Error during close() force-cleanup of MCP client",
                    exc_info=True,
                )

        stack = getattr(client, "stack", None)
        if stack is None:
            return

        try:
            await stack.aclose()
        except Exception:
            logger.debug(
                "Error during force-cleanup of MCP client",
                exc_info=True,
            )
        finally:
            for attr, default in (
                ("stack", None),
                ("session", None),
                ("is_connected", False),
            ):
                try:
                    setattr(client, attr, default)
                except Exception:
                    pass

    async def _prepare_client_config(
        self,
        key: str,
        client_config: "MCPClientConfig",
    ) -> "MCPClientConfig":
        """Refresh connector authorization before opening its transport."""

        if not self.agent_id or not client_config.oauth.enabled:
            return client_config
        from .lifecycle import prepare_client_oauth

        return await prepare_client_oauth(
            agent_id=self.agent_id,
            client_key=key,
            client_config=client_config,
        )

    @staticmethod
    def _build_client(client_config: "MCPClientConfig") -> Any:
        """Build MCP client instance by configured transport."""
        rebuild_info = {
            "name": client_config.name,
            "transport": client_config.transport,
            "url": client_config.url,
            "allow_private_network": client_config.allow_private_network,
            "headers": client_config.headers or None,
            "command": client_config.command,
            "args": list(client_config.args),
            "env": dict(client_config.env),
            "cwd": client_config.cwd or None,
        }

        if client_config.transport == "stdio":
            client = StdIOStatefulClient(
                name=client_config.name,
                command=client_config.command,
                args=client_config.args,
                env=client_config.env,
                cwd=client_config.cwd or None,
            )
            setattr(client, "allowed_tools", frozenset(client_config.allowed_tools))
            setattr(client, "_lantai_rebuild_info", rebuild_info)
            return client

        from .lifecycle import oauth_headers

        headers = oauth_headers(client_config)
        if headers:
            headers = {k: os.path.expandvars(v) for k, v in headers.items()}

        client = HttpStatefulClient(
            name=client_config.name,
            transport=client_config.transport,
            url=client_config.url,
            allow_private_network=client_config.allow_private_network,
            headers=headers or None,
        )
        setattr(client, "allowed_tools", frozenset(client_config.allowed_tools))
        setattr(client, "_lantai_rebuild_info", rebuild_info)
        return client
