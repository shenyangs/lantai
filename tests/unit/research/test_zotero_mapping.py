import httpx

from lantai.research.library import LibraryService
from lantai.research.zotero import import_local_zotero


def fixture_client(*, changed=False, unavailable=False):
    requests = []
    items = [
        {'key': 'PAPER001', 'version': 7, 'data': {'itemType': 'journalArticle', 'title': 'Paper', 'collections': ['CHILD001'], 'tags': [{'tag': '方法', 'type': 0}]}},
        {'key': 'ATTACH01', 'version': 3, 'data': {'itemType': 'attachment', 'parentItem': 'PAPER001', 'title': 'paper.pdf', 'md5': 'external-file-hash'}},
        {'key': 'ANNO0001', 'version': 4, 'data': {'itemType': 'annotation', 'parentItem': 'ATTACH01', 'annotationText': 'quoted passage', 'annotationComment': 'my thought', 'annotationPageLabel': '12', 'annotationPosition': '{"pageIndex":11,"rects":[[1,2,3,4]]}', 'annotationType': 'highlight'}},
    ]
    def respond(request):
        requests.append(request)
        headers = {'Zotero-Server-ID': 'fixture-library', 'Last-Modified-Version': '8' if changed and request.url.path.endswith('/collections') else '7'}
        if request.url.path.endswith('/items'):
            data = items
        elif request.url.path.endswith('/collections'):
            if unavailable: return httpx.Response(404)
            data = [{'key': 'ROOT0001', 'version': 1, 'data': {'name': '研究', 'parentCollection': False}}, {'key': 'CHILD001', 'version': 2, 'data': {'name': '方法', 'parentCollection': 'ROOT0001'}}]
        else:
            data = {}
        return httpx.Response(200, json=data, headers=headers)
    return httpx.Client(transport=httpx.MockTransport(respond)), requests


def test_zotero_collection_paths_tags_and_stable_mapping(tmp_path):
    library = LibraryService(tmp_path)
    client, requests = fixture_client()
    result = import_local_zotero(library, client=client)
    assert result['status'] == 'ok'
    work = library.list()[0]
    assert work['organization']['collections'] == ['研究 / 方法']
    assert work['organization']['tags'] == ['方法']
    assert work['csl']['custom']['collection_mapping'][0]['key'] == 'CHILD001'
    assert work['csl']['custom']['collection_mapping'][0]['path'] == ['研究', '方法']
    library.organize(work['work_id'], 1, tags=['local'], collections=['local'])
    import_local_zotero(library, client=client)
    assert library.work(work['work_id'])['organization']['collections'] == ['local']
    assert all(r.method == 'GET' for r in requests)


def test_zotero_annotation_retains_external_version_and_position(tmp_path):
    library = LibraryService(tmp_path)
    client, _ = fixture_client()
    import_local_zotero(library, client=client)
    annotation = next(n for n in library.list()[0]['annotations'] if n['annotation_type'] == 'annotation')
    anchor = annotation['origin']['external_anchor']
    assert anchor['attachment_key'] == 'ATTACH01'
    assert anchor['attachment_version'] == 3
    assert anchor['page_label'] == '12'
    assert anchor['position']['pageIndex'] == 11
    assert anchor['quote'] == 'quoted passage'
    assert anchor['verified_against_local_source'] is False


def test_zotero_collection_revision_race_leaves_library_untouched(tmp_path):
    library = LibraryService(tmp_path)
    client, _ = fixture_client(changed=True)
    assert import_local_zotero(library, client=client)['status'] == 'retry_required'
    assert library.list() == []


def test_zotero_unavailable_collections_preserve_metadata_with_warning(tmp_path):
    library = LibraryService(tmp_path)
    client, _ = fixture_client(unavailable=True)
    result = import_local_zotero(library, client=client)
    assert result['status'] == 'partial'
    assert 'collections_unavailable' in result['warnings']
    assert library.list()[0]['csl']['custom']['collections'] == ['CHILD001']
