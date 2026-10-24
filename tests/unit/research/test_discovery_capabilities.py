import httpx
import pytest

from lantai.research.literature import search_literature


def snapshot():
    doi = '10.1234/example'
    def handler(request):
        if request.url.host == 'api.crossref.org':
            return httpx.Response(200, json={'message': {'DOI': doi, 'title': ['Example'],
                'relation': {'is-preprint-of': [{'id': '10.1234/final', 'id-type': 'doi'}]}}})
        return httpx.Response(200, json={'doi': 'https://doi.org/' + doi, 'title': 'Example',
            'referenced_works': ['https://openalex.org/W1'], 'locations': [
                {'is_oa': True, 'pdf_url': 'https://example.org/paper.pdf', 'license': 'cc-by'}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return search_literature(doi, client=client)


def test_capability_receipts_preserve_identity_provenance_and_limits():
    from lantai.research.discovery import capability_result
    result = snapshot()
    metadata = capability_result(result, 'crossref', 'get_metadata')
    assert metadata['status'] == 'ok' and metadata['count'] == 1
    assert metadata['items'][0]['doi'] == '10.1234/example'
    assert metadata['raw_record_refs'][0]['sha256']
    assert metadata['retrieved_at'] == result['retrieved_at']
    assert metadata['connector_version'] == 'public-metadata-v5'
    relations = capability_result(result, 'crossref', 'get_relations')
    assert relations['items'][0]['relations']['is-preprint-of'][0]['id'] == '10.1234/final'
    assert relations['completeness'] == 'source_snapshot_only'
    fulltext = capability_result(result, 'openalex', 'locate_fulltext')
    assert fulltext['items'][0]['locations'][0]['url'] == 'https://example.org/paper.pdf'
    assert fulltext['fulltext_fetched'] is False


def test_unavailable_is_distinct_from_empty_and_failure():
    from lantai.research.discovery import capability_result
    result = snapshot()
    assert capability_result(result, 'crossref', 'locate_fulltext')['status'] == 'unavailable'
    result['sources']['openalex'] = {'status': 'rate_limited', 'http_status': 429, 'retry_at': 42}
    receipt = capability_result(result, 'openalex', 'get_metadata')
    assert receipt['status'] == 'rate_limited' and receipt['items'] == []
    assert receipt['retry_at'] == 42 and receipt['http_status'] == 429
    result['query'] = 'title search'
    assert capability_result(result, 'crossref', 'resolve_identifier')['status'] == 'unavailable'


def test_saved_capability_read_is_local_and_does_not_bypass_preview(tmp_path):
    from lantai.research.projects import ResearchService
    from lantai.research.library import LibraryService
    service = ResearchService(tmp_path)
    p = service.create_project('Question', [], policy='public_search', query_preview_required=True, idempotency_key='p')
    library = LibraryService(tmp_path)
    preview = library.search(p['project_id'], 1, 'query')
    with pytest.raises(ValueError, match='wrong research record type'):
        library.discovery_result(preview['id'], 'crossref', 'search')
    with library.store.transaction() as db:
        saved = library.store.put(db, 'search', snapshot(), p['project_id'], 1)
    assert library.discovery_result(saved['id'], 'openalex', 'get_relations')['items'][0]['referenced_works'] == ['https://openalex.org/W1']
    with pytest.raises(FileNotFoundError):
        LibraryService(tmp_path / 'other-agent').discovery_result(saved['id'], 'openalex', 'search')


@pytest.mark.asyncio
async def test_api_and_model_tool_read_same_saved_receipt(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from lantai.app.routers import research as api
    from lantai.agents.tools import research as tool
    from lantai.research.projects import ResearchService
    from lantai.research.library import LibraryService
    library = LibraryService(tmp_path)
    with library.store.transaction() as db:
        record = library.store.put(db, 'search', snapshot())
    async def agent(request):
        return SimpleNamespace(workspace_dir=tmp_path)
    monkeypatch.setattr(api, 'get_agent_for_request', agent)
    monkeypatch.setattr(tool, '_service', lambda: ResearchService(tmp_path))
    app = FastAPI(); app.include_router(api.router)
    client = TestClient(app)
    path = '/workspace/research/library/searches/' + record['id']
    response = client.get(path + '/crossref/locate_fulltext')
    assert response.status_code == 200 and response.json()['status'] == 'unavailable'
    assert client.get(path + '/unknown/search').status_code == 422
    value = await tool.research_library('discovery', search_id=record['id'], source='crossref', capability='locate_fulltext')
    assert json.loads(value.content[0]['text']) == response.json()
