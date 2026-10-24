"""Preview must render the immutable source and decline uncertain overlays."""
import base64
import io

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter
from .test_pdf_layout import make_source


def source(service, path):
    project = service.create_project('preview', [path], idempotency_key='preview')
    return service.source(project['source_versions'][0])


def test_preview_snapshot_and_geometry(tmp_path):
    service, path = make_source(tmp_path)
    original = source(service, path)
    (tmp_path / path).write_bytes(b'new content')
    preview = service.source_page(original['id'], 1)
    assert preview['raw_hash'] == original['raw_hash']
    assert preview['overlay_allowed'] is True
    image = Image.open(io.BytesIO(base64.b64decode(preview['png_base64'])))
    assert image.size == (preview['width'], preview['height'])
    region = original['pages'][0]['regions'][0]
    crop = image.crop(tuple(round(n) for n in (
        region['x'] * image.width, region['y'] * image.height,
        (region['x'] + region['width']) * image.width,
        (region['y'] + region['height']) * image.height)))
    assert crop.convert('L').getextrema()[0] < 100
    assert max(image.size) <= 1800


@pytest.mark.parametrize('change', ['crop', 'rotation'])
def test_uncertain_native_frame_shows_page_without_overlay(tmp_path, change):
    service, path = make_source(tmp_path)
    pdf = PdfReader(tmp_path / path)
    page = pdf.pages[0]
    if change == 'crop':
        page.cropbox.lower_left = (30, 30)
    else:
        page.rotate(90)
    writer = PdfWriter()
    writer.add_page(page)
    with (tmp_path / path).open('wb') as stream:
        writer.write(stream)
    result = service.source_page(source(service, path)['id'], 1)
    assert result['png_base64']
    assert result['overlay_allowed'] is False
    assert result['overlay_reason'] == 'native_coordinate_frame_unverified'


def test_integrity_page_bounds_and_agent_isolation(tmp_path):
    from lantai.research.projects import ResearchService
    service, path = make_source(tmp_path)
    original = source(service, path)
    for page in (0, 2, -1):
        with pytest.raises(ValueError, match='page'):
            service.source_page(original['id'], page)
    with pytest.raises(FileNotFoundError):
        ResearchService(tmp_path / 'other').source_page(original['id'], 1)
    (tmp_path / original['snapshot_path']).write_bytes(b'corrupt snapshot')
    with pytest.raises(ValueError, match='integrity'):
        service.source_page(original['id'], 1)


def test_page_api_agent_scope_and_invalid_page(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from lantai.app.routers import research as api
    service, path = make_source(tmp_path / 'a')
    original = source(service, path)
    async def agent(request):
        return SimpleNamespace(workspace_dir=tmp_path / request.headers['x-agent-id'])
    monkeypatch.setattr(api, 'get_agent_for_request', agent)
    app = FastAPI(); app.include_router(api.router)
    client = TestClient(app)
    url = f"/workspace/research/sources/{original['id']}/pages/"
    assert client.get(url + '1', headers={'x-agent-id': 'b'}).status_code == 404
    assert client.get(url + '0', headers={'x-agent-id': 'a'}).status_code == 422
    result = client.get(url + '1', headers={'x-agent-id': 'a'})
    assert result.status_code == 200
    assert result.json()['raw_hash'] == original['raw_hash']
    assert 'snapshot_path' not in result.json()


def test_ocr_render_frame_allows_rotated_scan_and_path_escape_fails(tmp_path):
    from lantai.research.pdf_preview import preview_page
    service, path = make_source(tmp_path)
    pdf = PdfReader(tmp_path / path)
    writer = PdfWriter(); writer.add_page(pdf.pages[0].rotate(90))
    with (tmp_path / path).open('wb') as stream:
        writer.write(stream)
    original = source(service, path)
    # OCR regions were measured on PDFium's rendered page, including rotation.
    original['pages'][0]['text_origin'] = 'local_vision_ocr'
    original['pages'][0]['coordinate_system'] = 'normalized_top_left'
    assert preview_page(tmp_path, original, 1)['overlay_allowed'] is True
    original['snapshot_path'] = '../outside.pdf'
    with pytest.raises(ValueError):
        preview_page(tmp_path, original, 1)
