import httpx
import pytest
from lantai.research.projects import ResearchService
from lantai.research.library import LibraryService
from lantai.research.store import RevisionConflict


def setup_project(path):
    service = ResearchService(path)
    p = service.create_project('Sensitive question', [], policy='public_search',
        query_preview_required=True, idempotency_key='new')
    return service, p, LibraryService(path)


def test_preview_is_durable_exact_and_single_use(tmp_path):
    service, p, library = setup_project(tmp_path)
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={'message': {'items': []}, 'results': []})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        preview = library.search(p['project_id'], 1, 'sensitive wording', 3, client=client)
        assert preview['status'] == 'approval_required' and calls == []
        assert len(preview['requests']) == 2
        reloaded = LibraryService(tmp_path)
        assert reloaded.query_previews(p['project_id'])['items'][0]['query'] == 'sensitive wording'
        result = reloaded.decide_query(preview['id'], True, client=client)
        assert result['status'] == 'ok' and len(calls) == 2
        assert result['query_preview_id'] == preview['id']
        with pytest.raises(RevisionConflict):
            reloaded.decide_query(preview['id'], True, client=client)
    assert len(calls) == 2


def test_revision_invalidates_preview_and_preserves_preference(tmp_path):
    service, p, library = setup_project(tmp_path)
    preview = library.search(p['project_id'], 1, 'query')
    revised = service.revise_project(p['project_id'], 1, question='Changed', idempotency_key='rev')
    assert revised['query_preview_required'] is True
    with pytest.raises(RevisionConflict):
        library.decide_query(preview['id'], True)
    assert library.query_previews(p['project_id'])['items'][0]['status'] == 'stale'


@pytest.mark.asyncio
async def test_tool_reads_confirmed_results_without_approval_permission(tmp_path):
    import inspect
    import json
    from lantai.agents.tools.research import research_library
    from lantai.config.context import WorkspaceContext, current_workspace_context
    service, p, library = setup_project(tmp_path)
    preview = library.search(p['project_id'], 1, 'query')
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={'message': {'items': []}, 'results': []}))) as client:
        result = library.decide_query(preview['id'], True, client=client)
    token = current_workspace_context.set(WorkspaceContext(tmp_path, tmp_path, tmp_path))
    try:
        status = await research_library('search_status', project_id=p['project_id'])
        result_id = json.loads(status.content[0]['text'])['items'][0]['result_id']
        response = await research_library('search_result', search_id=result_id)
        assert json.loads(response.content[0]['text']) == result
        with pytest.raises(ValueError, match='wrong research record type'):
            await research_library('search_result', search_id=preview['id'])
    finally:
        current_workspace_context.reset(token)
    parameters = inspect.signature(research_library).parameters
    assert 'approve' not in parameters and 'query_preview_required' not in parameters


def test_cancel_does_not_send_and_purge_removes_preview(tmp_path):
    service, p, library = setup_project(tmp_path)
    preview = library.search(p['project_id'], 1, 'query')
    assert library.decide_query(preview['id'], False)['status'] == 'cancelled'
    with pytest.raises(RevisionConflict):
        library.decide_query(preview['id'], True)
    service.archive_project(p['project_id'], 1)
    service.delete_project(p['project_id'], 1)
    with library.store.transaction() as db:
        assert not db.execute('SELECT 1 FROM research_query_previews').fetchone()


def test_concurrent_confirmation_dispatches_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    service, p, library = setup_project(tmp_path)
    preview = library.search(p['project_id'], 1, 'query')
    calls = []
    barrier = Barrier(2)
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'message': {'items': []}, 'results': []})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        def approve(_):
            instance = LibraryService(tmp_path)
            barrier.wait()
            try:
                return instance.decide_query(preview['id'], True, client=client)['status']
            except RevisionConflict:
                return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(approve, range(2)))
    assert sorted(outcomes) == ['conflict', 'ok']
    assert len(calls) == 2


def test_query_api_is_agent_scoped_and_results_are_readable(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from lantai.app.routers import research as api
    roots = {name: tmp_path / name for name in ('a', 'b')}
    service, p, library = setup_project(roots['a'])
    async def agent(request):
        return SimpleNamespace(workspace_dir=roots[request.headers['x-agent-id']])
    monkeypatch.setattr(api, 'get_agent_for_request', agent)
    app = FastAPI(); app.include_router(api.router)
    client = TestClient(app)
    header = {'x-agent-id': 'a'}
    body = {'project_id': p['project_id'], 'revision': 1, 'query': '10.5555/example'}
    preview = client.post('/workspace/research/library/search', headers=header, json=body).json()
    path = '/workspace/research/library/query-previews/' + preview['id'] + '/decision'
    assert client.post(path, headers={'x-agent-id': 'b'}, json={'approve': True}).status_code == 404
    assert client.post(path, headers=header, json={'approve': False}).json()['status'] == 'cancelled'
    assert client.post(path, headers=header, json={'approve': True}).status_code == 409
    with library.store.transaction() as db:
        record = library.store.put(db, 'search', {'papers': [], 'sources': {}, 'status': 'ok'}, p['project_id'], 1)
    path = '/workspace/research/library/searches/' + record['id']
    assert client.get(path, headers=header).json()['status'] == 'ok'
    assert client.get(path, headers={'x-agent-id': 'b'}).status_code == 404


@pytest.mark.parametrize('sanitized', [False, True])
def test_preview_and_consumed_decision_survive_backup(tmp_path, sanitized):
    import zipfile
    from types import SimpleNamespace
    from lantai.backup._ops.create_helpers import add_agent_workspaces
    source = tmp_path / 'source'
    service, p, library = setup_project(source)
    preview = library.search(p['project_id'], 1, 'sensitive query')
    library.decide_query(preview['id'], False)
    archive = tmp_path / 'backup.zip'
    with zipfile.ZipFile(archive, 'w') as output:
        assert add_agent_workspaces(output, [('researcher', SimpleNamespace(workspace_dir=source))], sanitized=sanitized)
    restored = tmp_path / 'restored'
    with zipfile.ZipFile(archive) as saved:
        index = next(name for name in saved.namelist() if name.endswith('.lantai/research/index.sqlite3'))
        target = restored / '.lantai/research/index.sqlite3'
        target.parent.mkdir(parents=True)
        target.write_bytes(saved.read(index))
    library = LibraryService(restored)
    assert library.query_previews(p['project_id'])['items'][0]['status'] == 'cancelled'
    with pytest.raises(RevisionConflict):
        library.decide_query(preview['id'], True)
