"""Ruled tables are snapshot-bound reading aids, never verified citations."""
import pytest
from reportlab.pdfgen.canvas import Canvas
from lantai.research.projects import ResearchService


def table_source(tmp_path):
    path = tmp_path / 'wiki/raw/sources/table.pdf'
    path.parent.mkdir(parents=True)
    canvas = Canvas(str(path), pagesize=(600, 800))
    for y in (700, 660, 620):
        canvas.line(40, y, 440, y)
    for x in (40, 240, 440):
        canvas.line(x, 620, x, 700)
    for x, y, text in ((50, 675, 'Group'), (250, 675, 'Value*'),
                       (50, 635, 'A'), (250, 635, '12.5')):
        canvas.drawString(x, y, text)
    canvas.drawString(40, 590, '* Units are illustrative only.')
    canvas.showPage()
    canvas.drawString(40, 700, 'No table here.')
    canvas.showPage(); canvas.save()
    service = ResearchService(tmp_path)
    project = service.create_project('table', [str(path.relative_to(tmp_path))],
                                     policy='offline', idempotency_key='table')
    return service, service.source(project['source_versions'][0]), path


def test_tables_preserve_cells_context_and_snapshot_without_verification(tmp_path):
    service, source, path = table_source(tmp_path)
    path.write_bytes(b'changed original')
    before = service.source(source['id'])
    result = service.source_tables(source['id'], 1)
    assert result['source_version_id'] == source['id']
    assert result['raw_hash'] == source['raw_hash']
    assert result['page'] == 1
    assert result['status'] == 'needs_visual_review'
    assert len(result['tables']) == 1
    table = result['tables'][0]
    assert [[c['text'] if c else None for c in row] for row in table['rows']] == [['Group', 'Value*'], ['A', '12.5']]
    assert all(len(c['bbox']) == 4 for row in table['rows'] for c in row)
    assert table['header_status'] == 'unreviewed'
    assert table['footnote_status'] == 'unresolved'
    assert '* Units are illustrative only.' in result['page_text']
    assert result['verified_against_local_source'] is False
    assert service.source(source['id']) == before
    assert service.source_tables(source['id'], 2)['status'] == 'no_ruled_tables'


def test_tables_keep_integrity_bounds_and_agent_isolation(tmp_path):
    service, source, _ = table_source(tmp_path)
    for number in (0, -1, 3, True):
        with pytest.raises(ValueError, match='page'):
            service.source_tables(source['id'], number)
    with pytest.raises(FileNotFoundError):
        ResearchService(tmp_path / 'other').source_tables(source['id'], 1)
    (tmp_path / source['snapshot_path']).write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='integrity'):
        service.source_tables(source['id'], 1)


def test_table_parser_failure_does_not_break_original_page(tmp_path, monkeypatch):
    import pdfplumber
    service, source, _ = table_source(tmp_path)
    monkeypatch.setattr(pdfplumber, 'open', lambda *args: (_ for _ in ()).throw(RuntimeError('parser failed')))
    result = service.source_tables(source['id'], 1)
    assert result['status'] == 'unavailable'
    assert result['tables'] == []
    assert service.source_page(source['id'], 1)['png_base64']


@pytest.mark.parametrize('limit', ['MAX_CELLS', 'MAX_TEXT'])
def test_limits_are_explicit_without_partial_tables(tmp_path, monkeypatch, limit):
    from lantai.research import pdf_tables
    service, source, _ = table_source(tmp_path)
    monkeypatch.setattr(pdf_tables, limit, 1)
    result = service.source_tables(source['id'], 1)
    assert result['status'] == 'size_limit'
    assert result['tables'] == []
    assert result['page_text'] == ''
    assert service.source_page(source['id'], 1)['png_base64']


def test_merged_slot_distinct_from_an_empty_cell(tmp_path):
    path = tmp_path / 'wiki/raw/sources/merged.pdf'
    path.parent.mkdir(parents=True)
    canvas = Canvas(str(path), pagesize=(600, 800))
    for y in (700, 660, 620):
        canvas.line(40, y, 440, y)
    for x in (40, 440):
        canvas.line(x, 620, x, 700)
    canvas.line(240, 620, 240, 660)
    canvas.drawString(50, 675, 'Spanning heading')
    canvas.drawString(50, 635, 'A')
    canvas.showPage(); canvas.save()
    service = ResearchService(tmp_path)
    project = service.create_project('merged', [str(path.relative_to(tmp_path))], idempotency_key='merged')
    table = service.source_tables(project['source_versions'][0], 1)['tables'][0]
    assert table['rows'][0][0]['text'] == 'Spanning heading'
    assert table['rows'][0][1] is None
    assert table['rows'][1][1]['text'] == ''
    assert table['rows'][0][0]['bbox'][2] > table['rows'][1][0]['bbox'][2]


def test_tables_api_scope_and_invalid_page(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from lantai.app.routers import research as api
    _, source, _ = table_source(tmp_path / 'a')
    async def agent(request):
        return SimpleNamespace(workspace_dir=tmp_path / request.headers['x-agent-id'])
    monkeypatch.setattr(api, 'get_agent_for_request', agent)
    app = FastAPI(); app.include_router(api.router)
    client = TestClient(app)
    url = f"/workspace/research/sources/{source['id']}/pages/"
    assert client.get(url + '1/tables', headers={'x-agent-id': 'b'}).status_code == 404
    assert client.get(url + '0/tables', headers={'x-agent-id': 'a'}).status_code == 422
    result = client.get(url + '1/tables', headers={'x-agent-id': 'a'})
    assert result.status_code == 200
    assert result.json()['status'] == 'needs_visual_review'
    assert 'snapshot_path' not in result.json()
