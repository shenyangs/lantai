import asyncio
from types import SimpleNamespace

import pytest

from lantai.research.projects import ResearchService
from lantai.runtime.models import ModelRequest, ModelResponse, ModelMessage, ModelUsage


@pytest.fixture(autouse=True)
def no_real_providers(monkeypatch):
    from lantai.providers import ProviderManager
    monkeypatch.setattr(ProviderManager, 'get_instance', lambda: SimpleNamespace(get_provider=lambda _: None))


def test_binding_uses_server_chat_and_current_revision(tmp_path):
    from lantai.research.chat import bind_chat
    service = ResearchService(tmp_path)
    p = service.create_project('q', [], idempotency_key='p', budget={'model_calls': 1})
    assert bind_chat(None, tmp_path) is None
    assert bind_chat(SimpleNamespace(meta={}), tmp_path) is None
    binding = bind_chat(SimpleNamespace(meta={'research_project_id': p['project_id']}), tmp_path)
    class Client:
        provider_id = 'test'
        model_name = 'test'
        calls = 0
        async def complete(self, request):
            self.calls += 1
            return ModelResponse(message=ModelMessage.text('assistant', 'private'), usage=ModelUsage(total_tokens=4))
    raw = Client()
    client = binding.wrap_native(raw)
    asyncio.run(client.complete(ModelRequest()))
    with pytest.raises(ValueError, match='budget'):
        asyncio.run(client.complete(ModelRequest()))
    assert raw.calls == 1
    revised = service.revise_project(p['project_id'], 1, budget={'model_calls': 2}, idempotency_key='raise')
    asyncio.run(client.complete(ModelRequest()))
    with service.store.transaction() as db:
        rows = db.execute('SELECT revision,surface FROM research_model_calls ORDER BY started').fetchall()
    assert [r['revision'] for r in rows] == [1, revised['revision']]
    assert {r['surface'] for r in rows} == {'main_chat'}


def test_cross_workspace_and_offline_fail_before_dispatch(tmp_path):
    from lantai.research.chat import bind_chat
    s = ResearchService(tmp_path / 'a')
    p = s.create_project('q', [], idempotency_key='p')
    with pytest.raises((FileNotFoundError, ValueError)):
        bind_chat(SimpleNamespace(meta={'research_project_id': p['project_id']}), tmp_path / 'b')
    offline = s.create_project('q', [], policy='offline', idempotency_key='offline')
    with pytest.raises(ValueError, match='Offline'):
        bind_chat(SimpleNamespace(meta={'research_project_id': offline['project_id']}), tmp_path / 'a')


def test_legacy_reserves_before_await_and_settles_closed_stream(tmp_path):
    from lantai.research.chat import bind_chat
    s = ResearchService(tmp_path)
    p = s.create_project('q', [], idempotency_key='p', budget={'model_calls': 1})
    binding = bind_chat(SimpleNamespace(meta={'research_project_id': p['project_id']}), tmp_path)
    class Legacy:
        _provider_id = 'test'
        model_name = 'test'
        calls = 0
        closed = False
        async def __call__(self, *args, **kwargs):
            self.calls += 1
            async def chunks():
                try:
                    yield SimpleNamespace(usage=ModelUsage(total_tokens=8))
                    yield SimpleNamespace(usage=None)
                finally:
                    self.closed = True
            return chunks()
    raw = Legacy()
    model = binding.wrap_legacy(raw)
    async def partial():
        stream = await model(messages=[{'role':'user', 'content':'PRIVATE'}])
        await anext(stream)
        await stream.aclose()
        with pytest.raises(ValueError, match='budget'):
            await model(messages=[])
    asyncio.run(partial())
    assert raw.calls == 1 and raw.closed
    data = s.project(p['project_id'])['inference']
    assert data['known_tokens'] == 8 and data['unknown_token_calls'] == 1
    assert b'PRIVATE' not in s.store.path.read_bytes()


def test_native_terminal_response_usage_is_not_lost(tmp_path):
    from lantai.research.chat import bind_chat
    from lantai.runtime.models import ModelStreamEvent
    s = ResearchService(tmp_path)
    p = s.create_project('q', [], idempotency_key='p')
    class Native:
        provider_id = 'test'
        model_name = 'test'
        async def stream(self, request):
            yield ModelStreamEvent(kind='message_completed', response=ModelResponse(
                message=ModelMessage.text('assistant', 'private'), usage=ModelUsage(total_tokens=13)))
    async def run():
        return [event async for event in bind_chat(SimpleNamespace(meta={'research_project_id': p['project_id']}), tmp_path).wrap_native(Native()).stream(ModelRequest())]
    asyncio.run(run())
    assert s.project(p['project_id'])['inference']['known_tokens'] == 13


@pytest.mark.parametrize('routing', [False, True])
def test_factory_wraps_each_selected_provider_without_replacing_routing(tmp_path, monkeypatch, routing):
    from lantai.agents import model_factory
    from lantai.config.config import AgentsLLMRoutingConfig
    from lantai.research.chat import bind_chat
    from lantai.runtime.models import ModelStreamEvent
    s = ResearchService(tmp_path)
    p = s.create_project('q', [], idempotency_key='p')
    primary = SimpleNamespace(provider_id='primary', model='one')
    fallback = SimpleNamespace(provider_id='fallback', model='two')
    cfg = SimpleNamespace(llm_routing=None)
    if routing:
        cfg.llm_routing = AgentsLLMRoutingConfig(enabled=True, mode='cloud_first',
            local={'provider_id':'primary', 'model':'one'}, cloud={'provider_id':'fallback', 'model':'two'})
    monkeypatch.setattr(model_factory, '_agent_retry_configs', lambda _: (cfg, None, None))
    monkeypatch.setattr(model_factory, 'resolve_effective_model_slot', lambda *a, **k: primary)
    monkeypatch.setattr(model_factory.ProviderManager, 'get_instance', lambda: SimpleNamespace(
        get_provider=lambda _: None, get_model_fallback_slot=lambda _: fallback))
    calls = []
    class Client:
        def __init__(self, slot):
            self.provider_id, self.model_name = slot.provider_id, slot.model
        async def complete(self, request):
            calls.append(self.provider_id)
            return ModelResponse(message=ModelMessage.text('assistant', 'private'), usage=ModelUsage(total_tokens=7))
        async def stream(self, request):
            result = await self.complete(request)
            yield ModelStreamEvent(kind='message_completed', response=result)
    monkeypatch.setattr(model_factory, '_model_client_for_slot', Client)
    # Retain the production retry/usage wrapper; its independent token sink is not under test.
    monkeypatch.setattr(model_factory, 'TokenRecordingModelClient', lambda c: c)
    bound = bind_chat(SimpleNamespace(meta={'research_project_id': p['project_id']}), tmp_path)
    client = model_factory.create_model_client('default', dispatch_wrapper=bound.wrap_native)
    async def run():
        return [e async for e in client.stream(ModelRequest(messages=[ModelMessage.text('user', 'q')]))]
    asyncio.run(run())
    expected = 'fallback' if routing else 'primary'
    assert calls == [expected]
    configs = s.project(p['project_id'])['inference']['configurations']
    assert len(configs) == 1 and configs[0]['provider'] == expected
    assert s.project(p['project_id'])['inference']['known_tokens'] == 7
