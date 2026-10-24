"""Report validation rollback must not leave an ambiguous delivery effect."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from lantai.agents.tools import research
from lantai.agents.tool_registry import build_lantai_tool_registry
from lantai.config.context import WorkspaceContext, current_workspace_context
from lantai.research.projects import ResearchService


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['invalid_claim', 'stale_revision', 'after_insert'])
async def test_rejected_report_proves_rollback_and_corrected_save_can_retry(tmp_path, monkeypatch, failure):
    service = ResearchService(tmp_path)
    project = service.create_project('Q', [], idempotency_key='p')
    project_id = project['project_id']
    monkeypatch.setattr(research, '_service', lambda: service)
    original_remember = service.store.remember
    if failure == 'after_insert':
        def reject_after_insert(*args, **kwargs):
            raise ValueError('Rejected before transaction commit')
        monkeypatch.setattr(service.store, 'remember', reject_after_insert)
    args = dict(project_id=project_id, revision=2 if failure == 'stale_revision' else 1,
        text='Original draft.', claims=[{'text': 'Absent claim.', 'evidence': []}] if failure == 'invalid_claim' else [],
        idempotency_key='r')
    result = await research.research_report(**args)
    assert result.is_error
    assert result.metadata['side_effect_outcome'] == 'known_not_applied'
    assert service.project(project_id)['report_id'] is None
    with service.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM records WHERE kind='report'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM requests WHERE key='r'").fetchone()[0] == 0
    monkeypatch.setattr(service.store, 'remember', original_remember)
    args.update(revision=1, claims=[])
    saved = await research.research_report(**args)
    assert not saved.is_error
    repeated = await research.research_report(**args)
    assert json.loads(saved.content[0]['text'])['id'] == json.loads(repeated.content[0]['text'])['id']


@pytest.mark.asyncio
@pytest.mark.parametrize('exception', [OSError('disk failure'), asyncio.CancelledError()])
async def test_uncertain_failure_or_cancel_does_not_claim_no_effect(tmp_path, monkeypatch, exception):
    service = ResearchService(tmp_path)
    project = service.create_project('Q', [], idempotency_key='p')
    monkeypatch.setattr(research, '_service', lambda: service)
    def uncertain(*args, **kwargs):
        raise exception
    monkeypatch.setattr(service, 'save_report', uncertain)
    with pytest.raises(type(exception)):
        await research.research_report(project['project_id'], 1, 'Draft.', [], 'r')


@pytest.mark.asyncio
async def test_response_serialization_after_commit_is_not_reported_as_rollback(tmp_path, monkeypatch):
    service = ResearchService(tmp_path)
    project = service.create_project('Q', [], idempotency_key='p')
    monkeypatch.setattr(research, '_service', lambda: service)
    def fail_response(value):
        raise ValueError('serialization failed after commit')
    monkeypatch.setattr(research, '_response', fail_response)
    with pytest.raises(ValueError, match='after commit'):
        await research.research_report(project['project_id'], 1, 'Draft.', [], 'r')
    assert service.project(project['project_id'])['report_id']


def test_only_builtin_report_can_attest_effect_settlement():
    config = SimpleNamespace(tools=SimpleNamespace(builtin={}))
    registry = build_lantai_tool_registry(agent_config=config, request_context={}, include_default_policy_middleware=False)
    spec = registry.get_tool('research_report')[0]
    assert spec.metadata['runtime_owned'] is True
    assert spec.metadata['trusted_effect_settlement'] is True
    async def replacement():
        pass
    overridden = build_lantai_tool_registry(agent_config=config, request_context={},
        include_default_policy_middleware=False, is_coding_mode_enabled_fn=lambda _: True,
        coding_tool_functions_factory=lambda _: {'research_report': replacement})
    assert overridden.get_tool('research_report')[0].origin == 'coding'
    assert not overridden.get_tool('research_report')[0].metadata.get('trusted_effect_settlement')


@pytest.mark.asyncio
async def test_native_report_rejection_then_correction_settles_failed_then_committed(tmp_path):
    from tests.unit.runtime.test_native_runtime import ScriptedModelClient, _completed, _runtime_input
    from lantai.runtime.adapters.native_runtime import NativeLantaiRuntime
    from lantai.runtime.effects import RuntimeEffectJournal
    from lantai.runtime.models import ModelStreamEvent, ModelResponse, ModelMessage
    from lantai.runtime.tools import ToolCall, LantaiToolRegistry
    service = ResearchService(tmp_path)
    project = service.create_project('Q', [], idempotency_key='p')
    args = dict(project_id=project['project_id'], revision=1, text='Draft.', claims=[], idempotency_key='r')
    full = build_lantai_tool_registry(agent_config=SimpleNamespace(tools=SimpleNamespace(builtin={})),
        request_context={}, include_default_policy_middleware=False)
    registry = LantaiToolRegistry()
    registry.register_tool(*full.get_tool('research_report'))
    def call(identifier, arguments):
        return [ModelStreamEvent(kind='tool_call_completed', tool_call=ToolCall(id=identifier, name='research_report', arguments=arguments)),
            ModelStreamEvent(kind='message_completed', response=ModelResponse(message=ModelMessage(role='assistant')))]
    client = ScriptedModelClient([call('bad', {**args, 'claims': [{'text': 'Absent.', 'evidence': []}]}),
        call('good', args), [_completed('Saved.', message_id='final')]])
    journal = RuntimeEffectJournal(tmp_path)
    runtime = NativeLantaiRuntime(model_client_factory=lambda _: client, tool_registry=registry, effect_journal=journal)
    token = current_workspace_context.set(WorkspaceContext(tmp_path, tmp_path, tmp_path))
    try:
        events = [event async for event in runtime.run_turn(_runtime_input())]
    finally:
        current_workspace_context.reset(token)
    completed = [event for event in events if event.type == 'tool.call.completed']
    assert [event.payload['metadata']['side_effect_outcome'] for event in completed] == ['known_not_applied', 'committed']
    statuses = [record.status for record in journal.list_effects('turn-1', 'turn-1:native-attempt')]
    assert statuses == ['intent', 'running', 'failed', 'intent', 'running', 'committed']
    assert service.project(project['project_id'])['report_id']
