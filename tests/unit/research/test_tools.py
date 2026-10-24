import asyncio
import hashlib
import json
from urllib.parse import urlparse, unquote

import pytest

from lantai.agents.tools.research import research_export, research_library, research_project, selected_research_sources
from lantai.config.context import WorkspaceContext, current_workspace_context, current_workspace_dir
from lantai.research.projects import ResearchService


@pytest.mark.asyncio
async def test_project_tools_use_agent_library_across_topic_roots(tmp_path):
    agent_home = tmp_path / 'agent'
    service = ResearchService(agent_home)
    project = service.create_project('Shared library project', [], idempotency_key='p')
    for topic in ('first', 'second'):
        state_root = tmp_path / topic / 'state'
        project_root = tmp_path / topic / 'external'
        context = current_workspace_context.set(WorkspaceContext(agent_home, state_root, project_root))
        try:
            result = await research_project('get', project_id=project['project_id'])
            assert json.loads(result.content[0]['text'])['project']['project_id'] == project['project_id']
            assert not (state_root / '.lantai/research').exists()
            assert not (project_root / '.lantai/research').exists()
        finally:
            current_workspace_context.reset(context)
    other_home = tmp_path / 'other-agent'
    context = current_workspace_context.set(WorkspaceContext(other_home, agent_home, agent_home))
    try:
        with pytest.raises(FileNotFoundError, match='Research project not found'):
            await research_project('get', project_id=project['project_id'])
    finally:
        current_workspace_context.reset(context)


@pytest.mark.asyncio
async def test_library_tool_notes_keep_model_authorship_when_used_as_sources(tmp_path):
    from lantai.research.library import LibraryService
    library = LibraryService(tmp_path)
    work = library.import_items([{'title': 'Study'}], origin={'provider': 'fixture'})['items'][0]['work_id']
    path = tmp_path / 'wiki/raw/sources/study.txt'
    path.parent.mkdir(parents=True)
    path.write_text('An observation.')
    research = ResearchService(tmp_path)
    project = research.create_project('What is known?', [str(path.relative_to(tmp_path))], idempotency_key='p')
    context = current_workspace_context.set(WorkspaceContext(tmp_path, tmp_path, tmp_path))
    try:
        result = await research_library('note', work_id=work, text='Further evidence is needed.')
        note = json.loads(result.content[0]['text'])
        assert note['origin']['provider'] == 'assistant-chat'
        result = await research_library('note_source', project_id=project['project_id'],
            annotation_id=note['id'], revision=1, idempotency_key='n')
        source_id = json.loads(result.content[0]['text'])['source_version_id']
        assert research.source_manifest(source_id)['source_kind'] == 'model_summary'
        assert research.source_manifest(source_id)['evidence_level'] == 'note'
    finally:
        current_workspace_context.reset(context)


@pytest.mark.asyncio
async def test_library_tool_organizes_with_revision_conflict_protection(tmp_path):
    from lantai.research.library import LibraryService
    from lantai.research.store import RevisionConflict
    library = LibraryService(tmp_path)
    work = library.import_items([{'title': 'Study'}], origin={'provider': 'fixture'})['items'][0]['work_id']
    context = current_workspace_context.set(WorkspaceContext(tmp_path, tmp_path, tmp_path))
    try:
        result = await research_library('organize', work_id=work, organization_revision=0,
                                        tags=['Methods'], collections=['Review'])
        assert json.loads(result.content[0]['text'])['organization']['tags'] == ['Methods']
        with pytest.raises(RevisionConflict):
            await research_library('organize', work_id=work, organization_revision=0, tags=[], collections=[])
    finally:
        current_workspace_context.reset(context)


def test_exact_source_binding_preserves_research_argument_types():
    from types import SimpleNamespace
    from lantai.agents.tool_registry import build_lantai_tool_registry
    cfg = SimpleNamespace(tools=SimpleNamespace(builtin={}))
    plain = build_lantai_tool_registry(agent_config=cfg, request_context={}, include_default_policy_middleware=False)
    scoped = build_lantai_tool_registry(agent_config=cfg, request_context={
        'knowledge_sources': [{'path': 'wiki/raw/sources/a.txt'}], 'knowledge_scope_mode': 'exact',
    }, include_default_policy_middleware=False)
    for name in ('research_project', 'research_read', 'research_report', 'research_review', 'research_library', 'research_export'):
        assert scoped.get_tool(name)[0].input_schema == plain.get_tool(name)[0].input_schema, name
    assert scoped.get_tool('research_project')[0].input_schema['properties']['source_paths']['type'] == 'array'
    assert scoped.get_tool('research_report')[0].input_schema['properties']['claims']['type'] == 'array'


def test_research_schema_declares_allowed_actions_and_evidence_levels():
    from types import SimpleNamespace
    from lantai.agents.tool_registry import build_lantai_tool_registry
    registry = build_lantai_tool_registry(agent_config=SimpleNamespace(tools=SimpleNamespace(builtin={})),
        request_context={}, include_default_policy_middleware=False)
    properties = registry.get_tool('research_project')[0].input_schema['properties']
    assert properties['action'] == {'type': 'string', 'enum': ['list', 'create', 'get', 'revise']}
    assert properties['source_levels'] == {'type': 'object', 'additionalProperties':
        {'type': 'string', 'enum': ['fulltext', 'abstract', 'excerpt']}}
    assert properties['subquestions'] == {'type': 'array', 'items': {'type': 'string'}}
    assert properties['outline'] == {'type': 'array', 'items': {'type': 'string'}}
    assert properties['policy']['enum'] == ['local_sources', 'public_search', 'offline']
    claim = registry.get_tool('research_report')[0].input_schema['properties']['claims']['items']
    assert claim['type'] == 'object'
    assert set(claim['required']) == {'text', 'evidence'}
    evidence = claim['properties']['evidence']['items']
    assert set(evidence['required']) == {'source_version_id'}
    assert evidence['properties']['quote']['type'] == 'string'
    assert evidence['properties']['block_index']['type'] == 'integer'


@pytest.mark.asyncio
async def test_legacy_adapter_runs_same_scoped_research_tools(tmp_path):
    from types import SimpleNamespace
    from lantai.agents.tool_registry import build_lantai_tool_registry
    from lantai.runtime.adapters.legacy_toolkit_adapter import LegacyToolkitAdapter
    from lantai.runtime.adapters.legacy_toolkit_components import LegacyToolkit
    from lantai.runtime.tools import LantaiToolRegistry
    relative = 'wiki/raw/sources/selected.txt'
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text('The study evaluated four datasets.')
    context = current_workspace_context.set(WorkspaceContext(tmp_path, tmp_path, tmp_path))
    try:
        full = build_lantai_tool_registry(agent_config=SimpleNamespace(tools=SimpleNamespace(builtin={})),
            request_context={'knowledge_sources': [{'path': relative}], 'knowledge_scope_mode': 'exact'},
            include_default_policy_middleware=False)
        registry = LantaiToolRegistry()
        for spec, executor in full.tool_items():
            if spec.name in {'research_project', 'research_read', 'research_report'}:
                registry.register_tool(spec, executor)
        toolkit = LegacyToolkit()
        LegacyToolkitAdapter(toolkit).register_tools(registry)

        async def invoke(name, arguments):
            stream = await toolkit.call_tool_function({'id': name, 'name': name, 'input': arguments})
            chunks = [chunk async for chunk in stream]
            return json.loads(chunks[-1].content[0]['text'])

        project = await invoke('research_project', {'action': 'create', 'question': 'Dataset count?',
            'source_paths': [relative], 'subquestions': ['What was measured?'], 'outline': ['Findings'],
            'idempotency_key': 'legacy-project'})
        assert project['subquestions'] == ['What was measured?']
        assert project['outline'] == ['Findings']
        source = project['source_versions'][0]
        reading = await invoke('research_read', {'project_id': project['project_id'], 'revision': 1,
            'source_version_id': source})
        report = await invoke('research_report', {'project_id': project['project_id'], 'revision': 1,
            'text': 'Four datasets were evaluated.', 'claims': [{'text': 'Four datasets were evaluated.',
                'evidence': [{'source_version_id': source, 'quote': reading['blocks'][0]['text']}]}],
            'idempotency_key': 'legacy-report'})
        service = ResearchService(tmp_path)
        assert service.report(report['id'])['text'] == 'Four datasets were evaluated.'
        coverage = service.project(project['project_id'])['reading']
        assert coverage['read_blocks'] == coverage['total_blocks'] == 1
        assert toolkit.get_json_schemas()[0]['function']['parameters']['properties']['source_paths']['type'] == 'array'
    finally:
        current_workspace_context.reset(context)


def test_research_tools_use_scoped_workspace_permissions(tmp_path):
    from lantai.security.tool_policy import ToolExecutionContext, evaluate_tool_policy
    names = ('research_project', 'research_read', 'research_report', 'research_review', 'research_library', 'research_export')
    for name in names:
        workspace = evaluate_tool_policy(name, {}, context=ToolExecutionContext(workspace_dir=str(tmp_path), permission_profile='workspace', permission_mode='quiet'))
        assert workspace.decision == 'allow', name
        for profile, mode in [('observe', 'quiet'), ('workspace', 'plan')]:
            restricted = evaluate_tool_policy(name, {}, context=ToolExecutionContext(workspace_dir=str(tmp_path), permission_profile=profile, permission_mode=mode))
            assert restricted.decision != 'allow', (name, profile, mode)


def test_chat_export_delivers_verified_managed_artifact_from_read_only_topic(tmp_path):
    state, topic = tmp_path / 'agent', tmp_path / 'topic'
    topic.mkdir()
    service = ResearchService(state)
    project = service.create_project('Q', [], idempotency_key='p')
    report = service.save_report(project['project_id'], 1, 'Draft research.', [], idempotency_key='r')
    topic_state = state / 'topic-state'
    context = current_workspace_context.set(WorkspaceContext(state, topic_state, topic, access_mode='read_only'))
    legacy = current_workspace_dir.set(topic)
    try:
        result = asyncio.run(research_export(project['project_id'], 1, report['id'], 'e'))
        assert not result.is_error
        file = next(block for block in result.content if block['type'] == 'file')
        from pathlib import Path
        path = Path(unquote(urlparse(file['source']['url']).path))
        assert path.is_relative_to(state)
        import json
        receipt = json.loads(result.content[0]['text'])
        assert hashlib.sha256(path.read_bytes()).hexdigest() == receipt['artifact_hash']
        assert receipt['file_path'] == str(path)
        assert receipt['attachment_included'] is True
        assert receipt['delivery_status'] == 'attached'
        assert receipt['redelivery_tool'] == 'research_export'
        assert receipt['artifact_path_base'] == 'agent_home'
        # The model-facing path is a projection, not machine-specific store data.
        persisted = service.store.get(receipt['id'], kind='export')
        assert 'file_path' not in persisted
        assert not Path(persisted['artifact_path']).is_absolute()
        repeated = asyncio.run(research_export(project['project_id'], 1, report['id'], 'e'))
        assert json.loads(repeated.content[0]['text']) == receipt
        assert list(topic.iterdir()) == []
    finally:
        current_workspace_context.reset(context)
        current_workspace_dir.reset(legacy)


def test_managed_export_is_bound_to_runtime_artifact_journal_and_agent(tmp_path):
    from types import SimpleNamespace
    from lantai.agents.tool_registry import build_lantai_tool_registry
    from lantai.runtime.artifact_effects import (
        RuntimeArtifactEffectJournal, artifact_effect_intents_from_result, settle_artifact_effects,
    )
    from lantai.runtime.runs import RuntimeEffectRecord, RuntimeRunOwner
    from lantai.runtime.tools import ToolCall, ToolResult
    state, topic = tmp_path / 'agent', tmp_path / 'topic'
    topic.mkdir()
    service = ResearchService(state)
    project = service.create_project('Q', [], idempotency_key='p')
    report = service.save_report(project['project_id'], 1, 'Draft research.', [], idempotency_key='r')
    registry = build_lantai_tool_registry(agent_config=SimpleNamespace(tools=SimpleNamespace(builtin={})),
        request_context={}, include_default_policy_middleware=False)
    spec = registry.get_tool('research_export')[0]
    effect = RuntimeEffectRecord(run_id='run-research', attempt_id='a1', step_id='export',
        effect_id='effect-export', source='chat', owner=RuntimeRunOwner(agent_id='default'), status='intent',
        tool_name='research_export', args_hash='sha256:args', policy_snapshot_hash='sha256:policy',
        idempotency_key='effect-export', release_edition='official')
    call = ToolCall(id='call-export', name='research_export')
    topic_state = state / 'topic-state'
    context = current_workspace_context.set(WorkspaceContext(state, topic_state, topic, access_mode='read_only'))
    try:
        response = asyncio.run(research_export(project['project_id'], 1, report['id'], 'export'))
        result = ToolResult(call_id=call.id, name=call.name, ok=True, content=response.content, metadata=response.metadata)
        def intents(tool_spec=spec):
            return artifact_effect_intents_from_result(source_effect=effect, tool_call=call,
                tool_spec=tool_spec, result=result, workspace_dir=topic)
        records = intents()
        assert len(records) == 1
        journal = RuntimeArtifactEffectJournal(topic)
        journal.append(records[0])
        settled = settle_artifact_effects(records, result=result, workspace_dir=topic)[0]
        journal.append(settled)
        assert settled.status == 'delivered'
        assert settled.sha256 == 'sha256:' + response.metadata['artifact_hash']
        assert settled.research_export_id == response.metadata['research_export_id']
        assert journal.latest(effect.run_id, effect.attempt_id, settled.artifact_effect_id) == settled
        from lantai.runtime.research_delivery import ResearchDeliveryContract, ResearchDeliveryCoordinator
        coordinator = ResearchDeliveryCoordinator(
            ResearchDeliveryContract(effect.run_id, effect.attempt_id, 1, 'local_only', 'graph', 'document'),
            journal, topic)
        projection = coordinator.project_artifact(artifact_effect_id=settled.artifact_effect_id,
            produced_for_task_revision=1, content_validated=False, visual_validated=False)
        assert projection.status == 'content_validation_pending'
        assert not (topic / '.lantai/research').exists()
        assert not (topic_state / '.lantai/research').exists()
        # Plugin output cannot opt itself into the managed store by name or receipt.
        assert intents(spec.model_copy(update={'origin': 'mcp'})) == ()
        other = tmp_path / 'other-agent'
        token = current_workspace_context.set(WorkspaceContext(other, other, topic))
        try:
            assert intents() == ()
            assert settle_artifact_effects(records, result=result, workspace_dir=topic)[0].status == 'missing'
        finally:
            current_workspace_context.reset(token)
        from lantai.research.exports import export_file
        export_file(service, settled.research_export_id).write_bytes(b'changed')
        assert settle_artifact_effects(records, result=result, workspace_dir=topic)[0].status == 'missing'
    finally:
        current_workspace_context.reset(context)


def test_exact_source_chat_cannot_access_other_projects_or_library(tmp_path):
    for name in ('selected', 'private'):
        path = tmp_path / f'wiki/raw/sources/{name}.txt'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
    service = ResearchService(tmp_path)
    p = service.create_project('Private Q', ['wiki/raw/sources/private.txt'], idempotency_key='p')
    context = current_workspace_context.set(WorkspaceContext(tmp_path, tmp_path, tmp_path))
    sources = selected_research_sources.set(('wiki/raw/sources/selected.txt',))
    try:
        with pytest.raises(ValueError, match='outside'):
            asyncio.run(research_project('get', project_id=p['project_id']))
        with pytest.raises(ValueError, match='exact-source'):
            asyncio.run(research_library('list'))
    finally:
        current_workspace_context.reset(context)
        selected_research_sources.reset(sources)


@pytest.mark.asyncio
async def test_pdf_reading_compacts_layout_spacing_without_changing_citation_anchors(monkeypatch):
    import lantai.agents.tools.research as tools
    original = 'Header' + ' ' * 20000 + 'Value\n' + ' ' * 8000 + 'Row\t12.5%\n'
    block = {'index': 7, 'page': 3, 'start': 42000, 'end': 42000 + len(original), 'text': original}
    response = {'path': 'wiki/raw/sources/paper.pdf', 'blocks': [block], 'next_block': 8,
                'source_version_id': 'source-version', 'coverage': 'complete'}
    class Service:
        def read_source(self, *args):
            return response
    monkeypatch.setattr(tools, '_service', Service)
    result = await tools.research_read('project', 1, 'source-version', 7, 1)
    value = json.loads(result.content[0]['text'])
    projected = value['blocks'][0]
    assert projected['reading_text'].split() == original.split()
    assert len(projected['reading_text']) < 100
    assert 'text' not in projected
    assert value['text_presentation'] == 'pdf_layout_spacing_compacted; cite by block_index, not reading_text offsets'
    assert {k: projected[k] for k in ('index', 'page', 'start', 'end')} == {k: block[k] for k in ('index', 'page', 'start', 'end')}
    assert response['blocks'][0]['text'] == original
    assert value['next_block'] == 8


@pytest.mark.asyncio
async def test_project_get_omits_pdf_render_geometry_and_storage_paths(monkeypatch):
    """GUI R05: a 40-page manifest must not crowd out the first reading call."""
    import copy
    import lantai.agents.tools.research as tools
    region = dict(start=0, end=4, x=.1, y=.1, width=.1, height=.02, confidence=None)
    pages = [dict(page=i + 1, start=i * 1000, end=(i + 1) * 1000,
                  issue='ocr_low_confidence' if i == 39 else None,
                  layout_status='mapped', regions=[region] * 300) for i in range(40)]
    source = dict(id='source-version', path='wiki/raw/sources/long.pdf',
                  pages=pages, blocks=[dict(index=i, page=i + 1, start=i * 1000,
                  end=(i + 1) * 1000) for i in range(40)], text_length=40000,
                  coverage='partial', issues=['page 40: ocr_low_confidence'],
                  evidence_level='fulltext', raw_hash='raw', text_hash='text',
                  snapshot_path='private/blob.pdf', text_chunks=[{'path': 'private/chunk'}])
    evidence = dict(project={'reading': {'read_blocks': 0, 'total_blocks': 40}},
                    sources=[source], report=None, review=None, report_history=[])
    original = copy.deepcopy(evidence)
    class Service:
        def evidence(self, *args):
            return evidence
    monkeypatch.setattr(tools, '_service', Service)
    result = await tools.research_project('get', project_id='project')
    assert len(result.content[0]['text']) < 16000
    value = json.loads(result.content[0]['text'])
    projected = value['sources'][0]
    assert projected['pages'] == [{k: v for k, v in p.items() if k != 'regions'} for p in pages]
    assert projected['blocks'] == source['blocks']
    for key in ('id', 'path', 'text_length', 'coverage', 'issues', 'evidence_level', 'raw_hash', 'text_hash'):
        assert projected[key] == source[key]
    assert 'snapshot_path' not in projected and 'text_chunks' not in projected
    assert evidence == original


@pytest.mark.asyncio
async def test_pdf_read_omits_render_geometry_but_keeps_text_and_uncertainty(monkeypatch):
    import copy
    import lantai.agents.tools.research as tools
    block = dict(index=39, page=40, start=39000, end=39037,
                 text='Tail evidence: improvement reversed.',
                 page_issues=['ocr_requires_visual_verification'],
                 regions=[dict(start=39000, end=39004, x=.1, y=.1, width=.1, height=.1)] * 10000)
    response = dict(path='wiki/raw/sources/long.pdf', blocks=[block], next_block=None,
                    source_version_id='version', coverage='partial', issues=['verify OCR'])
    original = copy.deepcopy(response)
    class Service:
        def read_source(self, *args):
            return response
    monkeypatch.setattr(tools, '_service', Service)
    result = await tools.research_read('project', 1, 'version', 39, 1)
    assert len(result.content[0]['text']) < 1500
    value = json.loads(result.content[0]['text'])
    assert value['blocks'][0]['reading_text'] == block['text']
    assert value['blocks'][0]['page_issues'] == block['page_issues']
    assert value['blocks'][0]['index'] == 39 and value['blocks'][0]['start'] == 39000
    assert value['next_block'] is None and value['coverage'] == 'partial'
    assert response == original


@pytest.mark.asyncio
async def test_long_pdf_tool_reading_reaches_tail_and_preserves_exact_citation(tmp_path):
    from reportlab.pdfgen.canvas import Canvas
    from lantai.agents.tools.research import research_read, research_report
    path = tmp_path / 'wiki/raw/sources/long.pdf'
    path.parent.mkdir(parents=True)
    canvas = Canvas(str(path), pagesize=(600, 800))
    for page in range(40):
        for line in range(30):
            canvas.drawString(40, 740 - 20 * line, f'Page {page + 1} row {line + 1}: measured observation repeated.')
        if page == 39:
            canvas.drawString(40, 110, 'TAIL: the initial improvement reversed.')
        canvas.showPage()
    canvas.save()
    service = ResearchService(tmp_path)
    project = service.create_project('Long PDF', [str(path.relative_to(tmp_path))], idempotency_key='long')
    source_id = project['source_versions'][0]
    before = service.source_manifest(source_id)
    assert len(before['pages']) == len(before['blocks']) == 40
    assert len(json.dumps(before)) > 1_000_000
    context = current_workspace_context.set(WorkspaceContext(tmp_path, tmp_path, tmp_path))
    try:
        manifest = await research_project('get', project_id=project['project_id'])
        assert len(manifest.content[0]['text']) < 20000
        next_block, blocks = 0, []
        while next_block is not None:
            response = await research_read(project['project_id'], 1, source_id, next_block)
            value = json.loads(response.content[0]['text'])
            assert len(response.content[0]['text']) < 20000
            blocks.extend(value['blocks'])
            next_block = value['next_block']
        assert [b['index'] for b in blocks] == list(range(40))
        assert 'TAIL: the initial improvement reversed.' in blocks[-1]['reading_text']
        response = await research_report(project['project_id'], 1, 'The initial improvement reversed.',
            [{'text': 'The initial improvement reversed.',
              'evidence': [{'source_version_id': source_id, 'block_index': 39}]}], 'tail-report')
        saved = service.report(json.loads(response.content[0]['text'])['id'])
        evidence = saved['claims'][0]['evidence'][0]
        original = service.source(source_id)['text']
        assert evidence['quote'] == original[evidence['start']:evidence['end']]
        assert 'TAIL: the initial improvement reversed.' in evidence['quote']
        assert service.project(project['project_id'])['reading']['read_blocks'] == 40
        assert service.source_manifest(source_id) == before
        assert service.evidence(project['project_id'])['sources'][0]['pages'] == before['pages']
        preview = service.source_page(source_id, 40)
        assert preview['page'] == 40 and preview['overlay_allowed'] is True
        assert preview['raw_hash'] == before['raw_hash'] and preview['png_base64']
    finally:
        current_workspace_context.reset(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("citation_mode", ["block", "quote"])
async def test_large_report_receipts_keep_retrievable_anchors_without_repeating_quotes(tmp_path, citation_mode):
    """U03's 77 block citations must not echo 400k+ evidence characters to chat."""
    from lantai.agents.tools.research import research_report, research_read
    path = tmp_path / 'wiki/raw/sources/evidence.txt'
    path.parent.mkdir(parents=True)
    path.write_text('Verbatim observation. ' * 300)
    service = ResearchService(tmp_path)
    project = service.create_project('Evidence receipt', [str(path.relative_to(tmp_path))], idempotency_key='p')
    pid, sid = project['project_id'], project['source_versions'][0]
    context = current_workspace_context.set(WorkspaceContext(tmp_path, tmp_path, tmp_path))
    try:
        reading = json.loads((await research_read(pid, 1, sid, 0, 1)).content[0]['text'])
        quote = reading['blocks'][0]['text']
        statements = [f'Observation {i} remains bounded.' for i in range(77)]
        text = '\n\n'.join(statements)
        span = {'source_version_id': sid, **({'block_index': 0} if citation_mode == 'block'
            else {'quote': quote, 'start': 0, 'end': len(quote)})}
        claims = [{'text': s, 'evidence': [span]} for s in statements]
        receipt = await research_report(pid, 1, text, claims, 'large-report')
        saved_id = json.loads(receipt.content[0]['text'])['id']
        stored = service.report(saved_id)
        assert sum(len(c['evidence'][0]['quote']) for c in stored['claims']) > 400000
        before = json.dumps(service.evidence(pid), sort_keys=True)
        detail = await research_project('get', project_id=pid)
        for result, is_get in [(receipt, False), (detail, True)]:
            assert len(result.content[0]['text']) < 40000
            value = json.loads(result.content[0]['text'])
            report = value['report'] if is_get else value
            assert report['id'] == saved_id and report['text_hash'] == stored['text_hash']
            assert report['review_status'] == 'not_reviewed'
            assert report['text'] == text
            assert len(report['claims']) == 77
            for claim, original in zip(report['claims'], stored['claims']):
                assert (claim['start'], claim['end']) == (original['start'], original['end'])
                span = claim['evidence'][0]
                assert 'quote' not in span
                assert span['quote_sha256'] == hashlib.sha256(quote.encode()).hexdigest()
                assert span['quote_chars'] == len(quote)
                assert span['source_version_id'] == sid
                if citation_mode == 'block':
                    assert span['source_block_index'] == 0
                assert span['start'] == 0 and span['end'] == len(quote)
            assert report['evidence_presentation'] == 'anchors_only; original quotes available via research_read'
        assert json.dumps(service.evidence(pid), sort_keys=True) == before
        reread = json.loads((await research_read(pid, 1, sid, 0, 1)).content[0]['text'])
        assert reread['blocks'][0]['text'] == quote
    finally:
        current_workspace_context.reset(context)
