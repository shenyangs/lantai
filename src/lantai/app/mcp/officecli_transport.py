# -*- coding: utf-8 -*-
"""POSIX OfficeCLI transport with private update policy and group ownership.

OfficeCLI's MCP entry bypasses its normal SKIP_UPDATE check. Its native
autoUpdate=false config must exist before spawn, in a fresh connection HOME.
The transport also owns descendants after a graceful MCP leader exit; an
exited leader alone is not a settlement receipt.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import tempfile
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TextIO

import anyio
import mcp_types
import psutil
from anyio.streams.text import TextReceiveStream
from mcp.client.stdio import StdioServerParameters, get_default_environment
from mcp.shared.message import SessionMessage

from ...utils.owned_subprocess import _PosixScope

logger = logging.getLogger(__name__)


async def _await_retained(task: asyncio.Task):
    """Retain a resource owner through repeated native task cancellation."""
    interrupted = False
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                interrupted = True
        return task.result(), interrupted


async def _wait_until(predicate, timeout: float) -> bool:
    deadline = anyio.current_time() + timeout
    while not predicate():
        if anyio.current_time() >= deadline:
            return False
        await anyio.sleep(0.02)
    return True


async def _close(stream) -> None:
    with suppress(
        OSError,
        anyio.BrokenResourceError,
        anyio.ClosedResourceError,
    ):
        await stream.aclose()


async def _settle(process, scope: _PosixScope) -> None:
    await _close(process.stdin)
    # Poll returncode: waiting on pipe EOF can hide an exited leader whose
    # child inherited stdout. Check the group even when the leader exits 0.
    await _wait_until(lambda: process.returncode is not None, 2.0)
    last_error = None
    try:
        if not scope.empty():
            scope.signal(signal.SIGTERM)
            await _wait_until(scope.empty, 2.0)
        if not scope.empty():
            scope.signal(signal.SIGKILL)
            await _wait_until(scope.empty, 2.0)
    except (OSError, RuntimeError, psutil.Error) as exc:
        last_error = str(exc)
        logger.error(
            "OfficeCLI cleanup retains group %s: %s",
            scope.pid,
            last_error,
        )

    # Never release ownership/reconnect while a kill survivor is live. A
    # denied signal or changed identity is observable and cannot justify
    # killing a different process or reporting successful cleanup.
    while True:
        try:
            if scope.empty():
                break
            scope.signal(signal.SIGKILL)
            error = "owned OfficeCLI group is still live after SIGKILL"
        except (OSError, RuntimeError, psutil.Error) as exc:
            error = str(exc)
        if error != last_error:
            logger.error(
                "OfficeCLI cleanup retains group %s: %s",
                scope.pid,
                error,
            )
            last_error = error
        await anyio.sleep(1.0)
    await _close(process.stdout)
    await process.aclose()


@asynccontextmanager
async def officecli_stdio_client(
    server: StdioServerParameters,
    errlog: TextIO = sys.stderr,
):
    """Keep update policy, stdio and the native group in one connection."""
    with tempfile.TemporaryDirectory(prefix="lantai-officecli-") as home:
        config_dir = Path(home) / ".officecli"
        config_dir.mkdir(mode=0o700)
        config = config_dir / "config.json"
        config.write_bytes(b'{"autoUpdate":false}\n')
        config.chmod(0o600)
        env = get_default_environment() | (server.env or {})
        env.update(
            HOME=home,
            OFFICECLI_SKIP_UPDATE="1",
            OFFICECLI_NO_AUTO_RESIDENT="1",
            OFFICECLI_RESIDENT_FLUSH="each",
        )

        async def spawn():
            process = await anyio.open_process(
                [server.command, *server.args],
                env=env,
                stderr=errlog,
                cwd=server.cwd,
                start_new_session=True,
            )
            # Capture the actual spawned group before another await. If
            # identity inspection fails, keep the handle and private HOME.
            # A scope without a birth receipt refuses signals to a live
            # leader; it can settle only after that leader actually exits.
            try:
                scope = _PosixScope(process)
            except (OSError, RuntimeError, psutil.Error):
                logger.exception(
                    "OfficeCLI retains unverified process %s",
                    process.pid,
                )
                scope = object.__new__(_PosixScope)
                scope.pid = process.pid
                scope.birth = None
                await _settle(process, scope)
                raise
            return process, scope

        # Cancellation during spawn cannot discard a newly created handle.
        (process, scope), interrupted = await _await_retained(
            asyncio.create_task(spawn()),
        )
        read_writer, read_stream = anyio.create_memory_object_stream(
            0,
        )
        write_stream, write_reader = anyio.create_memory_object_stream(0)
        writer_done = anyio.Event()
        cleanup = None
        shutting_down = False

        async def stdout_reader():
            stdout = TextReceiveStream(
                process.stdout,
                encoding=server.encoding,
                errors=server.encoding_error_handler,
            )
            try:
                async with read_writer:
                    buffer = ""
                    async for chunk in stdout:
                        lines = (buffer + chunk).split("\n")
                        buffer = lines.pop()
                        for line in lines:
                            try:
                                message = mcp_types.jsonrpc_message_adapter.validate_json(
                                    line,
                                    by_name=False,
                                )
                                value = SessionMessage(message)
                            except ValueError as exc:
                                value = exc
                            try:
                                await read_writer.send(value)
                            except (
                                anyio.ClosedResourceError,
                                anyio.BrokenResourceError,
                            ):
                                # Keep draining until owned shutdown closes
                                # stdout; a full pipe must not prevent EOF exit.
                                async for _ in stdout:
                                    pass
                                return
            except (anyio.ClosedResourceError, anyio.BrokenResourceError, OSError):
                if not shutting_down:
                    logger.exception("OfficeCLI stdout failed mid-session")

        async def stdin_writer():
            try:
                async with write_reader:
                    async for message in write_reader:
                        data = message.message.model_dump_json(
                            by_alias=True,
                            exclude_unset=True,
                        )
                        await process.stdin.send(
                            (data + "\n").encode(
                                server.encoding,
                                server.encoding_error_handler,
                            ),
                        )
            except (anyio.ClosedResourceError, anyio.BrokenResourceError, OSError):
                await read_writer.aclose()
            finally:
                writer_done.set()

        async def shutdown():
            read_stream.close()
            write_stream.close()
            with anyio.move_on_after(0.5):
                await writer_done.wait()
            await _settle(process, scope)
            for stream in (read_stream, write_stream, read_writer, write_reader):
                await stream.aclose()

        try:
            if interrupted:
                raise asyncio.CancelledError()
            async with anyio.create_task_group() as group:
                group.start_soon(stdout_reader)
                group.start_soon(stdin_writer)
                try:
                    yield read_stream, write_stream
                finally:
                    shutting_down = True
                    cleanup = asyncio.create_task(shutdown())
                    _, cancelled = await _await_retained(cleanup)
                    interrupted |= cancelled
                    group.cancel_scope.cancel()
        finally:
            # Covers cancellation after spawn but before task-group entry.
            if cleanup is None:
                shutting_down = True
                _, cancelled = await _await_retained(
                    asyncio.create_task(shutdown()),
                )
                interrupted |= cancelled
            if interrupted:
                raise asyncio.CancelledError()
