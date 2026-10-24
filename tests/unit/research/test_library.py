"""Bibliographic identity is separate from source evidence and reading."""
import json
from pathlib import Path

import httpx
import pytest

from lantai.research.library import LibraryService


@pytest.fixture
def library(tmp_path):
    return LibraryService(tmp_path)


def item(title='Original title', doi='10.1234/example', year=2024):
    return {'id': 'external', 'type': 'article-journal', 'title': title,
            'DOI': doi, 'author': [{'given': 'Jane', 'family': 'Smith'}],
            'issued': {'date-parts': [[year]]}, 'container-title': 'Research Journal',
            'volume': '12', 'issue': '3', 'page': '1-9'}


def test_doi_dedup_preserves_conflicting_versions_without_overwrite(library):
    first = library.import_items([item()], origin={'provider': 'manual'})['items'][0]
    same = library.import_items([item()], origin={'provider': 'bibtex'})['items'][0]
    assert same['work_id'] == first['work_id']
    conflict = library.import_items([item('Different title')], origin={'provider': 'crossref'})['items'][0]
    work = library.work(first['work_id'])
    assert len(work['versions']) == 2
    assert work['selected_version_id'] == first['version_id']
    assert 'metadata_conflict' in work['quality_issues']
    assert work['csl']['title'] == 'Original title'
    assert work['evidence_level'] == 'metadata'
    assert not work['fulltext_read']
    library.select_version(work['work_id'], conflict['version_id'], work['selected_version_id'])
    assert library.work(work['work_id'])['csl']['title'] == 'Different title'


def test_import_export_roundtrips_preserve_identity_names_dates_and_extra(library):
    value = item()
    value['author'] += [{'literal': 'Research Consortium'}]
    value['custom'] = {'zotero_key': 'ABCDEF12', 'edition': 'preprint', 'note': '中文摘要'}
    library.import_items([value], origin={'provider': 'manual'})
    for format in ('csl-json', 'bibtex', 'ris'):
        rendered = library.export(format)
        other = LibraryService(library.workspace / format)
        imported = other.import_text(rendered, format)
        assert imported['items']
        actual = other.list()[0]['csl']
        assert actual['DOI'] == value['DOI']
        assert actual['author'] == value['author']
        assert actual['issued'] == value['issued']
        assert actual['custom'] == value['custom']


def test_different_dois_and_preprints_are_related_only_with_explicit_link(library):
    values = library.import_items([item(), item(doi='10.1234/preprint')], origin={'provider': 'manual'})['items']
    assert values[0]['work_id'] != values[1]['work_id']
    library.relate(values[0]['work_id'], values[1]['work_id'], 'published-version')
    assert library.work(values[1]['work_id'])['relations'][0]['relation'] == 'published-version'


def test_malformed_import_is_atomic_and_agent_isolated(library):
    with pytest.raises(ValueError):
        library.import_items([item(), {'type': 'article-journal'}], origin={'provider': 'manual'})
    assert library.list() == []
    work = library.import_items([item()], origin={'provider': 'manual'})['items'][0]
    with pytest.raises(FileNotFoundError):
        LibraryService(library.workspace / 'other').work(work['work_id'])


def test_zotero_is_readonly_instance_scoped_and_attachments_do_not_imply_read(library):
    from lantai.research.zotero import import_local_zotero
    requests = []

    def response(request):
        requests.append((request.method, str(request.url)))
        headers = {'Zotero-Server-ID': 'library-instance-a', 'Last-Modified-Version': '7'}
        data = [{'key': 'ABCDEF12', 'version': 7, 'data': {'key': 'ABCDEF12', 'version': 7,
                 'itemType': 'journalArticle', 'title': 'Original title', 'DOI': '10.1234/example',
                 'creators': [{'creatorType': 'author', 'firstName': 'Jane', 'lastName': 'Smith'}],
                 'date': '2024', 'publicationTitle': 'Research Journal'}},
                {'key': 'NOTE1234', 'version': 1, 'data': {'itemType': 'note', 'parentItem': 'ABCDEF12', 'note': '<p>Read later</p>'}},
                {'key': 'ATTACH12', 'version': 1, 'data': {'itemType': 'attachment', 'parentItem': 'ABCDEF12', 'contentType': 'application/pdf', 'title': 'Paper.pdf'}}]
        if request.url.path.endswith('/items'):
            return httpx.Response(200, json=data if request.url.params.get('start') == '0' else [], headers=headers)
        return httpx.Response(200, json={'version': 3}, headers=headers)

    client = httpx.Client(transport=httpx.MockTransport(response))
    result = import_local_zotero(library, client=client)
    assert result['status'] == 'ok'
    assert all(method == 'GET' for method, _ in requests)
    assert all(url.startswith('http://127.0.0.1:23119/api/') for _, url in requests)
    work = library.list()[0]
    assert len(work['annotations']) == 2
    assert work['evidence_level'] == 'metadata'
    assert not work['fulltext_read']
    assert result['origin']['instance_id'] == 'library-instance-a'


def test_zotero_permission_denial_is_observable_and_never_fake_success(library):
    from lantai.research.zotero import import_local_zotero
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(403)))
    result = import_local_zotero(library, client=client)
    assert result['status'] == 'permission_required'
    assert library.list() == []


def test_offline_projects_reject_public_search_before_network(library):
    from lantai.research.projects import ResearchService
    p = ResearchService(library.workspace).create_project('Question', [], policy='offline', idempotency_key='offline')
    called = []
    client = httpx.Client(transport=httpx.MockTransport(lambda request: called.append(request)))
    with pytest.raises(ValueError, match='policy'):
        library.search(p['project_id'], 1, 'Query', client=client)
    assert called == []
