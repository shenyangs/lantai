"""Embedding dispatches obey the current project, including cached/threaded use."""
import asyncio
import json
from types import SimpleNamespace

import numpy as np
import pytest

from lantai.research.chat import ResearchChatBinding
from lantai.research.context import research_turn
from lantai.research.projects import ResearchService
from lantai.retrieval.embedding import LocalOnnxEmbeddingProvider, OpenAICompatibleEmbeddingProvider


def binding(tmp_path, key='p', **budget):
    service = ResearchService(tmp_path)
    project = service.create_project('q', [], idempotency_key=key, budget=budget)
    return ResearchChatBinding(service, project['project_id'])


def state(bound):
    return bound.service.project(bound.project)['inference']


def provider(monkeypatch, local=False, fail=False):
    calls = []
    if local:
        monkeypatch.setattr(LocalOnnxEmbeddingProvider, '_load_and_verify_manifest', lambda _: {
            'model_name': 'fixture', 'dimensions': 2, 'fingerprint': 'PRIVATE-FINGERPRINT'})
        raw = LocalOnnxEmbeddingProvider('/unused')
        raw._tokenizer = SimpleNamespace(encode_batch=lambda texts: [
            SimpleNamespace(ids=[1, 2], attention_mask=[1, 1], type_ids=[0, 0]) for _ in texts])
        def run(*args):
            calls.append(True)
            if fail:
                raise RuntimeError('inference failed')
            return [np.array([[3., 4.]])]
        raw._session = SimpleNamespace(get_inputs=lambda: [], run=run)
    else:
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return None
            def read(self):
                return json.dumps({'data': [{'index': 0, 'embedding': [3., 4.]}],
                                   'usage': {'total_tokens': 7}}).encode()
        def urlopen(request, timeout):
            calls.append(timeout)
            if fail:
                raise RuntimeError('inference failed')
            return Response()
        monkeypatch.setattr('lantai.retrieval.embedding.urllib.request.urlopen', urlopen)
        raw = OpenAICompatibleEmbeddingProvider(base_url='https://PRIVATE-ENDPOINT/v1',
            model_name='fixture', dimensions=2, api_key='PRIVATE-KEY', timeout_seconds=100)
    return raw, calls


@pytest.mark.parametrize('local', [False, True])
def test_budget_stops_dispatch_and_empty_batch_costs_nothing(tmp_path, monkeypatch, local):
    raw, calls = provider(monkeypatch, local)
    bound = binding(tmp_path, model_calls=1, model_seconds=60)
    with research_turn(bound):
        assert raw.embed([]).shape == (0, 2)
        assert state(bound)['calls'] == 0
        assert np.allclose(raw.embed(['PRIVATE-TEXT']), [[.6, .8]])
        with pytest.raises(ValueError, match='budget'):
            raw.embed(['PRIVATE-TEXT'])
    assert len(calls) == 1
    result = state(bound)
    assert result['calls'] == 1 and result['surfaces'] == ['embedding']
    assert result['known_tokens'] == (2 if local else 7)
    assert result['known_cost_cny'] is None
    if not local:
        assert 0 < calls[0] <= 60
    database = bound.service.store.path.read_bytes()
    for private in (b'PRIVATE-TEXT', b'PRIVATE-KEY', b'PRIVATE-ENDPOINT', b'PRIVATE-FINGERPRINT'):
        assert private not in database


@pytest.mark.parametrize('local', [False, True])
def test_cached_provider_uses_each_thread_context_and_leaves_ordinary_calls_alone(tmp_path, monkeypatch, local):
    raw, calls = provider(monkeypatch, local)
    one, two = binding(tmp_path, 'one'), binding(tmp_path, 'two')
    async def dispatch(bound):
        with research_turn(bound):
            return await asyncio.to_thread(raw.embed, ['fixture'])
    async def run():
        await asyncio.gather(dispatch(one), dispatch(two))
    asyncio.run(run())
    raw.embed(['ordinary'])
    assert len(calls) == 3
    assert state(one)['calls'] == state(two)['calls'] == 1


@pytest.mark.parametrize('local', [False, True])
def test_failure_settles_and_current_revision_can_raise_budget(tmp_path, monkeypatch, local):
    raw, calls = provider(monkeypatch, local, fail=True)
    bound = binding(tmp_path, model_calls=1)
    with research_turn(bound):
        with pytest.raises(RuntimeError, match='inference failed'):
            raw.embed(['fixture'])
        with pytest.raises(ValueError, match='budget'):
            raw.embed(['fixture'])
        bound.service.revise_project(bound.project, 1, budget={'model_calls': 2}, idempotency_key='raise')
        with pytest.raises(RuntimeError, match='inference failed'):
            raw.embed(['fixture'])
    result = state(bound)
    assert len(calls) == 2 and result['calls'] == 2
    assert result['unknown_token_calls'] == result['unknown_cost_calls'] == 2
    assert result['seconds'] < 60


def test_elapsed_time_is_never_clipped_to_reserved_allowance(tmp_path):
    from lantai.research.inference import reserve, finish
    bound = binding(tmp_path, model_seconds=60)
    identifier, allowance = reserve(bound.service, bound.project, 1, {}, 'embedding')
    finish(bound.service, identifier, allowance + 5, 'completed', {'total_tokens': 1})
    assert state(bound)['seconds'] == 65
    with pytest.raises(ValueError, match='budget'):
        reserve(bound.service, bound.project, 1, {}, 'embedding')


def test_bundled_model_preserves_vectors_when_project_accounting_is_enabled(tmp_path):
    from lantai.retrieval.embedding import get_default_local_embedding_provider
    raw = get_default_local_embedding_provider()
    texts = ['比较两种方法的证据', '研究结论的适用范围']
    baseline = raw.embed(texts)
    bound = binding(tmp_path, model_calls=1)
    with research_turn(bound):
        actual = raw.embed(texts)
    assert np.array_equal(actual, baseline)
    result = state(bound)
    assert result['calls'] == 1 and result['known_tokens'] > 0
    assert result['unknown_token_calls'] == 0
    assert result['known_cost_cny'] is None
