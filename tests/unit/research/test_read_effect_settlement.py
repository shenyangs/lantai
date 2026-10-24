"""Rejected reads must leave coverage and effect state safe for correction."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from lantai.agents.tools import research
from lantai.agents.tool_registry import build_lantai_tool_registry
from lantai.config.context import WorkspaceContext, current_workspace_context
from lantai.research.projects import ResearchService


def project_with_source(root):
    path = root / 'wiki/raw/sources/paper.txt'
    path.parent.mkdir(parents=True)
    path.write_text('Measured observation. ' * 1600)
    service = ResearchService(root)
    project = service.create_project('Q', [str(path.relative_to(root))], idempotency_key='p')
    return service, dict(project_id=project['project_id'], revision=1,
                        source_version_id=project['source_versions'][0])


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['five_blocks', 'stale_revision', 'outside_source', 'after_insert'])
async def test_read_rejection_rolls_back_and_allows_correction(tmp_path, monkeypatch, failure):
    service, args = project_with_source(tmp_path)
    monkeypatch.setattr(research, '_service', lambda: service)
    before = service.project(args['project_id'])
    bad = {**args, **{'five_blocks': {'block_count': 5}, 'stale_revision': {'revision': 2},
                     'outside_source': {'source_version_id': 'outside'}, 'after_insert': {}}[failure]}
    original_put = service.store.put
    if failure == 'after_insert':
        def reject(*pos, **kw):
            original_put(*pos, **kw)
            raise ValueError('Rejected before commit')
        monkeypatch.setattr(service.store, 'put', reject)
    result = await research.research_read(**bad)
    assert result.is_error
    assert result.metadata['side_effect_outcome'] == 'known_not_applied'
    assert service.project(args['project_id']) == before
    with service.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM records WHERE kind='reading'").fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM research_reading_usage').fetchone()[0] == 0
    monkeypatch.setattr(service.store, 'put', original_put)
    result = await research.research_read(**args, block_count=4)
    assert not result.is_error
    assert len(json.loads(result.content[0]['text'])['blocks']) == 4
    assert service.project(args['project_id'])['reading']['read_blocks'] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize('exception', [OSError('disk failure'), asyncio.CancelledError()])
async def test_uncertain_read_failure_does_not_attest_rollback(tmp_path, monkeypatch, exception):
    service, args = project_with_source(tmp_path)
    monkeypatch.setattr(research, '_service', lambda: service)
    def fail(*pos):
        raise exception
    monkeypatch.setattr(service, 'read_source', fail)
    with pytest.raises(type(exception)):
        await research.research_read(**args)


@pytest.mark.asyncio
async def test_read_response_failure_after_commit_remains_uncertain(tmp_path, monkeypatch):
    service, args = project_with_source(tmp_path)
    monkeypatch.setattr(research, '_service', lambda: service)
    def fail(value):
        raise ValueError('response failed after commit')
    monkeypatch.setattr(research, '_response', fail)
    with pytest.raises(ValueError, match='after commit'):
        await research.research_read(**args)
    assert service.project(args['project_id'])['reading']['read_blocks'] == 4


def test_only_builtin_read_can_attest_effect_settlement():
    config = SimpleNamespace(tools=SimpleNamespace(builtin={}))
    registry = build_lantai_tool_registry(agent_config=config, request_context={}, include_default_policy_middleware=False)
    assert registry.get_tool('research_read')[0].metadata['trusted_effect_settlement'] is True
    async def replacement():
        pass
    overridden = build_lantai_tool_registry(agent_config=config, request_context={},
        include_default_policy_middleware=False, is_coding_mode_enabled_fn=lambda _: True,
        coding_tool_functions_factory=lambda _: {'research_read': replacement})
    assert not overridden.get_tool('research_read')[0].metadata.get('trusted_effect_settlement')


@pytest.mark.asyncio
async def test_native_read_rejection_then_correction_settles_and_completes(tmp_path):
    from tests.unit.runtime.test_native_runtime import ScriptedModelClient, _completed, _runtime_input
    from lantai.runtime.adapters.native_runtime import NativeLantaiRuntime
    from lantai.runtime.effects import RuntimeEffectJournal
    from lantai.runtime.models import ModelStreamEvent, ModelResponse, ModelMessage
    from lantai.runtime.tools import ToolCall, LantaiToolRegistry
    service, args = project_with_source(tmp_path)
    full = build_lantai_tool_registry(agent_config=SimpleNamespace(tools=SimpleNamespace(builtin={})),
        request_context={}, include_default_policy_middleware=False)
    registry = LantaiToolRegistry()
    registry.register_tool(*full.get_tool('research_read'))
    def call(identifier, arguments):
        return [ModelStreamEvent(kind='tool_call_completed', tool_call=ToolCall(id=identifier, name='research_read', arguments=arguments)),
            ModelStreamEvent(kind='message_completed', response=ModelResponse(message=ModelMessage(role='assistant')))]
    client = ScriptedModelClient([call('bad', {**args, 'block_count': 5}),
        call('good', {**args, 'block_count': 4}), [_completed('Read.', message_id='final')]])
    journal = RuntimeEffectJournal(tmp_path)
    runtime = NativeLantaiRuntime(model_client_factory=lambda _: client, tool_registry=registry, effect_journal=journal)
    token = current_workspace_context.set(WorkspaceContext(tmp_path, tmp_path, tmp_path))
    try:
        events = [event async for event in runtime.run_turn(_runtime_input())]
    finally:
        current_workspace_context.reset(token)
    completed = [event for event in events if event.type == 'tool.call.completed']
    assert [event.payload['metadata']['side_effect_outcome'] for event in completed] == ['known_not_applied', 'committed']
    assert [record.status for record in journal.list_effects('turn-1', 'turn-1:native-attempt')] == [
        'intent', 'running', 'failed', 'intent', 'running', 'committed']
    assert service.project(args['project_id'])['reading']['read_blocks'] == 4
