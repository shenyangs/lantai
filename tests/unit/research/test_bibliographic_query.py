"""Public bibliographic fields must survive approval and cache boundaries."""
import json
import httpx
import pytest
from lantai.research.literature import query_requests, search_literature
from lantai.research.library import LibraryService
from lantai.research.projects import ResearchService
from lantai.research.discovery_cache import DiscoveryCache


def test_explicit_fields_target_upstream_fields_and_preserve_default_search():
    plan = query_requests('A trial', 3, query_mode='title_author', author='Janet L. Peacock')
    assert plan['crossref']['params'] == {'query.bibliographic': 'A trial', 'query.author': 'Janet L. Peacock', 'rows': 3}
    assert plan['openalex']['params'] == {'search.title': 'A trial', 'filter': 'raw_author_name.search:"Janet L. Peacock"', 'per-page': 3}
    assert query_requests('A trial')['openalex']['params']['search'] == 'A trial'
    assert 'filter' not in query_requests('A trial', query_mode='title_author')['openalex']['params']
    assert 'search.title' in query_requests('10.5555/title', query_mode='title_author')['openalex']['params']


@pytest.mark.parametrize('kwargs', [
    {'query_mode': 'unknown'}, {'author': 'Someone'},
    {'query_mode': 'title_author', 'author': 'Name,is_retracted:true'},
    {'query_mode': 'title_author', 'author': '"Name" OR *'},
    {'query_mode': 'title_author', 'author': 'x' * 201},
])
def test_invalid_options_fail_before_network(kwargs):
    with pytest.raises(ValueError):
        query_requests('Title', **kwargs)


def test_approval_preserves_exact_fields_after_reload_and_only_sends_once(tmp_path):
    project = ResearchService(tmp_path).create_project('Private question', [], policy='public_search', query_preview_required=True, idempotency_key='p')
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'message': {'items': []}, 'results': []})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        preview = LibraryService(tmp_path).search(project['project_id'], 1, 'Public title', query_mode='title_author', author='A B', client=client)
        assert not calls
        reloaded = LibraryService(tmp_path)
        saved = reloaded.query_previews(project['project_id'])['items'][0]
        assert saved['author'] == 'A B' and saved['query_mode'] == 'title_author'
        result = reloaded.decide_query(preview['id'], True, client=client)
        assert len(calls) == 2
        assert result['author'] == 'A B' and result['query_mode'] == 'title_author'
        for request in calls:
            source = 'crossref' if request.url.host == 'api.crossref.org' else 'openalex'
            assert dict(request.url.params) == {k: str(v) for k, v in saved['requests'][source]['params'].items()}
            assert 'Private question' not in str(request.url)
        with pytest.raises(ValueError):
            reloaded.decide_query(preview['id'], True, client=client)
        assert len(calls) == 2


def test_cache_separates_modes_and_authors(tmp_path):
    service = ResearchService(tmp_path)
    p = service.create_project('Question', [], policy='public_search', idempotency_key='p')
    cache = DiscoveryCache(service.store, p['project_id'])
    now = [1000.0]; cache.clock = lambda: now[0]
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'message': {'items': []}, 'results': []})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        for options in ({}, {'query_mode': 'title_author'}, {'query_mode': 'title_author', 'author': 'A B'}, {'query_mode': 'title_author', 'author': 'C D'}):
            first = search_literature('Title', cache=cache, client=client, **options)
            again = search_literature('Title', cache=cache, client=client, **options)
            assert all(s['cache'] == 'hit' for s in again['sources'].values())
            assert first['sources']['openalex']['request'] == again['sources']['openalex']['request']
            now[0] += 2
    assert len(calls) == 8


@pytest.mark.asyncio
async def test_tool_and_api_accept_explicit_fields_without_approving(tmp_path):
    from lantai.agents.tools.research import research_library
    from lantai.app.routers.research import LiteratureSearchRequest
    from lantai.config.context import WorkspaceContext, current_workspace_context
    p = ResearchService(tmp_path).create_project('Question', [], policy='public_search', query_preview_required=True, idempotency_key='p')
    args = LiteratureSearchRequest(project_id=p['project_id'], revision=1, query='Title', query_mode='title_author', author='A B').model_dump()
    token = current_workspace_context.set(WorkspaceContext(tmp_path, tmp_path, tmp_path))
    try:
        reply = await research_library('search', **args)
        value = json.loads(reply.content[0]['text'])
        assert value['status'] == 'approval_required'
        assert value['requests']['openalex']['params']['filter'] == 'raw_author_name.search:"A B"'
    finally:
        current_workspace_context.reset(token)
