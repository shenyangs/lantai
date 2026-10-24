"""Model dispatch limits and configuration evidence survive failures/restarts."""
import asyncio

import pytest

from lantai.research.projects import ResearchService
from lantai.runtime.models import ModelMessage, ModelRequest, ModelResponse, ModelUsage


def setup(tmp_path, **budget):
    service = ResearchService(tmp_path)
    project = service.create_project('Research', [], idempotency_key='new', budget=budget)
    return service, project


class Client:
    provider_id = 'test'
    model_name = 'model'

    def __init__(self, usage=None):
        self.calls = 0
        self.usage = usage

    async def complete(self, request):
        self.calls += 1
        return ModelResponse(message=ModelMessage.text('assistant', 'private response'), usage=self.usage)


def test_calls_are_reserved_before_dispatch_and_durable(tmp_path):
    from lantai.research.inference import ResearchModelClient
    service, p = setup(tmp_path, model_calls=1)
    raw = Client(ModelUsage(input_tokens=12, output_tokens=3, total_tokens=15, cost_cny=0))
    client = ResearchModelClient(raw, service, p['project_id'], 1, surface='review')
    asyncio.run(client.complete(ModelRequest()))
    with pytest.raises(ValueError, match='budget'):
        asyncio.run(client.complete(ModelRequest()))
    assert raw.calls == 1
    summary = ResearchService(tmp_path).project(p['project_id'])['inference']
    assert summary['calls'] == 1 and summary['known_tokens'] == 15
    assert summary['known_cost_cny'] == 0 and summary['unknown_cost_calls'] == 0


def test_configuration_versions_are_allowlisted_and_content_addressed(tmp_path):
    from lantai.research.inference import ResearchModelClient
    service, p = setup(tmp_path)
    client = ResearchModelClient(Client(), service, p['project_id'], 1, surface='review')
    for temperature in (0.2, 0.2, 0.7):
        asyncio.run(client.complete(ModelRequest(generation={'temperature': temperature, 'api_key': 'SECRET'},
            metadata={'prompt': 'PRIVATE'})))
    configs = service.project(p['project_id'])['inference']['configurations']
    assert len(configs) == 2
    assert sorted(c['generation']['temperature'] for c in configs) == [0.2, 0.7]
    assert 'SECRET' not in service.store.path.read_bytes().decode('latin1')
    assert 'private response' not in service.store.path.read_bytes().decode('latin1')
    assert service.project(p['project_id'])['inference']['unknown_token_calls'] == 3
    assert service.project(p['project_id'])['inference']['known_cost_cny'] is None


def test_token_limit_stops_next_call_without_fabricating_hard_billing_cap(tmp_path):
    from lantai.research.inference import ResearchModelClient
    service, p = setup(tmp_path, model_tokens=10)
    raw = Client(ModelUsage(input_tokens=12, output_tokens=3, total_tokens=15))
    client = ResearchModelClient(raw, service, p['project_id'], 1, surface='review')
    asyncio.run(client.complete(ModelRequest()))
    with pytest.raises(ValueError, match='budget'):
        asyncio.run(client.complete(ModelRequest()))
    assert raw.calls == 1
    summary = service.project(p['project_id'])['inference']
    assert summary['known_tokens'] == 15 and summary['unknown_cost_calls'] == 1
    revised = service.revise_project(p['project_id'], 1, budget={'model_tokens': 30}, idempotency_key='raise')
    asyncio.run(ResearchModelClient(raw, service, p['project_id'], revised['revision'], surface='review').complete(ModelRequest()))
    assert raw.calls == 2


def test_failure_and_stream_close_record_unknown_remote_tail(tmp_path):
    from lantai.research.inference import ResearchModelClient
    from lantai.runtime.models import ModelStreamEvent
    service, p = setup(tmp_path)

    class Broken(Client):
        async def complete(self, request):
            raise RuntimeError('failed')
        async def stream(self, request):
            yield ModelStreamEvent(kind='text_delta', delta='private', usage=ModelUsage(total_tokens=9))
            raise RuntimeError('failed stream')

    client = ResearchModelClient(Broken(), service, p['project_id'], 1, surface='chat')
    with pytest.raises(RuntimeError):
        asyncio.run(client.complete(ModelRequest()))

    async def partial():
        stream = client.stream(ModelRequest())
        await anext(stream)
        await stream.aclose()
    asyncio.run(partial())
    summary = service.project(p['project_id'])['inference']
    assert summary['calls'] == 2 and summary['unknown_token_calls'] == 2
    assert summary['known_tokens'] == 9 and summary['unknown_cost_calls'] == 2


def test_unfinished_reservation_holds_time_and_wrong_revision_never_calls(tmp_path):
    from lantai.research.inference import reserve, ResearchModelClient
    service, p = setup(tmp_path)
    raw = Client()
    with pytest.raises(ValueError, match='revision'):
        asyncio.run(ResearchModelClient(raw, service, p['project_id'], 2, surface='chat').complete(ModelRequest()))
    assert raw.calls == 0
    reserve(service, p['project_id'], 1, {'provider': 'test', 'model': 'model', 'generation': {}}, 'chat')
    with pytest.raises(ValueError, match='budget'):
        asyncio.run(ResearchModelClient(raw, ResearchService(tmp_path), p['project_id'], 1, surface='chat').complete(ModelRequest()))
    assert raw.calls == 0


def test_endpoint_and_default_generation_changes_change_config_version(tmp_path):
    from lantai.research.inference import ResearchModelClient
    service, p = setup(tmp_path)
    for temperature, endpoint in ((0.2, 'https://private.example/key'), (0.7, 'https://private.example/key'),
                                  (0.7, 'https://new.example/key')):
        client = ResearchModelClient(Client(), service, p['project_id'], 1, surface='review',
            generation={'temperature': temperature, 'secret': 'SECRET'}, endpoint=endpoint)
        asyncio.run(client.complete(ModelRequest()))
    configs = service.project(p['project_id'])['inference']['configurations']
    assert len(configs) == 3
    assert 'private.example' not in service.store.path.read_bytes().decode('latin1')
    assert 'SECRET' not in service.store.path.read_bytes().decode('latin1')


def test_timeout_closes_dispatch_and_preserves_ambiguous_cost(tmp_path, monkeypatch):
    import lantai.research.inference as inference
    service, p = setup(tmp_path)
    original = inference.reserve
    def short(*args, **kwargs):
        identifier, _ = original(*args, **kwargs)
        return identifier, .02
    monkeypatch.setattr(inference, 'reserve', short)
    closed = []
    class Slow(Client):
        async def complete(self, request):
            try:
                await asyncio.sleep(5)
            finally:
                closed.append(True)
    client = inference.ResearchModelClient(Slow(), service, p['project_id'], 1, surface='review')
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(client.complete(ModelRequest()))
    state = service.project(p['project_id'])['inference']
    assert closed == [True] and state['unknown_cost_calls'] == 1
    assert state['seconds'] < 1


@pytest.mark.parametrize('sanitized', [False, True])
def test_backup_preserves_model_reservations_and_configuration(tmp_path, sanitized):
    import zipfile
    from types import SimpleNamespace
    from lantai.backup._ops.create_helpers import add_agent_workspaces
    from lantai.research.inference import reserve
    workspace = tmp_path / 'original'
    service, p = setup(workspace)
    reserve(service, p['project_id'], 1, {'provider': 'test', 'model': 'model', 'generation': {}}, 'chat')
    archive = tmp_path / 'backup.zip'
    with zipfile.ZipFile(archive, 'w') as output:
        assert add_agent_workspaces(output, [('researcher', SimpleNamespace(workspace_dir=workspace))], sanitized=sanitized)
    restored = tmp_path / 'restored'
    with zipfile.ZipFile(archive) as saved:
        index = next(name for name in saved.namelist() if name.endswith('.lantai/research/index.sqlite3'))
        path = restored / '.lantai/research/index.sqlite3'
        path.parent.mkdir(parents=True)
        path.write_bytes(saved.read(index))
    state = ResearchService(restored).project(p['project_id'])['inference']
    assert state['calls'] == 1 and state['seconds'] == 3600
    assert state['unknown_cost_calls'] == 1 and state['unknown_token_calls'] == 1
    assert len(state['configurations']) == 1
