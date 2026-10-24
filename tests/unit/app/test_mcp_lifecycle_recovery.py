"""Cancellation and reconnect contracts for the real MCP client entry points."""
import asyncio
from contextlib import suppress
from types import SimpleNamespace

import pytest

from lantai.app.mcp import stateful_client as clients


def make_client(kind):
    if kind == "stdio":
        return clients.StdIOStatefulClient("probe", "unused")
    return clients.HttpStatefulClient(
        "probe", "streamable_http", "https://example.com/mcp"
    )


async def cancel_task(task):
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task


@pytest.mark.parametrize("kind", ["stdio", "http"])
async def test_close_reaps_initializing_lifecycle(kind):
    client = make_client(kind)
    task = asyncio.create_task(asyncio.Event().wait())
    client._lifecycle_task = task
    try:
        await client.close()
        assert task.done()
        assert client._lifecycle_task is None
        assert not client.is_connected
    finally:
        await cancel_task(task)


@pytest.mark.parametrize("kind", ["stdio", "http"])
@pytest.mark.parametrize("operation", ["cancel", "timeout"])
async def test_interrupted_connect_reaps_lifecycle(
    monkeypatch, kind, operation
):
    client = make_client(kind)
    started = asyncio.Event()
    lifecycle_tasks = []

    async def resolve(*args, **kwargs):
        return object()

    async def lifecycle():
        lifecycle_tasks.append(asyncio.current_task())
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(clients, "resolve_mcp_endpoint", resolve)
    monkeypatch.setattr(client, "_run_lifecycle", lifecycle)
    connect = asyncio.create_task(
        client.connect(timeout=0.03 if operation == "timeout" else 2)
    )
    try:
        await started.wait()
        if operation == "cancel":
            connect.cancel()
        done, _ = await asyncio.wait({connect}, timeout=0.3)
        assert (
            connect in done
        ), "connect timeout must include lifecycle cleanup"
        with pytest.raises(
            asyncio.CancelledError
            if operation == "cancel"
            else asyncio.TimeoutError
        ):
            await connect
        assert all(task.done() for task in lifecycle_tasks)
        assert client._lifecycle_task is None
    finally:
        await cancel_task(connect)
        for task in lifecycle_tasks:
            await cancel_task(task)


@pytest.mark.parametrize("kind", ["stdio", "http"])
async def test_connect_rejects_an_existing_pending_lifecycle(
    monkeypatch, kind
):
    client = make_client(kind)
    pending = asyncio.create_task(asyncio.Event().wait())
    client._lifecycle_task = pending
    created = []

    async def resolve(*args, **kwargs):
        return object()

    async def unexpected():
        created.append(asyncio.current_task())
        client._ready_event.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(clients, "resolve_mcp_endpoint", resolve)
    monkeypatch.setattr(client, "_run_lifecycle", unexpected)
    try:
        with pytest.raises(RuntimeError):
            await client.connect(timeout=0.1)
        assert client._lifecycle_task is pending
    finally:
        await cancel_task(pending)
        for task in created:
            await cancel_task(task)


async def test_reload_waits_for_new_transport_ready(monkeypatch):
    client = make_client("stdio")
    retired = asyncio.Event()
    permit_reconnect = asyncio.Event()
    connections = []

    class SDKClient:
        def __init__(self, *args, **kwargs):
            self.session = object()

        async def __aenter__(self):
            if connections:
                await permit_reconnect.wait()
            connections.append(self)
            return self

        async def __aexit__(self, *args):
            retired.set()

    monkeypatch.setattr(clients, "Client", SDKClient)
    monkeypatch.setattr(
        "mcp.client.stdio.stdio_client", lambda *args: object()
    )
    await client.connect(timeout=1)
    old_session = client.session
    reload = asyncio.create_task(client.reload(timeout=1))
    try:
        await asyncio.wait_for(retired.wait(), timeout=1)
        await asyncio.sleep(0)
        assert (
            not reload.done()
        ), "old readiness cannot acknowledge a new connection"
        permit_reconnect.set()
        await reload
        assert client.session is not old_session
        assert client.is_connected
    finally:
        permit_reconnect.set()
        await cancel_task(reload)
        await client.close()


@pytest.mark.parametrize("kind", ["stdio", "http"])
async def test_close_has_a_graceful_shutdown_deadline(monkeypatch, kind):
    client = make_client(kind)
    task = asyncio.create_task(asyncio.Event().wait())
    client._lifecycle_task = task
    client.is_connected = True
    monkeypatch.setattr(
        clients, "_MCP_CLOSE_TIMEOUT_SECONDS", 0.02, raising=False
    )
    try:
        await asyncio.wait_for(client.close(), timeout=0.3)
        assert task.done()
        assert client._lifecycle_task is None
        assert client._sdk_client is None
    finally:
        await cancel_task(task)


async def test_close_during_dns_cancels_initialization(monkeypatch):
    client = make_client("http")
    resolving = asyncio.Event()
    release = asyncio.Event()
    started = []

    async def resolve(*args, **kwargs):
        resolving.set()
        await release.wait()
        return object()

    async def lifecycle():
        started.append(True)
        client.is_connected = True
        client._ready_event.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(clients, "resolve_mcp_endpoint", resolve)
    monkeypatch.setattr(client, "_run_lifecycle", lifecycle)
    connecting = asyncio.create_task(client.connect(timeout=1))
    try:
        await resolving.wait()
        await client.close()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await connecting
        assert not started
        assert not client.is_connected
        assert client._lifecycle_task is None
    finally:
        await cancel_task(connecting)
        await cancel_task(client._lifecycle_task)


async def test_cancelled_close_propagates_after_reaping_owner():
    client = make_client("stdio")
    task = asyncio.create_task(asyncio.Event().wait())
    client._lifecycle_task = task
    client.is_connected = True
    closing = asyncio.create_task(client.close())
    await asyncio.sleep(0)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert task.done()
    assert client._lifecycle_task is None


async def test_cancelled_close_during_dns_cleanup_propagates(monkeypatch):
    client = make_client("http")
    resolving, cleaning, finish = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )

    async def resolve(*args, **kwargs):
        resolving.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await finish.wait()

    monkeypatch.setattr(clients, "resolve_mcp_endpoint", resolve)
    connecting = asyncio.create_task(client.connect(timeout=2))
    await resolving.wait()
    closing = asyncio.create_task(client.close())
    await cleaning.wait()
    closing.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    with pytest.raises(asyncio.CancelledError):
        await connecting
    assert client._initialization_task is None
    assert client._lifecycle_task is None
