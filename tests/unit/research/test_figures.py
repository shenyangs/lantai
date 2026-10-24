import base64
from io import BytesIO
from zipfile import ZipFile

import pytest
from PIL import Image
from pypdf import PdfReader

from lantai.research.projects import ResearchService, RevisionConflict
from lantai.research.exports import export_report, export_file


def png(size=(120, 60)):
    stream = BytesIO()
    Image.new('RGB', size, '#407080').save(stream, format='PNG')
    return stream.getvalue()


def setup(tmp_path):
    service = ResearchService(tmp_path)
    project = service.create_project('研究图像', [], idempotency_key='p')
    return service, project['project_id']


def upload(service, project, raw=None, key='figure'):
    from lantai.research.figures import FigureService
    return FigureService(service.workspace).upload(project, 1, base64.b64encode(raw or png()).decode(), idempotency_key=key)


def test_uploaded_figure_is_immutable_and_embedded_in_all_formats(tmp_path):
    service, project = setup(tmp_path)
    figure = upload(service, project)
    assert 'data' not in figure and figure['width'] == 120
    assert upload(service, project)['id'] == figure['id']
    text = f"# 结果\n\n![图一：观察结果](research-figure:{figure['id']})\n\n图中结果仍需人工核对。"
    report = service.save_report(project, 1, text, [], idempotency_key='r')
    assert report['figures'][0]['sha256'] == figure['sha256']
    for format in ('markdown', 'docx', 'pdf'):
        result = export_report(service, project, 1, report['id'], format, 'numeric', format)
        path = export_file(service, result['id'])
        assert result['citation_audit']['embedded_figure_count'] == 1
        assert result['citation_audit']['table_figure_reference_check'] == 'not_semantically_verified'
        if format == 'markdown':
            assert 'data:image/png;base64,' + base64.b64encode(png()).decode() in path.read_text()
        elif format == 'docx':
            with ZipFile(path) as archive:
                assert any(archive.read(name) == png() for name in archive.namelist() if name.startswith('word/media/'))
        else:
            assert sum(len(page.images) for page in PdfReader(path).pages) == 1


def test_scope_integrity_and_revision_are_enforced(tmp_path):
    service, project = setup(tmp_path)
    figure = upload(service, project)
    other = service.create_project('另一项目', [], idempotency_key='other')['project_id']
    with pytest.raises(ValueError, match='project'):
        service.save_report(other, 1, f"![图](research-figure:{figure['id']})", [], idempotency_key='bad')
    from lantai.research.figures import FigureService
    with pytest.raises(RevisionConflict):
        FigureService(service.workspace).upload(project, 2, base64.b64encode(png()).decode(), idempotency_key='stale')
    with pytest.raises(FileNotFoundError):
        FigureService(tmp_path / 'other-agent').load(project, figure['id'])
    with service.store.transaction() as db:
        db.execute("UPDATE records SET payload=payload || ' ' WHERE id=?", (figure['id'],))
    with pytest.raises(ValueError, match='integrity'):
        service.save_report(project, 1, f"![图](research-figure:{figure['id']})", [], idempotency_key='corrupt')


def test_invalid_images_rejected_and_code_or_external_markup_not_fetched(tmp_path, monkeypatch):
    service, project = setup(tmp_path)
    with pytest.raises(ValueError, match='PNG|JPEG'):
        upload(service, project, b'<svg>not a raster image</svg>')
    def forbidden(*args, **kwargs):
        raise AssertionError('export must not fetch remote images')
    monkeypatch.setattr('urllib.request.urlopen', forbidden)
    text = '```md\n![example](research-figure:figure-missing)\n```\n\n![external](https://example.com/private.png)'
    report = service.save_report(project, 1, text, [], idempotency_key='literal')
    assert report['figures'] == []
    result = export_report(service, project, 1, report['id'], 'pdf', 'numeric', 'literal-export')
    assert not any(page.images for page in PdfReader(export_file(service, result['id'])).pages)
    with pytest.raises(ValueError, match='standalone'):
        service.save_report(project, 1, 'inline ![x](research-figure:figure-missing)', [], expected_report_id=report['id'], idempotency_key='inline')


@pytest.mark.parametrize('sanitized', [False, True])
def test_backup_and_project_deletion_include_figure_bytes(tmp_path, sanitized):
    from types import SimpleNamespace
    from lantai.backup._ops.create_helpers import add_agent_workspaces
    from lantai.research.figures import FigureService
    service, project = setup(tmp_path)
    figure = upload(service, project)
    report = service.save_report(project, 1, f"![图](research-figure:{figure['id']})", [], idempotency_key='report')
    archive = tmp_path.with_name(tmp_path.name + '-backup.zip')
    with ZipFile(archive, 'w') as output:
        assert add_agent_workspaces(output, [('researcher', SimpleNamespace(workspace_dir=tmp_path))], sanitized=sanitized)
    restored = tmp_path / 'restored'
    target = restored / '.lantai/research/index.sqlite3'
    target.parent.mkdir(parents=True)
    with ZipFile(archive) as saved:
        target.write_bytes(saved.read(next(n for n in saved.namelist() if n.endswith('.lantai/research/index.sqlite3'))))
    clone = ResearchService(restored)
    result = export_report(clone, project, 1, report['id'], 'docx', 'numeric', 'restored')
    with ZipFile(export_file(clone, result['id'])) as document:
        assert any(document.read(n) == png() for n in document.namelist() if n.startswith('word/media/'))
    service.archive_project(project, 1)
    assert service.delete_project(project, 1)['status'] == 'deleted'
    with pytest.raises(FileNotFoundError):
        FigureService(tmp_path).load(project, figure['id'])
    assert FigureService(restored).load(project, figure['id'])['sha256'] == figure['sha256']


def test_limits_and_citation_markup_overlap(tmp_path, monkeypatch):
    from lantai.research import figures
    service, project = setup(tmp_path)
    figure = upload(service, project)
    monkeypatch.setattr(figures, 'PROJECT_BYTES', len(png()))
    with pytest.raises(ValueError, match='storage'):
        upload(service, project, key='extra')
    text = f"![图](research-figure:{figure['id']})"
    with pytest.raises(ValueError, match='outside figure'):
        service.save_report(project, 1, text, [{'text': text, 'evidence': []}], idempotency_key='overlap')
    assert service.project(project)['report_id'] is None


def test_upload_api_is_agent_scoped_and_returns_no_image_body(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from lantai.app.routers import research as api
    service, project = setup(tmp_path / 'a')
    async def agent(request):
        return SimpleNamespace(workspace_dir=tmp_path / request.headers['x-agent-id'])
    monkeypatch.setattr(api, 'get_agent_for_request', agent)
    app = FastAPI(); app.include_router(api.router)
    client = TestClient(app)
    url = f'/workspace/research/projects/{project}/figures'
    body = {'revision': 1, 'data': base64.b64encode(png()).decode(), 'idempotency_key': 'api'}
    assert client.post(url, headers={'x-agent-id': 'b'}, json=body).status_code == 404
    response = client.post(url, headers={'x-agent-id': 'a'}, json=body)
    assert response.status_code == 200, response.text
    assert 'data' not in response.json()
    assert client.post(url, headers={'x-agent-id': 'a'}, json={**body, 'revision': 2, 'idempotency_key': 'stale'}).status_code == 409


def test_export_keeps_figure_like_source_notes_literal(tmp_path):
    service = ResearchService(tmp_path)
    project = service.create_project('Figure syntax', [], scope='Quoted syntax: research-figure:figure-example', idempotency_key='p')['project_id']
    figure = upload(service, project)
    report = service.save_report(project, 1, f"![图](research-figure:{figure['id']})", [], idempotency_key='r')
    for format in ('markdown', 'docx', 'pdf'):
        result = export_report(service, project, 1, report['id'], format, 'numeric', format)
        assert export_file(service, result['id']).stat().st_size > 0


def test_tall_figure_and_long_caption_fit_export_pages(tmp_path):
    service, project = setup(tmp_path)
    figure = upload(service, project, png((60, 2000)))
    report = service.save_report(project, 1, f"![{'长图说明' * 75}](research-figure:{figure['id']})\n\n后续正文", [], idempotency_key='r')
    result = export_report(service, project, 1, report['id'], 'pdf', 'numeric', 'pdf')
    reader = PdfReader(export_file(service, result['id']))
    assert sum(len(page.images) for page in reader.pages) == 1
    assert '后续正文' in ''.join(page.extract_text() for page in reader.pages)


def test_repeated_references_cannot_expand_export_without_limit(tmp_path, monkeypatch):
    from lantai.research import figures
    service, project = setup(tmp_path)
    figure = upload(service, project)
    monkeypatch.setattr(figures, 'PROJECT_BYTES', len(png()))
    line = f"![图](research-figure:{figure['id']})"
    with pytest.raises(ValueError, match='Report figures'):
        service.save_report(project, 1, line + '\n' + line, [], idempotency_key='oversized-report')
    assert service.project(project)['report_id'] is None
