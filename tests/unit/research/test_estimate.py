"""Offline preflight evidence must never invent a whole-run price or duration."""
import pytest

from lantai.research.projects import ResearchService
from lantai.research.inference import reserve, finish
from lantai.research.store import RevisionConflict


def setup(tmp_path, **budget):
    path = tmp_path / 'wiki/raw/sources/input.txt'
    path.parent.mkdir(parents=True)
    path.write_text('A' * 7000)
    service = ResearchService(tmp_path)
    p = service.create_project('Read this source', ['wiki/raw/sources/input.txt'],
                               idempotency_key='create', budget=budget)
    return service, p


def test_estimate_is_read_only_and_remaining_reading_matches_budget(tmp_path):
    service, p = setup(tmp_path, reading_chars=6500)
    pid, sid = p['project_id'], p['source_versions'][0]
    service.read_source(pid, 1, sid, 0)
    before = service.project(pid)
    with service.store.transaction() as db:
        records = db.execute('SELECT COUNT(*) FROM records').fetchone()[0]
    result = service.estimate(pid, 1)
    assert result['reading']['remaining_chars'] == 1000
    assert result['reading']['additional_budget_chars'] == 1000
    assert result['reading']['budget_remaining_chars'] == 500
    assert result['reading']['fits_budget'] is False
    assert result['binding']['source_versions'] == [sid]
    assert result['binding']['revision'] == 1
    assert result['workflow']['cost_cny'] is None
    assert result['workflow']['seconds'] is None
    assert result['historical_calls'] == []
    assert service.project(pid) == before
    with service.store.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM records').fetchone()[0] == records


def test_estimate_revision_conflict_and_reading_refresh_fingerprint(tmp_path):
    service, p = setup(tmp_path)
    pid = p['project_id']
    before = service.estimate(pid, 1)
    service.read_source(pid, 1, p['source_versions'][0], 0)
    after = service.estimate(pid, 1)
    assert before['fingerprint'] != after['fingerprint']
    service.revise_project(pid, 1, budget={'reading_chars': 8000}, idempotency_key='revise')
    with pytest.raises(RevisionConflict):
        service.estimate(pid, 1)
    assert service.estimate(pid, 2)['reading']['remaining_chars'] == 1000


def test_historical_ranges_separate_config_surface_revision_and_unknowns(tmp_path):
    service, p = setup(tmp_path)
    pid = p['project_id']
    config = {'provider': 'fixture', 'model': 'fixture-model'}
    for surface, tokens, cost in [('chat', 100, 0), ('chat', 300, None), ('review', 900, 9)]:
        call, _ = reserve(service, pid, 1, config, surface)
        finish(service, call, tokens / 10, 'completed', {'total_tokens': tokens, 'cost_cny': cost})
    call, _ = reserve(service, pid, 1, {**config, 'model': 'another-model'}, 'chat')
    finish(service, call, 4, 'completed', None)
    call, _ = reserve(service, pid, 1, config, 'chat')
    finish(service, call, 999, 'interrupted', {'total_tokens': 999999, 'cost_cny': 999})
    result = service.estimate(pid, 1)
    assert result['excluded_incomplete_calls'] == 1
    rows = result['historical_calls']
    assert len(rows) == 3
    chat = next(r for r in rows if r['model'] == 'fixture-model' and r['surface'] == 'chat')
    assert chat['samples'] == 2
    assert chat['tokens'] == {'min': 100, 'max': 300, 'known_samples': 2}
    assert chat['cost_cny'] == {'min': 0, 'max': 0, 'known_samples': 1}
    assert result['workflow']['model_calls'] is None
    service.revise_project(pid, 1, question='A different question', idempotency_key='revise')
    assert service.estimate(pid, 2)['historical_calls'] == []


def test_running_reservation_is_not_observed_duration(tmp_path):
    service, p = setup(tmp_path)
    reserve(service, p['project_id'], 1, {'provider': 'fixture', 'model': 'fixture'}, 'chat')
    result = service.estimate(p['project_id'], 1)
    assert result['historical_calls'] == []
    assert result['excluded_incomplete_calls'] == 1
    assert 'model_seconds' in result['exhausted_budgets']
    assert result['workflow']['seconds'] is None


def test_empty_or_changed_sources_do_not_claim_full_coverage(tmp_path):
    service, p = setup(tmp_path)
    (tmp_path / 'wiki/raw/sources/input.txt').write_text('changed')
    result = service.estimate(p['project_id'], 1)
    assert result['reading']['status'] == 'partial'
    assert result['reading']['stale_sources'] == p['source_versions']
    empty = service.create_project('Search later', [], idempotency_key='empty')
    result = service.estimate(empty['project_id'], 1)
    assert result['reading']['status'] == 'unknown'
    assert result['reading']['remaining_chars'] == 0
    assert result['workflow']['seconds'] is None


def test_unparsed_pdf_is_partial_even_when_no_indexed_blocks_remain(tmp_path):
    from reportlab.pdfgen.canvas import Canvas
    service, _ = setup(tmp_path)
    path = tmp_path / 'wiki/raw/sources/scan.pdf'
    canvas = Canvas(str(path))
    canvas.rect(10, 10, 100, 100)
    canvas.showPage()
    canvas.save()
    p = service.create_project('Scan', ['wiki/raw/sources/scan.pdf'], idempotency_key='scan')
    result = service.estimate(p['project_id'], 1)
    assert result['reading']['status'] == 'partial'
    assert result['reading']['incomplete_sources'] == p['source_versions']
    assert result['workflow']['tokens'] is None


def test_estimate_api_is_agent_scoped_and_revision_guarded(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from lantai.app.routers import research as api
    roots = {name: tmp_path / name for name in ('a', 'b')}
    service, p = setup(roots['a'])

    async def agent(request):
        return SimpleNamespace(workspace_dir=roots[request.headers['x-agent-id']])

    monkeypatch.setattr(api, 'get_agent_for_request', agent)
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        url = '/workspace/research/projects/' + p['project_id'] + '/estimate'
        response = client.get(url + '?revision=1', headers={'x-agent-id': 'a'})
        assert response.status_code == 200, response.text
        assert response.json()['binding']['project_id'] == p['project_id']
        assert client.get(url + '?revision=1', headers={'x-agent-id': 'b'}).status_code == 404
        assert client.get(url, headers={'x-agent-id': 'a'}).status_code == 422
        service.revise_project(p['project_id'], 1, question='Next', idempotency_key='next')
        assert client.get(url + '?revision=1', headers={'x-agent-id': 'a'}).status_code == 409
