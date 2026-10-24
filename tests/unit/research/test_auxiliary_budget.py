"""Cached inference must obey the trusted research turn at dispatch time."""
import asyncio
from types import SimpleNamespace
import pytest
from lantai.research.chat import bind_chat
from lantai.research.projects import ResearchService
from lantai.runtime.models import ModelRequest, ModelResponse, ModelMessage, ModelUsage, ModelStreamEvent

@pytest.fixture(autouse=True)
def providers(monkeypatch):
    from lantai.providers import ProviderManager
    monkeypatch.setattr(ProviderManager, 'get_instance', lambda: SimpleNamespace(get_provider=lambda _: None))

def binding(tmp_path, key='p', calls=10):
    service = ResearchService(tmp_path)
    project = service.create_project('q', [], idempotency_key=key, budget={'model_calls': calls})
    return bind_chat(SimpleNamespace(meta={'research_project_id': project['project_id']}), tmp_path)

def state(bound):
    return bound.service.project(bound.project)['inference']

class Raw:
    provider_id = 'test'
    model_name = 'model'
    calls = 0
    closed = False
    async def complete(self, request):
        self.calls += 1
        return ModelResponse(message=ModelMessage.text('assistant', 'PRIVATE'), usage=ModelUsage(total_tokens=3))
    async def stream(self, request):
        self.calls += 1
        try:
            yield ModelStreamEvent(kind='text_delta', delta='PRIVATE', usage=ModelUsage(total_tokens=3))
            yield ModelStreamEvent(kind='text_delta', delta='tail')
        finally:
            self.closed = True
    async def __call__(self, **kwargs):
        if kwargs.pop('stream', False):
            return self.stream(ModelRequest())
        return await self.complete(ModelRequest())

@pytest.mark.parametrize('legacy', [False, True])
def test_cached_dispatch_budget_and_unbound_behavior(tmp_path, legacy):
    from lantai.research.context import research_turn
    from lantai.runtime.model_wrappers import TokenRecordingModelClient
    from lantai.token_usage.model_wrapper import TokenRecordingModelWrapper
    raw = Raw()
    cached = TokenRecordingModelWrapper('test', raw) if legacy else TokenRecordingModelClient(raw)
    bound = binding(tmp_path, calls=1)
    async def call():
        return await cached(messages=[]) if legacy else await cached.complete(ModelRequest())
    async def run():
        await call()
        with research_turn(bound):
            await call()
            with pytest.raises(ValueError, match='budget'):
                await call()
        await call()
    asyncio.run(run())
    assert raw.calls == 3
    assert state(bound)['calls'] == 1 and state(bound)['known_tokens'] == 3
    assert state(bound)['surfaces'] == ['auxiliary_text']

@pytest.mark.parametrize('legacy', [False, True])
def test_explicit_main_dispatch_is_not_double_charged(tmp_path, legacy):
    from lantai.research.context import research_turn
    from lantai.runtime.model_wrappers import TokenRecordingModelClient
    from lantai.token_usage.model_wrapper import TokenRecordingModelWrapper
    raw, bound = Raw(), binding(tmp_path, calls=1)
    client = bound.wrap_legacy(TokenRecordingModelWrapper('test', raw)) if legacy else bound.wrap_native(TokenRecordingModelClient(raw))
    async def run():
        with research_turn(bound):
            if legacy:
                await client(messages=[])
            else:
                await client.complete(ModelRequest())
    asyncio.run(run())
    assert raw.calls == 1 and state(bound)['calls'] == 1
    assert state(bound)['surfaces'] == ['main_chat']

@pytest.mark.parametrize('legacy', [False, True])
def test_partial_cached_stream_settles_and_closes_provider(tmp_path, legacy):
    from lantai.research.context import research_turn
    from lantai.runtime.model_wrappers import TokenRecordingModelClient
    from lantai.token_usage.model_wrapper import TokenRecordingModelWrapper
    raw, bound = Raw(), binding(tmp_path)
    client = TokenRecordingModelWrapper('test', raw) if legacy else TokenRecordingModelClient(raw)
    async def run():
        with research_turn(bound):
            stream = await client(messages=[], stream=True) if legacy else client.stream(ModelRequest())
            await anext(stream)
            await stream.aclose()
            assert raw.closed
            assert state(bound)['unknown_token_calls'] == 1
    asyncio.run(run())
    assert state(bound)['calls'] == 1 and state(bound)['known_tokens'] == 3

def test_project_context_isolated_between_tasks_and_reset_after_failure(tmp_path):
    from lantai.research.context import research_turn, current_binding
    from lantai.runtime.model_wrappers import TokenRecordingModelClient
    one, two = binding(tmp_path, 'one'), binding(tmp_path, 'two')
    cached = TokenRecordingModelClient(Raw())
    async def dispatch(bound):
        with research_turn(bound):
            await asyncio.sleep(0)
            await cached.complete(ModelRequest())
            assert current_binding() is bound
    async def run():
        await asyncio.gather(dispatch(one), dispatch(two))
        with pytest.raises(RuntimeError):
            with research_turn(one):
                raise RuntimeError('turn failed')
        assert current_binding() is None
    asyncio.run(run())
    assert state(one)['calls'] == state(two)['calls'] == 1


@pytest.mark.parametrize('legacy', [False, True])
def test_explicit_stream_does_not_leak_accounting_suppression_to_consumer(tmp_path, legacy):
    from lantai.research.context import research_turn
    from lantai.runtime.model_wrappers import TokenRecordingModelClient
    from lantai.token_usage.model_wrapper import TokenRecordingModelWrapper
    one, two = binding(tmp_path, 'one'), binding(tmp_path, 'two')
    raw = Raw()
    cached = TokenRecordingModelWrapper('test', raw) if legacy else TokenRecordingModelClient(raw)
    client = one.wrap_legacy(cached) if legacy else one.wrap_native(cached)
    async def run():
        with research_turn(one):
            stream = await client(messages=[], stream=True) if legacy else client.stream(ModelRequest())
            await anext(stream)
            with research_turn(two):
                if legacy:
                    await cached(messages=[])
                else:
                    await cached.complete(ModelRequest())
            await stream.aclose()
    asyncio.run(run())
    assert state(one)['calls'] == state(two)['calls'] == 1
    assert state(one)['surfaces'] == ['main_chat']
    assert state(two)['surfaces'] == ['auxiliary_text']
    assert raw.closed


def test_cancelled_auxiliary_dispatch_settles_reservation(tmp_path):
    from lantai.research.context import research_turn
    from lantai.runtime.model_wrappers import TokenRecordingModelClient
    bound = binding(tmp_path)
    closed = []
    async def run():
        entered = asyncio.Event()
        class Slow(Raw):
            async def complete(self, request):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    closed.append(True)
        with research_turn(bound):
            task = asyncio.create_task(TokenRecordingModelClient(Slow()).complete(ModelRequest()))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    asyncio.run(run())
    assert closed == [True]
    assert state(bound)['calls'] == state(bound)['unknown_token_calls'] == 1
    with bound.service.store.transaction() as db:
        assert db.execute('SELECT status FROM research_model_calls').fetchone()['status'] == 'interrupted'
