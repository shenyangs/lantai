import json
from types import SimpleNamespace

from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lantai.cli.research_cmd import research_group
from lantai.app.routers import research as api


def test_library_organization_api_is_revision_guarded_and_agent_scoped(tmp_path, monkeypatch):
    from lantai.research.library import LibraryService
    roots = {name: tmp_path / name for name in ('a', 'b')}
    work = LibraryService(roots['a']).import_items([{'title': 'Study'}],
        origin={'provider': 'fixture'})['items'][0]['work_id']
    async def agent(request):
        return SimpleNamespace(workspace_dir=roots[request.headers['x-agent-id']])
    monkeypatch.setattr(api, 'get_agent_for_request', agent)
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    url = '/workspace/research/library/works/' + work + '/organization'
    body = {'expected_revision': 0, 'tags': ['Methods'], 'collections': ['Review']}
    assert client.post(url, headers={'x-agent-id': 'b'}, json=body).status_code == 404
    response = client.post(url, headers={'x-agent-id': 'a'}, json=body)
    assert response.status_code == 200, response.text
    assert response.json()['organization']['revision'] == 1
    assert client.post(url, headers={'x-agent-id': 'a'}, json=body).status_code == 409
    assert client.post(url, headers={'x-agent-id': 'a'}, json={**body,
        'expected_revision': True}).status_code == 422


def test_imported_knowledge_source_can_start_research_without_widening_file_access(tmp_path, monkeypatch):
    from lantai.app.wiki.core import import_single_wiki_source
    info, _, _ = import_single_wiki_source(
        tmp_path, 'observation.txt', b'An observation does not establish causation.')
    assert info.path.startswith('.wiki/raw/sources/')

    async def agent(request):
        return SimpleNamespace(workspace_dir=tmp_path, agent_id='a')
    monkeypatch.setattr(api, 'get_agent_for_request', agent)
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    body = {'question': 'What does the observation support?',
            'source_paths': [info.path], 'idempotency_key': 'imported'}
    result = client.post('/workspace/research/projects', json=body)
    assert result.status_code == 200, result.text
    assert result.json()['source_paths'] == [info.path]
    assert client.post('/workspace/research/packets', json={
        'source_paths': [info.path]}).status_code == 200

    private = tmp_path / '.wiki/schema.md'
    private.write_text('private internal knowledge state')
    link = tmp_path / '.wiki/raw/sources/private-link.txt'
    link.symlink_to(private)
    for index, path in enumerate(('.wiki/schema.md', '.wiki/pages/sources/generated.md',
                                   '.wiki/raw/sources/../../schema.md',
                                   '.wiki/raw/sources/private-link.txt')):
        assert client.post('/workspace/research/projects', json={
            **body, 'source_paths': [path], 'idempotency_key': f'private-{index}'
        }).status_code == 422


def test_cli_packet_audit_and_missing_record(tmp_path):
    path = tmp_path / 'wiki/raw/sources/a.txt'
    path.parent.mkdir(parents=True)
    path.write_text('An observation.')
    runner = CliRunner()
    result = runner.invoke(research_group, ['packet', '--workspace', str(tmp_path), '--source', 'wiki/raw/sources/a.txt'])
    assert result.exit_code == 0, result.output
    packet = json.loads(result.output)
    claims = tmp_path / 'claims.json'
    claims.write_text('{"claims": []}')
    result = runner.invoke(research_group, ['audit', '--workspace', str(tmp_path), '--packet', packet['packet_id'], '--claims', str(claims)])
    assert result.exit_code == 2
    assert json.loads(result.output)['status'] == 'blocked'
    result = runner.invoke(research_group, ['show', '--workspace', str(tmp_path), 'packet-' + '0' * 32])
    assert result.exit_code == 1


def test_api_uses_request_agent_and_rejects_cross_workspace_record(tmp_path, monkeypatch):
    roots = {name: tmp_path / name for name in ['a', 'b']}
    for root in roots.values():
        path = root / 'wiki/raw/sources/a.txt'
        path.parent.mkdir(parents=True)
        path.write_text('Private agent evidence.')
    async def agent(request):
        return SimpleNamespace(workspace_dir=roots[request.headers['x-agent-id']])
    monkeypatch.setattr(api, 'get_agent_for_request', agent)
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    result = client.post('/workspace/research/packets', headers={'x-agent-id': 'a'}, json={'source_paths': ['wiki/raw/sources/a.txt']})
    assert result.status_code == 200
    packet_id = result.json()['packet_id']
    assert client.get('/workspace/research/records/' + packet_id, headers={'x-agent-id': 'b'}).status_code == 404
    assert client.get('/workspace/research/records/' + packet_id, headers={'x-agent-id': 'a'}).status_code == 200
    assert client.post('/workspace/research/packets', headers={'x-agent-id': 'a'}, json={'source_paths': ['../b/wiki/raw/sources/a.txt']}).status_code == 422
    result = client.post('/workspace/research/audits', headers={'x-agent-id': 'a'}, json={'packet_id': packet_id, 'claims': []})
    assert result.status_code == 200
    assert result.json()['status'] == 'blocked'


def test_project_api_anchor_export_conflict_and_agent_isolation(tmp_path, monkeypatch):
    roots = {n: tmp_path / n for n in ('a', 'b')}
    source_path = 'wiki/raw/sources/study.txt'
    source = roots['a'] / source_path
    source.parent.mkdir(parents=True)
    source.write_text('An association does not establish causation.')
    async def agent(request):
        name = request.headers['x-agent-id']
        return SimpleNamespace(workspace_dir=roots[name], agent_id=name)
    monkeypatch.setattr(api, 'get_agent_for_request', agent)
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    a, b = {'x-agent-id': 'a'}, {'x-agent-id': 'b'}
    created = client.post('/workspace/research/projects', headers=a, json={
        'question': 'What is supported?', 'source_paths': [source_path], 'idempotency_key': 'p'})
    assert created.status_code == 200
    p = created.json()
    report_body = {'revision': 1, 'text': 'Causation is not established.', 'claims': [{
        'text': 'Causation is not established.', 'evidence': [{'source_version_id': p['source_versions'][0],
        'quote': source.read_text()}]}], 'idempotency_key': 'r'}
    url = '/workspace/research/projects/' + p['project_id']
    saved = client.post(url + '/reports', headers=a, json=report_body)
    assert saved.status_code == 200, saved.text
    report = saved.json()
    assert report['claims'][0]['start'] == 0
    assert client.get(url, headers=b).status_code == 404
    assert client.post(url + '/reports', headers=a, json={**report_body, 'idempotency_key': 'bad',
        'reviewer': 'self-certified'}).status_code == 422
    assert client.post(url + '/reports', headers=a, json={**report_body, 'idempotency_key': 'conflict'}).status_code == 409
    exported = client.post(url + '/exports', headers=a, json={'revision': 1, 'report_id': report['id'],
        'idempotency_key': 'e'})
    assert exported.status_code == 200
    export_url = '/workspace/research/exports/' + exported.json()['id'] + '/file'
    assert client.get(export_url, headers=a).status_code == 200
    assert client.get(export_url, headers=b).status_code == 404
    from lantai.research.exports import export_file
    from lantai.research.projects import ResearchService
    export_file(ResearchService(roots['a']), exported.json()['id']).write_text('tampered')
    assert client.get(export_url, headers=a).status_code == 422
    lifecycle = {'expected_revision': 1}
    assert client.post(url + '/archive', headers=b, json=lifecycle).status_code == 404
    assert client.post(url + '/delete', headers=a, json=lifecycle).status_code == 422
    assert client.post(url + '/archive', headers=a, json=lifecycle).status_code == 200
    assert client.get('/workspace/research/projects', headers=a).json()['items'] == []
    archived = client.get('/workspace/research/projects?archived=true', headers=a).json()
    assert archived['items'][0]['project_id'] == p['project_id'] and not archived['pending_deletions']
    assert client.post(url + '/restore', headers=a, json=lifecycle).status_code == 200
    assert client.get(url, headers=a).json()['report']['id'] == report['id']
    assert client.post(url + '/archive', headers=a, json=lifecycle).status_code == 200
    assert client.post(url + '/delete', headers=a, json={'expected_revision': 2}).status_code == 409
    assert client.post(url + '/delete', headers=a, json=lifecycle).json()['status'] == 'deleted'
    assert client.get(export_url, headers=a).status_code == 404
    assert source.is_file()


def test_api_budget_roundtrip_and_resume_without_losing_draft(tmp_path, monkeypatch):
    async def agent(request):
        return SimpleNamespace(workspace_dir=tmp_path, agent_id='a')
    monkeypatch.setattr(api, 'get_agent_for_request', agent)
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    body = {'question': 'A bounded project', 'source_paths': [], 'budget': {'review_calls': 1},
            'idempotency_key': 'budget-create'}
    p = client.post('/workspace/research/projects', json=body).json()
    assert p['budget']['review_calls'] == 1
    url = '/workspace/research/projects/' + p['project_id']
    report = client.post(url + '/reports', json={'revision': 1, 'text': 'A draft.', 'claims': [],
        'idempotency_key': 'budget-draft'}).json()
    revised = client.post(url + '/revisions', json={'expected_revision': 1, 'budget': {'review_calls': 3},
        'idempotency_key': 'budget-raise'})
    assert revised.status_code == 200, revised.text
    detail = client.get(url).json()
    assert detail['project']['revision'] == 2
    assert detail['project']['budget_remaining']['review_calls'] == 3
    assert detail['report']['id'] == report['id']
    assert client.post('/workspace/research/projects', json={**body, 'budget': {'review_calls': True},
        'idempotency_key': 'invalid-budget'}).status_code == 422


def test_api_brief_roundtrip_and_clear(tmp_path, monkeypatch):
    async def agent(request):
        return SimpleNamespace(workspace_dir=tmp_path, agent_id='a')
    monkeypatch.setattr(api, 'get_agent_for_request', agent)
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    body = {'question': 'Question', 'subquestions': ['Measurement?'], 'outline': ['Findings'],
            'idempotency_key': 'brief-create'}
    response = client.post('/workspace/research/projects', json=body)
    assert response.status_code == 200, response.text
    p = response.json()
    assert p['subquestions'] == body['subquestions']
    url = '/workspace/research/projects/' + p['project_id']
    revised = client.post(url + '/revisions', json={'expected_revision': 1, 'subquestions': [],
        'outline': ['Limitations'], 'idempotency_key': 'brief-clear'})
    assert revised.status_code == 200, revised.text
    p = client.get(url).json()['project']
    assert p['subquestions'] == [] and p['outline'] == ['Limitations']
    assert client.post('/workspace/research/projects', json={**body, 'outline': [''],
        'idempotency_key': 'invalid-brief'}).status_code == 422


def test_human_opinion_api_is_scoped_and_cannot_supply_a_pass(tmp_path, monkeypatch):
    from lantai.research.projects import ResearchService
    roots = {name: tmp_path / name for name in ('a', 'b')}
    s = ResearchService(roots['a'])
    p = s.create_project('Review', [], idempotency_key='p')
    r = s.save_report(p['project_id'], 1, 'A finding.', [], idempotency_key='r')
    async def agent(request):
        return SimpleNamespace(workspace_dir=roots[request.headers['x-agent-id']])
    monkeypatch.setattr(api, 'get_agent_for_request', agent)
    app = FastAPI(); app.include_router(api.router); client = TestClient(app)
    url = '/workspace/research/projects/' + p['project_id'] + '/human-reviews'
    body = {'revision': 1, 'report_id': r['id'], 'expected_review_id': None,
            'text': 'Needs clarification.', 'verdict': 'question', 'idempotency_key': 'h'}
    assert client.post(url, headers={'x-agent-id': 'b'}, json=body).status_code == 404
    result = client.post(url, headers={'x-agent-id': 'a'}, json=body)
    assert result.status_code == 200, result.text
    assert client.post(url, headers={'x-agent-id': 'a'}, json={**body, 'status': 'reviewed'}).status_code == 422
    assert client.post(url, headers={'x-agent-id': 'a'}, json={**body,
        'expected_review_id': 'outdated', 'idempotency_key': 'stale'}).status_code == 409
    assert s.project(p['project_id'])['review_status'] == 'not_reviewed'


def test_notes_api_preserves_anchor_scope_and_imports_separate_source(tmp_path, monkeypatch):
    from lantai.research.library import LibraryService
    from lantai.research.projects import ResearchService
    roots = {name: tmp_path / name for name in ('a', 'b')}
    library = LibraryService(roots['a'])
    work = library.import_items([{'title': 'Study'}], origin={'provider': 'manual'})['items'][0]['work_id']
    project = ResearchService(roots['a']).create_project('Question', [], idempotency_key='p')
    async def agent(request):
        return SimpleNamespace(workspace_dir=roots[request.headers['x-agent-id']])
    monkeypatch.setattr(api, 'get_agent_for_request', agent)
    app = FastAPI(); app.include_router(api.router)
    client = TestClient(app)
    url = '/workspace/research/library/works/' + work + '/notes'
    assert client.post(url, headers={'x-agent-id': 'b'}, json={'text': 'A note'}).status_code == 404
    assert client.post(url, headers={'x-agent-id': 'a'}, json={'text': 'A note', 'origin': {'provider': 'paper'}}).status_code == 422
    note = client.post(url, headers={'x-agent-id': 'a'}, json={'text': 'A note'}).json()
    url = '/workspace/research/library/notes/' + note['id'] + '/source'
    body = {'project_id': project['project_id'], 'revision': 1, 'idempotency_key': 'note'}
    assert client.post(url, headers={'x-agent-id': 'b'}, json=body).status_code == 404
    result = client.post(url, headers={'x-agent-id': 'a'}, json=body)
    assert result.status_code == 200, result.text
    assert result.json()['project']['revision'] == 2
    assert client.post(url, headers={'x-agent-id': 'a'}, json={**body, 'idempotency_key': 'stale'}).status_code == 409
