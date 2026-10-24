"""PDF coordinates must belong to the exact immutable text; OCR is opt-in."""
from pathlib import Path

import pytest
from reportlab.pdfgen.canvas import Canvas
from lantai.research.projects import ResearchService


def make_source(tmp_path, text='Exact repeated repeated text.'):
    path = tmp_path / 'wiki/raw/sources/sample.pdf'
    path.parent.mkdir(parents=True)
    canvas = Canvas(str(path), pagesize=(600, 800))
    if text:
        canvas.drawString(40, 740, text)
    canvas.showPage()
    canvas.save()
    return ResearchService(tmp_path), str(path.relative_to(tmp_path))


def test_native_coordinates_preserve_exact_text_and_page(tmp_path):
    from pypdf import PdfReader
    service, path = make_source(tmp_path)
    original = PdfReader(tmp_path / path).pages[0].extract_text(extraction_mode='layout')
    project = service.create_project('coords', [path], idempotency_key='native')
    source = service.source(project['source_versions'][0])
    assert source['text'] == original
    page = source['pages'][0]
    assert page['coordinate_system'] == 'normalized_top_left'
    assert page['layout_status'] == 'mapped'
    assert [source['text'][r['start']:r['end']] for r in page['regions']] == ['Exact', 'repeated', 'repeated', 'text.']
    assert all(0 <= r['x'] < 1 and 0 <= r['y'] < 1 and r['width'] > 0 for r in page['regions'])
    read = service.read_source(project['project_id'], 1, source['id'], 0)
    assert read['blocks'][0]['regions'] == page['regions']


def test_mismatched_reading_order_never_invents_coordinates():
    from lantai.research.pdf_layout import map_words
    word = dict(text='second', x0=0, x1=20, top=0, bottom=10)
    assert map_words('first second', [word], 100, 100) == []


def test_ocr_default_off_and_explicit_revision_is_immutable(tmp_path, monkeypatch):
    from lantai.research import pdf_layout
    service, path = make_source(tmp_path, '')
    calls = []
    def recognize(*args):
        calls.append(args)
        return [dict(text='Scanned evidence', confidence=.65, x=.1, y=.1, width=.6, height=.05)]
    monkeypatch.setattr(pdf_layout, 'recognize_page', recognize)
    first = service.create_project('scan', [path], idempotency_key='off', policy='offline')
    old = service.source(first['source_versions'][0])
    assert not calls
    second = service.revise_project(first['project_id'], 1, local_ocr=True, idempotency_key='on')
    new = service.source(second['source_versions'][0])
    assert len(calls) == 1
    assert new['text'] == 'Scanned evidence'
    assert new['coverage'] == 'partial'
    assert new['pages'][0]['issue'] == 'ocr_low_confidence'
    assert new['pages'][0]['regions'][0]['confidence'] == .65
    assert service.source(old['id'])['text'] == ''
    assert second['local_ocr'] is True
    assert second['review_status'] == 'not_reviewed'


def test_ocr_failure_preserves_native_pages_and_remains_partial(tmp_path, monkeypatch):
    from lantai.research import pdf_layout
    service, path = make_source(tmp_path, '')
    monkeypatch.setattr(pdf_layout, 'recognize_page', lambda *args: [])
    project = service.create_project('scan', [path], local_ocr=True, idempotency_key='failed')
    source = service.source(project['source_versions'][0])
    assert source['coverage'] == 'partial'
    assert source['pages'][0]['issue'] == 'local_ocr_unavailable_or_no_text'
    assert source['text'] == ''


def test_ocr_setting_validated_before_reading_sources(tmp_path):
    service = ResearchService(tmp_path)
    with pytest.raises(ValueError, match='boolean'):
        service.create_project('scan', [], local_ocr='yes', idempotency_key='invalid')


def test_mixed_pages_keep_global_offsets_and_ocr_preference(tmp_path, monkeypatch):
    from pypdf import PdfReader
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from lantai.research import pdf_layout
    path = tmp_path / 'wiki/raw/sources/mixed.pdf'
    path.parent.mkdir(parents=True)
    pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
    canvas = Canvas(str(path), pagesize=(600, 800))
    canvas.setFont('STSong-Light', 14)
    canvas.drawString(40, 740, '中文样本：120，比例18.5%。')
    canvas.showPage()
    canvas.showPage()
    canvas.save()
    calls = []
    def recognize(*args):
        calls.append(args[1])
        return [dict(text='第二页扫描结果', confidence=.95, x=.1, y=.1, width=.6, height=.05)]
    monkeypatch.setattr(pdf_layout, 'recognize_page', recognize)
    service = ResearchService(tmp_path)
    project = service.create_project('mixed', [str(path.relative_to(tmp_path))], local_ocr=True,
                                     policy='offline', idempotency_key='mixed')
    source = service.source(project['source_versions'][0])
    first, second = source['pages']
    original = PdfReader(path).pages[0].extract_text(extraction_mode='layout')
    assert source['text'][:first['end']] == original
    assert first['layout_status'] == 'mapped'
    assert calls == [2]
    assert second['issue'] == 'ocr_requires_visual_verification'
    region = second['regions'][0]
    assert region['start'] > first['end']
    assert source['text'][region['start']:region['end']] == '第二页扫描结果'
    budget = service.revise_project(project['project_id'], 1, budget={'model_calls': 200}, idempotency_key='budget')
    assert budget['local_ocr'] is True
    assert budget['source_versions'] == project['source_versions']
    assert calls == [2]
    refreshed = service.revise_project(project['project_id'], 2, refresh_sources=True, idempotency_key='refresh')
    assert refreshed['local_ocr'] is True
    assert calls == [2, 2]


def test_omitted_ocr_keeps_legacy_create_idempotency_payload(tmp_path):
    from lantai.app.routers.research import ProjectRequest
    service, path = make_source(tmp_path)
    request = ProjectRequest(question='legacy', source_paths=[path], idempotency_key='legacy')
    assert request.local_ocr is None
    first = service.create_project(**request.model_dump())
    legacy_payload = dict(question='legacy', paths=[path], policy='local_sources', scope='',
                          source_levels={path: 'fulltext'}, exclusions='', output='report',
                          query_preview_required=False)
    with service.store.transaction() as db:
        assert service.store.request(db, 'legacy', legacy_payload)['id'] == first['id']
    assert service.create_project(**request.model_dump())['id'] == first['id']


def test_vision_bridge_uses_native_options_and_preserves_default_limit(tmp_path, monkeypatch):
    """Python dict raises on absent Vision keys on the current PyObjC bridge."""
    import sys
    from types import SimpleNamespace as NS
    from lantai.app.wiki.core import _extract_layout_lines_from_image_with_vision
    native_options = object()
    levels = []
    candidate = NS(string=lambda: 'line', confidence=lambda: .9)
    observation = NS(topCandidates_=lambda n: [candidate], boundingBox=lambda: NS(
        origin=NS(x=.1, y=.5), size=NS(width=.2, height=.02)))
    request = NS(setRecognitionLevel_=levels.append, setUsesLanguageCorrection_=lambda v: None,
                 setRecognitionLanguages_=lambda v: None, results=lambda: [observation] * 301)
    def handler(image, options):
        if options is not native_options:
            raise ValueError('NSInvalidArgumentException - key does not exist')
        return NS(performRequests_error_=lambda *args: (True, None))
    classes = {'VNRecognizeTextRequest': NS(alloc=lambda: NS(init=lambda: request)),
               'VNImageRequestHandler': NS(alloc=lambda: NS(initWithCIImage_options_=handler))}
    monkeypatch.setitem(sys.modules, 'objc', NS(loadBundle=lambda name, ns, **kwargs: ns.update(classes)))
    monkeypatch.setitem(sys.modules, 'Foundation', NS(NSURL=NS(fileURLWithPath_=str),
                         NSDictionary=NS(dictionary=lambda: native_options)))
    monkeypatch.setitem(sys.modules, 'Quartz', NS(CIImage=NS(imageWithContentsOfURL_=lambda v: object())))
    assert len(_extract_layout_lines_from_image_with_vision(tmp_path / 'image.png')) == 300
    assert len(_extract_layout_lines_from_image_with_vision(tmp_path / 'image.png', line_limit=None, accurate=True)) == 301
    assert levels == [1, 0]
