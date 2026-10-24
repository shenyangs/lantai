import httpx
import pytest

from lantai.research.literature import search_literature


def test_deduplicates_doi_preserves_conflicts_and_never_claims_fulltext():
    def handler(request):
        if request.url.host == 'api.crossref.org':
            return httpx.Response(200, json={'message': {'items': [{'DOI':'10.1234/TEST', 'title':['First title'], 'author':[{'given':'A', 'family':'B'}]}]}})
        return httpx.Response(200, json={'results':[{'doi':'https://doi.org/10.1234/test', 'title':'Different title', 'is_retracted':True, 'authorships':[], 'open_access':{'is_oa':True}}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = search_literature('test', client=client)
    assert len(result['papers']) == 1
    paper = result['papers'][0]
    assert paper['doi'] == '10.1234/test'
    assert paper['fulltext_read'] is False
    assert paper['retracted'] is True
    assert 'title_conflict' in paper['quality_issues']
    assert len(paper['records']) == 2


def test_partial_failure_not_empty_success():
    def handler(request):
        return httpx.Response(429) if request.url.host == 'api.crossref.org' else httpx.Response(200, json={'results':[]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = search_literature('test', client=client)
    assert result['status'] == 'partial'
    assert result['sources']['crossref']['status'] == 'rate_limited'
    assert result['papers'] == []


def test_malformed_response_is_error_and_missing_doi_not_merged():
    def handler(request):
        if request.url.host == 'api.openalex.org':
            return httpx.Response(200, json={'unexpected':True})
        return httpx.Response(200, json={'message':{'items':[{'title':['Same']},{'title':['Same']}]}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = search_literature('test', client=client)
    assert result['status'] == 'partial'
    assert len(result['papers']) == 2
    assert 'missing_doi' in result['papers'][0]['quality_issues']


def test_empty_query_rejected_before_network():
    with pytest.raises(ValueError):
        search_literature('   ')


@pytest.mark.parametrize('identifier_lookup', [False, True])
def test_openalex_null_location_source_preserves_metadata_and_oa_link(identifier_lookup):
    # OpenAlex permits a null source when the host is not a known Source.
    location = {'source': None, 'is_oa': True, 'license': 'cc-by',
                'pdf_url': 'https://example.org/paper.pdf', 'version': 'publishedVersion'}
    raw = {'doi': 'https://doi.org/10.5555/null-source', 'title': 'Hosted paper',
           'best_oa_location': location, 'locations': [location], 'type': 'article'}
    def handler(request):
        if request.url.host == 'api.crossref.org':
            return httpx.Response(404) if identifier_lookup else httpx.Response(200, json={'message': {'items': []}})
        return httpx.Response(200, json=raw if identifier_lookup else {'results': [raw]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = search_literature('10.5555/null-source' if identifier_lookup else 'Hosted paper', client=client)
    assert result['status'] == 'ok'
    assert result['sources']['openalex']['status'] == 'ok'
    paper = result['papers'][0]
    record = paper['records'][0]
    assert record['raw_record'] == raw
    assert record['title'] == 'Hosted paper'
    assert 'container-title' not in record['csl']
    assert len(record['oa_locations']) == 1
    assert record['oa_locations'][0]['url'] == location['pdf_url']
    assert record['oa_locations'][0]['license'] == 'cc-by'
    assert paper['fulltext_read'] is False


def test_doi_lookup_is_exact_and_404_is_missing_not_fuzzy_search():
    seen = []
    def handler(request):
        seen.append(str(request.url))
        assert not request.url.query
        return httpx.Response(404)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = search_literature('https://doi.org/10.5555/not-real', client=client)
    assert len(seen) == 2
    assert result['status'] == 'ok'
    assert result['papers'] == []


def test_doi_lookup_rejects_unrelated_response():
    def handler(request):
        raw = {'DOI': '10.1111/wrong', 'doi': 'https://doi.org/10.1111/wrong', 'title': ['Wrong']}
        return httpx.Response(200, json={'message':raw} if request.url.host == 'api.crossref.org' else raw)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = search_literature('10.5555/requested', client=client)
    assert result['status'] == 'error'
    assert not result['papers']


@pytest.mark.parametrize('source,kind,expected', [
    ('crossref', 'journal-article', 'article-journal'),
    ('crossref', 'dissertation', 'thesis'),
    ('crossref', 'book', 'book'),
    ('crossref', 'monograph', 'book'),
    ('crossref', 'edited-book', 'book'),
    ('crossref', 'reference-book', 'book'),
    ('crossref', 'book-chapter', 'chapter'),
    ('crossref', 'proceedings-article', 'paper-conference'),
    ('crossref', 'report', 'report'),
    ('crossref', 'dataset', 'dataset'),
    ('crossref', 'standard', 'standard'),
    ('crossref', 'posted-content', 'article'),
    ('openalex', 'article', 'article-journal'),
    ('openalex', 'dissertation', 'thesis'),
    ('openalex', 'book', 'book'),
    ('openalex', 'book-chapter', 'chapter'),
    ('openalex', 'report', 'report'),
    ('openalex', 'dataset', 'dataset'),
    ('openalex', 'preprint', 'article'),
    ('crossref', 'future-unknown-type', 'document'),
    ('openalex', None, 'document'),
])
def test_metadata_types_do_not_invent_journal_articles(source, kind, expected):
    from lantai.research.literature import _record
    raw = {'type': kind, 'title': ['Example'] if source == 'crossref' else 'Example'}
    assert _record(source, raw)['csl']['type'] == expected


def test_crossref_dissertation_import_formats_as_thesis_without_inventing_year(tmp_path):
    from lantai.research.citations import format_citations
    from lantai.research.library import LibraryService
    from lantai.research.projects import ResearchService
    project = ResearchService(tmp_path).create_project('Question', [], policy='public_search', idempotency_key='thesis')
    library = LibraryService(tmp_path)
    # Mirrors missing publication dates seen in the frozen public benchmark.
    raw = {'DOI': '10.5555/thesis', 'type': 'dissertation', 'title': ['Example thesis'],
           'author': [{'family': 'Author'}], 'publisher': 'Example University',
           'issued': {'date-parts': [[None]]}, 'created': {'date-parts': [[2022, 6, 10]]},
           'approved': {'date-parts': [[2018, 8, 13]]}}
    def handler(request):
        return httpx.Response(200, json={'message': raw}) if request.url.host == 'api.crossref.org' else httpx.Response(404)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = library.search(project['project_id'], 1, '10.5555/thesis', client=client)
    assert result['papers'][0]['year'] is None
    assert result['papers'][0]['records'][0]['raw_record'] == raw
    imported = library.import_candidate(result['id'], 0)['items'][0]
    csl = library.work(imported['work_id'])['csl']
    formatted = format_citations([{**csl, 'id': 'thesis'}], [[{'id': 'thesis'}]], 'gb7714')
    assert '[D/OL]' in formatted['bibliography'][0]
    assert '[J]' not in formatted['bibliography'][0]
    assert formatted['audit']['metadata_warnings'] == [{'id': 'thesis', 'missing_fields': ['issued']}]
    assert 'TY  - THES' in library.export('ris')


@pytest.mark.parametrize('source', ['crossref', 'openalex'])
def test_empty_upstream_authors_remain_missing_and_do_not_block_import(source):
    from lantai.research.literature import _record
    from lantai.research.library import normalize_csl
    from lantai.research.citations import format_citations
    raw = ({'title': ['Example'], 'author': [{}, {'family': '', 'given': ''}], 'type': 'dissertation'}
           if source == 'crossref' else
           {'title': 'Example', 'authorships': [{'author': {'display_name': ''}}], 'type': 'dissertation'})
    record = _record(source, raw)
    assert record['authors'] == []
    csl = normalize_csl(record['csl'])
    formatted = format_citations([{**csl, 'id': 'missing'}], [[{'id': 'missing'}]], 'apa')
    assert formatted['audit']['metadata_warnings'] == [{'id': 'missing', 'missing_fields': ['author', 'issued']}]
