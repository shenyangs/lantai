"""A recovered reflection retains its original research budget owner."""
import asyncio
from types import SimpleNamespace

import pytest

from lantai.app import post_answer_reflection_service as module
from lantai.app.utility_model import ResolvedUtilityModel
from lantai.agents.persona.post_answer_reflection import PostAnswerReflectionRequest
from lantai.research.chat import bind_chat
from lantai.research.context import research_turn, current_binding
from lantai.research.projects import ResearchService
from lantai.runtime.model_wrappers import TokenRecordingModelClient
from lantai.runtime.models import ModelMessage, ModelRequest, ModelResponse, ModelUsage


def project(home, key, calls=10):
    service = ResearchService(home)
    created = service.create_project('question', [], idempotency_key=key, budget={'model_calls': calls})
    return bind_chat(SimpleNamespace(meta={'research_project_id': created['project_id']}), home)


def request(home):
    return PostAnswerReflectionRequest(agent_id='default', workspace_dir=str(home),
        chat_id='chat', session_id='session', run_id='run', client_turn_id='turn',
        answer_message_id='answer', user_message='complete user context', final_answer='complete answer',
        plan={}, subject_context={}, need_persona=True, need_ququ=False, assistant_history=[])


@pytest.fixture
def model(monkeypatch):
    from lantai.providers import ProviderManager
    monkeypatch.setattr(ProviderManager, 'get_instance', lambda: SimpleNamespace(get_provider=lambda _: None))
    config = SimpleNamespace(mode='dedicated', background_processing_enabled=True,
                             model=SimpleNamespace(provider_id='test', model='helper'))
    monkeypatch.setattr(module, 'load_config', lambda: SimpleNamespace(utility_intelligence=config))
    calls = []
    class Raw:
        provider_id, model_name = 'test', 'helper'
        async def complete(self, value):
            calls.append(getattr(current_binding(), 'project', None))
            return ModelResponse(message=ModelMessage.text('assistant', 'fixture'), usage=ModelUsage(total_tokens=3))
    client = TokenRecordingModelClient(Raw())
    async def consume(*args, **kwargs):
        await client.complete(ModelRequest())
        return '{"attention":"model-owned fixture","carry_forward":"fixture"}'
    monkeypatch.setattr(module, 'consume_model_response', consume)
    monkeypatch.setattr(module, 'resolve_reflection_model', lambda _: ResolvedUtilityModel(model=object(), provider_id='test', model_id='helper'))
    monkeypatch.setattr(module, 'resolve_active_model', lambda _: None)
    return calls


def ledger(binding):
    return binding.service.project(binding.project)['inference']


@pytest.mark.asyncio
@pytest.mark.parametrize('caller_bound', [False, True])
async def test_recovered_job_uses_origin_agent_home(tmp_path, model, caller_bound):
    origin, caller = project(tmp_path, 'origin'), project(tmp_path, 'caller')
    topic = tmp_path / 'topics' / 'one'
    first = module.PostAnswerReflectionService(topic, agent_id='default', research_workspace=tmp_path)
    req = request(topic)
    with research_turn(origin):
        first._write_job(first._new_job(req))
    restarted = module.PostAnswerReflectionService(topic, agent_id='default', research_workspace=tmp_path)
    with research_turn(caller if caller_bound else None):
        await asyncio.gather(*restarted.recover_pending(chat_id='chat'))
        assert current_binding() is (caller if caller_bound else None)
    assert model == [origin.project]
    assert ledger(origin)['calls'] == 1 and ledger(caller)['calls'] == 0
    job = restarted._read_job(req.job_id)
    assert job['status'] == 'completed'
    assert job['request'] == req.to_dict()
    assert job['research_project_id'] == origin.project


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy', [False, True])
async def test_unassociated_job_does_not_charge_recovering_chat(tmp_path, model, legacy):
    caller = project(tmp_path, 'caller')
    service = module.PostAnswerReflectionService(tmp_path, agent_id='default')
    req = request(tmp_path)
    with research_turn(None):
        job = service._new_job(req)
    if legacy:
        job.pop('research_project_id', None)
    service._write_job(job)
    with research_turn(caller):
        await asyncio.gather(*service.recover_pending(chat_id='chat'))
    assert model == [None]
    assert ledger(caller)['calls'] == 0
    assert service._read_job(req.job_id)['status'] == 'completed'


@pytest.mark.asyncio
async def test_recovery_obeys_origin_budget_and_preserves_failed_job(tmp_path, model):
    origin, caller = project(tmp_path, 'origin', calls=1), project(tmp_path, 'caller')
    service = module.PostAnswerReflectionService(tmp_path, agent_id='default')
    req = request(tmp_path)
    with research_turn(origin):
        service._write_job(service._new_job(req))
        await module.consume_model_response()
    model.clear()
    with research_turn(caller):
        await asyncio.gather(*service.recover_pending(chat_id='chat'))
    assert model == []
    assert ledger(origin)['calls'] == 1 and ledger(caller)['calls'] == 0
    job = service._read_job(req.job_id)
    assert job['status'] == 'failed' and job['reflection'] is None
    assert job['request'] == req.to_dict()


def test_new_job_rejects_cross_agent_origin(tmp_path, model):
    origin = project(tmp_path / 'agent-one', 'origin')
    other = tmp_path / 'agent-two'
    service = module.PostAnswerReflectionService(other, agent_id='other')
    with research_turn(origin), pytest.raises(ValueError, match='workspace'):
        service._new_job(request(other))


@pytest.mark.asyncio
async def test_saved_reflection_delivers_after_origin_budget_exhausted(tmp_path, model):
    origin, caller = project(tmp_path, 'origin', calls=1), project(tmp_path, 'caller')
    service = module.PostAnswerReflectionService(tmp_path, agent_id='default')
    req = request(tmp_path)
    with research_turn(origin):
        job = service._new_job(req)
        await module.consume_model_response()
    job['reflection'] = {'attention': 'already generated', 'carry_forward': 'retained'}
    service._write_job(job)
    model.clear()
    with research_turn(caller):
        await asyncio.gather(*service.recover_pending(chat_id='chat'))
    assert model == []
    completed = service._read_job(req.job_id)
    assert completed['status'] == 'completed'
    assert completed['subject_persisted'] is True
    assert completed['reflection'] == job['reflection']
    assert ledger(origin)['calls'] == 1 and ledger(caller)['calls'] == 0


@pytest.mark.asyncio
async def test_missing_origin_project_does_not_fall_back_to_current_chat(tmp_path, model):
    caller = project(tmp_path, 'caller')
    service = module.PostAnswerReflectionService(tmp_path, agent_id='default')
    req = request(tmp_path)
    job = service._new_job(req)
    job['research_project_id'] = 'project-no-longer-present'
    service._write_job(job)
    with research_turn(caller):
        await asyncio.gather(*service.recover_pending(chat_id='chat'))
    assert model == [] and ledger(caller)['calls'] == 0
    assert service._read_job(req.job_id)['status'] == 'failed'
