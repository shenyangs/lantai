import importlib.util
from pathlib import Path


def load():
    path = Path(__file__).resolve().parents[3] / 'scripts/research_public_benchmark.py'
    spec = importlib.util.spec_from_file_location('research_public_benchmark', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_registry_denominator_excludes_datacite_but_retains_failed_crossref():
    def row(doi, registry, state, returned):
        return {'kind': 'identifiers', 'expected_id': doi, 'registry': registry, 'seconds': 2,
            'result': {'sources': {'crossref': {'status': state}, 'openalex': {'status': 'error'}},
                'papers': [{'records': [{'source': 'crossref', 'doi': returned, 'title': 'Title',
                    'authors': [], 'year': None}]}] if returned else []}}
    result = load().summarize([row('10.1234/a', 'crossref', 'ok', '10.1234/a'),
        row('10.1234/b', 'crossref', 'error', None), row('10.1234/c', 'datacite', 'ok', None)])
    values = result['crossref']['identifiers']
    assert values['attempts'] == 3
    assert values['known_registry_coverage_attempts'] == 2
    assert values['known_registry_coverage_found'] == 1
    assert values['source_ok'] == 2
    assert values['expected_id_found'] == 1
    assert values['missing_fields']['authors'] == 1
    assert values['status_counts'] == {'error': 1, 'ok': 2}
    assert values['wrong_identifier_accepted'] == 0
    assert values['precision_at_10'] is None
    assert values['ndcg_at_10'] is None
    assert result['openalex']['identifiers']['known_registry_coverage_attempts'] is None


def test_wrong_identity_is_counted_and_topics_are_not_self_labeled():
    values = load().summarize([{'kind': 'identifiers', 'expected_id': '10.1234/a', 'registry': 'crossref', 'seconds': 1,
        'result': {'sources': {'crossref': {'status': 'ok'}, 'openalex': {'status': 'ok'}}, 'papers': [
            {'records': [{'source': 'crossref', 'doi': '10.1234/wrong'}]}]}}])
    assert values['crossref']['identifiers']['wrong_identifier_accepted'] == 1
    assert values['crossref']['topics']['expected_id_found'] is None
    assert values['crossref']['topics']['query_wall_p50_seconds'] is None


def test_frozen_public_dataset_counts_identity_and_unassigned_labels():
    import hashlib
    import json
    path = Path(__file__).resolve().parents[2] / 'fixtures/research/public-discovery-v1.json'
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == 'f27bf1e6fc4eafb6fec42c12533015ebabfb9c82cbc0624d8290e174915eff99'
    data = json.loads(raw)
    assert [len(data[k]) for k in ('identifiers', 'title_author', 'topics', 'fulltext')] == [80, 30, 30, 30]
    identifiers = {v['id'] for v in data['identifiers']}
    assert len(identifiers) == 80
    assert all(v['id'] in identifiers for v in data['title_author'])
    assert all(v['relevance_labels'] is None for v in data['topics'])
    assert len(data['requests']) == 6
