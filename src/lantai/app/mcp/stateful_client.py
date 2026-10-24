# -*- coding: utf-8 -*-
"""MCP stateful clients with proper cross-task lifecycle management.

This module provides Lantai-owned MCP clients that solve the CPU leak issue
caused by cross-task context manager exits.

The issue occurs when an MCP client is closed from a different task:
- connect() enters AsyncExitStack in task A (e.g., startup event)
- close() exits AsyncExitStack in task B (e.g., reload background task)
- anyio.CancelScope requires enter/exit in the same task
- Error is silently ignored, leaving MCP processes and streams uncleaned

Our solution: Run the entire context manager lifecycle in a single dedicated
background task, using event-based signaling for reload/stop operations.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import AsyncExitStack, suppress
from datetime import timedelta
from typing import Any, Literal

import httpx2
from mcp import Client, ClientSession
from mcp.client.stdio import StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client

from ...security.tool_execution_context import build_sandboxed_tool_env
from .network_security import (
    ResolvedMCPEndpoint,
    create_pinned_mcp_http_client,
    resolve_mcp_endpoint,
)

logger = logging.getLogger(__name__)

_MCP_CLOSE_TIMEOUT_SECONDS = 5.0

_INTERNAL_STDIO_ENV_MARKERS = (
    "LANTAI",
    "COPAW",
    "BRIDGE",
    "DESKTOP",
)


def reconnect_backoff_seconds(attempt: int) -> float:
    """Return a bounded retry delay so a broken server cannot log-storm."""

    normalized = max(1, int(attempt))
    return float(min(30, 2 ** (normalized - 1)))


async def _wait_for_reconnect_or_stop(
    stop_event: asyncio.Event,
    delay_seconds: float,
) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay_seconds)
    except asyncio.TimeoutError:
        return


def build_mcp_stdio_env(
    explicit_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build the environment for stdio MCP subprocesses."""

    env = build_sandboxed_tool_env()
    # Packaged Guanlan runs this backend again as a stdio child. Preserve
    # the parent's keyring policy so it cannot reopen Keychain prompts that
    # the desktop explicitly disabled. These are controls, never secrets;
    # keep the general internal-token filter and reject explicit overrides.
    for key in ("LANTAI_DISABLE_KEYRING", "LANTAI_SKIP_KEYRING"):
        if key in os.environ:
            env[key] = os.environ[key]
    for key, value in (explicit_env or {}).items():
        normalized = str(key).upper()
        if any(marker in normalized for marker in _INTERNAL_STDIO_ENV_MARKERS):
            continue
        env[str(key)] = str(value)
    return env


def _tool_input_schema(tool: Any) -> dict[str, Any]:
    schema = getattr(tool, "inputSchema", None)
    if schema is None:
        schema = getattr(tool, "input_schema", None)
    if isinstance(schema, dict):
        return schema
    return {"type": "object", "properties": {}}


def _tool_json_schema(tool: Any) -> dict[str, Any]:
    name = str(getattr(tool, "name", "") or "")
    description = str(getattr(tool, "description", "") or "")
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": _tool_input_schema(tool),
        },
    }


class LantaiStatefulClientBase:
    """Shared Lantai MCP stateful client contract."""

    _lifecycle_task: asyncio.Task | None

    def __init__(self, name: str) -> None:
        self.name = name
        self.session: ClientSession | None = None
        self._sdk_client: Client | None = None
        self.is_connected = False
        self._cached_tools = None
        self.allowed_tools: frozenset[str] = frozenset()
        self.reconnect_attempt = 0
        self.next_retry_seconds = 0.0
        self.last_connection_error = ""
        self._connecting = False
        self._initialization_task: asyncio.Task | None = None

    async def _connect_with_timeout(self, timeout: float) -> None:
        if (
            self.is_connected
            or self._connecting
            or (
                self._lifecycle_task is not None
                and not self._lifecycle_task.done()
            )
        ):
            raise RuntimeError(
                f"MCP client '{self.name}' is already connected "
                "or connecting. "
                "Call close() before connecting again.",
            )
        self._connecting = True
        self._stop_event.clear()
        self._ready_event.clear()
        self._reload_event.clear()

        async def initialize() -> None:
            if isinstance(self, HttpStatefulClient):
                self._resolved_endpoint = await resolve_mcp_endpoint(
                    self.url,
                    allow_private_network=self.allow_private_network,
                )
            if self._stop_event.is_set():
                raise asyncio.CancelledError()
            self._lifecycle_task = asyncio.create_task(self._run_lifecycle())
            await self._ready_event.wait()

        initialization = asyncio.create_task(initialize())
        self._initialization_task = initialization
        try:
            await asyncio.wait_for(initialization, timeout=timeout)
        except BaseException:
            await self._stop_lifecycle_task(force_cancel=True)
            raise
        finally:
            self._initialization_task = None
            self._connecting = False

    async def _stop_lifecycle_task(
        self,
        *,
        force_cancel: bool = False,
    ) -> None:
        (
            "Stop the lifecycle task even if the client never became "
            "connected."
        )

        self._stop_event.set()
        initialization = self._initialization_task
        if initialization is not None and not initialization.done():
            initialization.cancel()
            with suppress(asyncio.CancelledError):
                await initialization
        task = self._lifecycle_task
        if task is None:
            self._sdk_client = None
            self.session = None
            self.is_connected = False
            self._cached_tools = None
            self._ready_event.clear()
            return

        if force_cancel and not task.done():
            task.cancel()
        try:
            try:
                await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=_MCP_CLOSE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise
        finally:
            # An interrupted close must not detach its live resource owner.
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            self._lifecycle_task = None
            self._sdk_client = None
            self.session = None
            self.is_connected = False
            self._cached_tools = None
            self._ready_event.clear()

    async def close(self, ignore_errors: bool = True) -> None:
        """Close MCP client and clean up resources.

        Args:
            ignore_errors: Whether to ignore errors during cleanup

        Raises:
            RuntimeError: If not connected (unless ignore_errors=True)
        """
        if (
            self._lifecycle_task is not None
            or self._initialization_task is not None
        ):
            try:
                cleanup = asyncio.create_task(
                    self._stop_lifecycle_task(
                        force_cancel=not self.is_connected,
                    ),
                )
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    # Cleanup owns its cancellation requests. A caller's
                    # cancellation must not interrupt or masquerade as them.
                    await cleanup
                    raise
            except Exception as e:
                if not ignore_errors:
                    raise
                logger.warning(
                    f"Error closing MCP client '{self.name}': {e}",
                )
            return

        if not self.is_connected:
            if not ignore_errors:
                raise RuntimeError(
                    f"MCP client '{self.name}' is not connected. "
                    f"Call connect() before closing.",
                )
            return

        try:
            # Signal stop and wait for lifecycle task to finish
            self._stop_event.set()
            if self._lifecycle_task:
                await self._lifecycle_task
                self._lifecycle_task = None
        except Exception as e:
            if not ignore_errors:
                raise
            logger.warning(
                f"Error closing MCP client '{self.name}': {e}",
            )

    def _filter_allowed_tools(self, tools: list[Any]) -> list[Any]:
        if not self.allowed_tools:
            return tools
        return [
            tool
            for tool in tools
            if str(getattr(tool, "name", "") or "") in self.allowed_tools
        ]

    def _validate_tool_allowed(self, name: str) -> None:
        if self.allowed_tools and name not in self.allowed_tools:
            raise PermissionError(
                f"MCP tool '{name}' is not permitted by this connector",
            )

    def _cached_tool(self, name: str) -> Any | None:
        return next(
            (
                tool
                for tool in self._cached_tools or []
                if getattr(tool, "name", None) == name
            ),
            None,
        )

    def _protocol_client(self) -> Any:
        return self._sdk_client or self.session

    async def _all_pages(self, method_name: str, item_field: str) -> list[Any]:
        (
            "Collect an MCP list method with cursor-cycle and "
            "page-count guards."
        )
        method = getattr(self._protocol_client(), method_name)
        items: list[Any] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _page in range(100):
            if cursor and self._sdk_client is None:
                from mcp.types import PaginatedRequestParams

                result = await method(
                    params=PaginatedRequestParams(cursor=cursor),
                )
            else:
                result = (
                    await method(cursor=cursor) if cursor else await method()
                )
            items.extend(list(getattr(result, item_field, ()) or ()))
            next_cursor = getattr(result, "next_cursor", None)
            if next_cursor is None:
                next_cursor = getattr(result, "nextCursor", None)
            cursor = str(next_cursor or "").strip() or None
            if cursor is None:
                return items
            if cursor in seen:
                raise RuntimeError(
                    f"MCP pagination cursor cycle in {method_name}",
                )
            seen.add(cursor)
        raise RuntimeError(
            f"MCP pagination exceeded 100 pages in {method_name}",
        )

    async def list_resources(self):
        """List MCP resources using the SDK v2 result contract."""
        self._validate_connection()
        return await self._all_pages("list_resources", "resources")

    async def list_resource_templates(self):
        """List MCP resource templates."""
        self._validate_connection()
        return await self._all_pages(
            "list_resource_templates",
            "resource_templates",
        )

    async def read_resource(self, uri: str):
        """Read one MCP resource by URI."""
        self._validate_connection()
        return await self._protocol_client().read_resource(uri)

    async def list_prompts(self):
        """List MCP prompt templates."""
        self._validate_connection()
        return await self._all_pages("list_prompts", "prompts")

    async def call_tool_exchange(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        input_responses: Any = None,
        request_state: str | None = None,
    ) -> Any:
        (
            "Expose one MRTR exchange without auto-answering user input "
            "requests."
        )
        self._validate_connection()
        self._validate_tool_allowed(name)
        return await self.session.call_tool(
            name,
            arguments or {},
            input_responses=input_responses,
            request_state=request_state,
            allow_input_required=True,
        )

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, str] | None = None,
    ):
        """Render one MCP prompt template."""
        self._validate_connection()
        return await self._protocol_client().get_prompt(
            name,
            arguments=arguments or {},
        )

    async def _call_session_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        *,
        target_tool: Any | None = None,
        read_timeout_seconds: float | None = None,
    ) -> Any:
        """Call MCP through the selected Connector Adapter seam."""

        from .connectors import connector_adapter_for_key

        tool = target_tool or self._cached_tool(name)
        input_schema = (
            _tool_input_schema(tool)
            if tool is not None
            else {"type": "object", "properties": {}}
        )
        adapter = connector_adapter_for_key(
            getattr(self, "_lantai_client_key", ""),
        )
        prepared = adapter.prepare(
            name,
            dict(arguments or {}),
            input_schema=input_schema,
            read_timeout_seconds=read_timeout_seconds,
        )
        protocol = self._protocol_client()
        session = self.session
        try:
            result = await adapter.execute(protocol, name, prepared)
            result = await adapter.finalize(session, name, result, prepared)
        except BaseException:
            try:
                await adapter.settle(session, name, prepared)
            except Exception:
                logger.exception(
                    "MCP connector cleanup failed after an interrupted call",
                )
            raise
        await adapter.settle(session, name, prepared)
        return result

    async def get_callable_function(
        self,
        func_name: str,
        wrap_tool_result: bool = False,
        execution_timeout: float | None = None,
    ):
        """Return an async callable for one MCP tool.

        ``wrap_tool_result`` is accepted for compatibility. Lantai returns the
        raw MCP result here; legacy runtime wrapping is handled by the
        migration adapter.
        """

        _ = wrap_tool_result
        self._validate_connection()
        self._validate_tool_allowed(func_name)
        if self._cached_tools is None:
            await self.list_tools()

        target_tool = None
        for tool in self._cached_tools or []:
            if getattr(tool, "name", None) == func_name:
                target_tool = tool
                break
        if target_tool is None:
            raise ValueError(f"Tool '{func_name}' not found in MCP server")

        async def call_mcp_tool(**kwargs: Any):
            timeout = None
            if execution_timeout:
                timeout = float(execution_timeout)
            return await self._call_session_tool(
                func_name,
                kwargs,
                target_tool=target_tool,
                read_timeout_seconds=timeout,
            )

        call_mcp_tool.__name__ = func_name
        call_mcp_tool.__doc__ = getattr(target_tool, "description", None) or ""
        setattr(call_mcp_tool, "name", func_name)
        setattr(call_mcp_tool, "description", call_mcp_tool.__doc__)
        setattr(call_mcp_tool, "json_schema", _tool_json_schema(target_tool))
        return call_mcp_tool


class StdIOStatefulClient(LantaiStatefulClientBase):
    """StdIO MCP client with proper cross-task lifecycle management.

    Key improvements:
    - Context manager enter/exit happens in the same asyncio task
    - Uses event-based signaling for reload/stop operations
    - Properly cleans up MCP subprocess and stdio streams
    - No CPU leak on reload
    - No zombie processes
    """

    def __init__(
        self,
        name: Any,
        command: Any,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        encoding: str = "utf-8",
        encoding_error_handler: Literal[
            "strict",
            "ignore",
            "replace",
        ] = "strict",
        read_timeout_seconds: float = 60 * 5,
    ) -> None:
        """Initialize the StdIO MCP client.

        Args:
            name: Client identifier (unique across MCP servers)
            command: The executable to run to start the server
            args: Command line arguments to pass to the executable
            env: The environment to use when spawning the process
            cwd: The working directory to use when spawning the process
            encoding: The text encoding used when sending/receiving messages
            encoding_error_handler: The text encoding error handler
            read_timeout_seconds: The read timeout seconds

        Raises:
            TypeError: If name or command is not a string
        """
        if not isinstance(name, str):
            raise TypeError(f"name must be str, got {type(name).__name__}")
        if not isinstance(command, str):
            raise TypeError(
                f"command must be str, got {type(command).__name__}",
            )

        super().__init__(name)
        self.server_params = StdioServerParameters(
            command=command,
            args=args or [],
            env=build_mcp_stdio_env(env),
            cwd=cwd,
            encoding=encoding,
            encoding_error_handler=encoding_error_handler,
        )
        self.read_timeout_seconds = read_timeout_seconds

        # Lifecycle management
        self._lifecycle_task: asyncio.Task | None = None
        self._reload_event = asyncio.Event()
        self._ready_event = asyncio.Event()
        self._stop_event = asyncio.Event()

    async def _run_lifecycle(self) -> None:
        """Run MCP client lifecycle in a dedicated task.

        This ensures __aenter__ and __aexit__ are called in the same task,
        avoiding the cross-task cancel scope error.
        """
        from mcp.client.stdio import stdio_client

        failure_count = 0
        while not self._stop_event.is_set():
            try:
                logger.debug(f"Connecting MCP client: {self.name}")

                # Enter context manager in THIS task
                async with AsyncExitStack() as stack:
                    transport = stdio_client(self.server_params)
                    if (
                        os.name == "posix"
                        and getattr(self, "_lantai_client_key", "")
                        == "officecli"
                    ):
                        from .officecli_transport import officecli_stdio_client

                        transport = officecli_stdio_client(self.server_params)
                    sdk_client = Client(
                        transport,
                        mode="auto",
                        read_timeout_seconds=float(self.read_timeout_seconds),
                    )
                    self._sdk_client = await stack.enter_async_context(
                        sdk_client,
                    )
                    self.session = self._sdk_client.session

                    # Mark as connected and signal ready
                    self.is_connected = True
                    failure_count = 0
                    self.reconnect_attempt = 0
                    self.next_retry_seconds = 0.0
                    self.last_connection_error = ""
                    self._ready_event.set()
                    logger.info(f"MCP client connected: {self.name}")

                    # Wait for reload or stop signal
                    while (
                        not self._reload_event.is_set()
                        and not self._stop_event.is_set()
                    ):
                        await asyncio.sleep(0.1)

                    # Clear state before exiting context
                    self.session = None
                    self._sdk_client = None
                    self.is_connected = False
                    self._cached_tools = None

                    if self._reload_event.is_set():
                        logger.info(f"Reloading MCP client: {self.name}")
                        self._reload_event.clear()
                        self._ready_event.clear()
                        # Context manager will exit here, then loop restarts
                    else:
                        logger.info(f"Stopping MCP client: {self.name}")
                        # Context manager will exit here, then loop exits

                # Context manager exits cleanly in THIS task

            except Exception as e:
                failure_count += 1
                retry_delay = reconnect_backoff_seconds(failure_count)
                self.reconnect_attempt = failure_count
                self.next_retry_seconds = retry_delay
                self.last_connection_error = str(e)
                logger.error(
                    "Error in MCP client lifecycle for %s: %s; "
                    "retrying in %.1fs",
                    self.name,
                    e,
                    retry_delay,
                    exc_info=True,
                )
                self.session = None
                self._sdk_client = None
                self.is_connected = False
                self._cached_tools = None
                self._ready_event.clear()
                await _wait_for_reconnect_or_stop(
                    self._stop_event,
                    retry_delay,
                )

        logger.info(f"MCP client lifecycle task exited: {self.name}")

    async def connect(self, timeout: float = 30.0) -> None:
        (
            "Connect within the deadline and reclaim resources on "
            "interruption."
        )
        await self._connect_with_timeout(timeout)

    async def reload(self, timeout: float = 30.0) -> None:
        """Reload the MCP client (reconnect).

        Args:
            timeout: Connection timeout in seconds (default 30s)

        Raises:
            RuntimeError: If not connected
            asyncio.TimeoutError: If reload times out
        """
        if not self.is_connected:
            raise RuntimeError(
                f"MCP client '{self.name}' is not connected. "
                f"Call connect() first.",
            )

        logger.info(f"Triggering reload for MCP client: {self.name}")
        self._ready_event.clear()
        self._reload_event.set()

        # Wait for new connection
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
            logger.info(f"Reload completed for MCP client: {self.name}")
        except asyncio.TimeoutError:
            logger.error(
                f"Timeout waiting for MCP client '{self.name}' to reload",
            )
            raise

    async def list_tools(self):
        """Get all available tools from the server.

        Returns:
            List of available MCP tools

        Raises:
            RuntimeError: If not connected
        """
        self._validate_connection()

        tools = await self._all_pages("list_tools", "tools")
        self._cached_tools = self._filter_allowed_tools(tools)
        return self._cached_tools

    async def call_tool(self, name: str, arguments: dict | None = None):
        """Call a tool on the MCP server.

        Args:
            name: Tool name
            arguments: Tool arguments (optional)

        Returns:
            Tool call result

        Raises:
            RuntimeError: If not connected
        """
        self._validate_connection()
        self._validate_tool_allowed(name)
        return await self._call_session_tool(name, arguments)

    def _validate_connection(self) -> None:
        """Validate the connection to the MCP server.

        Raises:
            RuntimeError: If not connected or session not initialized
        """
        if not self.is_connected:
            raise RuntimeError(
                f"MCP client '{self.name}' is not connected. "
                f"Call connect() first.",
            )

        if not self.session:
            raise RuntimeError(
                f"MCP client '{self.name}' session is not initialized. "
                f"Call connect() first.",
            )


class HttpStatefulClient(LantaiStatefulClientBase):
    """HTTP/SSE MCP client with proper cross-task lifecycle management.

    Supports both streamable HTTP and SSE transports.
    """

    def __init__(
        self,
        name: Any,
        transport: Any,
        url: Any,
        allow_private_network: bool = False,
        headers: dict[str, str] | None = None,
        timeout: float = 30,
        sse_read_timeout: float = 60 * 5,
        **client_kwargs: Any,
    ) -> None:
        """Initialize the HTTP MCP client.

        Args:
            name: Client identifier (unique across MCP servers)
            transport: The transport type ("streamable_http" or "sse")
            url: The URL to the MCP server
            allow_private_network: Explicit grant for private/LAN endpoints
            headers: Additional headers to include in the HTTP request
            timeout: The timeout for the HTTP request in seconds
            sse_read_timeout: The timeout for reading SSE in seconds
            **client_kwargs: Additional keyword arguments for the client

        Raises:
            TypeError: If name, transport, or url is not a string
            ValueError: If transport is not "streamable_http" or "sse"
        """
        if not isinstance(name, str):
            raise TypeError(f"name must be str, got {type(name).__name__}")
        if not isinstance(transport, str):
            raise TypeError(
                f"transport must be str, got {type(transport).__name__}",
            )
        if transport not in ["streamable_http", "sse"]:
            raise ValueError(
                f"transport must be 'streamable_http' or 'sse', "
                f"got {transport!r}",
            )
        if not isinstance(url, str):
            raise TypeError(f"url must be str, got {type(url).__name__}")

        super().__init__(name)
        self.transport = transport
        self.url = url
        self.allow_private_network = allow_private_network
        self.headers = headers
        self.timeout = timeout
        self.sse_read_timeout = sse_read_timeout
        self.read_timeout_seconds = sse_read_timeout
        self.client_kwargs = client_kwargs

        # Lifecycle management
        self._lifecycle_task: asyncio.Task | None = None
        self._reload_event = asyncio.Event()
        self._ready_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._resolved_endpoint: ResolvedMCPEndpoint | None = None

    async def _run_lifecycle(self) -> None:
        """Run MCP client lifecycle in a dedicated task."""
        failure_count = 0
        while not self._stop_event.is_set():
            try:
                logger.debug(f"Connecting MCP client: {self.name}")

                # Enter context manager in THIS task
                async with AsyncExitStack() as stack:
                    endpoint = self._resolved_endpoint
                    if endpoint is None:
                        raise RuntimeError("MCP endpoint was not resolved")
                    timeout_seconds = (
                        self.timeout.total_seconds()
                        if isinstance(self.timeout, timedelta)
                        else self.timeout
                    )
                    sse_read_timeout_seconds = (
                        self.sse_read_timeout.total_seconds()
                        if isinstance(self.sse_read_timeout, timedelta)
                        else self.sse_read_timeout
                    )
                    # Select client based on transport
                    if self.transport == "streamable_http":
                        # Configure httpx client with MCP-recommended timeouts
                        http_client = create_pinned_mcp_http_client(
                            endpoint,
                            headers=self.headers or {},
                            timeout=httpx2.Timeout(
                                connect=timeout_seconds,
                                read=sse_read_timeout_seconds,
                                write=timeout_seconds,
                                pool=timeout_seconds,
                            ),
                        )

                        # Add http_client to exit stack for proper cleanup
                        await stack.enter_async_context(http_client)

                        transport = streamable_http_client(
                            url=self.url,
                            http_client=http_client,
                        )
                    else:

                        def pinned_http_client_factory(
                            headers=None,
                            timeout=None,
                            auth=None,
                        ):
                            return create_pinned_mcp_http_client(
                                endpoint,
                                headers=headers,
                                timeout=timeout,
                                auth=auth,
                            )

                        transport = sse_client(
                            url=self.url,
                            headers=self.headers,
                            timeout=timeout_seconds,
                            sse_read_timeout=sse_read_timeout_seconds,
                            httpx_client_factory=pinned_http_client_factory,
                            **self.client_kwargs,
                        )

                    sdk_client = Client(
                        transport,
                        mode="auto",
                        read_timeout_seconds=float(sse_read_timeout_seconds),
                    )
                    self._sdk_client = await stack.enter_async_context(
                        sdk_client,
                    )
                    self.session = self._sdk_client.session

                    # Mark as connected and signal ready
                    self.is_connected = True
                    failure_count = self.reconnect_attempt = 0
                    self.next_retry_seconds = 0.0
                    self.last_connection_error = ""
                    self._ready_event.set()
                    logger.info(f"MCP client connected: {self.name}")

                    # Wait for reload or stop signal
                    while (
                        not self._reload_event.is_set()
                        and not self._stop_event.is_set()
                    ):
                        await asyncio.sleep(0.1)

                    # Clear state before exiting context
                    self.session = None
                    self._sdk_client = None
                    self.is_connected = False
                    self._cached_tools = None

                    if self._reload_event.is_set():
                        logger.info(f"Reloading MCP client: {self.name}")
                        self._reload_event.clear()
                        self._ready_event.clear()
                    else:
                        logger.info(f"Stopping MCP client: {self.name}")

                # Context manager exits cleanly in THIS task

            except Exception as e:
                failure_count += 1
                retry_delay = reconnect_backoff_seconds(failure_count)
                self.reconnect_attempt = failure_count
                self.next_retry_seconds = retry_delay
                self.last_connection_error = str(e)
                logger.error(
                    "Error in MCP client lifecycle for %s: %s; "
                    "retrying in %.1fs",
                    self.name,
                    e,
                    retry_delay,
                    exc_info=True,
                )
                self.session = None
                self._sdk_client = None
                self.is_connected = False
                self._cached_tools = None
                self._ready_event.clear()
                await _wait_for_reconnect_or_stop(
                    self._stop_event,
                    retry_delay,
                )

        logger.info(f"MCP client lifecycle task exited: {self.name}")

    async def connect(self, timeout: float = 30.0) -> None:
        (
            "Connect within the deadline and reclaim resources on "
            "interruption."
        )
        await self._connect_with_timeout(timeout)

    async def list_tools(self):
        """Get all available tools from the server.

        Returns:
            List of available MCP tools

        Raises:
            RuntimeError: If not connected
        """
        self._validate_connection()

        tools = await self._all_pages("list_tools", "tools")
        self._cached_tools = self._filter_allowed_tools(tools)
        return self._cached_tools

    async def call_tool(self, name: str, arguments: dict | None = None):
        """Call a tool on the MCP server.

        Args:
            name: Tool name
            arguments: Tool arguments (optional)

        Returns:
            Tool call result

        Raises:
            RuntimeError: If not connected
        """
        self._validate_connection()
        self._validate_tool_allowed(name)
        return await self._call_session_tool(name, arguments)

    def _validate_connection(self) -> None:
        """Validate the connection to the MCP server.

        Raises:
            RuntimeError: If not connected or session not initialized
        """
        if not self.is_connected:
            raise RuntimeError(
                f"MCP client '{self.name}' is not connected. "
                f"Call connect() first.",
            )

        if not self.session:
            raise RuntimeError(
                f"MCP client '{self.name}' session is not initialized. "
                f"Call connect() first.",
            )
