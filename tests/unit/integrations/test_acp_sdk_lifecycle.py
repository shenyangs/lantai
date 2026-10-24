"""Installed ACP SDK streams through the production authenticated HTTP facade."""
import asyncio
from contextlib import asynccontextmanager, suppress
from importlib import import_module
import json
from pathlib import Path
import secrets
from types import SimpleNamespace

import acp
from acp.interfaces import Client
import httpx
import pytest
import pytest_asyncio

from lantai.app import _app as production
from lantai.app import auth
from lantai.app.workspace_grants import (
    create_workspace_selection_receipt,
    default_workspace_grant_registry,
)
from lantai.integrations.acp_server.bridge import LantaiAcpBridge
from lantai.integrations.acp_server.server import LantaiAcpAgent



async def eventually(predicate, seconds=3):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(wait(), seconds)


class SyntheticGateRunner:
    def __init__(self):
        self.requests = {}
        self.gates = {}
        self.cancelled = set()

    async def stream_query(self, request):
        key = request['metadata']['background_task_idempotency_key']
        assert key not in self.requests, 'one physical stream per submission'
        self.requests[key] = request
        gate = self.gates.setdefault(key, asyncio.Event())
        try:
            await gate.wait()
            yield {'object': 'message', 'type': 'message', 'role': 'assistant',
                   'status': 'completed', 'content': [{'type': 'text', 'text': 'fixture complete'}]}
        except asyncio.CancelledError:
            self.cancelled.add(key)
            raise


class ActualASGIReceiptFault(httpx.AsyncBaseTransport):
    """Every success and fault follows a real production route invocation."""
    def __init__(self):
        self.inner = httpx.ASGITransport(app=production.app,
            client=('203.0.113.10', 31337), raise_app_exceptions=False)
        self.mode = ''
        self.used = False
        self.post_seen = asyncio.Event()
        self.release_post = asyncio.Event()
        self.posts = []
        self.lookups = []
        self.cancel_ids = []
        self.poll_ids = []

    async def handle_async_request(self, request):
        response = await self.inner.handle_async_request(request)
        await response.aread()
        path = request.url.path
        if request.method == 'POST' and path == '/api/agent/process/task':
            assert response.status_code == 200, 'real task admission failed'
            submitted = json.loads(request.content)
            identity = response.json()['task_id']
            self.posts.append((submitted['session_id'],
                submitted['metadata']['background_task_idempotency_key'], identity))
            self.post_seen.set()
            if not self.used and self.mode in {'hold_post', 'lose_post'}:
                self.used = True
                if self.mode == 'hold_post':
                    await self.release_post.wait()
                else:
                    await response.aclose()
                    raise httpx.ReadError('synthetic accepted receipt loss', request=request)
        elif request.method == 'GET' and '/task/submission/' in path:
            self.lookups.append(path.rsplit('/', 1)[-1])
        elif request.method == 'GET' and '/agent/process/task/' in path:
            self.poll_ids.append(path.rsplit('/', 1)[-1])
            if not self.used and self.mode == 'lose_poll':
                self.used = True
                await response.aclose()
                raise httpx.ReadError('synthetic polling receipt loss', request=request)
        elif request.method == 'POST' and path.endswith('/cancel'):
            self.cancel_ids.append(path.split('/')[-2])
        return response

    async def aclose(self):
        await self.inner.aclose()


class RecordingSdkClient(Client):
    def __init__(self):
        self.update_sessions = []

    async def session_update(self, session_id, update, **kwargs):
        # Record identity only, not response text.
        self.update_sessions.append(session_id)


@asynccontextmanager
async def actual_sdk(bridge):
    agent = LantaiAcpAgent(bridge)
    observer = RecordingSdkClient()
    handlers = set()
    writers = set()

    async def serve(reader, writer):
        owned = asyncio.current_task()
        handlers.add(owned)
        writers.add(writer)
        try:
            await acp.run_agent(agent, writer, reader)
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            handlers.discard(owned)
            writers.discard(writer)

    server = await asyncio.start_server(serve, '127.0.0.1', 0)
    reader, writer = await asyncio.open_connection('127.0.0.1', server.sockets[0].getsockname()[1])
    connection = acp.connect_to_agent(observer, writer, reader)
    try:
        initialized = await asyncio.wait_for(connection.initialize(acp.PROTOCOL_VERSION), 3)
        assert initialized.protocol_version == acp.PROTOCOL_VERSION
        yield SimpleNamespace(connection=connection, agent=agent, observer=observer, writer=writer)
    finally:
        await connection.close()
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()
        server.close()
        await server.wait_closed()
        for owned_writer in list(writers):
            owned_writer.close()
        if handlers:
            done, pending = await asyncio.wait(list(handlers), timeout=1)
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)


@pytest_asyncio.fixture
async def actual_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, 'AUTH_FILE', tmp_path / 'auth.json')
    desktop = secrets.token_hex(32)
    monkeypatch.setenv('LANTAI_DESKTOP_TOKEN', desktop)
    monkeypatch.setenv('LANTAI_AUTH_ENABLED', 'true')
    monkeypatch.setattr(default_workspace_grant_registry, '_store_path', tmp_path / 'grants.json')
    service = production.agent_service
    runner = SyntheticGateRunner()
    monkeypatch.setattr(service, 'runner', runner)
    monkeypatch.setattr(service, 'active_tasks', {})
    monkeypatch.setattr(service, 'task_locks', {})
    monkeypatch.setattr(service, '_idempotent_tasks', {})
    transport = ActualASGIReceiptFault()
    client = httpx.AsyncClient(base_url='http://audit.example/api/', transport=transport,
        headers={'X-Lantai-Desktop-Token': desktop})
    response = await client.post('/auth/register', json={
        'username': 'synthetic-acp-user', 'password': 'fixture-password-only'})
    assert response.status_code == 200, 'synthetic registration failed'
    client.headers['Authorization'] = 'Bearer ' + response.json()['token']
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    receipt = create_workspace_selection_receipt(project_root=workspace,
        agent_id='default', desktop_token=desktop)
    response = await client.post('/workspace-grants/claim',
        headers={'X-Agent-Id': 'default'}, json={'selection_receipt': receipt})
    assert response.status_code == 201, 'real workspace claim failed'
    grant_id = response.json()['id']
    bridge = LantaiAcpBridge(client=client, poll_interval_seconds=0.01)
    fixture = SimpleNamespace(service=service, runner=runner, transport=transport,
        bridge=bridge, workspace=workspace, grant_id=grant_id, client=client)
    try:
        yield fixture
    finally:
        transport.release_post.set()
        for gate in runner.gates.values():
            gate.set()
        for task_id in list(service.active_tasks):
            await service.cancel_task(task_id)
        await client.aclose()


async def session(sdk, backend):
    reply = await asyncio.wait_for(sdk.connection.new_session(str(backend.workspace)), 3)
    return reply.session_id


def post_for(backend, session_id):
    posts = [item for item in backend.transport.posts if item[0] == session_id]
    assert posts
    return posts[-1]


@pytest.mark.asyncio
async def test_sdk_pending_cancel_has_exact_backend_identity_and_cross_session_isolation(actual_backend):
    f = actual_backend
    f.transport.mode = 'hold_post'
    async with actual_sdk(f.bridge) as sdk:
        a, b = await session(sdk, f), await session(sdk, f)
        assert a != b
        prompt_a = asyncio.create_task(sdk.connection.prompt(a, [acp.text_block('fixture input')]))
        await asyncio.wait_for(f.transport.post_seen.wait(), 3)
        await eventually(lambda: len(f.runner.requests) == 1)
        a_identity = post_for(f, a)
        prompt_b = asyncio.create_task(sdk.connection.prompt(b, [acp.text_block('fixture input')]))
        await eventually(lambda: len(f.transport.posts) == 2 and len(f.runner.requests) == 2)
        b_identity = post_for(f, b)
        assert a_identity[1] != b_identity[1] and a_identity[2] != b_identity[2]
        for session_id in (a, b):
            with pytest.raises(acp.RequestError):
                await sdk.connection.prompt(session_id, [acp.text_block('fixture repeated input')])
        with pytest.raises(acp.RequestError):
            await sdk.connection.prompt('unknown-session', [acp.text_block('fixture input')])
        await sdk.connection.cancel('unknown-session')
        assert len(f.transport.posts) == 2 and f.transport.cancel_ids == []
        await sdk.connection.cancel(a)
        await eventually(lambda: f.bridge._active_tasks[a].cancel_requested)
        assert f.bridge._active_tasks[a].task_id is None
        assert f.service.get_task_status(b_identity[2])['status'] == 'running'
        f.transport.release_post.set()
        result_a = await asyncio.wait_for(prompt_a, 3)
        assert result_a.stop_reason == 'cancelled'
        assert f.transport.cancel_ids == [a_identity[2]]
        assert a_identity[1] in f.runner.cancelled
        assert b_identity[1] not in f.runner.cancelled
        assert f.service.get_task_status(b_identity[2])['status'] == 'running'
        for identity in (a_identity, b_identity):
            request = f.runner.requests[identity[1]]
            assert request['session_id'] == identity[0]
            assert request['user_id'] == 'acp:' + identity[0]
            assert request['channel_meta']['workspace_grant_id'] == f.grant_id
        f.runner.gates[b_identity[1]].set()
        result_b = await asyncio.wait_for(prompt_b, 3)
        assert result_b.stop_reason == 'end_turn'
        assert sdk.observer.update_sessions == [b]
        assert f.bridge._active_tasks == {}


@pytest.mark.asyncio
async def test_sdk_accepted_lost_post_resolves_actual_submission_without_second_post(actual_backend):
    f = actual_backend
    f.transport.mode = 'lose_post'
    async with actual_sdk(f.bridge) as sdk:
        a = await session(sdk, f)
        prompt = asyncio.create_task(sdk.connection.prompt(a, [acp.text_block('fixture input')]))
        await eventually(lambda: bool(f.transport.posts) and bool(f.transport.poll_ids))
        identity = post_for(f, a)
        assert f.transport.lookups == [identity[1]]
        assert f.bridge._active_tasks[a].task_id == identity[2]
        assert len(f.runner.requests) == len(f.transport.posts) == 1
        await sdk.connection.cancel(a)
        result = await asyncio.wait_for(prompt, 3)
        assert result.stop_reason == 'cancelled'
        assert f.transport.cancel_ids == [identity[2]]
        assert f.service.get_task_status(identity[2])['status'] == 'cancelled'
        assert sdk.observer.update_sessions == []


@pytest.mark.asyncio
async def test_sdk_poll_failure_keeps_original_identity_until_actual_cancel_then_new_turn(actual_backend):
    f = actual_backend
    f.transport.mode = 'lose_poll'
    async with actual_sdk(f.bridge) as sdk:
        a = await session(sdk, f)
        with pytest.raises(acp.RequestError):
            await asyncio.wait_for(sdk.connection.prompt(a, [acp.text_block('fixture input')]), 3)
        identity = post_for(f, a)
        await eventually(lambda: bool(f.runner.requests))
        assert f.bridge._active_tasks[a].task_id == identity[2]
        assert f.service.get_task_status(identity[2])['status'] == 'running'
        with pytest.raises(acp.RequestError):
            await sdk.connection.prompt(a, [acp.text_block('fixture retry input')])
        assert len(f.transport.posts) == 1
        await sdk.connection.cancel(a)
        await eventually(lambda: a not in f.bridge._active_tasks)
        assert f.transport.cancel_ids == [identity[2]]
        assert f.service.get_task_status(identity[2])['status'] == 'cancelled'
        prompt = asyncio.create_task(sdk.connection.prompt(a, [acp.text_block('fixture next input')]))
        await eventually(lambda: len(f.transport.posts) == 2 and len(f.runner.requests) == 2)
        fresh = post_for(f, a)
        assert fresh[1] != identity[1] and fresh[2] != identity[2]
        assert f.service.get_task_status(fresh[2])['status'] == 'running'
        f.runner.gates[fresh[1]].set()
        assert (await asyncio.wait_for(prompt, 3)).stop_reason == 'end_turn'
        assert sdk.observer.update_sessions == [a]
        assert f.transport.cancel_ids == [identity[2]]


@pytest.mark.asyncio
async def test_installed_sdk_disconnect_during_accepted_pending_submission_settles_exact_task(actual_backend):
    f = actual_backend
    f.transport.mode = 'hold_post'
    async with actual_sdk(f.bridge) as sdk:
        a = await session(sdk, f)
        prompt = asyncio.create_task(sdk.connection.prompt(a, [acp.text_block('fixture input')]))
        prompt.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        await asyncio.wait_for(f.transport.post_seen.wait(), 3)
        await eventually(lambda: bool(f.runner.requests))
        identity = post_for(f, a)
        await sdk.connection.close()
        sdk.writer.close()
        await sdk.writer.wait_closed()
        await eventually(lambda: f.bridge._active_tasks[a].cancel_requested)
        f.transport.release_post.set()
        await eventually(lambda: f.service.get_task_status(identity[2])['status'] == 'cancelled')
        await eventually(lambda: a not in f.bridge._active_tasks)
        assert len(f.transport.posts) == 1
        assert f.transport.cancel_ids == [identity[2]]
        assert identity[1] in f.runner.cancelled
        assert sdk.observer.update_sessions == []
        await asyncio.gather(prompt, return_exceptions=True)


@pytest.mark.asyncio
async def test_actual_cli_eof_preserves_http_client_until_pending_backend_settlement(actual_backend, monkeypatch):
    f = actual_backend
    f.transport.mode = 'hold_post'
    cli = import_module('lantai.cli.acp_server_cmd')
    bridge_module = import_module('lantai.integrations.acp_server.bridge')
    # Use the normal own-client constructor, with only its HTTP transport isolated.
    monkeypatch.setattr(bridge_module, 'create_async_agent_api_client', lambda base_url: f.client)
    observed = {}
    finished = asyncio.Event()
    handlers = set()

    async def serve(reader, writer):
        owned = asyncio.current_task()
        handlers.add(owned)
        async def actual_byte_stream(agent):
            observed['bridge'] = agent._bridge
            assert agent._bridge._owns_client is True
            await acp.run_agent(agent, writer, reader)
        monkeypatch.setattr(cli, 'run_agent', actual_byte_stream)
        try:
            await cli._serve_acp(base_url='http://audit.example', agent_id='default',
                                 task_timeout_seconds=60)
        finally:
            finished.set()
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            handlers.discard(owned)

    server = await asyncio.start_server(serve, '127.0.0.1', 0)
    reader, writer = await asyncio.open_connection('127.0.0.1', server.sockets[0].getsockname()[1])
    observer = RecordingSdkClient()
    connection = acp.connect_to_agent(observer, writer, reader)
    prompt = None
    try:
        await asyncio.wait_for(connection.initialize(acp.PROTOCOL_VERSION), 3)
        created = await connection.new_session(str(f.workspace))
        a = created.session_id
        prompt = asyncio.create_task(connection.prompt(a, [acp.text_block('fixture input')]))
        prompt.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        await asyncio.wait_for(f.transport.post_seen.wait(), 3)
        await eventually(lambda: bool(f.runner.requests))
        identity = post_for(f, a)
        await connection.close()
        writer.close()
        await writer.wait_closed()
        await eventually(lambda: observed['bridge']._active_tasks[a].cancel_requested)
        # Receipt arrives after the SDK has cancelled its request handler.
        f.transport.release_post.set()
        await eventually(lambda: f.service.get_task_status(identity[2])['status'] == 'cancelled')
        assert f.transport.cancel_ids == [identity[2]]
        assert identity[1] in f.runner.cancelled
        assert len(f.transport.posts) == 1
        await asyncio.wait_for(finished.wait(), 3)
        assert f.client.is_closed
    finally:
        f.transport.release_post.set()
        await connection.close()
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()
        server.close()
        await server.wait_closed()
        if prompt is not None:
            await asyncio.gather(prompt, return_exceptions=True)
        if handlers:
            done, pending = await asyncio.wait(list(handlers), timeout=1)
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_repeated_cancel_of_owned_bridge_close_waits_for_actual_backend_settlement(actual_backend, monkeypatch):
    f = actual_backend
    f.transport.mode = 'hold_post'
    module = import_module('lantai.integrations.acp_server.bridge')
    monkeypatch.setattr(module, 'create_async_agent_api_client', lambda base_url: f.client)
    bridge = LantaiAcpBridge(base_url='http://audit.example', poll_interval_seconds=.01)
    workspace = await bridge.require_workspace_grant(str(f.workspace))
    prompt = asyncio.create_task(bridge.run_prompt(
        session_id='close-cancel-fixture', text='fixture input', workspace=workspace))
    await asyncio.wait_for(f.transport.post_seen.wait(), 3)
    await eventually(lambda: bool(f.runner.requests))
    identity = post_for(f, 'close-cancel-fixture')
    closing = asyncio.create_task(bridge.aclose())
    try:
        await eventually(lambda: bridge._active_tasks['close-cancel-fixture'].cancel_requested)
        closing.cancel()
        await asyncio.sleep(.02)
        closing.cancel()
        assert not f.client.is_closed and not closing.done()
        f.transport.release_post.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closing, 3)
        await asyncio.gather(prompt, return_exceptions=True)
        assert f.service.get_task_status(identity[2])['status'] == 'cancelled'
        assert f.transport.cancel_ids == [identity[2]]
        assert len(f.transport.posts) == 1
        assert bridge._active_tasks == {} and f.client.is_closed
    finally:
        f.transport.release_post.set()
        await asyncio.gather(closing, prompt, return_exceptions=True)
