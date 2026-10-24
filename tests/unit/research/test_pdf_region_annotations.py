"""Manual raster regions preserve provenance without manufacturing quote evidence."""
import pytest
from lantai.research.library import LibraryService
from .test_pdf_layout import make_source


def setup(tmp_path):
    service, path = make_source(tmp_path)
    project = service.create_project('regions', [path], idempotency_key='region')
    source = service.source(project['source_versions'][0])
    library = LibraryService(tmp_path)
    work = library.import_items([{'title': 'Study'}], origin={'provider': 'fixture'})['items'][0]['work_id']
    library.link_source(work, source['id'])
    anchor = dict(source_version_id=source['id'], source_hash=source['raw_hash'], page=1,
                  coordinate_system='pdf_raster_normalized_top_left_v1', x=.1, y=.2, width=.3, height=.4)
    return service, project, source, library, work, anchor


def test_region_survives_restart_and_note_import_without_becoming_verified_quote(tmp_path):
    service, project, source, library, work, anchor = setup(tmp_path)
    note = library.annotate(work, 'Check this chart', origin={'provider': 'user'}, region_anchor=anchor)
    assert note['region_anchor'] == {**anchor, 'verified_against_local_source': False}
    assert 'anchor' not in note
    assert note['version_id'] == library.work(work)['selected_version_id']
    assert LibraryService(tmp_path).work(work)['annotations'][0] == note
    assert library.annotate(work, 'Check this chart', origin={'provider': 'user'}, region_anchor=anchor)['id'] == note['id']
    result = library.add_note_source(note['id'], project['project_id'], 1, idempotency_key='note-region')
    imported = service.source(result['source_version_id'])
    assert imported['evidence_level'] == 'note'
    assert imported['source_origin']['region_anchor'] == note['region_anchor']
    assert result['fulltext_read'] is False


@pytest.mark.parametrize('change', [dict(page=0), dict(page=2), dict(page=True), dict(page=1.5),
    dict(x=-.1), dict(x=True), dict(x=float('nan')), dict(width=0), dict(width=.95),
    dict(height=float('inf')), dict(coordinate_system='native'), dict(source_hash='stale'),
    dict(quote='fabricated'), dict(verified_against_local_source=True)])
def test_invalid_region_is_atomic(tmp_path, change):
    _, _, _, library, work, anchor = setup(tmp_path)
    with pytest.raises(ValueError):
        library.annotate(work, 'bad', origin={'provider': 'user'}, region_anchor={**anchor, **change})
    assert not library.work(work)['annotations']


def test_region_requires_linked_work_and_intact_snapshot(tmp_path):
    _, _, source, library, work, anchor = setup(tmp_path)
    other = library.import_items([{'title': 'Other'}], origin={'provider': 'fixture'})['items'][0]['work_id']
    with pytest.raises(ValueError):
        library.annotate(other, 'wrong paper', origin={'provider': 'user'}, region_anchor=anchor)
    with pytest.raises(ValueError):
        library.annotate(work, 'mixed', origin={'provider': 'user'}, anchor={'source_version_id': source['id'], 'quote': 'Exact'}, region_anchor=anchor)
    (tmp_path / source['snapshot_path']).write_bytes(b'corrupted')
    with pytest.raises(ValueError, match='integrity'):
        library.annotate(work, 'corrupt', origin={'provider': 'user'}, region_anchor=anchor)
    assert not library.work(work)['annotations']


def test_region_api_scopes_agent_and_preserves_existing_notes(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from lantai.app.routers import research as api
    _, _, _, library, work, anchor = setup(tmp_path / 'a')
    async def agent(request):
        return SimpleNamespace(workspace_dir=tmp_path / request.headers['x-agent-id'])
    monkeypatch.setattr(api, 'get_agent_for_request', agent)
    app = FastAPI(); app.include_router(api.router)
    client = TestClient(app)
    url = f'/workspace/research/library/works/{work}/notes'
    assert client.post(url, headers={'x-agent-id': 'b'}, json={'text': 'note', 'region_anchor': anchor}).status_code == 404
    result = client.post(url, headers={'x-agent-id': 'a'}, json={'text': 'note', 'region_anchor': anchor})
    assert result.status_code == 200
    assert result.json()['region_anchor']['verified_against_local_source'] is False
    assert client.post(url, headers={'x-agent-id': 'a'}, json={'text': 'ordinary note'}).status_code == 200
    assert client.post(url, headers={'x-agent-id': 'a'}, json={'text': 'bad', 'region_anchor': {**anchor, 'width': 2}}).status_code == 422
    assert len(library.work(work)['annotations']) == 2


@pytest.mark.parametrize('change', ['crop', 'rotation'])
def test_manual_regions_use_rendered_frame_when_native_text_coordinates_are_unverified(tmp_path, change):
    from pypdf import PdfReader, PdfWriter
    service, path = make_source(tmp_path)
    page = PdfReader(tmp_path / path).pages[0]
    if change == 'crop':
        page.cropbox.lower_left = (30, 30)
    else:
        page.rotate(90)
    writer = PdfWriter(); writer.add_page(page)
    with (tmp_path / path).open('wb') as stream:
        writer.write(stream)
    project = service.create_project('rotated', [path], idempotency_key='rotated')
    source = service.source(project['source_versions'][0])
    library = LibraryService(tmp_path)
    work = library.import_items([{'title': 'Study'}], origin={'provider': 'fixture'})['items'][0]['work_id']
    library.link_source(work, source['id'])
    assert service.source_page(source['id'], 1)['overlay_allowed'] is False
    region = dict(source_version_id=source['id'], source_hash=source['raw_hash'], page=1,
                  coordinate_system='pdf_raster_normalized_top_left_v1', x=.1, y=.2, width=.3, height=.4)
    assert library.annotate(work, 'chart', origin={'provider': 'user'}, region_anchor=region)['region_anchor']['x'] == .1
