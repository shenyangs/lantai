"""Interrupted research review can resume without duplicate committed reviews."""
import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from lantai.agents.tool_registry import build_lantai_tool_registry
from lantai.research.review import review_report
from lantai.runtime.effects import assess_runtime_effect_ambiguity
from tests.unit.research.test_review_v2 import IDENTITY, setup, result


@pytest.mark.asyncio
@pytest.mark.parametrize('interrupted', [False, True])
async def test_configured_prompt_change_invalidates_completed_and_partial_reviews(tmp_path, monkeypatch, interrupted):
    from lantai import providers
    from lantai.agents import model_factory
    from lantai.research import crossrefs
    from lantai.research.review import configured_review
    from lantai.runtime.models import ModelMessage, ModelResponse

    service, p, report = setup(tmp_path)
    calls = []

    class Client:
        provider_id = 'fixture'
        model_name = 'fixture'

        async def complete(self, request):
            payload = json.loads(request.messages[1].text_content())
            calls.append((request.messages[0].text_content(), payload.get('phase', 'full_scan')))
            if interrupted and len(calls) == 2:
                raise asyncio.CancelledError()
            value = {**result(payload, importance='critical'), 'references': []}
            return ModelResponse(message=ModelMessage.text('assistant', json.dumps(value)))

    provider = SimpleNamespace(base_url='http://localhost:1234', get_effective_generate_kwargs=lambda _: {})
    monkeypatch.setattr(providers, 'ProviderManager', lambda: SimpleNamespace(get_provider=lambda _: provider))
    monkeypatch.setattr(model_factory, 'create_model_client', lambda *args, **kwargs: Client())
    slot = SimpleNamespace(provider_id='fixture', model='fixture')

    async def invoke():
        return await configured_review(service, p['project_id'], 1, report['id'], 'fixture', model_slot=slot)

    if interrupted:
        with pytest.raises(asyncio.CancelledError):
            await invoke()
        old = None
    else:
        old = await invoke()
        assert (await invoke())['id'] == old['id']
    assert len(calls) == 2
    old_parts = {part['id'] for part in service.store.list('reviewpart', p['project_id'])}

    # Change an actual appended instruction without changing model, report or inputs.
    monkeypatch.setattr(crossrefs, 'PROMPT', crossrefs.PROMPT + ' Synthetic revised review instruction.')
    current = await invoke()
    assert len(calls) == 4
    assert [phase for _, phase in calls] == ['full_scan', 'critical_recheck'] * 2
    assert old_parts.isdisjoint(current['checkpoint_ids'])
    if old:
        assert current['id'] != old['id']
    assert current['identity']['prompt_version'] == hashlib.sha256(calls[-1][0].encode()).hexdigest()
    assert calls[0][0] != calls[-1][0]
    usage = service.project(p['project_id'])['budget_usage']
    repeated = await invoke()
    assert repeated['id'] == current['id'] and len(calls) == 4
    assert service.project(p['project_id'])['budget_usage'] == usage


def test_only_builtin_review_is_resumable():
    config = SimpleNamespace(tools=SimpleNamespace(builtin={}))
    registry = build_lantai_tool_registry(agent_config=config, request_context={},
        include_default_policy_middleware=False)
    recovery = registry.get_tool('research_review')[0].effect_recovery
    assert recovery.external_side_effect
    assert recovery.idempotency == 'natural_key'
    assert assess_runtime_effect_ambiguity(recovery).automatic_external_call_allowed
    async def replacement():
        pass
    overridden = build_lantai_tool_registry(agent_config=config, request_context={},
        include_default_policy_middleware=False, is_coding_mode_enabled_fn=lambda _: True,
        coding_tool_functions_factory=lambda _: {'research_review': replacement})
    assert not assess_runtime_effect_ambiguity(
        overridden.get_tool('research_review')[0].effect_recovery).automatic_external_call_allowed


@pytest.mark.asyncio
async def test_cancel_resume_and_repeat_preserve_checkpoints_budget_and_single_review(tmp_path):
    service, p, report = setup(tmp_path)
    phases = []
    async def interrupted(payload):
        phases.append(payload.get('phase', 'full_scan'))
        if payload.get('phase') == 'critical_recheck':
            raise asyncio.CancelledError()
        return result(payload, importance='critical')
    async def invoke(reviewer, identity=IDENTITY):
        return await review_report(service, p['project_id'], 1, report['id'], reviewer,
            reviewer_identity=identity)
    with pytest.raises(asyncio.CancelledError):
        await invoke(interrupted)
    assert service.project(p['project_id'])['review_id'] is None
    assert len(service.store.list('reviewpart', p['project_id'])) == 1
    async def resumed(payload):
        phases.append(payload.get('phase', 'full_scan'))
        return result(payload, importance='critical')
    first = await invoke(resumed)
    usage = service.project(p['project_id'])['budget_usage']
    repeated = await invoke(resumed)
    assert repeated['id'] == first['id']
    assert len(service.store.list('review', p['project_id'])) == 1
    assert service.project(p['project_id'])['budget_usage'] == usage
    assert phases == ['full_scan', 'critical_recheck', 'critical_recheck']
    # A different reviewer must perform a fresh scan, not inherit success.
    changed = await invoke(resumed, {**IDENTITY, 'model': 'second'})
    assert changed['id'] != first['id']
    assert phases[-2:] == ['full_scan', 'critical_recheck']


@pytest.mark.asyncio
async def test_review_after_more_reading_does_not_reuse_incomplete_result(tmp_path):
    from lantai.research.projects import ResearchService
    root = tmp_path / 'wiki/raw/sources'
    root.mkdir(parents=True)
    (root / 'a.txt').write_text('The first trial improved outcomes.')
    (root / 'b.txt').write_text('The second trial found no improvement.')
    service = ResearchService(tmp_path)
    p = service.create_project('Compare', ['wiki/raw/sources/a.txt', 'wiki/raw/sources/b.txt'],
        idempotency_key='create')
    sid, other = p['source_versions']
    service.read_source(p['project_id'], 1, sid, 0)
    report = service.save_report(p['project_id'], 1, 'The first trial improved outcomes.',
        [{'text': 'The first trial improved outcomes.', 'evidence': [
            {'source_version_id': sid, 'block_index': 0}]}], idempotency_key='report')
    async def reviewer(payload):
        return result(payload)
    first = await review_report(service, p['project_id'], 1, report['id'], reviewer,
        reviewer_identity=IDENTITY)
    assert first['status'] == 'needs_revision'
    service.read_source(p['project_id'], 1, other, 0)
    second = await review_report(service, p['project_id'], 1, report['id'], reviewer,
        reviewer_identity=IDENTITY)
    assert second['id'] != first['id']
    assert second['status'] == 'reviewed'


@pytest.mark.asyncio
async def test_native_retry_keeps_unknown_usage_but_does_not_require_human(tmp_path, monkeypatch):
    from lantai.agents.tools import research
    from tests.unit.runtime.test_native_runtime import ScriptedModelClient, _completed, _runtime_input
    from lantai.runtime.adapters.native_runtime import NativeLantaiRuntime
    from lantai.runtime.effects import RuntimeEffectJournal
    from lantai.runtime.models import ModelStreamEvent, ModelResponse, ModelMessage
    from lantai.runtime.tools import ToolCall, LantaiToolRegistry
    service, p, report = setup(tmp_path)
    monkeypatch.setattr(research, '_service', lambda: service)
    calls = 0
    async def configured(*args):
        async def reviewer(payload):
            nonlocal calls
            calls += 1
            value = result(payload)
            if calls == 1:
                value['claims'][0]['text'] = 'Invalid exact anchor'
            return value
        return await review_report(service, p['project_id'], 1, report['id'], reviewer,
            reviewer_identity=IDENTITY)
    monkeypatch.setattr(research, 'configured_review', configured)
    full = build_lantai_tool_registry(agent_config=SimpleNamespace(tools=SimpleNamespace(builtin={})),
        request_context={}, include_default_policy_middleware=False)
    registry = LantaiToolRegistry()
    registry.register_tool(*full.get_tool('research_review'))
    arguments = dict(project_id=p['project_id'], revision=1, report_id=report['id'])
    def call(identifier):
        return [ModelStreamEvent(kind='tool_call_completed', tool_call=ToolCall(
            id=identifier, name='research_review', arguments=arguments)),
            ModelStreamEvent(kind='message_completed', response=ModelResponse(message=ModelMessage(role='assistant')))]
    client = ScriptedModelClient([call('failed'), call('retry'), [_completed('Reviewed.', message_id='final')]])
    runtime = NativeLantaiRuntime(model_client_factory=lambda _: client, tool_registry=registry,
        effect_journal=RuntimeEffectJournal(tmp_path))
    events = [event async for event in runtime.run_turn(_runtime_input())]
    completed = [event for event in events if event.type == 'tool.call.completed']
    assert len(completed) == 2
    metadata = completed[0].payload['metadata']
    assert metadata['side_effect_outcome'] == 'unknown'
    assert metadata['automatic_retry_allowed'] is True
    assert metadata['requires_human'] is False
    assert completed[1].payload['metadata']['side_effect_outcome'] == 'committed'
    assert service.project(p['project_id'])['budget_usage']['review_calls'] == 2
    assert len(service.store.list('review', p['project_id'])) == 1
