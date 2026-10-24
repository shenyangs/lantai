import httpx
from lantai.research.literature import search_literature
from lantai.research.store import ResearchStore
from lantai.research.discovery_cache import DiscoveryCache
from lantai.research.projects import ResearchService


def make_cache(path, key='project'):
    service = ResearchService(path)
    project = service.create_project('Question', [], policy='public_search', idempotency_key=key)
    return DiscoveryCache(service.store, project['project_id'])


def test_durable_negative_cache_has_original_retrieval_and_no_network(tmp_path):
    cache = make_cache(tmp_path)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(404)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = search_literature('10.5555/missing', client=client, cache=cache)
        second = search_literature('10.5555/missing', client=client, cache=DiscoveryCache(ResearchStore(tmp_path), cache.project))
    assert len(calls) == 2
    assert second['status'] == 'ok'
    for name, state in second['sources'].items():
        assert state['cache'] == 'hit' and state['negative_cache'] is True
        assert state['retrieved_at'] == first['sources'][name]['retrieved_at']


def test_retry_after_blocks_different_query_without_hiding_other_source(tmp_path):
    cache = make_cache(tmp_path)
    now = [1000.0]
    cache.clock = lambda: now[0]
    calls = []
    def handler(request):
        calls.append(request.url.host)
        if request.url.host == 'api.crossref.org':
            return httpx.Response(429, headers={'Retry-After': '120'})
        return httpx.Response(200, json={'results': []})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = search_literature('one', client=client, cache=cache)
        now[0] += 2
        second = search_literature('two', client=client, cache=cache)
    assert first['sources']['crossref']['retry_at'] == 1120
    assert second['sources']['crossref']['status'] == 'rate_limited'
    assert second['sources']['crossref']['network_sent'] is False
    assert second['sources']['openalex']['status'] == 'ok'
    assert calls.count('api.crossref.org') == 1


def test_cache_expiry_and_project_isolation(tmp_path):
    cache = make_cache(tmp_path, 'a')
    now = [1000.0]
    cache.clock = lambda: now[0]
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(404)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        search_literature('10.5555/missing', client=client, cache=cache)
        now[0] += 3601
        search_literature('10.5555/missing', client=client, cache=cache)
        other = make_cache(tmp_path, 'b'); other.clock = lambda: now[0] + 2
        search_literature('10.5555/missing', client=client, cache=other)
    assert len(calls) == 6


def test_circuit_breaker_and_success_recovery(tmp_path):
    cache = make_cache(tmp_path)
    now = [1000.0]; cache.clock = lambda: now[0]
    failing = [True]
    def handler(request):
        if request.url.host == 'api.crossref.org' and failing[0]:
            return httpx.Response(503)
        return httpx.Response(200, json={'results': [], 'message': {'items': []}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        for i in range(3):
            result = search_literature(str(i), client=client, cache=cache)
            now[0] += 10
        blocked = search_literature('blocked', client=client, cache=cache)
        assert blocked['sources']['crossref']['status'] == 'circuit_open'
        assert blocked['status'] == 'partial'
        failing[0] = False; now[0] += 301
        recovered = search_literature('recovered', client=client, cache=cache)
        assert recovered['status'] == 'ok'


def test_purge_removes_cache_and_inflight_result_cannot_restore_it(tmp_path):
    cache = make_cache(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))) as client:
        search_literature('10.5555/missing', client=client, cache=cache)
    with cache.store.transaction() as db:
        assert db.execute('SELECT count(*) FROM research_discovery_cache').fetchone()[0] == 2
    service = ResearchService(tmp_path)
    service.archive_project(cache.project, 1)
    service.delete_project(cache.project, 1)
    cache.complete('crossref', 'late response', 5, {'status': 'ok'}, [])
    with cache.store.transaction() as db:
        assert db.execute('SELECT count(*) FROM research_discovery_cache').fetchone()[0] == 0


def test_inflight_admission_and_stale_completion_cannot_reset_new_cooldown(tmp_path):
    first = make_cache(tmp_path)
    second = DiscoveryCache(first.store, first.project)
    now = [1000.0]; first.clock = second.clock = lambda: now[0]
    assert first.begin('crossref', 'one', 5) is None
    busy = second.begin('crossref', 'two', 5)
    assert busy[0]['status'] == 'busy' and busy[0]['network_sent'] is False
    now[0] += 121
    assert second.begin('crossref', 'two', 5) is None
    second.complete('crossref', 'two', 5, {'status': 'rate_limited'}, [], '120')
    first.complete('crossref', 'one', 5, {'status': 'ok'}, [])
    blocked = first.begin('crossref', 'three', 5)
    assert blocked[0]['status'] == 'rate_limited'


def test_retry_after_http_date_and_invalid_values():
    from lantai.research.discovery_cache import retry_delay
    assert retry_delay('Thu, 01 Jan 1970 00:20:00 GMT', 1000) == 200
    assert retry_delay('yesterday', 1000) == 0


import pytest


@pytest.mark.parametrize('sanitized', [False, True])
def test_backup_restores_cache_and_cooldown(tmp_path, sanitized):
    import zipfile
    from types import SimpleNamespace
    from lantai.backup._ops.create_helpers import add_agent_workspaces
    original = tmp_path / 'original'
    cache = make_cache(original)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404))) as client:
        search_literature('10.5555/missing', client=client, cache=cache)
    archive = tmp_path / 'backup.zip'
    with zipfile.ZipFile(archive, 'w') as output:
        assert add_agent_workspaces(output, [('researcher', SimpleNamespace(workspace_dir=original))], sanitized=sanitized)
    restored = tmp_path / 'restored'
    with zipfile.ZipFile(archive) as saved:
        index = next(name for name in saved.namelist() if name.endswith('.lantai/research/index.sqlite3'))
        path = restored / '.lantai/research/index.sqlite3'; path.parent.mkdir(parents=True)
        path.write_bytes(saved.read(index))
    restored_cache = DiscoveryCache(ResearchStore(restored), cache.project)
    def no_network(request):
        raise AssertionError('restored cache must avoid network')
    with httpx.Client(transport=httpx.MockTransport(no_network)) as client:
        result = search_literature('10.5555/missing', client=client, cache=restored_cache)
    assert result['status'] == 'ok'
    assert all(s['cache'] == 'hit' for s in result['sources'].values())
    with restored_cache.store.transaction() as db:
        assert db.execute('SELECT count(*) FROM research_discovery_sources').fetchone()[0] == 2


def test_raw_metadata_and_cached_import_origin_keep_original_time(tmp_path):
    from lantai.research.library import LibraryService
    cache = make_cache(tmp_path)
    raw = {'DOI': '10.5555/test', 'title': ['Test'], 'author': [{'family': 'Author'}]}
    def handler(request):
        return httpx.Response(200, json={'message': raw}) if request.url.host == 'api.crossref.org' else httpx.Response(404)
    library = LibraryService(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = library.search(cache.project, 1, 'doi:10.5555/test', client=client)
        second = library.search(cache.project, 1, 'doi:10.5555/test', client=client)
    record = second['papers'][0]['records'][0]
    assert record['raw_record'] == raw and record['raw_record_hash']
    assert second['sources']['crossref']['pagination'] == 'identifier_lookup'
    imported = library.import_candidate(second['id'], 0)
    origin = library.work(imported['items'][0]['work_id'])['origins'][0]
    assert origin['retrieved_at'] == first['sources']['crossref']['retrieved_at']
    assert origin['cache'] == 'hit'


def test_corrupt_cache_not_returned_as_valid_metadata(tmp_path):
    cache = make_cache(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))) as client:
        search_literature('10.5555/missing', client=client, cache=cache)
        with cache.store.transaction() as db:
            db.execute("UPDATE research_discovery_cache SET payload='{}'")
        with pytest.raises(ValueError, match='integrity'):
            search_literature('10.5555/missing', client=client, cache=cache)


def test_normalization_upgrade_ignores_old_cache_but_preserves_source_cooldown(tmp_path, monkeypatch):
    import lantai.research.discovery_cache as module
    cache = make_cache(tmp_path)
    now = [1000.0]; cache.clock = lambda: now[0]
    with monkeypatch.context() as old:
        old.setattr(module, 'VERSION', 'public-metadata-v2')
        with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404))) as client:
            search_literature('10.5555/missing', client=client, cache=cache)
    calls = []
    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(404)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        blocked = search_literature('10.5555/missing', client=client, cache=cache)
        assert all(s.get('reason') == 'source_cooldown' for s in blocked['sources'].values())
        assert calls == []
        now[0] += 2
        refreshed = search_literature('10.5555/missing', client=client, cache=cache)
    assert len(calls) == 2
    assert all(s['cache'] == 'miss' for s in refreshed['sources'].values())
