# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""A transaction must settle native ownership before commit or deletion."""

import asyncio
from pathlib import Path

import anyio
import pytest
from mcp.types import CallToolResult, TextContent

import lantai.app.officecli_safe_bridge as bridge
from lantai.app.mcp import connectors
from lantai.app.mcp.stateful_client import StdIOStatefulClient


class ResidentSession:
    def __init__(self, *, mutation="ok", validation="ok", close_failure=0):
        self.mutation = mutation
        self.validation = validation
        self.close_failure = close_failure
        self.events = []
        self.close_count = 0
        self.resident = False
        self.working_path = None
        self.validation_started = asyncio.Event()
        self.validation_permitted = asyncio.Event()
        self.request_cancelled = False
        self.close_started = asyncio.Event()
        self.close_permitted = asyncio.Event()
        self.close_permitted.set()

    async def call_tool(self, name, arguments=None, read_timeout_seconds=None):
        _ = read_timeout_seconds
        assert name == "officecli"
        command = arguments["command"]
        verb = command[0]
        self.events.append(verb)
        self.working_path = Path(command[1])
        if verb == "close":
            self.close_count += 1
            self.close_started.set()
            await self.close_permitted.wait()
            assert (
                self.working_path.exists()
            ), "copy deleted before native close"
            if self.close_count == self.close_failure:
                return self.result(error=True)
            self.resident = False
            return self.result()
        if verb == "set":
            self.working_path.write_bytes(b"after")
            self.resident = True
            return self.operation_result(self.mutation)
        if verb == "validate":
            self.resident = True
            self.validation_started.set()
            if self.validation == "wait":
                try:
                    await self.validation_permitted.wait()
                except asyncio.CancelledError:
                    self.request_cancelled = True
                    raise
            return self.operation_result(self.validation)
        return self.result()

    @staticmethod
    def result(*, error=False):
        return CallToolResult(
            content=[TextContent(type="text", text="native result")],
            isError=error,
        )

    def operation_result(self, mode):
        if mode == "exception":
            raise OSError("native operation interrupted")
        if mode == "cancel":
            raise asyncio.CancelledError()
        return self.result(error=mode == "error")


@pytest.fixture(name="transaction_client")
def _make_transaction_client(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "WORKING_DIR", tmp_path / "working")
    source = tmp_path / "report.docx"
    source.write_bytes(b"before")
    commits = []
    real_commit = bridge.commit_officecli_transaction

    def make(session):
        def checked_commit(transaction):
            assert not session.resident, "validate reopened a native resident"
            assert transaction.working_path.exists()
            assert source.read_bytes() == b"before"
            commits.append(transaction)
            session.events.append("commit")
            return real_commit(transaction)

        monkeypatch.setattr(
            bridge,
            "commit_officecli_transaction",
            checked_commit,
        )
        client = StdIOStatefulClient("Office", "python")
        client._lantai_client_key = "officecli"
        client.session = session
        client.is_connected = True
        return client

    return source, commits, make


async def write(client, source):
    return await client.call_tool(
        "officecli",
        {"command": ["set", str(source), "/body/p[1]", "--prop", "bold=true"]},
    )


@pytest.mark.asyncio
async def test_validation_resident_closes_before_atomic_commit(
    transaction_client,
):
    source, commits, make = transaction_client
    session = ResidentSession()
    result = await write(make(session), source)
    assert session.events == ["set", "close", "validate", "close", "commit"]
    assert not session.resident
    assert not session.working_path.parent.exists()
    assert source.read_bytes() == b"after"
    assert len(commits) == 1
    assert "Lantai Office delivery receipt" in result.content[-1].text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,error",
    [
        ("error", RuntimeError),
        ("exception", OSError),
        ("cancel", asyncio.CancelledError),
    ],
)
async def test_validation_failure_settles_without_commit(
    transaction_client,
    mode,
    error,
):
    source, commits, make = transaction_client
    session = ResidentSession(validation=mode)
    with pytest.raises(error):
        await write(make(session), source)
    assert session.events == ["set", "close", "validate", "close"]
    assert not session.resident
    assert session.working_path.parent.exists() == (mode != "error")
    assert source.read_bytes() == b"before"
    assert not commits


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["error", "exception", "cancel"])
async def test_initial_failure_only_deletes_when_outcome_is_known(
    transaction_client,
    mode,
):
    source, commits, make = transaction_client
    session = ResidentSession(mutation=mode)
    client = make(session)
    if mode == "error":
        assert (await write(client, source)).is_error
    else:
        with pytest.raises(
            OSError if mode == "exception" else asyncio.CancelledError,
        ):
            await write(client, source)
    assert session.events == ["set", "close"]
    assert not session.resident
    assert session.working_path.parent.exists() == (mode != "error")
    assert source.read_bytes() == b"before"
    assert not commits


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_close", [1, 2])
async def test_close_error_preserves_copy_and_original_without_receipt(
    transaction_client,
    failed_close,
):
    source, commits, make = transaction_client
    session = ResidentSession(close_failure=failed_close)
    with pytest.raises(RuntimeError, match="OfficeCLI"):
        await write(make(session), source)
    assert source.read_bytes() == b"before"
    assert not commits
    assert session.resident
    assert session.working_path.read_bytes() == b"after"
    assert session.close_count == failed_close


@pytest.mark.asyncio
async def test_caller_cancel_waits_for_native_close(transaction_client):
    source, commits, make = transaction_client
    session = ResidentSession(validation="wait")
    task = asyncio.create_task(write(make(session), source))
    await session.validation_started.wait()
    session.close_started.clear()
    session.close_permitted.clear()
    task.cancel()
    await asyncio.sleep(0)
    abandoned = session.request_cancelled
    early_events = list(session.events)
    returned_early = task.done()
    session.validation_permitted.set()
    await asyncio.wait_for(session.close_started.wait(), timeout=1)
    assert session.working_path.exists()
    assert not task.done()
    task.cancel()
    await asyncio.sleep(0)
    assert (
        not task.done()
    ), "second caller cancellation detached native cleanup"
    session.close_permitted.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not abandoned, "caller abandoned the native request"
    assert early_events == ["set", "close", "validate"]
    assert not returned_early
    assert task.cancelled()
    assert not session.resident
    assert not session.working_path.parent.exists()
    assert source.read_bytes() == b"before"
    assert not commits


class DelayedMutationSession(ResidentSession):
    def __init__(self):
        super().__init__()
        self.mutation_started = asyncio.Event()
        self.mutation_permitted = asyncio.Event()

    async def call_tool(self, name, arguments=None, read_timeout_seconds=None):
        if arguments["command"][0] == "set":
            self.working_path = Path(arguments["command"][1])
            self.mutation_started.set()
            try:
                await self.mutation_permitted.wait()
            except asyncio.CancelledError:
                self.request_cancelled = True
                raise
        return await super().call_tool(name, arguments, read_timeout_seconds)


@pytest.mark.asyncio
async def test_cancelled_mutation_waits_for_actual_response_before_close(
    transaction_client,
):
    source, commits, make = transaction_client
    session = DelayedMutationSession()
    task = asyncio.create_task(write(make(session), source))
    await session.mutation_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    abandoned = session.request_cancelled
    returned_early = task.done()
    early_events = list(session.events)
    copy_retained = session.working_path.exists()
    session.mutation_permitted.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not abandoned
    assert not returned_early
    assert not early_events
    assert copy_retained
    assert session.events == ["set", "close"]
    assert not session.resident
    assert not session.working_path.parent.exists()
    assert source.read_bytes() == b"before"
    assert not commits


@pytest.mark.asyncio
async def test_reconnect_does_not_redirect_transaction_cleanup(
    transaction_client,
):
    source, commits, make = transaction_client
    original = DelayedMutationSession()
    replacement = ResidentSession()
    client = make(original)
    task = asyncio.create_task(write(client, source))
    await original.mutation_started.wait()
    client.session = replacement
    original.mutation_permitted.set()
    await task
    assert original.events == ["set", "close", "validate", "close", "commit"]
    assert not replacement.events
    assert source.read_bytes() == b"after"
    assert len(commits) == 1


@pytest.mark.asyncio
async def test_anyio_cancel_during_validation_close_never_commits(
    transaction_client,
):
    source, commits, make = transaction_client
    session = ResidentSession()
    scope_ready = asyncio.Event()
    scope = None

    original_call = session.call_tool

    async def blocked_second_close(
        name,
        arguments=None,
        read_timeout_seconds=None,
    ):
        if arguments["command"][0] == "close" and session.close_count == 1:
            session.close_started.clear()
            session.close_permitted.clear()
        return await original_call(name, arguments, read_timeout_seconds)

    session.call_tool = blocked_second_close

    async def run():
        nonlocal scope
        # The outer task reads this shared scope after scope_ready is set.
        with anyio.CancelScope() as scope:  # pylint: disable=unused-variable
            scope_ready.set()
            await write(make(session), source)

    task = asyncio.create_task(run())
    await scope_ready.wait()
    while session.close_count != 2:
        await asyncio.sleep(0)
    scope.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert session.working_path.exists()
    session.close_permitted.set()
    await task
    assert scope.cancelled_caught
    assert source.read_bytes() == b"before"
    assert not commits
    assert not session.resident
    assert not session.working_path.parent.exists()


@pytest.mark.asyncio
async def test_close_deadline_retains_copy_and_reports_failure(
    transaction_client,
    monkeypatch,
):
    source, commits, make = transaction_client
    session = ResidentSession()
    session.close_permitted.clear()
    monkeypatch.setattr(
        connectors,
        "_OFFICECLI_CLOSE_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )
    watchdog = asyncio.get_running_loop().call_later(
        0.06,
        session.close_permitted.set,
    )
    try:
        with pytest.raises(TimeoutError, match="working copy retained"):
            await asyncio.wait_for(write(make(session), source), timeout=0.2)
    finally:
        watchdog.cancel()
    assert source.read_bytes() == b"before"
    assert not commits
    assert session.resident
    assert session.working_path.read_bytes() == b"after"
    assert session.close_count == 1


@pytest.mark.asyncio
async def test_unknown_mutation_outcome_retains_copy_after_close(
    transaction_client,
):
    source, commits, make = transaction_client
    session = ResidentSession(mutation="exception")
    with pytest.raises(OSError):
        await write(make(session), source)
    assert session.events == ["set", "close"]
    assert not session.resident
    assert session.working_path.read_bytes() == b"after"
    assert source.read_bytes() == b"before"
    assert not commits


@pytest.mark.asyncio
async def test_cancelled_scope_does_not_start_native_mutation(
    transaction_client,
):
    source, commits, make = transaction_client
    session = ResidentSession()
    with anyio.CancelScope() as scope:
        scope.cancel()
        await write(make(session), source)
    assert scope.cancelled_caught
    assert not session.events
    assert source.read_bytes() == b"before"
    assert not commits


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "timeout"])
async def test_cancel_with_failed_close_preserves_primary_cancel_and_copy(
    transaction_client,
    monkeypatch,
    caplog,
    failure,
):
    source, commits, make = transaction_client
    session = ResidentSession(close_failure=1 if failure == "error" else 0)
    session.close_permitted.clear()
    monkeypatch.setattr(connectors, "_OFFICECLI_CLOSE_TIMEOUT_SECONDS", 0.01)
    task = asyncio.create_task(write(make(session), source))
    await session.close_started.wait()
    task.cancel()
    if failure == "error":
        session.close_permitted.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert (
        "could not close" if failure == "error" else "request timed out"
    ) in caplog.text
    assert str(session.working_path) in caplog.text
    assert session.working_path.read_bytes() == b"after"
    assert source.read_bytes() == b"before"
    assert session.resident
    assert not commits


@pytest.mark.asyncio
async def test_timed_out_remote_write_can_finish_only_in_retained_copy(
    transaction_client,
):
    source, commits, make = transaction_client
    session = ResidentSession()
    late_permitted = asyncio.Event()
    late_task = None
    original_call = session.call_tool

    async def disconnected_call(
        name,
        arguments=None,
        read_timeout_seconds=None,
    ):
        nonlocal late_task
        if arguments["command"][0] == "set":
            await original_call(name, arguments, read_timeout_seconds)

            async def late_remote_completion():
                await late_permitted.wait()
                session.working_path.write_bytes(b"late remote write")
                session.resident = True

            late_task = asyncio.create_task(late_remote_completion())
            raise TimeoutError("SDK waiter abandoned; remote result unknown")
        return await original_call(name, arguments, read_timeout_seconds)

    session.call_tool = disconnected_call
    with pytest.raises(TimeoutError):
        await write(make(session), source)
    assert session.working_path.exists()
    late_permitted.set()
    await late_task
    assert session.working_path.read_bytes() == b"late remote write"
    assert source.read_bytes() == b"before"
    assert not commits


@pytest.mark.asyncio
async def test_read_only_call_has_no_transaction_cleanup(transaction_client):
    source, commits, make = transaction_client
    session = ResidentSession()
    result = await make(session).call_tool(
        "officecli",
        {"command": ["get", str(source), "/body/p[1]"]},
    )
    assert session.events == ["get"]
    assert source.read_bytes() == b"before"
    assert not commits
    assert len(result.content) == 1


class LateRequestSession(ResidentSession):
    def __init__(self, verb):
        super().__init__()
        self.late_verb = verb
        self.close_ack = asyncio.Event()
        self.started = asyncio.Event()
        self.late_finished = asyncio.Event()

    async def call_tool(self, name, arguments=None, read_timeout_seconds=None):
        verb = arguments["command"][0]
        result = await super().call_tool(name, arguments, read_timeout_seconds)
        if verb == self.late_verb:
            self.started.set()
            try:
                await self.close_ack.wait()
            except asyncio.CancelledError:
                # A custom protocol waiter can defer cancellation until the
                # actual remote reply; completion is later than close ACK.
                await self.close_ack.wait()
            self.resident = True
            self.late_finished.set()
            return result
        if verb == "close" and self.started.is_set():
            self.close_ack.set()
        return result


@pytest.mark.asyncio
@pytest.mark.parametrize("late_verb", ["set", "validate"])
async def test_late_owned_completion_cannot_authorize_stale_close_cleanup(
    transaction_client,
    monkeypatch,
    late_verb,
):
    source, commits, make = transaction_client
    session = LateRequestSession(late_verb)
    monkeypatch.setattr(connectors, "_OFFICECLI_REQUEST_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(connectors, "_OFFICECLI_CANCEL_DRAIN_SECONDS", 0.01)
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(write(make(session), source), timeout=0.5)
        await asyncio.wait_for(session.late_finished.wait(), timeout=0.2)
        assert source.read_bytes() == b"before"
        assert not commits
        assert (
            session.working_path.exists()
        ), "late native resident's working copy was deleted"
    finally:
        await asyncio.sleep(0)
        # This behavior lock observes ownership after the late task completes.
        assert not connectors._OFFICECLI_PENDING_REQUESTS
