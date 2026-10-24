import asyncio
from pathlib import Path

import httpx
import pytest

from lantai.research.fulltext import fetch_open_pdf, import_open_fulltext
from lantai.research.library import LibraryService
from lantai.research.literature import _record
from lantai.research.projects import ResearchService


def identity_pdf(title, doi, *, metadata_title=None):
    from io import BytesIO
    from reportlab.pdfgen.canvas import Canvas
    data = BytesIO()
    canvas = Canvas(data)
    canvas.setTitle(metadata_title or title)
    canvas.drawString(40, 760, title)
    canvas.drawString(40, 740, doi)
    canvas.drawString(40, 720, 'Alice Smith')
    canvas.save()
    return data.getvalue()


def test_document_identity_requires_primary_title_and_exact_doi():
    from lantai.research.fulltext import match_pdf_identity
    csl = {'title': 'Careful measurements of research evidence', 'DOI': '10.1234/target'}
    receipt = match_pdf_identity(identity_pdf(csl['title'], csl['DOI']), csl)
    assert receipt['method'] == 'pdf_document_markers_v1'
    assert receipt['doi'] == csl['DOI']
    assert receipt['raw_hash'] and receipt['first_page_hash']
    with pytest.raises(ValueError, match='identity'):
        match_pdf_identity(identity_pdf('Entirely different research findings', csl['DOI']), csl)
    with pytest.raises(ValueError, match='identity'):
        match_pdf_identity(identity_pdf(csl['title'], '10.1234/target-other'), csl)
    # A reference on the first page is insufficient when the document declares
    # a different primary title.
    with pytest.raises(ValueError, match='identity'):
        match_pdf_identity(identity_pdf(csl['title'], csl['DOI'], metadata_title='Different paper'), csl)


def test_identity_uncertainty_requires_manual_import():
    from lantai.research.fulltext import match_pdf_identity
    with pytest.raises(ValueError, match='identity'):
        match_pdf_identity(identity_pdf('A title without an identifier', ''), {'title': 'A title without an identifier'})


def test_wrong_download_never_enters_project_or_library(tmp_path, monkeypatch):
    research = ResearchService(tmp_path)
    p = research.create_project('Q', [], policy='public_search', idempotency_key='p')
    library = LibraryService(tmp_path)
    work = library.import_items([{'title': 'Expected scientific research article', 'DOI': '10.1234/expected',
        'custom': {'oa_locations': [{'url': 'https://example.org/a.pdf', 'license': 'cc-by', 'is_oa': True}]}}],
        origin={'provider': 'test'})['items'][0]['work_id']
    async def download(url):
        return identity_pdf('Wrong scientific research article', '10.1234/wrong'), url
    def forbidden(*args):
        raise AssertionError('Wrong identity must be rejected before source import')
    monkeypatch.setattr('lantai.research.fulltext.fetch_open_pdf', download)
    monkeypatch.setattr('lantai.app.wiki.core.import_single_wiki_source', forbidden)
    with pytest.raises(ValueError, match='identity'):
        asyncio.run(import_open_fulltext(research, library, p['project_id'], 1, work, 0, 'wrong'))
    assert research.project(p['project_id'])['revision'] == 1
    assert not library.work(work)['source_versions']


def test_discovery_retains_bibliography_abstract_and_licensed_location():
    r = _record('openalex', {'doi': 'https://doi.org/10.1234/abc', 'title': 'Evidence',
        'publication_year': 2024, 'authorships': [{'author': {'display_name': 'Alice Smith'}}],
        'abstract_inverted_index': {'One': [0], 'result.': [1]},
        'best_oa_location': {'is_oa': True, 'license': 'cc-by', 'pdf_url': 'https://example.org/paper.pdf', 'version': 'publishedVersion'},
        'open_access': {'is_oa': True}, 'is_retracted': False})
    assert r['csl']['abstract'] == 'One result.'
    assert r['oa_locations'][0]['license'] == 'cc-by'
    assert r['oa_locations'][0]['version'] == 'publishedVersion'
    assert r['csl']['issued']['date-parts'] == [[2024]]


def test_private_redirect_is_revalidated_before_connection(monkeypatch):
    seen = []
    async def resolve(url):
        seen.append(url)
        if '127.0.0.1' in url: raise ValueError('Private address rejected')
        return object()
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def stream(self, method, url):
            class Response:
                async def __aenter__(self):
                    return httpx.Response(302, headers={'location': 'https://127.0.0.1/secret'}, request=httpx.Request('GET', url))
                async def __aexit__(self, *args): pass
            return Response()
    monkeypatch.setattr('lantai.research.fulltext._public_endpoint', resolve)
    monkeypatch.setattr('lantai.research.fulltext.create_pinned_mcp_http_client', lambda endpoint: Client())
    with pytest.raises(ValueError, match='Private'):
        asyncio.run(fetch_open_pdf('https://example.org/paper.pdf'))
    assert len(seen) == 2


def test_local_policy_blocks_download_before_network(tmp_path, monkeypatch):
    research = ResearchService(tmp_path)
    p = research.create_project('Q', [], policy='offline', idempotency_key='p')
    library = LibraryService(tmp_path)
    work = library.import_items([{'title': 'Paper', 'DOI': '10.1234/abc', 'custom': {'oa_locations': [
        {'url': 'https://example.org/a.pdf', 'license': 'cc-by', 'is_oa': True}]}}], origin={'provider': 'test'})['items'][0]['work_id']
    async def forbidden(*a): raise AssertionError('No request permitted')
    monkeypatch.setattr('lantai.research.fulltext.fetch_open_pdf', forbidden)
    with pytest.raises(ValueError, match='policy'):
        asyncio.run(import_open_fulltext(research, library, p['project_id'], 1, work, 0, 'x'))
    assert research.project(p['project_id'])['source_paths'] == []


def test_unknown_license_is_not_automatic_download(tmp_path, monkeypatch):
    research = ResearchService(tmp_path)
    p = research.create_project('Q', [], policy='public_search', idempotency_key='p')
    library = LibraryService(tmp_path)
    work = library.import_items([{'title': 'Paper', 'DOI': '10.1234/abc', 'custom': {'oa_locations': [
        {'url': 'https://example.org/a.pdf', 'license': None, 'is_oa': True}]}}], origin={'provider': 'test'})['items'][0]['work_id']
    with pytest.raises(ValueError, match='license'):
        asyncio.run(import_open_fulltext(research, library, p['project_id'], 1, work, 0, 'x'))


def test_retry_after_revision_commit_resumes_without_another_download(tmp_path, monkeypatch):
    research = ResearchService(tmp_path)
    project = research.create_project('Q', [], policy='public_search', idempotency_key='p')
    library = LibraryService(tmp_path)
    work = library.import_items([{'title': 'Careful measurements of research evidence', 'DOI': '10.1234/target', 'custom': {'oa_locations': [
        {'url': 'https://example.org/a.pdf', 'license': 'cc-by', 'is_oa': True}]}}],
        origin={'provider': 'test'})['items'][0]['work_id']
    downloads = []
    async def download(url):
        downloads.append(url)
        return identity_pdf('Careful measurements of research evidence', '10.1234/target'), url
    def imported(workspace, filename, data, agent_id):
        path = workspace / 'wiki/raw/sources/authorized.txt'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('Authorized full text.')
        return SimpleNamespace(path='wiki/raw/sources/authorized.txt'), None, None
    from types import SimpleNamespace
    monkeypatch.setattr('lantai.research.fulltext.fetch_open_pdf', download)
    monkeypatch.setattr('lantai.app.wiki.core.import_single_wiki_source', imported)
    link = library.link_source
    failed = []
    def interrupted(*args):
        if not failed:
            failed.append(True)
            raise RuntimeError('Interrupted after project commit')
        return link(*args)
    monkeypatch.setattr(library, 'link_source', interrupted)
    args = (research, library, project['project_id'], 1, work, 0, 'resume')
    with pytest.raises(RuntimeError, match='Interrupted'):
        asyncio.run(import_open_fulltext(*args))
    assert research.project(project['project_id'])['revision'] == 2
    result = asyncio.run(import_open_fulltext(*args))
    repeated = asyncio.run(import_open_fulltext(*args))
    assert result['id'] == repeated['id']
    assert result['project']['revision'] == 2
    assert result['receipt']['fulltext_read'] is False
    assert result['receipt']['identity']['doi'] == '10.1234/target'
    assert result['receipt']['identity']['raw_hash'] == result['receipt']['raw_hash']
    assert result['receipt']['source_version_id'] in result['project']['source_versions']
    assert downloads == ['https://example.org/a.pdf']
    assert len(research.store.list('fulltext_import')) == 1


@pytest.mark.parametrize('identity', [None, {'method': 'pdf_document_markers_v1', 'raw_hash': 'other'}])
def test_incomplete_checkpoint_cannot_bypass_identity(tmp_path, monkeypatch, identity):
    research = ResearchService(tmp_path)
    p = research.create_project('Q', [], policy='public_search', idempotency_key='p')
    library = LibraryService(tmp_path)
    work = library.import_items([{'title': 'Careful measurements of research evidence', 'DOI': '10.1234/target'}],
        origin={'provider': 'test'})['items'][0]['work_id']
    payload = {'project': p['project_id'], 'revision': 1, 'work': work, 'location': 0}
    with research.store.transaction() as db:
        checkpoint = research.store.put(db, 'fulltext_checkpoint', {'raw_hash': 'original', 'identity': identity}, p['project_id'], 1)
        research.store.remember(db, 'fulltext-source:old', payload, checkpoint)
    async def forbidden(*args):
        raise AssertionError('Resume must not make another request')
    monkeypatch.setattr('lantai.research.fulltext.fetch_open_pdf', forbidden)
    with pytest.raises(ValueError, match='identity'):
        asyncio.run(import_open_fulltext(research, library, p['project_id'], 1, work, 0, 'old'))
    assert research.project(p['project_id'])['revision'] == 1
